"""
Background Clip Fetcher - Auto-Fetch Clips alle 6 Stunden.

Fetcht automatisch neueste Clips für verifizierte Partner und speichert
Fetch-History in der Datenbank.
"""

import asyncio
import logging
import time

from discord.ext import commands

from ..storage import get_conn
from .clip_manager import ClipManager

log = logging.getLogger("TwitchStreams.ClipFetcher")


class ClipFetcher(commands.Cog):
    """Background Worker für automatisches Clip-Fetching."""

    def __init__(self, bot, twitch_api, clip_manager: ClipManager):
        """
        Args:
            bot: Discord bot instance
            twitch_api: TwitchAPI instance
            clip_manager: ClipManager instance
        """
        self.bot = bot
        self.api = twitch_api
        self.clip_manager = clip_manager
        self.enabled = True
        self.interval_seconds = 6 * 60 * 60  # 6 hours
        self.fetch_days = 7  # Fetch clips from last 7 days
        self.clip_limit = 20  # Max clips per streamer

        self._task = bot.loop.create_task(self._fetch_loop())
        log.info(
            "ClipFetcher started (interval=%ss, days=%s, limit=%s)",
            self.interval_seconds,
            self.fetch_days,
            self.clip_limit,
        )

    def cog_unload(self):
        """Cleanup on cog unload."""
        if self._task:
            self._task.cancel()

    async def _fetch_loop(self):
        """Main fetch loop - runs every 6 hours."""
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)  # Initial delay of 1 minute

        while not self.bot.is_closed() and self.enabled:
            try:
                await self.fetch_all_streamers()
            except Exception:
                log.exception("Clip fetch run failed")

            # Wait for next run
            await asyncio.sleep(self.interval_seconds)

    async def fetch_all_streamers(self):
        """Fetch clips for all active, verified partner streamers."""
        stats = {
            "streamers": 0,
            "clips_total": 0,
            "clips_new": 0,
            "errors": 0,
            "duration_ms": 0,
        }

        start_time = time.time()

        try:
            # Get all active, verified partners (exclude monitored-only and opt-out)
            with get_conn() as conn:
                streamers = conn.execute(
                    """
                    SELECT twitch_login
                      FROM twitch_streamers_partner_state
                     WHERE is_partner_active = 1
                     ORDER BY twitch_login ASC
                    """
                ).fetchall()

            log.info("Starting clip fetch for %s partner streamers", len(streamers))

            # Fetch clips for each streamer
            for streamer_row in streamers:
                streamer = streamer_row["twitch_login"]

                try:
                    fetch_start = time.time()

                    # Fetch clips
                    clips = await self.clip_manager.fetch_recent_clips(
                        streamer_login=streamer,
                        limit=self.clip_limit,
                        days=self.fetch_days,
                    )

                    fetch_duration_ms = int((time.time() - fetch_start) * 1000)

                    # Count new clips (clips that were registered during this fetch)
                    clips_new = len([c for c in clips if c.get("db_id")])

                    # Record fetch history
                    with get_conn() as conn:
                        conn.execute(
                            "INSERT OR IGNORE INTO twitch_streamers (twitch_login) VALUES (?)",
                            (streamer,),
                        )
                        conn.execute(
                            """
                            INSERT INTO clip_fetch_history
                                (streamer_login, clips_found, clips_new, fetch_duration_ms)
                            VALUES (?, ?, ?, ?)
                            """,
                            (streamer, len(clips), clips_new, fetch_duration_ms),
                        )

                    stats["streamers"] += 1
                    stats["clips_total"] += len(clips)
                    stats["clips_new"] += clips_new

                    log.debug(
                        "Fetched clips for %s: %s found, %s new",
                        streamer,
                        len(clips),
                        clips_new,
                    )

                    # Rate limit: Wait 1 second between fetches
                    await asyncio.sleep(1)

                except Exception as e:
                    log.exception("Failed to fetch clips for %s", streamer)
                    stats["errors"] += 1

                    # Record error in fetch history
                    with get_conn() as conn:
                        conn.execute(
                            "INSERT OR IGNORE INTO twitch_streamers (twitch_login) VALUES (?)",
                            (streamer,),
                        )
                        conn.execute(
                            """
                            INSERT INTO clip_fetch_history
                                (streamer_login, clips_found, clips_new, error)
                            VALUES (?, 0, 0, ?)
                            """,
                            (streamer, str(e)),
                        )

            stats["duration_ms"] = int((time.time() - start_time) * 1000)

            log.info(
                "Clip fetch complete: %s streamers, %s clips total (%s new), %s errors, took %sms",
                stats["streamers"],
                stats["clips_total"],
                stats["clips_new"],
                stats["errors"],
                stats["duration_ms"],
            )

            return stats

        except Exception:
            log.exception("Clip fetch failed")
            return stats

    async def fetch_single_streamer(self, streamer_login: str) -> dict:
        """
        Fetch clips for a single streamer (manual trigger).

        Returns:
            Stats dict
        """
        try:
            start_time = time.time()

            clips = await self.clip_manager.fetch_recent_clips(
                streamer_login=streamer_login,
                limit=self.clip_limit,
                days=self.fetch_days,
            )

            duration_ms = int((time.time() - start_time) * 1000)
            clips_new = len([c for c in clips if c.get("db_id")])

            # Record fetch history
            with get_conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO twitch_streamers (twitch_login) VALUES (?)",
                    (streamer_login,),
                )
                conn.execute(
                    """
                    INSERT INTO clip_fetch_history
                        (streamer_login, clips_found, clips_new, fetch_duration_ms)
                    VALUES (?, ?, ?, ?)
                    """,
                    (streamer_login, len(clips), clips_new, duration_ms),
                )

            log.info(
                "Manual fetch for %s: %s found, %s new",
                streamer_login,
                len(clips),
                clips_new,
            )

            return {
                "streamer": streamer_login,
                "clips_found": len(clips),
                "clips_new": clips_new,
                "duration_ms": duration_ms,
            }

        except Exception:
            log.exception("Failed to fetch clips for %s", streamer_login)
            raise

    def get_fetch_history(
        self,
        streamer_login: str | None = None,
        limit: int = 50,
    ) -> list:
        """
        Get fetch history.

        Args:
            streamer_login: Optional filter by streamer
            limit: Max results

        Returns:
            List of fetch history entries
        """
        try:
            with get_conn() as conn:
                if streamer_login:
                    rows = conn.execute(
                        """
                        SELECT id, streamer_login, fetched_at, clips_found, clips_new,
                               fetch_duration_ms, error
                          FROM clip_fetch_history
                         WHERE streamer_login = ?
                         ORDER BY fetched_at DESC
                         LIMIT ?
                        """,
                        (streamer_login, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT id, streamer_login, fetched_at, clips_found, clips_new,
                               fetch_duration_ms, error
                          FROM clip_fetch_history
                         ORDER BY fetched_at DESC
                         LIMIT ?
                        """,
                        (limit,),
                    ).fetchall()

                return [dict(row) for row in rows]

        except Exception:
            log.exception("Failed to get fetch history")
            return []


async def setup(bot):
    """Setup function for Discord.py cog."""
    # This cog is loaded by TwitchCog, not directly
    pass
