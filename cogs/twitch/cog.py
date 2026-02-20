"""Composable Twitch cog assembled from dedicated mixins."""

from __future__ import annotations

from .community.admin import TwitchAdminMixin
from .base import TwitchBaseCog
from .dashboard.mixin import TwitchDashboardMixin
from .community.leaderboard import (
    LeaderboardOptions,
    TwitchLeaderboardMixin,
    TwitchLeaderboardView,
)
from .analytics.legacy_token import LegacyTokenAnalyticsMixin
from .monitoring.monitoring import TwitchMonitoringMixin
from .raid.mixin import TwitchRaidMixin
from .raid.commands import RaidCommandsMixin
from .analytics.mixin import TwitchAnalyticsMixin
from .community.partner_recruit import TwitchPartnerRecruitMixin

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
