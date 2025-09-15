# cogs/live_match/steam_link_oauth.py
import os
import re
import time
import uuid
import logging
import html
import asyncio
from typing import Dict, Optional, List, Union
from urllib.parse import urlencode, urljoin, urlparse

import aiohttp
from aiohttp import web

import discord
from discord.ext import commands

from shared import db

log = logging.getLogger("SteamLink")

DISCORD_API = "https://discord.com/api"
STEAM_API_BASE = "https://api.steampowered.com"
STEAM_OPENID_ENDPOINT = "https://steamcommunity.com/openid/login"
OPENID_NS = "http://specs.openid.net/auth/2.0"
IDENTIFIER_SELECT = "http://specs.openid.net/auth/2.0/identifier_select"

PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
STEAM_RETURN_PATH = os.getenv("STEAM_RETURN_PATH", "/steam/return")
STEAM_RETURN_URL = (
    urljoin(PUBLIC_BASE_URL + "/", STEAM_RETURN_PATH.lstrip("/"))
    if PUBLIC_BASE_URL else ""
)

HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("STEAM_OAUTH_PORT", os.getenv("HTTP_PORT", "8888")))
CLIENT_SECRET = (os.getenv("DISCORD_OAUTH_CLIENT_SECRET") or "").strip()

# ---- UI-Konfig per ENV -------------------------------------------------------
# „Two-Step“ wird nicht mehr unterstützt, wir fahren nur One-Click.
LINK_COVER_IMAGE = (os.getenv("LINK_COVER_IMAGE") or "").strip()
LINK_COVER_LABEL = (os.getenv("LINK_COVER_LABEL") or "link.earlysalty.com").strip()
LINK_BUTTON_LABEL = (os.getenv("LINK_BUTTON_LABEL") or "Mit Discord verknüpfen").strip()
STEAM_BUTTON_LABEL = (os.getenv("STEAM_BUTTON_LABEL") or "Bei Steam anmelden").strip()


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


# ----------------------- Middleware (Top-Level) -------------------------------
@web.middleware
async def security_headers_mw(request: web.Request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as ex:
        if ex.status in (404, 405):
            resp = web.Response(
                text="Not Found" if ex.status == 404 else "Method Not Allowed",
                status=ex.status,
                content_type="text/plain",
            )
        else:
            resp = ex
    except Exception:
        log.exception("Unhandled error in request")
        resp = web.Response(
            text=(
                "<html><body style='font-family: system-ui, sans-serif'>"
                "<h3>❌ Unerwarteter Fehler</h3>"
                "<p>Bitte versuche es erneut. Wenn das Problem bleibt, kontaktiere den Admin.</p>"
                "</body></html>"
            ),
            content_type="text/html",
            status=500,
        )

    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "form-action https://steamcommunity.com; base-uri 'none'; frame-ancestors 'none'"
    )
    return resp


