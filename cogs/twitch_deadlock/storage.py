# =========================================
# cogs/twitch_deadlock/storage.py
# =========================================
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

DEFAULT_DB = os.path.join(os.path.expanduser("~"), "Documents", "Deadlock", "service", "deadlock.sqlite3")

# Use existing central DB location, honoring env used in the rest of the project
DB_PATH = (
    os.getenv("DEADLOCK_DB_PATH")
    or (os.path.join(os.getenv("DEADLOCK_DB_DIR", ""), "deadlock.sqlite3") if os.getenv("DEADLOCK_DB_DIR") else DEFAULT_DB)
)

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

PRAGMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS twitch_streamers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    twitch_login TEXT NOT NULL UNIQUE,
    twitch_user_id TEXT,
    require_discord_link INTEGER NOT NULL DEFAULT 0,
    last_description TEXT,
    last_link_ok INTEGER NOT NULL DEFAULT 0,
    added_by TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS twitch_settings (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    language_filter TEXT DEFAULT NULL,
    required_marker TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS twitch_live_state (
    twitch_user_id TEXT PRIMARY KEY,
    streamer_login TEXT NOT NULL,
    last_stream_id TEXT,
    last_started_at TEXT,
    last_title TEXT,
    last_game_id TEXT,
    is_live INTEGER NOT NULL DEFAULT 0,
    last_discord_message_id TEXT,
    last_notified_at DATETIME
);
"""

@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    # Using sqlite3 directly but against the central file that all cogs share.
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(PRAGMA_SQL)
        conn.executescript(SCHEMA_SQL)
        yield conn
    finally:
        conn.close()
