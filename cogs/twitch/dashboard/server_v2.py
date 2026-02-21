"""Embedded aiohttp app serving only the Twitch analytics dashboard v2."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlsplit, urlunsplit

import aiohttp
import discord
from aiohttp import web

from .. import storage
from ..analytics.api_v2 import AnalyticsV2Mixin
from ..constants import log
from ..raid.views import RaidAuthGenerateView, build_raid_requirements_embed
from .live import DashboardLiveMixin
from .stats import DashboardStatsMixin
from .templates import DashboardTemplateMixin

TWITCH_OAUTH_AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TWITCH_OAUTH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"  # noqa: S105
TWITCH_HELIX_USERS_URL = "https://api.twitch.tv/helix/users"
DISCORD_API_BASE_URL = "https://discord.com/api/v10"
TWITCH_DASHBOARDS_LOGIN_URL = "/twitch/auth/login?next=%2Ftwitch%2Fdashboards"
TWITCH_DASHBOARD_V2_LOGIN_URL = "/twitch/auth/login?next=%2Ftwitch%2Fdashboard-v2"
TWITCH_ADMIN_DISCORD_LOGIN_URL = "/twitch/auth/discord/login?next=%2Ftwitch%2Fadmin"
TWITCH_DASHBOARDS_DISCORD_LOGIN_URL = "/twitch/auth/discord/login?next=%2Ftwitch%2Fdashboards"
LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{3,25}$")
DEFAULT_DASHBOARD_MODERATOR_ROLE_ID = 1337518124647579661
DEFAULT_DASHBOARD_OWNER_USER_ID = 662995601738170389
KEYRING_SERVICE_NAME = "DeadlockBot"
TWITCH_ADMIN_PUBLIC_URL = (
    os.getenv("TWITCH_ADMIN_PUBLIC_URL")
    or os.getenv("MASTER_DASHBOARD_PUBLIC_URL")
    or "https://admin.earlysalty.de"
).strip()
TWITCH_ADMIN_DISCORD_REDIRECT_URI = (
    os.getenv("TWITCH_ADMIN_DISCORD_REDIRECT_URI")
    or f"{TWITCH_ADMIN_PUBLIC_URL.rstrip('/')}/twitch/auth/discord/callback"
).strip()


class DashboardV2Server(
    DashboardLiveMixin, DashboardStatsMixin, DashboardTemplateMixin, AnalyticsV2Mixin
):
    """Minimal dashboard server exposing only v2 routes and APIs."""

    def __init__(
        self,
        *,
        app_token: str | None,
        noauth: bool,
        partner_token: str | None,
        oauth_client_id: str | None = None,
        oauth_client_secret: str | None = None,
        oauth_redirect_uri: str | None = None,
        session_ttl_seconds: int = 12 * 3600,
        legacy_stats_url: str | None = None,
        add_cb: Callable[[str, bool], Awaitable[str]] | None = None,
        remove_cb: Callable[[str], Awaitable[str]] | None = None,
        list_cb: Callable[[], Awaitable[list[dict]]] | None = None,
        stats_cb: Callable[..., Awaitable[dict]] | None = None,
        verify_cb: Callable[[str, str], Awaitable[str]] | None = None,
        archive_cb: Callable[[str, str], Awaitable[str]] | None = None,
        discord_flag_cb: Callable[[str, bool], Awaitable[str]] | None = None,
        discord_profile_cb: Callable[[str, str | None, str | None, bool], Awaitable[str]]
        | None = None,
        raid_history_cb: Callable[..., Awaitable[list[dict]]] | None = None,
        raid_bot: Any | None = None,
        reload_cb: Callable[[], Awaitable[str]] | None = None,
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
        self._oauth_states: dict[str, dict[str, Any]] = {}
        self._auth_sessions: dict[str, dict[str, Any]] = {}
        self._oauth_state_ttl_seconds = 600
        self._rate_limits: dict[str, list[float]] = {}
        self._add = add_cb if callable(add_cb) else self._empty_add
        self._remove = remove_cb if callable(remove_cb) else self._empty_remove
        self._list = list_cb if callable(list_cb) else self._empty_list
        self._stats = stats_cb if callable(stats_cb) else self._empty_stats
        self._verify = verify_cb if callable(verify_cb) else self._empty_verify
        self._archive = archive_cb if callable(archive_cb) else self._empty_archive
        self._discord_flag = (
            discord_flag_cb if callable(discord_flag_cb) else self._empty_discord_flag
        )
        self._discord_profile = discord_profile_cb
        self._raid_history_cb = (
            raid_history_cb if callable(raid_history_cb) else self._empty_raid_history
        )
        self._raid_bot = raid_bot
        self._redirect_uri = str(
            getattr(getattr(raid_bot, "auth_manager", None), "redirect_uri", "") or ""
        ).strip()
        self._master_dashboard_href = "/admin"
        keyring_client_id = self._read_keyring_secret("DISCORD_OAUTH_CLIENT_ID")
        discord_bot = None
        auth_manager = getattr(raid_bot, "auth_manager", None) if raid_bot else None
        if auth_manager is not None:
            discord_bot = getattr(auth_manager, "_discord_bot", None)
        if discord_bot is None and raid_bot is not None:
            discord_bot = getattr(raid_bot, "_discord_bot", None)
        app_client_id = str(getattr(discord_bot, "application_id", "") or "").strip()
        self._discord_admin_client_id = (keyring_client_id or app_client_id).strip()
        self._discord_admin_client_secret = self._read_keyring_secret(
            "DISCORD_OAUTH_CLIENT_SECRET"
        ).strip()
        self._discord_admin_redirect_uri = TWITCH_ADMIN_DISCORD_REDIRECT_URI
        self._discord_admin_enabled = True
        self._discord_admin_owner_user_id = DEFAULT_DASHBOARD_OWNER_USER_ID
        self._discord_admin_moderator_role_id = DEFAULT_DASHBOARD_MODERATOR_ROLE_ID
        self._discord_admin_guild_ids: tuple[int, ...] = ()
        self._discord_admin_cookie_name = "twitch_admin_session"
        self._discord_admin_session_ttl = 12 * 3600
        self._discord_admin_state_ttl = 600
        self._discord_admin_oauth_states: dict[str, dict[str, Any]] = {}
        self._discord_admin_sessions: dict[str, dict[str, Any]] = {}
        self._discord_admin_required = self._discord_admin_enabled and bool(
            self._discord_admin_client_id
            and self._discord_admin_client_secret
            and self._discord_admin_redirect_uri
        )
        if self._discord_admin_enabled and not self._discord_admin_required:
            log.warning(
                "Twitch Admin Discord OAuth ist unvollständig (Client ID/Secret/Redirect fehlen). "
                "Fallback auf Token/localhost."
            )

    async def _empty_add(self, _: str, __: bool) -> str:
        return "Add-Funktion ist aktuell nicht verfügbar"

    async def _empty_remove(self, _: str) -> str:
        return "Remove-Funktion ist aktuell nicht verfügbar"

    async def _empty_list(self) -> list[dict]:
        return []

    async def _empty_stats(self, **_: Any) -> dict:
        return {"tracked": {}, "category": {}}

    async def _empty_verify(self, _: str, __: str) -> str:
        return "Verify-Funktion ist aktuell nicht verfügbar"

    async def _empty_archive(self, _: str, __: str) -> str:
        return "Archive-Funktion ist aktuell nicht verfügbar"

    async def _empty_discord_flag(self, _: str, __: bool) -> str:
        return "Discord-Flag-Funktion ist aktuell nicht verfügbar"

    async def _empty_raid_history(self, **_: Any) -> list[dict]:
        return []

    @staticmethod
    def _read_keyring_secret(key: str) -> str:
        secret_key = (key or "").strip()
        if not secret_key:
            return ""
        try:
            import keyring
        except Exception:
            return ""
        try:
            value = keyring.get_password(KEYRING_SERVICE_NAME, secret_key)
            if not value:
                value = keyring.get_password(f"{secret_key}@{KEYRING_SERVICE_NAME}", secret_key)
        except Exception:
            return ""
        return str(value or "").strip()

    def _check_admin_token(self, token: str | None) -> bool:
        if self._noauth:
            return True
        if not token or not self._token:
            return False
        return token == self._token

    @staticmethod
    def _host_without_port(raw: str | None) -> str:
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
    def _is_loopback_host(raw: str | None) -> bool:
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
    def _normalize_login(value: str) -> str | None:
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

    @staticmethod
    def _normalize_discord_admin_next_path(raw: str | None) -> str:
        fallback = "/twitch/admin"
        candidate = (raw or "").strip()
        if not candidate:
            return fallback
        try:
            parts = urlsplit(candidate)
        except Exception:
            return fallback
        if parts.scheme or parts.netloc:
            return fallback
        if not candidate.startswith("/") or not candidate.startswith("/twitch"):
            return fallback
        return candidate

    def _build_discord_admin_login_url(
        self, request: web.Request, *, next_path: str | None = None
    ) -> str:
        if not self._discord_admin_required:
            return "/twitch/admin"
        normalized_next = self._normalize_discord_admin_next_path(
            next_path or (request.rel_url.path_qs if request.rel_url else "/twitch/admin")
        )
        return f"/twitch/auth/discord/login?{urlencode({'next': normalized_next})}"

    @staticmethod
    def _canonical_discord_admin_post_login_path(raw: str | None) -> str:
        normalized = DashboardV2Server._normalize_discord_admin_next_path(raw)
        normalized_path = (urlsplit(normalized).path or "").rstrip("/") or "/"
        if normalized_path == "/twitch/dashboards":
            return "/twitch/dashboards"
        return "/twitch/admin"

    def _normalized_discord_admin_redirect_uri(self) -> str | None:
        raw = (self._discord_admin_redirect_uri or "").strip()
        if not raw:
            return None
        candidate = raw if "://" in raw else f"https://{raw}"
        try:
            parsed = urlparse(candidate)
        except Exception:
            return None
        scheme = (parsed.scheme or "").strip().lower()
        host = (parsed.hostname or "").strip().lower()
        if scheme not in {"http", "https"}:
            return None
        if scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
            return None
        if parsed.username or parsed.password or not parsed.netloc:
            return None
        if (parsed.path or "").rstrip("/") != "/twitch/auth/discord/callback":
            return None
        return urlunsplit((scheme, parsed.netloc, "/twitch/auth/discord/callback", "", ""))

    def _cleanup_discord_admin_state(self) -> None:
        now = time.time()
        expired_states = [
            key
            for key, row in self._discord_admin_oauth_states.items()
            if now - float(row.get("created_at", 0.0)) > self._discord_admin_state_ttl
        ]
        for key in expired_states:
            self._discord_admin_oauth_states.pop(key, None)

        expired_sessions = [
            key
            for key, row in self._discord_admin_sessions.items()
            if float(row.get("expires_at", 0.0)) <= now
        ]
        for key in expired_sessions:
            self._discord_admin_sessions.pop(key, None)

        max_states = 1000
        if len(self._discord_admin_oauth_states) > max_states:
            oldest_states = sorted(
                self._discord_admin_oauth_states.items(),
                key=lambda item: float(item[1].get("created_at", 0.0)),
            )
            for key, _ in oldest_states[: len(self._discord_admin_oauth_states) - max_states]:
                self._discord_admin_oauth_states.pop(key, None)

        max_sessions = 5000
        if len(self._discord_admin_sessions) > max_sessions:
            oldest_sessions = sorted(
                self._discord_admin_sessions.items(),
                key=lambda item: float(item[1].get("created_at", 0.0)),
            )
            for key, _ in oldest_sessions[: len(self._discord_admin_sessions) - max_sessions]:
                self._discord_admin_sessions.pop(key, None)

    def _set_discord_admin_cookie(
        self,
        response: web.StreamResponse,
        request: web.Request,
        session_id: str,
    ) -> None:
        response.set_cookie(
            self._discord_admin_cookie_name,
            session_id,
            max_age=self._discord_admin_session_ttl,
            httponly=True,
            secure=self._is_secure_request(request),
            samesite="Lax",
            path="/",
        )

    def _clear_discord_admin_cookie(self, response: web.StreamResponse) -> None:
        response.del_cookie(self._discord_admin_cookie_name, path="/")

    def _get_discord_admin_session(self, request: web.Request) -> dict[str, Any] | None:
        if not self._discord_admin_required:
            return None
        self._cleanup_discord_admin_state()
        session_id = (request.cookies.get(self._discord_admin_cookie_name) or "").strip()
        if not session_id:
            return None
        session = self._discord_admin_sessions.get(session_id)
        if not session:
            return None
        now = time.time()
        if float(session.get("expires_at", 0.0)) <= now:
            self._discord_admin_sessions.pop(session_id, None)
            return None
        session["expires_at"] = now + self._discord_admin_session_ttl
        session["last_seen_at"] = now
        session.setdefault("auth_type", "discord_admin")
        return session

    def _is_discord_admin_request(self, request: web.Request) -> bool:
        return bool(self._get_discord_admin_session(request))

    async def _exchange_discord_admin_code(
        self,
        code: str,
        redirect_uri: str,
    ) -> dict[str, Any] | None:
        payload = {
            "client_id": self._discord_admin_client_id,
            "client_secret": self._discord_admin_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{DISCORD_API_BASE_URL}/oauth2/token",
                data=payload,
                headers=headers,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    log.warning(
                        "Discord admin OAuth exchange failed (status=%s body=%s)",
                        response.status,
                        self._sanitize_log_value(body[:200]),
                    )
                    return None
                data = await response.json()
        return data if isinstance(data, dict) else None

    async def _fetch_discord_admin_user(self, access_token: str) -> dict[str, Any] | None:
        if not access_token:
            return None
        timeout = aiohttp.ClientTimeout(total=20)
        headers = {"Authorization": f"Bearer {access_token}"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{DISCORD_API_BASE_URL}/users/@me", headers=headers
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    log.warning(
                        "Discord admin user lookup failed (status=%s body=%s)",
                        response.status,
                        self._sanitize_log_value(body[:200]),
                    )
                    return None
                data = await response.json()
        return data if isinstance(data, dict) else None

    async def _check_discord_admin_membership(self, user_id: int) -> tuple[bool, str]:
        if user_id == self._discord_admin_owner_user_id:
            return True, "owner_override"

        discord_bot = None
        raid_bot = getattr(self, "_raid_bot", None)
        if raid_bot is not None:
            auth_manager = getattr(raid_bot, "auth_manager", None)
            discord_bot = getattr(auth_manager, "_discord_bot", None) if auth_manager else None
            if discord_bot is None:
                discord_bot = getattr(raid_bot, "_discord_bot", None)

        guilds: list[Any] = []
        seen: set[int] = set()
        for guild_id in self._discord_admin_guild_ids:
            guild = discord_bot.get_guild(guild_id) if discord_bot else None
            if guild and guild.id not in seen:
                guilds.append(guild)
                seen.add(guild.id)
        if not guilds and discord_bot:
            guilds = list(getattr(discord_bot, "guilds", []) or [])

        for guild in guilds:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except Exception:
                    member = None
            if member is None:
                continue
            perms = getattr(member, "guild_permissions", None)
            if perms and bool(getattr(perms, "administrator", False)):
                return True, f"guild_admin:{guild.id}"
            role_ids = {
                int(role.id) for role in getattr(member, "roles", []) if getattr(role, "id", None)
            }
            if self._discord_admin_moderator_role_id in role_ids:
                return True, f"moderator_role:{guild.id}"
        return False, "missing_admin_or_moderator_role"

    async def discord_auth_login(self, request: web.Request) -> web.StreamResponse:
        if not self._discord_admin_required:
            raise web.HTTPFound("/twitch/admin")
        existing = self._get_discord_admin_session(request)
        next_path = self._normalize_discord_admin_next_path(request.query.get("next"))
        if existing:
            destination = self._canonical_discord_admin_post_login_path(next_path)
            raise web.HTTPFound(destination)

        redirect_uri = self._normalized_discord_admin_redirect_uri()
        if not redirect_uri:
            expected_redirect = (
                str(self._discord_admin_redirect_uri or "").strip()
                or "https://admin.earlysalty.de/twitch/auth/discord/callback"
            )
            return web.Response(
                text=(
                    "Discord OAuth Redirect URI ist ungültig. "
                    f"Erwartet wird exakt: {expected_redirect}."
                ),
                status=503,
            )

        self._cleanup_discord_admin_state()
        state = secrets.token_urlsafe(32)
        self._discord_admin_oauth_states[state] = {
            "created_at": time.time(),
            "next_path": next_path,
            "redirect_uri": redirect_uri,
        }
        query = urlencode(
            {
                "client_id": self._discord_admin_client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "identify",
                "state": state,
            }
        )
        raise web.HTTPFound(f"{DISCORD_API_BASE_URL}/oauth2/authorize?{query}")

    async def discord_auth_callback(self, request: web.Request) -> web.StreamResponse:
        if not self._discord_admin_required:
            raise web.HTTPFound("/twitch/admin")

        error = (request.query.get("error") or "").strip()
        if error:
            return web.Response(text=f"Discord OAuth Fehler: {error}", status=401)

        state = (request.query.get("state") or "").strip()
        code = (request.query.get("code") or "").strip()
        if not state or not code:
            return web.Response(text="Fehlender OAuth state/code.", status=400)

        self._cleanup_discord_admin_state()
        state_data = self._discord_admin_oauth_states.pop(state, None)
        if not state_data:
            return web.Response(text="OAuth state ungültig oder abgelaufen.", status=400)

        token_data = await self._exchange_discord_admin_code(
            code,
            str(state_data.get("redirect_uri") or ""),
        )
        access_token = str((token_data or {}).get("access_token") or "").strip()
        if not access_token:
            return web.Response(text="OAuth Austausch fehlgeschlagen.", status=401)

        user = await self._fetch_discord_admin_user(access_token)
        if not user:
            return web.Response(text="Discord User konnte nicht geladen werden.", status=401)

        user_id_raw = str(user.get("id") or "").strip()
        if not user_id_raw.isdigit():
            return web.Response(text="Ungültige Discord User-ID.", status=401)
        user_id = int(user_id_raw)
        allowed, reason = await self._check_discord_admin_membership(user_id)
        if not allowed:
            log.warning(
                "AUDIT twitch-dashboard discord login denied: user=%s reason=%s peer=%s",
                user_id,
                self._sanitize_log_value(reason),
                self._sanitize_log_value(self._peer_host(request)),
            )
            return web.Response(
                text=(
                    "Kein Zugriff. Es wird Administrator-Recht oder die Moderator-Rolle benötigt."
                ),
                status=403,
            )

        username = str(user.get("username") or "").strip()
        global_name = str(user.get("global_name") or "").strip()
        discriminator = str(user.get("discriminator") or "0").strip()
        if global_name:
            display_name = global_name
        elif discriminator and discriminator != "0":
            display_name = f"{username}#{discriminator}"
        else:
            display_name = username or f"User {user_id}"

        now = time.time()
        session_id = secrets.token_urlsafe(32)
        self._discord_admin_sessions[session_id] = {
            "auth_type": "discord_admin",
            "user_id": user_id,
            "username": username,
            "display_name": display_name,
            "reason": reason,
            "created_at": now,
            "last_seen_at": now,
            "expires_at": now + self._discord_admin_session_ttl,
        }

        log.info(
            "AUDIT twitch-dashboard discord login success: user=%s reason=%s peer=%s",
            user_id,
            self._sanitize_log_value(reason),
            self._sanitize_log_value(self._peer_host(request)),
        )

        destination = self._canonical_discord_admin_post_login_path(state_data.get("next_path"))
        response = web.HTTPFound(destination)
        self._set_discord_admin_cookie(response, request, session_id)
        raise response

    async def discord_auth_logout(self, request: web.Request) -> web.StreamResponse:
        session_id = (request.cookies.get(self._discord_admin_cookie_name) or "").strip()
        if session_id:
            self._discord_admin_sessions.pop(session_id, None)
        login_url = (
            TWITCH_ADMIN_DISCORD_LOGIN_URL if self._discord_admin_required else "/twitch/admin"
        )
        response = web.HTTPFound(login_url)
        self._clear_discord_admin_cookie(response)
        raise response

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
            "/twitch/raid/analytics",
            "/twitch/reload",
            "/twitch/market",
        )
        if request.path.startswith(admin_only_prefixes):
            token = request.headers.get("X-Admin-Token") or request.query.get("token")
            if self._is_local_request(request):
                return
            if self._is_discord_admin_request(request):
                return
            if self._check_admin_token(token):
                return
            login_url = (
                TWITCH_ADMIN_DISCORD_LOGIN_URL if self._discord_admin_required else "/twitch/admin"
            )
            if request.method in {"GET", "HEAD"}:
                raise web.HTTPFound(login_url)
            raise web.HTTPUnauthorized(
                text="Admin authentication required",
                headers={"X-Auth-Login": login_url},
            )

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
        ok: str | None = None,
        err: str | None = None,
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
                "/twitch/raid/analytics",
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

        params: dict[str, str] = {}
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

        # Hard cap to prevent unbounded growth under heavy abuse
        _MAX_STATES = 500
        if len(self._oauth_states) > _MAX_STATES:
            oldest = sorted(
                self._oauth_states.items(),
                key=lambda kv: float(kv[1].get("created_at", 0)),
            )
            for k, _ in oldest[: len(self._oauth_states) - _MAX_STATES]:
                self._oauth_states.pop(k, None)

        _MAX_SESSIONS = 2000
        if len(self._auth_sessions) > _MAX_SESSIONS:
            oldest_s = sorted(
                self._auth_sessions.items(),
                key=lambda kv: float(kv[1].get("created_at", 0)),
            )
            for k, _ in oldest_s[: len(self._auth_sessions) - _MAX_SESSIONS]:
                self._auth_sessions.pop(k, None)

    def _check_rate_limit(
        self,
        request: web.Request,
        *,
        max_requests: int = 10,
        window_seconds: float = 60.0,
    ) -> bool:
        """Sliding-window rate limiter per peer IP.  Returns True if allowed."""
        peer = self._peer_host(request)
        now = time.time()
        hits = self._rate_limits.get(peer, [])
        hits = [t for t in hits if now - t < window_seconds]
        if len(hits) >= max_requests:
            self._rate_limits[peer] = hits
            return False
        hits.append(now)
        self._rate_limits[peer] = hits
        # Prevent unbounded growth – clear when tracking too many distinct IPs
        if len(self._rate_limits) > 1000:
            self._rate_limits.clear()
        return True

    def _is_oauth_configured(self) -> bool:
        return bool(self._oauth_client_id and self._oauth_client_secret)

    def _is_secure_request(self, request: web.Request) -> bool:
        # Only trust X-Forwarded-Proto when the TCP peer is a loopback address
        # (i.e. a trusted local proxy).  Accepting this header from arbitrary
        # clients would allow spoofing the "secure" flag and mis-marking cookies.
        peer = self._peer_host(request)
        if self._is_loopback_host(peer):
            forwarded_proto = (
                (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
            )
            if forwarded_proto:
                return forwarded_proto == "https"
        return bool(request.secure)

    def _build_oauth_redirect_uri(self) -> str | None:
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
            log.warning(
                "TWITCH_DASHBOARD_AUTH_REDIRECT_URI must use https unless host is localhost"
            )
            return None
        if not parsed.netloc:
            log.warning("TWITCH_DASHBOARD_AUTH_REDIRECT_URI is missing host")
            return None
        if path == "/twitch/raid/callback":
            log.warning(
                "TWITCH_DASHBOARD_AUTH_REDIRECT_URI points to raid callback and is not allowed"
            )
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

    def _normalize_next_path(self, raw_path: str | None) -> str:
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
    def _safe_internal_redirect(
        location: str | None, *, fallback: str = "/twitch/dashboard-v2"
    ) -> str:
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
    def _safe_oauth_authorize_redirect(location: str | None) -> str:
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
    def _canonical_post_login_destination(next_path: str | None) -> str:
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
        next_path = self._normalize_next_path(
            request.rel_url.path_qs if request.rel_url else "/twitch/dashboard-v2"
        )
        if self._should_use_discord_admin_login(request):
            return self._build_discord_admin_login_url(request, next_path=next_path)
        return f"/twitch/auth/login?{urlencode({'next': next_path})}"

    def _should_use_discord_admin_login(self, request: web.Request) -> bool:
        if not self._discord_admin_required:
            return False
        admin_context_prefixes = (
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
            "/twitch/raid/analytics",
            "/twitch/reload",
            "/twitch/market",
        )
        return request.path.startswith(admin_context_prefixes)

    def _resolve_legacy_stats_url(self) -> str:
        # The legacy stats dashboard is now always served locally.
        return "/twitch/stats"

    def _get_dashboard_auth_session(self, request: web.Request) -> dict[str, Any] | None:
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

    def _set_session_cookie(
        self, response: web.StreamResponse, request: web.Request, session_id: str
    ) -> None:
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

    def _create_dashboard_session(
        self, *, twitch_login: str, twitch_user_id: str, display_name: str
    ) -> str:
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

    def _is_partner_allowed(
        self, *, twitch_login: str, twitch_user_id: str
    ) -> dict[str, Any] | None:
        login = (twitch_login or "").strip().lower()
        user_id = (twitch_user_id or "").strip()
        if not login and not user_id:
            return None

        with storage.get_conn() as conn:
            row = conn.execute(
                """
                SELECT twitch_login, twitch_user_id
                FROM twitch_streamers_partner_state
                WHERE is_partner_active = 1
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

    async def _exchange_code_for_user(self, code: str, redirect_uri: str) -> dict[str, str] | None:
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
                    log.warning(
                        "Dashboard OAuth exchange failed with status %s",
                        token_resp.status,
                    )
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
                    log.warning(
                        "Dashboard OAuth user lookup failed with status %s",
                        user_resp.status,
                    )
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
        if self._is_local_request(request) or self._is_discord_admin_request(request):
            destination = "/twitch/admin"
            fallback = "/twitch/admin"
        else:
            destination = "/twitch/dashboards"
            fallback = "/twitch/dashboards"
        if request.query_string:
            destination = f"{destination}?{request.query_string}"
        safe_destination = self._safe_internal_redirect(destination, fallback=fallback)
        raise web.HTTPFound(safe_destination)

    async def public_home(self, request: web.Request) -> web.StreamResponse:
        """Public homepage for OAuth verification and app information."""
        dashboard_url = (
            "/twitch/dashboards"
            if self._check_v2_auth(request)
            else TWITCH_DASHBOARDS_DISCORD_LOGIN_URL
            if self._should_use_discord_admin_login(request)
            else TWITCH_DASHBOARDS_LOGIN_URL
        )
        dashboard_label = (
            "Dashboard oeffnen"
            if self._check_v2_auth(request)
            else "Mit Discord anmelden"
            if self._should_use_discord_admin_login(request)
            else "Mit Twitch anmelden"
        )
        safe_dashboard_url = html.escape(
            self._safe_internal_redirect(dashboard_url, fallback="/twitch/dashboards"),
            quote=True,
        )
        safe_dashboard_label = html.escape(dashboard_label, quote=True)

        page = (
            "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Deutsche Deadlock Community</title>"
            "<style>"
            ":root{color-scheme:light;}"
            "body{margin:0;background:#f8fafc;color:#0f172a;font-family:Segoe UI,Arial,sans-serif;line-height:1.55;}"
            ".wrap{max-width:980px;margin:0 auto;padding:30px 18px 44px;}"
            ".top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;}"
            "h1{margin:0;font-size:1.85rem;}"
            ".tag{display:inline-block;margin-top:10px;padding:5px 10px;border-radius:999px;background:#dbeafe;color:#1e3a8a;font-weight:600;font-size:.85rem;}"
            ".panel{margin-top:18px;background:#ffffff;border:1px solid #dbe2ea;border-radius:14px;padding:18px;}"
            ".muted{color:#334155;}"
            ".actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;}"
            ".btn{display:inline-block;padding:10px 14px;border-radius:10px;text-decoration:none;font-weight:600;}"
            ".btn-primary{background:#2563eb;color:#fff;}"
            ".btn-secondary{border:1px solid #cbd5e1;color:#0f172a;background:#fff;}"
            "footer{margin-top:22px;padding-top:12px;border-top:1px solid #e2e8f0;color:#475569;font-size:.92rem;}"
            "a{color:#1d4ed8;}"
            "</style></head><body><main class='wrap'>"
            "<div class='top'>"
            "<h1>Deutsche Deadlock Community</h1>"
            "<a href='/privacy'>Datenschutzerklaerung</a>"
            "</div>"
            "<div class='tag'>Offizielle App-Startseite</div>"
            "<section class='panel'>"
            "<h2 style='margin-top:0;'>Wozu dient diese App?</h2>"
            "<p class='muted'>"
            "Diese App wird von der <strong>Deutsche Deadlock Community</strong> betrieben und unterstuetzt "
            "verifizierte Community-Streamer bei Twitch-Funktionen: Analytics-Dashboard, Raid-Autorisierung "
            "und Clip-Management inklusive Social-Media-Veroeffentlichung."
            "</p>"
            "<p class='muted'>"
            "Die App ist ein Community-Tool fuer Streamer-Partner. Allgemeine Informationen (inklusive "
            "Datenschutz und Nutzungsbedingungen) sind ohne Anmeldung aufrufbar."
            "</p>"
            "<div class='actions'>"
            f"<a class='btn btn-primary' href='{safe_dashboard_url}'>{safe_dashboard_label}</a>"
            "<a class='btn btn-secondary' href='/terms'>Nutzungsbedingungen</a>"
            "<a class='btn btn-secondary' href='/privacy'>Datenschutzerklaerung</a>"
            "</div>"
            "</section>"
            "<footer>"
            "App-Name im OAuth-Zustimmungsbildschirm: <strong>Deutsche Deadlock Community</strong>"
            "</footer>"
            "</main></body></html>"
        )
        return web.Response(text=page, content_type="text/html", charset="utf-8")

    async def admin(self, request: web.Request) -> web.StreamResponse:
        """Legacy partner admin surface (streamer management)."""
        return await DashboardLiveMixin.index(self, request)

    @staticmethod
    def _build_raid_auth_start_html(login: str, auth_url: str) -> str:
        safe_login = html.escape(login, quote=True)
        safe_auth_url = html.escape(auth_url, quote=True)
        return "".join(
            [
                "<html><head><title>Raid Bot Autorisierung</title></head>",
                "<body style='font-family: sans-serif; max-width: 680px; margin: 48px auto;'>",
                "<h1>Raid Bot Autorisierung</h1>",
                "<p>Streamer: <strong>",
                safe_login,
                "</strong></p>",
                "<p>Klicke auf den Link unten, um den Raid Bot zu autorisieren:</p>",
                "<p><a href='",
                safe_auth_url,
                "' style='padding: 10px 20px; background: #9146FF; color: white; text-decoration: none; border-radius: 5px;'>",
                "Auf Twitch autorisieren</a></p>",
                "<p style='color: #666; font-size: 0.9em;'>",
                "Der Raid Bot kann dann automatisch in deinem Namen raiden, wenn du offline gehst.",
                "</p></body></html>",
            ]
        )

    @staticmethod
    def _build_raid_history_rows(history: list[dict]) -> str:
        rows: list[str] = []
        for entry in history:
            success_icon = "OK" if entry.get("success") else "X"
            executed_at = str(entry.get("executed_at") or "")[:19]
            try:
                stream_duration_min = int(entry.get("stream_duration_sec") or 0) // 60
            except (TypeError, ValueError):
                stream_duration_min = 0

            rows.append(
                "".join(
                    [
                        "<tr>",
                        "<td>",
                        html.escape(success_icon, quote=True),
                        "</td>",
                        "<td>",
                        html.escape(executed_at, quote=True),
                        "</td>",
                        "<td><strong>",
                        html.escape(str(entry.get("from_broadcaster_login") or ""), quote=True),
                        "</strong></td>",
                        "<td><strong>",
                        html.escape(str(entry.get("to_broadcaster_login") or ""), quote=True),
                        "</strong></td>",
                        "<td>",
                        html.escape(str(entry.get("viewer_count") or 0), quote=True),
                        "</td>",
                        "<td>",
                        html.escape(str(stream_duration_min), quote=True),
                        " min</td>",
                        "<td>",
                        html.escape(str(entry.get("candidates_count") or 0), quote=True),
                        "</td>",
                        "<td style='color: red; font-size: 0.85em;'>",
                        html.escape(str(entry.get("error_message") or ""), quote=True),
                        "</td>",
                        "</tr>",
                    ]
                )
            )

        if rows:
            return "".join(rows)
        return "<tr><td colspan='8'>Keine Raids gefunden</td></tr>"

    @staticmethod
    def _build_raid_history_page(rows_html: str) -> str:
        return "".join(
            [
                "<html><head><title>Raid History</title><style>",
                "body { font-family: sans-serif; margin: 32px; }",
                "table { border-collapse: collapse; width: 100%; }",
                "th, td { border: 1px solid #ddd; padding: 12px 10px; text-align: left; }",
                "th { background-color: #9146FF; color: white; }",
                "tr:nth-child(even) { background-color: #f2f2f2; }",
                "</style></head><body>",
                "<h1>Raid History</h1>",
                "<p><a href='/twitch/admin'>Zurueck zum Dashboard</a></p>",
                "<table><thead><tr>",
                "<th>Status</th><th>Zeitpunkt</th><th>Von</th><th>Nach</th>",
                "<th>Viewer</th><th>Stream-Dauer</th><th>Kandidaten</th><th>Fehler</th>",
                "</tr></thead><tbody>",
                rows_html,
                "</tbody></table></body></html>",
            ]
        )

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
            text=self._build_raid_auth_start_html(login, auth_url),
            content_type="text/html",
        )

    async def raid_auth_go(self, request: web.Request) -> web.StreamResponse:
        """Kurz-Redirect für Discord-Buttons → leitet zum vollen Twitch-OAuth-URL weiter.

        Kein Token erforderlich – der State ist das Geheimnis (10 Min TTL).
        Discord-Button-URLs sind auf 512 Zeichen limitiert; der volle OAuth-URL
        überschreitet dieses Limit.  Der Button verweist stattdessen auf diesen
        Endpoint, der den gespeicherten URL nachschlägt und weiterleitet.
        """
        state = (request.query.get("state") or "").strip()
        if not state:
            return web.Response(text="Missing state parameter", status=400)

        auth_manager = getattr(getattr(self, "_raid_bot", None), "auth_manager", None)
        if not auth_manager:
            return web.Response(text="Raid bot not initialized", status=503)

        full_url = auth_manager.get_pending_auth_url(state)
        if not full_url:
            return web.Response(
                text="<html><body>Link abgelaufen oder ungültig. "
                "Bitte erneut auf den Button in Discord klicken.</body></html>",
                content_type="text/html",
                status=410,
            )

        raise web.HTTPFound(location=full_url)

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

        discord_user_id = str(
            row["discord_user_id"] if hasattr(row, "keys") else row[0] or ""
        ).strip()
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
        rows_html = self._build_raid_history_rows(history)
        page_html = self._build_raid_history_page(rows_html)
        return web.Response(text=page_html, content_type="text/html")

    async def raid_analytics(self, request: web.Request) -> web.StreamResponse:
        """Raid analytics: sent/received balance, leechers, manual raids."""
        self._require_token(request)

        with storage.get_conn() as conn:
            # Active partners set
            partner_rows = conn.execute(
                "SELECT twitch_login FROM twitch_streamers_partner_state WHERE is_partner_active = 1"
            ).fetchall()
            partners: set = {r[0].lower() for r in partner_rows}

            # Sent stats
            sent_rows = conn.execute(
                """
                SELECT from_broadcaster_login, COUNT(*) as cnt, SUM(viewer_count) as viewers
                FROM twitch_raid_history WHERE success = 1
                GROUP BY from_broadcaster_login ORDER BY cnt DESC
                """
            ).fetchall()

            # Received stats
            recv_rows = conn.execute(
                """
                SELECT to_broadcaster_login, COUNT(*) as cnt, SUM(viewer_count) as viewers
                FROM twitch_raid_history WHERE success = 1
                GROUP BY to_broadcaster_login ORDER BY cnt DESC
                """
            ).fetchall()

            # Manual raids
            manual_rows = conn.execute(
                """
                SELECT from_broadcaster_login, to_broadcaster_login, viewer_count, executed_at
                FROM twitch_raid_history
                WHERE reason = 'manual_chat_command'
                ORDER BY executed_at DESC
                """
            ).fetchall()

            # Date range
            date_row = conn.execute(
                "SELECT MIN(executed_at), MAX(executed_at), COUNT(*) FROM twitch_raid_history WHERE success = 1"
            ).fetchone()

        sent_map: dict = {r[0].lower(): {"cnt": r[1], "viewers": r[2] or 0} for r in sent_rows}
        recv_map: dict = {r[0].lower(): {"cnt": r[1], "viewers": r[2] or 0} for r in recv_rows}

        all_logins = sorted(partners | sent_map.keys() | recv_map.keys())

        # Per-partner balance (only active partners for main table)
        partner_stats = []
        for login in sorted(partners):
            s = sent_map.get(login, {}).get("cnt", 0)
            r = recv_map.get(login, {}).get("cnt", 0)
            sv = sent_map.get(login, {}).get("viewers", 0)
            rv = recv_map.get(login, {}).get("viewers", 0)
            partner_stats.append(
                {
                    "login": login,
                    "sent": s,
                    "received": r,
                    "balance": s - r,
                    "viewers_sent": sv,
                    "viewers_recv": rv,
                }
            )
        partner_stats.sort(key=lambda x: x["balance"], reverse=True)

        leechers = [p for p in partner_stats if p["sent"] == 0 and p["received"] > 0]

        # External receivers of manual raids (non-partner targets)
        manual_list = []
        for row in manual_rows:
            raider = (row[0] or "").lower()
            target = (row[1] or "").lower()
            manual_list.append(
                {
                    "from": raider,
                    "to": target,
                    "viewers": row[2] or 0,
                    "at": str(row[3] or "")[:16],
                    "is_partner": target in partners,
                }
            )

        date_min = str(date_row[0] or "")[:10]
        date_max = str(date_row[1] or "")[:10]
        total = date_row[2] or 0

        page_html = self._build_raid_analytics_page(
            partner_stats=partner_stats,
            leechers=leechers,
            manual_list=manual_list,
            date_min=date_min,
            date_max=date_max,
            total=total,
        )
        return web.Response(text=page_html, content_type="text/html")

    @staticmethod
    def _build_raid_analytics_page(
        *,
        partner_stats: list,
        leechers: list,
        manual_list: list,
        date_min: str,
        date_max: str,
        total: int,
    ) -> str:
        import json as _json

        labels = _json.dumps([p["login"] for p in partner_stats])
        sent_data = _json.dumps([p["sent"] for p in partner_stats])
        recv_data = _json.dumps([p["received"] for p in partner_stats])

        # Balance table rows
        balance_rows = []
        for p in partner_stats:
            b = p["balance"]
            if b > 0:
                badge = f"<span class='badge badge-ok'>+{b}</span>"
            elif b < 0:
                badge = f"<span class='badge badge-err'>{b}</span>"
            else:
                badge = "<span class='badge badge-neutral'>0</span>"
            style = " class='leecher-row'" if p["sent"] == 0 and p["received"] > 0 else ""
            balance_rows.append(
                f"<tr{style}>"
                f"<td><strong>{html.escape(p['login'])}</strong></td>"
                f"<td>{p['sent']}</td>"
                f"<td>{p['received']}</td>"
                f"<td>{badge}</td>"
                f"<td>{p['viewers_sent']}</td>"
                f"<td>{p['viewers_recv']}</td>"
                f"</tr>"
            )
        balance_rows_html = "".join(balance_rows) or "<tr><td colspan='6'>Keine Daten</td></tr>"

        # Leecher list
        if leechers:
            leecher_items = "".join(
                f"<li><strong>{html.escape(l['login'])}</strong> — {l['received']} Raids empfangen, 0 gesendet</li>"
                for l in leechers
            )
            leecher_html = f"<div class='alert-card'><h2>Keine Raids zurückgegeben <span class='badge badge-err'>{len(leechers)}</span></h2><ul>{leecher_items}</ul></div>"
        else:
            leecher_html = "<div class='alert-card alert-ok'><h2>Alle aktiven Partner haben bereits geraided ✓</h2></div>"

        # Manual raids table
        if manual_list:
            manual_rows = []
            for m in manual_list:
                status_badge = (
                    '<span class="badge badge-ok">Partner</span>'
                    if m["is_partner"]
                    else '<span class="badge badge-warn">Extern</span>'
                )
                manual_rows.append(
                    f"<tr>"
                    f"<td><strong>{html.escape(m['from'])}</strong></td>"
                    f"<td><strong>{html.escape(m['to'])}</strong></td>"
                    f"<td>{status_badge}</td>"
                    f"<td>{m['viewers']}</td>"
                    f"<td>{html.escape(m['at'])}</td>"
                    f"</tr>"
                )
            manual_rows_html = "".join(manual_rows)
        else:
            manual_rows_html = "<tr><td colspan='5'>Keine manuellen Raids</td></tr>"

        return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Raid Analytics</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Fraunces:wght@500;600;700&family=Space+Grotesk:wght@400;500;600&display=swap');
  :root {{
    color-scheme: dark;
    --bg:#0b0a14; --bg-alt:#141226; --card:#1b1630; --bd:#2c2349; --text:#f2edff; --muted:#a394c7;
    --accent:#7c3aed; --accent-2:#f472b6; --accent-3:#d6ccff;
    --ok-bg:#0f2f24; --ok-bd:#1f9d7a; --ok-fg:#baf7dd;
    --err-bg:#3b0f1c; --err-bd:#b91c1c; --err-fg:#fecaca;
    --warn-bg:#2f210b; --warn-bd:#d97706; --warn-fg:#fde68a;
    --shadow:rgba(0,0,0,.45);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: "Space Grotesk", "Segoe UI", sans-serif;
    background: radial-gradient(900px 540px at 5% -10%, rgba(124,58,237,0.35), transparent 60%),
                radial-gradient(900px 540px at 95% 0%, rgba(244,114,182,0.22), transparent 55%),
                linear-gradient(180deg, #0b0a14 0%, #100c1f 55%, #0b0a14 100%);
    color: var(--text);
    padding: 2rem 1.8rem 3rem;
    min-height: 100vh;
  }}
  body::before {{
    content:""; position:fixed; inset:0;
    background: repeating-linear-gradient(135deg, rgba(255,255,255,0.04) 0 1px, transparent 1px 14px);
    opacity:0.2; pointer-events:none; z-index:0;
  }}
  body > * {{ position: relative; z-index: 1; }}
  h1 {{ font-family: "Fraunces", serif; font-size: 2rem; margin-bottom: .3rem; }}
  h2 {{ font-family: "Fraunces", serif; font-size: 1.15rem; margin-bottom: .8rem; color: var(--accent-3); }}
  .meta {{ color: var(--muted); font-size: .85rem; margin-bottom: 2rem; }}
  .nav {{ margin-bottom: 1.8rem; display: flex; gap: .8rem; flex-wrap: wrap; }}
  .nav a {{ color: var(--muted); text-decoration: none; padding: .4rem .8rem; border: 1px solid var(--bd); border-radius: 999px; font-size: .88rem; transition: border-color .15s; }}
  .nav a:hover {{ border-color: var(--accent); color: var(--text); }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.4rem; margin-bottom: 1.4rem; }}
  @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .card {{ background: var(--card); border: 1px solid var(--bd); border-radius: 1rem; padding: 1.4rem; box-shadow: 0 12px 30px var(--shadow); }}
  .card-full {{ grid-column: 1 / -1; }}
  .chart-wrap {{ position: relative; height: 340px; }}
  .chart-wrap-tall {{ position: relative; height: {max(280, len(partner_stats) * 38)}px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  th {{ color: var(--accent-3); text-transform: uppercase; letter-spacing: .07em; font-size: .75rem; padding: .55rem .5rem; border-bottom: 1px solid var(--bd); text-align: left; }}
  td {{ padding: .6rem .5rem; border-bottom: 1px solid rgba(44,35,73,.5); vertical-align: middle; }}
  tr:last-child td {{ border-bottom: none; }}
  tr.leecher-row td {{ background: rgba(185,28,28,.06); }}
  .badge {{ display:inline-flex; align-items:center; padding:.18rem .55rem; border-radius:999px; font-size:.78rem; font-weight:700; border:1px solid; }}
  .badge-ok {{ background:var(--ok-bg); color:var(--ok-fg); border-color:var(--ok-bd); }}
  .badge-err {{ background:var(--err-bg); color:var(--err-fg); border-color:var(--err-bd); }}
  .badge-warn {{ background:var(--warn-bg); color:var(--warn-fg); border-color:var(--warn-bd); }}
  .badge-neutral {{ background:rgba(124,58,237,.15); color:var(--accent-3); border-color:rgba(124,58,237,.35); }}
  .alert-card {{ background: var(--card); border: 1px solid var(--err-bd); border-radius: 1rem; padding: 1.4rem; margin-bottom: 1.4rem; }}
  .alert-card.alert-ok {{ border-color: var(--ok-bd); }}
  .alert-card ul {{ padding-left: 1.2rem; margin-top: .5rem; }}
  .alert-card li {{ margin-bottom: .35rem; color: var(--muted); font-size: .9rem; }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-bottom: 1.4rem; }}
  .stat {{ background: var(--card); border: 1px solid var(--bd); border-radius: .8rem; padding: 1rem 1.2rem; text-align: center; }}
  .stat .num {{ font-family: "Fraunces", serif; font-size: 2rem; color: var(--accent-3); }}
  .stat .lbl {{ font-size: .8rem; color: var(--muted); margin-top: .2rem; }}
</style>
</head>
<body>
<h1>Raid Analytics</h1>
<p class="meta">Zeitraum: {html.escape(date_min)} – {html.escape(date_max)}</p>

<nav class="nav">
  <a href="/twitch/admin">← Admin</a>
  <a href="/twitch/raid/history">Raid History</a>
</nav>

<div class="stat-grid">
  <div class="stat"><div class="num">{total}</div><div class="lbl">Raids gesamt</div></div>
  <div class="stat"><div class="num">{len(partner_stats)}</div><div class="lbl">Aktive Partner</div></div>
  <div class="stat"><div class="num">{len(leechers)}</div><div class="lbl">Nur Empfänger</div></div>
</div>

{leecher_html}

<div class="grid">
  <div class="card card-full">
    <h2>Raids gesendet vs. empfangen pro Partner</h2>
    <div class="chart-wrap-tall">
      <canvas id="barChart"></canvas>
    </div>
  </div>

  <div class="card card-full">
    <h2>Balance-Tabelle (Partner)</h2>
    <table>
      <thead><tr>
        <th>Streamer</th><th>Gesendet</th><th>Empfangen</th><th>Balance</th><th>Viewer gesendet</th><th>Viewer empfangen</th>
      </tr></thead>
      <tbody>{balance_rows_html}</tbody>
    </table>
  </div>

  <div class="card card-full">
    <h2>Manuelle Raids <span class="badge badge-neutral">{len(manual_list)}</span></h2>
    <table>
      <thead><tr>
        <th>Von</th><th>Nach</th><th>Typ</th><th>Viewer</th><th>Zeitpunkt</th>
      </tr></thead>
      <tbody>{manual_rows_html}</tbody>
    </table>
  </div>
</div>

<script>
const labels = {labels};
const sentData = {sent_data};
const recvData = {recv_data};

const ctx = document.getElementById('barChart').getContext('2d');
new Chart(ctx, {{
  type: 'bar',
  data: {{
    labels: labels,
    datasets: [
      {{
        label: 'Gesendet',
        data: sentData,
        backgroundColor: 'rgba(124,58,237,0.75)',
        borderColor: 'rgba(124,58,237,1)',
        borderWidth: 1,
        borderRadius: 4,
      }},
      {{
        label: 'Empfangen',
        data: recvData,
        backgroundColor: 'rgba(244,114,182,0.6)',
        borderColor: 'rgba(244,114,182,1)',
        borderWidth: 1,
        borderRadius: 4,
      }}
    ]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ labels: {{ color: '#f2edff', font: {{ family: 'Space Grotesk' }} }} }},
      tooltip: {{
        backgroundColor: '#1b1630',
        borderColor: '#2c2349',
        borderWidth: 1,
        titleColor: '#d6ccff',
        bodyColor: '#a394c7',
      }}
    }},
    scales: {{
      x: {{
        grid: {{ color: 'rgba(44,35,73,0.6)' }},
        ticks: {{ color: '#a394c7', stepSize: 1 }},
        beginAtZero: true,
      }},
      y: {{
        grid: {{ display: false }},
        ticks: {{ color: '#f2edff', font: {{ size: 12 }} }},
      }}
    }}
  }}
}});
</script>
</body>
</html>"""

    async def stats_entry(self, request: web.Request) -> web.StreamResponse:
        """Canonical public entrypoint that links old + beta analytics dashboards."""
        if not self._check_v2_auth(request):
            login_url = (
                TWITCH_DASHBOARDS_DISCORD_LOGIN_URL
                if self._should_use_discord_admin_login(request)
                else TWITCH_DASHBOARDS_LOGIN_URL
            )
            raise web.HTTPFound(login_url)

        legacy_url = self._resolve_legacy_stats_url()
        beta_url = "/twitch/dashboard-v2"
        logout_url = (
            "/twitch/auth/discord/logout"
            if self._is_discord_admin_request(request)
            else "/twitch/auth/logout"
        )

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
            "<div class='card'><h2 style='margin-top:0;'>📱 Social Media Publisher</h2>"
            "<p class='muted'>Verwalte Twitch-Clips und veröffentliche auf TikTok, YouTube & Instagram</p>"
            "<a class='btn' href='/social-media'>Social Media Dashboard öffnen</a></div>"
            "</div></div></body></html>"
        )
        return web.Response(text=html, content_type="text/html")

    async def auth_login(self, request: web.Request) -> web.StreamResponse:
        """Kick off Twitch OAuth login for dashboard access."""
        next_path = self._normalize_next_path(request.query.get("next"))

        if self._check_v2_auth(request):
            destination = self._canonical_post_login_destination(next_path)
            raise web.HTTPFound(destination)

        if not self._check_rate_limit(request, max_requests=10, window_seconds=60.0):
            return web.Response(text="Zu viele Anfragen. Bitte warte kurz.", status=429)

        if not self._is_oauth_configured():
            return web.Response(
                text="Twitch OAuth ist aktuell nicht konfiguriert.",
                status=503,
            )

        self._cleanup_auth_state()
        redirect_uri = self._build_oauth_redirect_uri()
        if not redirect_uri:
            return web.Response(
                text=(
                    "Twitch OAuth Redirect-URI ist nicht konfiguriert oder ungültig. "
                    "Bitte eine gültige /twitch/auth/callback URL konfigurieren."
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
        if not self._check_rate_limit(request, max_requests=10, window_seconds=60.0):
            return web.Response(text="Zu viele Anfragen. Bitte warte kurz.", status=429)

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
            return web.Response(
                text="OAuth-Austausch fehlgeschlagen. Bitte erneut versuchen.",
                status=401,
            )

        partner = self._is_partner_allowed(
            twitch_login=user.get("twitch_login") or "",
            twitch_user_id=user.get("twitch_user_id") or "",
        )
        if not partner:
            log.warning(
                "AUDIT dashboard login denied: twitch=%s peer=%s",
                self._sanitize_log_value(user.get("twitch_login")),
                self._sanitize_log_value(self._peer_host(request)),
            )
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
        log.info(
            "AUDIT dashboard login success: twitch=%s peer=%s",
            self._sanitize_log_value(partner.get("twitch_login")),
            self._sanitize_log_value(self._peer_host(request)),
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
                f"<p><code>{html.escape(expected_uri, quote=True)}</code></p>"
                if expected_uri
                else ""
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
        state_discord_user_id: str | None = None
        if login.lower().startswith("discord:"):
            candidate_discord_id = login.split(":", 1)[1].strip()
            if candidate_discord_id.isdigit():
                state_discord_user_id = candidate_discord_id

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
                    raise RuntimeError(
                        f"Failed to fetch Twitch user info ({user_resp.status}): {body[:300]}"
                    )
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
                    post_setup(
                        twitch_user_id,
                        twitch_login,
                        state_discord_user_id=state_discord_user_id,
                    ),
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
            session = self._auth_sessions.pop(session_id, None)
            twitch_login = (session or {}).get("twitch_login", "unknown") if session else "unknown"
            log.info(
                "AUDIT dashboard logout: twitch=%s peer=%s",
                self._sanitize_log_value(twitch_login),
                self._sanitize_log_value(self._peer_host(request)),
            )

        response = web.HTTPFound(TWITCH_DASHBOARD_V2_LOGIN_URL)
        self._clear_session_cookie(response)
        raise response

    async def discord_link(self, request: web.Request) -> web.StreamResponse:
        """Persist Discord profile metadata from the stats dashboard."""
        self._require_token(request)
        if not callable(self._discord_profile):
            location = self._redirect_location(
                request, err="Discord-Link ist aktuell nicht verfügbar"
            )
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
            location = self._redirect_location(
                request, err="Discord-Daten konnten nicht gespeichert werden"
            )
        safe_location = self._safe_internal_redirect(location, fallback="/twitch/stats")
        raise web.HTTPFound(location=safe_location)

    async def market_research(self, request: web.Request) -> web.StreamResponse:
        """Serve the internal Market Research dashboard."""
        self._require_token(request)

        html = """
        <!DOCTYPE html>
        <html lang="de">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Deadlock Market Research (Internal)</title>
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <style>
                body { font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 20px; }
                .container { max-width: 1400px; margin: 0 auto; }
                h1 { color: #f8fafc; border-bottom: 1px solid #334155; padding-bottom: 10px; }
                .card { background: #1e293b; border-radius: 8px; padding: 20px; margin-bottom: 20px; border: 1px solid #334155; }
                .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
                table { width: 100%; border-collapse: collapse; margin-top: 10px; }
                th, td { text-align: left; padding: 12px; border-bottom: 1px solid #334155; }
                th { color: #94a3b8; font-weight: 600; text-transform: uppercase; font-size: 0.85rem; }
                tr:hover { background: #334155; }
                .badge { padding: 4px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }
                .badge-live { background: #ef4444; color: white; }
                .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 20px; }
                .stat-box { background: #0f172a; padding: 15px; border-radius: 6px; text-align: center; border: 1px solid #334155; }
                .stat-val { font-size: 2rem; font-weight: bold; color: #38bdf8; }
                .stat-label { color: #94a3b8; font-size: 0.9rem; }
                .progress-bar { background: #334155; height: 8px; border-radius: 4px; overflow: hidden; margin-top: 5px; }
                .progress-fill { height: 100%; background: #38bdf8; }
                .sentiment-pos { color: #4ade80; }
                .sentiment-neg { color: #f87171; }
                .question-item { border-left: 4px solid #38bdf8; padding: 10px; margin-bottom: 10px; background: #0f172a; border-radius: 0 4px 4px 0; }
                .question-meta { font-size: 0.8rem; color: #94a3b8; margin-top: 5px; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Deadlock DACH Market Research 🕵️‍♂️</h1>
                
                <div class="stat-grid" id="kpi">
                    <!-- Loaded via JS -->
                </div>

                <div class="card">
                    <h2>📈 Market Volume (24h)</h2>
                    <div style="height: 300px; position: relative;">
                        <canvas id="marketChart"></canvas>
                    </div>
                </div>

                <div class="grid-2">
                    <div class="card">
                        <h2>🔥 Meta Snapshot (Top Mentions 1h)</h2>
                        <table id="meta-table">
                            <thead><tr><th>Term</th><th>Mentions</th><th>Trend</th></tr></thead>
                            <tbody></tbody>
                        </table>
                    </div>
                    <div class="card">
                        <h2>🌡️ Sentiment Analysis</h2>
                        <div id="sentiment-chart" style="padding: 20px; text-align: center;"></div>
                    </div>
                </div>

                <div class="grid-2">
                    <div class="card">
                        <h2>🕸️ Viewer Overlap (Shared Chatters)</h2>
                        <table id="overlap-table">
                            <thead><tr><th>Streamer A</th><th>Streamer B</th><th>Shared Users</th></tr></thead>
                            <tbody></tbody>
                        </table>
                    </div>
                    <div class="card">
                        <h2>❓ Question Radar (Latest)</h2>
                        <div id="questions" style="max-height: 400px; overflow-y: auto; padding-right: 10px;">
                            <!-- Questions go here -->
                        </div>
                    </div>
                </div>

                <div class="card">
                    <h2>Live Monitored Channels</h2>
                    <table id="channels">
                        <thead>
                            <tr>
                                <th>Streamer</th>
                                <th>Viewers</th>
                                <th>Chat Activity</th>
                                <th>Lurker %</th>
                                <th>Msg/Min</th>
                                <th>Top Topic</th>
                            </tr>
                        </thead>
                        <tbody></tbody>
                    </table>
                </div>
            </div>

            <script>
                let marketChart = null;

                async function loadData() {
                    const res = await fetch('/twitch/api/market_data');
                    const data = await res.json();
                    
                    // KPIs
                    document.getElementById('kpi').innerHTML = `
                        <div class="stat-box">
                            <div class="stat-val">${data.total_monitored}</div>
                            <div class="stat-label">Active Monitored Channels</div>
                        </div>
                        <div class="stat-box">
                            <div class="stat-val">${data.total_viewers.toLocaleString()}</div>
                            <div class="stat-label">Total Deadlock Viewers (DACH)</div>
                        </div>
                        <div class="stat-box">
                            <div class="stat-val">${data.avg_chat_health.toFixed(1)}%</div>
                            <div class="stat-label">Avg Chat Engagement</div>
                        </div>
                        <div class="stat-box">
                            <div class="stat-val">${data.total_messages.toLocaleString()}</div>
                            <div class="stat-label">Messages Analyzed (1h)</div>
                        </div>
                        <div class="stat-box">
                            <div class="stat-val">${data.avg_lurker_ratio.toFixed(1)}%</div>
                            <div class="stat-label">Avg Lurker Ratio</div>
                        </div>
                    `;

                    // Market Chart
                    const ctx = document.getElementById('marketChart').getContext('2d');
                    const chartLabels = data.market_history.map(h => {
                        const d = new Date(h.ts + 'Z');
                        return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
                    });
                    
                    const chartData = {
                        labels: chartLabels,
                        datasets: [
                            {
                                label: 'Total Viewers',
                                data: data.market_history.map(h => h.total_viewers),
                                borderColor: '#38bdf8',
                                backgroundColor: 'rgba(56, 189, 248, 0.1)',
                                fill: true,
                                tension: 0.4
                            },
                            {
                                label: 'Streamer Count',
                                data: data.market_history.map(h => h.streamer_count * 10), // Scale for visibility
                                borderColor: '#f472b6',
                                borderDash: [5, 5],
                                tension: 0.1,
                                yAxisID: 'y1'
                            }
                        ]
                    };

                    if (marketChart) {
                        marketChart.data = chartData;
                        marketChart.update();
                    } else {
                        marketChart = new Chart(ctx, {
                            type: 'line',
                            data: chartData,
                            options: {
                                responsive: true,
                                maintainAspectRatio: false,
                                scales: {
                                    y: { beginAtZero: true, grid: { color: '#334155' } },
                                    y1: { position: 'right', beginAtZero: true, grid: { display: false } },
                                    x: { grid: { display: false } }
                                },
                                plugins: { legend: { labels: { color: '#e2e8f0' } } }
                            }
                        });
                    }

                    // Questions
                    document.getElementById('questions').innerHTML = data.questions.map(q => `
                        <div class="question-item">
                            <div>${q.content}</div>
                            <div class="question-meta">in @${q.streamer} • ${q.ts.split('T')[1].substring(0, 5)} Uhr</div>
                        </div>
                    `).join('');

                    // Meta Snapshot
                    document.getElementById('meta-table').querySelector('tbody').innerHTML = data.meta_snapshot.map(m => `
                        <tr>
                            <td><strong>${m.term}</strong></td>
                            <td>${m.count}</td>
                            <td><div class="progress-bar"><div class="progress-fill" style="width: ${Math.min(100, m.count * 2)}%"></div></div></td>
                        </tr>
                    `).join('');

                    // Sentiment
                    const sent = data.sentiment;
                    document.getElementById('sentiment-chart').innerHTML = `
                        <div style="display: flex; justify-content: space-around; font-size: 1.2rem;">
                            <div class="sentiment-pos">Positiv: ${sent.positive} (${sent.pos_pct}%)</div>
                            <div style="color: #94a3b8;">Neutral: ${sent.neutral} (${sent.neu_pct}%)</div>
                            <div class="sentiment-neg">Negativ: ${sent.negative} (${sent.neg_pct}%)</div>
                        </div>
                        <div style="display: flex; height: 20px; margin-top: 15px; border-radius: 10px; overflow: hidden;">
                            <div style="width: ${sent.pos_pct}%; background: #4ade80;"></div>
                            <div style="width: ${sent.neu_pct}%; background: #94a3b8;"></div>
                            <div style="width: ${sent.neg_pct}%; background: #f87171;"></div>
                        </div>
                    `;

                    // Overlap
                    document.getElementById('overlap-table').querySelector('tbody').innerHTML = data.overlap.map(o => `
                        <tr>
                            <td>${o.a}</td>
                            <td>${o.b}</td>
                            <td>${o.shared}</td>
                        </tr>
                    `).join('');

                    // Channels Table
                    const tbody = document.querySelector('#channels tbody');
                    tbody.innerHTML = data.channels.map(c => `
                        <tr>
                            <td>
                                <strong>${c.login}</strong>
                                ${c.is_live ? '<span class="badge badge-live">LIVE</span>' : ''}
                            </td>
                            <td>${c.viewers}</td>
                            <td>${c.chat_health.toFixed(1)}%</td>
                            <td>${c.lurker_ratio.toFixed(1)}%</td>
                            <td>${c.msg_per_min.toFixed(1)}</td>
                            <td>${c.top_topic || '-'}</td>
                        </tr>
                    `).join('');
                }
                loadData();
                setInterval(loadData, 30000);
            </script>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")

    async def api_market_data(self, request: web.Request) -> web.Response:
        """API providing aggregated data for market research including Meta & Sentiment."""
        # Simple auth check (internal/admin only)
        if not self._check_admin_token(
            request.headers.get("X-Admin-Token") or request.query.get("token")
        ) and not self._is_local_request(request):
            return web.json_response({"error": "Unauthorized"}, status=401)

        try:
            with storage.get_conn() as conn:
                # 1. Active Monitored Channels
                rows = conn.execute("""
                    SELECT s.twitch_login, l.last_viewer_count
                    FROM twitch_streamers s
                    LEFT JOIN twitch_live_state l ON s.twitch_user_id = l.twitch_user_id
                    WHERE s.is_monitored_only = 1
                """).fetchall()

                channels = []
                total_viewers = 0

                for r in rows:
                    login = r[0]
                    viewers = r[1] or 0
                    total_viewers += viewers

                    # Recent chat stats
                    chat_stats = conn.execute(
                        """
                        SELECT COUNT(*), COUNT(DISTINCT chatter_login)
                        FROM twitch_chat_messages
                        WHERE streamer_login = ? 
                          AND message_ts >= datetime('now', '-1 hour')
                    """,
                        [login],
                    ).fetchone()

                    msgs = chat_stats[0] or 0
                    active_chatters = chat_stats[1] or 0

                    # Lurker stats
                    session_id_row = conn.execute(
                        "SELECT active_session_id FROM twitch_live_state WHERE streamer_login = ?",
                        (login,),
                    ).fetchone()

                    lurkers = 0
                    total_connected = active_chatters
                    if session_id_row and session_id_row[0]:
                        lurker_stats = conn.execute(
                            """
                            SELECT COUNT(*), SUM(CASE WHEN messages = 0 THEN 1 ELSE 0 END)
                            FROM twitch_session_chatters WHERE session_id = ?
                        """,
                            (session_id_row[0],),
                        ).fetchone()
                        if lurker_stats:
                            total_connected = lurker_stats[0] or active_chatters
                            lurkers = lurker_stats[1] or 0

                    channels.append(
                        {
                            "login": login,
                            "viewers": viewers,
                            "is_live": viewers > 0,
                            "chat_health": min(100, (active_chatters / max(1, viewers)) * 100)
                            if viewers > 0
                            else 0,
                            "lurker_ratio": (lurkers / max(1, total_connected)) * 100,
                            "msg_per_min": msgs / 60.0,
                            "top_topic": "n/a",
                        }
                    )

                channels.sort(key=lambda x: x["viewers"], reverse=True)
                avg_health = sum(c["chat_health"] for c in channels) / max(1, len(channels))
                avg_lurker = sum(c["lurker_ratio"] for c in channels) / max(1, len(channels))

                # --- 2. Market History (24h) ---
                history_rows = conn.execute("""
                    SELECT ts_utc, SUM(viewer_count) as total_viewers, COUNT(DISTINCT streamer) as streamer_count
                    FROM twitch_stats_category
                    WHERE ts_utc >= datetime('now', '-24 hours')
                    GROUP BY ts_utc
                    ORDER BY ts_utc ASC
                """).fetchall()
                market_history = [
                    {"ts": r[0], "total_viewers": r[1], "streamer_count": r[2]}
                    for r in history_rows
                ]

                # --- 3. Question Radar ---
                question_rows = conn.execute("""
                    SELECT content, streamer_login, message_ts
                    FROM twitch_chat_messages
                    WHERE message_ts >= datetime('now', '-6 hours')
                      AND content LIKE '%?%'
                      AND length(content) > 10
                    ORDER BY message_ts DESC
                    LIMIT 20
                """).fetchall()
                questions = [{"content": r[0], "streamer": r[1], "ts": r[2]} for r in question_rows]

                # --- 4. Meta Snapshot & Sentiment (1h) ---
                deadlock_terms = [
                    "abrams",
                    "bebop",
                    "dynamo",
                    "grey talon",
                    "haze",
                    "infernus",
                    "ivy",
                    "kelvin",
                    "lady geist",
                    "mcginnis",
                    "mo & krill",
                    "paradox",
                    "pocket",
                    "seven",
                    "vindicta",
                    "viscous",
                    "warden",
                    "wraith",
                    "yamato",
                    "lash",
                    "shiv",
                    "urn",
                    "midboss",
                    "soul",
                    "flex slot",
                    "build",
                    "op",
                    "nerf",
                    "buff",
                    "patch",
                ]
                recent_msgs = conn.execute(
                    "SELECT content FROM twitch_chat_messages WHERE message_ts >= datetime('now', '-1 hour')"
                ).fetchall()

                term_counts = {t: 0 for t in deadlock_terms}
                sentiment = {"positive": 0, "negative": 0, "neutral": 0}
                pos_words = {
                    "pog",
                    "gg",
                    "nice",
                    "cool",
                    "krass",
                    "lol",
                    "win",
                    "stark",
                }
                neg_words = {
                    "rip",
                    "bad",
                    "lose",
                    "troll",
                    "cringe",
                    "throw",
                    "sucks",
                    "lag",
                }

                for row in recent_msgs:
                    content = (row[0] or "").lower()
                    for t in deadlock_terms:
                        if t in content:
                            term_counts[t] += 1
                    is_pos = any(w in content for w in pos_words)
                    is_neg = any(w in content for w in neg_words)
                    if is_pos and not is_neg:
                        sentiment["positive"] += 1
                    elif is_neg and not is_pos:
                        sentiment["negative"] += 1
                    else:
                        sentiment["neutral"] += 1

                meta_snapshot = sorted(
                    [{"term": k, "count": v} for k, v in term_counts.items() if v > 0],
                    key=lambda x: x["count"],
                    reverse=True,
                )[:10]
                total_sent = sum(sentiment.values()) or 1
                sent_data = {
                    "positive": sentiment["positive"],
                    "negative": sentiment["negative"],
                    "neutral": sentiment["neutral"],
                    "pos_pct": round(sentiment["positive"] / total_sent * 100, 1),
                    "neg_pct": round(sentiment["negative"] / total_sent * 100, 1),
                    "neu_pct": round(sentiment["neutral"] / total_sent * 100, 1),
                }

                # --- 5. Overlap (Top 5 Pairs) ---
                top_logins = [c["login"] for c in channels[:5]]
                overlap = []
                if len(top_logins) >= 2:
                    login_slots = (top_logins + ["!unused!"] * 5)[:5]
                    rows_overlap = conn.execute(
                        """
                        SELECT c1.streamer_login, c2.streamer_login, COUNT(DISTINCT c1.chatter_login)
                        FROM twitch_chat_messages c1
                        JOIN twitch_chat_messages c2 ON c1.chatter_login = c2.chatter_login AND c1.streamer_login < c2.streamer_login
                        WHERE c1.message_ts >= datetime('now', '-6 hours') AND c2.message_ts >= datetime('now', '-6 hours')
                          AND c1.streamer_login IN (?, ?, ?, ?, ?)
                          AND c2.streamer_login IN (?, ?, ?, ?, ?)
                        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 5
                    """,
                        login_slots + login_slots,
                    ).fetchall()
                    overlap = [{"a": ro[0], "b": ro[1], "shared": ro[2]} for ro in rows_overlap]

                return web.json_response(
                    {
                        "total_monitored": len(channels),
                        "total_viewers": total_viewers,
                        "avg_chat_health": avg_health,
                        "avg_lurker_ratio": avg_lurker,
                        "total_messages": len(recent_msgs),
                        "market_history": market_history,
                        "questions": questions,
                        "channels": channels,
                        "meta_snapshot": meta_snapshot,
                        "sentiment": sent_data,
                        "overlap": overlap,
                    }
                )
        except Exception as e:
            log.exception("Market API Error")
            return web.json_response({"error": str(e)}, status=500)

    async def reload_cog(self, request: web.Request) -> web.Response:
        """Optional reload endpoint for admin tooling compatibility."""
        token = (await request.post()).get("token", "")
        if not self._check_admin_token(token):
            log.warning(
                "AUDIT dashboard reload_cog: unauthorized attempt from peer=%s",
                self._sanitize_log_value(self._peer_host(request)),
            )
            return web.Response(text="Unauthorized", status=401)

        log.info(
            "AUDIT dashboard reload_cog: triggered by peer=%s",
            self._sanitize_log_value(self._peer_host(request)),
        )
        if self._reload_cb:
            msg = await self._reload_cb()
            return web.Response(text=msg)
        return web.Response(text="Kein Reload-Handler definiert", status=501)

    def _register_social_media_routes(self, app: web.Application) -> None:
        """Register Social Media Clip Publisher routes."""
        try:
            from ..social_media import ClipManager, create_social_media_app

            # Create clip manager (no Twitch API dependency yet)
            clip_manager = ClipManager()

            # Create social media dashboard with auth checker
            social_app = create_social_media_app(
                clip_manager=clip_manager,
                auth_checker=self._check_v2_auth,
                auth_session_getter=self._get_dashboard_auth_session,
            )

            # Mount social media routes
            for route in social_app.router.routes():
                app.router.add_route(
                    route.method,
                    route.resource.canonical,
                    route.handler,
                )

            log.info("Social Media Dashboard routes registered successfully")
        except Exception:
            log.exception("Failed to register Social Media Dashboard routes")

    def attach(self, app: web.Application) -> None:
        app.add_routes(
            [
                web.get("/", self.public_home),
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
                web.get("/twitch/raid/go", self.raid_auth_go),
                web.get("/twitch/raid/requirements", self.raid_requirements),
                web.get("/twitch/raid/history", self.raid_history),
                web.get("/twitch/raid/analytics", self.raid_analytics),
                web.get("/twitch/auth/login", self.auth_login),
                web.get("/twitch/auth/callback", self.auth_callback),
                web.get("/twitch/auth/logout", self.auth_logout),
                web.get("/twitch/auth/discord/login", self.discord_auth_login),
                web.get("/twitch/auth/discord/callback", self.discord_auth_callback),
                web.get("/twitch/auth/discord/logout", self.discord_auth_logout),
                web.get("/twitch/raid/callback", self.raid_oauth_callback),
                web.post("/twitch/discord_link", self.discord_link),
                web.post("/twitch/reload", self.reload_cog),
                web.get("/twitch/market", self.market_research),
                web.get("/twitch/api/market_data", self.api_market_data),
            ]
        )
        self._register_v2_routes(app.router)
        self._register_social_media_routes(app)


@web.middleware
async def _security_headers_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Attach minimal security headers to every response."""
    response = await handler(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    return response


def build_v2_app(
    *,
    noauth: bool,
    token: str | None,
    partner_token: str | None = None,
    oauth_client_id: str | None = None,
    oauth_client_secret: str | None = None,
    oauth_redirect_uri: str | None = None,
    session_ttl_seconds: int = 12 * 3600,
    legacy_stats_url: str | None = None,
    add_cb: Callable[[str, bool], Awaitable[str]] | None = None,
    remove_cb: Callable[[str], Awaitable[str]] | None = None,
    list_cb: Callable[[], Awaitable[list[dict]]] | None = None,
    stats_cb: Callable[..., Awaitable[dict]] | None = None,
    verify_cb: Callable[[str, str], Awaitable[str]] | None = None,
    archive_cb: Callable[[str, str], Awaitable[str]] | None = None,
    discord_flag_cb: Callable[[str, bool], Awaitable[str]] | None = None,
    discord_profile_cb: Callable[[str, str | None, str | None, bool], Awaitable[str]] | None = None,
    raid_history_cb: Callable[..., Awaitable[list[dict]]] | None = None,
    raid_bot: Any | None = None,
    reload_cb: Callable[[], Awaitable[str]] | None = None,
    eventsub_webhook_handler: Any | None = None,
) -> web.Application:
    app = web.Application(middlewares=[_security_headers_middleware])
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
    if eventsub_webhook_handler is not None:
        app.router.add_post(
            "/twitch/eventsub/callback",
            eventsub_webhook_handler.handle_request,
        )
    return app


__all__ = ["DashboardV2Server", "build_v2_app"]
