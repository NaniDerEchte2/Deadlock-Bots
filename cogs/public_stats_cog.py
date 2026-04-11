"""PublicStatsCog - starts/stops the public activity statistics server (port 8768)."""

from __future__ import annotations

import asyncio
import importlib
import logging

from typing import TYPE_CHECKING
from discord.ext import commands

if TYPE_CHECKING:
    from main_bot import MasterBot

log = logging.getLogger(__name__)

PUBLIC_STATS_HOST = "0.0.0.0"
PUBLIC_STATS_PORT = 8768


class PublicStatsCog(commands.Cog):
    """Wraps PublicStatsServer as a reloadable cog."""

    def __init__(self, bot: MasterBot) -> None:
        self.bot = bot
        self.server: object | None = None
        self._start_task: asyncio.Task | None = None

        try:
            from service.public_stats import PublicStatsServer

            self.server = PublicStatsServer(host=PUBLIC_STATS_HOST, port=PUBLIC_STATS_PORT)
            log.info("PublicStatsServer initialisiert (Port %s).", PUBLIC_STATS_PORT)
        except Exception as e:
            log.error("PublicStatsServer konnte nicht initialisiert werden: %s", e)
            self.server = None

    async def cog_load(self) -> None:
        if self.server is None:
            return
        self._start_task = asyncio.create_task(self._start_server())

    async def cog_unload(self) -> None:
        if self._start_task and not self._start_task.done():
            self._start_task.cancel()
            try:
                await self._start_task
            except asyncio.CancelledError:
                log.debug("PublicStatsServer start task cancelled during cog_unload")

        if self.server:
            try:
                await self.server.stop()
            except Exception as e:
                log.error("Fehler beim Stoppen des PublicStatsServers: %s", e)

    async def _start_server(self) -> None:
        await self.bot.wait_until_ready()
        if self.server:
            try:
                await self.server.start()
            except Exception as e:
                log.error("PublicStatsServer konnte nicht gestartet werden: %s", e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PublicStatsCog(bot))  # type: ignore[arg-type]