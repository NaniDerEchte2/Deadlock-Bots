# cogs/live_match/steam_link_oauth.py
import os
import re
import time
import uuid
import logging
from typing import Dict, Optional, List
from urllib.parse import urlencode, urljoin

import aiohttp
from aiohttp import web

import discord
from discord.ext import commands

from shared import db

log = logging.getLogger("SteamLink")

DISCORD_API = "https://discord.com/api"

# ----------------------- ENV / Defaults --------------------------------------
def _env_client_id(bot: commands.Bot) -> str:
    cid = (os.getenv("DISCORD_OAUTH_CLIENT_ID") or "").strip()
    if cid:
        return cid
    app_id = getattr(bot, "application_id", None)
    return str(app_id) if app_id else ""

def _env_redirect() -> str:
    explicit = (os.getenv("DISCORD_OAUTH_REDIRECT") or "").strip()
    if explicit:
        return explicit
    public_base = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
    if public_base:
        return f"{public_base}/discord/callback"
    host = os.getenv("HTTP_HOST", "127.0.0.1").strip()
    port = int(os.getenv("STEAM_OAUTH_PORT", os.getenv("HTTP_PORT", "8888")))
    scheme = "http" if host.startswith(("127.", "0.", "localhost")) else "https"
    return f"{scheme}://{host}:{port}/discord/callback"

HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("STEAM_OAUTH_PORT", os.getenv("HTTP_PORT", "8888")))
CLIENT_SECRET = (os.getenv("DISCORD_OAUTH_CLIENT_SECRET") or "").strip()

# ---- Steam OpenID Settings ---------------------------------------------------
STEAM_OPENID_ENDPOINT = "https://steamcommunity.com/openid/login"  # zwingend https!
OPENID_NS = "http://specs.openid.net/auth/2.0"
IDENTIFIER_SELECT = "http://specs.openid.net/auth/2.0/identifier_select"
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
STEAM_RETURN_PATH = os.getenv("STEAM_RETURN_PATH", "/steam/return")
STEAM_RETURN_URL = (
    urljoin(PUBLIC_BASE_URL + "/", STEAM_RETURN_PATH.lstrip("/"))
    if PUBLIC_BASE_URL else ""
)

