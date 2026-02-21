import logging
import os
import re
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

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
_SERVICE_WARNING_LIGHT_THRESHOLD = _env_int(
    "TWITCH_SERVICE_WARNING_LIGHT_THRESHOLD",
    4,
    minimum=1,
)
_SERVICE_WARNING_PUBLIC_THRESHOLD = _env_int(
    "TWITCH_SERVICE_WARNING_PUBLIC_THRESHOLD",
    7,
    minimum=1,
)
_SERVICE_WARNING_STRONG_THRESHOLD = _env_int(
    "TWITCH_SERVICE_WARNING_STRONG_THRESHOLD",
    10,
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
_SERVICE_WARNING_HINT_COOLDOWN_SEC = _env_int(
    "TWITCH_SERVICE_WARNING_HINT_COOLDOWN_SEC",
    120,
    minimum=15,
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
_SERVICE_WARNING_FIRST_CHAT_WINDOW_SEC = _env_int(
    "TWITCH_SERVICE_WARNING_FIRST_CHAT_WINDOW_SEC",
    120,
    minimum=10,
)
_SERVICE_WARNING_SEQUENCE_WINDOW_SEC = _env_int(
    "TWITCH_SERVICE_WARNING_SEQUENCE_WINDOW_SEC",
    30,
    minimum=5,
)
_SERVICE_WARNING_SEQUENCE_MIN_MSGS = _env_int(
    "TWITCH_SERVICE_WARNING_SEQUENCE_MIN_MSGS",
    3,
    minimum=2,
)
_SERVICE_WARNING_SHORT_MSG_MAX_CHARS = _env_int(
    "TWITCH_SERVICE_WARNING_SHORT_MSG_MAX_CHARS",
    32,
    minimum=8,
)

if _SERVICE_WARNING_PUBLIC_THRESHOLD < _SERVICE_WARNING_LIGHT_THRESHOLD:
    _SERVICE_WARNING_PUBLIC_THRESHOLD = _SERVICE_WARNING_LIGHT_THRESHOLD
if _SERVICE_WARNING_STRONG_THRESHOLD < _SERVICE_WARNING_PUBLIC_THRESHOLD:
    _SERVICE_WARNING_STRONG_THRESHOLD = _SERVICE_WARNING_PUBLIC_THRESHOLD

_SERVICE_PATTERNS = (
    (
        "language_probe",
        3,
        (
            re.compile(r"\bdo\s+(?:u|you)\s+speak\s+english\b", re.IGNORECASE),
            re.compile(r"\b(?:speak|sprichst)\s+(?:english|englisch)\b", re.IGNORECASE),
            re.compile(r"\bwhere\s+are\s+you\s+from\b", re.IGNORECASE),
            re.compile(r"\bhow\s+old\s+are\s+you\b", re.IGNORECASE),
        ),
    ),
    (
        "new_here",
        2,
        (
            re.compile(r"\bnew\s+here\b", re.IGNORECASE),
            re.compile(r"\bfirst\s+time\s+here\b", re.IGNORECASE),
            re.compile(r"\bi(?:'m| am)\s+new\s+to\s+your\s+channel\b", re.IGNORECASE),
            re.compile(r"\bjust\s+found\s+your\s+stream\b", re.IGNORECASE),
            re.compile(r"\bbrand\s+new\s+streamer\b", re.IGNORECASE),
            re.compile(r"\bnew\s+streamer\s+(?:here|btw)?\b", re.IGNORECASE),
            re.compile(r"\bbin\s+neu\s+hier\b", re.IGNORECASE),
        ),
    ),
    (
        "streaming_leadin",
        2,
        (
            re.compile(r"\bwhat\s+got\s+you\s+into\s+streaming\b", re.IGNORECASE),
            re.compile(r"\bwhat\s+made\s+you\s+start\s+streaming\b", re.IGNORECASE),
            re.compile(r"\bhow\s+long\s+have\s+you\s+been\s+streaming\b", re.IGNORECASE),
            re.compile(r"\bwhat(?:'s|s)\s+your\s+stream(?:ing)?\s+schedule\b", re.IGNORECASE),
            re.compile(r"\bdo\s+you\s+stream\s+on\s+(?:youtube|yt)\b", re.IGNORECASE),
            re.compile(r"\bwie\s+lange\s+streamst\s+du\s+schon\b", re.IGNORECASE),
            re.compile(r"\bwelcome\s+to\s+(?:the\s+)?twitch\b", re.IGNORECASE),
        ),
    ),
    (
        "growth_pitch",
        3,
        (
            re.compile(r"\blet'?s\s+support\s+each\s+other\b", re.IGNORECASE),
            re.compile(r"\bi\s+can\s+help\s+you\s+grow\b", re.IGNORECASE),
            re.compile(r"\bboost\s+viewers?\b", re.IGNORECASE),
            re.compile(r"\bmore\s+viewers?\b", re.IGNORECASE),
            re.compile(r"\bi\s+work\s+with\s+streamers?\b", re.IGNORECASE),
            re.compile(r"\baffiliate\b", re.IGNORECASE),
            re.compile(r"\bpromot(?:e|ion)\b", re.IGNORECASE),
            re.compile(r"\bmehr\s+viewer\b", re.IGNORECASE),
            re.compile(r"\bhelfen?\s+zu\s+wachsen\b", re.IGNORECASE),
            re.compile(r"\btop\s+viewers?\b", re.IGNORECASE),
            re.compile(r"\bbest\s+viewers?\b", re.IGNORECASE),
        ),
    ),
    (
        "design_pitch",
        4,
        (
            re.compile(r"\bdo\s+you\s+have\s+a\s+logo\b", re.IGNORECASE),
            re.compile(r"\bneed\s+emotes?\b", re.IGNORECASE),
            re.compile(r"\boverlays?\b", re.IGNORECASE),
            re.compile(r"\bpanels?\b", re.IGNORECASE),
            re.compile(r"\bcustomi[sz]ed\s+panels?\b", re.IGNORECASE),
            re.compile(r"\bbanner\b", re.IGNORECASE),
            re.compile(r"\bgraphic(?:s)?\s+designer\b", re.IGNORECASE),
            re.compile(r"\bportfolio\b", re.IGNORECASE),
            re.compile(r"\bshow\s+(?:you\s+)?(?:some\s+of\s+)?my\s+work\b", re.IGNORECASE),
            re.compile(r"\bcommissions?\b", re.IGNORECASE),
            re.compile(r"\bbranding\b", re.IGNORECASE),
            re.compile(
                r"\bbrauchst\s+du\s+(?:ein\s+)?(?:logo|emotes?|overlay)\b",
                re.IGNORECASE,
            ),
        ),
    ),
    (
        "offplatform",
        4,
        (
            re.compile(r"\bcan\s+i\s+dm\s+you\b", re.IGNORECASE),
            re.compile(r"\badd\s+me\s+on\s+(?:discord|instagram)\b", re.IGNORECASE),
            re.compile(r"\badd\s+me\b", re.IGNORECASE),
            re.compile(r"\baccept\s+my\s+request\b", re.IGNORECASE),
            re.compile(r"\bcheck\s+your\s+whispers?\b", re.IGNORECASE),
            re.compile(r"\bi\s+sent\s+you\s+a\s+message\b", re.IGNORECASE),
            re.compile(r"\bclick\s+the\s+link\b", re.IGNORECASE),
            re.compile(r"\bsharing\s+something\b", re.IGNORECASE),
            re.compile(r"\bdiscord\b", re.IGNORECASE),
            re.compile(r"\binstagram\b", re.IGNORECASE),
            re.compile(r"\bdm\b", re.IGNORECASE),
            re.compile(r"\bwhisper\b", re.IGNORECASE),
        ),
    ),
    (
        "urgency_probe",
        2,
        (
            re.compile(r"\bquick\s+question\b", re.IGNORECASE),
            re.compile(r"\bcan\s+i\s+ask\s+you\s+something\b", re.IGNORECASE),
            re.compile(r"\bwon'?t\s+take\s+long\b", re.IGNORECASE),
            re.compile(r"\bjust\s+a\s+suggestion\b", re.IGNORECASE),
            re.compile(r"\bdon'?t\s+ignore\s+me\b", re.IGNORECASE),
        ),
    ),
    (
        "intrusive_probe",
        2,
        (
            re.compile(r"\bwhat(?:'s| is)\s+your\s+real\s+name\b", re.IGNORECASE),
            re.compile(r"\bwhere\s+do\s+you\s+live\b", re.IGNORECASE),
            re.compile(r"\bshar(?:e|ing)\s+(?:my|your)\s+address\b", re.IGNORECASE),
            re.compile(r"\bare\s+you\s+single\b", re.IGNORECASE),
            re.compile(r"\bface\s+reveal\b", re.IGNORECASE),
            re.compile(r"\bcan\s+you\s+turn\s+on\s+cam\b", re.IGNORECASE),
            re.compile(r"\bwru\s+from\b", re.IGNORECASE),
        ),
    ),
    (
        "greeting",
        1,
        (
            re.compile(r"\bhey+\b", re.IGNORECASE),
            re.compile(r"\bhi+\b", re.IGNORECASE),
            re.compile(r"\bhii+\b", re.IGNORECASE),
            re.compile(r"\bwhat(?:'s|s)\s+good\b", re.IGNORECASE),
            re.compile(r"\bw(?:hat)?\s+are\s+you\s+up\s+to\b", re.IGNORECASE),
            re.compile(r"\bwie\s+geht(?:s|['’]s)\b", re.IGNORECASE),
            re.compile(r"\balls?\s+gut\b", re.IGNORECASE),
        ),
    ),
    (
        "wellbeing",
        1,
        (
            re.compile(r"\bhow\s+(?:are|r)\s+(?:you|u)\b", re.IGNORECASE),
            re.compile(r"\bhru+\b", re.IGNORECASE),
            re.compile(r"\bwie\s+geht(?:s|['’]s)\b", re.IGNORECASE),
        ),
    ),
)

_GENERIC_PRAISE_RE = re.compile(
    r"\b(?:cool|amazing|nice\s+stream|love\s+your\s+vibe|you'?re\s+so\s+entertaining|this\s+is\s+awesome|great\s+content|setup\s+is\s+fire|awesome)\b",
    re.IGNORECASE,
)
_STREAM_CONTEXT_RE = re.compile(
    r"\b(?:deadlock|fight|boss|round|match|kill|build|lane|rank|aim|ability|ult|teamfight|objective|clip)\b",
    re.IGNORECASE,
)
_LINK_RE = re.compile(
    r"(?:https?://|www\.|discord\.gg/|bit\.ly/|t\.me/|linktr\.ee/|tinyurl\.com/)",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"(?:^|\s)@[A-Za-z0-9_.]{3,}\b")


class ServicePitchWarningMixin:
    def _init_service_pitch_warning(self) -> None:
        self._service_warning_log = Path("logs") / "twitch_service_warnings.log"
        self._service_warning_activity: dict[tuple[str, str], deque[tuple[float, int]]] = {}
        self._service_warning_message_history: dict[
            tuple[str, str], deque[tuple[float, str, set[str]]]
        ] = {}
        self._service_warning_first_seen: dict[tuple[str, str], float] = {}
        self._service_warning_channel_cd: dict[str, float] = {}
        self._service_warning_user_cd: dict[tuple[str, str], float] = {}
        self._service_warning_hint_cd: dict[tuple[str, str], float] = {}
        self._service_warning_account_age_cache: dict[str, tuple[float, int | None]] = {}
        self._service_warning_follower_cache: dict[str, tuple[float, int | None]] = {}

    @staticmethod
    def _normalize_text(content: str) -> str:
        return " ".join((content or "").strip().split())

    def _score_service_pitch_message(self, content: str) -> tuple[int, list[str], set[str]]:
        raw = (content or "").strip()
        if not raw:
            return 0, [], set()

        score = 0
        reasons: list[str] = []
        matched_features: set[str] = set()

        for feature, feature_score, patterns in _SERVICE_PATTERNS:
            if feature in matched_features:
                continue
            for pattern in patterns:
                if pattern.search(raw):
                    matched_features.add(feature)
                    score += int(feature_score)
                    reasons.append(f"feature:{feature}")
                    break

        lowered = raw.casefold()
        if _GENERIC_PRAISE_RE.search(raw):
            tokens = [t for t in re.split(r"\s+", lowered) if t]
            praise_score = 2 if (len(tokens) <= 5 and not _STREAM_CONTEXT_RE.search(raw)) else 1
            score += praise_score
            matched_features.add("generic_praise")
            reasons.append(f"feature:generic_praise({praise_score})")

        has_link = bool(_LINK_RE.search(raw))
        has_platform_ref = bool(
            re.search(r"\b(?:discord|instagram|tiktok|youtube|yt|ig)\b", lowered)
        )
        has_handle = bool(_HANDLE_RE.search(raw))
        if has_link or (has_platform_ref and has_handle):
            score += 4
            matched_features.add("external_link_or_handle")
            reasons.append("feature:external_link_or_handle")

        return score, reasons, matched_features

    @staticmethod
    def _prune_service_activity_bucket(bucket: deque[tuple[float, int]], now: float) -> None:
        while bucket and (now - float(bucket[0][0])) > float(_SERVICE_WARNING_WINDOW_SEC):
            bucket.popleft()

    @staticmethod
    def _prune_service_message_history_bucket(
        bucket: deque[tuple[float, str, set[str]]],
        now: float,
    ) -> None:
        while bucket and (now - float(bucket[0][0])) > float(_SERVICE_WARNING_SEQUENCE_WINDOW_SEC):
            bucket.popleft()

    @staticmethod
    def _token_count(content: str) -> int:
        if not content:
            return 0
        return len([part for part in re.split(r"\s+", content) if part])

    def _score_sequence_signals(
        self,
        bucket: deque[tuple[float, str, set[str]]],
    ) -> tuple[int, list[str]]:
        if len(bucket) < int(_SERVICE_WARNING_SEQUENCE_MIN_MSGS):
            return 0, []

        score = 0
        reasons: list[str] = []
        short_messages = sum(
            1
            for _, msg, _ in bucket
            if len(msg) <= int(_SERVICE_WARNING_SHORT_MSG_MAX_CHARS) and self._token_count(msg) <= 7
        )
        if short_messages >= int(_SERVICE_WARNING_SEQUENCE_MIN_MSGS):
            score += 2
            reasons.append("sequence:short_multi_line_burst")

        all_features: set[str] = set()
        for _, _, feats in bucket:
            all_features.update(feats)

        if "language_probe" in all_features and (
            "greeting" in all_features or "wellbeing" in all_features
        ):
            score += 2
            reasons.append("sequence:greeting_language_combo")

        if "streaming_leadin" in all_features and (
            "generic_praise" in all_features or "new_here" in all_features
        ):
            score += 2
            reasons.append("sequence:praise_or_new_plus_streaming_question")

        return score, reasons

    @staticmethod
    def _score_combo_signals(features: set[str]) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []

        if {"new_here", "growth_pitch"}.issubset(features):
            score += 3
            reasons.append("combo:new_here_plus_growth")
        if {"design_pitch", "offplatform"}.issubset(features):
            score += 3
            reasons.append("combo:design_plus_offplatform")
        if {"growth_pitch", "offplatform"}.issubset(features):
            score += 2
            reasons.append("combo:growth_plus_offplatform")
        if {"language_probe", "streaming_leadin"}.issubset(features):
            score += 2
            reasons.append("combo:language_plus_streaming")
        if {"greeting", "wellbeing", "language_probe"}.issubset(features):
            score += 2
            reasons.append("combo:greeting_wellbeing_language")

        return score, reasons

    def _early_window_score(
        self, channel_login: str, chatter_key: str, now: float
    ) -> tuple[int, list[str]]:
        key = (channel_login, chatter_key)
        first_seen = self._service_warning_first_seen.get(key)
        if first_seen is None:
            self._service_warning_first_seen[key] = now
            return 1, ["timing:first_appearance_window"]
        if (now - float(first_seen)) <= float(_SERVICE_WARNING_FIRST_CHAT_WINDOW_SEC):
            return 1, ["timing:first_appearance_window"]
        return 0, []

    @staticmethod
    def _prune_simple_monotonic_cache(
        cache: dict, now: float, *, max_len: int, max_age_sec: float
    ) -> None:
        if len(cache) <= max_len:
            return
        stale_before = now - float(max_age_sec)
        stale_keys = []
        for key, value in cache.items():
            if isinstance(value, tuple) and len(value) == 2:
                cache_ts = float(value[0])
            elif isinstance(value, (int, float)):
                cache_ts = float(value)
            else:
                stale_keys.append(key)
                continue
            if cache_ts < stale_before:
                stale_keys.append(key)
        for key in stale_keys:
            cache.pop(key, None)

    async def _get_account_age_days(self, author_id: str, author_login: str) -> int | None:
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
                created_at = created_at.replace(tzinfo=UTC)

            age_days = max(0, int((datetime.now(UTC) - created_at).days))
            self._service_warning_account_age_cache[cache_key] = (now, age_days)
            self._prune_simple_monotonic_cache(
                self._service_warning_account_age_cache,
                now,
                max_len=8192,
                max_age_sec=float(_SERVICE_WARNING_ACCOUNT_CACHE_TTL_SEC) * 4.0,
            )
            return age_days
        except Exception:
            log.debug("Konnte Account-Alter fuer %s nicht laden", cache_key, exc_info=True)
            self._service_warning_account_age_cache[cache_key] = (now, None)
            return None

    def _get_streamer_followers_hint(self, channel_login: str) -> int | None:
        login = (channel_login or "").strip().lower().lstrip("#")
        if not login:
            return None

        now = time.monotonic()
        cached = self._service_warning_follower_cache.get(login)
        if isinstance(cached, tuple) and len(cached) == 2:
            cached_ts, cached_count = cached
            if (now - float(cached_ts)) <= float(_SERVICE_WARNING_FOLLOWER_CACHE_TTL_SEC):
                return cached_count

        follower_count: int | None = None
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
        self._prune_simple_monotonic_cache(
            self._service_warning_follower_cache,
            now,
            max_len=2048,
            max_age_sec=float(_SERVICE_WARNING_FOLLOWER_CACHE_TTL_SEC) * 4.0,
        )
        return follower_count

    def _is_low_follower_target(self, channel_login: str) -> tuple[bool, int | None]:
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
        follower_count: int | None,
        score: int,
        message_count: int,
        severity: str,
        reasons: list[str],
        content: str,
    ) -> None:
        try:
            self._service_warning_log.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(UTC).isoformat()
            reason_text = ",".join(reasons) if reasons else "-"
            follower_text = "-" if follower_count is None else str(follower_count)
            safe_content = (content or "").replace("\n", " ").strip()[:350]
            line = (
                f"{ts}\t{severity}\t{channel_login}\t{chatter_login or '-'}\t{chatter_id or '-'}\t"
                f"age_days={account_age_days}\tfollowers={follower_text}\tscore={score}\t"
                f"msgs={message_count}\t{reason_text}\t{safe_content}\n"
            )
            with self._service_warning_log.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:
            log.debug("Konnte Service-Warnung nicht loggen", exc_info=True)

    @staticmethod
    def _build_service_warning_text(*, chatter_login: str, strong: bool) -> str:
        mention = f"@{chatter_login} " if chatter_login else ""
        if strong:
            return (
                f"🛡️ {mention}wurde als potenzieller Service-Pitcher erkannt (verkauft oft Designs/Viewer/Scam). "
                "Empfehlung an Streamer/Mods: Ignorieren & Bannen."
            )
        return (
            f"[Hinweis] {mention}bitte keine Service-/Promo-Angebote im Chat "
            "(inkl. DM/externen Link-Angeboten). Danke."
        )

    async def _maybe_warn_service_pitch(self, message, *, channel_login: str) -> bool:
        raw_content = self._normalize_text(str(getattr(message, "content", "") or ""))
        if not raw_content:
            return False
        if raw_content.startswith(self.prefix or "!"):
            return False

        author = getattr(message, "author", None)
        if author is None:
            return False
        if bool(getattr(author, "moderator", False)) or bool(getattr(author, "broadcaster", False)):
            return False

        score, reasons, features = self._score_service_pitch_message(raw_content)
        if score <= 0:
            return False

        chatter_login = (getattr(author, "name", "") or "").strip().lower()
        chatter_id = str(getattr(author, "id", "") or "").strip()
        chatter_key = chatter_login or chatter_id or "unknown"
        now = time.monotonic()

        account_age_days = await self._get_account_age_days(chatter_id, chatter_login)
        if account_age_days is None or int(account_age_days) >= int(
            _SERVICE_WARNING_ACCOUNT_MAX_DAYS
        ):
            return False
        score += 2
        reasons.append("account:newer_than_3_months")
        features.add("new_account")

        is_low_target, follower_count = self._is_low_follower_target(channel_login)
        if not is_low_target:
            return False
        if follower_count is None:
            reasons.append("target:unknown_followers_assume_small")
        else:
            reasons.append(f"target:followers_{follower_count}")
            if follower_count <= int(_SERVICE_WARNING_MAX_FOLLOWERS // 2):
                score += 1
                reasons.append("target:very_small_channel")

        combo_score, combo_reasons = self._score_combo_signals(features)
        if combo_score > 0:
            score += combo_score
            reasons.extend(combo_reasons)

        early_score, early_reasons = self._early_window_score(channel_login, chatter_key, now)
        if early_score > 0:
            score += early_score
            reasons.extend(early_reasons)

        bucket_key = (channel_login, chatter_key)
        history_bucket = self._service_warning_message_history.setdefault(bucket_key, deque())
        history_bucket.append((now, raw_content, set(features)))
        self._prune_service_message_history_bucket(history_bucket, now)

        sequence_score, sequence_reasons = self._score_sequence_signals(history_bucket)
        if sequence_score > 0:
            score += sequence_score
            reasons.extend(sequence_reasons)

        bucket = self._service_warning_activity.setdefault(bucket_key, deque())
        bucket.append((now, int(score)))
        self._prune_service_activity_bucket(bucket, now)

        total_score = int(sum(int(item[1]) for item in bucket))
        msg_count = len(bucket)
        if total_score < int(_SERVICE_WARNING_MIN_SCORE) or msg_count < int(
            _SERVICE_WARNING_MIN_MESSAGES
        ):
            return False
        if total_score < int(_SERVICE_WARNING_LIGHT_THRESHOLD):
            return False

        self._prune_simple_monotonic_cache(
            self._service_warning_first_seen,
            now,
            max_len=8192,
            max_age_sec=max(float(_SERVICE_WARNING_FIRST_CHAT_WINDOW_SEC) * 20.0, 3600.0),
        )

        severity = "HINT"
        if total_score >= int(_SERVICE_WARNING_STRONG_THRESHOLD):
            severity = "WARNING_STRONG"
        elif total_score >= int(_SERVICE_WARNING_PUBLIC_THRESHOLD):
            severity = "WARNING_PUBLIC"

        if severity == "HINT":
            hint_cd_until = float(self._service_warning_hint_cd.get(bucket_key, 0.0))
            if now < hint_cd_until:
                return False
        else:
            channel_cd_until = float(self._service_warning_channel_cd.get(channel_login, 0.0))
            user_cd_until = float(self._service_warning_user_cd.get(bucket_key, 0.0))
            if now < channel_cd_until or now < user_cd_until:
                # Escalation Logic: If user is on cooldown (already warned) BUT triggers a STRONG warning again,
                # we escalate to Timeout if possible.
                if severity == "WARNING_STRONG" and now < user_cd_until:
                    # Check if we can timeout
                    channel = (
                        self._resolve_message_channel(message)
                        if hasattr(self, "_resolve_message_channel")
                        else None
                    )
                    if channel is None:
                        channel = getattr(message, "channel", None)

                    if channel:
                        # Escalation: Timeout + Final Warning
                        try:
                            # 10 Minuten Timeout als Denkzettel
                            if hasattr(channel, "timeout"):
                                await channel.timeout(
                                    chatter_id, 600, "Service-Pitch / Spam Escalation"
                                )
                            elif hasattr(self, "timeout_user"):
                                await self.timeout_user(
                                    getattr(channel, "id", None) or channel_login,
                                    chatter_id,
                                    600,
                                    "Service-Pitch / Spam Escalation",
                                )

                            escalation_text = (
                                f"🛡️ @{chatter_login} Timeout (10m) wegen wiederholter Service-Pitches/Spam. "
                                "Empfehlung: User bannen, falls das Verhalten anhält."
                            )
                            await self._send_chat_message(
                                channel, escalation_text, source="service_warning"
                            )
                            # Reset cooldown to avoid double-triggering immediately
                            self._service_warning_user_cd[bucket_key] = now + float(
                                _SERVICE_WARNING_USER_COOLDOWN_SEC
                            )
                            self._record_service_warning(
                                channel_login=channel_login,
                                chatter_login=chatter_login,
                                chatter_id=chatter_id,
                                account_age_days=int(account_age_days),
                                follower_count=follower_count,
                                score=total_score,
                                message_count=msg_count,
                                severity="ESCALATED_TIMEOUT",
                                reasons=reasons + ["escalation:ignored_previous_warning"],
                                content=raw_content,
                            )
                            return True
                        except Exception:
                            log.debug("Escalation Timeout failed", exc_info=True)

                return False

        if severity != "HINT":
            channel = (
                self._resolve_message_channel(message)
                if hasattr(self, "_resolve_message_channel")
                else None
            )
            if channel is None:
                channel = getattr(message, "channel", None)
            if channel is None:
                return False
            warning_text = self._build_service_warning_text(
                chatter_login=chatter_login,
                strong=(severity == "WARNING_STRONG"),
            )
            sent = await self._send_chat_message(channel, warning_text, source="service_warning")
            if not sent:
                return False

        if severity != "HINT":
            self._service_warning_channel_cd[channel_login] = now + float(
                _SERVICE_WARNING_CHANNEL_COOLDOWN_SEC
            )
            self._service_warning_user_cd[bucket_key] = now + float(
                _SERVICE_WARNING_USER_COOLDOWN_SEC
            )
            bucket.clear()
            history_bucket.clear()
        else:
            self._service_warning_hint_cd[bucket_key] = now + float(
                _SERVICE_WARNING_HINT_COOLDOWN_SEC
            )

        self._record_service_warning(
            channel_login=channel_login,
            chatter_login=chatter_login,
            chatter_id=chatter_id,
            account_age_days=int(account_age_days),
            follower_count=follower_count,
            score=total_score,
            message_count=msg_count,
            severity=severity,
            reasons=reasons,
            content=raw_content,
        )
        return True
