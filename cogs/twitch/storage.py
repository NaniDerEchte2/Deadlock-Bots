# cogs/twitch/storage.py
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("TwitchStreams")

# --- zentrale DB anbinden, mit Fallback auf lokale Datei --------------------
try:
    from service.db import get_conn as _central_get_conn  # type: ignore
    from service.db import db_path as _central_db_path  # type: ignore
except Exception:
    _central_get_conn = None  # type: ignore[assignment]
    _central_db_path = None  # type: ignore[assignment]

if _central_get_conn is None:
    _fallback = os.getenv("DEADLOCK_DB_PATH")
    if not _fallback:
        if _central_db_path is not None:
            _fallback = _central_db_path()
        else:
            user_profile = os.environ.get("USERPROFILE")
            if user_profile:
                base_dir = Path(user_profile) / "Documents" / "Deadlock" / "service"
            else:
                base_dir = Path.home() / "Documents" / "Deadlock" / "service"
            _fallback = str(base_dir / "deadlock.sqlite3")
    _FALLBACK_PATH = _fallback
else:
    _FALLBACK_PATH = _central_db_path() if "_central_db_path" in globals() and _central_db_path else None

if _FALLBACK_PATH:
    Path(_FALLBACK_PATH).parent.mkdir(parents=True, exist_ok=True)


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
    """
    Contextmanager für eine SQLite-Connection.
    - Nutzt die zentrale DB (service.db.get_conn), wenn verfügbar.
    - Fällt ansonsten auf eine lokale Datei (deadlock.db) zurück.
    Wichtig: In *jedem* Zweig muss 'yield' verwendet werden (kein 'return' eines Generators)!
    """
    # Versuch: zentrale DB
    if _central_get_conn:
        try:
            cm = _central_get_conn()  # liefert selbst einen Contextmanager
        except Exception:
            log.exception("Zentrale DB nicht verfügbar – nutze lokalen Fallback")
            cm = None
        if cm is not None:
            with cm as conn:  # type: ignore[misc]
                ensure_schema(conn)
                yield conn
                return

    # Fallback: lokale Datei
    with _fallback_ctx() as conn:
        yield conn


# --- Schema / Migration -----------------------------------------------------

def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}  # Spaltenname ist Index 1

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
            twitch_login               TEXT PRIMARY KEY,
            twitch_user_id             TEXT,
            require_discord_link       INTEGER DEFAULT 0,
            next_link_check_at         TEXT,
            discord_user_id            TEXT,
            discord_display_name       TEXT,
            is_on_discord              INTEGER DEFAULT 0,
            manual_verified_permanent  INTEGER DEFAULT 0,
            manual_verified_until      TEXT,
            manual_verified_at         TEXT,
            manual_partner_opt_out     INTEGER DEFAULT 0,
            created_at                 TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for col, spec in [
        ("twitch_user_id", "TEXT"),
        ("require_discord_link", "INTEGER DEFAULT 0"),
        ("next_link_check_at", "TEXT"),
        ("discord_user_id", "TEXT"),
        ("discord_display_name", "TEXT"),
        ("is_on_discord", "INTEGER DEFAULT 0"),
        ("manual_verified_permanent", "INTEGER DEFAULT 0"),
        ("manual_verified_until", "TEXT"),
        ("manual_verified_at", "TEXT"),
        ("manual_partner_opt_out", "INTEGER DEFAULT 0"),
        ("created_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
    ]:
        _add_column_if_missing(conn, "twitch_streamers", col, spec)

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_twitch_streamers_user_id ON twitch_streamers(twitch_user_id)"
    )

    # 2) twitch_live_state
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
    # Neue/zusätzliche Spalten für neuere Cog-Versionen:
    _add_column_if_missing(conn, "twitch_live_state", "last_seen_at", "TEXT")
    _add_column_if_missing(conn, "twitch_live_state", "last_game", "TEXT")
    _add_column_if_missing(conn, "twitch_live_state", "last_viewer_count", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "twitch_live_state", "last_tracking_token", "TEXT")

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

    # 4) Link-Klick-Tracking
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_link_clicks (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            clicked_at       TEXT    DEFAULT CURRENT_TIMESTAMP,
            streamer_login   TEXT    NOT NULL,
            tracking_token   TEXT,
            discord_user_id  TEXT,
            discord_username TEXT,
            guild_id         TEXT,
            channel_id       TEXT,
            message_id       TEXT,
            ref_code         TEXT,
            source_hint      TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_link_clicks_streamer ON twitch_link_clicks(streamer_login)"
    )

