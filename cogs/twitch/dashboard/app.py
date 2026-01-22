"""Composable Twitch dashboard application."""

from __future__ import annotations

from typing import Optional

from aiohttp import web

from .base import DashboardBase, log
from .live import DashboardLiveMixin
from .stats import DashboardStatsMixin
from .templates import DashboardTemplateMixin
from .raid import DashboardRaidMixin
from .analyse import DashboardAnalyseMixin


class Dashboard(
    DashboardRaidMixin,
    DashboardAnalyseMixin,
    DashboardStatsMixin,
    DashboardLiveMixin,
    DashboardTemplateMixin,
    DashboardBase,
):
    # Callback für Cog-Reload
    _reload_cb = None

    async def reload_cog(self, request: web.Request) -> web.Response:
        """Handler für den Cog-Reload."""
        token = (await request.post()).get("token", "")
        if not self._check_token(token):
            return web.Response(text="Unauthorized", status=401)

        if self._reload_cb:
            msg = await self._reload_cb()
            return web.Response(text=msg)
        return web.Response(text="Kein Reload-Handler definiert", status=501)

    def attach(self, app: web.Application):
        app.add_routes([
            web.get("/twitch", self.index),
            web.get("/twitch/add_any", self.add_any),
            web.get("/twitch/add_url", self.add_url),
            web.get("/twitch/add_login/{login}", self.add_login),
            web.post("/twitch/add_streamer", self.add_streamer),
            web.post("/twitch/remove", self.remove),
            web.post("/twitch/verify", self.verify),
            web.post("/twitch/discord_flag", self.discord_flag),
            web.post("/twitch/discord_link", self.discord_link),
            web.get("/twitch/stats", self.stats),
            web.get("/twitch/analyse", self.analyse),
            web.get("/twitch/partners", self.partner_stats),
            # Raid Bot Routes
            web.get("/twitch/raid/auth", self.raid_auth_start),
            web.get("/twitch/raid/callback", self.raid_oauth_callback),
            web.post("/twitch/raid/toggle", self.raid_toggle),
            web.get("/twitch/raid/history", self.raid_history),
            # Reload
            web.post("/twitch/reload", self.reload_cog),
        ])


def build_app(
    *,
    noauth: bool,
    token: Optional[str],
    partner_token: Optional[str] = None,
    add_cb=None,
    remove_cb=None,
    list_cb=None,
    stats_cb=None,
    verify_cb=None,
    discord_flag_cb=None,
    discord_profile_cb=None,
    raid_history_cb=None,
    raid_bot=None,
    reload_cb=None,
    http_session=None,
    redirect_uri: str = "",
) -> web.Application:
    app = web.Application()

    # HTTP-Session für OAuth-Callbacks speichern
    if http_session:
        app["_http_session"] = http_session

    have_full_ui = all(
        cb is not None
        for cb in (
            add_cb,
            remove_cb,
            list_cb,
            stats_cb,
            verify_cb,
            discord_flag_cb,
            discord_profile_cb,
        )
    )

    if have_full_ui:
        ui = Dashboard(
            app_token=token,
            noauth=noauth,
            partner_token=partner_token,
            add_cb=add_cb,
            remove_cb=remove_cb,
            list_cb=list_cb,
            stats_cb=stats_cb,
            verify_cb=verify_cb,
            discord_flag_cb=discord_flag_cb,
            discord_profile_cb=discord_profile_cb,
        )
        # Raid-Bot Attribute setzen
        ui._raid_bot = raid_bot
        ui._redirect_uri = redirect_uri
        ui._raid_history_cb = raid_history_cb
        ui._reload_cb = reload_cb
        ui.attach(app)
    else:
        async def index(request: web.Request):
            return web.Response(text="Twitch dashboard is running.")

        app.add_routes([web.get("/twitch", index)])

    return app


try:
    Dashboard.build_app = staticmethod(build_app)  # type: ignore[attr-defined]
except Exception as exc:  # pragma: no cover - defensive logging
    log.debug("Konnte Dashboard.build_app nicht setzen: %s", exc)


__all__ = ["Dashboard", "build_app"]
