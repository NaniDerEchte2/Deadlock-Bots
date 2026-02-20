"""Dashboard Cog - Makes the dashboard reloadable like any other cog."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from discord.ext import commands

if TYPE_CHECKING:
    from main_bot import MasterBot

log = logging.getLogger(__name__)

DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8766


class DashboardCog(commands.Cog):
    """Wraps the DashboardServer as a reloadable cog."""

    def __init__(self, bot: MasterBot) -> None:
        self.bot = bot
        self.dashboard: Optional[object] = None
        self._start_task: Optional[asyncio.Task] = None

        # Import and create dashboard server
        try:
            from service.dashboard import DashboardServer

            self.dashboard = DashboardServer(
                self.bot, host=DASHBOARD_HOST, port=DASHBOARD_PORT
            )
            log.info(
                "Dashboard initialized in cog (Host %s, Port %s)",
                DASHBOARD_HOST,
                DASHBOARD_PORT,
            )

        except Exception as e:
            log.error("Could not initialize dashboard in cog: %s", e)
            self.dashboard = None

    async def cog_load(self) -> None:
        """Called when the cog is loaded - start the dashboard server."""
        if self.dashboard is None:
            return

        # Set bot.dashboard for backward compatibility
        self.bot.dashboard = self.dashboard

        log.info("Starting dashboard HTTP server...")
        self._start_task = asyncio.create_task(self._start_dashboard())

    async def cog_unload(self) -> None:
        """Called when the cog is unloaded/reloaded - stop the dashboard server."""
        if self._start_task and not self._start_task.done():
            self._start_task.cancel()
            try:
                await self._start_task
            except asyncio.CancelledError:
                log.debug("Dashboard start task cancelled during unload")

        if self.dashboard:
            log.info("Stopping dashboard HTTP server...")
            try:
                await self.dashboard.stop()
            except Exception as e:
                log.error("Error stopping dashboard: %s", e)

        # Clear bot.dashboard reference
        if hasattr(self.bot, "dashboard"):
            self.bot.dashboard = None

    async def _start_dashboard(self) -> None:
        """Background task to start the dashboard server."""
        await self.bot.wait_until_ready()

        if self.dashboard:
            try:
                await self.dashboard.start()
                log.info("Dashboard HTTP server started successfully")
            except Exception as e:
                log.error("Failed to start dashboard: %s", e)


async def setup(bot: commands.Bot) -> None:
    """Setup function to add the cog to the bot."""
    await bot.add_cog(DashboardCog(bot))  # type: ignore[arg-type]
