"""Composable Twitch cog assembled from dedicated mixins."""

from __future__ import annotations

from .analytics.legacy_token import LegacyTokenAnalyticsMixin
from .analytics.mixin import TwitchAnalyticsMixin
from .base import TwitchBaseCog
from .community.admin import TwitchAdminMixin
from .community.leaderboard import (
    LeaderboardOptions,
    TwitchLeaderboardMixin,
    TwitchLeaderboardView,
)
from .community.partner_recruit import TwitchPartnerRecruitMixin
from .dashboard.mixin import TwitchDashboardMixin
from .monitoring.monitoring import TwitchMonitoringMixin
from .raid.commands import RaidCommandsMixin
from .raid.mixin import TwitchRaidMixin

__all__ = [
    "TwitchStreamCog",
    "LeaderboardOptions",
    "TwitchLeaderboardView",
]


class TwitchStreamCog(
    LegacyTokenAnalyticsMixin,
    TwitchAnalyticsMixin,
    TwitchRaidMixin,
    RaidCommandsMixin,
    TwitchPartnerRecruitMixin,
    TwitchDashboardMixin,
    TwitchLeaderboardMixin,
    TwitchAdminMixin,
    TwitchMonitoringMixin,
    TwitchBaseCog,
):
    """Monitor Twitch-Streamer (Deadlock), poste Go-Live, sammle Stats, Dashboard, Auto-Raids."""

    # The mixins and base class provide the full implementation.
    pass
