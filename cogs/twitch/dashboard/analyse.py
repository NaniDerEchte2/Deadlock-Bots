"""Analyse page for the Twitch dashboard - kept for backwards compatibility."""

from __future__ import annotations

import html
from typing import Optional

from aiohttp import web


class DashboardAnalyseMixin:
    """Legacy analyse view - redirects to new analytics dashboard."""

    async def analyse(self, request: web.Request) -> web.Response:
        """Redirect to new analytics dashboard."""
        # Keep for backwards compatibility, redirect to new analytics
        return web.HTTPFound("/twitch/analytics")


__all__ = ["DashboardAnalyseMixin"]
