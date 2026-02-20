"""TurnierPublicCog - starts/stops the public tournament website server (port 8767)."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from discord.ext import commands

log = logging.getLogger(__name__)


class TurnierPublicCog(commands.Cog):
    """Wraps TurnierPublicServer as a reloadable cog."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.server: Optional[object] = None
        self._start_task: Optional[asyncio.Task] = None

        try:
            from service.turnier_public import TurnierPublicServer

            self.server = TurnierPublicServer(self.bot)
            log.info("TurnierPublicServer initialisiert.")
        except Exception as e:
            log.error("TurnierPublicServer konnte nicht initialisiert werden: %s", e)
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
                pass

        if self.server:
            try:
                await self.server.stop()
            except Exception as e:
                log.error("Fehler beim Stoppen des TurnierPublicServers: %s", e)

    async def _start_server(self) -> None:
        await self.bot.wait_until_ready()
        if self.server:
            try:
                await self.server.start()
            except Exception as e:
                log.error("TurnierPublicServer konnte nicht gestartet werden: %s", e)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TurnierPublicCog(bot))
