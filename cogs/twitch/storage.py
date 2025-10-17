# cogs/twitch/storage.py
import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterable

log = logging.getLogger("TwitchStreams")

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
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_conn():
    """Gibt einen Connection-Context zurück (zentral wenn verfügbar, sonst lokale Datei)."""
    if _central_get_conn:
        with _central_get_conn() as conn:  # type: ignore[misc]
            try:
                ensure_schema(conn)
                yield conn
            finally:
                pass
    else:
        return _fallback_ctx()


# --- Schema / Migration -----------------------------------------------------

def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")  # Spalten introspektieren
    return {row[1] for row in cur.fetchall()}          # name ist Index 1

def _add_column_if_missing(conn: sqlite3.Connection, table: str, name: str, spec: str) -> None:
    cols = _columns(conn, table)
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
        log.info("DB: added column %s.%s", table, name)


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
            next_link_check_at     TEXT,
            manual_verified_permanent INTEGER DEFAULT 0,
            manual_verified_until  TEXT,
            manual_verified_at     TEXT,
            created_at             TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for col, spec in [
        ("twitch_user_id", "TEXT"),
        ("require_discord_link", "INTEGER DEFAULT 0"),
        ("next_link_check_at", "TEXT"),
        ("manual_verified_permanent", "INTEGER DEFAULT 0"),
        ("manual_verified_until", "TEXT"),
        ("manual_verified_at", "TEXT"),
        ("created_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
    ]:
        _add_column_if_missing(conn, "twitch_streamers", col, spec)

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_twitch_streamers_user_id ON twitch_streamers(twitch_user_id)"
    )

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
    # Additive columns used by newer cog versions
    _add_column_if_missing(conn, "twitch_live_state", "last_seen_at", "TEXT")
    _add_column_if_missing(conn, "twitch_live_state", "last_game", "TEXT")
    _add_column_if_missing(conn, "twitch_live_state", "last_viewer_count", "INTEGER DEFAULT 0")

    # 3) Stats-Logs
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_stats_tracked (
            ts_utc       TEXT,
            streamer     TEXT,
            viewer_count INTEGER,
            is_partner   INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_stats_category (
            ts_utc       TEXT,
            streamer     TEXT,
            viewer_count INTEGER,
            is_partner   INTEGER DEFAULT 0
        )
        """
    )
    _add_column_if_missing(conn, "twitch_stats_tracked", "is_partner", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "twitch_stats_category", "is_partner", "INTEGER DEFAULT 0")

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
