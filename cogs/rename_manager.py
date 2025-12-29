# cogs/rename_manager.py
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Optional, Tuple

import discord
from discord.ext import commands

from service.config import settings

logger = logging.getLogger("RenameManagerCog")

QUEUE_CHECK_INTERVAL_SECONDS = 1.0
ERROR_BACKOFF_SECONDS = 10.0

class RenameManagerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._rename_queue: deque[Tuple[int, str, str]] = deque()  # (channel_id, new_name, reason)
        self._rename_task: Optional[asyncio.Task[None]] = None
        self._rename_lock = asyncio.Lock()
        self._last_rename_attempt_time: float = 0.0

    async def cog_load(self):
        logger.info("RenameManagerCog loaded. Starting rename queue processor.")
        self._start_rename_processor()

    async def cog_unload(self):
        logger.info("RenameManagerCog unloaded. Cancelling rename queue processor.")
        if self._rename_task:
            self._rename_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._rename_task

    def _start_rename_processor(self):
        if self._rename_task is None or self._rename_task.done():
            self._rename_task = self.bot.loop.create_task(self._process_rename_queue())

    async def _process_rename_queue(self):
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(QUEUE_CHECK_INTERVAL_SECONDS)

                channel_id: Optional[int] = None
                new_name: str = ""
                reason: str = ""
                throttle_sleep = 0.0

                async with self._rename_lock:
                    if not self._rename_queue:
                        continue

                    time_since_last = time.monotonic() - self._last_rename_attempt_time
                    if time_since_last < settings.global_rename_throttle_seconds:
                        throttle_sleep = settings.global_rename_throttle_seconds - time_since_last
                    else:
                        channel_id, new_name, reason = self._rename_queue.popleft()

                if throttle_sleep:
                    logger.debug("Rename Queue (Local): Global throttle active. Sleeping for %.1fs.", throttle_sleep)
                    await asyncio.sleep(throttle_sleep)
                    continue

                assert channel_id is not None  # For type checkers; guarded by queue check above
                channel = self.bot.get_channel(channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    logger.warning("Rename Queue (Local): Channel %s not found or not a voice channel. Skipping.", channel_id)
                    continue

                if channel.name == new_name:
                    logger.debug("Rename Queue (Local): Channel %s already has desired name. Skipping.", channel.name)
                    self._last_rename_attempt_time = time.monotonic()
                    continue

                try:
                    await channel.edit(name=new_name, reason=reason)
                    logger.info("Channel renamed (Local): %s -> %s (Reason: %s)", channel.name, new_name, reason)
                    self._last_rename_attempt_time = time.monotonic()
                except discord.HTTPException as e:
                    if e.status == 429:
                        retry_after = float(e.headers.get("Retry-After", 1.0))
                        logger.warning(
                            "Rename Queue (Local): Rate limit hit for channel %s. Retrying in %.1fs.",
                            channel.name,
                            retry_after,
                        )
                        async with self._rename_lock:
                            self._rename_queue.appendleft((channel_id, new_name, reason))
                        await asyncio.sleep(retry_after)
                    else:
                        logger.error("Rename Queue (Local): Failed to rename channel %s to %s: %s", channel.name, new_name, e)
                except Exception as e:
                    logger.error(
                        "Rename Queue (Local): Unexpected error renaming channel %s to %s: %s",
                        channel.name,
                        new_name,
                        e,
                        exc_info=True,
                    )
            except asyncio.CancelledError:
                logger.info("Rename queue processor (Local) cancelled.")
                break
            except Exception as e:
                logger.error("Rename queue processor (Local) crashed: %s", e, exc_info=True)
                await asyncio.sleep(ERROR_BACKOFF_SECONDS)

        logger.info("Rename queue processor (Local) stopped.")

    def queue_local_rename_request(self, channel_id: int, new_name: str, reason: str = "Automated Rename"):
        async def _add_to_queue():
            async with self._rename_lock:
                new_queue = deque()
                for cid, name_in_queue, rsn_in_queue in self._rename_queue:
                    if cid == channel_id:
                        if name_in_queue == new_name:
                            logger.debug("Rename Queue: Channel %s zu '%s' bereits in Queue. Ignoriere neue Anfrage.", channel_id, new_name)
                            return
                        logger.debug(
                            "Rename Queue: ueberschreibe alte Anfrage fuer Channel %s ('%s' -> '%s').",
                            channel_id,
                            name_in_queue,
                            new_name,
                        )
                    else:
                        new_queue.append((cid, name_in_queue, rsn_in_queue))

                self._rename_queue = new_queue
                self._rename_queue.append((channel_id, new_name, reason))
                logger.debug("Rename Queue: Anfrage fuer Channel %s zu '%s' hinzugefuegt/aktualisiert.", channel_id, new_name)

        self.bot.loop.create_task(_add_to_queue())

async def setup(bot: commands.Bot):
    await bot.add_cog(RenameManagerCog(bot))
