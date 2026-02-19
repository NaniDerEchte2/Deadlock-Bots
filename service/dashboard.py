from __future__ import annotations

import asyncio
import datetime as _dt
import errno
import html
import ipaddress
import json
import math
import logging
import os
import secrets
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from aiohttp import ClientSession, ClientTimeout, web

from service import db

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_EXCLUDED_ROLE_IDS = {
    1304416311383818240,
    1309741866098491479,
}

LOG_TAIL_DEFAULT_LINES = 400
LOG_TAIL_MAX_LINES = 5000
LOG_TAIL_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_DASHBOARD_MODERATOR_ROLE_ID = 1337518124647579661
DEFAULT_DASHBOARD_OWNER_USER_ID = 662995601738170389
KEYRING_SERVICE_NAME = "DeadlockBot"
MASTER_DASHBOARD_PUBLIC_URL = (
    os.getenv("MASTER_DASHBOARD_PUBLIC_URL") or "https://admin.earlysalty.com"
).strip()
MASTER_DASHBOARD_DISCORD_REDIRECT_URI = (
    os.getenv("MASTER_DASHBOARD_DISCORD_REDIRECT_URI")
    or f"{MASTER_DASHBOARD_PUBLIC_URL.rstrip('/')}/auth/discord/callback"
).strip()
MASTER_DASHBOARD_DEFAULT_SCHEME = "http"
DISCORD_API_BASE_URL = "https://discord.com/api/v10"
DEFAULT_NSSM_SERVICE_NAME = KEYRING_SERVICE_NAME
DEFAULT_NSSM_RESTART_DELAY_SECONDS = 1.0
DEFAULT_BOT_RESTART_MIN_INTERVAL_SECONDS = 15.0
NSSM_PATH_CANDIDATES = (
    r"C:\ProgramData\chocolatey\bin\nssm.exe",
    r"C:\nssm\win64\nssm.exe",
    r"C:\nssm\nssm.exe",
)
POWERSHELL_PATH_CANDIDATES = (
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
)

try:
    from service.standalone_manager import (
        StandaloneAlreadyRunning,
        StandaloneConfigNotFound,
        StandaloneManagerError,
        StandaloneNotRunning,
    )
except Exception:
    StandaloneAlreadyRunning = StandaloneConfigNotFound = StandaloneManagerError = StandaloneNotRunning = None  # type: ignore


_DASHBOARD_HTML_PATH = Path(__file__).resolve().parent / "static" / "dashboard.html"


def _load_index_html() -> str:
    """
    Lädt das Dashboard-HTML aus service/static/dashboard.html.
    Kein Fallback, kein Caching: Fehlender/defekter File => 500.
    """
    try:
        return _DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    except Exception as exc:
        logging.getLogger(__name__).error(
            "Dashboard HTML konnte nicht geladen werden (%s): %s",
            _DASHBOARD_HTML_PATH,
            exc,
            exc_info=True,
        )
        raise


