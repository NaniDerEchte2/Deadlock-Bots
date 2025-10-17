"""Composable Twitch cog assembled from dedicated mixins."""

from __future__ import annotations

from .admin import TwitchAdminMixin
from .base import TwitchBaseCog
from .dashboard_mixin import TwitchDashboardMixin
from .leaderboard import LeaderboardOptions, TwitchLeaderboardMixin, TwitchLeaderboardView
from .monitoring import TwitchMonitoringMixin

__all__ = [
    "TwitchStreamCog",
    "LeaderboardOptions",
    "TwitchLeaderboardView",
]


class TwitchStreamCog(
    TwitchDashboardMixin,
    TwitchLeaderboardMixin,
    TwitchAdminMixin,
    TwitchMonitoringMixin,
    TwitchBaseCog,
):
    """Monitor Twitch-Streamer (Deadlock), poste Go-Live, sammle Stats, Dashboard."""

    # The mixins and base class provide the full implementation.
    pass
