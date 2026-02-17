import asyncio
import logging
import random
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

from service import db

from ..storage import get_conn
from .constants import (
    _PROMO_ACTIVITY_ENABLED,
    PROMO_ACTIVITY_CHATTER_DEDUP_SEC,
    PROMO_ACTIVITY_MIN_CHATTERS,
    PROMO_ACTIVITY_MIN_MSGS,
    PROMO_ACTIVITY_MIN_RAW_MSGS_SINCE_PROMO,
    PROMO_ACTIVITY_TARGET_MPM,
    PROMO_ACTIVITY_WINDOW_MIN,
    PROMO_ATTEMPT_COOLDOWN_MIN,
    PROMO_CHANNEL_ALLOWLIST,
    _PROMO_COOLDOWN_MAX,
    _PROMO_COOLDOWN_MIN,
    PROMO_DISCORD_INVITE,
    PROMO_IGNORE_COMMANDS,
    _PROMO_INTERVAL_MIN,
    PROMO_LOOP_INTERVAL_SEC,
    PROMO_MESSAGES,
    PROMO_OVERALL_COOLDOWN_MIN,
    PROMO_VIEWER_SPIKE_COOLDOWN_MIN,
    PROMO_VIEWER_SPIKE_ENABLED,
    PROMO_VIEWER_SPIKE_MIN_CHAT_SILENCE_SEC,
    PROMO_VIEWER_SPIKE_MIN_DELTA,
    PROMO_VIEWER_SPIKE_MIN_RATIO,
    PROMO_VIEWER_SPIKE_MIN_SESSIONS,
    PROMO_VIEWER_SPIKE_MIN_STATS_SAMPLES,
    PROMO_VIEWER_SPIKE_SESSION_SAMPLE_LIMIT,
    PROMO_VIEWER_SPIKE_STATS_SAMPLE_LIMIT,
)

log = logging.getLogger("TwitchStreams.ChatBot")


