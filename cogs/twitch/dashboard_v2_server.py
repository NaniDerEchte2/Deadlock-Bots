"""Embedded aiohttp app serving only the Twitch analytics dashboard v2."""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from aiohttp import web

from .analytics_v2 import AnalyticsV2Mixin


class DashboardV2Server(AnalyticsV2Mixin):
    """Minimal dashboard server exposing only v2 routes and APIs."""

    def __init__(
        self,
        *,
        app_token: Optional[str],
        noauth: bool,
        partner_token: Optional[str],
        reload_cb: Optional[Callable[[], Awaitable[str]]] = None,
    ) -> None:
        self._token = app_token
        self._noauth = noauth
        self._partner_token = partner_token
        self._reload_cb = reload_cb

    def _check_admin_token(self, token: Optional[str]) -> bool:
        if self._noauth:
            return True
        if not token or not self._token:
            return False
        return token == self._token

    async def index(self, request: web.Request) -> web.StreamResponse:
        """Keep /twitch alive and forward to the v2 dashboard."""
        destination = "/twitch/dashboard-v2"
        if request.query_string:
            destination = f"{destination}?{request.query_string}"
        raise web.HTTPFound(destination)

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
            web.get("/twitch", self.index),
            web.post("/twitch/reload", self.reload_cog),
        ])
        self._register_v2_routes(app.router)


def build_v2_app(
    *,
    noauth: bool,
    token: Optional[str],
    partner_token: Optional[str] = None,
    reload_cb: Optional[Callable[[], Awaitable[str]]] = None,
) -> web.Application:
    app = web.Application()
    DashboardV2Server(
        app_token=token,
        noauth=noauth,
        partner_token=partner_token,
        reload_cb=reload_cb,
    ).attach(app)
    return app


__all__ = ["DashboardV2Server", "build_v2_app"]