# ----------------------- Cog --------------------------------------------------
class SteamLink(commands.Cog):
    """
    Linking-Flow:
      1) /link → Discord OAuth2 (identify + connections) (One-Click)
      2) 0 Treffer → Fallback-Seite → Steam OpenID (automatische Weiterleitung)
      3) /steam/return → SteamID64 extrahieren → speichern
      4) Erfolg → DM an den User (cozy Abschluss) & alte Overlays aufräumen
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app = web.Application(middlewares=[security_headers_mw])

        # HTTP-Routen
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/discord/callback", self.handle_discord_callback)
               # expliziter One-Click: kein Two-Step/VerifyView mehr
        self.app.router.add_get("/steam/login", self.handle_steam_login)
        self.app.router.add_get(STEAM_RETURN_PATH, self.handle_steam_return)
        self.app.router.add_get("/favicon.ico", self.handle_favicon)
        self.app.router.add_get("/robots.txt", self.handle_robots)

        self._runner: Optional[web.AppRunner] = None
        self._states: Dict[str, Dict[str, float]] = {}  # state -> {uid, ts}

    # --------------- Lifecycle -----------------------------------------------
    async def cog_load(self) -> None:
        _ensure_schema()
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

        cid = _env_client_id(self.bot)
        if not cid:
            log.warning("Discord OAuth CLIENT_ID fehlt (DISCORD_OAUTH_CLIENT_ID oder bot.application_id).")
        if not CLIENT_SECRET:
            log.warning("DISCORD_OAUTH_CLIENT_SECRET fehlt – Token-Exchange wird scheitern.")

        if not PUBLIC_BASE_URL:
            log.error("PUBLIC_BASE_URL ist NICHT gesetzt – Steam OpenID wird verweigert.")
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

    async def _discord_at_name(self, uid: int) -> str:
        """Fallback-Name '@Discord' falls keine Persona vorhanden."""
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            if not user:
                return f"@{uid}"
            at = getattr(user, "global_name", None) or getattr(user, "display_name", None) or user.name
            at = str(at).strip() if at else str(uid)
            if at.startswith("@"):
                return at
            return f"@{at}"
        except Exception:
            return f"@{uid}"

    async def _cleanup_recent_bot_dms(self, user: Union[discord.User, discord.Member], *, limit: int = 25) -> None:
        try:
            dm = user.dm_channel or await user.create_dm()
            bot_id = self.bot.user.id if self.bot.user else None
            if not bot_id:
                return
            async for msg in dm.history(limit=limit):
                if msg.author and msg.author.id == bot_id:
                    try:
                        await msg.delete()
                    except Exception:
                        pass
        except Exception:
            pass

    async def _notify_user_linked(self, user_id: int, steam_ids: List[str]) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            if not user:
                return
            await self._cleanup_recent_bot_dms(user, limit=25)
            shine = (
                "✨ **Connection complete.**\n"
                "Du funkelst jetzt ein Stückchen heller — und die Welt ein winziges bisschen auch.\n\n"
                "_Tipp: Mit `/links` siehst du deine verknüpften Accounts._"
            )
            await user.send(shine)
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

    # Öffentliche Helper (für andere Cogs)
    def build_discord_link_for(self, uid: int) -> str:
        try:
            return self._build_discord_auth_url(int(uid))
        except Exception:
            log.exception("build_discord_link_for failed (uid=%s)", uid)
            return ""

    def build_steam_openid_for(self, uid: int) -> str:
        try:
            s = self._mk_state(int(uid))
            return self._build_steam_login_url(s)
        except Exception:
            log.exception("build_steam_openid_for failed (uid=%s)", uid)
            return ""

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

    async def _save_steam_links_from_discord(self, uid: int, conns: List[dict]) -> List[str]:
        saved: List[str] = []
        if not conns:
            return saved

        for c in conns:
            try:
                if str(c.get("type", "")).lower() != "steam":
                    continue

                sid_raw = str(c.get("id") or "").strip()
                steam_id: Optional[str] = None

                if re.fullmatch(r"\d{17}", sid_raw):
                    steam_id = sid_raw
                else:
                    name_or_vanity = str(c.get("name") or "").strip()
                    steam_id = await self._resolve_steam_input(sid_raw) or await self._resolve_steam_input(name_or_vanity)

                if not steam_id:
                    meta = c.get("metadata") or {}
                    meta_sid = str(meta.get("steam_id") or "").strip()
                    if re.fullmatch(r"\d{17}", meta_sid):
                        steam_id = meta_sid

                if not steam_id:
                    log.info("Ignoriere Verbindung ohne gültige SteamID: %s", c)
                    continue

                persona = await self._fetch_persona(steam_id) or (c.get("name") or "")
                if not persona:
                    persona = await self._discord_at_name(uid)

                verified = 1 if c.get("verified") else 0
                _save_steam_link_row(uid, steam_id, persona, verified)
                saved.append(steam_id)

            except Exception:
                log.exception("Fehler beim Speichern der Steam-Verknüpfung: user_id=%s, conn=%s", uid, c)

        return saved

    # ---------- Steam resolving helpers --------------------------------------
    async def _resolve_vanity(self, vanity: str) -> Optional[str]:
        key = (os.getenv("STEAM_API_KEY") or "").strip()
        if not key:
            return None
        url = f"{STEAM_API_BASE}/ISteamUser/ResolveVanityURL/v0001/"
        params = {"key": key, "vanityurl": vanity}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, params=params, timeout=10) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
                    resp = data.get("response", {})
                    if resp.get("success") == 1:
                        sid = resp.get("steamid")
                        if sid and re.fullmatch(r"\d{17}", sid):
                            return sid
        except Exception:
            return None
        return None

    async def _resolve_steam_input(self, raw: str) -> Optional[str]:
        """Akzeptiert: 17-stellige ID, steamcommunity-URL (/profiles/<id> oder /id/<vanity>), oder Vanity-String."""
        s = (raw or "").strip()
        if not s:
            return None

        if re.fullmatch(r"\d{17}", s):
            return s

        try:
            u = urlparse(s)
        except Exception:
            u = None
        if u and u.netloc and "steamcommunity.com" in u.netloc:
            path = (u.path or "").rstrip("/")
            m = re.search(r"/profiles/(\d{17})$", path)
            if m:
                return m.group(1)
            m = re.search(r"/id/([^/]+)$", path)
            if m:
                return await self._resolve_vanity(m.group(1))

        if re.fullmatch(r"[A-Za-z0-9_.\-]+", s):
            return await self._resolve_vanity(s)

        return None

    async def _fetch_persona(self, steam_id: str) -> Optional[str]:
        key = (os.getenv("STEAM_API_KEY") or "").strip()
        if not key:
            return None
        url = f"{STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v0002/"
        params = {"key": key, "steamids": steam_id}
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, params=params, timeout=10) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
                    players = data.get("response", {}).get("players", [])
                    if players:
                        return players[0].get("personaname") or None
        except Exception:
            return None
        return None

    # ---- Steam OpenID helpers -----------------------------------------------
    def _require_public_base(self) -> None:
        if not PUBLIC_BASE_URL:
            raise RuntimeError("PUBLIC_BASE_URL ist nicht gesetzt.")

    def _steam_return_to(self, state: str) -> str:
        self._require_public_base()
        return f"{urljoin(PUBLIC_BASE_URL + '/', STEAM_RETURN_PATH.lstrip('/'))}?{urlencode({'state': state})}"

    def _steam_realm(self) -> str:
        self._require_public_base()
        return PUBLIC_BASE_URL

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
        sid = m.group(1) if m else None
        if sid and re.fullmatch(r"\d{17}", sid):
            return sid
        return None

    # --------------- HTTP-Handler --------------------------------------------
    async def handle_index(self, request: web.Request) -> web.Response:
        html_doc = (
            "<html><body style='font-family: system-ui, sans-serif'>"
            "<h2>Deadlock Bot – Link Service</h2>"
            "<p>✅ Server läuft. Nutze im Discord <code>/link</code>, "
            "<code>/addsteam</code>, <code>/setprimary</code> oder <code>/link_steam</code>.</p>"
            "<p><a href='/health'>Health-Check</a></p>"
            "</body></html>"
        )
        return web.Response(text=html_doc, content_type="text/html")

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "ts": int(time.time())})

    async def handle_favicon(self, request: web.Request) -> web.Response:
        return web.Response(status=204)

    async def handle_robots(self, request: web.Request) -> web.Response:
        return web.Response(text="User-agent: *\nDisallow: /\n", content_type="text/plain")

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
            await self._notify_user_linked(uid, saved_ids)
            html_doc = (
                "<html><body style='font-family: system-ui, sans-serif'>"
                "<h3>✅ Verknüpfung abgeschlossen</h3>"
                f"<p>{len(saved_ids)} Steam-Account(s) wurden gespeichert.</p>"
                "<p>Du kannst dieses Fenster jetzt schließen.</p>"
                "</body></html>"
            )
            return web.Response(text=html_doc, content_type="text/html")

        # Seamless Redirect zu Steam OpenID
        try:
            steam_state = self._mk_state(uid)
            steam_login = self._build_steam_login_url(steam_state)
            raise web.HTTPFound(location=steam_login)
        except Exception:
            steam_state = self._mk_state(uid)
            steam_login = self._build_steam_login_url(steam_state) if PUBLIC_BASE_URL else "#"
            steam_login_safe = html.escape(steam_login, quote=True)
            html_doc = (
                "<html><head>"
                f"<meta http-equiv='refresh' content=\"0; url={steam_login_safe}\"/>"
                "</head><body style='font-family: system-ui, sans-serif'>"
                "<h3>Weiterleitung zu Steam …</h3>"
                f"<p><a href=\"{steam_login_safe}\" style='padding:10px 14px;"
                "background:#2a475e;color:#fff;border-radius:6px;text-decoration:none;'>Falls nichts passiert, hier klicken</a></p>"
                "</body></html>"
            )
            return web.Response(text=html_doc, content_type="text/html")

    async def handle_steam_login(self, request: web.Request) -> web.Response:
        uid_q = request.query.get("uid")
        if not uid_q or not uid_q.isdigit():
            return web.Response(text="missing uid", status=400)
        uid = int(uid_q)
        s = self._mk_state(uid)
        login_url = self._build_steam_login_url(s)
        login_url_safe = html.escape(login_url, quote=True)
        html_doc = (
            "<html><body style='font-family: system-ui, sans-serif'>"
            "<h3>Weiter zu Steam</h3>"
            f"<p><a href=\"{login_url_safe}\">Steam Login öffnen</a></p>"
            "</body></html>"
        )
        return web.Response(text=html_doc, content_type="text/html")

    async def handle_steam_return(self, request: web.Request) -> web.Response:
        try:
            state = request.query.get("state", "")
            uid = self._pop_state(state)
            if not uid:
                return web.Response(text="invalid/expired state", status=400)

            steam_id = await self._verify_steam_openid(request)
            if not steam_id:
                return web.Response(text="OpenID validation failed", status=400)

            display_name = await self._fetch_persona(steam_id) or await self._discord_at_name(uid)
            _save_steam_link_row(uid, steam_id, display_name, verified=1)
            await self._notify_user_linked(uid, [steam_id])

            steam_id_safe = html.escape(steam_id, quote=True)
            body = (
                "<h3>✅ Verknüpfung abgeschlossen</h3>"
                f"<p>Deine SteamID64 ist: <b>{steam_id_safe}</b>.</p>"
                "<p>Du kannst dieses Fenster schließen und zu Discord zurückkehren.</p>"
            )
            return web.Response(text=body, content_type="text/html")

        except Exception:
            log.exception("Fehler im Steam-Return")
            return web.Response(
                text=(
                    "<html><body style='font-family: system-ui, sans-serif'>"
                    "<h3>❌ Unerwarteter Fehler</h3>"
                    "<p>Bitte versuche es erneut. Wenn das Problem bleibt, kontaktiere den Admin.</p>"
                    "</body></html>"
                ),
                content_type="text/html",
                status=500,
            )

    # --------------- Commands -------------------------------------------------
    async def _defer_if_needed(self, ctx: commands.Context) -> None:
        """Sofortiges Defer, um 3-Sekunden-Timeout zu vermeiden."""
        if getattr(ctx, "interaction", None) and not ctx.interaction.response.is_done():
            try:
                await ctx.interaction.response.defer(ephemeral=True)
            except Exception:
                pass

    @commands.hybrid_command(
        name="link",
        description="Verknüpfe deine Steam-Accounts (Discord → connections; Fallback Steam OpenID)"
    )
    async def link(self, ctx: commands.Context) -> None:
        # Link-Flow ist instant – kein Defer nötig.
        desc = (
            "• Wenn in deinem Discord-Profil **kein** Steam verknüpft ist, "
            "leite ich dich automatisch zu Steam weiter.\n"
            "• Anmeldedaten bleiben bei Steam.\n"
            "• Ich schicke dir eine DM, sobald die Verknüpfung durch ist."
        )

        embed = discord.Embed(title="Steam/Discord verknüpfen", description=desc, color=discord.Color.green())
        if LINK_COVER_IMAGE:
            embed.set_image(url=LINK_COVER_IMAGE)
        embed.set_author(name=LINK_COVER_LABEL)

        try:
            url = self._build_discord_auth_url(ctx.author.id)
        except Exception as e:
            await self._send_ephemeral(ctx, f"❌ OAuth-Fehler: `{e}` – prüfe .env & Dev-Portal Redirect.")
            return

        view = discord.ui.View()
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=LINK_BUTTON_LABEL, url=url))
        await self._send_ephemeral(ctx, embed=embed, view=view)

    @commands.hybrid_command(
        name="link_steam",
        description="Direkt: Steam-Login (OpenID) starten"
    )
    async def link_steam(self, ctx: commands.Context) -> None:
        # Link-Flow ist instant – kein Defer nötig.
        desc = "Bestätige deinen Account via Steam OpenID."
        embed = discord.Embed(title="Direkt bei Steam anmelden", description=desc, color=discord.Color.green())
        if LINK_COVER_IMAGE:
            embed.set_image(url=LINK_COVER_IMAGE)
        embed.set_author(name=LINK_COVER_LABEL)

        s = self._mk_state(ctx.author.id)
        url = self._build_steam_login_url(s)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=STEAM_BUTTON_LABEL, url=url))
        await self._send_ephemeral(ctx, embed=embed, view=view)

    @commands.hybrid_command(name="links", description="Zeigt deine gespeicherten Steam-Links")
    async def links(self, ctx: commands.Context) -> None:
        # DB-only – in der Regel <3s, aber wir sind großzügig und deferen sicherheitshalber nicht.
        rows = db.query_all(
            "SELECT steam_id, name, verified, primary_account "
            "FROM steam_links WHERE user_id=? "
            "ORDER BY primary_account DESC, updated_at DESC",
            (ctx.author.id,),
        )
        if not rows:
            await self._send_ephemeral(ctx, "Keine Steam-Links gefunden. Nutze `/link`, `/addsteam` oder `/link_steam`.")
            return
        lines = []
        for r in rows:
            sid = r["steam_id"]
            nm = r["name"] or "—"
            chk = " ✅" if r["verified"] else ""
            prim = " [primary]" if r["primary_account"] else ""
            lines.append(f"- **{sid}** ({nm}){chk}{prim}")
        await self._send_ephemeral(ctx, "Deine verknüpften Accounts:\n" + "\n".join(lines))

    @commands.hybrid_command(
        name="whoami",
        description="Prüft ID/Vanity/Profil-Link und zeigt Persona + SteamID"
    )
    async def whoami(self, ctx: commands.Context, steam: str) -> None:
        await self._defer_if_needed(ctx)  # <- verhindert 3s-Timeout
        # Schnelle Budget-Klammer für UX (optional)
        try:
            sid = await asyncio.wait_for(self._resolve_steam_input(steam), timeout=8)
        except asyncio.TimeoutError:
            await self._send_ephemeral(ctx, "⏳ Steam/Netzwerk langsam. Bitte nochmal versuchen.")
            return

        if not sid:
            await self._send_ephemeral(ctx, "❌ Konnte aus deiner Eingabe keine SteamID bestimmen.")
            return

        try:
            persona = await asyncio.wait_for(self._fetch_persona(sid), timeout=8)
        except asyncio.TimeoutError:
            persona = None

        if persona:
            await self._send_ephemeral(ctx, f"👤 **{persona}** → SteamID64: `{sid}`")
        else:
            await self._send_ephemeral(ctx, f"SteamID64: `{sid}` (Persona nicht abrufbar)")

    @commands.hybrid_command(
        name="addsteam",
        description="Inoffiziell: fügt manuell eine SteamID hinzu – akzeptiert ID, Vanity oder Profil-Link."
    )
    async def addsteam(self, ctx: commands.Context, steam: str, name: Optional[str] = None, primary: Optional[bool] = False) -> None:
        await self._defer_if_needed(ctx)  # <- verhindert 3s-Timeout
        try:
            sid = await asyncio.wait_for(self._resolve_steam_input(steam), timeout=8)
        except asyncio.TimeoutError:
            await self._send_ephemeral(ctx, "⏳ Steam/Netzwerk langsam. Bitte nochmal versuchen.")
            return

        if not sid:
            await self._send_ephemeral(ctx, "❌ Ungültige Eingabe. Erwarte SteamID64, Vanity oder steamcommunity-Link.")
            return

        try:
            persona = await asyncio.wait_for(self._fetch_persona(sid), timeout=8)
        except asyncio.TimeoutError:
            persona = None

        display_name = name or (persona or await self._discord_at_name(ctx.author.id))
        _save_steam_link_row(ctx.author.id, sid, display_name, verified=0)

        if primary:
            db.execute("UPDATE steam_links SET primary_account=0 WHERE user_id=?", (ctx.author.id,))
            db.execute(
                "UPDATE steam_links SET primary_account=1, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND steam_id=?",
                (ctx.author.id, sid),
            )
            await self._send_ephemeral(ctx, f"✅ Hinzugefügt & als Primär gesetzt: `{sid}` (manuell, unverified)")
        else:
            await self._send_ephemeral(ctx, f"✅ Hinzugefügt: `{sid}` (manuell, unverified)")

    @commands.hybrid_command(
        name="setprimary",
        description="Markiert einen Steam-Account als Primär (akzeptiert ID/Vanity/Link; legt bei Bedarf an)."
    )
    async def setprimary(self, ctx: commands.Context, steam: str, name: Optional[str] = None) -> None:
        await self._defer_if_needed(ctx)  # <- verhindert 3s-Timeout
        try:
            sid = await asyncio.wait_for(self._resolve_steam_input(steam), timeout=8)
        except asyncio.TimeoutError:
            await self._send_ephemeral(ctx, "⏳ Steam/Netzwerk langsam. Bitte nochmal versuchen.")
            return

        if not sid:
            await self._send_ephemeral(ctx, "❌ Ungültige Eingabe. Erwarte SteamID64, Vanity oder steamcommunity-Link.")
            return

        try:
            persona = await asyncio.wait_for(self._fetch_persona(sid), timeout=8)
        except asyncio.TimeoutError:
            persona = None

        display_name = name or (persona or await self._discord_at_name(ctx.author.id))
        _save_steam_link_row(ctx.author.id, sid, display_name, verified=0)
        db.execute("UPDATE steam_links SET primary_account=0 WHERE user_id=?", (ctx.author.id,))
        db.execute(
            "UPDATE steam_links SET primary_account=1, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND steam_id=?",
            (ctx.author.id, sid),
        )
        await self._send_ephemeral(ctx, f"✅ Primär gesetzt: `{sid}`")

    @commands.hybrid_command(name="unlink", description="Entfernt einen Steam-Link (ID/Vanity/Profil-Link möglich)")
    async def unlink(self, ctx: commands.Context, steam: str) -> None:
        # DB-only plus kleine Auflösung; zur Sicherheit deferen wir nicht zwingend.
        sid = await self._resolve_steam_input(steam)
        if not sid and re.fullmatch(r"\d{17}", steam or ""):
            sid = steam
        if not sid:
            await self._send_ephemeral(ctx, "❌ Ungültige Eingabe. Erwarte SteamID64, Vanity oder steamcommunity-Link.")
            return
        db.execute("DELETE FROM steam_links WHERE user_id=? AND steam_id=?", (ctx.author.id, sid))
        await self._send_ephemeral(ctx, f"Entfernt: `{sid}`")

    # --------- unified ephemeral sender (embed/view-fähig) -------------------
    async def _send_ephemeral(
        self,
        ctx: commands.Context,
        content: Optional[str] = None,
        *,
        embed: Optional[discord.Embed] = None,
        view: Optional[discord.ui.View] = None
    ) -> None:
        # Wichtig: None → discord.utils.MISSING (verhindert view.is_finished() auf None)
        c = content if content is not None else discord.utils.MISSING
        e = embed if embed is not None else discord.utils.MISSING
        v = view if view is not None else discord.utils.MISSING

        if getattr(ctx, "interaction", None) and not ctx.interaction.response.is_done():
            await ctx.interaction.response.send_message(c, embed=e, view=v, ephemeral=True)
        elif getattr(ctx, "interaction", None):
            await ctx.interaction.followup.send(c, embed=e, view=v, ephemeral=True)
        else:
            await ctx.reply(c if c is not discord.utils.MISSING else "", embed=e if e is not discord.utils.MISSING else None, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamLink(bot))
