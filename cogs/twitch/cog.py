"""Composable Twitch cog assembled from dedicated mixins."""

from __future__ import annotations

from .admin import TwitchAdminMixin
from .base import TwitchBaseCog
from .dashboard_mixin import TwitchDashboardMixin
from .leaderboard import LeaderboardOptions, TwitchLeaderboardMixin, TwitchLeaderboardView
from .monitoring import TwitchMonitoringMixin
from .raid_mixin import TwitchRaidMixin
from .raid_commands import RaidCommandsMixin

__all__ = [
    "TwitchStreamCog",
    "LeaderboardOptions",
    "TwitchLeaderboardView",
]


class TwitchStreamCog(
    TwitchRaidMixin,
    RaidCommandsMixin,
    TwitchDashboardMixin,
    TwitchLeaderboardMixin,
    TwitchAdminMixin,
    TwitchMonitoringMixin,
    TwitchBaseCog,
):
    """Monitor Twitch-Streamer (Deadlock), poste Go-Live, sammle Stats, Dashboard, Auto-Raids."""

    # The mixins and base class provide the full implementation.
    pass
