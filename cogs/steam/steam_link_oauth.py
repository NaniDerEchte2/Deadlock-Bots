# cogs/steam/steam_link_oauth.py
import os
import re
import time
import uuid
import logging
import html
import asyncio
from typing import Any, Dict, Optional, List, Union
from urllib.parse import urlencode, urljoin, urlparse

import aiohttp
from aiohttp import web

import discord
from discord.ext import commands

from service import db

from cogs.steam.friend_requests import queue_friend_request, queue_friend_requests
from cogs.steam.logging_utils import sanitize_log_value

log = logging.getLogger("SteamLink")

DISCORD_API = "https://discord.com/api"
STEAM_API_BASE = "https://api.steampowered.com"
STEAM_OPENID_ENDPOINT = "https://steamcommunity.com/openid/login"
OPENID_NS = "http://specs.openid.net/auth/2.0"
IDENTIFIER_SELECT = "http://specs.openid.net/auth/2.0/identifier_select"

PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL") or "https://link.earlysalty.com"
).rstrip("/")
STEAM_RETURN_PATH = os.getenv("STEAM_RETURN_PATH", "/steam/return")
STEAM_RETURN_URL = (
    urljoin(PUBLIC_BASE_URL + "/", STEAM_RETURN_PATH.lstrip("/"))
    if PUBLIC_BASE_URL
    else ""
)

HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")  # noqa: S104
HTTP_PORT = int(os.getenv("STEAM_OAUTH_PORT", os.getenv("HTTP_PORT", "8888")))
CLIENT_SECRET = (os.getenv("DISCORD_OAUTH_CLIENT_SECRET") or "").strip()

# State TTL
STATE_TTL_SEC = 600  # 10 min

# UI (deutsche Labels)
LINK_COVER_IMAGE = (os.getenv("LINK_COVER_IMAGE") or "").strip()
LINK_COVER_LABEL = (os.getenv("LINK_COVER_LABEL") or "link.earlysalty.com").strip()
LINK_BUTTON_LABEL = (os.getenv("LINK_BUTTON_LABEL") or "Via Discord verknüpfen").strip()
STEAM_BUTTON_LABEL = (
    os.getenv("STEAM_BUTTON_LABEL") or "Direkt bei Steam anmelden"
).strip()

# ---------------------------------------------------------------------------
# Öffentliche Schnittstelle für andere Cogs (Welcome-DM, Rules-Panel, etc.)
# ---------------------------------------------------------------------------
__all__ = ("get_public_urls", "start_urls_for")


def _safe_log_repr(value: Any) -> str:
    """Return a repr-style string with control chars escaped for safe logging."""
    return repr(sanitize_log_value(value))


def _env_client_id(bot: commands.Bot) -> str:
    cid = (os.getenv("DISCORD_OAUTH_CLIENT_ID") or "").strip()
    if cid:
        return cid
    app_id = getattr(bot, "application_id", None)
    return str(app_id) if app_id else ""


def _env_redirect() -> str:
    # Hardcoded Discord OAuth Redirect URI (muss mit Discord Developer Portal übereinstimmen)
    return "https://link.earlysalty.com/discord/callback"


def get_public_urls() -> dict:
    """
    Quelle der Wahrheit für Start-/Callback-Links.
    UI-Cogs importieren diese Funktion und hängen selbst '?uid=<discord_id>' an.
    Kein Fallback: fehlt etwas Wesentliches -> ImportError (Start abbrechen).
    """
    base = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
    if not base:
        # entspricht Vorgabe: hart abbrechen, kein Fallback
        raise ImportError(
            "PUBLIC_BASE_URL ist nicht gesetzt – keine öffentlichen URLs verfügbar."
        )

    urls = {
        # Startpunkte (an diese hängen die UIs ?uid=<id>)
        "discord_start": f"{base}/discord/login",
        "steam_openid_start": f"{base}/steam/login",
        # Callbacks (für Vollständigkeit/Debug)
        "discord_callback": _env_redirect(),
        "steam_return": urljoin(base + "/", STEAM_RETURN_PATH.lstrip("/")),
    }

    # Minimalvalidierung
    for k in (
        "discord_start",
        "steam_openid_start",
        "discord_callback",
        "steam_return",
    ):
        u = urls.get(k)
        if not u or "://" not in u:
            raise ImportError(f"Ungültige URL für {k}: {u!r}")
    return urls


