"""Embedded aiohttp app serving only the Twitch analytics dashboard v2."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import secrets
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlsplit, urlunsplit

import aiohttp
import discord
from aiohttp import web

from . import storage
from .analytics_v2 import AnalyticsV2Mixin
from .dashboard.live import DashboardLiveMixin
from .dashboard.stats import DashboardStatsMixin
from .dashboard.templates import DashboardTemplateMixin
from .logger import log
from .raid_views import RaidAuthGenerateView, build_raid_requirements_embed

TWITCH_OAUTH_AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TWITCH_OAUTH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_HELIX_USERS_URL = "https://api.twitch.tv/helix/users"
LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{3,25}$")


class DashboardV2Server(DashboardLiveMixin, DashboardStatsMixin, DashboardTemplateMixin, AnalyticsV2Mixin):
    """Minimal dashboard server exposing only v2 routes and APIs."""

    def __init__(
        self,
        *,
        app_token: Optional[str],
        noauth: bool,
        partner_token: Optional[str],
        oauth_client_id: Optional[str] = None,
        oauth_client_secret: Optional[str] = None,
        oauth_redirect_uri: Optional[str] = None,
        session_ttl_seconds: int = 12 * 3600,
        legacy_stats_url: Optional[str] = None,
        add_cb: Optional[Callable[[str, bool], Awaitable[str]]] = None,
        remove_cb: Optional[Callable[[str], Awaitable[str]]] = None,
        list_cb: Optional[Callable[[], Awaitable[List[dict]]]] = None,
        stats_cb: Optional[Callable[..., Awaitable[dict]]] = None,
        verify_cb: Optional[Callable[[str, str], Awaitable[str]]] = None,
        archive_cb: Optional[Callable[[str, str], Awaitable[str]]] = None,
        discord_flag_cb: Optional[Callable[[str, bool], Awaitable[str]]] = None,
        discord_profile_cb: Optional[Callable[[str, Optional[str], Optional[str], bool], Awaitable[str]]] = None,
        raid_history_cb: Optional[Callable[..., Awaitable[List[dict]]]] = None,
        raid_bot: Optional[Any] = None,
        reload_cb: Optional[Callable[[], Awaitable[str]]] = None,
    ) -> None:
        self._token = app_token
        self._noauth = noauth
        self._partner_token = partner_token
        self._oauth_client_id = oauth_client_id
        self._oauth_client_secret = oauth_client_secret
        self._oauth_redirect_uri = oauth_redirect_uri
        self._session_ttl_seconds = max(1800, int(session_ttl_seconds or 12 * 3600))
        self._legacy_stats_url = (legacy_stats_url or "").strip() or None
        self._reload_cb = reload_cb
        self._session_cookie_name = "twitch_dash_session"
        self._oauth_states: Dict[str, Dict[str, Any]] = {}
        self._auth_sessions: Dict[str, Dict[str, Any]] = {}
        self._oauth_state_ttl_seconds = 600
        self._add = add_cb if callable(add_cb) else self._empty_add
        self._remove = remove_cb if callable(remove_cb) else self._empty_remove
        self._list = list_cb if callable(list_cb) else self._empty_list
        self._stats = stats_cb if callable(stats_cb) else self._empty_stats
        self._verify = verify_cb if callable(verify_cb) else self._empty_verify
        self._archive = archive_cb if callable(archive_cb) else self._empty_archive
        self._discord_flag = discord_flag_cb if callable(discord_flag_cb) else self._empty_discord_flag
        self._discord_profile = discord_profile_cb
        self._raid_history_cb = raid_history_cb if callable(raid_history_cb) else self._empty_raid_history
        self._raid_bot = raid_bot
        self._redirect_uri = (
            str(getattr(getattr(raid_bot, "auth_manager", None), "redirect_uri", "") or "").strip()
        )
        self._master_dashboard_href = "/admin"

    async def _empty_add(self, _: str, __: bool) -> str:
        return "Add-Funktion ist aktuell nicht verfügbar"

    async def _empty_remove(self, _: str) -> str:
        return "Remove-Funktion ist aktuell nicht verfügbar"

    async def _empty_list(self) -> List[dict]:
        return []

    async def _empty_stats(self, **_: Any) -> dict:
        return {"tracked": {}, "category": {}}

    async def _empty_verify(self, _: str, __: str) -> str:
        return "Verify-Funktion ist aktuell nicht verfügbar"

    async def _empty_archive(self, _: str, __: str) -> str:
        return "Archive-Funktion ist aktuell nicht verfügbar"

    async def _empty_discord_flag(self, _: str, __: bool) -> str:
        return "Discord-Flag-Funktion ist aktuell nicht verfügbar"

    async def _empty_raid_history(self, **_: Any) -> List[dict]:
        return []

    def _check_admin_token(self, token: Optional[str]) -> bool:
        if self._noauth:
            return True
        if not token or not self._token:
            return False
        return token == self._token

    @staticmethod
    def _host_without_port(raw: Optional[str]) -> str:
        if not raw:
            return ""
        host = raw.split(",")[0].strip()
        if not host:
            return ""
        if host.startswith("["):
            end = host.find("]")
            if end != -1:
                host = host[1:end]
        elif ":" in host:
            host = host.split(":", 1)[0]
        return host.lower()

    @staticmethod
    def _is_loopback_host(raw: Optional[str]) -> bool:
        host = DashboardV2Server._host_without_port(raw)
        if not host:
            return False
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _peer_host(request: web.Request) -> str:
        remote = (request.remote or "").strip() if hasattr(request, "remote") else ""
        if remote:
            return remote
        transport = getattr(request, "transport", None)
        if transport is None:
            return ""
        peer = transport.get_extra_info("peername")
        if isinstance(peer, tuple) and peer:
            return str(peer[0]).strip()
        if isinstance(peer, str):
            return peer.strip()
        return ""

    def _effective_client_host(self, request: web.Request, peer_host: str) -> str:
        normalized_peer = self._host_without_port(peer_host)
        if self._is_loopback_host(normalized_peer):
            real_ip = (request.headers.get("X-Real-IP") or "").split(",")[0].strip()
            normalized_real = self._host_without_port(real_ip)
            if normalized_real:
                return normalized_real
        return normalized_peer

    def _is_local_request(self, request: web.Request) -> bool:
        host_header = request.headers.get("Host") or request.host or ""
        request_host = self._host_without_port(host_header)
        if not self._is_loopback_host(request_host):
            return False

        peer_host = self._peer_host(request)
        if not peer_host:
            return False
        client_host = self._effective_client_host(request, peer_host)
        return self._is_loopback_host(client_host)

    @staticmethod
    def _normalize_login(value: str) -> Optional[str]:
        if not value:
            return None
        s = unquote(value).strip()
        if not s:
            return None
        if s.startswith("@"):
            s = s[1:].strip()
        if "twitch.tv" in s or "://" in s or "/" in s:
            if "://" not in s:
                s = f"https://{s}"
            try:
                parts = urlsplit(s)
                segs = [p for p in (parts.path or "").split("/") if p]
                if segs:
                    s = segs[0]
            except Exception:
                return None
        s = s.strip().lower()
        if LOGIN_RE.match(s):
            return s
        return None

    @staticmethod
    def _sanitize_log_value(value: Any) -> str:
        text = "" if value is None else str(value)
        return text.replace("\r", "\\r").replace("\n", "\\n")

    async def _do_add(self, raw: str) -> str:
        login = self._normalize_login(raw)
        if not login:
            raise web.HTTPBadRequest(text="invalid twitch login or url")
        msg = await self._add(login, False)
        return msg or "added"

    def _require_token(self, request: web.Request) -> None:
        admin_only_prefixes = (
            "/twitch/admin",
            "/twitch/live",
            "/twitch/add_any",
            "/twitch/add_url",
            "/twitch/add_login",
            "/twitch/add_streamer",
            "/twitch/remove",
            "/twitch/verify",
            "/twitch/archive",
            "/twitch/discord_flag",
            "/twitch/discord_link",
            "/twitch/raid/auth",
            "/twitch/raid/requirements",
            "/twitch/raid/history",
            "/twitch/reload",
        )
        if request.path.startswith(admin_only_prefixes):
            if self._is_local_request(request):
                return
            raise web.HTTPForbidden(text="admin dashboard is localhost-only")

        if self._check_v2_auth(request):
            return
        token = request.headers.get("X-Admin-Token") or request.query.get("token")
        if self._check_admin_token(token):
            return
        raise web.HTTPUnauthorized(text="missing or invalid token")

    def _require_partner_token(self, request: web.Request) -> None:
        if self._check_v2_auth(request):
            return
        if self._noauth:
            return
        partner_header = request.headers.get("X-Partner-Token")
        partner_query = request.query.get("partner_token")
        admin_header = request.headers.get("X-Admin-Token")
        admin_query = request.query.get("token")

        if self._partner_token:
            if partner_header == self._partner_token or partner_query == self._partner_token:
                return
            if admin_header == self._token or admin_query == self._token:
                return
            raise web.HTTPUnauthorized(text="missing or invalid partner token")
        raise web.HTTPUnauthorized(text="missing or invalid partner token")

    def _redirect_location(
        self,
        request: web.Request,
        *,
        ok: Optional[str] = None,
        err: Optional[str] = None,
        default_path: str = "/twitch/stats",
    ) -> str:
        if default_path == "/twitch/stats":
            admin_action_prefixes = (
                "/twitch/admin",
                "/twitch/live",
                "/twitch/add_any",
                "/twitch/add_url",
                "/twitch/add_login",
                "/twitch/add_streamer",
                "/twitch/remove",
                "/twitch/verify",
                "/twitch/archive",
                "/twitch/discord_flag",
                "/twitch/raid/auth",
                "/twitch/raid/requirements",
                "/twitch/raid/history",
            )
            if request.path.startswith(admin_action_prefixes):
                default_path = "/twitch/admin"

        referer = request.headers.get("Referer")
        if referer:
            try:
                parts = urlsplit(referer)
                if parts.path:
                    params = dict(parse_qsl(parts.query, keep_blank_values=True))
                    params.pop("ok", None)
                    params.pop("err", None)
                    if ok:
                        params["ok"] = ok
                    if err:
                        params["err"] = err
                    return urlunsplit(("", "", parts.path, urlencode(params), "")) or default_path
            except Exception:
                log.debug("Could not construct redirect from referer", exc_info=True)

        params: Dict[str, str] = {}
        if ok:
            params["ok"] = ok
        if err:
            params["err"] = err
        if params:
            return f"{default_path}?{urlencode(params)}"
        return default_path

    def _cleanup_auth_state(self) -> None:
        now = time.time()
        expired_states = [
            key
            for key, row in self._oauth_states.items()
            if now - float(row.get("created_at", 0.0)) > self._oauth_state_ttl_seconds
        ]
        for key in expired_states:
            self._oauth_states.pop(key, None)

        expired_sessions = [
            sid
            for sid, row in self._auth_sessions.items()
            if float(row.get("expires_at", 0.0)) <= now
        ]
        for sid in expired_sessions:
            self._auth_sessions.pop(sid, None)

    def _is_oauth_configured(self) -> bool:
        return bool(self._oauth_client_id and self._oauth_client_secret)

    def _is_secure_request(self, request: web.Request) -> bool:
        forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        if forwarded_proto:
            return forwarded_proto == "https"
        return bool(request.secure)

    def _build_oauth_redirect_uri(self) -> Optional[str]:
        configured = (self._oauth_redirect_uri or "").strip()
        if not configured:
            return None

        candidate = configured if "://" in configured else f"https://{configured}"
        try:
            parsed = urlparse(candidate)
        except Exception:
            log.warning("TWITCH_DASHBOARD_AUTH_REDIRECT_URI is invalid and cannot be parsed")
            return None

        scheme = (parsed.scheme or "").strip().lower()
        host = (parsed.hostname or "").strip().lower()
        path = (parsed.path or "").rstrip("/")

        if parsed.username or parsed.password:
            log.warning("TWITCH_DASHBOARD_AUTH_REDIRECT_URI must not contain user info")
            return None
        if scheme not in {"https", "http"}:
            log.warning("TWITCH_DASHBOARD_AUTH_REDIRECT_URI must use http(s)")
            return None
        if scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
            log.warning("TWITCH_DASHBOARD_AUTH_REDIRECT_URI must use https unless host is localhost")
            return None
        if not parsed.netloc:
            log.warning("TWITCH_DASHBOARD_AUTH_REDIRECT_URI is missing host")
            return None
        if path == "/twitch/raid/callback":
            log.warning("TWITCH_DASHBOARD_AUTH_REDIRECT_URI points to raid callback and is not allowed")
            return None
        if path != "/twitch/auth/callback":
            log.warning("TWITCH_DASHBOARD_AUTH_REDIRECT_URI must point to /twitch/auth/callback")
            return None

        return urlunsplit((scheme, parsed.netloc, "/twitch/auth/callback", "", ""))

    @staticmethod
    def _render_oauth_page(title: str, body_html: str) -> str:
        return (
            "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title, quote=True)}</title>"
            "<style>"
            "body{font-family:Segoe UI,Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;}"
            ".wrap{max-width:760px;margin:0 auto;padding:36px 18px;}"
            ".card{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:20px;}"
            "h1{margin:0 0 12px 0;font-size:24px;}"
            "p{line-height:1.5;margin:10px 0;}"
            "code{background:#0b1220;border:1px solid #23304a;padding:2px 6px;border-radius:6px;}"
            "a{color:#93c5fd;}"
            "</style></head><body><div class='wrap'><div class='card'>"
            f"<h1>{html.escape(title)}</h1>{body_html}</div></div></body></html>"
        )

    def _normalize_next_path(self, raw_path: Optional[str]) -> str:
        fallback = "/twitch/dashboard-v2"
        candidate = (raw_path or "").strip()
        if not candidate:
            return fallback
        parsed = urlparse(candidate)
        if parsed.scheme or parsed.netloc:
            return fallback
        if not candidate.startswith("/"):
            return fallback
        if not candidate.startswith("/twitch"):
            return fallback
        return candidate

    @staticmethod
    def _safe_internal_redirect(location: Optional[str], *, fallback: str = "/twitch/dashboard-v2") -> str:
        candidate = (location or "").strip()
        if not candidate:
            return fallback
        try:
            parts = urlsplit(candidate)
        except Exception:
            return fallback
        if parts.scheme or parts.netloc:
            return fallback
        if not candidate.startswith("/"):
            return fallback
        return candidate

    @staticmethod
    def _safe_oauth_authorize_redirect(location: Optional[str]) -> str:
        candidate = (location or "").strip()
        if not candidate:
            return TWITCH_OAUTH_AUTHORIZE_URL
        try:
            parts = urlsplit(candidate)
        except Exception:
            return TWITCH_OAUTH_AUTHORIZE_URL
        host = (parts.netloc or "").split("@")[-1].split(":", 1)[0].strip().lower()
        if parts.scheme != "https" or host != "id.twitch.tv" or parts.path != "/oauth2/authorize":
            return TWITCH_OAUTH_AUTHORIZE_URL
        return candidate

    @staticmethod
    def _canonical_post_login_destination(next_path: Optional[str]) -> str:
        fallback = "/twitch/dashboard-v2"
        candidate = (next_path or "").strip()
        if not candidate:
            return fallback
        try:
            parts = urlsplit(candidate)
        except Exception:
            return fallback
        if parts.scheme or parts.netloc:
            return fallback

        normalized_path = (parts.path or "").rstrip("/") or "/"
        if normalized_path == "/twitch/stats":
            return "/twitch/stats"
        if normalized_path == "/twitch/dashboards":
            return "/twitch/dashboards"
        if normalized_path == "/twitch/dashboard-v2":
            return "/twitch/dashboard-v2"
        return fallback

    def _build_dashboard_login_url(self, request: web.Request) -> str:
        next_path = self._normalize_next_path(request.rel_url.path_qs if request.rel_url else "/twitch/dashboard-v2")
        return f"/twitch/auth/login?{urlencode({'next': next_path})}"

    def _resolve_legacy_stats_url(self) -> str:
        # The legacy stats dashboard is now always served locally.
        return "/twitch/stats"

    def _get_dashboard_auth_session(self, request: web.Request) -> Optional[Dict[str, Any]]:
        self._cleanup_auth_state()
        session_id = (request.cookies.get(self._session_cookie_name) or "").strip()
        if not session_id:
            return None
        session = self._auth_sessions.get(session_id)
        if not session:
            return None

        now = time.time()
        expires_at = float(session.get("expires_at", 0.0))
        if expires_at <= now:
            self._auth_sessions.pop(session_id, None)
            return None

        session["expires_at"] = now + self._session_ttl_seconds
        return session

    def _set_session_cookie(self, response: web.StreamResponse, request: web.Request, session_id: str) -> None:
        response.set_cookie(
            self._session_cookie_name,
            session_id,
            max_age=self._session_ttl_seconds,
            httponly=True,
            secure=self._is_secure_request(request),
            samesite="Lax",
            path="/",
        )

    def _clear_session_cookie(self, response: web.StreamResponse) -> None:
        response.del_cookie(self._session_cookie_name, path="/")

    def _create_dashboard_session(self, *, twitch_login: str, twitch_user_id: str, display_name: str) -> str:
        self._cleanup_auth_state()
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        self._auth_sessions[session_id] = {
            "twitch_login": twitch_login,
            "twitch_user_id": twitch_user_id,
            "display_name": display_name or twitch_login,
            "is_partner": True,
            "created_at": now,
            "expires_at": now + self._session_ttl_seconds,
        }
        return session_id

    def _is_partner_allowed(self, *, twitch_login: str, twitch_user_id: str) -> Optional[Dict[str, Any]]:
        login = (twitch_login or "").strip().lower()
        user_id = (twitch_user_id or "").strip()
        if not login and not user_id:
            return None

        with storage.get_conn() as conn:
            row = conn.execute(
                """
                SELECT twitch_login, twitch_user_id
                FROM twitch_streamers
                WHERE archived_at IS NULL
                  AND COALESCE(manual_partner_opt_out, 0) = 0
                  AND (
                      COALESCE(manual_verified_permanent, 0) = 1
                      OR manual_verified_at IS NOT NULL
                      OR (manual_verified_until IS NOT NULL AND manual_verified_until > datetime('now'))
                  )
                  AND (
                      LOWER(twitch_login) = LOWER(?)
                      OR (? != '' AND twitch_user_id = ?)
                  )
                LIMIT 1
                """,
                (login, user_id, user_id),
            ).fetchone()

        if not row:
            return None

        if hasattr(row, "keys"):
            return {
                "twitch_login": str(row["twitch_login"] or ""),
                "twitch_user_id": str(row["twitch_user_id"] or ""),
            }
        return {
            "twitch_login": str(row[0] or ""),
            "twitch_user_id": str(row[1] or ""),
        }

    async def _exchange_code_for_user(self, code: str, redirect_uri: str) -> Optional[Dict[str, str]]:
        if not self._is_oauth_configured():
            return None

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                TWITCH_OAUTH_TOKEN_URL,
                data={
                    "client_id": self._oauth_client_id,
                    "client_secret": self._oauth_client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            ) as token_resp:
                if token_resp.status != 200:
                    log.warning("Dashboard OAuth exchange failed with status %s", token_resp.status)
                    return None
                token_data = await token_resp.json()

            access_token = str(token_data.get("access_token") or "").strip()
            if not access_token:
                return None

            async with session.get(
                TWITCH_HELIX_USERS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Client-Id": str(self._oauth_client_id),
                },
            ) as user_resp:
                if user_resp.status != 200:
                    log.warning("Dashboard OAuth user lookup failed with status %s", user_resp.status)
                    return None
                user_data = await user_resp.json()

        users = user_data.get("data") if isinstance(user_data, dict) else None
        if not isinstance(users, list) or not users:
            return None
        user = users[0] or {}
        return {
            "twitch_login": str(user.get("login") or "").strip().lower(),
            "twitch_user_id": str(user.get("id") or "").strip(),
            "display_name": str(user.get("display_name") or user.get("login") or "").strip(),
        }

    async def index(self, request: web.Request) -> web.StreamResponse:
        """Entrypoint with local-first admin behavior.

        Local requests should land directly in the legacy stats/admin UI.
        Public/proxied requests keep the dashboard selection page.
        """
        if self._is_local_request(request):
            destination = "/twitch/admin"
            fallback = "/twitch/admin"
        else:
            destination = "/twitch/dashboards"
            fallback = "/twitch/dashboards"
        if request.query_string:
            destination = f"{destination}?{request.query_string}"
        safe_destination = self._safe_internal_redirect(destination, fallback=fallback)
        raise web.HTTPFound(safe_destination)

    async def admin(self, request: web.Request) -> web.StreamResponse:
        """Legacy partner admin surface (streamer management)."""
        return await DashboardLiveMixin.index(self, request)

    async def raid_auth_start(self, request: web.Request) -> web.StreamResponse:
        """Create OAuth URL for raid bot authorization."""
        self._require_token(request)
        login = (request.query.get("login") or "").strip().lower()
        if not login:
            return web.Response(text="Missing login parameter", status=400)

        auth_manager = getattr(getattr(self, "_raid_bot", None), "auth_manager", None)
        if not auth_manager:
            return web.Response(text="Raid bot not initialized", status=503)

        auth_url = str(auth_manager.generate_auth_url(login))
        return web.Response(
            text=(
                "<html><head><title>Raid Bot Autorisierung</title></head>"
                "<body style='font-family: sans-serif; max-width: 680px; margin: 48px auto;'>"
                "<h1>Raid Bot Autorisierung</h1>"
                f"<p>Streamer: <strong>{html.escape(login, quote=True)}</strong></p>"
                "<p>Klicke auf den Link unten, um den Raid Bot zu autorisieren:</p>"
                f"<p><a href='{html.escape(auth_url, quote=True)}' "
                "style='padding: 10px 20px; background: #9146FF; color: white; text-decoration: none; border-radius: 5px;'>"
                "Auf Twitch autorisieren</a></p>"
                "<p style='color: #666; font-size: 0.9em;'>"
                "Der Raid Bot kann dann automatisch in deinem Namen raiden, wenn du offline gehst."
                "</p></body></html>"
            ),
            content_type="text/html",
        )

    async def raid_requirements(self, request: web.Request) -> web.StreamResponse:
        """Send raid OAuth requirement DM with one-click fresh link generation."""
        self._require_token(request)

        login = (request.query.get("login") or "").strip().lower()
        if not login:
            return web.Response(text="Missing login parameter", status=400)

        auth_manager = getattr(getattr(self, "_raid_bot", None), "auth_manager", None)
        if not auth_manager:
            return web.Response(text="Raid bot not initialized", status=503)

        try:
            with storage.get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT discord_user_id
                    FROM twitch_streamers
                    WHERE lower(twitch_login) = lower(?)
                    """,
                    (login,),
                ).fetchone()
        except Exception:
            log.exception(
                "Failed to load Discord link for raid requirements (%s)",
                self._sanitize_log_value(login),
            )
            return web.Response(text="Failed to load Discord link", status=500)

        if not row:
            return web.Response(text="Streamer not found", status=404)

        discord_user_id = str(row["discord_user_id"] if hasattr(row, "keys") else row[0] or "").strip()
        if not discord_user_id:
            return web.Response(text="No Discord user linked for this streamer", status=404)

        try:
            user_id_int = int(discord_user_id)
        except (TypeError, ValueError):
            return web.Response(text="Invalid Discord user id", status=400)

        discord_bot = getattr(auth_manager, "_discord_bot", None)
        if not discord_bot:
            return web.Response(text="Discord bot not available", status=503)

        user = discord_bot.get_user(user_id_int)
        if user is None:
            try:
                user = await discord_bot.fetch_user(user_id_int)
            except discord.NotFound:
                user = None
            except discord.HTTPException:
                log.exception(
                    "Failed to fetch Discord user %s for %s",
                    user_id_int,
                    self._sanitize_log_value(login),
                )
                user = None

        if user is None:
            return web.Response(text="Discord user not found", status=404)

        embed = build_raid_requirements_embed(login)
        view = RaidAuthGenerateView(auth_manager=auth_manager, twitch_login=login)

        try:
            await user.send(embed=embed, view=view)
        except discord.Forbidden:
            log.warning(
                "Discord DM blocked for %s (%s)",
                self._sanitize_log_value(login),
                user_id_int,
            )
            return web.Response(text="Discord DM blocked", status=403)
        except discord.HTTPException:
            log.exception(
                "Failed to send raid requirements DM to %s (%s)",
                self._sanitize_log_value(login),
                user_id_int,
            )
            return web.Response(text="Failed to send Discord DM", status=502)

        ok_message = f"Anforderungen per Discord an @{login} gesendet"
        location = self._redirect_location(request, ok=ok_message, default_path="/twitch/admin")
        safe_location = self._safe_internal_redirect(location, fallback="/twitch/admin")
        raise web.HTTPFound(location=safe_location)

    async def raid_history(self, request: web.Request) -> web.StreamResponse:
        """Render raid history table for dashboard operators."""
        self._require_token(request)

        try:
            limit = int((request.query.get("limit") or "50").strip())
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 500))
        from_broadcaster = (request.query.get("from") or "").strip().lower()

        history = await self._raid_history_cb(limit=limit, from_broadcaster=from_broadcaster)

        rows_html = ""
        for entry in history:
            success_icon = "OK" if entry.get("success") else "X"
            executed_at = str(entry.get("executed_at") or "")[:19]
            try:
                stream_duration_min = int(entry.get("stream_duration_sec") or 0) // 60
            except (TypeError, ValueError):
                stream_duration_min = 0
            rows_html += (
                "<tr>"
                f"<td>{html.escape(success_icon)}</td>"
                f"<td>{html.escape(executed_at)}</td>"
                f"<td><strong>{html.escape(str(entry.get('from_broadcaster_login') or ''))}</strong></td>"
                f"<td><strong>{html.escape(str(entry.get('to_broadcaster_login') or ''))}</strong></td>"
                f"<td>{html.escape(str(entry.get('viewer_count') or 0))}</td>"
                f"<td>{html.escape(str(stream_duration_min))} min</td>"
                f"<td>{html.escape(str(entry.get('candidates_count') or 0))}</td>"
                f"<td style='color: red; font-size: 0.85em;'>{html.escape(str(entry.get('error_message') or ''))}</td>"
                "</tr>"
            )

        return web.Response(
            text=(
                "<html><head><title>Raid History</title><style>"
                "body { font-family: sans-serif; margin: 32px; }"
                "table { border-collapse: collapse; width: 100%; }"
                "th, td { border: 1px solid #ddd; padding: 12px 10px; text-align: left; }"
                "th { background-color: #9146FF; color: white; }"
                "tr:nth-child(even) { background-color: #f2f2f2; }"
                "</style></head><body>"
                "<h1>Raid History</h1>"
                "<p><a href='/twitch/admin'>Zurueck zum Dashboard</a></p>"
                "<table><thead><tr>"
                "<th>Status</th><th>Zeitpunkt</th><th>Von</th><th>Nach</th>"
                "<th>Viewer</th><th>Stream-Dauer</th><th>Kandidaten</th><th>Fehler</th>"
                "</tr></thead><tbody>"
                + (rows_html if rows_html else "<tr><td colspan='8'>Keine Raids gefunden</td></tr>")
                + "</tbody></table></body></html>"
            ),
            content_type="text/html",
        )

    async def stats_entry(self, request: web.Request) -> web.StreamResponse:
        """Canonical public entrypoint that links old + beta analytics dashboards."""
        if not self._check_v2_auth(request):
            raise web.HTTPFound("/twitch/auth/login?next=%2Ftwitch%2Fstats")

        legacy_url = self._resolve_legacy_stats_url()
        beta_url = "/twitch/dashboard-v2"
        logout_url = "/twitch/auth/logout"

        html = (
            "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Twitch Stats Dashboard</title>"
            "<style>"
            "body{font-family:Segoe UI,Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;}"
            ".wrap{max-width:980px;margin:0 auto;padding:32px 18px;}"
            ".cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;}"
            ".card{background:#111827;border:1px solid #1f2937;border-radius:12px;padding:18px;}"
            ".btn{display:inline-block;margin-top:10px;padding:10px 14px;border-radius:8px;text-decoration:none;"
            "background:#2563eb;color:#fff;font-weight:600;}"
            ".muted{color:#94a3b8;font-size:14px;}"
            ".top{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;gap:10px;}"
            "a.logout{color:#93c5fd;text-decoration:none;font-size:14px;}"
            "</style></head><body><div class='wrap'>"
            "<div class='top'><h1 style='margin:0;'>Twitch Dashboard Zugang</h1>"
            f"<a class='logout' href='{logout_url}'>Logout</a></div>"
            "<p class='muted'>Beta ist jetzt für verifizierte Streamer-Partner freigeschaltet.</p>"
            "<div class='cards'>"
            "<div class='card'><h2 style='margin-top:0;'>Stats Dashboard (Alt)</h2>"
            "<p class='muted'>Bestehendes Dashboard für die bisherigen Stats-Ansichten.</p>"
            f"<a class='btn' href='{legacy_url}'>Altes Dashboard öffnen</a></div>"
            "<div class='card'><h2 style='margin-top:0;'>Analyse Dashboard (Beta)</h2>"
            "<p class='muted'>Neues v2 Analytics Dashboard mit erweiterten Insights.</p>"
            f"<a class='btn' href='{beta_url}'>Beta Dashboard öffnen</a></div>"
            "</div></div></body></html>"
        )
        return web.Response(text=html, content_type="text/html")

    async def auth_login(self, request: web.Request) -> web.StreamResponse:
        """Kick off Twitch OAuth login for dashboard access."""
        next_path = self._normalize_next_path(request.query.get("next"))

        if self._check_v2_auth(request):
            destination = self._canonical_post_login_destination(next_path)
            raise web.HTTPFound(destination)

        if not self._is_oauth_configured():
            return web.Response(
                text="Twitch OAuth nicht konfiguriert. Bitte TWITCH_CLIENT_ID und TWITCH_CLIENT_SECRET setzen.",
                status=503,
            )

        self._cleanup_auth_state()
        redirect_uri = self._build_oauth_redirect_uri()
        if not redirect_uri:
            return web.Response(
                text=(
                    "Twitch OAuth Redirect-URI ist nicht konfiguriert oder ungültig. "
                    "Bitte TWITCH_DASHBOARD_AUTH_REDIRECT_URI auf "
                    "https://<dein-host>/twitch/auth/callback setzen."
                ),
                status=503,
            )
        state = secrets.token_urlsafe(24)
        self._oauth_states[state] = {
            "created_at": time.time(),
            "next_path": next_path,
            "redirect_uri": redirect_uri,
        }
        auth_url = f"{TWITCH_OAUTH_AUTHORIZE_URL}?{urlencode({'client_id': self._oauth_client_id, 'redirect_uri': redirect_uri, 'response_type': 'code', 'state': state})}"
        safe_auth_url = self._safe_oauth_authorize_redirect(auth_url)
        raise web.HTTPFound(safe_auth_url)

    async def auth_callback(self, request: web.Request) -> web.StreamResponse:
        """Handle Twitch OAuth callback, verify partner status, and create session."""
        if not self._is_oauth_configured():
            return web.Response(text="OAuth ist nicht konfiguriert.", status=503)

        self._cleanup_auth_state()

        error = (request.query.get("error") or "").strip()
        if error:
            return web.Response(
                text=f"OAuth-Fehler: {error}. Bitte Login erneut starten.",
                status=401,
            )

        state = (request.query.get("state") or "").strip()
        code = (request.query.get("code") or "").strip()
        if not state or not code:
            return web.Response(text="Fehlender OAuth state/code.", status=400)

        state_data = self._oauth_states.pop(state, None)
        if not state_data:
            return web.Response(text="OAuth state ungültig oder abgelaufen.", status=400)

        user = await self._exchange_code_for_user(code, str(state_data.get("redirect_uri") or ""))
        if not user:
            return web.Response(text="OAuth-Austausch fehlgeschlagen. Bitte erneut versuchen.", status=401)

        partner = self._is_partner_allowed(
            twitch_login=user.get("twitch_login") or "",
            twitch_user_id=user.get("twitch_user_id") or "",
        )
        if not partner:
            return web.Response(
                text=(
                    f"Kein Zugriff: Twitch-Account '{user.get('display_name') or user.get('twitch_login')}' "
                    "ist nicht als Streamer-Partner freigegeben."
                ),
                status=403,
            )

        session_id = self._create_dashboard_session(
            twitch_login=partner.get("twitch_login") or user.get("twitch_login") or "",
            twitch_user_id=partner.get("twitch_user_id") or user.get("twitch_user_id") or "",
            display_name=user.get("display_name") or "",
        )
        destination = self._safe_internal_redirect(
            self._normalize_next_path(state_data.get("next_path")),
            fallback="/twitch/dashboard-v2",
        )
        response = web.HTTPFound(destination)
        self._set_session_cookie(response, request, session_id)
        raise response

    async def raid_oauth_callback(self, request: web.Request) -> web.StreamResponse:
        """Handle Twitch OAuth callback for raid authorization."""
        raid_bot = self._raid_bot
        auth_manager = getattr(raid_bot, "auth_manager", None) if raid_bot else None

        code = (request.query.get("code") or "").strip()
        state = (request.query.get("state") or "").strip()
        error = (request.query.get("error") or "").strip()

        if error:
            expected_uri = (getattr(auth_manager, "redirect_uri", "") or "").strip()
            expected_html = (
                f"<p><code>{html.escape(expected_uri, quote=True)}</code></p>" if expected_uri else ""
            )
            if error == "redirect_mismatch":
                message = (
                    "<p>Twitch hat die Redirect-URI abgelehnt (redirect_mismatch).</p>"
                    "<p>Bitte trage diese URL exakt in der Twitch Application unter "
                    "<strong>OAuth Redirect URLs</strong> ein und starte die Autorisierung neu:</p>"
                    f"{expected_html}"
                )
            else:
                message = (
                    "<p>OAuth-Fehler beim Autorisieren.</p>"
                    "<p>Bitte die Autorisierung erneut starten.</p>"
                )
            return web.Response(
                text=self._render_oauth_page("Autorisierung fehlgeschlagen", message),
                status=400,
                content_type="text/html",
            )

        if not code or not state:
            return web.Response(
                text=self._render_oauth_page(
                    "Ungültige Anfrage",
                    "<p>Fehlender OAuth Code oder State.</p>",
                ),
                status=400,
                content_type="text/html",
            )

        if not raid_bot or not auth_manager:
            return web.Response(
                text=self._render_oauth_page(
                    "Raid-Bot nicht verfügbar",
                    "<p>Der Raid-Bot ist aktuell nicht initialisiert. Bitte später erneut versuchen.</p>",
                ),
                status=503,
                content_type="text/html",
            )

        login = auth_manager.verify_state(state)
        if not login:
            return web.Response(
                text=self._render_oauth_page(
                    "Ungültiger State",
                    "<p>Der OAuth-State ist ungültig oder abgelaufen. Bitte den Link neu erzeugen.</p>",
                ),
                status=400,
                content_type="text/html",
            )

        session = getattr(raid_bot, "session", None)
        owns_session = False
        if session is None:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
            owns_session = True

        try:
            token_data = await auth_manager.exchange_code_for_token(code, session)

            access_token = str(token_data.get("access_token") or "").strip()
            refresh_token = str(token_data.get("refresh_token") or "").strip()
            if not access_token:
                raise RuntimeError("Missing access_token in Twitch OAuth response")
            if not refresh_token:
                raise RuntimeError("Missing refresh_token in Twitch OAuth response")

            headers = {
                "Client-ID": str(auth_manager.client_id),
                "Authorization": f"Bearer {access_token}",
            }
            async with session.get(TWITCH_HELIX_USERS_URL, headers=headers) as user_resp:
                if user_resp.status != 200:
                    body = await user_resp.text()
                    raise RuntimeError(f"Failed to fetch Twitch user info ({user_resp.status}): {body[:300]}")
                user_payload = await user_resp.json()

            users = user_payload.get("data") if isinstance(user_payload, dict) else None
            if not isinstance(users, list) or not users:
                raise RuntimeError("Missing Twitch user data in OAuth callback")
            user_info = users[0] or {}

            twitch_user_id = str(user_info.get("id") or "").strip()
            twitch_login = str(user_info.get("login") or "").strip().lower()
            if not twitch_user_id or not twitch_login:
                raise RuntimeError("Invalid Twitch user payload in OAuth callback")

            scopes_raw = token_data.get("scope", [])
            if isinstance(scopes_raw, str):
                scopes = [scope for scope in scopes_raw.split() if scope]
            elif isinstance(scopes_raw, list):
                scopes = [str(scope).strip() for scope in scopes_raw if str(scope).strip()]
            else:
                scopes = []

            auth_manager.save_auth(
                twitch_user_id=twitch_user_id,
                twitch_login=twitch_login,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=int(token_data.get("expires_in", 3600) or 3600),
                scopes=scopes,
            )

            post_setup = getattr(raid_bot, "complete_setup_for_streamer", None)
            if callable(post_setup):
                asyncio.create_task(
                    post_setup(twitch_user_id, twitch_login),
                    name="twitch.raid.complete_setup",
                )

            log.info("Raid auth successful for %s", twitch_login)
            success_html = (
                "<p>Der Raid-Bot wurde erfolgreich autorisiert.</p>"
                "<p>Du kannst dieses Fenster jetzt schließen.</p>"
            )
            return web.Response(
                text=self._render_oauth_page("Autorisierung erfolgreich", success_html),
                content_type="text/html",
            )
        except Exception:
            log.exception("Raid OAuth callback failed for state login=%s", login)
            return web.Response(
                text=self._render_oauth_page(
                    "Fehler bei der Autorisierung",
                    "<p>Beim Speichern der Twitch-Autorisierung ist ein interner Fehler aufgetreten.</p>"
                    "<p>Bitte den Vorgang erneut starten.</p>",
                ),
                status=500,
                content_type="text/html",
            )
        finally:
            if owns_session:
                await session.close()

    async def auth_logout(self, request: web.Request) -> web.StreamResponse:
        """Logout and clear dashboard session cookie."""
        session_id = (request.cookies.get(self._session_cookie_name) or "").strip()
        if session_id:
            self._auth_sessions.pop(session_id, None)

        response = web.HTTPFound("/twitch/auth/login?next=%2Ftwitch%2Fdashboard-v2")
        self._clear_session_cookie(response)
        raise response

    async def discord_link(self, request: web.Request) -> web.StreamResponse:
        """Persist Discord profile metadata from the stats dashboard."""
        self._require_token(request)
        if not callable(self._discord_profile):
            location = self._redirect_location(request, err="Discord-Link ist aktuell nicht verfügbar")
            safe_location = self._safe_internal_redirect(location, fallback="/twitch/stats")
            raise web.HTTPFound(location=safe_location)

        data = await request.post()
        login = (data.get("login") or "").strip()
        discord_user_id = (data.get("discord_user_id") or "").strip()
        discord_display_name = (data.get("discord_display_name") or "").strip()
        member_raw = (data.get("member_flag") or "").strip().lower()
        mark_member = member_raw in {"1", "true", "on", "yes"}

        try:
            message = await self._discord_profile(
                login,
                discord_user_id=discord_user_id or None,
                discord_display_name=discord_display_name or None,
                mark_member=mark_member,
            )
            location = self._redirect_location(request, ok=message)
        except ValueError as exc:
            location = self._redirect_location(request, err=str(exc))
        except Exception:
            log.exception("dashboard discord_link failed")
            location = self._redirect_location(request, err="Discord-Daten konnten nicht gespeichert werden")
        safe_location = self._safe_internal_redirect(location, fallback="/twitch/stats")
        raise web.HTTPFound(location=safe_location)

    async def reload_cog(self, request: web.Request) -> web.Response:
        """Optional reload endpoint for admin tooling compatibility."""
        token = (await request.post()).get("token", "")
        if not self._check_admin_token(token):
            return web.Response(text="Unauthorized", status=401)

        if self._reload_cb:
            msg = await self._reload_cb()
            return web.Response(text=msg)
        return web.Response(text="Kein Reload-Handler definiert", status=501)

    def attach(self, app: web.Application) -> None:
        app.add_routes([
            web.get("/", self.index),
            web.get("/twitch", self.index),
            web.get("/twitch/", self.index),
            web.get("/twitch/admin", self.admin),
            web.get("/twitch/live", self.admin),
            web.get("/twitch/add_any", self.add_any),
            web.get("/twitch/add_url", self.add_url),
            web.get("/twitch/add_login/{login}", self.add_login),
            web.post("/twitch/add_streamer", self.add_streamer),
            web.post("/twitch/remove", self.remove),
            web.post("/twitch/verify", self.verify),
            web.post("/twitch/archive", self.archive),
            web.post("/twitch/discord_flag", self.discord_flag),
            web.get("/twitch/stats", self.stats),
            web.get("/twitch/partners", self.partner_stats),
            web.get("/twitch/dashboards", self.stats_entry),
            web.get("/twitch/raid/auth", self.raid_auth_start),
            web.get("/twitch/raid/requirements", self.raid_requirements),
            web.get("/twitch/raid/history", self.raid_history),
            web.get("/twitch/auth/login", self.auth_login),
            web.get("/twitch/auth/callback", self.auth_callback),
            web.get("/twitch/auth/logout", self.auth_logout),
            web.get("/twitch/raid/callback", self.raid_oauth_callback),
            web.post("/twitch/discord_link", self.discord_link),
            web.post("/twitch/reload", self.reload_cog),
        ])
        self._register_v2_routes(app.router)


