import re
from typing import List, Set

# Whitelist für bekannte legitime Bots (keine Spam-Prüfung)
_WHITELISTED_BOTS = {
    "streamelements",
    "nightbot",
    "streamlabs",
    "moobot",
    "fossabot",
    "wizebot",
    "pretzelrocks",
    "soundalerts",
}

_SPAM_PHRASES = (
    "Best viewers streamboo.com",
    "Best viewers streamboo .com",
    "Best viewers streamboo com",
    "Best viewers smmtop32.online",
    "Best viewers smmtop32 .online",
    "Best viewers smmtop32 online",
    "Best viewers on",
    "Best viewers",
    "B̟est viewers",
    "Cheap Viewers",
    "Ch͟eap viewers",
    "(remove the space)",
    "Cool overlay \N{THUMBS UP SIGN} Honestly, it\N{RIGHT SINGLE QUOTATION MARK}s so hard to get found on the directory lately. I have small tips on beating the algorithm. Mind if I send you an share?",
    "Mind if I send you an share",
    " Viewers https://smmbest5.online",
    "Viewers smmbest4.online",
    "Viewers streamboo .com",
    "Viewers smmhype12.ru",
    "Viewers smmhype1.ru",
    "Viewers smmhype",
    "viewers on streamboo .com (remove the space)",
    "Hey friend I really enjoy your content so I give you a follow I’d love to be a friend and of you feel free to Add me on Discord",
)
# Entferne "viewer" und "viewers" aus den Fragmenten - zu allgemein und führt zu False Positives
_SPAM_FRAGMENTS = (
    "best viewers",  # Nur die Kombination ist verdächtig
    "cheap viewers",  # Nur die Kombination ist verdächtig
    "streamboo.com",
    "streamboo .com",
    "streamboo com",
    "streamboo",
    "smmtop32.online",
    "smmtop32 .online",
    "smmtop32 online",
    "smmtop32",
    "remove the space",
    "cool overlay",
    "get found on the directory",
    "beating the algorithm",
    "d!sc",
    "smmbest4.online",
    "smmbest5.online",
    "rookie",
    "smmhype12.ru",
    "smmhype1.ru",
    "smmhype",
    "topsmm3.ru",
    "topsmm3 .ru",
    "topsmm3 ru",
    "topsmm3",
)
_SPAM_MIN_MATCHES = 3

# ---------------------------------------------------------------------------
# Periodische Chat-Promos
# ---------------------------------------------------------------------------
_PROMO_MESSAGES: List[str] = [
    "heyo! Falls ihr bock habt auf Deadlock und noch eine deutsche Community sucht – schau gerne mal vorbei: {invite}",
    "Hey! Noch eine deutsche Deadlock-Community am suchen? Wir sind hier: {invite} 🎮",
    "Falls du noch eine deutsche Deadlock-Community sucht – schau doch mal vorbei: {invite}",
]

_PROMO_DISCORD_INVITE: str = "https://discord.gg/z5TfVHuQq2"
_PROMO_INTERVAL_MIN: int = 30

# Promo-Activity (ohne ENV; hier direkt konfigurieren)
_PROMO_ACTIVITY_ENABLED: bool = True
_PROMO_CHANNEL_ALLOWLIST: Set[str] = set()
_PROMO_ACTIVITY_WINDOW_MIN: int = 8
_PROMO_ACTIVITY_MIN_MSGS: int = 5
_PROMO_ACTIVITY_MIN_CHATTERS: int = 1
_PROMO_ACTIVITY_MIN_RAW_MSGS_SINCE_PROMO: int = 16
_PROMO_ACTIVITY_TARGET_MPM: float = 3.0
_PROMO_ACTIVITY_CHATTER_DEDUP_SEC: int = 30 #derselbe Chatter zählt höchstens einmal alle x Sekunden
_PROMO_COOLDOWN_MIN: int = 30
_PROMO_COOLDOWN_MAX: int = 120
_PROMO_OVERALL_COOLDOWN_MIN: int = 20
_PROMO_ATTEMPT_COOLDOWN_MIN: int = 5
_PROMO_IGNORE_COMMANDS: bool = True
_PROMO_LOOP_INTERVAL_SEC: int = 60

# Periodischer Fallback: wenn Chat still ist, aber Viewer über "normal" liegen
_PROMO_VIEWER_SPIKE_ENABLED: bool = True
_PROMO_VIEWER_SPIKE_COOLDOWN_MIN: int = 90
_PROMO_VIEWER_SPIKE_MIN_CHAT_SILENCE_SEC: int = 300
_PROMO_VIEWER_SPIKE_MIN_RATIO: float = 1.10
_PROMO_VIEWER_SPIKE_MIN_DELTA: int = 2
_PROMO_VIEWER_SPIKE_MIN_SESSIONS: int = 3
_PROMO_VIEWER_SPIKE_SESSION_SAMPLE_LIMIT: int = 20
_PROMO_VIEWER_SPIKE_STATS_SAMPLE_LIMIT: int = 240
_PROMO_VIEWER_SPIKE_MIN_STATS_SAMPLES: int = 40

if _PROMO_COOLDOWN_MAX < _PROMO_COOLDOWN_MIN:
    _PROMO_COOLDOWN_MAX = _PROMO_COOLDOWN_MIN

# ---------------------------------------------------------------------------
# Deadlock Zugangsfragen (Invite-Only Hinweise)
# ---------------------------------------------------------------------------
_DEADLOCK_INVITE_REPLY: str = (
    "Wenn du einen Zugang benötigst, schau gerne auf unserem Discord vorbei, "
    "dort bekommst du eine Einladung und Hilfe beim Einstieg :) {invite}"
)
_INVITE_QUESTION_CHANNEL_COOLDOWN_SEC: int = 120
_INVITE_QUESTION_USER_COOLDOWN_SEC: int = 3600
_INVITE_QUESTION_RE = re.compile(
    r"\b(wie|wo|wann|wieso|warum|woher)\b"
    r"|\b(kann|darf)\s+man\b"
    r"|\b(bekomm|krieg|erhalt)\w*\s+(man|ich)\b",
    re.IGNORECASE,
)
_INVITE_ACCESS_RE = re.compile(
    r"\b(spielen|spiel|play|zugang|einladung|invite|beta|key|access|ea|early\s*access|reinkomm\w*|rankomm\w*)\b",
    re.IGNORECASE,
)
_INVITE_STRONG_ACCESS_RE = re.compile(
    r"\b(zugang|einladung|invite|beta|key|access|ea|early\s*access|reinkomm\w*|rankomm\w*)\b",
    re.IGNORECASE,
)
_INVITE_GAME_CONTEXT_RE = re.compile(
    r"\b(game|spiel)\b",
    re.IGNORECASE,
)