# ----------------------- DB-Schema -------------------------------------------
def _ensure_schema() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS steam_links(
          user_id         INTEGER NOT NULL,
          steam_id        TEXT    NOT NULL,
          name            TEXT,
          verified        INTEGER DEFAULT 0,
          primary_account INTEGER DEFAULT 0,
          created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
          updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (user_id, steam_id)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_user ON steam_links(user_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_steam ON steam_links(steam_id)")

def _save_steam_link_row(user_id: int, steam_id: str, name: str = "", verified: int = 0) -> None:
    db.execute(
        """
        INSERT INTO steam_links(user_id, steam_id, name, verified)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id, steam_id) DO UPDATE SET
          name=excluded.name,
          verified=excluded.verified,
          updated_at=CURRENT_TIMESTAMP
        """,
        (int(user_id), str(steam_id), name or "", int(verified)),
    )

# ----------------------- Cog --------------------------------------------------
class SteamLink(commands.Cog):
    """
    Flow:
      1) /link → Discord OAuth2 (identify + connections)
      2) Wenn 0 Steam-Accounts in Discord: Fallback-Seite → Steam OpenID
      3) Optional: /link_steam → direkt Steam OpenID
      4) /steam/return → SteamID64 extrahieren → speichern
      5) Nach Erfolg: DM an den User
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app = web.Application()
        # HTTP-Routen
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/discord/callback", self.handle_discord_callback)
        self.app.router.add_get("/steam/login", self.handle_steam_login)     # manuell anstoßbar
        self.app.router.add_get(STEAM_RETURN_PATH, self.handle_steam_return) # OpenID-Return
        self._runner: Optional[web.AppRunner] = None

        # In-Memory states (10 min)
        self._states: Dict[str, Dict[str, float]] = {}  # state -> {uid, ts}

    # --------------- Lifecycle -----------------------------------------------
    async def cog_load(self) -> None:
        _ensure_schema()

        cid = _env_client_id(self.bot)
        if not cid:
            log.warning("Discord OAuth CLIENT_ID fehlt (DISCORD_OAUTH_CLIENT_ID oder bot.application_id).")
        if not CLIENT_SECRET:
            log.warning("DISCORD_OAUTH_CLIENT_SECRET fehlt – Token-Exchange wird scheitern.")

        if not PUBLIC_BASE_URL:
            log.error("PUBLIC_BASE_URL ist NICHT gesetzt – Steam OpenID wird verweigert. Setze z. B. https://link.earlysalty.com")
        else:
            log.info("Steam OpenID return_to: %s", STEAM_RETURN_URL)

        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=HTTP_HOST, port=HTTP_PORT)
        await site.start()
        log.info(
            "OAuth/OpenID Callback-Server läuft auf %s:%s (Discord redirect=%s)",
            HTTP_HOST, HTTP_PORT, _env_redirect()
        )

    async def cog_unload(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # --------------- Helpers --------------------------------------------------
    def _mk_state(self, uid: int) -> str:
        s = uuid.uuid4().hex
        self._states[s] = {"uid": int(uid), "ts": time.time()}
        return s

    def _pop_state(self, s: str) -> Optional[int]:
        data = self._states.pop(s, None)
        if not data:
            return None
        if time.time() - data["ts"] > 600:
            return None
        return int(data["uid"])

    async def _notify_user_linked(self, user_id: int, steam_ids: List[str]) -> None:
        """Schickt dem User eine DM über den Bot. Ignoriert Fehler still (DMs ggf. deaktiviert)."""
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            if not user:
                return
            if steam_ids:
                sids = ", ".join(steam_ids)
                txt = f"✅ Deine Verknüpfung war erfolgreich.\nVerknüpfte SteamID(s): **{sids}**"
            else:
                txt = "✅ Deine Verknüpfung war erfolgreich."
            await user.send(txt)
        except Exception as e:
            log.info("Konnte User-DM nicht senden (id=%s): %s", user_id, e)

    def _build_discord_auth_url(self, uid: int) -> str:
        client_id = _env_client_id(self.bot)
        redirect_uri = _env_redirect()
        if not client_id:
            raise RuntimeError("DISCORD_OAUTH_CLIENT_ID/bot.application_id nicht gesetzt")
        if not redirect_uri:
            raise RuntimeError("DISCORD_OAUTH_REDIRECT/PUBLIC_BASE_URL/HTTP_HOST fehlen")

        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "identify connections",
            "prompt": "consent",
            "state": self._mk_state(uid),
        }
        return f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}"

    # ---------- Discord OAuth helpers ----------------------------------------
    async def _discord_token_exchange(self, code: str) -> Optional[dict]:
        client_id = _env_client_id(self.bot)
        redirect_uri = _env_redirect()
        if not client_id or not CLIENT_SECRET:
            return None

        data = {
            "client_id": client_id,
            "client_secret": CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async with aiohttp.ClientSession() as s:
            async with s.post(f"{DISCORD_API}/oauth2/token", data=data, headers=headers) as r:
                if r.status != 200:
                    body = await r.text()
                    log.warning("Discord Token-Exchange fehlgeschlagen (%s): %s", r.status, body)
                    return None
                return await r.json()

    async def _discord_fetch_connections(self, access_token: str) -> Optional[List[dict]]:
        headers = {"Authorization": f"Bearer {access_token}"}
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{DISCORD_API}/users/@me/connections", headers=headers) as r:
                if r.status != 200:
                    body = await r.text()
                    log.warning("Discord Connections-API fehlgeschlagen (%s): %s", r.status, body)
                    return None
                return await r.json()

    async def _save_steam_links_from_discord(self, user_id: int, conns: List[dict]) -> List[str]:
        """Speichert Steam-Links und gibt die gespeicherten SteamIDs zurück."""
        saved_ids: List[str] = []
        if not conns:
            return saved_ids
        for c in conns:
            if c.get("type") != "steam":
                continue
            steam_id = str(c.get("id") or "").strip()
            if not steam_id:
                continue
            name = (c.get("name") or "").strip()
            verified = 1 if c.get("verified") else 0
            _save_steam_link_row(user_id, steam_id, name, verified)
            saved_ids.append(steam_id)
        return saved_ids

    # ---- Steam OpenID helpers (HART auf PUBLIC_BASE_URL) ---------------------
    def _require_public_base(self) -> None:
        if not PUBLIC_BASE_URL:
            raise RuntimeError(
                "PUBLIC_BASE_URL ist nicht gesetzt. Setze z. B. PUBLIC_BASE_URL=https://link.earlysalty.com"
            )

    def _steam_return_to(self, state: str) -> str:
        self._require_public_base()
        return f"{urljoin(PUBLIC_BASE_URL + '/', STEAM_RETURN_PATH.lstrip('/'))}?{urlencode({'state': state})}"

    def _steam_realm(self) -> str:
        self._require_public_base()
        return PUBLIC_BASE_URL  # z. B. https://link.earlysalty.com

    def _build_steam_login_url(self, state: str) -> str:
        self._require_public_base()
        params = {
            "openid.ns": OPENID_NS,
            "openid.mode": "checkid_setup",
            "openid.return_to": self._steam_return_to(state),
            "openid.realm": self._steam_realm(),
            "openid.identity": IDENTIFIER_SELECT,
            "openid.claimed_id": IDENTIFIER_SELECT,
        }
        url = f"{STEAM_OPENID_ENDPOINT}?{urlencode(params)}"
        safe = url.replace(state, "[state]")
        log.info("Steam OpenID URL (safe): %s", safe)
        return url

    async def _verify_steam_openid(self, request: web.Request) -> Optional[str]:
        query = dict(request.query)
        if query.get("openid.mode") != "id_res":
            return None

        verify_params = query.copy()
        verify_params["openid.mode"] = "check_authentication"

        async with aiohttp.ClientSession() as session:
            async with session.post(STEAM_OPENID_ENDPOINT, data=verify_params, timeout=15) as resp:
                body = await resp.text()
                if resp.status != 200 or "is_valid:true" not in body:
                    log.warning("Steam OpenID verify fehlgeschlagen: HTTP=%s body=%s", resp.status, body)
                    return None

        claimed_id = query.get("openid.claimed_id", "")
        m = re.search(r"/openid/id/(\d+)$", claimed_id)
        if m:
            return m.group(1)
        m = re.search(r"/id/(\d+)$", claimed_id)
        return m.group(1) if m else None

    # --------------- HTTP-Handler --------------------------------------------
    async def handle_index(self, request: web.Request) -> web.Response:
        html = (
            "<html><body style='font-family: system-ui, sans-serif'>"
            "<h2>Deadlock Bot – Link Service</h2>"
            "<p>✅ Server läuft. Nutze im Discord <code>/link</code> oder <code>/link_steam</code>.</p>"
            "<p><a href='/health'>Health-Check</a></p>"
            "</body></html>"
        )
        return web.Response(text=html, content_type="text/html")

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "ts": int(time.time())})

    async def handle_discord_callback(self, request: web.Request) -> web.Response:
        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state:
            return web.Response(text="missing code/state", status=400)

        uid = self._pop_state(state)
        if not uid:
            return web.Response(text="invalid/expired state", status=400)

        token = await self._discord_token_exchange(code)
        if not token:
            return web.Response(text="token exchange failed", status=400)

        at = token.get("access_token")
        if not at:
            return web.Response(text="no access_token", status=400)

        conns = await self._discord_fetch_connections(at)
        if conns is None:
            return web.Response(text="connections fetch failed", status=400)

        saved_ids = await self._save_steam_links_from_discord(uid, conns)
        if saved_ids:
            # DM senden
            await self._notify_user_linked(uid, saved_ids)
            html = (
                "<html><body style='font-family: system-ui, sans-serif'>"
                "<h3>✅ Verknüpfung abgeschlossen</h3>"
                f"<p>{len(saved_ids)} Steam-Account(s) wurden gespeichert.</p>"
                "<p>Du kannst dieses Fenster jetzt schließen.</p>"
                "</body></html>"
            )
            return web.Response(text=html, content_type="text/html")

        # --- Fallback: Steam OpenID über Zwischenseite (Meta-Refresh + Button)
        steam_state = self._mk_state(uid)
        steam_login = self._build_steam_login_url(steam_state)
        html = (
            "<html><head>"
            "<meta http-equiv='refresh' content='1; url=" + steam_login + "'/>"
            "</head><body style='font-family: system-ui, sans-serif'>"
            "<h3>Kein Steam-Account in deinen Discord-Verknüpfungen gefunden.</h3>"
            "<p>Bitte melde dich kurz bei Steam an, um deinen Account zu bestätigen.</p>"
            f"<p><a href='{steam_login}' style='padding:10px 14px;"
            "background:#2a475e;color:#fff;border-radius:6px;text-decoration:none;'>Bei Steam anmelden</a></p>"
            "<p><small>Falls eine Schutzsoftware blockt, öffne den Link ggf. in einem privaten Fenster oder über Mobilnetz.</small></p>"
            "</body></html>"
        )
        return web.Response(text=html, content_type="text/html")

    async def handle_steam_login(self, request: web.Request) -> web.Response:
        # Optional: manuell anstoßbar – erwartet ?uid=<discord_id>
        uid_q = request.query.get("uid")
        if not uid_q or not uid_q.isdigit():
            return web.Response(text="missing uid", status=400)
        uid = int(uid_q)
        s = self._mk_state(uid)
        login_url = self._build_steam_login_url(s)
        html = (
            "<html><body style='font-family: system-ui, sans-serif'>"
            "<h3>Weiter zu Steam</h3>"
            f"<p><a href='{login_url}'>Steam Login öffnen</a></p>"
            "</body></html>"
        )
        return web.Response(text=html, content_type="text/html")

    async def handle_steam_return(self, request: web.Request) -> web.Response:
        try:
            state = request.query.get("state", "")
            uid = self._pop_state(state)
            if not uid:
                return web.Response(text="invalid/expired state", status=400)

            steam_id = await self._verify_steam_openid(request)
            if not steam_id:
                return web.Response(text="OpenID validation failed", status=400)

            _save_steam_link_row(uid, steam_id)

            # DM senden
            await self._notify_user_linked(uid, [steam_id])

            body = (
                "<h3>✅ Verknüpfung abgeschlossen</h3>"
                f"<p>Deine SteamID64 ist: <b>{steam_id}</b>.</p>"
                "<p>Du kannst dieses Fenster schließen und zu Discord zurückkehren.</p>"
            )
            return web.Response(text=body, content_type="text/html")

        except Exception as e:
            log.exception("Fehler im Steam-Return")
            return web.Response(text=f"Fehler: {e}", status=500)

    # --------------- Commands -------------------------------------------------
    @commands.hybrid_command(name="link", description="Verknüpfe deine Steam-Accounts (Discord → connections; Fallback Steam OpenID)")
    async def link(self, ctx: commands.Context) -> None:
        try:
            url = self._build_discord_auth_url(ctx.author.id)
        except Exception as e:
            await self._send_ephemeral(ctx, f"❌ OAuth-Fehler: `{e}` – prüfe .env & Dev-Portal Redirect.")
            return

        msg = (
            "🔗 **Klicke zum Verknüpfen (Discord OAuth2):**\n"
            f"{url}\n\n"
            "• Scopes: `identify`, `connections`\n"
            "• Falls dort **kein Steam** hinterlegt ist, leite ich dich auf die Steam-Anmeldeseite weiter.\n"
            "• **Ich schicke dir eine DM**, sobald die Verknüpfung durch ist."
        )
        await self._send_ephemeral(ctx, msg)

    @commands.hybrid_command(name="link_steam", description="Direkt: Steam-Login (OpenID) starten")
    async def link_steam(self, ctx: commands.Context) -> None:
        s = self._mk_state(ctx.author.id)
        login_url = self._build_steam_login_url(s)
        await self._send_ephemeral(ctx, f"🔗 **Direkt zu Steam:**\n{login_url}\n\n• **Ich schicke dir eine DM**, sobald die Verknüpfung durch ist.")

    @commands.hybrid_command(name="links", description="Zeigt deine gespeicherten Steam-Links")
    async def links(self, ctx: commands.Context) -> None:
        rows = db.query_all(
            "SELECT steam_id, name, verified, primary_account "
            "FROM steam_links WHERE user_id=? "
            "ORDER BY primary_account DESC, updated_at DESC",
            (ctx.author.id,),
        )
        if not rows:
            await self._send_ephemeral(ctx, "Keine Steam-Links gefunden. Nutze `/link` oder `/link_steam`.")
            return
        lines = []
        for r in rows:
            sid = r["steam_id"]
            nm = r["name"] or "—"
            chk = " ✅" if r["verified"] else ""
            prim = " [primary]" if r["primary_account"] else ""
            lines.append(f"- **{sid}** ({nm}){chk}{prim}")
        await self._send_ephemeral(ctx, "Deine verknüpften Accounts:\n" + "\n".join(lines))

    @commands.hybrid_command(name="unlink", description="Entfernt einen Steam-Link")
    async def unlink(self, ctx: commands.Context, steam_id: str) -> None:
        db.execute("DELETE FROM steam_links WHERE user_id=? AND steam_id=?", (ctx.author.id, steam_id))
        await self._send_ephemeral(ctx, f"Entfernt: `{steam_id}`")

    @commands.hybrid_command(name="setprimary", description="Markiert einen Steam-Account als Primär")
    async def setprimary(self, ctx: commands.Context, steam_id: str) -> None:
        db.execute("UPDATE steam_links SET primary_account=0 WHERE user_id=?", (ctx.author.id,))
        db.execute(
            "UPDATE steam_links SET primary_account=1, updated_at=CURRENT_TIMESTAMP "
            "WHERE user_id=? AND steam_id=?",
            (ctx.author.id, steam_id),
        )
        await self._send_ephemeral(ctx, f"✅ Primär gesetzt: `{steam_id}`")

    async def _send_ephemeral(self, ctx: commands.Context, content: str) -> None:
        if getattr(ctx, "interaction", None) and not ctx.interaction.response.is_done():
            await ctx.interaction.response.send_message(content, ephemeral=True)
        elif getattr(ctx, "interaction", None):
            await ctx.interaction.followup.send(content, ephemeral=True)
        else:
            await ctx.reply(content)


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamLink(bot))