def start_urls_for(uid: int) -> dict:
    """
    Liefert user-spezifische Start-URLs MIT ?uid=... für Discord-OAuth und Steam-OpenID.
    Wird vom SteamLinkStepView (Welcome-DM / Rules-Panel) beim Klick verwendet.
    """
    base = (os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
    if not base:
        # bewusst nicht hart fehlschlagen – die UI meldet es dem Nutzer ephemer
        return {"discord_start": "", "steam_openid_start": ""}

    u = int(uid)
    return {
        "discord_start": f"{base}/discord/login?uid={u}",
        "steam_openid_start": f"{base}/steam/login?uid={u}",
    }


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
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_steam_links_user ON steam_links(user_id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_steam_links_steam ON steam_links(steam_id)"
    )


def _save_steam_link_row(
    user_id: int, steam_id: str, name: str = "", verified: int = 0
) -> None:
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
    try:
        queue_friend_request(steam_id)
    except Exception:
        log.exception(
            "Konnte Steam-Freundschaftsanfrage nicht einreihen (steam_id=%s)",
            _safe_log_repr(steam_id),
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
      1) /account_verknüpfen → Discord OAuth2 (identify + connections) (Lazy-Start)
      2) Keine Steam-Verknüpfung gefunden → seamless Redirect zu Steam OpenID
      3) /steam/return → SteamID64 extrahieren → speichern
      4) Erfolg → DM an User
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app = web.Application(middlewares=[security_headers_mw])

        # HTTP-Routen
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/health", self.handle_health)

        # Discord OAuth2 (Lazy-Start + Callback)
        self.app.router.add_get("/discord/login", self.handle_discord_login)
        self.app.router.add_get("/discord/callback", self.handle_discord_callback)

        # Steam OpenID (Lazy-Start + Return)
        self.app.router.add_get("/steam/login", self.handle_steam_login)
        self.app.router.add_get(STEAM_RETURN_PATH, self.handle_steam_return)

        # Kleinkram
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
            log.warning(
                "Discord OAuth CLIENT_ID fehlt (DISCORD_OAUTH_CLIENT_ID oder bot.application_id)."
            )
        if not CLIENT_SECRET:
            log.warning(
                "DISCORD_OAUTH_CLIENT_SECRET fehlt – Token-Exchange wird scheitern."
            )

        if not PUBLIC_BASE_URL:
            log.error(
                "PUBLIC_BASE_URL ist NICHT gesetzt – Start-/Return-Routen brauchen sie."
            )
        else:
            log.info("Steam OpenID return_to: %s", STEAM_RETURN_URL)

        self._runner = web.AppRunner(self.app)
        await self._runner.setup()

        # Retry logic for port availability during reloads
        max_retries = 5
        retry_delay = 0.5

        for attempt in range(max_retries):
            try:
                site = web.TCPSite(self._runner, host=HTTP_HOST, port=HTTP_PORT)
                await site.start()
                log.info(
                    "OAuth/OpenID Callback-Server läuft auf %s:%s (Discord redirect=%s, STATE_TTL_SEC=%ss)",
                    HTTP_HOST,
                    HTTP_PORT,
                    _env_redirect(),
                    STATE_TTL_SEC,
                )
                return
            except OSError as e:
                # Check for address in use (WinError 10048 or EADDRINUSE)
                import errno

                is_addr_in_use = e.errno == 10048 or e.errno == errno.EADDRINUSE

                if is_addr_in_use and attempt < max_retries - 1:
                    log.debug(
                        "Steam OAuth port %s belegt, versuche es erneut in %ss... (Versuch %s/%s)",
                        HTTP_PORT,
                        retry_delay,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                log.exception(
                    "Konnte OAuth Callback-Server nicht starten (Port belegt oder anderer Fehler)"
                )
                break
            except Exception:
                log.exception("Konnte OAuth Callback-Server nicht starten")
                break

    async def cog_unload(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # --------------- Helpers --------------------------------------------------
    def _mk_state(self, uid: int, context: str = "steam_link") -> str:
        s = uuid.uuid4().hex
        self._states[s] = {"uid": int(uid), "ts": time.time(), "context": context}
        return s

    def _pop_state(self, s: str) -> Optional[int]:
        data = self._states.pop(s, None)
        if not data:
            return None
        if time.time() - data["ts"] > STATE_TTL_SEC:
            return None
        return int(data["uid"])

    def _pop_state_data(self, s: str) -> Optional[dict]:
        """Returns full state dict including context, or None if missing/expired."""
        data = self._states.pop(s, None)
        if not data:
            return None
        if time.time() - data["ts"] > STATE_TTL_SEC:
            return None
        return data

    async def _discord_at_name(self, uid: int) -> str:
        try:
            user = self.bot.get_user(uid) or await self.bot.fetch_user(uid)
            if not user:
                return f"@{uid}"
            at = (
                getattr(user, "global_name", None)
                or getattr(user, "display_name", None)
                or user.name
            )
            at = str(at).strip() if at else str(uid)
            if at.startswith("@"):
                return at
            return f"@{at}"
        except Exception:
            return f"@{uid}"

    async def _cleanup_recent_bot_dms(
        self, user: Union[discord.User, discord.Member], *, limit: int = 25
    ) -> None:
        try:
            dm = user.dm_channel or await user.create_dm()
            bot_id = self.bot.user.id if self.bot.user else None
            if not bot_id:
                return
            async for msg in dm.history(limit=limit):
                if msg.author and msg.author.id == bot_id:
                    try:
                        await msg.delete()
                    except Exception as e:
                        log.debug(
                            "DM-Cleanup scheiterte (user_id=%s): %s",
                            getattr(user, "id", "?"),
                            e,
                            exc_info=True,
                        )
        except Exception as e:
            log.debug(
                "DM-Cleanup übersprungen/fehlgeschlagen (user_id=%s): %s",
                getattr(user, "id", "?"),
                e,
                exc_info=True,
            )

    async def _notify_user_linked(self, user_id: int, steam_ids: List[str]) -> None:
        try:
            queue_friend_requests(steam_ids)
        except Exception:
            ids_for_log = steam_ids or []
            steam_id_count = len(ids_for_log)
            invalid_ids = any(
                not re.fullmatch(r"\d{17,20}", str(sid)) for sid in ids_for_log
            )
            try:
                safe_user = int(user_id)
            except Exception:
                safe_user = None
            log.exception(
                "Konnte Steam-Freundschaftsanfragen nicht in die Queue legen",
                # Avoid logging raw user-controlled identifiers; keep structured, bounded data only.
                extra={
                    "user_id": safe_user,
                    "steam_id_count": steam_id_count,
                    "steam_ids_invalid": invalid_ids,
                },
            )
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            if not user:
                return
            await self._cleanup_recent_bot_dms(user, limit=25)
            shine = (
                "✨ **Connection complete.**\n"
                "🤝 Unser Steam-Bot schickt dir gleich eine Freundschaftsanfrage. "
            )
            await user.send(shine)
        except Exception as e:
            log.info("Konnte User-DM nicht senden (id=%s): %s", user_id, e)

    def _build_discord_auth_url(self, uid: int, context: str = "steam_link") -> str:
        client_id = _env_client_id(self.bot)
        redirect_uri = _env_redirect()
        if not client_id:
            raise RuntimeError(
                "DISCORD_OAUTH_CLIENT_ID/bot.application_id nicht gesetzt"
            )
        if not redirect_uri:
            raise RuntimeError(
                "DISCORD_OAUTH_REDIRECT/PUBLIC_BASE_URL/HTTP_HOST fehlen"
            )

        # Turnier context only needs identify scope; steam_link needs connections too
        scope = "identify" if context == "turnier" else "identify connections"
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "prompt": "consent",
            "state": self._mk_state(uid, context=context),
        }
        return f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}"

    # Öffentliche Helper (für andere Cogs über Bot-Instanz, optional)
    def discord_start_url_for(self, uid: int) -> str:
        if not PUBLIC_BASE_URL:
            return ""
        return f"{PUBLIC_BASE_URL}/discord/login?uid={int(uid)}"

    def steam_start_url_for(self, uid: int) -> str:
        if not PUBLIC_BASE_URL:
            return ""
        return f"{PUBLIC_BASE_URL}/steam/login?uid={int(uid)}"

    # Rückwärtskompatibel
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
            async with s.post(
                f"{DISCORD_API}/oauth2/token", data=data, headers=headers
            ) as r:
                if r.status != 200:
                    log.warning("Discord OAuth exchange failed (HTTP %s).", r.status)
                    return None
                return await r.json()

    async def _discord_fetch_user(self, access_token: str) -> Optional[dict]:
        """Fetch the authenticated Discord user's basic info (/users/@me)."""
        headers = {"Authorization": f"Bearer {access_token}"}
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{DISCORD_API}/users/@me", headers=headers) as r:
                if r.status != 200:
                    log.warning("Discord @me API failed (HTTP %s).", r.status)
                    return None
                return await r.json()

    async def _discord_fetch_connections(
        self, access_token: str
    ) -> Optional[List[dict]]:
        headers = {"Authorization": f"Bearer {access_token}"}
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{DISCORD_API}/users/@me/connections", headers=headers
            ) as r:
                if r.status != 200:
                    log.warning(
                        "Discord Connections-API fehlgeschlagen (HTTP %s).", r.status
                    )
                    return None
                return await r.json()

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
                        if sid and re.fullmatch(r"\d{17,20}", sid):
                            return sid
        except Exception:
            return None
        return None

    async def _resolve_steam_input(self, raw: str) -> Optional[str]:
        s = (raw or "").strip()
        if not s:
            return None

        # 1) 17–20-stellige ID direkt (tolerant, wie gewünscht)
        if re.fullmatch(r"\d{17,20}", s):
            return s

        # 2) URL?
        try:
            u = urlparse(s)
        except Exception:
            u = None

        if u and (u.hostname or "").lower():
            host = (u.hostname or "").lower().strip(".")
            if host == "steamcommunity.com" or host.endswith(".steamcommunity.com"):
                path = (u.path or "").rstrip("/")
                m = re.search(r"/profiles/(\d{17,20})$", path)
                if m:
                    return m.group(1)
                m = re.search(r"/id/([^/]+)$", path)
                if m:
                    return await self._resolve_vanity(m.group(1))

        # 3) nackter Vanity-Kandidat
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
            async with session.post(
                STEAM_OPENID_ENDPOINT, data=verify_params, timeout=15
            ) as resp:
                body = await resp.text()
                if resp.status != 200 or "is_valid:true" not in body:
                    log.warning(
                        "Steam OpenID verify fehlgeschlagen (HTTP %s).", resp.status
                    )
                    return None

        claimed_id = query.get("openid.claimed_id", "")
        m = re.search(r"/openid/id/(\d+)$", claimed_id)
        sid = m.group(1) if m else None
        if sid and re.fullmatch(r"\d{17,20}", sid):  # tolerant
            return sid
        return None

    # --------------- HTTP-Handler --------------------------------------------
    async def handle_index(self, request: web.Request) -> web.Response:
        html_doc = (
            "<html><body style='font-family: system-ui, sans-serif'>"
            "<h2>Deadlock Bot - Link Service</h2>"
            "<p>✅ Server läuft. Nutze im Discord <code>/account_verknüpfen</code> "
            "oder <code>/steam setprimary</code>.</p>"
            "<p><a href='/health'>Health-Check</a></p>"
            "</body></html>"
        )
        return web.Response(text=html_doc, content_type="text/html")

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "ts": int(time.time())})

    async def handle_favicon(self, request: web.Request) -> web.Response:
        return web.Response(status=204)

    async def handle_robots(self, request: web.Request) -> web.Response:
        return web.Response(
            text="User-agent: *\nDisallow: /\n", content_type="text/plain"
        )

    async def handle_discord_login(self, request: web.Request) -> web.Response:
        context = request.query.get("context", "steam_link")
        if context == "turnier":
            uid = 0  # Will be determined after Discord OAuth; not needed for turnier flow
        else:
            uid_q = request.query.get("uid")
            if not uid_q or not uid_q.isdigit():
                return web.Response(text="missing uid", status=400)
            uid = int(uid_q)
        try:
            url = self._build_discord_auth_url(uid, context=context)
            raise web.HTTPFound(location=url)
        except web.HTTPFound:
            raise
        except Exception as e:
            log.exception("discord/login failed: %s", e)
            return web.Response(text="failed to start oauth", status=500)

    async def handle_discord_callback(self, request: web.Request) -> web.Response:
        code = request.query.get("code")
        state_key = request.query.get("state")
        if not code or not state_key:
            return web.Response(text="missing code/state", status=400)

        state_data = self._pop_state_data(state_key)
        if not state_data:
            return web.Response(text="invalid/expired state", status=400)

        uid = int(state_data["uid"])
        context = str(state_data.get("context", "steam_link"))

        token = await self._discord_token_exchange(code)
        if not token:
            return web.Response(text="token exchange failed", status=400)

        at = token.get("access_token")
        if not at:
            return web.Response(text="no access_token", status=400)

        # ── Turnier context: issue one-time token, redirect to turnier site ──
        if context == "turnier":
            try:
                user_info = await self._discord_fetch_user(at)
                if not user_info:
                    return web.Response(text="user fetch failed", status=400)
                actual_uid = int(user_info["id"])
                display_name = str(
                    user_info.get("global_name")
                    or user_info.get("username")
                    or str(actual_uid)
                ).strip()
                from cogs.customgames.tournament_store import create_auth_token_async
                auth_token = await create_auth_token_async(actual_uid, display_name)
                turnier_url = f"https://turnier.earlysalty.com/auth/complete?token={auth_token}"
                raise web.HTTPFound(location=turnier_url)
            except web.HTTPFound:
                raise
            except Exception:
                log.exception("Turnier OAuth callback fehlgeschlagen")
                return web.Response(
                    text=(
                        "<html><body style='font-family: system-ui, sans-serif'>"
                        "<h3>❌ Anmeldung fehlgeschlagen</h3>"
                        "<p>Bitte versuche es erneut.</p>"
                        "</body></html>"
                    ),
                    content_type="text/html",
                    status=500,
                )

        # ── Default steam_link context ──
        conns = await self._discord_fetch_connections(at)
        if conns is None:
            return web.Response(text="connections fetch failed", status=400)

        saved_ids = await self._save_steam_links_from_discord(uid, conns)
        if saved_ids:
            await self._notify_user_linked(uid, saved_ids)
            html_doc = (
                "<html><body style='font-family: system-ui, sans-serif'>"
                "<h3>✅ Verknüpfung abgeschlossen</h3>"
                "<p>Steam-Account(s) wurden gespeichert.</p>"
                "<p>Du kannst dieses Fenster jetzt schließen.</p>"
                "</body></html>"
            )
            return web.Response(text=html_doc, content_type="text/html")

        # Seamless Redirect zu Steam OpenID
        try:
            steam_state = self._mk_state(uid)
            steam_login = self._build_steam_login_url(steam_state)
            raise web.HTTPFound(location=steam_login)
        except web.HTTPFound:
            raise
        except Exception:
            log.exception("Discord callback: Weiterleitung zu Steam fehlgeschlagen")
            return web.Response(
                text=(
                    "<html><body style='font-family: system-ui, sans-serif'>"
                    "<h3>❌ Weiterleitung fehlgeschlagen</h3>"
                    "<p>Bitte starte den Link-Vorgang in Discord erneut.</p>"
                    "</body></html>"
                ),
                content_type="text/html",
                status=500,
            )

    async def handle_steam_login(self, request: web.Request) -> web.Response:
        uid_q = request.query.get("uid")
        if not uid_q or not uid_q.isdigit():
            return web.Response(text="missing uid", status=400)
        uid = int(uid_q)
        s = self._mk_state(uid)
        login_url = self._build_steam_login_url(s)
        try:
            raise web.HTTPFound(location=login_url)
        except web.HTTPFound:
            raise
        except Exception:
            log.exception("Steam login redirect failed")
            return web.Response(text="failed to start steam login", status=500)

    async def handle_steam_return(self, request: web.Request) -> web.Response:
        try:
            if request.method == "HEAD" or request.query.get("healthcheck") == "1":
                return web.Response(text="steam return ok", status=200)

            state = request.query.get("state", "")
            uid = self._pop_state(state)
            if not uid:
                return web.Response(text="invalid/expired state", status=400)

            steam_id = await self._verify_steam_openid(request)
            if not steam_id:
                return web.Response(text="OpenID validation failed", status=400)

            display_name = await self._fetch_persona(
                steam_id
            ) or await self._discord_at_name(uid)
            # Verified=0, da wir erst die Freundschaftsanfrage abwarten wollen
            _save_steam_link_row(uid, steam_id, display_name, verified=0)
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

    # --------------- Connections → SteamIDs ----------------------------------
    async def _save_steam_links_from_discord(
        self, uid: int, conns: List[dict]
    ) -> List[str]:
        saved: List[str] = []
        if not conns:
            return saved

        for c in conns:
            try:
                if str(c.get("type", "")).lower() != "steam":
                    continue

                sid_raw = str(c.get("id") or "").strip()
                steam_id: Optional[str] = None

                if re.fullmatch(r"\d{17,20}", sid_raw):
                    steam_id = sid_raw
                else:
                    name_or_vanity = str(c.get("name") or "").strip()
                    steam_id = await self._resolve_steam_input(
                        sid_raw
                    ) or await self._resolve_steam_input(name_or_vanity)

                if not steam_id:
                    meta = c.get("metadata") or {}
                    meta_sid = str(meta.get("steam_id") or "").strip()
                    if re.fullmatch(r"\d{17,20}", meta_sid):
                        steam_id = meta_sid

                if not steam_id:
                    log.info(
                        "Ignoriere Verbindung ohne gültige SteamID: %s",
                        _safe_log_repr(c),
                    )
                    continue

                persona = await self._fetch_persona(steam_id) or (c.get("name") or "")
                if not persona:
                    persona = await self._discord_at_name(uid)

                # Wir setzen verified=0, damit der User erst Freund werden muss
                _save_steam_link_row(uid, steam_id, persona, verified=0)
                saved.append(steam_id)

            except Exception:
                log.exception(
                    "Fehler beim Speichern der Steam-Verknüpfung: user_id=%s, conn=%s",
                    uid,
                    _safe_log_repr(c),
                )

        return saved

    # --------------- Commands -------------------------------------------------
    async def _defer_if_needed(self, ctx: commands.Context) -> None:
        if getattr(ctx, "interaction", None) and not ctx.interaction.response.is_done():
            try:
                await ctx.interaction.response.defer(ephemeral=True)
            except Exception as e:
                log.debug(
                    "Defer fehlgeschlagen (ctx.user_id=%s): %s",
                    getattr(getattr(ctx, "author", None), "id", "?"),
                    e,
                    exc_info=True,
                )

    async def _send_ephemeral(
        self,
        ctx: commands.Context,
        content: Optional[str] = None,
        *,
        embed: Optional[discord.Embed] = None,
        view: Optional[discord.ui.View] = None,
    ) -> None:
        c = content if content is not None else discord.utils.MISSING
        e = embed if embed is not None else discord.utils.MISSING
        v = view if view is not None else discord.utils.MISSING

        if getattr(ctx, "interaction", None) and not ctx.interaction.response.is_done():
            await ctx.interaction.response.send_message(
                c, embed=e, view=v, ephemeral=True
            )
        elif getattr(ctx, "interaction", None):
            await ctx.interaction.followup.send(c, embed=e, view=v, ephemeral=True)
        else:
            await ctx.reply(
                c if c is not discord.utils.MISSING else "",
                embed=e if e is not discord.utils.MISSING else None,
                view=view,
            )

    async def _send_account_link_panel(self, ctx: commands.Context) -> None:
        desc = (
            "Waehle, wie du deinen Account verknüpfen willst:\n"
            "- **Discord**: liest deine verbundenen Accounts und erkennt Steam automatisch.\n"
            "- **Steam**: direkter OpenID-Login bei Steam.\n\n"
            "Nach erfolgreicher Verknüpfung bekommst du automatisch eine Steam-Freundschaftsanfrage vom Bot."
        )
        embed = discord.Embed(
            title="Account verknüpfen",
            description=desc,
            color=discord.Color.green(),
        )
        if LINK_COVER_IMAGE:
            embed.set_image(url=LINK_COVER_IMAGE)
        embed.set_author(name=LINK_COVER_LABEL)

        if not PUBLIC_BASE_URL:
            await self._send_ephemeral(
                ctx, "⚠️ PUBLIC_BASE_URL fehlt - Start-Links können nicht gebaut werden."
            )
            return

        discord_start_url = f"{PUBLIC_BASE_URL}/discord/login?uid={ctx.author.id}"
        steam_start_url = f"{PUBLIC_BASE_URL}/steam/login?uid={ctx.author.id}"
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label=LINK_BUTTON_LABEL,
                url=discord_start_url,
            )
        )
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label=STEAM_BUTTON_LABEL,
                url=steam_start_url,
            )
        )
        await self._send_ephemeral(ctx, embed=embed, view=view)

    @discord.app_commands.allowed_installs(guilds=True, users=True)
    @discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.hybrid_command(
        name="account_verknüpfen",
        description="Zeigt die Verknüpfungsoptionen für Discord und Steam",
    )
    async def account_verknuepfen(self, ctx: commands.Context) -> None:
        await self._send_account_link_panel(ctx)

    @discord.app_commands.allowed_installs(guilds=True, users=True)
    @discord.app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @commands.hybrid_group(
        name="steam", description="Steam-Links verwalten", invoke_without_command=True
    )
    async def steam(self, ctx: commands.Context) -> None:
        # Fallback: ohne Subcommand direkt den Link-Flow starten
        if ctx.invoked_subcommand is None:
            await self._send_account_link_panel(ctx)

    @steam.command(
        name="link",
        with_app_command=False,
        description="Legacy-Alias für die Account-Verknüpfung",
    )
    async def steam_link(self, ctx: commands.Context) -> None:
        await self._send_account_link_panel(ctx)

    @steam.command(
        name="link_steam",
        with_app_command=False,
        description="Legacy-Alias für die Account-Verknüpfung",
    )
    async def steam_link_steam(self, ctx: commands.Context) -> None:
        await self._send_account_link_panel(ctx)

    @steam.command(name="links", description="Zeigt deine gespeicherten Steam-Links")
    async def steam_links(self, ctx: commands.Context) -> None:
        rows = db.query_all(
            "SELECT steam_id, name, verified, primary_account "
            "FROM steam_links WHERE user_id=? "
            "ORDER BY primary_account DESC, updated_at DESC",
            (ctx.author.id,),
        )
        if not rows:
            await self._send_ephemeral(
                ctx, "Keine Steam-Links gefunden. Nutze `/account_verknüpfen`."
            )
            return
        lines = []
        for r in rows:
            sid = r["steam_id"]
            nm = r["name"] or "-"
            chk = " ✅" if r["verified"] else ""
            prim = " [primary]" if r["primary_account"] else ""
            lines.append(f"- **{sid}** ({nm}){chk}{prim}")
        await self._send_ephemeral(
            ctx, "Deine verknüpften Accounts:\n" + "\n".join(lines)
        )

    @steam.command(
        name="whoami",
        description="Prüft ID/Vanity/Profil-Link und zeigt Persona + SteamID",
    )
    async def steam_whoami(self, ctx: commands.Context, steam: str) -> None:
        await self._defer_if_needed(ctx)
        try:
            sid = await asyncio.wait_for(self._resolve_steam_input(steam), timeout=8)
        except asyncio.TimeoutError:
            await self._send_ephemeral(
                ctx, "⚠️ Steam/Netzwerk langsam. Bitte nochmal versuchen."
            )
            return

        if not sid:
            await self._send_ephemeral(
                ctx, "❌ Konnte aus deiner Eingabe keine SteamID bestimmen."
            )
            return

        try:
            persona = await asyncio.wait_for(self._fetch_persona(sid), timeout=8)
        except asyncio.TimeoutError:
            persona = None

        if persona:
            await self._send_ephemeral(ctx, f"✅ **{persona}** -> SteamID64: `{sid}`")
        else:
            await self._send_ephemeral(
                ctx, f"SteamID64: `{sid}` (Persona nicht abrufbar)"
            )

    @steam.command(
        name="setprimary",
        description="Markiert einen bestehenden Steam-Account als Primär (akzeptiert ID/Vanity/Link).",
    )
    async def steam_setprimary(
        self, ctx: commands.Context, steam: str, name: Optional[str] = None
    ) -> None:
        await self._defer_if_needed(ctx)
        try:
            sid = await asyncio.wait_for(self._resolve_steam_input(steam), timeout=8)
        except asyncio.TimeoutError:
            await self._send_ephemeral(
                ctx, "⚠️ Steam/Netzwerk langsam. Bitte nochmal versuchen."
            )
            return

        if not sid:
            await self._send_ephemeral(
                ctx,
                "❌ Ungültige Eingabe. Erwarte SteamID64, Vanity oder steamcommunity-Link.",
            )
            return

        existing = db.query_one(
            "SELECT steam_id, name FROM steam_links WHERE user_id=? AND steam_id=?",
            (ctx.author.id, sid),
        )
        if not existing:
            await self._send_ephemeral(
                ctx,
                "Kein gespeicherter Steam-Link gefunden. Bitte zuerst `/account_verknüpfen` nutzen.",
            )
            return

        try:
            persona = await asyncio.wait_for(self._fetch_persona(sid), timeout=8)
        except asyncio.TimeoutError:
            persona = None

        if name or persona or existing["name"]:
            display_name = name or persona or existing["name"]
            db.execute(
                "UPDATE steam_links SET name=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND steam_id=?",
                (display_name, ctx.author.id, sid),
            )

        db.execute(
            "UPDATE steam_links SET primary_account=0 WHERE user_id=?", (ctx.author.id,)
        )
        db.execute(
            "UPDATE steam_links SET primary_account=1, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND steam_id=?",
            (ctx.author.id, sid),
        )
        await self._send_ephemeral(ctx, f"✅ Primär gesetzt: `{sid}`")

    @steam.command(
        name="unlink",
        description="Entfernt einen Steam-Link (ID/Vanity/Profil-Link möglich)",
    )
    async def steam_unlink(self, ctx: commands.Context, steam: str) -> None:
        sid = await self._resolve_steam_input(steam)
        if not sid and re.fullmatch(r"\d{17,20}", steam or ""):
            sid = steam
        if not sid:
            await self._send_ephemeral(
                ctx,
                "❌ Ungültige Eingabe. Erwarte SteamID64, Vanity oder steamcommunity-Link.",
            )
            return
        db.execute(
            "DELETE FROM steam_links WHERE user_id=? AND steam_id=?",
            (ctx.author.id, sid),
        )
        await self._send_ephemeral(ctx, f"Entfernt: `{sid}`")


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamLink(bot))
