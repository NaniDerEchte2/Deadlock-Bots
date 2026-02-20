"""Analytics background tasks for Twitch."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from discord.ext import tasks

from .. import storage

log = logging.getLogger("TwitchStreams.Analytics")


class TwitchAnalyticsMixin:
    """
    Mixin for periodic analytics collection (Subs, Ads, etc.).
    Requires authorized OAuth tokens and matching scopes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._analytics_task = self.collect_analytics_data.start()
        self._chatters_task = self.collect_chatters_data.start()

    async def cog_unload(self):
        await super().cog_unload()
        self.collect_analytics_data.cancel()
        self.collect_chatters_data.cancel()

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
                log.debug(
                    "Skipping analytics collection: no valid authorization available."
                )
                continue

            scopes = {
                s.lower() for s in self._raid_bot.auth_manager.get_scopes(user_id)
            }
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

    async def _collect_subs_for_user(
        self, user_id: str, login: str, token: str
    ) -> bool:
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
                (user_id, login, total, points, now_iso),
            )
        return True

    async def _collect_ads_schedule_for_user(
        self, user_id: str, login: str, token: str
    ) -> bool:
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

    # ------------------------------------------------------------------
    # Chatters Poller (alle 5 Min, nur für live Streams)
    # Tracked Lurker via GET /helix/chat/chatters (moderator:read:chatters)
    # ------------------------------------------------------------------

    async def _poll_chatters_single(
        self,
        user_id: str,
        login: str,
        session_id: int,
        now_iso: str,
        token: Optional[str] = None,
    ) -> "tuple[int, str, list[dict]] | None":
        """Pollt Chatters für einen Streamer via Helix API (nur wenn Token + moderator:read:chatters Scope vorhanden)."""
        chatters = []

        # 1. Versuch: Offizielle API mit Token (wenn vorhanden)
        if token:
            scopes = (
                {s.lower() for s in self._raid_bot.auth_manager.get_scopes(user_id)}
                if getattr(self, "_raid_bot", None)
                else set()
            )
            if "moderator:read:chatters" in scopes:
                try:
                    chatters = await self.api.get_chatters(
                        broadcaster_id=user_id,
                        moderator_id=user_id,
                        user_token=token,
                    )
                    log.debug(
                        "Chatters-Poller: %d Chatters via Helix API für %s",
                        len(chatters),
                        login,
                    )
                except Exception:
                    log.warning(
                        "Chatters-Poller: Helix API fehlgeschlagen für %s",
                        login,
                        exc_info=True,
                    )
            else:
                log.warning(
                    "Chatters-Poller: %s missing required 'moderator:read:chatters' scope. "
                    "Streamer must re-authorize.",
                    login,
                )
        if not chatters:
            return None

        log.debug(
            "Chatters-Poller: %d Chatters für %s (session %s)",
            len(chatters),
            login,
            session_id,
        )
        return (session_id, login, chatters)

    @tasks.loop(seconds=30)
    async def collect_chatters_data(self):
        """
        Pollt Chatters-Liste für ALLE live Streamer (Partner + Monitored + Category).

        WICHTIG: Datensammlung für Analyse läuft für ALLE.
        Bot-Funktionen (Raids, Commands, etc.) nur für Partner!
        """
        if not self.api:
            return

        try:
            with storage.get_conn() as conn:
                # Hole ALLE aktiven Sessions (Partner + Monitored + Category)
                rows = conn.execute(
                    """
                    SELECT ls.twitch_user_id, ls.streamer_login, ls.active_session_id, ra.twitch_user_id as has_auth
                    FROM twitch_live_state ls
                    LEFT JOIN twitch_raid_auth ra ON ra.twitch_user_id = ls.twitch_user_id AND ra.raid_enabled = 1
                    WHERE ls.is_live = 1
                      AND ls.active_session_id IS NOT NULL
                    """
                ).fetchall()

                if rows:
                    log.debug(
                        "Chatters-Poller: Tracking %d live Streamer (alle für Analyse)",
                        len(rows),
                    )
        except Exception:
            log.exception("Chatters-Poller: Fehler beim Laden der live Streamer")
            return

        if not rows:
            return

        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tasks_list = []

        # Token-Resolution vorbereiten (nur für Partner)
        session = self.api.get_http_session()
        auth_mgr = getattr(self, "_raid_bot", None) and getattr(
            self._raid_bot, "auth_manager", None
        )

        for row in rows:
            user_id = row[0] if not hasattr(row, "keys") else row["twitch_user_id"]
            login = row[1] if not hasattr(row, "keys") else row["streamer_login"]
            sess_id = row[2] if not hasattr(row, "keys") else row["active_session_id"]
            has_auth = row[3] if not hasattr(row, "keys") else row["has_auth"]

            async def _wrap_poll(u_id, lgn, s_id):
                tok = None
                if has_auth and auth_mgr:
                    tok = await auth_mgr.get_valid_token(u_id, session)
                return await self._poll_chatters_single(u_id, lgn, s_id, now_iso, tok)

            tasks_list.append(_wrap_poll(user_id, login, sess_id))

        # Alle API-Calls parallel feuern
        results = await asyncio.gather(*tasks_list, return_exceptions=True)

        # Batch-Write
        payloads = [r for r in results if isinstance(r, tuple)]
        if not payloads:
            return

        try:
            with storage.get_conn() as conn:
                for session_id, login, chatters in payloads:
                    # Nur last_seen_at für bereits bekannte Chatter aktualisieren.
                    # Neue Einträge kommen ausschließlich über _track_chat_health (IRC Bot).
                    to_update = []
                    for chatter in chatters:
                        c_login = (chatter.get("user_login") or "").lower().strip()
                        if c_login:
                            to_update.append((now_iso, session_id, c_login))

                    if to_update:
                        conn.executemany(
                            "UPDATE twitch_session_chatters SET last_seen_at = ? WHERE session_id = ? AND chatter_login = ?",
                            to_update,
                        )
        except Exception:
            log.exception("Chatters-Poller: Batch-DB-Fehler")

    @collect_chatters_data.before_loop
    async def _before_chatters(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    async def _handle_stream_online(
        self, broadcaster_user_id: str, broadcaster_login: str, event: dict
    ) -> None:
        """Wird von stream.online EventSub aufgerufen – triggert sofort den Go-Live-Handler."""
        handler = getattr(self, "_handle_stream_went_live", None)
        if callable(handler):
            log.info(
                "EventSub stream.online: %s (%s) ist live – triggere Go-Live-Handler",
                broadcaster_login or broadcaster_user_id,
                broadcaster_user_id,
            )
            await handler(broadcaster_user_id, broadcaster_login)

    async def _handle_channel_update(
        self, broadcaster_user_id: str, event: dict
    ) -> None:
        """Speichert eine channel.update Notification (Titel/Game-Änderung) in der DB."""
        title = (event.get("title") or "").strip() or None
        game_name = (
            event.get("category_name") or event.get("game_name") or ""
        ).strip() or None
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
        gifter_login = (
            event.get("gifter_login") or event.get("gifter_user_login") or ""
        ).strip().lower() or None
        cumulative_months = (
            int(event.get("cumulative_months") or event.get("months") or 0) or None
        )
        streak_months = int(event.get("streak_months") or 0) or None
        message_data = event.get("message") or {}
        if isinstance(message_data, dict):
            message = (message_data.get("text") or "").strip() or None
        else:
            message = str(message_data).strip() or None
        total_gifted = int(event.get("total") or 0) or None

        session_id = self._get_active_session_id_by_user_id(broadcaster_user_id)

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
                        session_id,
                        broadcaster_user_id,
                        event_type,
                        user_login,
                        tier,
                        is_gift,
                        gifter_login,
                        cumulative_months,
                        streak_months,
                        message,
                        total_gifted,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
        except Exception:
            log.exception(
                "_store_subscription_event: Fehler für %s (%s)",
                broadcaster_user_id,
                event_type,
            )

    def _get_active_session_id_by_user_id(
        self, broadcaster_user_id: str
    ) -> "int | None":
        """Gibt die aktive session_id für einen Broadcaster zurück (über twitch_live_state).

        twitch_stream_sessions hat keine twitch_user_id-Spalte – deshalb über
        twitch_live_state.active_session_id lookupaben.
        """
        try:
            with storage.get_conn() as c:
                row = c.execute(
                    "SELECT active_session_id FROM twitch_live_state WHERE twitch_user_id = ?",
                    (broadcaster_user_id,),
                ).fetchone()
            if row and row[0] is not None:
                return int(
                    row[0] if not hasattr(row, "keys") else row["active_session_id"]
                )
        except Exception:
            log.debug(
                "_get_active_session_id_by_user_id: Fehler für %s",
                broadcaster_user_id,
                exc_info=True,
            )
        return None

    async def _store_ad_break_event(
        self, broadcaster_user_id: str, event: dict
    ) -> None:
        """Speichert ein channel.ad_break.begin Event."""
        duration_seconds = int(event.get("duration_seconds") or 0) or None
        is_automatic = int(bool(event.get("is_automatic")))

        session_id = self._get_active_session_id_by_user_id(broadcaster_user_id)

        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_ad_break_events
                        (session_id, twitch_user_id, duration_seconds, is_automatic, started_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        broadcaster_user_id,
                        duration_seconds,
                        is_automatic,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
        except Exception:
            log.exception("_store_ad_break_event: Fehler für %s", broadcaster_user_id)

    async def _store_bits_event(self, broadcaster_user_id: str, event: dict) -> None:
        """Speichert ein channel.cheer (Bits) Event in der Datenbank."""
        donor_login = (
            event.get("user_login") or event.get("user_name") or ""
        ).strip().lower() or None
        amount = int(event.get("bits") or event.get("amount") or 0)
        message = (event.get("message") or "").strip() or None
        if not amount:
            return
        # Session ID für den aktuellen Stream bestimmen (optional)
        session_id = self._get_active_session_id_by_user_id(broadcaster_user_id)

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
            log.exception(
                "_store_bits_event: Fehler beim Speichern für %s", broadcaster_user_id
            )

    async def _store_channel_points_event(
        self, broadcaster_user_id: str, event: dict
    ) -> None:
        """Speichert ein channel.channel_points_*_reward_redemption.add Event."""
        user_login = (
            event.get("user_login") or event.get("user_name") or ""
        ).strip().lower() or None
        reward = event.get("reward") or {}
        reward_id = (reward.get("id") or event.get("reward_id") or "").strip() or None
        reward_title = (
            reward.get("title") or event.get("reward_title") or ""
        ).strip() or None
        reward_cost = int(reward.get("cost") or event.get("reward_cost") or 0) or None
        user_input = (event.get("user_input") or "").strip() or None
        redeemed_at = (event.get("redeemed_at") or "").strip() or datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds")

        session_id = self._get_active_session_id_by_user_id(broadcaster_user_id)

        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_channel_points_events
                        (session_id, twitch_user_id, user_login, reward_id, reward_title, reward_cost, user_input, redeemed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        broadcaster_user_id,
                        user_login,
                        reward_id,
                        reward_title,
                        reward_cost,
                        user_input,
                        redeemed_at,
                    ),
                )
        except Exception:
            log.exception(
                "_store_channel_points_event: Fehler beim Speichern für %s",
                broadcaster_user_id,
            )

    async def _store_hype_train_event(
        self,
        broadcaster_user_id: str,
        event: dict,
        *,
        ended: bool,
        progress: bool = False,
    ) -> None:
        """Speichert ein channel.hype_train.begin/progress/end Event in der Datenbank."""
        started_at = (event.get("started_at") or "").strip() or None
        ended_at = (event.get("ended_at") or "").strip() or None if ended else None
        level = int(event.get("level") or 0) or None
        total_progress = (
            int(event.get("total") or event.get("total_progress") or 0) or None
        )
        duration_seconds: int | None = None
        if started_at and ended_at:
            try:
                from datetime import datetime as _dt

                dt_start = _dt.fromisoformat(started_at.replace("Z", "+00:00"))
                dt_end = _dt.fromisoformat(ended_at.replace("Z", "+00:00"))
                duration_seconds = max(0, int((dt_end - dt_start).total_seconds()))
            except (TypeError, ValueError):
                log.debug(
                    "_store_hype_train_event: Konnte Dauer nicht berechnen für %s",
                    broadcaster_user_id,
                    exc_info=True,
                )

        session_id = self._get_active_session_id_by_user_id(broadcaster_user_id)

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
                        (
                            ended_at,
                            duration_seconds,
                            level,
                            total_progress,
                            broadcaster_user_id,
                            started_at,
                        ),
                    ).rowcount
                    if updated:
                        return
                phase = "progress" if progress else ("end" if ended else "begin")
                c.execute(
                    """
                    INSERT INTO twitch_hype_train_events
                        (session_id, twitch_user_id, started_at, ended_at,
                         duration_seconds, level, total_progress, event_phase)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        broadcaster_user_id,
                        started_at,
                        ended_at,
                        duration_seconds,
                        level,
                        total_progress,
                        phase,
                    ),
                )
        except Exception:
            log.exception(
                "_store_hype_train_event: Fehler beim Speichern für %s",
                broadcaster_user_id,
            )

    async def _store_ban_event(
        self, broadcaster_user_id: str, event: dict, *, unbanned: bool = False
    ) -> None:
        """Speichert ein channel.ban / channel.unban Event."""
        event_type = "unban" if unbanned else "ban"
        target_login = (
            event.get("user_login") or event.get("user_name") or ""
        ).strip().lower() or None
        target_id = str(event.get("user_id") or "").strip() or None
        moderator_login = (
            event.get("moderator_user_login") or ""
        ).strip().lower() or None
        reason = (event.get("reason") or "").strip() or None
        ends_at = (event.get("ends_at") or "").strip() or None  # None = permanent

        session_id = self._get_active_session_id_by_user_id(broadcaster_user_id)

        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_ban_events
                        (session_id, twitch_user_id, event_type, target_login, target_id,
                         moderator_login, reason, ends_at, received_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        broadcaster_user_id,
                        event_type,
                        target_login,
                        target_id,
                        moderator_login,
                        reason,
                        ends_at,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
        except Exception:
            log.exception(
                "_store_ban_event: Fehler für %s (%s)", broadcaster_user_id, event_type
            )

    async def _store_shoutout_event(
        self, broadcaster_user_id: str, event: dict, *, direction: str
    ) -> None:
        """Speichert ein channel.shoutout.create / channel.shoutout.receive Event.
        direction: 'sent' | 'received'
        """
        if direction == "sent":
            other_id = str(event.get("to_broadcaster_user_id") or "").strip() or None
            other_login = (
                event.get("to_broadcaster_user_login") or ""
            ).strip().lower() or None
            moderator_login = (
                event.get("moderator_user_login") or ""
            ).strip().lower() or None
            viewer_count = int(event.get("viewer_count") or 0)
        else:
            other_id = str(event.get("from_broadcaster_user_id") or "").strip() or None
            other_login = (
                event.get("from_broadcaster_user_login") or ""
            ).strip().lower() or None
            moderator_login = None
            viewer_count = int(event.get("viewer_count") or 0)

        try:
            with storage.get_conn() as c:
                c.execute(
                    """
                    INSERT INTO twitch_shoutout_events
                        (twitch_user_id, direction, other_broadcaster_id, other_broadcaster_login,
                         moderator_login, viewer_count, received_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        broadcaster_user_id,
                        direction,
                        other_id,
                        other_login,
                        moderator_login,
                        viewer_count,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
        except Exception:
            log.exception(
                "_store_shoutout_event: Fehler für %s (%s)",
                broadcaster_user_id,
                direction,
            )
