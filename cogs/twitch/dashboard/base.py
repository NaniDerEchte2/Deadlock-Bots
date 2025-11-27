"""Core utilities and shared helpers for the Twitch dashboard."""

from __future__ import annotations

import logging
import os
import re
from typing import Awaitable, Callable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote

from aiohttp import web

log = logging.getLogger("TwitchStreams")

# Erlaubte Twitch-Logins: 3–25 Zeichen, a–z, 0–9, _
LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{3,25}$")

def _sanitize_log_value(value: str) -> str:
    text = "" if value is None else str(value)
    return text.replace("\r", "\\r").replace("\n", "\\n")


class DashboardBase:
    """Common setup and helpers shared by the dashboard views."""

    def __init__(
        self,
        *,
        app_token: Optional[str],
        noauth: bool,
        partner_token: Optional[str],
        add_cb: Callable[[str, bool], Awaitable[str]],
        remove_cb: Callable[[str], Awaitable[None]],
        list_cb: Callable[[], Awaitable[List[dict]]],
        stats_cb: Callable[..., Awaitable[dict]],
        verify_cb: Callable[[str, str], Awaitable[str]],
        discord_flag_cb: Callable[[str, bool], Awaitable[str]],
        discord_profile_cb: Callable[[str, Optional[str], Optional[str], bool], Awaitable[str]],
    ):
        self._token = app_token
        self._noauth = noauth
        self._partner_token = partner_token
        self._add = add_cb
        self._remove = remove_cb
        self._list = list_cb
        self._stats = stats_cb
        self._verify = verify_cb
        self._discord_flag = discord_flag_cb
        self._discord_profile = discord_profile_cb
        self._master_dashboard_url = self._resolve_master_dashboard_url()
        self._master_dashboard_href = html_escape(self._master_dashboard_url, quote=True)

    @staticmethod
    def _normalize_host(host: Optional[str]) -> str:
        if not host:
            return "127.0.0.1"
        host = host.strip()
        if not host:
            return "127.0.0.1"
        if host in {"0.0.0.0", "::", "*"}:
            return "127.0.0.1"
        return host

    @staticmethod
    def _parse_port(raw: Optional[str], default: int) -> int:
        if not raw:
            return default
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except ValueError as exc:
            log.debug("Ungültiger Portwert '%s': %s", raw, exc)
        log.warning("Ungültiger Portwert '%s' – verwende %s", raw, default)
        return default

    @staticmethod
    def _format_url(host: str, port: int, path: str) -> str:
        host = DashboardBase._normalize_host(host)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{host}:{port}"
        normalized_path = path if path.startswith("/") else f"/{path}"
        return urlunsplit(("http", netloc, normalized_path, "", ""))

    @staticmethod
    def _host_without_port(raw: Optional[str]) -> str:
        if not raw:
            return ""
        host = raw.split(",")[0].strip()
        if not host:
            return ""
        if host.startswith("["):
            # IPv6 literal
            end = host.find("]")
            if end != -1:
                host = host[1:end]
        elif ":" in host:
            host = host.split(":", 1)[0]
        return host.lower()

    def _is_local_request(self, request: web.Request) -> bool:
        context_header = request.headers.get("X-Dashboard-Context")
        if context_header:
            lowered = context_header.strip().lower()
            if lowered == "local":
                return True
            if lowered == "public":
                return False

        host_header = (
            request.headers.get("X-Forwarded-Host")
            or request.headers.get("Host")
            or request.host
            or ""
        )
        host = self._host_without_port(host_header)
        if host in {"127.0.0.1", "localhost", "::1"}:
            return True
        transport = getattr(request, "transport", None)
        if transport is not None:
            peer = transport.get_extra_info("peername")
            if isinstance(peer, tuple) and peer:
                peer_host = self._host_without_port(str(peer[0]))
                if peer_host in {"127.0.0.1", "localhost", "::1"}:
                    return True
        return False

    def _resolve_master_dashboard_url(self) -> str:
        host = os.getenv("MASTER_DASHBOARD_HOST") or "127.0.0.1"
        port = self._parse_port(os.getenv("MASTER_DASHBOARD_PORT"), 8766)
        return self._format_url(host, port, "/admin")

    # ---------- Auth ----------
    def _require_token(self, request: web.Request):
        if self._noauth:
            return
        token = request.headers.get("X-Admin-Token") or request.query.get("token")
        if not token or not self._token or token != self._token:
            raise web.HTTPUnauthorized(text="missing or invalid token")

    def _require_partner_token(self, request: web.Request):
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

    # ---------- Redirect helper ----------
    def _redirect_location(
        self,
        request: web.Request,
        *,
        ok: Optional[str] = None,
        err: Optional[str] = None,
    ) -> str:
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
                    return urlunsplit(("", "", parts.path, urlencode(params), "")) or "/twitch"
            except Exception as exc:
                safe_referer = _sanitize_log_value(referer)
                log.debug("Failed to build Twitch redirect from referer %s: %s", safe_referer, exc, exc_info=True)

        params = {}
        if ok:
            params["ok"] = ok
        if err:
            params["err"] = err
        if params:
            return f"/twitch?{urlencode(params)}"
        return "/twitch"

    # ---------- Helpers ----------
    @staticmethod
    def _normalize_login(value: str) -> Optional[str]:
        """
        Akzeptiert: Login, @Login, komplette/teil-URL, urlencoded etc.
        Gibt den Twitch-Login (lowercase) zurück oder None wenn ungültig.
        """
        if not value:
            return None
        s = unquote(value).strip()  # z.B. "twitch.tv%2Fxy" -> "twitch.tv/xy"
        if not s:
            return None

        if s.startswith("@"):
            s = s[1:].strip()

        if "twitch.tv" in s or "://" in s or "/" in s:
            if "://" not in s:
                s = "https://" + s  # urlsplit braucht ein Schema
            try:
                parts = urlsplit(s)  # robustes URL-Parsing
                segs = [p for p in (parts.path or "").split("/") if p]
                if segs:
                    s = segs[0]
            except Exception:
                return None

        s = s.strip().lower()
        if LOGIN_RE.match(s):
            return s
        return None

    async def _do_add(self, raw: str) -> str:
        login = self._normalize_login(raw)
        if not login:
            raise web.HTTPBadRequest(text="invalid twitch login or url")
        msg = await self._add(login, False)
        return msg or "added"


def html_escape(value: str, *, quote: bool = False) -> str:
    """Local helper so we can avoid importing html at module level."""

    import html

    return html.escape(value, quote=quote)


__all__ = ["DashboardBase", "LOGIN_RE", "log", "html_escape"]
