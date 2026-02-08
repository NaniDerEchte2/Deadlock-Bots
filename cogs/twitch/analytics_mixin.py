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
    Mixin for periodic analytics collection (Subs, etc.).
    Requires authorized OAuth tokens (channel:read:subscriptions).
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

        log.info("Starting analytics collection (Subs)...")
        
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

        count = 0
        for row in rows:
            user_id = row[0] if not hasattr(row, "keys") else row["twitch_user_id"]
            login = row[1] if not hasattr(row, "keys") else row["twitch_login"]
            
            # Use RaidBot's auth manager to get a fresh token if possible
            if not getattr(self, "_raid_bot", None):
                continue
                
            session = self.api.get_http_session()
            token = await self._raid_bot.auth_manager.get_valid_token(user_id, session)
            
            if not token:
                log.debug("No valid token for %s, skipping analytics", login)  # nosemgrep
                continue

            # Check if token has the required scope for subs
            scopes = self._raid_bot.auth_manager.get_scopes(user_id)
            if "channel:read:subscriptions" not in scopes:
                log.debug("Token for %s missing 'channel:read:subscriptions' scope, skipping subs", login)  # nosemgrep
                continue

            try:
                await self._collect_subs_for_user(user_id, login, token)
                count += 1
            except Exception:
                log.exception("Failed to collect analytics for %s", login)
                
            # Sleep to be nice to the API
            await asyncio.sleep(2)

        log.info("Analytics collection finished. Processed %d users.", count)

    async def _collect_subs_for_user(self, user_id: str, login: str, token: str):
        """Fetch and store subscription data."""
        data = await self.api.get_broadcaster_subscriptions(user_id, token)
        if not data:
            return

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

    @collect_analytics_data.before_loop
    async def _before_analytics(self):
        await self.bot.wait_until_ready()