def build_v2_app(
    *,
    noauth: bool,
    token: Optional[str],
    partner_token: Optional[str] = None,
    oauth_client_id: Optional[str] = None,
    oauth_client_secret: Optional[str] = None,
    oauth_redirect_uri: Optional[str] = None,
    session_ttl_seconds: int = 12 * 3600,
    legacy_stats_url: Optional[str] = None,
    add_cb: Optional[Callable[[str, bool], Awaitable[str]]] = None,
    remove_cb: Optional[Callable[[str], Awaitable[str]]] = None,
    list_cb: Optional[Callable[[], Awaitable[List[dict]]]] = None,
    stats_cb: Optional[Callable[..., Awaitable[dict]]] = None,
    verify_cb: Optional[Callable[[str, str], Awaitable[str]]] = None,
    archive_cb: Optional[Callable[[str, str], Awaitable[str]]] = None,
    discord_flag_cb: Optional[Callable[[str, bool], Awaitable[str]]] = None,
    discord_profile_cb: Optional[Callable[[str, Optional[str], Optional[str], bool], Awaitable[str]]] = None,
    raid_history_cb: Optional[Callable[..., Awaitable[List[dict]]]] = None,
    raid_bot: Optional[Any] = None,
    reload_cb: Optional[Callable[[], Awaitable[str]]] = None,
) -> web.Application:
    app = web.Application()
    DashboardV2Server(
        app_token=token,
        noauth=noauth,
        partner_token=partner_token,
        oauth_client_id=oauth_client_id,
        oauth_client_secret=oauth_client_secret,
        oauth_redirect_uri=oauth_redirect_uri,
        session_ttl_seconds=session_ttl_seconds,
        legacy_stats_url=legacy_stats_url,
        add_cb=add_cb,
        remove_cb=remove_cb,
        list_cb=list_cb,
        stats_cb=stats_cb,
        verify_cb=verify_cb,
        archive_cb=archive_cb,
        discord_flag_cb=discord_flag_cb,
        discord_profile_cb=discord_profile_cb,
        raid_history_cb=raid_history_cb,
        raid_bot=raid_bot,
        reload_cb=reload_cb,
    ).attach(app)
    return app


__all__ = ["DashboardV2Server", "build_v2_app"]
