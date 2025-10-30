"""Composable Twitch dashboard application."""

from __future__ import annotations

from typing import Optional

from aiohttp import web

from .base import DashboardBase, log
from .live import DashboardLiveMixin
from .stats import DashboardStatsMixin
from .templates import DashboardTemplateMixin


class Dashboard(
    DashboardStatsMixin,
    DashboardLiveMixin,
    DashboardTemplateMixin,
    DashboardBase,
):
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
            web.get("/twitch/partners", self.partner_stats),
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
) -> web.Application:
    app = web.Application()
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