class PromoMixin:
    def _promo_channel_allowed(self, login: str) -> bool:
        if not PROMO_MESSAGES:
            return False
        if PROMO_CHANNEL_ALLOWLIST and login not in PROMO_CHANNEL_ALLOWLIST:
            return False
        return True

    async def _get_promo_invite(self, login: str) -> tuple[Optional[str], bool]:
        resolver = getattr(self, "_resolve_streamer_invite", None)
        if callable(resolver):
            try:
                result = await resolver(login)
                if isinstance(result, tuple):
                    invite, is_specific = result
                else:
                    invite, is_specific = result, True
                if invite:
                    return str(invite), bool(is_specific)
            except Exception:
                log.debug("_resolve_streamer_invite failed for %s", login, exc_info=True)

        if PROMO_DISCORD_INVITE:
            return PROMO_DISCORD_INVITE, False
        return None, False

    def _record_raw_chat_message(self, login: str) -> None:
        if not login:
            return

        raw_map = getattr(self, "_last_raw_chat_message_ts", None)
        if not isinstance(raw_map, dict):
            raw_map = {}
            setattr(self, "_last_raw_chat_message_ts", raw_map)
        raw_map[login] = time.monotonic()

        raw_count_map = getattr(self, "_raw_msg_count_since_promo", None)
        if not isinstance(raw_count_map, dict):
            raw_count_map = {}
            setattr(self, "_raw_msg_count_since_promo", raw_count_map)
        raw_count_map[login] = int(raw_count_map.get(login, 0)) + 1

    def _raw_msg_count_since_last_promo(self, login: str) -> int:
        raw_count_map = getattr(self, "_raw_msg_count_since_promo", None)
        if not isinstance(raw_count_map, dict):
            return 0
        return int(raw_count_map.get(login, 0))

    def _has_new_raw_chat_since_last_promo(self, login: str) -> bool:
        last_sent = self._last_promo_sent.get(login)
        if last_sent is None:
            return True

        raw_map = getattr(self, "_last_raw_chat_message_ts", None)
        if not isinstance(raw_map, dict):
            return False

        last_raw = raw_map.get(login)
        if last_raw is None:
            return False

        return float(last_raw) > float(last_sent)

    def _prune_promo_activity(self, bucket: Deque[Tuple[float, str]], now: float) -> None:
        window_sec = PROMO_ACTIVITY_WINDOW_MIN * 60
        while bucket and now - bucket[0][0] > window_sec:
            bucket.popleft()

    def _prune_promo_chatter_dedupe(self, login: str, now: float) -> None:
        dedupe_state = getattr(self, "_promo_chatter_dedupe", None)
        if not isinstance(dedupe_state, dict):
            return
        chatter_last = dedupe_state.get(login)
        if not isinstance(chatter_last, dict) or not chatter_last:
            return

        max_age_sec = max(float(PROMO_ACTIVITY_WINDOW_MIN * 60), float(PROMO_ACTIVITY_CHATTER_DEDUP_SEC) * 4.0)
        stale = [chatter for chatter, ts in chatter_last.items() if now - float(ts) > max_age_sec]
        for chatter in stale:
            chatter_last.pop(chatter, None)
        if not chatter_last:
            dedupe_state.pop(login, None)

    def _record_promo_activity(self, login: str, chatter_login: str, now: float) -> None:
        dedupe_state = getattr(self, "_promo_chatter_dedupe", None)
        if not isinstance(dedupe_state, dict):
            dedupe_state = {}
            setattr(self, "_promo_chatter_dedupe", dedupe_state)

        chatter_last = dedupe_state.setdefault(login, {})
        last_seen = chatter_last.get(chatter_login)
        if last_seen is not None and now - float(last_seen) < float(PROMO_ACTIVITY_CHATTER_DEDUP_SEC):
            return

        chatter_last[chatter_login] = now
        self._prune_promo_chatter_dedupe(login, now)

        bucket = self._promo_activity.setdefault(login, deque())
        bucket.append((now, chatter_login))
        self._prune_promo_activity(bucket, now)

    def _get_promo_activity_stats(self, login: str, now: float) -> Tuple[int, int, float]:
        bucket = self._promo_activity.get(login)
        if not bucket:
            return 0, 0, 0.0
        self._prune_promo_activity(bucket, now)
        msg_count = len(bucket)
        if msg_count <= 0:
            return 0, 0, 0.0
        unique_chatters = len({c for _, c in bucket})
        msgs_per_min = msg_count / max(1.0, float(PROMO_ACTIVITY_WINDOW_MIN))
        return msg_count, unique_chatters, msgs_per_min

    def _promo_cooldown_sec(self, msgs_per_min: float) -> float:
        min_cd = float(_PROMO_COOLDOWN_MIN)
        max_cd = float(_PROMO_COOLDOWN_MAX)
        if max_cd < min_cd:
            max_cd = min_cd
        target = float(PROMO_ACTIVITY_TARGET_MPM)
        ratio = 1.0 if target <= 0 else min(1.0, msgs_per_min / target)
        return (min_cd + (1.0 - ratio) * (max_cd - min_cd)) * 60.0

    def _overall_promo_cooldown_sec(self) -> float:
        return max(0.0, float(PROMO_OVERALL_COOLDOWN_MIN) * 60.0)

    def _overall_promo_ready(self, login: str, now: float) -> bool:
        overall_sec = self._overall_promo_cooldown_sec()
        if overall_sec <= 0:
            return True
        last_sent = self._last_promo_sent.get(login)
        if last_sent is None:
            return True
        return (now - float(last_sent)) >= overall_sec

    def _promo_attempt_allowed(self, login: str, now: float) -> bool:
        last_attempt = self._last_promo_attempt.get(login)
        if last_attempt is not None and now - last_attempt < (PROMO_ATTEMPT_COOLDOWN_MIN * 60):
            return False
        self._last_promo_attempt[login] = now
        return True

    @staticmethod
    def _make_promo_channel(login: str, channel_id: str):
        class _Channel:
            __slots__ = ("name", "id")

            def __init__(self, name: str, cid: str):
                self.name = name
                self.id = cid

        return _Channel(login, channel_id)

    async def _send_promo_message(self, login: str, channel_id: str, now: float, *, reason: str) -> bool:
        invite, is_specific = await self._get_promo_invite(login)
        if not invite:
            return False

        msg = random.choice(PROMO_MESSAGES).format(invite=invite)
        ok = await self._send_announcement(
            self._make_promo_channel(login, channel_id),
            msg,
            color="purple",
            source="promo",
        )
        if not ok:
            return False

        self._last_promo_sent[login] = now
        raw_count_map = getattr(self, "_raw_msg_count_since_promo", None)
        if isinstance(raw_count_map, dict):
            raw_count_map[login] = 0
        if reason == "viewer_spike":
            viewer_spike_map = getattr(self, "_last_promo_viewer_spike", None)
            if not isinstance(viewer_spike_map, dict):
                viewer_spike_map = {}
                setattr(self, "_last_promo_viewer_spike", viewer_spike_map)
            viewer_spike_map[login] = now

        if is_specific:
            marker = getattr(self, "_mark_streamer_invite_sent", None)
            if callable(marker):
                marker(login)
        return True

    def _has_recent_chat_activity(self, login: str, now: float) -> bool:
        msg_count, unique_chatters, _ = self._get_promo_activity_stats(login, now)
        return msg_count > 0 and unique_chatters > 0

    def _latest_chat_activity_age_sec(self, login: str, now: float) -> Optional[float]:
        bucket = self._promo_activity.get(login)
        if bucket is None:
            return None
        self._prune_promo_activity(bucket, now)
        if len(bucket) == 0:
            return None
        last_ts = float(bucket[-1][0])
        return max(0.0, now - last_ts)

    def _get_viewer_spike_context(self, login: str) -> Optional[Tuple[int, float, str, int, float]]:
        row_sessions = None
        row_stats = None

        try:
            with get_conn():
                row_sessions = db.query_one(
                    """
                    SELECT AVG(avg_viewers) AS avg_viewers, COUNT(*) AS sample_count
                      FROM (
                            SELECT avg_viewers
                              FROM twitch_stream_sessions
                             WHERE streamer_login = ?
                               AND ended_at IS NOT NULL
                               AND avg_viewers > 0
                             ORDER BY started_at DESC
                             LIMIT ?
                      ) recent_sessions
                    """,
                    (login, int(max(1, PROMO_VIEWER_SPIKE_SESSION_SAMPLE_LIMIT))),
                )
                row_stats = db.query_one(
                    """
                    SELECT AVG(viewer_count) AS avg_viewers, COUNT(*) AS sample_count
                      FROM (
                            SELECT viewer_count
                              FROM twitch_stats_tracked
                             WHERE LOWER(streamer) = ?
                               AND viewer_count > 0
                             ORDER BY ts_utc DESC
                             LIMIT ?
                      ) recent_stats
                    """,
                    (login, int(max(1, PROMO_VIEWER_SPIKE_STATS_SAMPLE_LIMIT))),
                )
                row_live = db.query_one(
                    """
                    SELECT last_viewer_count
                      FROM twitch_live_state
                     WHERE streamer_login = ?
                       AND is_live = 1
                    """,
                    (login,),
                )
        except Exception:
            log.debug("Viewer-Spike-Kontext konnte für %s nicht geladen werden", login, exc_info=True)
            return None

        if not row_live:
            return None

        current_viewers = int((row_live["last_viewer_count"] if hasattr(row_live, "keys") else row_live[0]) or 0)
        if current_viewers <= 0:
            return None

        baseline = 0.0
        sample_count = 0
        source = ""
        if row_sessions is not None:
            sessions_avg = float((row_sessions["avg_viewers"] if hasattr(row_sessions, "keys") else row_sessions[0]) or 0.0)
            sessions_cnt = int((row_sessions["sample_count"] if hasattr(row_sessions, "keys") else row_sessions[1]) or 0)
            if sessions_cnt >= int(PROMO_VIEWER_SPIKE_MIN_SESSIONS) and sessions_avg > 0:
                baseline = sessions_avg
                sample_count = sessions_cnt
                source = "sessions"

        if baseline <= 0 and row_stats is not None:
            stats_avg = float((row_stats["avg_viewers"] if hasattr(row_stats, "keys") else row_stats[0]) or 0.0)
            stats_cnt = int((row_stats["sample_count"] if hasattr(row_stats, "keys") else row_stats[1]) or 0)
            if stats_cnt >= int(PROMO_VIEWER_SPIKE_MIN_STATS_SAMPLES) and stats_avg > 0:
                baseline = stats_avg
                sample_count = stats_cnt
                source = "tracked"

        if baseline <= 0:
            return None

        threshold = max(
            baseline * float(PROMO_VIEWER_SPIKE_MIN_RATIO),
            baseline + float(PROMO_VIEWER_SPIKE_MIN_DELTA),
        )
        return current_viewers, baseline, source, sample_count, threshold

    async def _maybe_send_promo_with_stats(self, login: str, channel_id: str, now: float) -> bool:
        if not self._promo_channel_allowed(login):
            return False
        if not self._overall_promo_ready(login, now):
            return False

        min_raw_msgs = max(0, int(PROMO_ACTIVITY_MIN_RAW_MSGS_SINCE_PROMO))
        if min_raw_msgs > 0 and self._raw_msg_count_since_last_promo(login) < min_raw_msgs:
            return False

        msg_count, unique_chatters, msgs_per_min = self._get_promo_activity_stats(login, now)
        if PROMO_ACTIVITY_MIN_MSGS > 0 and msg_count < PROMO_ACTIVITY_MIN_MSGS:
            return False
        if PROMO_ACTIVITY_MIN_CHATTERS > 0 and unique_chatters < PROMO_ACTIVITY_MIN_CHATTERS:
            return False

        last_sent = self._last_promo_sent.get(login)
        cooldown_sec = self._promo_cooldown_sec(msgs_per_min)
        if last_sent is not None and now - last_sent < cooldown_sec:
            return False

        if not self._promo_attempt_allowed(login, now):
            return False

        ok = await self._send_promo_message(login, channel_id, now, reason="chat_activity")
        if ok:
            log.info(
                "Chat-Promo gesendet in %s (reason=chat_activity, activity=%d msgs/%d chatters, cooldown=%.1f min)",
                login,
                msg_count,
                unique_chatters,
                cooldown_sec / 60.0,
            )
        return ok

    async def _maybe_send_viewer_spike_promo(self, login: str, channel_id: str, now: float) -> bool:
        if not PROMO_VIEWER_SPIKE_ENABLED:
            return False
        if not self._promo_channel_allowed(login):
            return False
        if not self._overall_promo_ready(login, now):
            return False
        if not self._has_new_raw_chat_since_last_promo(login):
            return False
        activity_age_sec = self._latest_chat_activity_age_sec(login, now)
        if (
            activity_age_sec is not None
            and activity_age_sec < float(PROMO_VIEWER_SPIKE_MIN_CHAT_SILENCE_SEC)
        ):
            return False

        ctx = self._get_viewer_spike_context(login)
        if ctx is None:
            return False

        current_viewers, baseline, source, sample_count, threshold = ctx
        if float(current_viewers) < float(threshold):
            return False

        viewer_spike_map = getattr(self, "_last_promo_viewer_spike", None)
        if not isinstance(viewer_spike_map, dict):
            viewer_spike_map = {}
            setattr(self, "_last_promo_viewer_spike", viewer_spike_map)

        last_viewer_promo = viewer_spike_map.get(login)
        viewer_cd_sec = float(PROMO_VIEWER_SPIKE_COOLDOWN_MIN) * 60.0
        if last_viewer_promo is not None and now - last_viewer_promo < viewer_cd_sec:
            return False

        if not self._promo_attempt_allowed(login, now):
            return False

        ok = await self._send_promo_message(login, channel_id, now, reason="viewer_spike")
        if ok:
            log.info(
                "Chat-Promo gesendet in %s (reason=viewer_spike, viewers=%d, baseline=%.1f, threshold=%.1f, source=%s:%d, cooldown=%.1f min)",
                login,
                current_viewers,
                baseline,
                threshold,
                source,
                sample_count,
                viewer_cd_sec / 60.0,
            )
        return ok

    async def _maybe_send_activity_promo(self, message) -> None:
        if not _PROMO_ACTIVITY_ENABLED:
            return

        channel = getattr(message, "channel", None)
        if channel is None:
            channel = getattr(message, "source_broadcaster", None) or getattr(message, "broadcaster", None)

        channel_name = (
            getattr(channel, "name", "")
            or getattr(channel, "login", "")
            or ""
        )
        login = channel_name.lstrip("#").lower()
        if not login or not self._promo_channel_allowed(login):
            return

        # WICHTIG: Promo-Messages nur für PARTNER (nicht Monitored-Only)!
        from ..partner_utils import is_partner_channel_for_chat_tracking
        if not is_partner_channel_for_chat_tracking(login):
            return

        if PROMO_IGNORE_COMMANDS:
            content = message.content or ""
            if content.strip().startswith(self.prefix or "!"):
                return

        author = getattr(message, "author", None)
        chatter_login = (getattr(author, "name", "") or "").lower()
        if not chatter_login:
            return

        now = time.monotonic()
        self._record_promo_activity(login, chatter_login, now)

        channel_id = getattr(channel, "id", None) or self._channel_ids.get(login)
        if not channel_id:
            return

        await self._maybe_send_promo_with_stats(login, str(channel_id), now)

    # ------------------------------------------------------------------
    # Periodische Chat-Promos
    # ------------------------------------------------------------------
    async def _periodic_promo_loop(self) -> None:
        """Hauptschleife: prüft alle X Sekunden, ob eine Promo gesendet werden soll."""
        loop_interval_sec = max(15, int(PROMO_LOOP_INTERVAL_SEC))
        try:
            while True:
                await asyncio.sleep(loop_interval_sec)
                try:
                    await self._send_promo_if_due()
                except Exception:
                    log.debug("_send_promo_if_due fehlgeschlagen", exc_info=True)
        except asyncio.CancelledError:
            log.info("Chat-Promo-Loop wurde abgebrochen")

    async def _send_promo_if_due(self) -> None:
        """Sendet eine Promo in jeden live-Kanal, für den das Intervall abgelaufen ist."""
        now = time.monotonic()
        live_channels = await self._get_live_channels_for_promo()

        if _PROMO_ACTIVITY_ENABLED or PROMO_VIEWER_SPIKE_ENABLED:
            for login, broadcaster_id in live_channels:
                if not self._promo_channel_allowed(login):
                    continue
                sent = False
                if _PROMO_ACTIVITY_ENABLED:
                    sent = await self._maybe_send_promo_with_stats(login, str(broadcaster_id), now)
                if not sent and PROMO_VIEWER_SPIKE_ENABLED:
                    await self._maybe_send_viewer_spike_promo(login, str(broadcaster_id), now)
            return

        interval_sec = max(_PROMO_INTERVAL_MIN * 60, self._overall_promo_cooldown_sec())
        for login, broadcaster_id in live_channels:
            last = self._last_promo_sent.get(login)
            if last is None:
                self._last_promo_sent[login] = now
                continue

            if now - last < interval_sec:
                continue

            invite, is_specific = await self._get_promo_invite(login)
            if not invite:
                continue
            msg = random.choice(PROMO_MESSAGES).format(invite=invite)

            class _Channel:
                __slots__ = ("name", "id")
                def __init__(self, name: str, channel_id: str):
                    self.name = name
                    self.id = channel_id

            ok = await self._send_chat_message(
                _Channel(login, broadcaster_id),
                msg,
                source="promo",
            )
            if ok:
                self._last_promo_sent[login] = now
                if is_specific:
                    marker = getattr(self, "_mark_streamer_invite_sent", None)
                    if callable(marker):
                        marker(login)
                log.info("Chat-Promo gesendet in %s", login)
            else:
                log.debug("Chat-Promo in %s fehlgeschlagen", login)

    async def _get_live_channels_for_promo(self) -> List[Tuple[str, str]]:
        """Gibt alle live-Kanäle zurück, in denen der Bot aktiv ist (login, broadcaster_id)."""
        if not self._channel_ids:
            return []

        allowed_logins = {str(login).lower() for login in self._channel_ids if login}
        if not allowed_logins:
            return []

        try:
            # Uses storage.get_conn() so twitch_* schema is ensured before querying.
            with get_conn():
                rows = db.query_all(
                    """
                    SELECT s.twitch_login, s.twitch_user_id
                      FROM twitch_streamers s
                      JOIN twitch_live_state l ON s.twitch_user_id = l.twitch_user_id
                     WHERE l.is_live = 1
                    """
                )
        except Exception:
            log.debug("_get_live_channels_for_promo: DB-Query fehlgeschlagen", exc_info=True)
            return []

        channels: List[Tuple[str, str]] = []
        for row in rows:
            if not row[0] or not row[1]:
                continue
            login = str(row[0]).lower()
            if login in allowed_logins:
                channels.append((login, str(row[1])))
        return channels
