# cogs/rename_manager.py
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Dict, Optional

import discord
from discord.ext import commands

from service import db
from service.config import settings

logger = logging.getLogger("RenameManagerCog")

QUEUE_CHECK_INTERVAL_SECONDS = 1.0
ERROR_BACKOFF_SECONDS = 10.0
# Discord allows channel renames only very sparsely in practice.
RENAME_THROTTLE_SECONDS = 360
MAX_RETRIES = 5


class RenameManagerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._rename_task: Optional[asyncio.Task[None]] = None
        self._last_rename_attempt_by_channel: Dict[int, float] = {}

    async def cog_load(self):
        await self._ensure_db_schema()
        await self._recover_stuck_requests()
        if settings.use_db_rename_worker:
            logger.info("RenameManagerCog loaded in enqueue-only mode (USE_DB_RENAME_WORKER=1).")
            return
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

    def _worker_id(self) -> int:
        try:
            if self.bot.user and self.bot.user.id:
                return int(self.bot.user.id)
        except Exception:
            logger.debug("RenameManagerCog: failed to resolve worker id from bot user", exc_info=True)
        return 1

    async def _ensure_db_schema(self) -> None:
        await db.execute_async(
            """
            CREATE TABLE IF NOT EXISTS rename_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                new_name TEXT NOT NULL,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                retry_count INTEGER DEFAULT 0,
                last_error TEXT,
                assigned_worker_id INTEGER DEFAULT 0
            )
            """
        )
        await db.execute_async(
            """
            CREATE TABLE IF NOT EXISTS rename_global_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_rename_timestamp DATETIME DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', '-5 minutes')),
                next_worker_id INTEGER DEFAULT 1
            )
            """
        )
        await db.execute_async(
            "INSERT OR IGNORE INTO rename_global_state(id) VALUES (1)"
        )
        await db.execute_async(
            "CREATE INDEX IF NOT EXISTS idx_rename_requests_status_created ON rename_requests(status, created_at, id)"
        )
        await db.execute_async(
            "CREATE INDEX IF NOT EXISTS idx_rename_requests_channel_status ON rename_requests(channel_id, status, id)"
        )

    async def _recover_stuck_requests(self) -> None:
        # Falls der Bot/Worker neu startet, hängende PROCESSING-Jobs wieder freigeben.
        await db.execute_async(
            """
            UPDATE rename_requests
            SET status='PENDING',
                assigned_worker_id=0,
                processed_at=NULL
            WHERE status='PROCESSING'
            """
        )

    async def _enqueue_request(self, channel_id: int, new_name: str, reason: str) -> None:
        channel_id = int(channel_id)
        new_name = str(new_name).strip()
        reason = str(reason or "Automated Rename").strip()
        if not new_name:
            return

        async with db.transaction() as conn:
            # Nur der neueste pending Wunsch pro Channel bleibt bestehen.
            conn.execute(
                "DELETE FROM rename_requests WHERE channel_id=? AND status='PENDING'",
                (channel_id,),
            )
            conn.execute(
                """
                INSERT INTO rename_requests(
                    channel_id, new_name, reason, status, created_at, processed_at, retry_count, last_error, assigned_worker_id
                )
                VALUES(?, ?, ?, 'PENDING', CURRENT_TIMESTAMP, NULL, 0, NULL, 0)
                """,
                (channel_id, new_name, reason),
            )

    async def _claim_next_request(self) -> Optional[Dict[str, object]]:
        async with db.transaction() as conn:
            row = conn.execute(
                """
                SELECT id, channel_id, new_name, COALESCE(reason, 'Automated Rename') AS reason, retry_count
                FROM rename_requests
                WHERE status='PENDING'
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None

            upd = conn.execute(
                """
                UPDATE rename_requests
                SET status='PROCESSING',
                    assigned_worker_id=?,
                    processed_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='PENDING'
                """,
                (self._worker_id(), int(row["id"])),
            )
            if upd.rowcount != 1:
                return None

            return {
                "id": int(row["id"]),
                "channel_id": int(row["channel_id"]),
                "new_name": str(row["new_name"]),
                "reason": str(row["reason"] or "Automated Rename"),
                "retry_count": int(row["retry_count"] or 0),
            }

    async def _set_request_pending(self, request_id: int, *, last_error: Optional[str], increment_retry: bool) -> None:
        if increment_retry:
            await db.execute_async(
                """
                UPDATE rename_requests
                SET status='PENDING',
                    created_at=CURRENT_TIMESTAMP,
                    processed_at=NULL,
                    assigned_worker_id=0,
                    retry_count=retry_count+1,
                    last_error=?
                WHERE id=?
                """,
                (last_error, int(request_id)),
            )
            return

        await db.execute_async(
            """
            UPDATE rename_requests
            SET status='PENDING',
                processed_at=NULL,
                assigned_worker_id=0,
                last_error=?
            WHERE id=?
            """,
            (last_error, int(request_id)),
        )

    async def _set_request_done(self, request_id: int) -> None:
        await db.execute_async(
            """
            UPDATE rename_requests
            SET status='DONE',
                processed_at=CURRENT_TIMESTAMP,
                assigned_worker_id=?,
                last_error=NULL
            WHERE id=?
            """,
            (self._worker_id(), int(request_id)),
        )

    async def _set_request_failed(self, request_id: int, *, last_error: str) -> None:
        await db.execute_async(
            """
            UPDATE rename_requests
            SET status='FAILED',
                processed_at=CURRENT_TIMESTAMP,
                assigned_worker_id=?,
                last_error=?
            WHERE id=?
            """,
            (self._worker_id(), last_error[:1000], int(request_id)),
        )

    async def _process_rename_queue(self):
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(QUEUE_CHECK_INTERVAL_SECONDS)
                request = await self._claim_next_request()
                if not request:
                    continue

                req_id = int(request["id"])  # type: ignore[index]
                channel_id = int(request["channel_id"])  # type: ignore[index]
                new_name = str(request["new_name"])  # type: ignore[index]
                reason = str(request["reason"])  # type: ignore[index]
                retry_count = int(request["retry_count"])  # type: ignore[index]

                last_attempt = self._last_rename_attempt_by_channel.get(channel_id, 0.0)
                elapsed = time.monotonic() - last_attempt
                if elapsed < RENAME_THROTTLE_SECONDS:
                    remaining = RENAME_THROTTLE_SECONDS - elapsed
                    await self._set_request_pending(
                        req_id,
                        last_error=f"Channel throttle active ({remaining:.1f}s remaining)",
                        increment_retry=False,
                    )
                    await asyncio.sleep(min(5.0, max(0.5, remaining)))
                    continue

                channel = self.bot.get_channel(channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    logger.warning("Rename Queue (DB): Channel %s not found or not a voice channel. Marking FAILED.", channel_id)
                    await self._set_request_failed(req_id, last_error="Channel not found or not voice")
                    continue

                if channel.name == new_name:
                    logger.debug("Rename Queue (DB): Channel %s already has desired name. Marking DONE.", channel.name)
                    await self._set_request_done(req_id)
                    continue

                try:
                    await channel.edit(name=new_name, reason=reason)
                    logger.info("Channel renamed (DB queue): %s -> %s (Reason: %s)", channel.name, new_name, reason)
                    self._last_rename_attempt_by_channel[channel_id] = time.monotonic()
                    await self._set_request_done(req_id)
                except discord.HTTPException as e:
                    if e.status == 429:
                        retry_after = float(e.headers.get("Retry-After", 1.0))
                        logger.warning(
                            "Rename Queue (DB): Rate limit hit for channel %s. Retrying in %.1fs.",
                            channel.name,
                            retry_after,
                        )
                        self._last_rename_attempt_by_channel[channel_id] = time.monotonic()
                        await self._set_request_pending(
                            req_id,
                            last_error=f"HTTP 429 (retry_after={retry_after})",
                            increment_retry=True,
                        )
                        await asyncio.sleep(retry_after)
                    else:
                        if retry_count + 1 >= MAX_RETRIES:
                            await self._set_request_failed(req_id, last_error=f"HTTP {e.status}: {e}")
                        else:
                            await self._set_request_pending(
                                req_id,
                                last_error=f"HTTP {e.status}: {e}",
                                increment_retry=True,
                            )
                except Exception as e:
                    if retry_count + 1 >= MAX_RETRIES:
                        await self._set_request_failed(req_id, last_error=f"Unexpected error: {e}")
                    else:
                        await self._set_request_pending(
                            req_id,
                            last_error=f"Unexpected error: {e}",
                            increment_retry=True,
                        )
            except asyncio.CancelledError:
                logger.info("Rename queue processor (DB) cancelled.")
                break
            except Exception as e:
                logger.error("Rename queue processor (DB) crashed: %s", e, exc_info=True)
                await asyncio.sleep(ERROR_BACKOFF_SECONDS)

        logger.info("Rename queue processor (DB) stopped.")

    def queue_local_rename_request(self, channel_id: int, new_name: str, reason: str = "Automated Rename"):
        self.bot.loop.create_task(self._enqueue_request(channel_id, new_name, reason))


async def setup(bot: commands.Bot):
    await bot.add_cog(RenameManagerCog(bot))