class DashboardServer:
    """Simple aiohttp based dashboard for managing the master bot."""

    def __init__(
        self,
        bot: "MasterBot",
        *,
        host: str = "127.0.0.1",
        port: int = 8766,
        token: Optional[str] = None,
    ) -> None:
        self.bot = bot
        self.host = host
        self.port = port
        self.token = (token or "").strip() or None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._lock = asyncio.Lock()
        self._started = False
        self._restart_lock = asyncio.Lock()
        self._restart_task: Optional[asyncio.Task] = None
        self._last_restart: Dict[str, Any] = {"at": None, "ok": None, "error": None}
        self._lifecycle = getattr(bot, "lifecycle", None)
        self._nssm_service_name = (
            os.getenv("MASTER_NSSM_SERVICE_NAME") or DEFAULT_NSSM_SERVICE_NAME
        ).strip()
        self._nssm_executable = (os.getenv("MASTER_NSSM_EXE") or "").strip()
        self._powershell_executable = (os.getenv("MASTER_POWERSHELL_EXE") or "").strip()
        nssm_enabled_raw = (os.getenv("MASTER_NSSM_RESTART_ENABLED") or "1").strip().lower()
        self._nssm_service_restart_enabled = nssm_enabled_raw not in {"0", "false", "no", "off"}
        allow_query_token_raw = (os.getenv("MASTER_DASHBOARD_ALLOW_QUERY_TOKEN") or "0").strip().lower()
        self._allow_query_token = allow_query_token_raw in {"1", "true", "yes", "on"}
        self._nssm_restart_delay_seconds = self._parse_positive_float(
            os.getenv(
                "MASTER_NSSM_RESTART_DELAY_SECONDS",
                str(DEFAULT_NSSM_RESTART_DELAY_SECONDS),
            ),
            default=DEFAULT_NSSM_RESTART_DELAY_SECONDS,
            env_name="MASTER_NSSM_RESTART_DELAY_SECONDS",
        )
        self._bot_restart_min_interval_seconds = self._parse_positive_float(
            os.getenv(
                "MASTER_BOT_RESTART_MIN_INTERVAL_SECONDS",
                str(DEFAULT_BOT_RESTART_MIN_INTERVAL_SECONDS),
            ),
            default=DEFAULT_BOT_RESTART_MIN_INTERVAL_SECONDS,
            env_name="MASTER_BOT_RESTART_MIN_INTERVAL_SECONDS",
        )
        self._bot_restart_lock = asyncio.Lock()
        self._last_bot_restart_request_monotonic = 0.0
        keyring_client_id = self._read_keyring_secret("DISCORD_OAUTH_CLIENT_ID")
        app_client_id = str(getattr(bot, "application_id", "") or "").strip()
        self._discord_client_id = (keyring_client_id or app_client_id).strip()
        self._discord_client_secret = self._read_keyring_secret("DISCORD_OAUTH_CLIENT_SECRET").strip()
        self._discord_redirect_uri = MASTER_DASHBOARD_DISCORD_REDIRECT_URI
        self._discord_auth_enabled = True
        self._discord_owner_user_id = DEFAULT_DASHBOARD_OWNER_USER_ID
        self._discord_moderator_role_id = DEFAULT_DASHBOARD_MODERATOR_ROLE_ID
        self._discord_auth_guild_ids: Tuple[int, ...] = ()
        self._discord_session_cookie = "master_dash_session"
        self._discord_sessions: Dict[str, Dict[str, Any]] = {}
        self._discord_oauth_states: Dict[str, Dict[str, Any]] = {}
        self._discord_oauth_state_ttl = 600
        self._discord_session_ttl = 12 * 3600
        self._discord_auth_required = self._discord_auth_enabled and self._is_discord_oauth_configured()
        self._auth_misconfigured = False
        self._scheme = MASTER_DASHBOARD_DEFAULT_SCHEME
        self._listen_base_url = self._format_base_url(self.host, self.port, self._scheme)
        try:
            self._public_base_url = self._normalize_public_url(
                MASTER_DASHBOARD_PUBLIC_URL,
                default_scheme="https",
            )
        except Exception as exc:
            logging.warning(
                "Master dashboard public URL '%s' invalid (%s) - falling back to listen URL",
                MASTER_DASHBOARD_PUBLIC_URL,
                exc,
            )
            self._public_base_url = self._listen_base_url
        self._allowed_request_origins = self._build_allowed_request_origins()

        self._twitch_dashboard_href = self._resolve_twitch_dashboard_href()
        self._steam_return_url = self._derive_steam_return_url()
        self._raid_health_url = self._derive_raid_health_url()
        self._health_cache: List[Dict[str, Any]] = []
        self._health_cache_expiry = 0.0
        self._health_cache_lock = asyncio.Lock()
        self._health_cache_ttl = self._parse_positive_float(
            "30.0",
            default=30.0,
            env_name="DASHBOARD_HEALTHCHECK_CACHE_SECONDS",
        )
        self._health_timeout = self._parse_positive_float(
            "6.0",
            default=6.0,
            env_name="DASHBOARD_HEALTHCHECK_TIMEOUT_SECONDS",
        )
        self._health_targets = self._build_health_targets()
        self._log_dir = Path(__file__).resolve().parent.parent / "logs"
        if self._discord_auth_enabled and not self._is_discord_oauth_configured():
            if self.token:
                logging.warning(
                    "Dashboard Discord OAuth ist unvollständig (Client ID/Secret fehlt). "
                    "Fallback auf Token-only Auth."
                )
                self._discord_auth_required = False
            else:
                logging.error(
                    "Dashboard Auth-Konfiguration ungültig: Discord OAuth aktiviert, aber Client ID/Secret "
                    "nicht im Windows-Tresor (%s) vorhanden. Dashboard bleibt gesperrt.",
                    KEYRING_SERVICE_NAME,
                )
                self._discord_auth_required = False
                self._auth_misconfigured = True
        if not self._discord_auth_required and not self.token and not self._auth_misconfigured:
            logging.warning(
                "Dashboard läuft ohne Auth (kein Discord OAuth und kein Dashboard-Token gesetzt)."
            )

    @staticmethod
    def _sanitize(value: Any) -> Any:
        """Recursively normalise values so the JSON payload never emits NaN/Infinity."""
        if isinstance(value, dict):
            return {key: DashboardServer._sanitize(val) for key, val in value.items()}
        if isinstance(value, list):
            return [DashboardServer._sanitize(item) for item in value]
        if isinstance(value, tuple):
            return [DashboardServer._sanitize(item) for item in value]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    @staticmethod
    def _safe_log_value(value: Any) -> str:
        """
        Sanitize values before logging to avoid log injection via crafted newlines.
        """
        text = "" if value is None else str(value)
        return text.replace("\r", "\\r").replace("\n", "\\n")

    def _json(self, payload: Any, **kwargs: Any) -> web.Response:
        return web.json_response(self._sanitize(payload), **kwargs)

    async def _cleanup(self) -> None:
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        self._site = None
        self._runner = None

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return

            @web.middleware
            async def _security_headers(request: web.Request, handler: Any) -> web.StreamResponse:
                response = await handler(request)
                response.headers.setdefault("X-Frame-Options", "DENY")
                response.headers.setdefault("X-Content-Type-Options", "nosniff")
                response.headers.setdefault("X-XSS-Protection", "1; mode=block")
                response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                response.headers.setdefault(
                    "Permissions-Policy",
                    "geolocation=(), microphone=(), camera=(), payment=()",
                )
                return response

            app = web.Application(middlewares=[_security_headers])
            app["dashboard"] = self
            app.add_routes(
                [
                    web.get("/", self._handle_index),
                    web.get("/admin", self._handle_index),
                    web.get("/auth/discord/login", self._handle_discord_login),
                    web.get("/auth/discord/callback", self._handle_discord_callback),
                    web.get("/auth/logout", self._handle_logout),
                    web.post("/auth/logout", self._handle_logout),
                    web.get("/api/auth/me", self._handle_auth_me),
                    web.get("/api/status", self._handle_status),
                    web.post("/api/bot/restart", self._handle_bot_restart),
                    web.post("/api/twitch/reload", self._handle_twitch_reload),
                    web.get("/api/twitch/metrics", self._handle_twitch_metrics),
                    web.post("/api/dashboard/restart", self._handle_dashboard_restart),
                    web.post("/api/cogs/reload", self._handle_reload),
                    web.post("/api/cogs/load", self._handle_load),
                    web.post("/api/cogs/unload", self._handle_unload),
                    web.post("/api/cogs/reload-all", self._handle_reload_all),
                    web.post("/api/cogs/reload-namespace", self._handle_reload_namespace),
                    web.post("/api/cogs/block", self._handle_block),
                    web.post("/api/cogs/unblock", self._handle_unblock),
                    web.get("/api/voice-stats", self._handle_voice_stats),
                    web.get("/api/voice-history", self._handle_voice_history),
                    web.get("/api/user-retention", self._handle_user_retention),
                    web.get("/api/member-events", self._handle_member_events),
                    web.get("/api/message-activity", self._handle_message_activity),
                    web.get("/api/co-player-network", self._handle_co_player_network),
                    web.get("/api/co-player-network/", self._handle_co_player_network),
                    web.get("/api/server-stats", self._handle_server_stats),
                    web.get("/api/tournament/overview", self._handle_tournament_overview),
                    web.post("/api/tournament/team", self._handle_tournament_team_create),
                    web.post("/api/tournament/assign", self._handle_tournament_assign),
                    web.post("/api/tournament/remove", self._handle_tournament_remove),
                    web.post("/api/cogs/discover", self._handle_discover),
                    web.get("/api/logs", self._handle_log_index),
                    web.get("/api/logs/{name}", self._handle_log_read),
                    web.get("/api/standalone", self._handle_standalone_list),
                    web.get("/api/standalone/{key}/logs", self._handle_standalone_logs),
                    web.post("/api/standalone/{key}/start", self._handle_standalone_start),
                    web.post("/api/standalone/{key}/stop", self._handle_standalone_stop),
                    web.post("/api/standalone/{key}/restart", self._handle_standalone_restart),
                    web.post("/api/standalone/{key}/autostart", self._handle_standalone_autostart),
                    web.post("/api/standalone/{key}/command", self._handle_standalone_command),
                ]
            )

            addr_in_use = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", 10048)}
            win_access = {getattr(errno, "WSAEACCES", 10013), errno.EACCES}

            async def _start_with(reuse_address: Optional[bool]) -> str:
                runner = web.AppRunner(app)
                await runner.setup()

                site_kwargs: Dict[str, Any] = {}
                if reuse_address:
                    site_kwargs["reuse_address"] = True

                try:
                    site = web.TCPSite(runner, self.host, self.port, **site_kwargs)
                    await site.start()
                except OSError as e:
                    await runner.cleanup()
                    if reuse_address and os.name == "nt" and e.errno in win_access:
                        logging.warning(
                            "reuse_address konnte auf Windows nicht aktiviert werden (%s). "
                            "Starte Dashboard ohne reuse_address.",
                            e,
                        )
                        return "retry_without_reuse"
                    if e.errno in addr_in_use:
                        return "addr_in_use"
                    raise

                self._runner = runner
                self._site = site
                return "started"

            async def _start_without_reuse_with_retries() -> None:
                retries = 3
                delay = 0.5
                for attempt in range(retries):
                    attempt_result = await _start_with(reuse_address=False)
                    if attempt_result == "started":
                        return
                    if attempt_result == "addr_in_use" and attempt < retries - 1:
                        await asyncio.sleep(delay)
                        delay *= 2
                        continue
                    if attempt_result == "addr_in_use":
                        raise RuntimeError(
                            f"Dashboard-Port {self.host}:{self.port} ist bereits belegt"
                        )
                    raise RuntimeError("Dashboard konnte nicht gestartet werden")
                raise RuntimeError("Dashboard konnte nicht gestartet werden")

            if os.name != "nt":
                result = await _start_with(reuse_address=True)
                if result == "addr_in_use":
                    raise RuntimeError(
                        f"Dashboard-Port {self.host}:{self.port} ist bereits belegt"
                    )
                if result != "started":
                    raise RuntimeError("Dashboard konnte nicht gestartet werden")
            else:
                result = await _start_with(reuse_address=True)
                if result == "started":
                    pass
                elif result == "retry_without_reuse":
                    await _start_without_reuse_with_retries()
                elif result == "addr_in_use":
                    # reuse_address hat trotzdem einen Konflikt ausgelöst – wir warten
                    # kurz und versuchen den Start ohne reuse_address erneut.
                    await asyncio.sleep(0.5)
                    await _start_without_reuse_with_retries()
                else:
                    raise RuntimeError("Dashboard konnte nicht gestartet werden")

            self._started = True
            base_no_slash = self._public_base_url.rstrip("/")
            if base_no_slash.lower().endswith("/admin"):
                admin_path = base_no_slash
            else:
                admin_path = base_no_slash + "/admin"
            logging.info("Master dashboard listening on %s", self._listen_base_url)
            if self._public_base_url != self._listen_base_url:
                logging.info("Master dashboard public URL set to %s", self._public_base_url)
            logging.info("Master dashboard admin UI: %s", admin_path)

    async def stop(self) -> None:
        async with self._lock:
            if not self._started:
                return
            try:
                await self._cleanup()
            finally:
                self._started = False
                logging.info("Master dashboard stopped")

    async def _restart_dashboard(self) -> Dict[str, Any]:
        # Allow the response to be flushed before we tear the server down.
        await asyncio.sleep(0.25)
        stop_error: Optional[str] = None
        try:
            await self.stop()
        except Exception as exc:  # pragma: no cover - defensive restart path
            stop_error = str(exc)
            logging.exception("Stopping dashboard before restart failed: %s", exc)

        await asyncio.sleep(0.1)

        try:
            await self.start()
            result: Dict[str, Any] = {
                "ok": stop_error is None,
                "listen_url": self._listen_base_url,
                "public_url": self._public_base_url,
            }
            if stop_error:
                result["error"] = stop_error
        except Exception as exc:  # pragma: no cover - defensive restart path
            logging.exception("Dashboard start failed during restart: %s", exc)
            result = {"ok": False, "error": str(exc)}

        self._last_restart = {
            "ok": result.get("ok"),
            "error": result.get("error"),
            "at": _dt.datetime.utcnow().isoformat() + "Z",
        }
        return result

    def _on_restart_finished(self, task: asyncio.Task) -> None:
        try:
            result = task.result()
            if isinstance(result, dict) and not result.get("ok", True):
                logging.warning("Dashboard restart finished with errors: %s", result.get("error"))
            else:
                logging.info("Dashboard restart completed")
        except Exception:  # pragma: no cover - defensive restart path
            logging.exception("Dashboard restart task crashed")
        finally:
            self._restart_task = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
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

    def _is_discord_oauth_configured(self) -> bool:
        return bool(self._discord_client_id and self._discord_client_secret and self._discord_redirect_uri)

    def _is_auth_enforced(self) -> bool:
        return bool(self.token or self._discord_auth_required or self._auth_misconfigured)

    def _extract_bearer_token(self, request: web.Request) -> str:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            token = header.split(" ", 1)[1].strip()
        else:
            token = header.strip()
        if not token and self._allow_query_token:
            token = (request.query.get("token") or "").strip()
        return token

    @staticmethod
    def _host_without_port(raw: Optional[str]) -> str:
        if not raw:
            return ""
        value = raw.split(",")[0].strip()
        if not value:
            return ""
        if value.startswith("["):
            end = value.find("]")
            if end != -1:
                value = value[1:end]
        elif ":" in value:
            host_part, port_part = value.rsplit(":", 1)
            if port_part.isdigit():
                value = host_part
        return value.lower()

    @staticmethod
    def _is_loopback_host(raw: Optional[str]) -> bool:
        host = DashboardServer._host_without_port(raw)
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

    def _is_secure_request(self, request: web.Request) -> bool:
        peer = self._peer_host(request)
        if self._is_loopback_host(peer):
            forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
            if forwarded_proto:
                return forwarded_proto == "https"
        return bool(request.secure)

    @staticmethod
    def _normalize_auth_next_path(raw: Optional[str]) -> str:
        fallback = "/admin"
        candidate = (raw or "").strip()
        if not candidate:
            return fallback
        try:
            parsed = urlparse(candidate)
        except Exception:
            return fallback
        if parsed.scheme or parsed.netloc:
            return fallback
        if not candidate.startswith("/"):
            return fallback
        if candidate.startswith("/admin") or candidate.startswith("/api/"):
            return candidate
        return fallback

    @staticmethod
    def _safe_internal_redirect(location: Optional[str], *, fallback: str = "/admin") -> str:
        candidate = (location or "").strip()
        if not candidate:
            return fallback
        if "\r" in candidate or "\n" in candidate:
            return fallback
        try:
            parsed = urlparse(candidate)
        except Exception:
            return fallback
        if parsed.scheme or parsed.netloc:
            return fallback
        if not candidate.startswith("/"):
            return fallback
        return candidate

    @staticmethod
    def _safe_template_href(location: Optional[str], *, fallback: str = "/admin") -> str:
        candidate = (location or "").strip()
        if not candidate:
            return fallback
        if any(ch in candidate for ch in {"\r", "\n", "<", ">", "\"", "'"}):
            return fallback
        try:
            parsed = urlparse(candidate)
        except Exception:
            return fallback
        scheme = (parsed.scheme or "").strip().lower()
        if scheme:
            if scheme not in {"http", "https"}:
                return fallback
            if not parsed.netloc:
                return fallback
            return candidate
        if parsed.netloc or not candidate.startswith("/"):
            return fallback
        return candidate

    def _build_discord_login_url(self, request: web.Request, *, next_path: Optional[str] = None) -> str:
        if not self._discord_auth_required:
            return "/admin"
        normalized = self._normalize_auth_next_path(
            next_path or (request.rel_url.path_qs if request.rel_url else "/admin")
        )
        return f"/auth/discord/login?{urlencode({'next': normalized})}"

    @staticmethod
    def _normalize_origin(raw: Optional[str]) -> Optional[str]:
        value = (raw or "").strip()
        if not value:
            return None
        try:
            parsed = urlparse(value)
        except Exception:
            return None
        scheme = (parsed.scheme or "").strip().lower()
        netloc = (parsed.netloc or "").strip().lower()
        if scheme not in {"http", "https"} or not netloc:
            return None
        return f"{scheme}://{netloc}"

    def _build_allowed_request_origins(self) -> Set[str]:
        origins: Set[str] = set()
        for base_url in (self._public_base_url, self._listen_base_url):
            normalized = self._normalize_origin(base_url)
            if normalized:
                origins.add(normalized)

        extra_raw = (os.getenv("MASTER_DASHBOARD_ALLOWED_ORIGINS") or "").strip()
        if extra_raw:
            for part in extra_raw.split(","):
                normalized = self._normalize_origin(part)
                if normalized:
                    origins.add(normalized)

        return origins

    def _request_origin(self, request: web.Request) -> Optional[str]:
        origin = self._normalize_origin(request.headers.get("Origin"))
        if origin:
            return origin

        referer = (request.headers.get("Referer") or "").strip()
        if not referer:
            return None
        return self._normalize_origin(referer)

    def _is_allowed_request_origin(self, request: web.Request) -> bool:
        if not self._allowed_request_origins:
            return True
        request_origin = self._request_origin(request)
        if not request_origin:
            return False
        return request_origin in self._allowed_request_origins

    @staticmethod
    def _requires_csrf_check(request: web.Request) -> bool:
        return request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}

    def _ensure_session_csrf_token(self, session: Dict[str, Any]) -> str:
        token = str(session.get("csrf_token") or "").strip()
        if token:
            return token
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
        return token

    def _check_csrf(self, request: web.Request, session: Dict[str, Any]) -> bool:
        expected = self._ensure_session_csrf_token(session)
        provided = (request.headers.get("X-CSRF-Token") or "").strip()
        if not provided:
            return False
        try:
            return secrets.compare_digest(provided, expected)
        except Exception:
            return False

    def _cleanup_discord_auth_state(self) -> None:
        now = time.time()
        expired_states = [
            key
            for key, row in self._discord_oauth_states.items()
            if now - float(row.get("created_at", 0.0)) > self._discord_oauth_state_ttl
        ]
        for key in expired_states:
            self._discord_oauth_states.pop(key, None)

        expired_sessions = [
            sid
            for sid, row in self._discord_sessions.items()
            if float(row.get("expires_at", 0.0)) <= now
        ]
        for sid in expired_sessions:
            self._discord_sessions.pop(sid, None)

        max_states = 1000
        if len(self._discord_oauth_states) > max_states:
            oldest = sorted(
                self._discord_oauth_states.items(),
                key=lambda item: float(item[1].get("created_at", 0.0)),
            )
            for state_key, _ in oldest[: len(self._discord_oauth_states) - max_states]:
                self._discord_oauth_states.pop(state_key, None)

        max_sessions = 5000
        if len(self._discord_sessions) > max_sessions:
            oldest_sessions = sorted(
                self._discord_sessions.items(),
                key=lambda item: float(item[1].get("created_at", 0.0)),
            )
            for session_key, _ in oldest_sessions[: len(self._discord_sessions) - max_sessions]:
                self._discord_sessions.pop(session_key, None)

    def _set_discord_session_cookie(self, response: web.StreamResponse, request: web.Request, session_id: str) -> None:
        response.set_cookie(
            self._discord_session_cookie,
            session_id,
            max_age=self._discord_session_ttl,
            httponly=True,
            secure=self._is_secure_request(request),
            samesite="Lax",
            path="/",
        )

    def _clear_discord_session_cookie(self, response: web.StreamResponse) -> None:
        response.del_cookie(self._discord_session_cookie, path="/")

    def _get_discord_auth_session(self, request: web.Request) -> Optional[Dict[str, Any]]:
        if not self._discord_auth_required:
            return None
        self._cleanup_discord_auth_state()
        session_id = (request.cookies.get(self._discord_session_cookie) or "").strip()
        if not session_id:
            return None
        session = self._discord_sessions.get(session_id)
        if not session:
            return None
        now = time.time()
        if float(session.get("expires_at", 0.0)) <= now:
            self._discord_sessions.pop(session_id, None)
            return None
        session["expires_at"] = now + self._discord_session_ttl
        session["last_seen_at"] = now
        self._ensure_session_csrf_token(session)
        return session

    def _auth_session_for_request(self, request: web.Request) -> Optional[Dict[str, Any]]:
        return self._get_discord_auth_session(request)

    async def _check_discord_member_access(self, discord_user_id: int) -> Tuple[bool, str]:
        if discord_user_id == self._discord_owner_user_id:
            return True, "owner_override"

        guilds: List[Any] = []
        seen: set[int] = set()
        for guild_id in self._discord_auth_guild_ids:
            guild = self.bot.get_guild(guild_id)
            if guild and guild.id not in seen:
                guilds.append(guild)
                seen.add(guild.id)
        if not guilds:
            guilds = list(getattr(self.bot, "guilds", []) or [])

        for guild in guilds:
            member = guild.get_member(discord_user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(discord_user_id)
                except Exception:
                    member = None
            if member is None:
                continue
            try:
                permissions = getattr(member, "guild_permissions", None)
                if permissions and bool(getattr(permissions, "administrator", False)):
                    return True, f"guild_admin:{guild.id}"
            except Exception:
                logger.debug(
                    "Failed to evaluate guild admin permissions for discord user %s in guild %s",
                    discord_user_id,
                    getattr(guild, "id", "?"),
                    exc_info=True,
                )
            try:
                role_ids = {int(r.id) for r in getattr(member, "roles", []) if getattr(r, "id", None)}
            except Exception:
                role_ids = set()
            if self._discord_moderator_role_id in role_ids:
                return True, f"moderator_role:{guild.id}"
        return False, "missing_admin_or_moderator_role"

    async def _exchange_discord_code(self, code: str, redirect_uri: str) -> Optional[Dict[str, Any]]:
        payload = {
            "client_id": self._discord_client_id,
            "client_secret": self._discord_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        timeout = ClientTimeout(total=20)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{DISCORD_API_BASE_URL}/oauth2/token",
                data=payload,
                headers=headers,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.warning(
                        "Discord OAuth code exchange failed (status=%s, body=%s)",
                        response.status,
                        self._safe_log_value(body[:200]),
                    )
                    return None
                data = await response.json()
        return data if isinstance(data, dict) else None

    async def _fetch_discord_user(self, access_token: str) -> Optional[Dict[str, Any]]:
        if not access_token:
            return None
        timeout = ClientTimeout(total=20)
        headers = {"Authorization": f"Bearer {access_token}"}
        async with ClientSession(timeout=timeout) as session:
            async with session.get(f"{DISCORD_API_BASE_URL}/users/@me", headers=headers) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.warning(
                        "Discord user lookup failed (status=%s, body=%s)",
                        response.status,
                        self._safe_log_value(body[:200]),
                    )
                    return None
                data = await response.json()
        return data if isinstance(data, dict) else None

    def _has_valid_auth(self, request: web.Request) -> bool:
        if self._discord_auth_required and self._get_discord_auth_session(request):
            return True
        if self.token:
            provided = self._extract_bearer_token(request)
            if provided and secrets.compare_digest(provided, self.token):
                return True
        return False

    def _check_auth(self, request: web.Request, *, required: bool = True) -> None:
        enforce = required or self._is_auth_enforced()
        if not enforce:
            return
        if self._auth_misconfigured:
            raise web.HTTPServiceUnavailable(
                text=(
                    "Dashboard Auth ist nicht korrekt konfiguriert. "
                    "Discord OAuth Client-ID/Secret fehlen im Windows-Tresor (DeadlockBot)."
                )
            )
        session = self._get_discord_auth_session(request) if self._discord_auth_required else None
        if session:
            if self._requires_csrf_check(request):
                if not self._is_allowed_request_origin(request):
                    raise web.HTTPForbidden(text="Origin validation failed")
                if not self._check_csrf(request, session):
                    raise web.HTTPForbidden(text="CSRF validation failed")
            return
        if self.token:
            provided = self._extract_bearer_token(request)
            if provided and secrets.compare_digest(provided, self.token):
                return

        next_path = "/admin" if request.path.startswith("/api/") else None
        login_url = self._build_discord_login_url(request, next_path=next_path)
        headers: Dict[str, str] = {"X-Auth-Login": login_url}
        if self.token:
            headers["WWW-Authenticate"] = "Bearer"
        raise web.HTTPUnauthorized(text="Authentication required", headers=headers)

    def _list_log_files(self) -> List[Dict[str, Any]]:
        log_dir = self._log_dir
        if not log_dir.exists() or not log_dir.is_dir():
            return []
        entries: List[Dict[str, Any]] = []
        for path in log_dir.iterdir():
            if not path.is_file():
                continue
            name = path.name
            if name.startswith("."):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            modified = _dt.datetime.fromtimestamp(
                stat.st_mtime,
                tz=_dt.timezone.utc,
            ).isoformat()
            entries.append(
                {
                    "name": name,
                    "size": stat.st_size,
                    "modified": modified,
                    "modified_ts": stat.st_mtime,
                }
            )
        entries.sort(key=lambda item: item["modified_ts"], reverse=True)
        for item in entries:
            item.pop("modified_ts", None)
        return entries

    def _resolve_log_file(self, name: str) -> Path:
        raw = (name or "").strip()
        if not raw:
            raise web.HTTPBadRequest(text="Log file missing")
        if raw in {".", ".."} or Path(raw).name != raw or ".." in raw:
            raise web.HTTPBadRequest(text="Invalid log filename")
        log_dir = self._log_dir
        path = log_dir / raw
        try:
            resolved = path.resolve()
            log_dir_resolved = log_dir.resolve()
        except (OSError, RuntimeError) as exc:
            raise web.HTTPBadRequest(text="Invalid log filename") from exc
        if log_dir_resolved not in resolved.parents and resolved != log_dir_resolved:
            raise web.HTTPBadRequest(text="Invalid log filename")
        if not path.exists() or not path.is_file():
            raise web.HTTPNotFound(text="Log file not found")
        return path

    @staticmethod
    def _tail_log_lines(path: Path, limit: int) -> List[str]:
        if limit <= 0:
            return []
        block_size = 8192
        data = b""
        lines: List[bytes] = []
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            while position > 0 and len(lines) <= limit and len(data) < LOG_TAIL_MAX_BYTES:
                read_size = min(block_size, position)
                position -= read_size
                handle.seek(position)
                data = handle.read(read_size) + data
                lines = data.splitlines()
        if len(lines) > limit:
            lines = lines[-limit:]
        return [line.decode("utf-8", errors="replace") for line in lines]

    def _normalize_names(self, items: Iterable[str]) -> List[str]:
        normalized: List[str] = []
        for raw in items:
            resolved, matches = self.bot.resolve_cog_identifier(raw)
            if resolved:
                normalized.append(resolved)
                continue
            if matches:
                raise web.HTTPBadRequest(text=f"Identifier '{raw}' is ambiguous: {', '.join(matches)}")
            raise web.HTTPBadRequest(text=f"Cog '{raw}' not found")
        return normalized

    @staticmethod
    def _format_netloc(host: str, port: Optional[int], scheme: str) -> str:
        safe_host = host.strip() or "127.0.0.1"
        if ":" in safe_host and not (safe_host.startswith("[") and safe_host.endswith("]")):
            safe_host = f"[{safe_host}]"
        default_ports = {"http": 80, "https": 443}
        default_port = default_ports.get(scheme, None)
        if port is None or (default_port is not None and port == default_port):
            return safe_host
        return f"{safe_host}:{port}"

    @staticmethod
    def _format_base_url(host: str, port: Optional[int], scheme: str) -> str:
        netloc = DashboardServer._format_netloc(host, port, scheme)
        return urlunparse((scheme, netloc, "", "", "", ""))

    @staticmethod
    def _normalize_public_url(value: str, *, default_scheme: str) -> str:
        raw = value.strip()
        if not raw:
            raise ValueError("Dashboard public URL must not be empty")
        parsed = urlparse(raw)
        if parsed.scheme and parsed.netloc:
            try:
                parsed_port: Optional[int] = parsed.port
            except ValueError:
                parsed_port = None
            netloc = DashboardServer._format_netloc(
                parsed.hostname or parsed.netloc,
                parsed_port,
                parsed.scheme,
            )
            path = parsed.path.rstrip("/")
            return urlunparse((parsed.scheme, netloc, path, parsed.params, parsed.query, parsed.fragment))

        if parsed.netloc and not parsed.scheme:
            scheme = default_scheme
            try:
                parsed_port = parsed.port
            except ValueError:
                parsed_port = None
            netloc = DashboardServer._format_netloc(parsed.hostname or parsed.netloc, parsed_port, scheme)
            path = parsed.path.rstrip("/")
            return urlunparse((scheme, netloc, path, parsed.params, parsed.query, parsed.fragment))

        fallback = urlparse(f"{default_scheme}://{raw}")
        try:
            fallback_port = fallback.port
        except ValueError:
            fallback_port = None
        netloc = DashboardServer._format_netloc(
            fallback.hostname or fallback.netloc or fallback.path,
            fallback_port,
            fallback.scheme,
        )
        path = fallback.path.rstrip("/")
        return urlunparse(
            (fallback.scheme, netloc, path, fallback.params, fallback.query, fallback.fragment)
        )

    def _resolve_nssm_executable_path(self) -> Optional[str]:
        configured = (self._nssm_executable or "").strip()
        if configured:
            configured_path = Path(configured).expanduser()
            if configured_path.is_absolute() and configured_path.is_file():
                return str(configured_path)
            return None

        for candidate in NSSM_PATH_CANDIDATES:
            if Path(candidate).is_file():
                return candidate
        return None

    def _resolve_powershell_executable_path(self) -> Optional[str]:
        configured = (self._powershell_executable or "").strip()
        if configured:
            configured_path = Path(configured).expanduser()
            if configured_path.is_absolute() and configured_path.is_file():
                return str(configured_path)
            return None

        for candidate in POWERSHELL_PATH_CANDIDATES:
            if Path(candidate).is_file():
                return candidate
        return None

    @staticmethod
    def _powershell_literal(value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def _build_nssm_restart_script(self, nssm_executable: str) -> str:
        delay_ms = max(250, int(self._nssm_restart_delay_seconds * 1000))
        return (
            "$ErrorActionPreference = 'Stop'; "
            f"Start-Sleep -Milliseconds {delay_ms}; "
            f"& {self._powershell_literal(nssm_executable)} restart {self._powershell_literal(self._nssm_service_name)}"
        )

    def _schedule_nssm_service_restart(self) -> Tuple[bool, str]:
        if not self._nssm_service_restart_enabled:
            return False, "NSSM restart disabled (MASTER_NSSM_RESTART_ENABLED=0)"
        if os.name != "nt":
            return False, "NSSM restart is only available on Windows hosts"
        if not self._nssm_service_name:
            return False, "NSSM service name missing (MASTER_NSSM_SERVICE_NAME)"

        nssm_executable = self._resolve_nssm_executable_path()
        if not nssm_executable:
            if self._nssm_executable:
                return False, (
                    "NSSM executable not found or not absolute file path: "
                    f"{self._nssm_executable}"
                )
            return False, "nssm.exe not found in hardened path list (set MASTER_NSSM_EXE)"

        powershell_exe = self._resolve_powershell_executable_path()
        if not powershell_exe:
            if self._powershell_executable:
                return False, (
                    "PowerShell executable not found or not absolute file path: "
                    f"{self._powershell_executable}"
                )
            return False, "powershell.exe not found in hardened path list"

        creationflags = 0
        for flag_name in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS", "CREATE_NO_WINDOW"):
            creationflags |= int(getattr(subprocess, flag_name, 0) or 0)

        command = [
            powershell_exe,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            self._build_nssm_restart_script(nssm_executable),
        ]
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
        except Exception as exc:
            logger.exception("Failed to launch NSSM restart for service '%s': %s", self._nssm_service_name, exc)
            return False, f"Failed to launch NSSM restart: {exc}"

        logger.info(
            "NSSM restart scheduled for service '%s' (nssm=%s, delay=%.2fs)",
            self._safe_log_value(self._nssm_service_name),
            self._safe_log_value(nssm_executable),
            self._nssm_restart_delay_seconds,
        )
        return True, f"Service restart requested ({self._nssm_service_name})"

    @staticmethod
    def _parse_positive_float(raw: Optional[str], *, default: float, env_name: str) -> float:
        if raw is None:
            return default
        value = raw.strip()
        if not value:
            return default
        try:
            parsed = float(value)
        except ValueError:
            logging.warning("%s '%s' invalid – using default %.1fs", env_name, raw, default)
            return default
        if parsed <= 0:
            logging.warning("%s '%s' must be > 0 – using default %.1fs", env_name, raw, default)
            return default
        return parsed

    @staticmethod
    def _coerce_bool(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        return None

    @staticmethod
    def _coerce_int(value: Any, default: Optional[int] = None) -> Optional[int]:
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_metadata_json(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if raw is None:
            return {}
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8", errors="ignore")
            except Exception:
                return {}
        if not isinstance(raw, str):
            return {}
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _resolve_twitch_dashboard_href(self) -> str:
        public_base = (self._public_base_url or "").rstrip("/")
        if public_base.lower().endswith("/admin"):
            public_base = public_base[:-6]
        if public_base and not self._is_loopback_host(urlparse(public_base).hostname or ""):
            return f"{public_base}/twitch/admin"

        base = self._format_base_url("127.0.0.1", 8765, self._scheme)
        return f"{base.rstrip('/')}/twitch/admin"

    def _derive_steam_return_url(self) -> Optional[str]:
        base = (self._public_base_url or "").strip().rstrip("/")
        if not base:
            return None
        path = "/steam/return"
        path = "/" + path.lstrip("/")
        return f"{base}{path}"

    def _derive_raid_health_url(self) -> Optional[str]:
        return "https://raid.earlysalty.com/health"

    def _build_health_targets(self) -> List[Dict[str, Any]]:
        targets: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()

        def _append_query_param(url: str, key: str, value: str) -> str:
            try:
                parsed = urlparse(url)
            except Exception:
                return url
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if query.get(key) == value:
                return url
            query[key] = value
            return urlunparse(parsed._replace(query=urlencode(query)))

        def _add_target(
            label: str,
            url: str,
            *,
            key: Optional[str] = None,
            method: str = "GET",
        ) -> None:
            safe_url = (url or "").strip()
            if not safe_url:
                return
            if safe_url.startswith("http://") or safe_url.startswith("https://"):
                try:
                    safe_url = self._normalize_public_url(safe_url, default_scheme=self._scheme)
                except Exception as exc:
                    logging.warning("Healthcheck URL '%s' invalid (%s) – skipping entry", url, exc)
                    return
            safe_label = (label or safe_url).strip() or safe_url
            safe_method = (method or "GET").strip().upper() or "GET"
            key_base = (key or self._slugify_health_key(safe_label)).strip() or "health"
            unique_key = key_base
            suffix = 2
            while unique_key in seen_keys:
                unique_key = f"{key_base}-{suffix}"
                suffix += 1
            seen_keys.add(unique_key)

            entry: Dict[str, Any] = {
                "key": unique_key,
                "label": safe_label,
                "url": safe_url,
                "method": safe_method,
            }
            targets.append(entry)

        if self._twitch_dashboard_href:
            _add_target("Twitch Dashboard", self._twitch_dashboard_href, key="twitch-dashboard")
        if self._steam_return_url:
            steam_health_url = _append_query_param(self._steam_return_url, "healthcheck", "1")
            _add_target("Steam OAuth Callback", steam_health_url, key="steam-oauth-callback")
        if self._raid_health_url:
            _add_target("Raid Callback Host", self._raid_health_url, key="raid-callback-host")

        # Explicit Health Checks for Core Domains
        _add_target("Main Site", "https://earlysalty.de/health", key="main-site")
        _add_target("Steam Link Service", "https://link.earlysalty.com/health", key="steam-link-service")
        _add_target("Raid Service", "https://raid.earlysalty.com/health", key="raid-service")
        # /twitch/stats requires auth; use a public endpoint to avoid false 401 alarms.
        _add_target("Twitch Stats", "https://twitch.earlysalty.com/twitch/api/v2/auth-status", key="twitch-stats")

        return targets

    @staticmethod
    def _slugify_health_key(value: str) -> str:
        slug = "".join(char.lower() if char.isalnum() else "-" for char in value)
        pieces = [part for part in slug.split("-") if part]
        return "-".join(pieces) or "health"

    def _normalized_discord_redirect_uri(self) -> Optional[str]:
        raw = (self._discord_redirect_uri or "").strip()
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
        if (parsed.path or "").rstrip("/") != "/auth/discord/callback":
            return None
        return urlunparse((scheme, parsed.netloc, "/auth/discord/callback", "", "", ""))

    async def _handle_discord_login(self, request: web.Request) -> web.StreamResponse:
        if self._auth_misconfigured:
            raise web.HTTPServiceUnavailable(
                text=(
                    "Dashboard Auth ist nicht korrekt konfiguriert. "
                    "Discord OAuth Client-ID/Secret fehlen im Windows-Tresor (DeadlockBot)."
                )
            )
        if not self._discord_auth_required:
            raise web.HTTPFound("/admin")

        existing = self._get_discord_auth_session(request)
        next_path = self._normalize_auth_next_path(request.query.get("next"))
        if existing:
            safe_next = self._safe_internal_redirect(next_path, fallback="/admin")
            raise web.HTTPFound(safe_next)

        redirect_uri = self._normalized_discord_redirect_uri()
        if not redirect_uri:
            expected_redirect = (
                str(self._discord_redirect_uri or "").strip()
                or "https://admin.earlysalty.com/auth/discord/callback"
            )
            raise web.HTTPServiceUnavailable(
                text=(
                    "Discord OAuth Redirect URI ist ungültig. "
                    f"Erwartet wird exakt: {expected_redirect}."
                )
            )

        self._cleanup_discord_auth_state()
        state = secrets.token_urlsafe(32)
        self._discord_oauth_states[state] = {
            "created_at": time.time(),
            "next_path": next_path,
            "redirect_uri": redirect_uri,
        }
        query = urlencode(
            {
                "client_id": self._discord_client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "identify",
                "state": state,
            }
        )
        raise web.HTTPFound(f"{DISCORD_API_BASE_URL}/oauth2/authorize?{query}")

    async def _handle_discord_callback(self, request: web.Request) -> web.StreamResponse:
        if not self._discord_auth_required:
            raise web.HTTPFound("/admin")

        error = (request.query.get("error") or "").strip()
        if error:
            return web.Response(text=f"Discord OAuth Fehler: {error}", status=401)

        state = (request.query.get("state") or "").strip()
        code = (request.query.get("code") or "").strip()
        if not state or not code:
            return web.Response(text="Fehlender OAuth state/code.", status=400)

        self._cleanup_discord_auth_state()
        state_data = self._discord_oauth_states.pop(state, None)
        if not state_data:
            return web.Response(text="OAuth state ungültig oder abgelaufen.", status=400)

        token_data = await self._exchange_discord_code(code, str(state_data.get("redirect_uri") or ""))
        access_token = str((token_data or {}).get("access_token") or "").strip()
        if not access_token:
            return web.Response(text="OAuth Austausch fehlgeschlagen.", status=401)

        user = await self._fetch_discord_user(access_token)
        if not user:
            return web.Response(text="Discord-User konnte nicht geladen werden.", status=401)

        user_id = self._coerce_int(user.get("id"), None)
        if not user_id:
            return web.Response(text="Ungültige Discord-User-ID.", status=401)

        allowed, reason = await self._check_discord_member_access(int(user_id))
        if not allowed:
            logger.warning(
                "AUDIT master-dashboard login denied: discord_user=%s reason=%s peer=%s",
                user_id,
                self._safe_log_value(reason),
                self._safe_log_value(self._peer_host(request)),
            )
            return web.Response(
                text=(
                    "Kein Zugriff auf das Admin-Dashboard. "
                    "Benötigt: Administrator-Recht oder Moderator-Rolle."
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

        self._cleanup_discord_auth_state()
        now = time.time()
        session_id = secrets.token_urlsafe(32)
        self._discord_sessions[session_id] = {
            "user_id": int(user_id),
            "username": username,
            "display_name": display_name,
            "reason": reason,
            "csrf_token": secrets.token_urlsafe(32),
            "created_at": now,
            "last_seen_at": now,
            "expires_at": now + self._discord_session_ttl,
        }

        logger.info(
            "AUDIT master-dashboard login success: discord_user=%s reason=%s peer=%s",
            user_id,
            self._safe_log_value(reason),
            self._safe_log_value(self._peer_host(request)),
        )

        destination = self._normalize_auth_next_path(state_data.get("next_path"))
        safe_destination = self._safe_internal_redirect(destination, fallback="/admin")
        response = web.HTTPFound(safe_destination)
        self._set_discord_session_cookie(response, request, session_id)
        raise response

    async def _handle_logout(self, request: web.Request) -> web.StreamResponse:
        session_id = (request.cookies.get(self._discord_session_cookie) or "").strip()
        if session_id:
            self._discord_sessions.pop(session_id, None)
        login_url = self._safe_internal_redirect(
            self._build_discord_login_url(request, next_path="/admin"),
            fallback="/auth/discord/login?next=%2Fadmin",
        )
        response = web.HTTPFound(login_url)
        self._clear_discord_session_cookie(response)
        raise response

    async def _handle_auth_me(self, request: web.Request) -> web.Response:
        if not self._discord_auth_required:
            return self._json(
                {
                    "enabled": False,
                    "authenticated": False,
                    "mode": "token" if self.token else "none",
                }
            )

        session = self._get_discord_auth_session(request)
        if not session:
            provided = self._extract_bearer_token(request)
            if self.token and provided and secrets.compare_digest(provided, self.token):
                return self._json(
                    {
                        "enabled": bool(self._discord_auth_required),
                        "authenticated": True,
                        "mode": "token",
                        "user": None,
                        "csrf_token": None,
                    }
                )
            self._check_auth(request)
            raise web.HTTPUnauthorized(text="Authentication required")

        csrf_token = self._ensure_session_csrf_token(session)
        return self._json(
            {
                "enabled": True,
                "authenticated": True,
                "mode": "discord",
                "user": {
                    "id": session.get("user_id"),
                    "display_name": session.get("display_name"),
                    "username": session.get("username"),
                },
                "csrf_token": csrf_token,
            }
        )

    async def _handle_index(self, request: web.Request) -> web.Response:
        if self._auth_misconfigured:
            raise web.HTTPServiceUnavailable(
                text=(
                    "Dashboard Auth ist nicht korrekt konfiguriert. "
                    "Discord OAuth Client-ID/Secret fehlen im Windows-Tresor (DeadlockBot)."
                )
            )
        if self._is_auth_enforced() and not self._has_valid_auth(request):
            if self._discord_auth_required:
                login_url = self._safe_internal_redirect(
                    self._build_discord_login_url(request, next_path="/admin"),
                    fallback="/auth/discord/login?next=%2Fadmin",
                )
                raise web.HTTPFound(login_url)
            self._check_auth(request)

        session = self._get_discord_auth_session(request)
        display_name = str((session or {}).get("display_name") or "Nicht angemeldet")
        safe_twitch_url = html.escape(
            self._safe_template_href(self._twitch_dashboard_href or "", fallback="/twitch/admin"),
            quote=True,
        )
        safe_discord_login_url = html.escape(
            self._safe_template_href(
                self._build_discord_login_url(request, next_path="/admin"),
                fallback="/auth/discord/login?next=%2Fadmin",
            ),
            quote=True,
        )
        safe_auth_logout_url = html.escape(
            self._safe_template_href("/auth/logout", fallback="/auth/logout"),
            quote=True,
        )
        html_text = (
            _load_index_html()
            .replace("{{TWITCH_URL}}", safe_twitch_url)
            .replace("{{AUTH_USER_LABEL}}", html.escape(display_name, quote=True))
            .replace("{{DISCORD_LOGIN_URL}}", safe_discord_login_url)
            .replace("{{AUTH_LOGOUT_URL}}", safe_auth_logout_url)
        )
        return web.Response(text=html_text, content_type="text/html")

    def _voice_cog(self) -> Any:
        """
        Try to retrieve the VoiceActivityTrackerCog instance without importing it directly.
        Falls back to name matching to stay resilient if the cog isn't loaded.
        """
        try:
            cog = self.bot.get_cog("VoiceActivityTrackerCog")
            if cog:
                return cog
        except Exception:
            logging.getLogger(__name__).debug(
                "VoiceActivityTrackerCog lookup failed via direct get_cog", exc_info=True
            )
        for cog in self.bot.cogs.values():
            if cog.__class__.__name__ == "VoiceActivityTrackerCog":
                return cog
        return None

    def _resolve_display_names(self, user_ids: Iterable[int]) -> Dict[int, str]:
        names: Dict[int, str] = {}
        for uid in {u for u in user_ids if u}:
            display_name: Optional[str] = None
            for guild in self.bot.guilds:
                try:
                    member = guild.get_member(uid)
                except Exception:
                    member = None
                if member:
                    display_name = getattr(member, "display_name", None) or getattr(member, "name", None)
                    break
            if not display_name:
                user = self.bot.get_user(uid)
                if user:
                    display_name = getattr(user, "display_name", None) or getattr(user, "name", None)
            names[uid] = display_name or f"User {uid}"
        return names

    def _retention_excluded_roles(self) -> set[int]:
        try:
            cog = self.bot.get_cog("UserRetentionCog")
            cfg = getattr(cog, "config", None) if cog else None
            roles = getattr(cfg, "excluded_role_ids", None)
            resolved = {int(r) for r in roles if r} if roles else set()
            if resolved:
                return resolved
        except Exception:  # pragma: no cover - defensive
            logger.debug("Could not resolve retention exclusion roles from cog", exc_info=True)
        return set(DEFAULT_RETENTION_EXCLUDED_ROLE_IDS)

    def _has_retention_excluded_role(
        self,
        user_id: Optional[int],
        guild_id: Optional[int],
        excluded: set[int],
    ) -> bool:
        if not user_id or not excluded:
            return False

        guilds: List[Any] = []
        if guild_id:
            try:
                guild = self.bot.get_guild(int(guild_id))
            except Exception:
                guild = None
            if guild:
                guilds.append(guild)
        if not guilds:
            guilds = list(self.bot.guilds)

        for guild in guilds:
            try:
                member = guild.get_member(int(user_id))
            except Exception:
                member = None
            if not member:
                continue
            try:
                member_roles = {r.id for r in getattr(member, "roles", []) or []}
            except Exception:
                member_roles = set()
            if member_roles and member_roles & excluded:
                return True
        return False

    async def _collect_live_voice_sessions(self) -> List[Dict[str, Any]]:
        cog = self._voice_cog()
        if not cog:
            return []
        try:
            voice_sessions = dict(getattr(cog, "voice_sessions", {}) or {})
        except Exception:
            voice_sessions = {}
        now = _dt.datetime.utcnow()
        sessions: List[Dict[str, Any]] = []
        for session in voice_sessions.values():
            user_id = session.get("user_id")
            start_time = session.get("start_time")
            guild_id = session.get("guild_id")
            channel_id = session.get("channel_id")
            channel_name = session.get("channel_name")
            if not channel_name and guild_id and channel_id:
                guild = self.bot.get_guild(guild_id)
                if guild:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        channel_name = getattr(channel, "name", None) or channel_name
            started_at: Optional[str]
            if isinstance(start_time, _dt.datetime):
                try:
                    started_at = start_time.replace(tzinfo=_dt.timezone.utc).isoformat()
                except Exception:
                    started_at = start_time.isoformat()
                duration_seconds = max(0, int((now - start_time).total_seconds()))
            else:
                started_at = None
                duration_seconds = 0
            sessions.append(
                {
                    "user_id": user_id,
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "duration_seconds": duration_seconds,
                    "peak_users": session.get("peak_users") or 1,
                    "started_at": started_at,
                }
            )
        sessions.sort(key=lambda s: s.get("duration_seconds", 0), reverse=True)
        return sessions
    async def _handle_twitch_reload(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        if hasattr(self.bot, "reload_cog"):
            # MasterBot mit CogLoaderMixin -> nutzt _purge_namespace_modules
            success, msg = await self.bot.reload_cog("cogs.twitch")
            if success:
                return web.json_response({"ok": True, "message": msg})
            else:
                return web.json_response({"ok": False, "error": msg}, status=500)
        else:
            try:
                await self.bot.reload_extension("cogs.twitch")
                return web.json_response({"ok": True, "message": "Twitch module reloaded (no purge)"})
            except Exception:
                logger.exception("Failed to reload Twitch module via dashboard")
                return web.json_response({"ok": False, "error": "Internal server error"}, status=500)

    async def _handle_twitch_metrics(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))

        raw_hours = request.query.get("hours")
        try:
            hours = int(raw_hours) if raw_hours else 24
            if hours <= 0:
                raise ValueError
            hours = min(hours, 168)
        except ValueError:
            raise web.HTTPBadRequest(text="hours must be a positive integer (max 168)")

        cutoff = f"-{hours} hours"

        def _safe_query_all(query: str, params: Tuple[Any, ...] = ()) -> List[Any]:
            try:
                return db.query_all(query, params)
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "no such table" in msg or "no such column" in msg:
                    return []
                raise

        def _safe_query_one(query: str, params: Tuple[Any, ...] = ()) -> Optional[Any]:
            try:
                return db.query_one(query, params)
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if "no such table" in msg or "no such column" in msg:
                    return None
                raise

        def _as_int(row: Any, key: str, default: int = 0) -> int:
            if row is None:
                return default
            try:
                value = row[key] if hasattr(row, "keys") else row[key]
            except Exception:
                try:
                    value = row[key]
                except Exception:
                    return default
            try:
                return int(value or 0)
            except Exception:
                return default

        def _as_float(row: Any, key: str, default: float = 0.0) -> float:
            if row is None:
                return default
            try:
                value = row[key] if hasattr(row, "keys") else row[key]
            except Exception:
                try:
                    value = row[key]
                except Exception:
                    return default
            try:
                return float(value or 0.0)
            except Exception:
                return default

        try:
            raids_hourly_rows = _safe_query_all(
                """
                SELECT
                    strftime('%Y-%m-%d %H:00:00', datetime(replace(substr(executed_at, 1, 19), 'T', ' '))) AS bucket_hour,
                    COUNT(*) AS raid_count,
                    SUM(COALESCE(viewer_count, 0)) AS raid_viewers
                FROM twitch_raid_history
                WHERE datetime(replace(substr(executed_at, 1, 19), 'T', ' ')) >= datetime('now', ?)
                GROUP BY bucket_hour
                ORDER BY bucket_hour ASC
                """,
                (cutoff,),
            )
            raids_summary_row = _safe_query_one(
                """
                SELECT
                    COUNT(*) AS raids_total,
                    SUM(COALESCE(viewer_count, 0)) AS raid_viewers_total,
                    COUNT(DISTINCT LOWER(COALESCE(to_broadcaster_login, ''))) AS unique_targets,
                    COUNT(DISTINCT LOWER(COALESCE(from_broadcaster_login, ''))) AS unique_sources
                FROM twitch_raid_history
                WHERE datetime(replace(substr(executed_at, 1, 19), 'T', ' ')) >= datetime('now', ?)
                """,
                (cutoff,),
            )

            active_hourly_rows = _safe_query_all(
                """
                SELECT
                    strftime('%Y-%m-%d %H:00:00', datetime(replace(substr(ts_utc, 1, 19), 'T', ' '))) AS bucket_hour,
                    COUNT(DISTINCT LOWER(COALESCE(streamer, ''))) AS active_streamers
                FROM twitch_stats_tracked
                WHERE datetime(replace(substr(ts_utc, 1, 19), 'T', ' ')) >= datetime('now', ?)
                GROUP BY bucket_hour
                ORDER BY bucket_hour ASC
                """,
                (cutoff,),
            )
            active_now_row = _safe_query_one(
                """
                SELECT COUNT(*) AS active_now
                FROM twitch_live_state
                WHERE COALESCE(is_live, 0) = 1
                """
            )

            eventsub_hourly_rows = _safe_query_all(
                """
                SELECT
                    strftime('%Y-%m-%d %H:00:00', datetime(replace(substr(ts_utc, 1, 19), 'T', ' '))) AS bucket_hour,
                    AVG(COALESCE(utilization_pct, 0)) AS avg_utilization_pct,
                    MAX(COALESCE(utilization_pct, 0)) AS peak_utilization_pct,
                    AVG(COALESCE(used_slots, 0)) AS avg_used_slots,
                    MAX(COALESCE(used_slots, 0)) AS peak_used_slots,
                    AVG(COALESCE(listener_count, 0)) AS avg_listener_count,
                    COUNT(*) AS samples
                FROM twitch_eventsub_capacity_snapshot
                WHERE datetime(replace(substr(ts_utc, 1, 19), 'T', ' ')) >= datetime('now', ?)
                GROUP BY bucket_hour
                ORDER BY bucket_hour ASC
                """,
                (cutoff,),
            )
            eventsub_summary_row = _safe_query_one(
                """
                SELECT
                    AVG(COALESCE(utilization_pct, 0)) AS avg_utilization_pct,
                    MAX(COALESCE(utilization_pct, 0)) AS peak_utilization_pct,
                    AVG(COALESCE(used_slots, 0)) AS avg_used_slots,
                    MAX(COALESCE(used_slots, 0)) AS peak_used_slots,
                    AVG(COALESCE(listener_count, 0)) AS avg_listener_count,
                    MAX(COALESCE(listener_count, 0)) AS max_listener_count,
                    SUM(COALESCE(samples, 1)) AS samples
                FROM (
                    SELECT utilization_pct, used_slots, listener_count, 1 AS samples
                    FROM twitch_eventsub_capacity_snapshot
                    WHERE datetime(replace(substr(ts_utc, 1, 19), 'T', ' ')) >= datetime('now', ?)
                )
                """,
                (cutoff,),
            )
            eventsub_latest_row = _safe_query_one(
                """
                SELECT
                    ts_utc,
                    utilization_pct,
                    used_slots,
                    total_slots,
                    listener_count,
                    trigger_reason
                FROM twitch_eventsub_capacity_snapshot
                ORDER BY datetime(replace(substr(ts_utc, 1, 19), 'T', ' ')) DESC
                LIMIT 1
                """
            )
            eventsub_reason_rows = _safe_query_all(
                """
                SELECT
                    trigger_reason,
                    COUNT(*) AS samples,
                    MAX(COALESCE(utilization_pct, 0)) AS peak_utilization_pct
                FROM twitch_eventsub_capacity_snapshot
                WHERE datetime(replace(substr(ts_utc, 1, 19), 'T', ' ')) >= datetime('now', ?)
                GROUP BY trigger_reason
                ORDER BY samples DESC, trigger_reason ASC
                LIMIT 8
                """,
                (cutoff,),
            )
        except Exception as exc:
            logging.exception("Failed to load twitch metrics: %s", exc)
            raise web.HTTPInternalServerError(text="Twitch metrics unavailable") from exc

        now_utc = _dt.datetime.now(tz=_dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        start_utc = now_utc - _dt.timedelta(hours=max(0, hours - 1))
        bucket_keys: List[str] = []
        labels: List[str] = []
        for i in range(hours):
            bucket_dt = start_utc + _dt.timedelta(hours=i)
            bucket_keys.append(bucket_dt.strftime("%Y-%m-%d %H:00:00"))
            labels.append(bucket_dt.strftime("%d.%m %H:%M"))

        raids_map: Dict[str, Dict[str, float]] = {}
        for row in raids_hourly_rows:
            key = str(row["bucket_hour"] if hasattr(row, "keys") else row[0] or "")
            if not key:
                continue
            raids_map[key] = {
                "raid_count": float(row["raid_count"] if hasattr(row, "keys") else row[1] or 0),
                "raid_viewers": float(row["raid_viewers"] if hasattr(row, "keys") else row[2] or 0),
            }

        active_map: Dict[str, float] = {}
        for row in active_hourly_rows:
            key = str(row["bucket_hour"] if hasattr(row, "keys") else row[0] or "")
            if not key:
                continue
            active_map[key] = float(row["active_streamers"] if hasattr(row, "keys") else row[1] or 0)

        eventsub_map: Dict[str, Dict[str, float]] = {}
        for row in eventsub_hourly_rows:
            key = str(row["bucket_hour"] if hasattr(row, "keys") else row[0] or "")
            if not key:
                continue
            eventsub_map[key] = {
                "avg_utilization_pct": float(row["avg_utilization_pct"] if hasattr(row, "keys") else row[1] or 0.0),
                "peak_utilization_pct": float(row["peak_utilization_pct"] if hasattr(row, "keys") else row[2] or 0.0),
                "avg_used_slots": float(row["avg_used_slots"] if hasattr(row, "keys") else row[3] or 0.0),
                "peak_used_slots": float(row["peak_used_slots"] if hasattr(row, "keys") else row[4] or 0.0),
                "avg_listener_count": float(row["avg_listener_count"] if hasattr(row, "keys") else row[5] or 0.0),
                "samples": float(row["samples"] if hasattr(row, "keys") else row[6] or 0),
            }

        raids_series: List[int] = []
        raid_viewers_series: List[int] = []
        active_streamers_series: List[int] = []
        eventsub_avg_util_series: List[Optional[float]] = []
        eventsub_peak_util_series: List[Optional[float]] = []
        eventsub_used_slots_series: List[Optional[float]] = []
        eventsub_listener_series: List[Optional[float]] = []

        for key in bucket_keys:
            raid_row = raids_map.get(key, {})
            active_row = active_map.get(key, 0.0)
            event_row = eventsub_map.get(key)

            raids_series.append(int(round(float(raid_row.get("raid_count", 0.0)))))
            raid_viewers_series.append(int(round(float(raid_row.get("raid_viewers", 0.0)))))
            active_streamers_series.append(int(round(float(active_row or 0.0))))

            if event_row:
                eventsub_avg_util_series.append(round(float(event_row.get("avg_utilization_pct", 0.0)), 2))
                eventsub_peak_util_series.append(round(float(event_row.get("peak_utilization_pct", 0.0)), 2))
                eventsub_used_slots_series.append(round(float(event_row.get("avg_used_slots", 0.0)), 2))
                eventsub_listener_series.append(round(float(event_row.get("avg_listener_count", 0.0)), 2))
            else:
                eventsub_avg_util_series.append(None)
                eventsub_peak_util_series.append(None)
                eventsub_used_slots_series.append(None)
                eventsub_listener_series.append(None)

        raids_total = _as_int(raids_summary_row, "raids_total", 0)
        raid_viewers_total = _as_int(raids_summary_row, "raid_viewers_total", 0)
        unique_targets = _as_int(raids_summary_row, "unique_targets", 0)
        unique_sources = _as_int(raids_summary_row, "unique_sources", 0)

        active_now = _as_int(active_now_row, "active_now", 0)
        active_peak = max(active_streamers_series) if active_streamers_series else 0
        active_avg = (sum(active_streamers_series) / len(active_streamers_series)) if active_streamers_series else 0.0

        eventsub_avg = _as_float(eventsub_summary_row, "avg_utilization_pct", 0.0)
        eventsub_peak = _as_float(eventsub_summary_row, "peak_utilization_pct", 0.0)
        eventsub_avg_slots = _as_float(eventsub_summary_row, "avg_used_slots", 0.0)
        eventsub_peak_slots = _as_float(eventsub_summary_row, "peak_used_slots", 0.0)
        eventsub_avg_listeners = _as_float(eventsub_summary_row, "avg_listener_count", 0.0)
        eventsub_max_listeners = _as_int(eventsub_summary_row, "max_listener_count", 0)
        eventsub_samples = _as_int(eventsub_summary_row, "samples", 0)

        eventsub_latest = {
            "ts_utc": (eventsub_latest_row["ts_utc"] if eventsub_latest_row and hasattr(eventsub_latest_row, "keys") else None),
            "utilization_pct": _as_float(eventsub_latest_row, "utilization_pct", 0.0),
            "used_slots": _as_int(eventsub_latest_row, "used_slots", 0),
            "total_slots": _as_int(eventsub_latest_row, "total_slots", 0),
            "listener_count": _as_int(eventsub_latest_row, "listener_count", 0),
            "reason": (eventsub_latest_row["trigger_reason"] if eventsub_latest_row and hasattr(eventsub_latest_row, "keys") else None),
        }

        reason_top = []
        for row in eventsub_reason_rows:
            reason_top.append(
                {
                    "reason": str(row["trigger_reason"] if hasattr(row, "keys") else row[0] or ""),
                    "samples": int(row["samples"] if hasattr(row, "keys") else row[1] or 0),
                    "peak_utilization_pct": float(row["peak_utilization_pct"] if hasattr(row, "keys") else row[2] or 0.0),
                }
            )

        payload = {
            "window_hours": hours,
            "generated_at": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds"),
            "summary": {
                "raids_total": raids_total,
                "raid_viewers_total": raid_viewers_total,
                "unique_targets": unique_targets,
                "unique_sources": unique_sources,
                "active_streamers_now": active_now,
                "active_streamers_peak": active_peak,
                "active_streamers_avg": round(active_avg, 2),
                "eventsub_samples": eventsub_samples,
                "eventsub_avg_utilization_pct": round(eventsub_avg, 2),
                "eventsub_peak_utilization_pct": round(eventsub_peak, 2),
                "eventsub_avg_used_slots": round(eventsub_avg_slots, 2),
                "eventsub_peak_used_slots": round(eventsub_peak_slots, 2),
                "eventsub_avg_listener_count": round(eventsub_avg_listeners, 2),
                "eventsub_max_listener_count": eventsub_max_listeners,
                "eventsub_latest": eventsub_latest,
            },
            "timeline": {
                "labels": labels,
                "raids": raids_series,
                "raid_viewers": raid_viewers_series,
                "active_streamers": active_streamers_series,
                "eventsub_avg_utilization_pct": eventsub_avg_util_series,
                "eventsub_peak_utilization_pct": eventsub_peak_util_series,
                "eventsub_avg_used_slots": eventsub_used_slots_series,
                "eventsub_avg_listener_count": eventsub_listener_series,
            },
            "reasons_top": reason_top,
        }
        return self._json(payload)

    async def _handle_voice_stats(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        raw_limit = request.query.get("limit")
        try:
            limit = int(raw_limit) if raw_limit else 10
            if limit <= 0:
                raise ValueError
            limit = min(limit, 50)
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be a positive integer (max 50)")

        try:
            summary_row = db.query_one(
                """
                SELECT COUNT(*) AS user_count,
                       SUM(total_seconds) AS total_seconds,
                       SUM(total_points) AS total_points,
                       MAX(last_update) AS last_update
                FROM voice_stats
                """
            )
            top_time_rows = db.query_all(
                """
                SELECT user_id, total_seconds, total_points, last_update
                FROM voice_stats
                ORDER BY total_seconds DESC, total_points DESC
                LIMIT ?
                """,
                (limit,),
            )
            top_point_rows = db.query_all(
                """
                SELECT user_id, total_seconds, total_points, last_update
                FROM voice_stats
                ORDER BY total_points DESC, total_seconds DESC
                LIMIT ?
                """,
                (limit,),
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to load voice stats: %s", exc)
            raise web.HTTPInternalServerError(text="Voice stats unavailable") from exc

        live_sessions = await self._collect_live_voice_sessions()
        user_ids = set()
        for row in top_time_rows + top_point_rows:
            try:
                uid = row["user_id"]
            except Exception:
                uid = None
            if uid:
                user_ids.add(uid)
        for sess in live_sessions:
            uid = sess.get("user_id")
            if uid:
                user_ids.add(uid)
        name_map = self._resolve_display_names(user_ids)

        def _map_row(row: Any) -> Dict[str, Any]:
            uid = row["user_id"]
            return {
                "user_id": uid,
                "display_name": name_map.get(uid, f"User {uid}"),
                "total_seconds": int(row["total_seconds"] or 0),
                "total_points": int(row["total_points"] or 0),
                "last_update": row["last_update"],
            }

        summary = {
            "tracked_users": int(summary_row["user_count"] or 0) if summary_row else 0,
            "total_seconds": int(summary_row["total_seconds"] or 0) if summary_row else 0,
            "total_points": int(summary_row["total_points"] or 0) if summary_row else 0,
            "last_update": summary_row["last_update"] if summary_row else None,
        }
        if summary["tracked_users"] > 0:
            summary["avg_seconds_per_user"] = summary["total_seconds"] / summary["tracked_users"]
        else:
            summary["avg_seconds_per_user"] = 0

        live_summary = {
            "active_sessions": len(live_sessions),
            "total_seconds": sum(sess.get("duration_seconds", 0) for sess in live_sessions),
        }
        for sess in live_sessions:
            uid = sess.get("user_id")
            if uid:
                sess["display_name"] = name_map.get(uid, f"User {uid}")

        payload = {
            "summary": summary,
            "top_by_time": [_map_row(r) for r in top_time_rows],
            "top_by_points": [_map_row(r) for r in top_point_rows],
            "live": {
                "summary": live_summary,
                "sessions": live_sessions,
            },
        }
        return self._json(payload)

    async def _handle_voice_history(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        range_raw = request.query.get("range")
        top_raw = request.query.get("top")
        mode_raw = request.query.get("mode") or "hour"
        user_raw = request.query.get("user_id")
        try:
            days = int(range_raw) if range_raw else 14
            if days <= 0:
                raise ValueError
            days = min(days, 90)
        except ValueError:
            raise web.HTTPBadRequest(text="range must be a positive integer (days, max 90)")
        try:
            top_limit = int(top_raw) if top_raw else 10
            if top_limit <= 0:
                raise ValueError
            top_limit = min(top_limit, 50)
        except ValueError:
            raise web.HTTPBadRequest(text="top must be a positive integer (max 50)")
        mode = mode_raw.strip().lower()
        if mode not in {"hour", "day", "week", "month"}:
            raise web.HTTPBadRequest(text="mode must be one of hour, day, week, month")
        user_id: Optional[int] = None
        if user_raw:
            try:
                user_id = int(user_raw)
            except ValueError:
                raise web.HTTPBadRequest(text="user_id must be an integer")

        cutoff = f"-{days} day"
        user_filter = user_id

        try:
            daily_rows = db.query_all(
                """
                SELECT date(started_at) AS day,
                       SUM(duration_seconds) AS total_seconds,
                       COUNT(*) AS sessions,
                       COUNT(DISTINCT user_id) AS users
                FROM voice_session_log
                WHERE started_at >= datetime('now', ?)
                GROUP BY date(started_at)
                ORDER BY day DESC
                """,
                (cutoff,),
            )
            top_users_rows = db.query_all(
                """
                SELECT user_id,
                       MAX(display_name) AS display_name,
                       SUM(duration_seconds) AS total_seconds,
                       SUM(points) AS total_points,
                       COUNT(*) AS sessions
                FROM voice_session_log
                WHERE started_at >= datetime('now', ?)
                  AND (? IS NULL OR user_id = ?)
                GROUP BY user_id
                ORDER BY total_seconds DESC, total_points DESC
                LIMIT ?
                """,
                (cutoff, user_filter, user_filter, top_limit),
            )
            hourly_rows = db.query_all(
                """
                WITH grouped AS (
                    SELECT
                        CASE
                            WHEN ? = 'hour' THEN strftime('%H', started_at)
                            WHEN ? = 'day' THEN strftime('%w', started_at)
                            WHEN ? = 'week' THEN strftime('%Y-%W', started_at)
                            ELSE strftime('%Y-%m', started_at)
                        END AS bucket,
                        duration_seconds,
                        COALESCE(peak_users, 0) AS peak_users
                    FROM voice_session_log
                    WHERE started_at >= datetime('now', ?)
                      AND (? IS NULL OR user_id = ?)
                )
                SELECT bucket,
                       SUM(duration_seconds) AS total_seconds,
                       COUNT(*) AS sessions,
                       SUM(peak_users) AS sum_peak
                FROM grouped
                GROUP BY bucket
                ORDER BY bucket
                """,
                (mode, mode, mode, cutoff, user_filter, user_filter),
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Failed to load voice history: %s", exc)
            raise web.HTTPInternalServerError(text="Voice history unavailable") from exc

        user_ids: set[int] = set()
        for row in top_users_rows:
            try:
                uid = row["user_id"]
            except Exception:
                uid = None
            if uid:
                user_ids.add(uid)
        if user_id:
            user_ids.add(user_id)
        name_map = self._resolve_display_names(user_ids)

        def _map_top_user(row: Any) -> Dict[str, Any]:
            uid = row["user_id"]
            return {
                "user_id": uid,
                "display_name": row["display_name"] or name_map.get(uid, f"User {uid}"),
                "total_seconds": int(row["total_seconds"] or 0),
                "total_points": int(row["total_points"] or 0),
                "sessions": int(row["sessions"] or 0),
            }

        daily = [
            {
                "day": row["day"],
                "total_seconds": int(row["total_seconds"] or 0),
                "sessions": int(row["sessions"] or 0),
                "users": int(row["users"] or 0),
            }
            for row in daily_rows
        ]

        buckets = []
        for row in hourly_rows:
            sessions_count = int(row["sessions"] or 0)
            buckets.append(
                {
                    "label": row["bucket"],
                    "total_seconds": int(row["total_seconds"] or 0),
                    "sessions": sessions_count,
                    "avg_peak": (
                        (int(row["sum_peak"] or 0) / sessions_count)
                        if sessions_count > 0
                        else 0
                    ),
                }
            )

        if mode == "hour":
            existing = {b["label"]: b for b in buckets}
            buckets = []
            for h in range(24):
                key = str(h).zfill(2)
                buckets.append(
                    existing.get(
                        key,
                        {"label": key, "total_seconds": 0, "sessions": 0, "avg_peak": 0},
                    )
                )

        if mode == "day":
            existing = {b["label"]: b for b in buckets}
            buckets = []
            weekdays = ["Sonntag", "Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag"]
            for day in range(7):
                key = str(day)
                data = existing.get(key, {})
                buckets.append({
                    "label": weekdays[day],
                    "total_seconds": data.get("total_seconds", 0),
                    "sessions": data.get("sessions", 0),
                    "avg_peak": data.get("avg_peak", 0),
                })

        user_summary: Optional[Dict[str, Any]] = None
        if user_id is not None:
            try:
                range_stats = db.query_one(
                    """
                    SELECT SUM(duration_seconds) AS total_seconds,
                           SUM(points) AS total_points,
                           COUNT(*) AS sessions,
                           SUM(COALESCE(peak_users, 0)) AS sum_peak,
                           COUNT(DISTINCT date(started_at)) AS active_days,
                           MAX(ended_at) AS last_session
                    FROM voice_session_log
                    WHERE started_at >= datetime('now', ?)
                      AND (? IS NULL OR user_id = ?)
                    """,
                    (cutoff, user_filter, user_filter),
                )
                lifetime_stats = db.query_one(
                    """
                    SELECT total_seconds, total_points, last_update
                    FROM voice_stats
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )
                lifetime_sessions_row = db.query_one(
                    """
                    SELECT COUNT(*) AS sessions, MAX(ended_at) AS last_session
                    FROM voice_session_log
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )
            except Exception as exc:  # noqa: BLE001
                logging.exception("Failed to build user voice summary: %s", exc)
                raise web.HTTPInternalServerError(text="Voice history unavailable") from exc

            range_seconds = int(range_stats["total_seconds"] or 0) if range_stats else 0
            range_points = int(range_stats["total_points"] or 0) if range_stats else 0
            range_sessions = int(range_stats["sessions"] or 0) if range_stats else 0
            range_avg_session = (range_seconds / range_sessions) if range_sessions else 0
            range_avg_peak = (
                (int(range_stats["sum_peak"] or 0) / range_sessions) if range_sessions else 0
            )
            range_days = int(range_stats["active_days"] or 0) if range_stats else 0

            lifetime_seconds = int(lifetime_stats["total_seconds"] or 0) if lifetime_stats else 0
            lifetime_points = int(lifetime_stats["total_points"] or 0) if lifetime_stats else 0
            lifetime_last_update = lifetime_stats["last_update"] if lifetime_stats else None
            lifetime_sessions = (
                int(lifetime_sessions_row["sessions"] or 0) if lifetime_sessions_row else 0
            )
            last_session = None
            if range_stats:
                last_session = range_stats["last_session"]
            if not last_session and lifetime_sessions_row:
                last_session = lifetime_sessions_row["last_session"]

            user_summary = {
                "user_id": user_id,
                "display_name": name_map.get(user_id, f"User {user_id}"),
                "range_seconds": range_seconds,
                "range_points": range_points,
                "range_sessions": range_sessions,
                "range_days": range_days,
                "range_avg_session_seconds": range_avg_session,
                "range_avg_peak": range_avg_peak,
                "lifetime_seconds": lifetime_seconds,
                "lifetime_points": lifetime_points,
                "lifetime_sessions": lifetime_sessions,
                "lifetime_last_update": lifetime_last_update,
                "last_session": last_session,
            }

        payload = {
            "range_days": days,
            "mode": mode,
            "user": (
                {"user_id": user_id, "display_name": name_map.get(user_id, f"User {user_id}")}
                if user_id
                else None
            ),
            "daily": daily,
            "top_users": [_map_top_user(r) for r in top_users_rows],
            "buckets": buckets,
            "user_summary": user_summary,
        }
        return self._json(payload)

    async def _handle_user_retention(self, request: web.Request) -> web.Response:
        """
        Liefert Kennzahlen für den User-Retention-Cog.
        Nutzt die gleichen Default-Schwellen wie im Cog (siehe RetentionConfig in cogs/user_retention.py).
        """
        self._check_auth(request, required=bool(self.token))

        # Defaults aus RetentionConfig
        min_weekly_sessions = 0.5
        min_total_active_days = 3
        inactivity_threshold_days = 14
        min_days_between_messages = 30
        max_miss_you_per_user = 1
        excluded_roles = self._retention_excluded_roles()

        try:
            # Ermittele vorhandene Spalten, um kompatibel mit evtl. aelterem Schema zu sein
            retention_columns = set()
            try:
                rows = db.query_all("PRAGMA table_info(user_retention_tracking)")
                for r in rows:
                    # sqlite3.Row oder tuple
                    name = r["name"] if hasattr(r, "__getitem__") else r[1]
                    retention_columns.add(str(name))
            except Exception:  # pragma: no cover - defensive
                retention_columns = set()

            total_tracked_row = db.query_one(
                "SELECT COUNT(*) FROM user_retention_tracking"
            )
            total_tracked = total_tracked_row[0] if total_tracked_row else 0

            opted_out_row = db.query_one(
                "SELECT COUNT(*) FROM user_retention_tracking WHERE opted_out = 1"
            )
            opted_out = opted_out_row[0] if opted_out_row else 0

            regular_active_row = db.query_one(
                """
                SELECT COUNT(*)
                FROM user_retention_tracking
                WHERE avg_weekly_sessions >= ? AND total_active_days >= ?
                """,
                (min_weekly_sessions, min_total_active_days),
            )
            regular_active = regular_active_row[0] if regular_active_row else 0

            candidate_where = [
                "avg_weekly_sessions >= ?",
                "total_active_days >= ?",
                "(strftime('%s','now') - last_active_at) / 86400 >= ?",
                "opted_out = 0",
            ]
            candidate_params: list[Any] = [
                min_weekly_sessions,
                min_total_active_days,
                inactivity_threshold_days,
            ]

            has_last_sent = "last_miss_you_sent_at" in retention_columns or "last_miss_you_at" in retention_columns
            has_miss_count = "miss_you_count" in retention_columns or "miss_you_sent" in retention_columns

            if has_last_sent:
                candidate_where.append(
                    "(last_miss_you_sent_at IS NULL OR (strftime('%s','now') - last_miss_you_sent_at) / 86400 >= ?)"
                    if "last_miss_you_sent_at" in retention_columns
                    else "(last_miss_you_at IS NULL OR (strftime('%s','now') - last_miss_you_at) / 86400 >= ?)"
                )
                candidate_params.append(min_days_between_messages)
            if has_miss_count:
                candidate_where.append(
                    "(miss_you_count IS NULL OR miss_you_count < ?)"
                    if "miss_you_count" in retention_columns
                    else "(miss_you_sent IS NULL OR miss_you_sent < ?)"
                )
                candidate_params.append(max_miss_you_per_user)

            candidate_where_sql = " AND ".join(candidate_where)

            miss_you_row = db.query_one(
                "SELECT COUNT(*) FROM user_retention_messages WHERE message_type = 'miss_you'"
            )
            miss_you_sent = miss_you_row[0] if miss_you_row else 0

            feedback_row = db.query_one(
                "SELECT COUNT(*) FROM user_retention_messages WHERE message_type = 'feedback'"
            )
            feedback_received = feedback_row[0] if feedback_row else 0

            select_fields = [
                "urt.user_id",
                "urt.guild_id",
                "urt.last_active_at",
                "urt.total_active_days",
                "urt.avg_weekly_sessions",
                "(strftime('%s','now') - urt.last_active_at) / 86400 AS days_inactive",
                """
                (
                    SELECT m.delivery_status
                    FROM user_retention_messages m
                    WHERE m.user_id = urt.user_id AND m.message_type = 'miss_you'
                    ORDER BY m.sent_at DESC
                    LIMIT 1
                ) AS last_message_status
                """,
                """
                (
                    SELECT m.sent_at
                    FROM user_retention_messages m
                    WHERE m.user_id = urt.user_id AND m.message_type = 'miss_you'
                    ORDER BY m.sent_at DESC
                    LIMIT 1
                ) AS last_message_at
                """,
            ]

            if "last_miss_you_sent_at" in retention_columns:
                select_fields.append("urt.last_miss_you_sent_at")
            elif "last_miss_you_at" in retention_columns:
                select_fields.append("urt.last_miss_you_at AS last_miss_you_sent_at")
            else:
                select_fields.append("NULL AS last_miss_you_sent_at")

            if "miss_you_count" in retention_columns:
                select_fields.append("urt.miss_you_count")
            elif "miss_you_sent" in retention_columns:
                select_fields.append("urt.miss_you_sent AS miss_you_count")
            else:
                select_fields.append("NULL AS miss_you_count")

            candidate_select_sql = ", ".join(select_fields)
            candidate_sql = (
                "SELECT " + candidate_select_sql + "\n"  # nosec B608
                "FROM user_retention_tracking urt\n"
                "WHERE " + candidate_where_sql + "\n"
                "ORDER BY days_inactive DESC"
            )
            candidate_rows_raw = db.query_all(candidate_sql, tuple(candidate_params))

            filtered_rows = [
                row
                for row in candidate_rows_raw
                if not self._has_retention_excluded_role(
                    row["user_id"],
                    row["guild_id"],
                    excluded_roles,
                )
            ]
            inactive_candidates = len(filtered_rows)
            candidate_rows = filtered_rows[:50]

            user_ids = [row["user_id"] for row in candidate_rows if row and row["user_id"]]
            name_map = self._resolve_display_names(user_ids)

            payload = {
                "summary": {
                    "total_tracked": total_tracked,
                    "opted_out": opted_out,
                    "regular_active": regular_active,
                    "inactive_candidates": inactive_candidates,
                    "miss_you_sent": miss_you_sent,
                    "feedback_received": feedback_received,
                },
                "candidates": [
                    {
                        "display_name": (
                            name_map.get(row["user_id"])
                            if name_map.get(row["user_id"])
                            else f"User {row['user_id']}"
                        ),
                        "user_id": row["user_id"],
                        "guild_id": row["guild_id"],
                        "last_active_at": row["last_active_at"],
                        "days_inactive": max(0, int(row["days_inactive"] or 0)),
                        "total_active_days": row["total_active_days"],
                        "avg_weekly_sessions": row["avg_weekly_sessions"],
                        "last_miss_you_sent_at": row["last_miss_you_sent_at"],
                        "miss_you_count": row["miss_you_count"],
                        "last_message_status": row["last_message_status"],
                        "last_message_at": row["last_message_at"],
                    }
                    for row in candidate_rows
                ],
                # legacy key, im UI jetzt als Kandidatenliste genutzt
                "recent": [
                    {
                        "display_name": (
                            name_map.get(row["user_id"])
                            if name_map.get(row["user_id"])
                            else f"User {row['user_id']}"
                        ),
                        "user_id": row["user_id"],
                        "guild_id": row["guild_id"],
                        "last_active_at": row["last_active_at"],
                        "days_inactive": max(0, int(row["days_inactive"] or 0)),
                        "total_active_days": row["total_active_days"],
                        "avg_weekly_sessions": row["avg_weekly_sessions"],
                        "last_miss_you_sent_at": row["last_miss_you_sent_at"],
                        "miss_you_count": row["miss_you_count"],
                        "last_message_status": row["last_message_status"],
                        "last_message_at": row["last_message_at"],
                    }
                    for row in candidate_rows
                ],
            }
            return self._json(payload)

        except Exception as e:
            logger.error("Error building user retention payload: %s", e, exc_info=True)
            raise web.HTTPInternalServerError(text="Failed to load user retention data")

    async def _handle_member_events(self, request: web.Request) -> web.Response:
        """Handler für Member-Events (Joins, Leaves, Bans)."""
        self._check_auth(request, required=bool(self.token))

        raw_limit = request.query.get("limit")
        event_type = request.query.get("type")  # optional filter
        guild_id_raw = request.query.get("guild_id")

        try:
            limit = int(raw_limit) if raw_limit else 50
            if limit <= 0:
                raise ValueError
            limit = min(limit, 200)
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be a positive integer (max 200)")

        guild_id: Optional[int] = None
        if guild_id_raw:
            try:
                guild_id = int(guild_id_raw)
            except ValueError:
                raise web.HTTPBadRequest(text="guild_id must be an integer")

        try:
            guild_filter = guild_id if guild_id else None
            event_filter = (event_type or "").strip() or None

            # Hole Events
            events = db.query_all(
                """
                SELECT id, user_id, guild_id, event_type, timestamp,
                       display_name, account_created_at, join_position, metadata
                FROM member_events
                WHERE (? IS NULL OR guild_id = ?)
                  AND (? IS NULL OR event_type = ?)
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (guild_filter, guild_filter, event_filter, event_filter, limit),
            )

            # Event-Type Counts
            event_counts = db.query_all(
                """
                SELECT event_type, COUNT(*) as count
                FROM member_events
                WHERE (? IS NULL OR guild_id = ?)
                  AND (? IS NULL OR event_type = ?)
                GROUP BY event_type
                ORDER BY count DESC
                """,
                (guild_filter, guild_filter, event_filter, event_filter),
            )

            # Recent Joins (letzten 7 Tage)
            recent_joins = db.query_one(
                """
                SELECT COUNT(*) as count
                FROM member_events
                WHERE event_type = 'join'
                  AND timestamp >= datetime('now', '-7 days')
                  AND (? IS NULL OR guild_id = ?)
                """,
                (guild_filter, guild_filter),
            )

            # Recent Leaves (letzten 7 Tage)
            recent_leaves = db.query_one(
                """
                SELECT COUNT(*) as count
                FROM member_events
                WHERE event_type = 'leave'
                  AND timestamp >= datetime('now', '-7 days')
                  AND (? IS NULL OR guild_id = ?)
                """,
                (guild_filter, guild_filter),
            )

            events_list = []
            for row in events:
                events_list.append({
                    "id": row[0],
                    "user_id": row[1],
                    "guild_id": row[2],
                    "event_type": row[3],
                    "timestamp": row[4],
                    "display_name": row[5],
                    "account_created_at": row[6],
                    "join_position": row[7],
                    "metadata": row[8],
                })

            counts = {row[0]: row[1] for row in event_counts}

            payload = {
                "events": events_list,
                "summary": {
                    "total_events": len(events_list),
                    "event_counts": counts,
                    "recent_joins_7d": recent_joins[0] if recent_joins else 0,
                    "recent_leaves_7d": recent_leaves[0] if recent_leaves else 0,
                },
            }
            return self._json(payload)

        except Exception as exc:
            logging.exception("Failed to load member events: %s", exc)
            raise web.HTTPInternalServerError(text="Member events unavailable") from exc

    async def _handle_message_activity(self, request: web.Request) -> web.Response:
        """Handler für Message-Activity."""
        self._check_auth(request, required=bool(self.token))

        raw_limit = request.query.get("limit")
        guild_id_raw = request.query.get("guild_id")

        try:
            limit = int(raw_limit) if raw_limit else 20
            if limit <= 0:
                raise ValueError
            limit = min(limit, 100)
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be a positive integer (max 100)")

        guild_id: Optional[int] = None
        if guild_id_raw:
            try:
                guild_id = int(guild_id_raw)
            except ValueError:
                raise web.HTTPBadRequest(text="guild_id must be an integer")

        try:
            guild_filter = guild_id if guild_id else None

            # Top Users by Message Count
            top_users = db.query_all(
                """
                SELECT user_id, guild_id, channel_id, message_count,
                       last_message_at, first_message_at
                FROM message_activity
                WHERE (? IS NULL OR guild_id = ?)
                ORDER BY message_count DESC
                LIMIT ?
                """,
                (guild_filter, guild_filter, limit),
            )

            # Summary
            summary = db.query_one(
                """
                SELECT
                    COUNT(*) as total_users,
                    SUM(message_count) as total_messages,
                    AVG(message_count) as avg_per_user
                FROM message_activity
                WHERE (? IS NULL OR guild_id = ?)
                """,
                (guild_filter, guild_filter),
            )

            # Resolve display names
            user_ids = {row[0] for row in top_users}
            name_map = self._resolve_display_names(user_ids)

            users_list = []
            for row in top_users:
                user_id = row[0]
                users_list.append({
                    "user_id": user_id,
                    "display_name": name_map.get(user_id, f"User {user_id}"),
                    "guild_id": row[1],
                    "channel_id": row[2],
                    "message_count": row[3],
                    "last_message_at": row[4],
                    "first_message_at": row[5],
                })

            payload = {
                "top_users": users_list,
                "summary": {
                    "total_users": summary[0] if summary else 0,
                    "total_messages": summary[1] if summary else 0,
                    "avg_per_user": round(summary[2], 1) if summary and summary[2] else 0,
                },
            }
            return self._json(payload)

        except Exception as exc:
            logging.exception("Failed to load message activity: %s", exc)
            raise web.HTTPInternalServerError(text="Message activity unavailable") from exc

    async def _handle_co_player_network(self, request: web.Request) -> web.Response:
        """Aggregiertes Co-Player-Netzwerk mit persistierten Anzeigenamen."""
        self._check_auth(request, required=bool(self.token))

        raw_limit = request.query.get("limit")
        raw_min_sessions = request.query.get("min_sessions")

        try:
            limit = int(raw_limit) if raw_limit else 120
            if limit <= 0:
                raise ValueError
            limit = min(limit, 400)
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be a positive integer (max 400)")

        try:
            min_sessions = int(raw_min_sessions) if raw_min_sessions else 1
            if min_sessions <= 0:
                raise ValueError
            min_sessions = min(min_sessions, 100000)
        except ValueError:
            raise web.HTTPBadRequest(text="min_sessions must be a positive integer")

        try:
            rows = db.query_all(
                """
                SELECT user_id, co_player_id, sessions_together, total_minutes_together,
                       last_played_together, user_display_name, co_player_display_name
                FROM user_co_players
                WHERE sessions_together >= ?
                ORDER BY sessions_together DESC, total_minutes_together DESC, last_played_together DESC
                LIMIT ?
                """,
                (min_sessions, limit * 2),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to load co-player network: %s", exc)
            raise web.HTTPInternalServerError(text="Co-player network unavailable") from exc

        def _ts(value: Any) -> float:
            if value is None:
                return 0.0
            if isinstance(value, (int, float)):
                return float(value)
            try:
                normalized = str(value).replace("T", " ").replace("Z", "")
                return _dt.datetime.fromisoformat(normalized).timestamp()
            except Exception:
                return 0.0

        edges: Dict[Tuple[int, int], Dict[str, Any]] = {}
        name_map: Dict[int, str] = {}
        missing_names: Set[int] = set()

        for row in rows:
            try:
                uid = int(row["user_id"])
                coid = int(row["co_player_id"])
            except Exception:
                continue
            if uid == coid:
                continue
            sessions = int(row["sessions_together"] or 0)
            minutes = int(row["total_minutes_together"] or 0)
            last_played = row["last_played_together"]
            key = (uid, coid) if uid < coid else (coid, uid)
            edge = edges.get(key)
            ts_value = _ts(last_played)
            if edge is None:
                edges[key] = {
                    "source": key[0],
                    "target": key[1],
                    "sessions": sessions,
                    "minutes": minutes,
                    "last_played": last_played,
                    "last_played_ts": ts_value,
                }
            else:
                edge["sessions"] = max(edge["sessions"], sessions)
                edge["minutes"] = max(edge["minutes"], minutes)
                if ts_value > edge.get("last_played_ts", 0):
                    edge["last_played_ts"] = ts_value
                    edge["last_played"] = last_played

            user_name = row["user_display_name"]
            co_name = row["co_player_display_name"]
            if user_name:
                name_map[uid] = user_name
            else:
                missing_names.add(uid)
            if co_name:
                name_map[coid] = co_name
            else:
                missing_names.add(coid)

        if missing_names:
            resolved = self._resolve_display_names(missing_names)
            for uid, name in resolved.items():
                if name:
                    name_map[uid] = name

        updates: List[Tuple[Optional[str], Optional[str], int, int]] = []
        for row in rows:
            try:
                uid = int(row["user_id"])
                coid = int(row["co_player_id"])
            except Exception:
                continue
            new_user_name = name_map.get(uid)
            new_co_name = name_map.get(coid)
            if (not row["user_display_name"] and new_user_name) or (
                not row["co_player_display_name"] and new_co_name
            ):
                updates.append((new_user_name, new_co_name, uid, coid))

        if updates:
            try:
                db.executemany(
                    """
                    UPDATE user_co_players
                    SET user_display_name = COALESCE(?, user_display_name),
                        co_player_display_name = COALESCE(?, co_player_display_name)
                    WHERE user_id = ? AND co_player_id = ?
                    """,
                    updates,
                )
            except Exception:
                logger.debug("Could not persist co-player display names", exc_info=True)

        edge_values = sorted(edges.values(), key=lambda e: (e["sessions"], e["minutes"]), reverse=True)
        trimmed_edges = edge_values[:limit]

        nodes: Dict[int, Dict[str, Any]] = {}
        for edge in trimmed_edges:
            source = edge["source"]
            target = edge["target"]
            for node_id in (source, target):
                if node_id not in nodes:
                    nodes[node_id] = {
                        "id": node_id,
                        "name": name_map.get(node_id, f"User {node_id}"),
                        "sessions": 0,
                        "minutes": 0,
                        "degree": 0,
                    }
                nodes[node_id]["degree"] += 1

            nodes[source]["sessions"] += edge["sessions"]
            nodes[target]["sessions"] += edge["sessions"]
            nodes[source]["minutes"] += edge["minutes"]
            nodes[target]["minutes"] += edge["minutes"]

        for node in nodes.values():
            node["weight"] = max(node["sessions"], node["minutes"] // 10)

        links = [
            {
                "source": edge["source"],
                "target": edge["target"],
                "sessions": edge["sessions"],
                "minutes": edge["minutes"],
                "last_played": edge.get("last_played"),
            }
            for edge in trimmed_edges
        ]

        payload = {
            "nodes": sorted(nodes.values(), key=lambda n: n["sessions"], reverse=True),
            "links": links,
            "meta": {
                "total_edges": len(edges),
                "returned_edges": len(links),
                "total_nodes": len(nodes),
                "generated_at": _dt.datetime.utcnow().isoformat() + "Z",
                "min_sessions": min_sessions,
            },
        }
        return self._json(payload)

    def _collect_public_vanity_links(self, guild_filter: Optional[int]) -> List[Dict[str, Any]]:
        links: List[Dict[str, Any]] = []
        for guild in self.bot.guilds:
            if guild_filter is not None and int(guild.id) != int(guild_filter):
                continue
            code = str(getattr(guild, "vanity_url_code", "") or "").strip()
            if not code:
                continue
            links.append(
                {
                    "guild_id": int(guild.id),
                    "guild_name": getattr(guild, "name", None),
                    "type": "vanity",
                    "label": "Vanity-Link",
                    "code": code,
                    "url": f"https://discord.gg/{code}",
                }
            )
        links.sort(key=lambda item: str(item.get("guild_name") or "").lower())
        return links

    def _build_member_source_analytics(
        self,
        guild_filter: Optional[int],
        *,
        days: int = 30,
    ) -> Dict[str, Any]:
        window_days = max(1, min(int(days), 365))
        cutoff_expr = f"-{window_days} days"

        join_rows = db.query_all(
            """
            SELECT id, user_id, guild_id, timestamp, display_name, metadata
            FROM member_events
            WHERE event_type = 'join'
              AND timestamp >= datetime('now', ?)
              AND (? IS NULL OR guild_id = ?)
            ORDER BY timestamp DESC
            """,
            (cutoff_expr, guild_filter, guild_filter),
        )

        twitch_invite_lookup: Dict[str, str] = {}
        twitch_assigned_links: List[Dict[str, Any]] = []
        try:
            twitch_rows = db.query_all(
                """
                SELECT streamer_login, invite_code, invite_url, created_at, last_sent_at
                FROM twitch_streamer_invites
                WHERE (? IS NULL OR guild_id = ?)
                ORDER BY streamer_login ASC
                """,
                (guild_filter, guild_filter),
            )
            for row in twitch_rows:
                streamer_login = str(row[0] or "").strip().lower()
                invite_code = str(row[1] or "").strip()
                invite_url = str(row[2] or "").strip()
                created_at = row[3]
                last_sent_at = row[4]

                if invite_code and streamer_login and invite_code.lower() not in twitch_invite_lookup:
                    twitch_invite_lookup[invite_code.lower()] = streamer_login

                if streamer_login or invite_code or invite_url:
                    twitch_assigned_links.append(
                        {
                            "streamer_login": streamer_login or None,
                            "invite_code": invite_code or None,
                            "invite_url": invite_url or (f"https://discord.gg/{invite_code}" if invite_code else None),
                            "created_at": created_at,
                            "last_sent_at": last_sent_at,
                        }
                    )
        except sqlite3.OperationalError:
            # Tabelle existiert nur wenn Twitch-Schema initialisiert wurde.
            pass
        except Exception:
            logger.debug("Failed to load twitch invite assignments", exc_info=True)

        bucket_counts: Dict[str, int] = {
            "public": 0,
            "twitch": 0,
            "personal": 0,
            "unknown": 0,
        }
        public_groups: Dict[str, Dict[str, Any]] = {
            "server_discovery": {"kind": "server_discovery", "label": "Server entdecken", "count": 0},
            "vanity": {"kind": "vanity", "label": "Vanity-Link", "count": 0},
            "other": {"kind": "other", "label": "Public (Sonstige)", "count": 0},
        }
        twitch_groups: Dict[str, Dict[str, Any]] = {}
        personal_groups: Dict[str, Dict[str, Any]] = {}
        recent: List[Dict[str, Any]] = []
        backfill_updates: List[Tuple[str, int]] = []

        for row in join_rows:
            event_id = self._coerce_int(row[0], None)
            user_id = self._coerce_int(row[1], 0) or 0
            timestamp = row[3]
            display_name = row[4] or f"User {user_id}"
            metadata = self._parse_metadata_json(row[5])
            metadata_changed = False

            bucket_raw = str(metadata.get("join_source_bucket") or "").strip().lower()
            kind_raw = str(metadata.get("join_source_kind") or metadata.get("join_source_type") or "").strip().lower()
            label_raw = str(metadata.get("join_source_label") or "").strip()
            invite_code = str(metadata.get("invite_code") or "").strip()
            invite_url = str(metadata.get("invite_url") or "").strip()
            if not invite_url and invite_code:
                invite_url = f"https://discord.gg/{invite_code}"
                metadata["invite_url"] = invite_url
                metadata_changed = True
            twitch_login = str(metadata.get("twitch_streamer_login") or "").strip().lower()
            if not twitch_login and invite_code:
                twitch_login = twitch_invite_lookup.get(invite_code.lower(), "")
                if twitch_login:
                    metadata["twitch_streamer_login"] = twitch_login
                    metadata_changed = True

            bucket = bucket_raw
            if bucket not in bucket_counts:
                if twitch_login:
                    bucket = "twitch"
                elif kind_raw in {"server_discovery", "discovery", "public_discovery", "vanity", "vanity_url"}:
                    bucket = "public"
                elif invite_code or kind_raw in {"invite_link", "personal", "personal_invite"}:
                    bucket = "personal"
                else:
                    bucket = "unknown"
                metadata["join_source_bucket"] = bucket
                metadata_changed = True

            bucket_counts[bucket] += 1

            public_kind: Optional[str] = None
            personal_label: Optional[str] = None
            if bucket == "public":
                if kind_raw in {"server_discovery", "discovery", "public_discovery"}:
                    public_kind = "server_discovery"
                elif kind_raw in {"vanity", "vanity_url", "public_vanity"}:
                    public_kind = "vanity"
                else:
                    public_kind = "other"
                public_groups[public_kind]["count"] += 1

            if not kind_raw:
                if bucket == "twitch":
                    kind_raw = "twitch_streamer"
                elif bucket == "personal":
                    kind_raw = "invite_link"
                elif bucket == "public":
                    if public_kind == "server_discovery":
                        kind_raw = "server_discovery"
                    elif public_kind == "vanity":
                        kind_raw = "vanity"
                    else:
                        kind_raw = "public_other"
                else:
                    kind_raw = "unknown"
                metadata["join_source_kind"] = kind_raw
                metadata_changed = True

            if bucket == "twitch":
                key = twitch_login or invite_code.lower() or "unknown"
                label = twitch_login or (f"Invite {invite_code}" if invite_code else "Unbekannt")
                entry = twitch_groups.get(key)
                if entry is None:
                    entry = {
                        "streamer_login": twitch_login or None,
                        "label": label,
                        "invite_code": invite_code or None,
                        "invite_url": invite_url or None,
                        "count": 0,
                    }
                    twitch_groups[key] = entry
                entry["count"] += 1
                if invite_code and not entry.get("invite_code"):
                    entry["invite_code"] = invite_code
                if invite_url and not entry.get("invite_url"):
                    entry["invite_url"] = invite_url

            if bucket == "personal":
                inviter_id = self._coerce_int(metadata.get("inviter_id"), None)
                inviter_name = str(metadata.get("inviter_name") or "").strip()
                if inviter_id is not None:
                    key = f"id:{inviter_id}"
                    personal_label = inviter_name or f"User {inviter_id}"
                elif inviter_name:
                    key = f"name:{inviter_name.lower()}"
                    personal_label = inviter_name
                elif invite_code:
                    key = f"code:{invite_code.lower()}"
                    personal_label = f"Invite {invite_code}"
                else:
                    key = "other"
                    personal_label = "Sonstige Invite-Links"

                entry = personal_groups.get(key)
                if entry is None:
                    entry = {
                        "label": personal_label,
                        "inviter_id": inviter_id,
                        "invite_code": invite_code or None,
                        "invite_url": invite_url or None,
                        "count": 0,
                    }
                    personal_groups[key] = entry
                entry["count"] += 1
                if invite_code and not entry.get("invite_code"):
                    entry["invite_code"] = invite_code
                if invite_url and not entry.get("invite_url"):
                    entry["invite_url"] = invite_url

            source_label = label_raw
            if not source_label:
                if bucket == "public":
                    if public_kind == "server_discovery":
                        source_label = "Public: Server entdecken"
                    elif public_kind == "vanity":
                        source_label = "Public: Vanity-Link"
                    else:
                        source_label = "Public"
                elif bucket == "twitch":
                    source_label = f"Twitch: {twitch_login}" if twitch_login else "Twitch"
                elif bucket == "personal":
                    source_label = f"Persoenlich: {personal_label or 'Invite-Link'}"
                else:
                    source_label = "Unbekannt"

            if not label_raw:
                metadata["join_source_label"] = source_label
                metadata_changed = True

            if not str(metadata.get("join_source_confidence") or "").strip():
                confidence = "high"
                if bucket == "unknown":
                    confidence = "low"
                elif bucket == "public" and kind_raw in {"server_discovery", "discovery", "public_discovery"}:
                    confidence = "medium"
                metadata["join_source_confidence"] = confidence
                metadata_changed = True

            if not str(metadata.get("join_source_detected_at") or "").strip():
                metadata["join_source_detected_at"] = timestamp or _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                metadata_changed = True

            if metadata_changed and event_id is not None:
                backfill_updates.append((json.dumps(metadata, separators=(",", ":")), int(event_id)))

            recent.append(
                {
                    "user_id": user_id,
                    "display_name": display_name,
                    "timestamp": timestamp,
                    "bucket": bucket,
                    "label": source_label,
                    "invite_code": invite_code or None,
                    "invite_url": invite_url or None,
                    "twitch_streamer_login": twitch_login or None,
                }
            )

        if backfill_updates:
            try:
                db.executemany(
                    "UPDATE member_events SET metadata = ? WHERE id = ?",
                    backfill_updates,
                )
                logger.info("Member-source metadata backfilled for %d join event(s)", len(backfill_updates))
            except Exception:
                logger.debug("Failed to persist member-source metadata backfill", exc_info=True)

        public_breakdown = [entry for entry in public_groups.values() if int(entry.get("count", 0)) > 0]
        twitch_breakdown = sorted(
            twitch_groups.values(),
            key=lambda item: (-int(item.get("count", 0)), str(item.get("label") or "").lower()),
        )
        personal_breakdown = sorted(
            personal_groups.values(),
            key=lambda item: (-int(item.get("count", 0)), str(item.get("label") or "").lower()),
        )
        twitch_assigned_links = sorted(
            twitch_assigned_links,
            key=lambda item: (
                str(item.get("streamer_login") or "").lower(),
                str(item.get("invite_code") or "").lower(),
            ),
        )

        known_joins = bucket_counts["public"] + bucket_counts["twitch"] + bucket_counts["personal"]
        unknown_joins = bucket_counts["unknown"]

        return {
            "window_days": window_days,
            "tracked_joins": len(join_rows),
            "known_joins": known_joins,
            "unknown_joins": unknown_joins,
            "bucket_counts": bucket_counts,
            "bucket_labels": {
                "public": "Public",
                "twitch": "Twitch",
                "personal": "Persoenlich/Sonstige",
                "unknown": "Unbekannt",
            },
            "public_breakdown": public_breakdown,
            "public_links": self._collect_public_vanity_links(guild_filter),
            "twitch_breakdown": twitch_breakdown,
            "twitch_assigned_links": twitch_assigned_links,
            "personal_breakdown": personal_breakdown,
            "recent": recent[:20],
        }

    async def _handle_server_stats(self, request: web.Request) -> web.Response:
        """Handler für aggregierte Server-Statistiken."""
        self._check_auth(request, required=bool(self.token))

        guild_id_raw = request.query.get("guild_id")

        guild_id: Optional[int] = None
        if guild_id_raw:
            try:
                guild_id = int(guild_id_raw)
            except ValueError:
                raise web.HTTPBadRequest(text="guild_id must be an integer")

        try:
            guild_filter = guild_id if guild_id else None

            # Member Events Summary
            member_events_summary = db.query_all(
                """
                SELECT event_type, COUNT(*) as count
                FROM member_events
                WHERE (? IS NULL OR guild_id = ?)
                GROUP BY event_type
                """,
                (guild_filter, guild_filter),
            )

            # Message Activity Summary
            message_summary = db.query_one(
                """
                SELECT SUM(message_count) as total
                FROM message_activity
                WHERE (? IS NULL OR guild_id = ?)
                """,
                (guild_filter, guild_filter),
            )

            # Voice Activity Summary
            voice_summary = db.query_one(
                """
                SELECT SUM(duration_seconds) as total_seconds
                FROM voice_session_log
                WHERE (? IS NULL OR guild_id = ?)
                """,
                (guild_filter, guild_filter),
            )

            # Active Users (last 7 days)
            active_users_7d = db.query_one(
                """
                SELECT COUNT(DISTINCT user_id) as count
                FROM message_activity
                WHERE (? IS NULL OR guild_id = ?)
                  AND last_message_at >= datetime('now', '-7 days')
                """,
                (guild_filter, guild_filter),
            )

            # Growth (Joins vs Leaves last 30 days)
            growth = db.query_one(
                """
                SELECT
                    SUM(CASE WHEN event_type = 'join' THEN 1 ELSE 0 END) as joins,
                    SUM(CASE WHEN event_type = 'leave' THEN 1 ELSE 0 END) as leaves
                FROM member_events
                WHERE (? IS NULL OR guild_id = ?)
                  AND timestamp >= datetime('now', '-30 days')
                """,
                (guild_filter, guild_filter),
            )

            member_sources_30d = self._build_member_source_analytics(guild_filter, days=30)

            payload = {
                "member_events": {row[0]: row[1] for row in member_events_summary},
                "total_messages": message_summary[0] if message_summary and message_summary[0] else 0,
                "total_voice_hours": (voice_summary[0] // 3600) if voice_summary and voice_summary[0] else 0,
                "active_users_7d": active_users_7d[0] if active_users_7d else 0,
                "growth_30d": {
                    "joins": growth[0] if growth else 0,
                    "leaves": growth[1] if growth else 0,
                    "net": (growth[0] or 0) - (growth[1] or 0) if growth else 0,
                },
                "member_sources_30d": member_sources_30d,
            }
            return self._json(payload)

        except Exception as exc:
            logging.exception("Failed to load server stats: %s", exc)
            raise web.HTTPInternalServerError(text="Server stats unavailable") from exc

    def _resolve_tournament_guild_id(self, raw_value: Any) -> int:
        parsed = self._coerce_int(raw_value, None)
        if parsed is not None:
            return int(parsed)
        if self.bot.guilds:
            return int(self.bot.guilds[0].id)
        raise web.HTTPBadRequest(text="guild_id is required")

    async def _tournament_guilds_payload(self) -> List[Dict[str, Any]]:
        from cogs.customgames import tournament_store as tstore

        counts = await tstore.guild_signup_counts_async()
        payload: List[Dict[str, Any]] = []
        known_ids: Set[int] = set()
        for guild in sorted(self.bot.guilds, key=lambda g: (g.name or "").lower()):
            guild_id = int(guild.id)
            known_ids.add(guild_id)
            payload.append(
                {
                    "id": guild_id,
                    "name": guild.name,
                    "signups": int(counts.get(guild_id, 0)),
                }
            )
        for guild_id, signups in sorted(counts.items(), key=lambda item: item[0]):
            gid = int(guild_id)
            if gid in known_ids:
                continue
            payload.append(
                {
                    "id": gid,
                    "name": f"Guild {gid}",
                    "signups": int(signups),
                }
            )
        return payload

    def _decorate_tournament_signups(
        self,
        guild_id: int,
        signups: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        guild = self.bot.get_guild(int(guild_id))
        decorated: List[Dict[str, Any]] = []
        for row in signups:
            item = dict(row)
            user_id = int(item.get("user_id") or 0)
            member = guild.get_member(user_id) if guild else None
            item["display_name"] = member.display_name if member else f"User {user_id}"
            item["mention"] = member.mention if member else None
            rank_key = str(item.get("rank") or "initiate")
            item["rank_label"] = rank_key.capitalize()
            decorated.append(item)
        return decorated

    async def _handle_tournament_overview(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        from cogs.customgames import tournament_store as tstore

        await tstore.ensure_schema_async()
        guild_id = self._resolve_tournament_guild_id(request.query.get("guild_id"))
        teams = await tstore.list_teams_async(guild_id)
        signups = await tstore.list_signups_async(guild_id)
        summary = await tstore.summary_async(guild_id)

        payload = {
            "guild_id": guild_id,
            "guilds": await self._tournament_guilds_payload(),
            "summary": summary,
            "teams": teams,
            "signups": self._decorate_tournament_signups(guild_id, signups),
        }
        return self._json(payload)

    async def _handle_tournament_team_create(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        from cogs.customgames import tournament_store as tstore

        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Payload must be a JSON object")

        await tstore.ensure_schema_async()
        guild_id = self._resolve_tournament_guild_id(payload.get("guild_id"))
        name = str(payload.get("name") or "").strip()
        if not name:
            raise web.HTTPBadRequest(text="'name' is required")
        created_by = self._coerce_int(payload.get("created_by"), None)

        try:
            team = await tstore.get_or_create_team_async(guild_id, name, created_by=created_by)
        except ValueError as exc:
            logger.warning(
                "Tournament team creation rejected: %s",
                self._safe_log_value(exc),
            )
            raise web.HTTPBadRequest(text="Invalid team payload") from exc

        return self._json(
            {
                "ok": True,
                "team": team,
                "created": bool(team.get("created")),
            },
            status=201 if bool(team.get("created")) else 200,
        )

    async def _handle_tournament_assign(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        from cogs.customgames import tournament_store as tstore

        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Payload must be a JSON object")

        await tstore.ensure_schema_async()
        guild_id = self._resolve_tournament_guild_id(payload.get("guild_id"))
        user_id = self._coerce_int(payload.get("user_id"), None)
        if user_id is None:
            raise web.HTTPBadRequest(text="'user_id' must be an integer")

        team_raw = payload.get("team_id")
        if team_raw in (None, ""):
            team_id: Optional[int] = None
        else:
            team_id = self._coerce_int(team_raw, None)
            if team_id is None:
                raise web.HTTPBadRequest(text="'team_id' must be an integer or null")

        try:
            updated = await tstore.assign_signup_team_async(
                guild_id,
                int(user_id),
                team_id=team_id,
            )
        except ValueError as exc:
            logger.warning(
                "Tournament signup assignment rejected: %s",
                self._safe_log_value(exc),
            )
            raise web.HTTPBadRequest(text="Invalid team assignment payload") from exc

        if not updated:
            raise web.HTTPNotFound(text="Signup not found")

        signup = await tstore.get_signup_async(guild_id, int(user_id))
        decorated = self._decorate_tournament_signups(guild_id, [signup]) if signup else []
        return self._json({"ok": True, "signup": decorated[0] if decorated else signup})

    async def _handle_tournament_remove(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        from cogs.customgames import tournament_store as tstore

        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON payload") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Payload must be a JSON object")

        await tstore.ensure_schema_async()
        guild_id = self._resolve_tournament_guild_id(payload.get("guild_id"))
        user_id = self._coerce_int(payload.get("user_id"), None)
        if user_id is None:
            raise web.HTTPBadRequest(text="'user_id' must be an integer")

        removed = await tstore.remove_signup_async(guild_id, int(user_id))
        return self._json({"ok": removed, "removed": removed})

    async def _handle_status(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        auth_session = self._auth_session_for_request(request)

        bot = self.bot
        tz = bot.startup_time.tzinfo
        now = _dt.datetime.now(tz=tz) if tz else _dt.datetime.now()
        uptime_delta = now - bot.startup_time
        uptime = str(uptime_delta).split(".")[0]

        discovered = bot.cogs_list
        status_map = bot.cog_status.copy()
        active = set(bot.active_cogs())

        items: List[Dict[str, Any]] = []
        for cog in discovered:
            status = status_map.get(cog, "loaded" if cog in active else "unloaded")
            items.append(
                {
                    "name": cog,
                    "status": status,
                    "loaded": cog in active,
                    "namespace": self._namespace_for(cog),
                }
            )

        namespaces = self._namespace_summary(discovered)

        latency = getattr(bot, "latency", None)
        if latency is not None and math.isfinite(latency):
            latency_ms = round(latency * 1000, 2)
        else:
            latency_ms = None

        lifecycle_state: Dict[str, Any] | None = None
        lifecycle = self._lifecycle or getattr(bot, "lifecycle", None)
        if lifecycle:
            try:
                lifecycle_state = lifecycle.snapshot()
            except Exception as exc:
                logging.getLogger(__name__).warning("Lifecycle snapshot fehlgeschlagen: %s", exc)
                lifecycle_state = {"enabled": True, "error": str(exc)}

        restart_in_progress = bool(self._restart_task and not self._restart_task.done())
        last_restart = self._last_restart if any(self._last_restart.values()) else None
        csrf_token = self._ensure_session_csrf_token(auth_session) if auth_session else None

        payload = {
            "bot": {
                "user": str(bot.user) if bot.user else None,
                "id": getattr(bot.user, "id", None),
                "uptime": uptime,
                "guilds": len(bot.guilds),
                "latency_ms": latency_ms,
            },
            "cogs": {
                "items": items,
                "active": sorted(active),
                "namespaces": namespaces,
                "discovered": discovered,
                "tree": self._build_tree(),
                "blocked": sorted(self.bot.blocked_namespaces),
            },
            "dashboard": {
                "listen_url": self._listen_base_url,
                "public_url": self._public_base_url,
                "running": self._started,
                "restart_in_progress": restart_in_progress,
                "last_restart": last_restart,
            },
            "lifecycle": lifecycle_state or {"enabled": False},
            "settings": {
                "per_cog_unload_timeout": bot.per_cog_unload_timeout,
            },
            "auth": {
                "enforced": self._is_auth_enforced(),
                "mode": (
                    "discord"
                    if self._discord_auth_required
                    else ("token" if self.token else "none")
                ),
                "discord_enabled": self._discord_auth_required,
                "login_url": self._build_discord_login_url(request, next_path="/admin"),
                "logout_url": "/auth/logout",
                "csrf_token": csrf_token,
                "user": (
                    {
                        "id": auth_session.get("user_id"),
                        "display_name": auth_session.get("display_name"),
                        "username": auth_session.get("username"),
                    }
                    if auth_session
                    else None
                ),
            },
            "health": await self._collect_health_checks(),
            "standalone": await self._collect_standalone_snapshot(),
        }
        return self._json(payload)

    async def _handle_bot_restart(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        safe_remote = self._safe_log_value(request.remote)
        logger.warning("AUDIT master-dashboard bot_restart requested from %s", safe_remote)
        async with self._bot_restart_lock:
            now = time.monotonic()
            since_last = now - self._last_bot_restart_request_monotonic
            if (
                self._last_bot_restart_request_monotonic > 0
                and since_last < self._bot_restart_min_interval_seconds
            ):
                retry_after = max(0.0, self._bot_restart_min_interval_seconds - since_last)
                return self._json(
                    {
                        "ok": False,
                        "message": f"Restart cooldown active ({retry_after:.1f}s remaining)",
                        "retry_after_seconds": round(retry_after, 1),
                    }
                )

            service_restart_ok, service_message = self._schedule_nssm_service_restart()
            if service_restart_ok:
                self._last_bot_restart_request_monotonic = now
                return self._json(
                    {
                        "ok": True,
                        "message": service_message,
                        "restart_mode": "nssm_service",
                    }
                )

            logger.warning("NSSM service restart unavailable: %s", self._safe_log_value(service_message))
            lifecycle = self._lifecycle or getattr(self.bot, "lifecycle", None)
            if not lifecycle:
                return self._json(
                    {
                        "ok": False,
                        "message": (
                            "Restart unavailable (NSSM failed and no lifecycle fallback attached): "
                            f"{service_message}"
                        ),
                    }
                )

            scheduled = await lifecycle.request_restart(reason="dashboard_lifecycle_fallback")
            if scheduled:
                self._last_bot_restart_request_monotonic = now
                return self._json(
                    {
                        "ok": True,
                        "message": (
                            "Lifecycle restart scheduled (NSSM service restart unavailable): "
                            f"{service_message}"
                        ),
                        "restart_mode": "lifecycle_fallback",
                    }
                )
            return self._json(
                {
                    "ok": False,
                    "message": "Restart already pending (lifecycle fallback path)",
                    "restart_mode": "lifecycle_fallback",
                }
            )

    async def _handle_dashboard_restart(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        if self._restart_task and not self._restart_task.done():
            return self._json({"ok": True, "message": "Dashboard restart already running"})

        self._restart_task = asyncio.create_task(self._restart_dashboard())
        self._restart_task.add_done_callback(self._on_restart_finished)
        return self._json({"ok": True, "message": "Dashboard restart scheduled"})

    async def _collect_health_checks(self) -> List[Dict[str, Any]]:
        if not self._health_targets:
            return []
        now = asyncio.get_running_loop().time()
        if self._health_cache and now < self._health_cache_expiry:
            return self._health_cache
        async with self._health_cache_lock:
            if self._health_cache and now < self._health_cache_expiry:
                return self._health_cache
            data = await self._refresh_health_checks()
            self._health_cache = data
            self._health_cache_expiry = now + self._health_cache_ttl
            return data

    async def _refresh_health_checks(self) -> List[Dict[str, Any]]:
        timeout = ClientTimeout(total=self._health_timeout)
        async with ClientSession(timeout=timeout) as session:
            tasks = [self._probe_health_target(session, target) for target in self._health_targets]
            return await asyncio.gather(*tasks)

    async def _probe_health_target(
        self,
        session: ClientSession,
        target: Dict[str, Any],
    ) -> Dict[str, Any]:
        url = target.get("url") or ""
        method = (target.get("method") or "GET").strip().upper() or "GET"
        allow_redirects_value = target.get("allow_redirects")
        allow_redirects = True
        coerced_redirects = self._coerce_bool(allow_redirects_value)
        if coerced_redirects is not None:
            allow_redirects = coerced_redirects

        verify_ssl_value = target.get("verify_ssl")
        ssl_param: Any = None
        coerced_ssl = self._coerce_bool(verify_ssl_value)
        if coerced_ssl is False:
            ssl_param = False

        timeout_value = target.get("timeout")
        request_timeout = None
        if timeout_value is not None:
            try:
                parsed_timeout = float(timeout_value)
                if parsed_timeout > 0:
                    request_timeout = ClientTimeout(total=parsed_timeout)
            except (TypeError, ValueError):
                logging.warning(
                    "Healthcheck target '%s' timeout '%s' invalid – falling back to default",
                    target.get("label") or target.get("key") or url,
                    timeout_value,
                )

        expected_status = target.get("expect_status")

        def _status_ok(status_code: int) -> bool:
            if expected_status is None:
                return 200 <= status_code < 400
            if isinstance(expected_status, int):
                return status_code == expected_status
            if isinstance(expected_status, (list, tuple, set)):
                try:
                    allowed = {int(item) for item in expected_status}
                except (TypeError, ValueError):
                    allowed = set(expected_status)
                return status_code in allowed
            if isinstance(expected_status, str):
                stripped = expected_status.strip()
                if stripped.isdigit():
                    return status_code == int(stripped)
            return 200 <= status_code < 400

        start = time.perf_counter()
        status: Optional[int] = None
        reason: Optional[str] = None
        ok = False
        error: Optional[str] = None
        resolved_url = url
        body_excerpt: Optional[str] = None

        request_kwargs: Dict[str, Any] = {"allow_redirects": allow_redirects}
        if ssl_param is not None:
            request_kwargs["ssl"] = ssl_param
        if request_timeout:
            request_kwargs["timeout"] = request_timeout

        try:
            async with session.request(method, url, **request_kwargs) as resp:
                status = resp.status
                reason = resp.reason
                resolved_url = str(resp.url)
                ok = _status_ok(status)
                if not ok:
                    try:
                        text = await resp.text()
                    except Exception:
                        text = ""
                    if text:
                        body_excerpt = text[:280]
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        result: Dict[str, Any] = {
            "key": target.get("key"),
            "label": target.get("label") or target.get("key") or url,
            "url": url,
            "method": method,
            "ok": ok,
            "status": status,
            "reason": reason,
            "latency_ms": duration_ms,
            "checked_at": _dt.datetime.utcnow().isoformat() + "Z",
        }
        if resolved_url and resolved_url != url:
            result["resolved_url"] = resolved_url
        if error:
            result["error"] = error
        if body_excerpt and not ok:
            result["body_excerpt"] = body_excerpt
        return result


    async def _collect_standalone_snapshot(self) -> List[Dict[str, Any]]:
        manager = getattr(self.bot, "standalone_manager", None)
        if not manager:
            return []
        try:
            return await manager.snapshot()
        except Exception as exc:
            logging.getLogger(__name__).error("Standalone snapshot failed: %s", exc)
            return []

    def _require_standalone_manager(self):
        manager = getattr(self.bot, "standalone_manager", None)
        if not manager:
            raise web.HTTPNotFound(text="Standalone manager unavailable")
        return manager

    def _namespace_for(self, module: str) -> str:
        parts = module.split(".")
        if len(parts) >= 3:
            return ".".join(parts[:3])
        if len(parts) >= 2:
            return ".".join(parts[:2])
        return module

    def _namespace_summary(self, modules: Iterable[str]) -> List[Dict[str, Any]]:
        counter: Dict[str, int] = {}
        for mod in modules:
            ns = self._namespace_for(mod)
            counter[ns] = counter.get(ns, 0) + 1
        return [
            {"namespace": ns, "count": counter[ns]}
            for ns in sorted(counter.keys())
        ]

    def _build_tree(self) -> Dict[str, Any]:
        bot = self.bot
        root_dir = bot.cogs_dir
        active = set(bot.active_cogs())
        discovered = set(bot.cogs_list)
        status_map = bot.cog_status.copy()

        def is_manageable(path: str) -> bool:
            if path == "cogs":
                return False
            return path in active or path in discovered or path in status_map

        def node_status(path: str, *, blocked: bool) -> Optional[str]:
            status = status_map.get(path)
            if status:
                return status
            if blocked:
                return "blocked"
            if path in active:
                return "loaded"
            if path in discovered:
                return "unloaded"
            return None

        def walk(directory: Path, parts: List[str]) -> Dict[str, Any]:
            module_path = "cogs"
            if parts:
                module_path = "cogs." + ".".join(parts)

            blocked_dir = bot.is_namespace_blocked(module_path, assume_normalized=True)
            status = node_status(module_path, blocked=blocked_dir)
            manageable_dir = is_manageable(module_path)
            loaded_dir = module_path in active
            discovered_dir = module_path in discovered
            is_package = (
                module_path in discovered
                or module_path in status_map
                or module_path in active
            ) and module_path != "cogs"

            module_count = 1 if is_package else 0
            loaded_count = 1 if is_package and loaded_dir else 0
            discovered_count = 1 if is_package and discovered_dir else 0

            children: List[Dict[str, Any]] = []
            try:
                entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
            except FileNotFoundError:
                entries = []

            for entry in entries:
                if entry.name.startswith("__pycache__"):
                    continue
                if entry.is_dir():
                    child = walk(entry, parts + [entry.name])
                    children.append(child)
                    module_count += child.get("module_count", 0)
                    loaded_count += child.get("loaded_count", 0)
                    discovered_count += child.get("discovered_count", 0)
                    continue
                if entry.suffix != ".py" or entry.name == "__init__.py":
                    continue
                if parts:
                    mod_path = "cogs." + ".".join(parts + [entry.stem])
                else:
                    mod_path = f"cogs.{entry.stem}"
                blocked_child = bot.is_namespace_blocked(mod_path, assume_normalized=True)
                loaded_child = mod_path in active
                discovered_child = mod_path in discovered
                manageable_child = is_manageable(mod_path)
                status_child = node_status(mod_path, blocked=blocked_child) or "not_discovered"
                child = {
                    "type": "module",
                    "name": entry.stem,
                    "path": mod_path,
                    "blocked": blocked_child,
                    "loaded": loaded_child,
                    "discovered": discovered_child,
                    "manageable": manageable_child,
                    "status": status_child,
                }
                children.append(child)
                module_count += 1
                if loaded_child:
                    loaded_count += 1
                if discovered_child:
                    discovered_count += 1

            return {
                "type": "directory",
                "name": directory.name if parts else "cogs",
                "path": module_path,
                "blocked": blocked_dir,
                "status": status,
                "is_package": is_package,
                "manageable": manageable_dir,
                "loaded": loaded_dir,
                "discovered": discovered_dir,
                "module_count": module_count,
                "loaded_count": loaded_count,
                "discovered_count": discovered_count,
                "children": children,
            }

        if not root_dir.exists():
            return {
                "type": "directory",
                "name": "cogs",
                "path": "cogs",
                "blocked": bot.is_namespace_blocked("cogs"),
                "status": None,
                "is_package": False,
                "manageable": False,
                "loaded": False,
                "discovered": False,
                "module_count": 0,
                "loaded_count": 0,
                "discovered_count": 0,
                "children": [],
            }

        return walk(root_dir, [])

    async def _handle_reload(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        payload = await request.json()
        names = payload.get("names") or []
        if not isinstance(names, list) or not names:
            raise web.HTTPBadRequest(text="'names' must be a non-empty list")
        normalized = self._normalize_names(names)
        safe_names = self._safe_log_value(normalized)
        safe_remote = self._safe_log_value(request.remote)
        logger.info("AUDIT master-dashboard cog_reload: names=%s from %s", safe_names, safe_remote)

        results: Dict[str, Dict[str, Any]] = {}
        async with self._lock:
            for name in normalized:
                if self.bot.is_namespace_blocked(name, assume_normalized=True):
                    results[name] = {
                        "ok": False,
                        "message": f"🚫 {name} ist blockiert",
                    }
                    continue
                if name not in self.bot.extensions:
                    results[name] = {
                        "ok": False,
                        "message": f"{name} is not loaded",
                    }
                    continue
                ok, message = await self.bot.reload_cog(name)
                results[name] = {"ok": ok, "message": message}
        return self._json({"results": results})

    async def _handle_load(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        payload = await request.json()
        names = payload.get("names") or []
        if not isinstance(names, list) or not names:
            raise web.HTTPBadRequest(text="'names' must be a non-empty list")
        self.bot.auto_discover_cogs()
        normalized = self._normalize_names(names)

        results: Dict[str, Dict[str, Any]] = {}
        async with self._lock:
            for name in normalized:
                if self.bot.is_namespace_blocked(name, assume_normalized=True):
                    results[name] = {
                        "ok": False,
                        "message": f"🚫 {name} ist blockiert",
                    }
                    continue
                ok, message = await self.bot.reload_cog(name)
                results[name] = {"ok": ok, "message": message}
        return self._json({"results": results})

    async def _handle_unload(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        payload = await request.json()
        names = payload.get("names") or []
        if not isinstance(names, list) or not names:
            raise web.HTTPBadRequest(text="'names' must be a non-empty list")
        normalized = self._normalize_names(names)
        safe_names = self._safe_log_value(normalized)
        safe_remote = self._safe_log_value(request.remote)
        logger.warning("AUDIT master-dashboard cog_unload: names=%s from %s", safe_names, safe_remote)

        results: Dict[str, Dict[str, Any]] = {}
        async with self._lock:
            unload_result = await self.bot.unload_many(normalized)
            for name in normalized:
                status = unload_result.get(name, "unknown")
                if status == "unloaded":
                    results[name] = {"ok": True, "message": f"✅ Unloaded {name}"}
                elif status == "timeout":
                    results[name] = {"ok": False, "message": f"⏱️ Timeout unloading {name}"}
                elif status.startswith("error"):
                    results[name] = {"ok": False, "message": status}
                else:
                    results[name] = {"ok": False, "message": status}
        return self._json({"results": results})

    async def _handle_reload_all(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        async with self._lock:
            ok, summary = await self.bot.reload_all_cogs_with_discovery()
        if ok:
            return self._json({"ok": True, "summary": summary})
        raise web.HTTPInternalServerError(text=str(summary))

    async def _handle_reload_namespace(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        payload = await request.json()
        namespace = payload.get("namespace")
        if not namespace:
            raise web.HTTPBadRequest(text="'namespace' is required")

        try:
            normalized = self.bot.normalize_namespace(namespace)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid namespace")

        if self.bot.is_namespace_blocked(normalized, assume_normalized=True):
            return self._json(
                {
                    "ok": False,
                    "results": {},
                    "message": f"{normalized} ist blockiert",
                }
            )

        async with self._lock:
            results = await self.bot.reload_namespace(normalized)
        ok = all(v in ("loaded", "reloaded") for v in results.values())
        if not results:
            message = f"Keine Cogs unter {normalized} gefunden"
        else:
            message = f"Reloaded {len(results)} cogs under {normalized}"
        return self._json({"ok": ok, "results": results, "message": message})

    async def _handle_discover(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        before = set(self.bot.cogs_list)
        self.bot.auto_discover_cogs()
        after = set(self.bot.cogs_list)
        new = sorted(after - before)
        return self._json({"ok": True, "new": new, "count": len(after)})

    async def _handle_block(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        payload = await request.json()
        path = payload.get("path")
        if not path:
            raise web.HTTPBadRequest(text="'path' is required")
        safe_path = self._safe_log_value(path)
        safe_remote = self._safe_log_value(request.remote)
        logger.warning("AUDIT master-dashboard cog_block: path=%s from %s", safe_path, safe_remote)
        async with self._lock:
            try:
                result = await self.bot.block_namespace(path)
            except ValueError:
                raise web.HTTPBadRequest(text="Invalid namespace")
        namespace = result.get("namespace", path)
        changed = result.get("changed", False)
        unloaded = result.get("unloaded", {})
        message = (
            f"🚫 {namespace} blockiert" if changed else f"{namespace} war bereits blockiert"
        )
        return self._json(
            {
                "ok": True,
                "namespace": namespace,
                "changed": changed,
                "unloaded": unloaded,
                "message": message,
            }
        )

    async def _handle_unblock(self, request: web.Request) -> web.Response:
        self._check_auth(request)
        payload = await request.json()
        path = payload.get("path")
        if not path:
            raise web.HTTPBadRequest(text="'path' is required")
        safe_path = self._safe_log_value(path)
        safe_remote = self._safe_log_value(request.remote)
        logger.info("AUDIT master-dashboard cog_unblock: path=%s from %s", safe_path, safe_remote)
        async with self._lock:
            try:
                result = await self.bot.unblock_namespace(path)
            except ValueError:
                raise web.HTTPBadRequest(text="Invalid namespace")
        namespace = result.get("namespace", path)
        changed = result.get("changed", False)
        message = (
            f"✅ {namespace} freigegeben" if changed else f"{namespace} war nicht blockiert"
        )
        return self._json(
            {
                "ok": True,
                "namespace": namespace,
                "changed": changed,
                "message": message,
            }
        )

    async def _handle_log_index(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        entries = self._list_log_files()
        return self._json({"logs": entries})

    async def _handle_log_read(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        name = request.match_info.get("name", "")
        lines_raw = request.query.get("lines")
        try:
            lines = int(lines_raw) if lines_raw else LOG_TAIL_DEFAULT_LINES
            if lines <= 0:
                raise ValueError
            lines = min(lines, LOG_TAIL_MAX_LINES)
        except ValueError:
            raise web.HTTPBadRequest(
                text=f"lines must be a positive integer <= {LOG_TAIL_MAX_LINES}"
            )
        path = self._resolve_log_file(name)
        try:
            stat = path.stat()
            entries = self._tail_log_lines(path, lines)
        except OSError as exc:
            logging.getLogger(__name__).exception(
                "Failed reading log file %s: %s", self._safe_log_value(name), exc
            )
            raise web.HTTPInternalServerError(text="Failed to read log file") from exc
        modified = _dt.datetime.fromtimestamp(
            stat.st_mtime,
            tz=_dt.timezone.utc,
        ).isoformat()
        return self._json(
            {
                "name": path.name,
                "size": stat.st_size,
                "modified": modified,
                "lines": entries,
            }
        )


    async def _handle_standalone_list(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        data = await self._collect_standalone_snapshot()
        return self._json({"bots": data})

    async def _handle_standalone_logs(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        manager = self._require_standalone_manager()
        key = request.match_info.get("key", "").strip()
        limit_raw = request.query.get("limit")
        try:
            limit = int(limit_raw) if limit_raw else 200
            if limit <= 0:
                raise ValueError
            limit = min(limit, 1000)
        except ValueError:
            raise web.HTTPBadRequest(text="limit must be a positive integer <= 1000")
        try:
            logs = await manager.logs(key, limit=limit)
        except Exception as exc:
            if StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            raise
        return self._json({"logs": logs})

    async def _handle_standalone_start(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        manager = self._require_standalone_manager()
        key = request.match_info.get("key", "").strip()
        safe_key = self._safe_log_value(key)
        try:
            status = await manager.start(key)
        except Exception as exc:
            if StandaloneAlreadyRunning and isinstance(exc, StandaloneAlreadyRunning):
                status = await manager.status(key)
            elif StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            elif StandaloneManagerError and isinstance(exc, StandaloneManagerError):
                logging.getLogger(__name__).exception(
                    "Error when starting standalone bot (key=%s)", safe_key
                )
                raise web.HTTPInternalServerError(text="An internal error has occurred.") from exc
            else:
                logging.getLogger(__name__).exception(
                    "Unexpected error when starting standalone bot (key=%s)", safe_key
                )
                raise web.HTTPInternalServerError(text="An internal error has occurred.") from exc
        return self._json({"standalone": status})

    async def _handle_standalone_stop(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        manager = self._require_standalone_manager()
        key = request.match_info.get("key", "").strip()
        safe_key = self._safe_log_value(key)
        try:
            status = await manager.stop(key)
        except Exception as exc:
            if StandaloneNotRunning and isinstance(exc, StandaloneNotRunning):
                status = await manager.status(key)
            elif StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            elif StandaloneManagerError and isinstance(exc, StandaloneManagerError):
                logging.getLogger(__name__).exception(
                    "Error when stopping standalone bot (key=%s)", safe_key
                )
                raise web.HTTPInternalServerError(text="An internal error has occurred.") from exc
            else:
                logging.getLogger(__name__).exception(
                    "Unexpected error when stopping standalone bot (key=%s)", safe_key
                )
                raise web.HTTPInternalServerError(text="An internal error has occurred.") from exc
        return self._json({"standalone": status})

    async def _handle_standalone_restart(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        manager = self._require_standalone_manager()
        key = request.match_info.get("key", "").strip()
        safe_key = self._safe_log_value(key)
        try:
            status = await manager.restart(key)
        except Exception as exc:
            if StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            if StandaloneManagerError and isinstance(exc, StandaloneManagerError):
                logging.getLogger(__name__).exception(
                    "Error when restarting standalone bot (key=%s)", safe_key
                )
                raise web.HTTPInternalServerError(text="An internal error has occurred.") from exc
            logging.getLogger(__name__).exception(
                "Unexpected error when restarting standalone bot (key=%s)", safe_key
            )
            raise web.HTTPInternalServerError(text="An internal error has occurred.") from exc
        return self._json({"standalone": status})

    async def _handle_standalone_autostart(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        manager = self._require_standalone_manager()
        key = request.match_info.get("key", "").strip()
        try:
            manager.config(key)
        except Exception as exc:
            if StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            raise

        try:
            payload = await request.json()
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, web.HTTPException):
                raise
            raise web.HTTPBadRequest(text="Invalid JSON payload") from exc

        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Payload must be a JSON object")

        enabled_raw = payload.get("enabled")
        enabled: Optional[bool]
        if isinstance(enabled_raw, bool):
            enabled = enabled_raw
        elif isinstance(enabled_raw, (int, float)):
            enabled = bool(enabled_raw)
        elif isinstance(enabled_raw, str):
            lowered = enabled_raw.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                enabled = True
            elif lowered in {"0", "false", "no", "off"}:
                enabled = False
            else:
                enabled = None
        else:
            enabled = None

        if enabled is None:
            raise web.HTTPBadRequest(text="'enabled' must be a boolean")

        try:
            status = await manager.set_autostart(key, enabled)
        except Exception as exc:
            if StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            raise

        return self._json({"standalone": status})

    async def _handle_standalone_command(self, request: web.Request) -> web.Response:
        self._check_auth(request, required=bool(self.token))
        manager = self._require_standalone_manager()
        key = request.match_info.get("key", "").strip()
        safe_key = self._safe_log_value(key)
        try:
            manager.config(key)
        except Exception as exc:
            if StandaloneConfigNotFound and isinstance(exc, StandaloneConfigNotFound):
                raise web.HTTPNotFound(text="Standalone bot not found")
            raise

        payload = await request.json()
        command = str(payload.get("command") or "").strip()
        if not command:
            raise web.HTTPBadRequest(text="'command' is required")
        command_payload = payload.get("payload")
        try:
            payload_json = json.dumps(command_payload, ensure_ascii=False) if command_payload is not None else None
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="payload must be JSON-serializable")

        db.execute(
            "INSERT INTO standalone_commands(bot, command, payload, status, created_at) "
            "VALUES(?, ?, ?, 'pending', CURRENT_TIMESTAMP)",
            (key, command, payload_json),
        )
        row = db.query_one("SELECT last_insert_rowid()")
        command_id = row[0] if row else None

        try:
            await manager.ensure_running(key)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Could not ensure %s running after command enqueue: %s",
                safe_key,
                self._safe_log_value(exc),
            )

        status = await manager.status(key)
        return self._json(
            {
                "queued": command_id,
                "standalone": status,
            },
            status=201,
        )

if TYPE_CHECKING:  # pragma: no cover - avoid runtime dependency cycle
    from main_bot import MasterBot
