import asyncio
import logging
import random
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

from .storage import get_conn
from .twitch_chat_bot_constants import (
    _PROMO_ACTIVITY_ENABLED,
    _PROMO_ACTIVITY_MIN_CHATTERS,
    _PROMO_ACTIVITY_MIN_MSGS,
    _PROMO_ACTIVITY_TARGET_MPM,
    _PROMO_ACTIVITY_WINDOW_MIN,
    _PROMO_ATTEMPT_COOLDOWN_MIN,
    _PROMO_CHANNEL_ALLOWLIST,
    _PROMO_COOLDOWN_MAX,
    _PROMO_COOLDOWN_MIN,
    _PROMO_DISCORD_INVITE,
    _PROMO_IGNORE_COMMANDS,
    _PROMO_INTERVAL_MIN,
    _PROMO_MESSAGES,
)

log = logging.getLogger("TwitchStreams.ChatBot")


class PromoMixin:
    def _promo_channel_allowed(self, login: str) -> bool:
        if not _PROMO_MESSAGES:
            return False
        if _PROMO_CHANNEL_ALLOWLIST and login not in _PROMO_CHANNEL_ALLOWLIST:
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

        if _PROMO_DISCORD_INVITE:
            return _PROMO_DISCORD_INVITE, False
        return None, False

    def _prune_promo_activity(self, bucket: Deque[Tuple[float, str]], now: float) -> None:
        window_sec = _PROMO_ACTIVITY_WINDOW_MIN * 60
        while bucket and now - bucket[0][0] > window_sec:
            bucket.popleft()

    def _record_promo_activity(self, login: str, chatter_login: str, now: float) -> None:
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
        unique_chatters = 0
        if _PROMO_ACTIVITY_MIN_CHATTERS > 0:
            unique_chatters = len({c for _, c in bucket})
        msgs_per_min = msg_count / max(1.0, float(_PROMO_ACTIVITY_WINDOW_MIN))
        return msg_count, unique_chatters, msgs_per_min

    def _promo_cooldown_sec(self, msgs_per_min: float) -> float:
        min_cd = float(_PROMO_COOLDOWN_MIN)
        max_cd = float(_PROMO_COOLDOWN_MAX)
        if max_cd < min_cd:
            max_cd = min_cd
        target = float(_PROMO_ACTIVITY_TARGET_MPM)
        ratio = 1.0 if target <= 0 else min(1.0, msgs_per_min / target)
        return (min_cd + (1.0 - ratio) * (max_cd - min_cd)) * 60.0

    async def _maybe_send_promo_with_stats(self, login: str, channel_id: str, now: float) -> None:
        if not self._promo_channel_allowed(login):
            return

        msg_count, unique_chatters, msgs_per_min = self._get_promo_activity_stats(login, now)
        if _PROMO_ACTIVITY_MIN_MSGS > 0 and msg_count < _PROMO_ACTIVITY_MIN_MSGS:
            return
        if _PROMO_ACTIVITY_MIN_CHATTERS > 0 and unique_chatters < _PROMO_ACTIVITY_MIN_CHATTERS:
            return

        last_sent = self._last_promo_sent.get(login)
        cooldown_sec = self._promo_cooldown_sec(msgs_per_min)
        if last_sent is not None and now - last_sent < cooldown_sec:
            return

        last_attempt = self._last_promo_attempt.get(login)
        if last_attempt is not None and now - last_attempt < (_PROMO_ATTEMPT_COOLDOWN_MIN * 60):
            return
        self._last_promo_attempt[login] = now

        invite, is_specific = await self._get_promo_invite(login)
        if not invite:
            return
        msg = random.choice(_PROMO_MESSAGES).format(invite=invite)

        class _Channel:
            __slots__ = ("name", "id")
            def __init__(self, name: str, channel_id: str):
                self.name = name
                self.id = channel_id

        ok = await self._send_chat_message(_Channel(login, channel_id), msg)
        if ok:
            self._last_promo_sent[login] = now
            if is_specific:
                marker = getattr(self, "_mark_streamer_invite_sent", None)
                if callable(marker):
                    marker(login)
            log.info(
                "Chat-Promo gesendet in %s (activity=%d msgs/%d chatters, cooldown=%.1f min)",
                login,
                msg_count,
                unique_chatters,
                cooldown_sec / 60.0,
            )

    async def _maybe_send_activity_promo(self, message) -> None:
        if not _PROMO_ACTIVITY_ENABLED:
            return

        channel_name = getattr(message.channel, "name", "") or ""
        login = channel_name.lstrip("#").lower()
        if not login or not self._promo_channel_allowed(login):
            return

        if _PROMO_IGNORE_COMMANDS:
            content = message.content or ""
            if content.strip().startswith(self.prefix or "!"):
                return

        author = getattr(message, "author", None)
        chatter_login = (getattr(author, "name", "") or "").lower()
        if not chatter_login:
            return

        now = time.monotonic()
        self._record_promo_activity(login, chatter_login, now)

        channel_id = getattr(message.channel, "id", None) or self._channel_ids.get(login)
        if not channel_id:
            return

        await self._maybe_send_promo_with_stats(login, str(channel_id), now)

    # ------------------------------------------------------------------
    # Periodische Chat-Promos
    # ------------------------------------------------------------------
    async def _periodic_promo_loop(self) -> None:
        """Hauptschleife: prüft alle 120 s, ob eine Promo gesendet werden soll."""
        try:
            while True:
                await asyncio.sleep(120)
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

        if _PROMO_ACTIVITY_ENABLED:
            for login, broadcaster_id in live_channels:
                if not self._promo_channel_allowed(login):
                    continue
                await self._maybe_send_promo_with_stats(login, str(broadcaster_id), now)
            return

        interval_sec = _PROMO_INTERVAL_MIN * 60
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
            msg = random.choice(_PROMO_MESSAGES).format(invite=invite)

            class _Channel:
                __slots__ = ("name", "id")
                def __init__(self, name: str, channel_id: str):
                    self.name = name
                    self.id = channel_id

            ok = await self._send_chat_message(_Channel(login, broadcaster_id), msg)
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

        logins = list(self._channel_ids.keys())
        placeholders = ",".join("?" * len(logins))

        try:
            with get_conn() as conn:
                rows = conn.execute(
                    f"""
                    SELECT s.twitch_login, s.twitch_user_id
                      FROM twitch_streamers s
                      JOIN twitch_live_state l ON s.twitch_user_id = l.twitch_user_id
                     WHERE l.is_live = 1
                       AND LOWER(s.twitch_login) IN ({placeholders})
                    """,
                    logins,
                ).fetchall()
        except Exception:
            log.debug("_get_live_channels_for_promo: DB-Query fehlgeschlagen", exc_info=True)
            return []

        return [(str(r[0]).lower(), str(r[1])) for r in rows if r[0] and r[1]]
