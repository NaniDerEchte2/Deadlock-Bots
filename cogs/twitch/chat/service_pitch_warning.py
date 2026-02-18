import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple

from ..storage import get_conn

log = logging.getLogger("TwitchStreams.ChatBot")


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return max(minimum, parsed)


_SERVICE_WARNING_ACCOUNT_MAX_DAYS = _env_int(
    "TWITCH_SERVICE_WARNING_ACCOUNT_MAX_DAYS",
    90,
    minimum=1,
)
_SERVICE_WARNING_MAX_FOLLOWERS = _env_int(
    "TWITCH_SERVICE_WARNING_MAX_FOLLOWERS",
    750,
    minimum=1,
)
_SERVICE_WARNING_WINDOW_SEC = _env_int(
    "TWITCH_SERVICE_WARNING_WINDOW_SEC",
    8 * 60,
    minimum=60,
)
_SERVICE_WARNING_MIN_SCORE = _env_int(
    "TWITCH_SERVICE_WARNING_MIN_SCORE",
    3,
    minimum=1,
)
_SERVICE_WARNING_MIN_MESSAGES = _env_int(
    "TWITCH_SERVICE_WARNING_MIN_MESSAGES",
    2,
    minimum=1,
)
_SERVICE_WARNING_CHANNEL_COOLDOWN_SEC = _env_int(
    "TWITCH_SERVICE_WARNING_CHANNEL_COOLDOWN_SEC",
    15 * 60,
    minimum=30,
)
_SERVICE_WARNING_USER_COOLDOWN_SEC = _env_int(
    "TWITCH_SERVICE_WARNING_USER_COOLDOWN_SEC",
    6 * 60 * 60,
    minimum=60,
)
_SERVICE_WARNING_ACCOUNT_CACHE_TTL_SEC = _env_int(
    "TWITCH_SERVICE_WARNING_ACCOUNT_CACHE_TTL_SEC",
    6 * 60 * 60,
    minimum=60,
)
_SERVICE_WARNING_FOLLOWER_CACHE_TTL_SEC = _env_int(
    "TWITCH_SERVICE_WARNING_FOLLOWER_CACHE_TTL_SEC",
    15 * 60,
    minimum=30,
)


_SERVICE_STRONG_PATTERNS = (
    (re.compile(r"\bdo\s+(?:u|you)\s+speak\s+english\b", re.IGNORECASE), "do_you_speak_english"),
    (re.compile(r"\bbrand\s+new\s+streamer\b", re.IGNORECASE), "brand_new_streamer"),
    (re.compile(r"\bwelcome\s+to\s+(?:the\s+)?twitch\b", re.IGNORECASE), "welcome_to_twitch"),
    (re.compile(r"\bwhat(?:'s|s)\s+good\b", re.IGNORECASE), "whats_good"),
    (re.compile(r"\bwhat\s+got\s+you\s+into\s+streaming\b", re.IGNORECASE), "what_got_you_into_streaming"),
    (re.compile(r"\bwhat\s+made\s+you\s+start\s+streaming\b", re.IGNORECASE), "what_made_you_start_streaming"),
)

_SERVICE_SOFT_PATTERNS = (
    (re.compile(r"\bhow\s+are\s+(?:u|you)\b", re.IGNORECASE), "how_are_you"),
    (re.compile(r"\bnew\s+streamer\b", re.IGNORECASE), "new_streamer"),
    (re.compile(r"\bhey+\b", re.IGNORECASE), "hey"),
    (re.compile(r"\bcool\b", re.IGNORECASE), "cool"),
    (re.compile(r"\bamazing\b", re.IGNORECASE), "amazing"),
    (re.compile(r"\boh\s+i+\s+ee\b", re.IGNORECASE), "oh_i_ee"),
)


