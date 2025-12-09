"""Automatic Build Publisher Cog.

This cog acts as a worker that processes the hero_build_clones queue:
- Picks up pending clones from the database
- Creates BUILD_PUBLISH tasks for the Steam bridge
- Monitors and updates clone status based on task results
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Dict, Optional

from discord.ext import commands

from service import db

log = logging.getLogger(__name__)


class BuildPublisher(commands.Cog):
    """Worker that publishes hero builds via the Steam bridge."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.enabled = True  # ENABLED - Clean start with strict schema
        self.interval_seconds = 10 * 60  # 10 minutes
        self.monitor_interval_seconds = 2 * 60  # 2 minutes for task monitoring
        self.max_attempts = 3
        self.batch_size = 5  # Process max 5 builds per run
        self.wait_for_gc_ready = True  # Wait for GC instead of skipping

        self._publisher_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self.last_run_ts: Optional[int] = None
        self.last_error: Optional[str] = None
        self.consecutive_skips = 0  # Track how many times we skipped due to GC not ready

        if self.enabled:
            self._publisher_task = bot.loop.create_task(self._publisher_loop())
            self._monitor_task = bot.loop.create_task(self._monitor_loop())
            log.info(
                "Build publisher enabled (publish_interval=%ss, monitor_interval=%ss, max_attempts=%s, batch=%s)",
                self.interval_seconds,
                self.monitor_interval_seconds,
                self.max_attempts,
                self.batch_size,
            )

    def cog_unload(self) -> None:
        if self._publisher_task:
            self._publisher_task.cancel()
        if self._monitor_task:
            self._monitor_task.cancel()

    async def _publisher_loop(self) -> None:
        """Main publisher loop - creates BUILD_PUBLISH tasks."""
        await self.bot.wait_until_ready()
        # Wait 30s after bot ready to ensure Steam bridge is up
        await asyncio.sleep(30)

        while not self.bot.is_closed():
            try:
                await self.process_queue(triggered_by="auto")
            except Exception:
                log.exception("Build publisher run failed")
            await asyncio.sleep(self.interval_seconds)

    async def _monitor_loop(self) -> None:
        """Monitor loop - checks task completion and updates clone status."""
        await self.bot.wait_until_ready()
        # Wait 60s after bot ready to give publisher a head start
        await asyncio.sleep(60)

        while not self.bot.is_closed():
            try:
                await self.monitor_tasks()
            except Exception:
                log.exception("Build monitor run failed")
            await asyncio.sleep(self.monitor_interval_seconds)

    async def process_queue(self, *, triggered_by: str = "manual") -> Dict[str, int]:
        """Process pending builds in the queue."""
        if not self.enabled:
            return {"skipped": 1}

        async with self._lock:
            stats = {"checked": 0, "queued": 0, "skipped": 0, "errors": 0, "cancelled_excess": 0}
            # Check if Steam bridge is ready
            conn = db.connect()
            cursor = conn.execute("""
                SELECT payload FROM standalone_bot_state
                WHERE bot='steam' LIMIT 1
            """)
            state_row = cursor.fetchone()

            if state_row:
                payload = json.loads(state_row["payload"]) if state_row["payload"] else {}
                # Extract runtime state from nested payload structure
                runtime = payload.get("runtime", {})
                logged_in = runtime.get("logged_on", False)
                gc_ready = runtime.get("deadlock_gc_ready", False)

                if not logged_in:
                    self.consecutive_skips += 1
                    log.warning(
                        "Build publisher skipped: Steam not logged in (skip #%s)",
                        self.consecutive_skips
                    )
                    stats["skipped"] = 1
                    return stats

                if not gc_ready:
                    self.consecutive_skips += 1
                    skip_duration_min = self.consecutive_skips * (self.interval_seconds / 60)
                    log.warning(
                        "Build publisher skipped: Deadlock GC not ready (skip #%s, total: %.0f min, waiting for handshake)",
                        self.consecutive_skips,
                        skip_duration_min
                    )
                    stats["skipped"] = 1
                    # Log error if GC not ready for too long
                    if self.consecutive_skips >= 6:  # 60 minutes
                        log.error(
                            "Build publisher: GC not ready for %s intervals (%.0f min). Check Steam bridge status!",
                            self.consecutive_skips,
                            skip_duration_min
                        )
                    return stats

            # Reset skip counter on successful check
            if self.consecutive_skips > 0:
                log.info("GC is now ready, resuming build publishing after %s skips", self.consecutive_skips)
            self.consecutive_skips = 0

            try:
                # Get pending clones, applying the 3-build-per-hero rule based on priority
                cursor = conn.execute(
                    """
                    WITH RankedClones AS (
                        SELECT
                            hbc.origin_hero_build_id,
                            hbc.hero_id,
                            hbc.target_language,
                            hbc.target_name,
                            hbc.target_description,
                            hbc.attempts,
                            hbs.publish_ts, -- Needed for sorting
                            wba.priority,   -- Needed for sorting
                            hbc.created_at, -- Also needed for final order
                            ROW_NUMBER() OVER(PARTITION BY hbc.hero_id ORDER BY COALESCE(wba.priority, 0) ASC, hbs.publish_ts DESC, hbc.created_at ASC) as rn
                        FROM hero_build_clones hbc
                        INNER JOIN hero_build_sources hbs ON hbc.origin_hero_build_id = hbs.hero_build_id
                        LEFT JOIN watched_build_authors wba ON hbs.author_account_id = wba.author_account_id
                        WHERE hbc.status = 'pending'
                          AND hbc.attempts < ?
                    )
                    SELECT origin_hero_build_id, hero_id, target_language,
                           target_name, target_description, attempts
                    FROM RankedClones
                    WHERE rn <= 3
                    ORDER BY created_at ASC -- Process oldest valid tasks first
                    LIMIT ?
                    """,
                    (self.max_attempts, self.batch_size),
                )
                rows = cursor.fetchall()
                stats["checked"] = len(rows)

                for row in rows:
                    origin_id = row["origin_hero_build_id"]
                    try:
                        # Create BUILD_PUBLISH task
                        payload = {
                            "origin_hero_build_id": origin_id,
                            "target_name": row["target_name"],
                            "target_description": row["target_description"],
                            "target_language": row["target_language"],
                            # Use minimal mode for first attempt, full mode for retries
                            "minimal": row["attempts"] == 0,
                        }

                        task_cursor = conn.execute(
                            "INSERT INTO steam_tasks(type, payload, status) VALUES(?, ?, 'PENDING')",
                            ("BUILD_PUBLISH", json.dumps(payload)),
                        )
                        task_id = task_cursor.lastrowid

                        # Update clone status
                        conn.execute(
                            """
                            UPDATE hero_build_clones
                            SET status = 'processing',
                                last_attempt_at = ?,
                                attempts = attempts + 1,
                                status_info = ?
                            WHERE origin_hero_build_id = ?
                              AND target_language = ?
                            """,
                            (
                                int(time.time()),
                                f"Task #{task_id} created",
                                origin_id,
                                row["target_language"],
                            ),
                        )
                        # Autocommit mode - no explicit commit needed

                        stats["queued"] += 1
                        log.info(
                            "Created BUILD_PUBLISH task #%s for build %s (hero=%s, attempts=%s)",
                            task_id,
                            origin_id,
                            row["hero_id"],
                            row["attempts"] + 1,
                        )
                    except Exception as exc:
                        log.exception("Failed to create task for build %s", origin_id)
                        stats["errors"] += 1
                        # Mark as failed if max attempts reached
                        if row["attempts"] >= self.max_attempts - 1:
                            conn.execute(
                                """
                                UPDATE hero_build_clones
                                SET status = 'failed',
                                    status_info = ?
                                WHERE origin_hero_build_id = ?
                                  AND target_language = ?
                                """,
                                (
                                    f"Failed to create task: {str(exc)[:500]}",
                                    origin_id,
                                    row["target_language"],
                                ),
                            )
                            # conn.commit() removed - db.py uses autocommit mode (isolation_level=None)

                # NOTE: Do NOT close the connection - it's the global shared connection
                # managed by service/db.py. Closing it breaks all other cogs!
                # conn.close()  # ❌ REMOVED

                # After processing, mark any remaining 'pending' builds that are not in the top 3 as cancelled.
                # This ensures the DB is cleaned of old, irrelevant pending tasks.
                # This query uses a CTE to rank all pending builds and updates those with rank > 3.
                cancel_cursor = conn.execute(
                    """
                    WITH RankedPendingClones AS (
                        SELECT
                            hbc.ROWID,
                            ROW_NUMBER() OVER(PARTITION BY hbc.hero_id ORDER BY COALESCE(wba.priority, 0) ASC, hbs.publish_ts DESC, hbc.created_at ASC) as rn
                        FROM hero_build_clones hbc
                        INNER JOIN hero_build_sources hbs ON hbc.origin_hero_build_id = hbs.hero_build_id
                        LEFT JOIN watched_build_authors wba ON hbs.author_account_id = wba.author_account_id
                        WHERE hbc.status = 'pending'
                          AND hbc.attempts < ?
                    )
                    UPDATE hero_build_clones
                    SET status = 'cancelled',
                        status_info = 'Cancelled: Excluded by 3-build-per-hero rule based on priority/recency.'
                    WHERE ROWID IN (SELECT ROWID FROM RankedPendingClones WHERE rn > 3);
                    """,
                    (self.max_attempts,)
                )
                stats["cancelled_excess"] = cancel_cursor.rowcount
                if stats["cancelled_excess"] > 0:
                    log.info("Build publisher: Cancelled %s excess pending builds due to 3-build-per-hero rule.", stats["cancelled_excess"])

                self.last_run_ts = int(time.time())
                self.last_error = None
                db.set_kv("build_publisher", "last_run_ts", str(self.last_run_ts))
                db.set_kv("build_publisher", "last_run_stats", json.dumps(stats))
                db.set_kv("build_publisher", "last_run_trigger", triggered_by)

                if stats["queued"] > 0 or stats["errors"] > 0:
                    log.info(
                        "Build publisher run completed: %s queued, %s errors, %s checked",
                        stats["queued"],
                        stats["errors"],
                        stats["checked"],
                    )

            except Exception as exc:
                self.last_error = str(exc)
                db.set_kv("build_publisher", "last_error", self.last_error)
                log.exception("Build publisher run failed")
                raise

            return stats

    async def monitor_tasks(self) -> Dict[str, int]:
        """Monitor running BUILD_PUBLISH tasks and update clone status."""
        stats = {"checked": 0, "completed": 0, "failed": 0, "reset_stale": 0}

        conn = db.connect()

        # First, reset stale processing builds (stuck for > 30 minutes)
        stale_threshold = int(time.time()) - (30 * 60)
        stale_cursor = conn.execute(
            """
            UPDATE hero_build_clones
            SET status = 'pending',
                status_info = 'Reset: stuck in processing for >30min',
                attempts = CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END
            WHERE status = 'processing'
              AND last_attempt_at < ?
              AND last_attempt_at IS NOT NULL
            """,
            (stale_threshold,),
        )
        stats["reset_stale"] = stale_cursor.rowcount
        if stats["reset_stale"] > 0:
            log.warning(
                "Reset %s stale builds stuck in processing state",
                stats["reset_stale"]
            )
            # Autocommit mode - no explicit commit needed

        # Find processing clones with completed tasks
        cursor = conn.execute(
            """
            SELECT c.origin_hero_build_id, c.target_language, c.status_info,
                   t.status as task_status, t.result, t.error, t.id as task_id
            FROM hero_build_clones c
            INNER JOIN steam_tasks t ON (
                t.type = 'BUILD_PUBLISH'
                AND json_extract(t.payload, '$.origin_hero_build_id') = c.origin_hero_build_id
            )
            WHERE c.status = 'processing'
              AND t.status IN ('DONE', 'FAILED')
            ORDER BY t.finished_at DESC
            """
        )
        rows = cursor.fetchall()
        stats["checked"] = len(rows)

        for row in rows:
            origin_id = row["origin_hero_build_id"]
            task_status = row["task_status"]

            if task_status == "DONE":
                # Parse result
                result = json.loads(row["result"]) if row["result"] else {}
                uploaded_id = result.get("response", {}).get("hero_build_id")
                version = result.get("response", {}).get("version")

                conn.execute(
                    """
                    UPDATE hero_build_clones
                    SET status = 'uploaded',
                        uploaded_build_id = ?,
                        uploaded_version = ?,
                        status_info = ?,
                        updated_at = ?
                    WHERE origin_hero_build_id = ?
                      AND target_language = ?
                    """,
                    (
                        uploaded_id,
                        version,
                        f"Published as build #{uploaded_id} v{version}",
                        int(time.time()),
                        origin_id,
                        row["target_language"],
                    ),
                )
                stats["completed"] += 1
                log.info("Build %s published successfully as #%s", origin_id, uploaded_id)

            elif task_status == "FAILED":
                error_msg = row["error"] or "Unknown error"
                conn.execute(
                    """
                    UPDATE hero_build_clones
                    SET status = 'failed',
                        status_info = ?,
                        updated_at = ?
                    WHERE origin_hero_build_id = ?
                      AND target_language = ?
                    """,
                    (
                        f"Task #{row['task_id']} failed: {error_msg[:500]}",
                        int(time.time()),
                        origin_id,
                        row["target_language"],
                    ),
                )
                stats["failed"] += 1
                log.warning("Build %s publishing failed: %s", origin_id, error_msg[:100])

        # conn.commit() removed - db.py uses autocommit mode (isolation_level=None)
        # NOTE: Do NOT close the connection - it's the global shared connection
        # managed by service/db.py. Closing it breaks all other cogs!
        # conn.close()  # ❌ REMOVED

        if stats["completed"] > 0 or stats["failed"] > 0:
            log.info(
                "Task monitor: %s completed, %s failed, %s checked",
                stats["completed"],
                stats["failed"],
                stats["checked"],
            )

        return stats


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BuildPublisher(bot))
