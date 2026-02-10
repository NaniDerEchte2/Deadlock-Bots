"""Analytics background tasks for Twitch."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from discord.ext import tasks

from . import storage

log = logging.getLogger("TwitchStreams.Analytics")


class TwitchAnalyticsMixin:
    """
    Mixin for periodic analytics collection (Subs, Ads, etc.).
    Requires authorized OAuth tokens and matching scopes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._analytics_task = self.collect_analytics_data.start()

    async def cog_unload(self):
        await super().cog_unload()
        self.collect_analytics_data.cancel()

    @tasks.loop(hours=6)
    async def collect_analytics_data(self):
        """
        Periodically collect analytics data for authorized streamers.
        Runs every 6 hours to avoid API spam, as these numbers don't change extremely fast.
        """
        if not self.api:
            return

        try:
            await self.bot.wait_until_ready()
        except Exception:
            return

        log.info("Starting analytics collection (Subs + Ads)...")
        
        # Get authorized users with raid_enabled=1 (assuming they granted scopes)
        # Note: We should actually check if they have the specific scope, 
        # but for now we assume the new scope set is used if they re-authed.
        try:
            with storage.get_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT twitch_user_id, twitch_login
                    FROM twitch_raid_auth
                    WHERE raid_enabled = 1
                    """
                ).fetchall()
        except Exception:
            log.exception("Failed to load authorized users for analytics")
            return

        users_processed = 0
        subs_snapshots = 0
        ads_snapshots = 0
        for row in rows:
            user_id = row[0] if not hasattr(row, "keys") else row["twitch_user_id"]
            login = row[1] if not hasattr(row, "keys") else row["twitch_login"]
            
            # Use RaidBot's auth manager to get a fresh token if possible
            if not getattr(self, "_raid_bot", None):
                continue
                
            session = self.api.get_http_session()
            token = await self._raid_bot.auth_manager.get_valid_token(user_id, session)
            
            if not token:
                log.debug("Skipping analytics collection: no valid authorization available.")
                continue

            scopes = {s.lower() for s in self._raid_bot.auth_manager.get_scopes(user_id)}
            did_collect_for_user = False

            try:
                if "channel:read:subscriptions" in scopes:
                    if await self._collect_subs_for_user(user_id, login, token):
                        subs_snapshots += 1
                        did_collect_for_user = True

                if "channel:read:ads" in scopes:
                    if await self._collect_ads_schedule_for_user(user_id, login, token):
                        ads_snapshots += 1
                        did_collect_for_user = True
            except Exception:
                log.exception("Failed to collect analytics for %s", login)

            if did_collect_for_user:
                users_processed += 1
                # Sleep to be nice to the API
                await asyncio.sleep(2)
            else:
                log.debug(
                    "Skipping analytics metrics for %s: missing scopes (need channel:read:subscriptions and/or channel:read:ads).",
                    login,
                )

        log.info(
            "Analytics collection finished. users=%d, subs_snapshots=%d, ads_snapshots=%d",
            users_processed,
            subs_snapshots,
            ads_snapshots,
        )

    async def _collect_subs_for_user(self, user_id: str, login: str, token: str) -> bool:
        """Fetch and store subscription data."""
        data = await self.api.get_broadcaster_subscriptions(user_id, token)
        if not data:
            return False

        total = int(data.get("total", 0))
        points = int(data.get("points", 0))
        
        # Determine breakdown from 'data' list if available (depends on API response pagination,
        # usually getting 'total' is enough for the headline number. 
        # Detailed breakdown per tier might require iterating all pages which is expensive.
        # For now, we store total and points.
        
        # Twitch API /subscriptions returns a list of sub objects. 
        # "total" field in the response represents the total number of subscriptions.
        # "points" is also returned in the response root.
        
        # We can try to approximate tiers if we only fetch the first page, but 'total' is exact.
        
        now_iso = datetime.now(timezone.utc).isoformat()
        
        with storage.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO twitch_subscriptions_snapshot
                (twitch_user_id, twitch_login, total, points, snapshot_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, login, total, points, now_iso)
            )
        return True

    async def _collect_ads_schedule_for_user(self, user_id: str, login: str, token: str) -> bool:
        """Fetch and store ad schedule data."""
        data = await self.api.get_ad_schedule(user_id, token)
        if not data:
            return False

        def _safe_int(value):
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def _safe_time_text(value):
            if value is None:
                return None
            if isinstance(value, str):
                return value.strip() or None
            if isinstance(value, (int, float)):
                ts = float(value)
                if ts <= 0:
                    return None
                # Some APIs occasionally return milliseconds; normalize to seconds.
                if ts > 10_000_000_000:
                    ts = ts / 1000.0
                try:
                    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                except (OverflowError, OSError, ValueError):
                    return str(int(ts))
            text = str(value).strip()
            return text or None

        now_iso = datetime.now(timezone.utc).isoformat()
        next_ad_at = _safe_time_text(data.get("next_ad_at"))
        last_ad_at = _safe_time_text(data.get("last_ad_at"))
        duration = _safe_int(data.get("duration"))
        preroll_free_time = _safe_int(data.get("preroll_free_time"))
        snooze_count = _safe_int(data.get("snooze_count"))
        snooze_refresh_at = _safe_time_text(data.get("snooze_refresh_at"))

        with storage.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO twitch_ads_schedule_snapshot
                (
                    twitch_user_id, twitch_login, next_ad_at, last_ad_at,
                    duration, preroll_free_time, snooze_count, snooze_refresh_at, snapshot_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    login,
                    next_ad_at,
                    last_ad_at,
                    duration,
                    preroll_free_time,
                    snooze_count,
                    snooze_refresh_at,
                    now_iso,
                ),
            )
        return True

    @collect_analytics_data.before_loop
    async def _before_analytics(self):
        await self.bot.wait_until_ready()
