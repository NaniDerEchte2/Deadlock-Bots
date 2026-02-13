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

    async def _handle_stream_online(self, broadcaster_user_id: str, broadcaster_login: str, event: dict) -> None:
        """Wird von stream.online EventSub aufgerufen – triggert sofort den Go-Live-Handler."""
        handler = getattr(self, "_handle_stream_went_live", None)
        if callable(handler):
            log.info(
                "EventSub stream.online: %s (%s) ist live – triggere Go-Live-Handler",
                broadcaster_login or broadcaster_user_id,
                broadcaster_user_id,
            )
            await handler(broadcaster_user_id, broadcaster_login)

    async def _handle_channel_update(self, broadcaster_user_id: str, event: dict) -> None:
        """Speichert eine channel.update Notification (Titel/Game-Änderung) in der DB."""
        title = (event.get("title") or "").strip() or None
        game_name = (event.get("category_name") or event.get("game_name") or "").strip() or None
        language = (event.get("broadcaster_language") or "").strip() or None
        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_channel_updates (twitch_user_id, title, game_name, language, recorded_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        broadcaster_user_id,
                        title,
                        game_name,
                        language,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
                # Auch twitch_live_state aktualisieren, falls Stream gerade läuft
                c.execute(
                    """
                    UPDATE twitch_live_state
                       SET last_title = COALESCE(?, last_title),
                           last_game  = COALESCE(?, last_game)
                     WHERE twitch_user_id = ? AND is_live = 1
                    """,
                    (title, game_name, broadcaster_user_id),
                )
        except Exception:
            log.exception("_handle_channel_update: Fehler für %s", broadcaster_user_id)

    async def _store_subscription_event(
        self, broadcaster_user_id: str, event: dict, event_type: str
    ) -> None:
        """Speichert channel.subscribe / channel.subscription.gift / channel.subscription.message."""
        user_login = (
            event.get("user_login") or event.get("user_name") or ""
        ).strip().lower() or None
        tier = (event.get("tier") or "1000").strip()
        is_gift = int(bool(event.get("is_gift")))
        gifter_login = (event.get("gifter_login") or event.get("gifter_user_login") or "").strip().lower() or None
        cumulative_months = int(event.get("cumulative_months") or event.get("months") or 0) or None
        streak_months = int(event.get("streak_months") or 0) or None
        message_data = event.get("message") or {}
        if isinstance(message_data, dict):
            message = (message_data.get("text") or "").strip() or None
        else:
            message = str(message_data).strip() or None
        total_gifted = int(event.get("total") or 0) or None

        session_id: int | None = None
        try:
            with storage.get_conn() as c:
                row = c.execute(
                    """
                    SELECT id FROM twitch_stream_sessions
                     WHERE twitch_user_id = ? AND ended_at IS NULL
                     ORDER BY started_at DESC LIMIT 1
                    """,
                    (broadcaster_user_id,),
                ).fetchone()
            if row:
                session_id = int(row[0] if not hasattr(row, "keys") else row["id"])
        except Exception:
            pass

        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_subscription_events
                        (session_id, twitch_user_id, event_type, user_login, tier,
                         is_gift, gifter_login, cumulative_months, streak_months,
                         message, total_gifted, received_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id, broadcaster_user_id, event_type, user_login, tier,
                        is_gift, gifter_login, cumulative_months, streak_months,
                        message, total_gifted,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
        except Exception:
            log.exception("_store_subscription_event: Fehler für %s (%s)", broadcaster_user_id, event_type)

    async def _store_ad_break_event(self, broadcaster_user_id: str, event: dict) -> None:
        """Speichert ein channel.ad_break.begin Event."""
        duration_seconds = int(event.get("duration_seconds") or 0) or None
        is_automatic = int(bool(event.get("is_automatic")))

        session_id: int | None = None
        try:
            with storage.get_conn() as c:
                row = c.execute(
                    """
                    SELECT id FROM twitch_stream_sessions
                     WHERE twitch_user_id = ? AND ended_at IS NULL
                     ORDER BY started_at DESC LIMIT 1
                    """,
                    (broadcaster_user_id,),
                ).fetchone()
            if row:
                session_id = int(row[0] if not hasattr(row, "keys") else row["id"])
        except Exception:
            pass

        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_ad_break_events
                        (session_id, twitch_user_id, duration_seconds, is_automatic, started_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session_id, broadcaster_user_id, duration_seconds, is_automatic,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
        except Exception:
            log.exception("_store_ad_break_event: Fehler für %s", broadcaster_user_id)

    async def _store_bits_event(self, broadcaster_user_id: str, event: dict) -> None:
        """Speichert ein channel.cheer (Bits) Event in der Datenbank."""
        donor_login = (event.get("user_login") or event.get("user_name") or "").strip().lower() or None
        amount = int(event.get("bits") or event.get("amount") or 0)
        message = (event.get("message") or "").strip() or None
        if not amount:
            return
        # Session ID für den aktuellen Stream bestimmen (optional)
        session_id: int | None = None
        try:
            with storage.get_conn() as c:
                row = c.execute(
                    """
                    SELECT id FROM twitch_stream_sessions
                     WHERE twitch_user_id = ?
                       AND ended_at IS NULL
                     ORDER BY started_at DESC LIMIT 1
                    """,
                    (broadcaster_user_id,),
                ).fetchone()
            if row:
                session_id = int(row[0] if not hasattr(row, "keys") else row["id"])
        except Exception:
            log.debug("_store_bits_event: Konnte session_id nicht ermitteln", exc_info=True)

        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_bits_events
                        (session_id, twitch_user_id, donor_login, amount, message, received_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        broadcaster_user_id,
                        donor_login,
                        amount,
                        message,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
        except Exception:
            log.exception("_store_bits_event: Fehler beim Speichern für %s", broadcaster_user_id)

    async def _store_hype_train_event(
        self, broadcaster_user_id: str, event: dict, *, ended: bool
    ) -> None:
        """Speichert ein channel.hype_train.begin/end Event in der Datenbank."""
        started_at = (event.get("started_at") or "").strip() or None
        ended_at = (event.get("ended_at") or "").strip() or None if ended else None
        level = int(event.get("level") or 0) or None
        total_progress = int(event.get("total") or event.get("total_progress") or 0) or None
        duration_seconds: int | None = None
        if started_at and ended_at:
            try:
                from datetime import datetime as _dt
                dt_start = _dt.fromisoformat(started_at.replace("Z", "+00:00"))
                dt_end = _dt.fromisoformat(ended_at.replace("Z", "+00:00"))
                duration_seconds = max(0, int((dt_end - dt_start).total_seconds()))
            except Exception:
                pass

        session_id: int | None = None
        try:
            with storage.get_conn() as c:
                row = c.execute(
                    """
                    SELECT id FROM twitch_stream_sessions
                     WHERE twitch_user_id = ?
                       AND ended_at IS NULL
                     ORDER BY started_at DESC LIMIT 1
                    """,
                    (broadcaster_user_id,),
                ).fetchone()
            if row:
                session_id = int(row[0] if not hasattr(row, "keys") else row["id"])
        except Exception:
            log.debug("_store_hype_train_event: Konnte session_id nicht ermitteln", exc_info=True)

        try:
            with storage.get_conn() as c:
                if ended:
                    # Versuche, ein bereits vorhandenes begin-Event zu aktualisieren
                    updated = c.execute(
                        """
                        UPDATE twitch_hype_train_events
                           SET ended_at = ?,
                               duration_seconds = ?,
                               level = COALESCE(?, level),
                               total_progress = COALESCE(?, total_progress)
                         WHERE twitch_user_id = ?
                           AND started_at = ?
                           AND ended_at IS NULL
                        """,
                        (ended_at, duration_seconds, level, total_progress, broadcaster_user_id, started_at),
                    ).rowcount
                    if updated:
                        return
                c.execute(
                    """
                    INSERT INTO twitch_hype_train_events
                        (session_id, twitch_user_id, started_at, ended_at,
                         duration_seconds, level, total_progress)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        broadcaster_user_id,
                        started_at,
                        ended_at,
                        duration_seconds,
                        level,
                        total_progress,
                    ),
                )
        except Exception:
            log.exception(
                "_store_hype_train_event: Fehler beim Speichern für %s", broadcaster_user_id
            )
