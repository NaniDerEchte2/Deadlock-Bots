# cogs/twitch_deadlock/storage.py
import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterable

log = logging.getLogger("TwitchDeadlock")

# --- zentrale DB anbinden, mit Fallback auf lokale Datei --------------------
try:
    from service.db import get_conn as _central_get_conn  # type: ignore
except Exception:
    _central_get_conn = None
    _FALLBACK_PATH = os.getenv("DEADLOCK_DB_PATH") or os.path.join(os.getcwd(), "deadlock.db")
    os.makedirs(os.path.dirname(_FALLBACK_PATH), exist_ok=True)

    @contextmanager
    def _fallback_ctx():
        conn = sqlite3.connect(_FALLBACK_PATH)
        conn.row_factory = sqlite3.Row
        try:
            ensure_schema(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()


def get_conn():
    """
    Liefert einen Context-Manager, der beim Öffnen `ensure_schema()` ausführt.
    Bevorzugt die zentrale DB aus service.db, sonst Fallback-Datei.
    """
    if _central_get_conn:
        cm = _central_get_conn()

        class _Wrapper:
            def __enter__(self):
                self._conn = cm.__enter__()
                # zentrale DB -> Schema bei Bedarf anheben
                ensure_schema(self._conn)
                return self._conn

            def __exit__(self, exc_type, exc, tb):
                return cm.__exit__(exc_type, exc, tb)

        return _Wrapper()
    else:
        return _fallback_ctx()


# --- Schema / Migration -----------------------------------------------------

def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")  # Spalten introspektieren
    return {row[1] for row in cur.fetchall()}          # name ist Index 1
# Siehe Doku zu PRAGMA table_info. :contentReference[oaicite:1]{index=1}

def _add_column_if_missing(conn: sqlite3.Connection, table: str, name: str, spec: str) -> None:
    cols = _columns(conn, table)
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
        log.info("DB: added column %s.%s", table, name)
# SQLite hat kein "ADD COLUMN IF NOT EXISTS" → erst via PRAGMA prüfen. :contentReference[oaicite:2]{index=2}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Erstellt fehlende Tabellen/Spalten. Idempotent."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # 1) twitch_streamers
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_streamers (
            twitch_login           TEXT PRIMARY KEY,
            twitch_user_id         TEXT,
            require_discord_link   INTEGER DEFAULT 0,
            last_description       TEXT,
            last_link_ok           INTEGER DEFAULT 0,
            last_link_checked_at   TEXT,
            next_link_check_at     TEXT,
            created_at             TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # bestehende Alt-Tabellen anheben
    for col, spec in [
        ("twitch_user_id", "TEXT"),
        ("require_discord_link", "INTEGER DEFAULT 0"),
        ("last_description", "TEXT"),
        ("last_link_ok", "INTEGER DEFAULT 0"),
        ("last_link_checked_at", "TEXT"),
        ("next_link_check_at", "TEXT"),
        ("created_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
    ]:
        _add_column_if_missing(conn, "twitch_streamers", col, spec)

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_twitch_streamers_user_id ON twitch_streamers(twitch_user_id)"
    )
    # (INSERT OR IGNORE nutzt diese PK/UNIQUEs als Konfliktziel. :contentReference[oaicite:3]{index=3})

    # 2) twitch_live_state – wird per ON CONFLICT(twitch_user_id) upserted
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_live_state (
            twitch_user_id            TEXT PRIMARY KEY,
            streamer_login            TEXT NOT NULL,
            last_stream_id            TEXT,
            last_started_at           TEXT,
            last_title                TEXT,
            last_game_id              TEXT,
            last_discord_message_id   TEXT,
            last_notified_at          TEXT,
            is_live                   INTEGER DEFAULT 0
        )
        """
    )

    # 3) twitch_stream_logs – periodische Samples
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_stream_logs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             TEXT DEFAULT CURRENT_TIMESTAMP,
            streamer_login TEXT,
            user_id        TEXT,
            title          TEXT,
            viewers        INTEGER,
            started_at     TEXT,
            language       TEXT,
            game_id        TEXT,
            game_name      TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_stream_logs_login_ts ON twitch_stream_logs(streamer_login, ts)"
    )

    # 4) twitch_settings – Channel je Guild
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_settings (
            guild_id        INTEGER PRIMARY KEY,
            channel_id      INTEGER,
            language_filter TEXT,
            required_marker TEXT
        )
        """
    )