class ServicePitchWarningMixin:
    def _init_service_pitch_warning(self) -> None:
        self._service_warning_log = Path("logs") / "twitch_service_warnings.log"
        self._service_warning_activity: Dict[Tuple[str, str], Deque[Tuple[float, int]]] = {}
        self._service_warning_channel_cd: Dict[str, float] = {}
        self._service_warning_user_cd: Dict[Tuple[str, str], float] = {}
        self._service_warning_account_age_cache: Dict[str, Tuple[float, Optional[int]]] = {}
        self._service_warning_follower_cache: Dict[str, Tuple[float, Optional[int]]] = {}

    @staticmethod
    def _score_service_pitch_message(content: str) -> tuple[int, list[str]]:
        raw = (content or "").strip()
        if not raw:
            return 0, []

        score = 0
        reasons: list[str] = []
        matched: set[str] = set()

        for pattern, label in _SERVICE_STRONG_PATTERNS:
            if label in matched:
                continue
            if pattern.search(raw):
                matched.add(label)
                score += 2
                reasons.append(f"strong:{label}")

        for pattern, label in _SERVICE_SOFT_PATTERNS:
            if label in matched:
                continue
            if pattern.search(raw):
                matched.add(label)
                score += 1
                reasons.append(f"soft:{label}")

        return score, reasons

    @staticmethod
    def _prune_service_activity_bucket(bucket: Deque[Tuple[float, int]], now: float) -> None:
        while bucket and (now - float(bucket[0][0])) > float(_SERVICE_WARNING_WINDOW_SEC):
            bucket.popleft()

    async def _get_account_age_days(self, author_id: str, author_login: str) -> Optional[int]:
        cache_key = (author_id or author_login or "").strip().lower()
        if not cache_key:
            return None

        now = time.monotonic()
        cached = self._service_warning_account_age_cache.get(cache_key)
        if isinstance(cached, tuple) and len(cached) == 2:
            cached_ts, cached_age = cached
            if (now - float(cached_ts)) <= float(_SERVICE_WARNING_ACCOUNT_CACHE_TTL_SEC):
                return cached_age

        if not hasattr(self, "fetch_users"):
            return None

        try:
            users = []
            if author_id and str(author_id).isdigit():
                users = await self.fetch_users(ids=[int(author_id)])
            elif author_login:
                users = await self.fetch_users(logins=[author_login])

            if not users:
                self._service_warning_account_age_cache[cache_key] = (now, None)
                return None

            created_at = getattr(users[0], "created_at", None)
            if created_at is None:
                self._service_warning_account_age_cache[cache_key] = (now, None)
                return None
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            age_days = max(0, int((datetime.now(timezone.utc) - created_at).days))
            self._service_warning_account_age_cache[cache_key] = (now, age_days)
            return age_days
        except Exception:
            log.debug("Konnte Account-Alter fuer %s nicht laden", cache_key, exc_info=True)
            self._service_warning_account_age_cache[cache_key] = (now, None)
            return None

    def _get_streamer_followers_hint(self, channel_login: str) -> Optional[int]:
        login = (channel_login or "").strip().lower().lstrip("#")
        if not login:
            return None

        now = time.monotonic()
        cached = self._service_warning_follower_cache.get(login)
        if isinstance(cached, tuple) and len(cached) == 2:
            cached_ts, cached_count = cached
            if (now - float(cached_ts)) <= float(_SERVICE_WARNING_FOLLOWER_CACHE_TTL_SEC):
                return cached_count

        follower_count: Optional[int] = None
        try:
            with get_conn() as conn:
                row = conn.execute(
                    """
                    SELECT COALESCE(followers_end, followers_start) AS follower_total
                      FROM twitch_stream_sessions
                     WHERE streamer_login = ?
                       AND COALESCE(followers_end, followers_start) IS NOT NULL
                     ORDER BY COALESCE(ended_at, started_at) DESC
                     LIMIT 1
                    """,
                    (login,),
                ).fetchone()
                if row is not None:
                    raw_value = row["follower_total"] if hasattr(row, "keys") else row[0]
                    if raw_value is not None:
                        follower_count = max(0, int(raw_value))
        except Exception:
            log.debug("Konnte Follower-Hint fuer %s nicht lesen", login, exc_info=True)

        self._service_warning_follower_cache[login] = (now, follower_count)
        return follower_count

    def _is_low_follower_target(self, channel_login: str) -> tuple[bool, Optional[int]]:
        follower_count = self._get_streamer_followers_hint(channel_login)
        if follower_count is None:
            return True, None
        return follower_count <= int(_SERVICE_WARNING_MAX_FOLLOWERS), follower_count

    def _record_service_warning(
        self,
        *,
        channel_login: str,
        chatter_login: str,
        chatter_id: str,
        account_age_days: int,
        follower_count: Optional[int],
        score: int,
        reasons: list[str],
        content: str,
    ) -> None:
        try:
            self._service_warning_log.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).isoformat()
            reason_text = ",".join(reasons) if reasons else "-"
            follower_text = "-" if follower_count is None else str(follower_count)
            safe_content = (content or "").replace("\n", " ").strip()[:350]
            line = (
                f"{ts}\t{channel_login}\t{chatter_login or '-'}\t{chatter_id or '-'}\t"
                f"{account_age_days}\t{follower_text}\t{score}\t{reason_text}\t{safe_content}\n"
            )
            with self._service_warning_log.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            log.debug("Konnte Service-Warnung nicht loggen", exc_info=True)

    def _build_service_warning_text(
        self,
        *,
        chatter_login: str,
        account_age_days: int,
    ) -> str:
        mention = f"@{chatter_login} " if chatter_login else ""
        
        return (
            f"[Warnung] {mention} wirkt wie Service-Akquise Konto Alter: {account_age_days} Tage. "
            f"Das ist oft keine normale Zuschauer-Interaktion, sondern smalltalk vor der Aquiese."
        )

    async def _maybe_warn_service_pitch(self, message, *, channel_login: str) -> bool:
        raw_content = str(getattr(message, "content", "") or "")
        if not raw_content:
            return False
        if raw_content.strip().startswith(self.prefix or "!"):
            return False

        author = getattr(message, "author", None)
        if author is None:
            return False
        if bool(getattr(author, "moderator", False)) or bool(getattr(author, "broadcaster", False)):
            return False

        score, reasons = self._score_service_pitch_message(raw_content)
        if score <= 0:
            return False

        chatter_login = (getattr(author, "name", "") or "").strip().lower()
        chatter_id = str(getattr(author, "id", "") or "").strip()

        account_age_days = await self._get_account_age_days(chatter_id, chatter_login)
        if account_age_days is None or int(account_age_days) >= int(_SERVICE_WARNING_ACCOUNT_MAX_DAYS):
            return False

        is_low_target, follower_count = self._is_low_follower_target(channel_login)
        if not is_low_target:
            return False

        now = time.monotonic()
        chatter_key = chatter_login or chatter_id or "unknown"
        bucket_key = (channel_login, chatter_key)
        bucket = self._service_warning_activity.setdefault(bucket_key, deque())
        bucket.append((now, int(score)))
        self._prune_service_activity_bucket(bucket, now)

        total_score = int(sum(int(item[1]) for item in bucket))
        msg_count = len(bucket)
        if total_score < int(_SERVICE_WARNING_MIN_SCORE) or msg_count < int(_SERVICE_WARNING_MIN_MESSAGES):
            return False

        channel_cd_until = float(self._service_warning_channel_cd.get(channel_login, 0.0))
        user_cd_until = float(self._service_warning_user_cd.get(bucket_key, 0.0))
        if now < channel_cd_until or now < user_cd_until:
            return False

        channel = self._resolve_message_channel(message) if hasattr(self, "_resolve_message_channel") else None
        if channel is None:
            channel = getattr(message, "channel", None)
        if channel is None:
            return False

        warning_text = self._build_service_warning_text(
            chatter_login=chatter_login,
            account_age_days=int(account_age_days),
            follower_count=follower_count,
        )
        sent = await self._send_chat_message(channel, warning_text, source="service_warning")
        if not sent:
            return False

        self._service_warning_channel_cd[channel_login] = now + float(_SERVICE_WARNING_CHANNEL_COOLDOWN_SEC)
        self._service_warning_user_cd[bucket_key] = now + float(_SERVICE_WARNING_USER_COOLDOWN_SEC)
        bucket.clear()

        self._record_service_warning(
            channel_login=channel_login,
            chatter_login=chatter_login,
            chatter_id=chatter_id,
            account_age_days=int(account_age_days),
            follower_count=follower_count,
            score=total_score,
            reasons=reasons,
            content=raw_content,
        )
        return True
