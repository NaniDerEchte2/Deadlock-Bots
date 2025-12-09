# cogs/rename_manager.py
from __future__ import annotations

import asyncio
import logging
import time
import contextlib
import datetime as dt # Added for datetime operations
from collections import deque
from typing import Optional, Tuple

import discord
from discord.ext import commands

from service.config import settings
from service import db

logger = logging.getLogger("RenameManagerCog")

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
        worker_id = 1 # Main Bot's local queue is worker ID 1
        
        # Helper to get/update global rename state atomically
        async def _get_and_update_global_rename_state_local():
            async with db.transaction():
                # Ensure the global state row exists
                await db.execute_async(
                    """
                    INSERT OR IGNORE INTO rename_global_state (id, last_rename_timestamp, next_worker_id)
                    VALUES (1, STRFTIME('%Y-%m-%d %H:%M:%S', 'now', '-5 minutes'), 1)
                    """
                )
                
                state_row = await db.query_one_async(
                    "SELECT last_rename_timestamp, next_worker_id FROM rename_global_state WHERE id = 1"
                )
                
                last_ts_str = state_row["last_rename_timestamp"]
                last_ts = time.mktime(dt.datetime.strptime(last_ts_str, '%Y-%m-%d %H:%M:%S').timetuple())
                next_worker = state_row["next_worker_id"]

                return last_ts, next_worker

        async def _set_global_rename_timestamp():
            await db.execute_async(
                "UPDATE rename_global_state SET last_rename_timestamp = STRFTIME('%Y-%m-%d %H:%M:%S', 'now') WHERE id = 1"
            )

        while not self.bot.is_closed():
            try:
                await asyncio.sleep(1) # Check queue every second

                # --- Global Throttle Check ---
                last_rename_time, next_worker_to_assign = await _get_and_update_global_rename_state_local()
                time_since_last_global_rename = time.time() - last_rename_time
                
                if time_since_last_global_rename < settings.global_rename_throttle_seconds:
                    sleep_for = settings.global_rename_throttle_seconds - time_since_last_global_rename
                    logger.debug(f"Rename Queue (Local): Global throttle active. Sleeping for {sleep_for:.1f}s.")
                    await asyncio.sleep(sleep_for)
                    continue # Re-check everything after sleep

                async with self._rename_lock:
                    if not self._rename_queue:
                        continue

                    # Only process if this worker is designated to process now, or if no DB worker is used
                    if settings.use_db_rename_worker and next_worker_to_assign != worker_id:
                        logger.debug(f"Rename Queue (Local): Not my turn. Next worker is {next_worker_to_assign}.")
                        # Sleep a bit to not busy-wait but also allow next worker to pick up
                        await asyncio.sleep(POLL_INTERVAL_SECONDS)
                        continue

                    channel_id, new_name, reason = self._rename_queue.popleft()
                    self._last_rename_attempt_time = time.monotonic() # Update last attempt time

                    channel = self.bot.get_channel(channel_id)
                    if not isinstance(channel, discord.VoiceChannel):
                        logger.warning(f"Rename Queue (Local): Channel {channel_id} not found or not a voice channel. Skipping.")
                        continue

                    if channel.name == new_name:
                        logger.debug(f"Rename Queue (Local): Channel {channel.name} already has desired name. Skipping.")
                        # This counts as processing, so update global throttle
                        await _set_global_rename_timestamp()
                        continue

                    try:
                        await channel.edit(name=new_name, reason=reason)
                        logger.info(f"Channel renamed (Local): {channel.name} -> {new_name} (Reason: {reason})")
                        await _set_global_rename_timestamp() # Update global throttle after success
                    except discord.HTTPException as e:
                        if e.status == 429: # Rate limit hit
                            retry_after = float(e.headers.get("Retry-After", 1.0))
                            logger.warning(f"Rename Queue (Local): Rate limit hit for channel {channel.name}. Retrying in {retry_after:.1f}s.")
                            self._rename_queue.appendleft((channel_id, new_name, reason)) # Put back at front
                            await asyncio.sleep(retry_after)
                        else:
                            logger.error(f"Rename Queue (Local): Failed to rename channel {channel.name} to {new_name}: {e}")
                    except Exception as e:
                        logger.error(f"Rename Queue (Local): Unexpected error renaming channel {channel.name} to {new_name}: {e}", exc_info=True)
            except asyncio.CancelledError:
                logger.info("Rename queue processor (Local) cancelled.")
                break
            except Exception as e:
                logger.error(f"Rename queue processor (Local) crashed: {e}", exc_info=True)
                await asyncio.sleep(POLL_INTERVAL_SECONDS * 2) # Longer sleep on crash

        logger.info("Rename queue processor (Local) stopped.")

    def queue_local_rename_request(self, channel_id: int, new_name: str, reason: str = "Automated Rename"):
        async def _add_to_queue():
            async with self._rename_lock:
                new_queue = deque()
                removed_existing = False
                for cid, name_in_queue, rsn_in_queue in self._rename_queue:
                    if cid == channel_id:
                        if name_in_queue == new_name:
                            logger.debug(f"Rename Queue: Channel {channel_id} zu '{new_name}' bereits in Queue. Ignoriere neue Anfrage.")
                            return # Exit if already in queue with same target name
                        logger.debug(f"Rename Queue: Überschreibe alte Anfrage für Channel {channel_id} ('{name_in_queue}' -> '{new_name}').")
                        removed_existing = True
                    else:
                        new_queue.append((cid, name_in_queue, rsn_in_queue))
                
                self._rename_queue = new_queue # Replace the queue with the cleaned version

                self._rename_queue.append((channel_id, new_name, reason))
                logger.debug(f"Rename Queue: Anfrage für Channel {channel_id} zu '{new_name}' hinzugefügt/aktualisiert.")
        
        # We need to schedule this coroutine, as queue_local_rename_request is called synchronously
        self.bot.loop.create_task(_add_to_queue())

async def setup(bot: commands.Bot):
    await bot.add_cog(RenameManagerCog(bot))

