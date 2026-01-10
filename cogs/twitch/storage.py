# cogs/twitch/storage.py
import logging
import sqlite3
from contextlib import contextmanager

from service import db as central_db

log = logging.getLogger("TwitchStreams")


@contextmanager
def get_conn():
    """
    Contextmanager fuer eine SQLite-Connection.
    - Nutzt ausschliesslich die zentrale DB (service.db.get_conn) als einzige Quelle.
    """
    with central_db.get_conn() as conn:
        ensure_schema(conn)
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
    # NOTE: PRAGMAs (journal_mode, foreign_keys, etc.) are already set by
    # the central DB (service/db.py). Setting them again can corrupt the connection
    # in multi-threaded environments. DO NOT add PRAGMA calls here.

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
        ("raid_bot_enabled", "INTEGER DEFAULT 0"),  # Auto-Raid Opt-in/out (default: off)
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
    _add_column_if_missing(conn, "twitch_live_state", "active_session_id", "INTEGER")
    _add_column_if_missing(conn, "twitch_live_state", "had_deadlock_in_session", "INTEGER DEFAULT 0")

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

    # 5) Stream Sessions & Engagement
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_stream_sessions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            streamer_login     TEXT NOT NULL,
            stream_id          TEXT,
            started_at         TEXT NOT NULL,
            ended_at           TEXT,
            duration_seconds   INTEGER DEFAULT 0,
            start_viewers      INTEGER DEFAULT 0,
            peak_viewers       INTEGER DEFAULT 0,
            end_viewers        INTEGER DEFAULT 0,
            avg_viewers        REAL    DEFAULT 0,
            samples            INTEGER DEFAULT 0,
            retention_5m       REAL,
            retention_10m      REAL,
            retention_20m      REAL,
            dropoff_pct        REAL,
            dropoff_label      TEXT,
            unique_chatters    INTEGER DEFAULT 0,
            first_time_chatters INTEGER DEFAULT 0,
            returning_chatters INTEGER DEFAULT 0,
            followers_start    INTEGER,
            followers_end      INTEGER,
            follower_delta     INTEGER,
            notes              TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_sessions_login ON twitch_stream_sessions(streamer_login, started_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_sessions_open ON twitch_stream_sessions(streamer_login) WHERE ended_at IS NULL"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_session_viewers (
            session_id        INTEGER NOT NULL,
            ts_utc            TEXT    NOT NULL,
            minutes_from_start INTEGER,
            viewer_count      INTEGER NOT NULL,
            PRIMARY KEY (session_id, ts_utc)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_session_viewers_session ON twitch_session_viewers(session_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_session_chatters (
            session_id          INTEGER NOT NULL,
            streamer_login      TEXT    NOT NULL,
            chatter_login       TEXT    NOT NULL,
            chatter_id          TEXT,
            first_message_at    TEXT    NOT NULL,
            messages            INTEGER DEFAULT 0,
            is_first_time_global INTEGER DEFAULT 0,
            PRIMARY KEY (session_id, chatter_login)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_session_chatters_login ON twitch_session_chatters(streamer_login, session_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_chatter_rollup (
            streamer_login   TEXT NOT NULL,
            chatter_login    TEXT NOT NULL,
            chatter_id       TEXT,
            first_seen_at    TEXT NOT NULL,
            last_seen_at     TEXT NOT NULL,
            total_messages   INTEGER DEFAULT 0,
            total_sessions   INTEGER DEFAULT 0,
            PRIMARY KEY (streamer_login, chatter_login)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_chat_messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER NOT NULL,
            streamer_login  TEXT NOT NULL,
            chatter_login   TEXT,
            message_ts      TEXT NOT NULL,
            is_command      INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_chat_messages_session ON twitch_chat_messages(session_id, message_ts)"
    )

    # 6) Raid-Autorisierung (OAuth User Access Tokens)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_raid_auth (
            twitch_user_id       TEXT PRIMARY KEY,
            twitch_login         TEXT NOT NULL,
            access_token         TEXT NOT NULL,
            refresh_token        TEXT NOT NULL,
            token_expires_at     TEXT NOT NULL,
            scopes               TEXT NOT NULL,
            authorized_at        TEXT DEFAULT CURRENT_TIMESTAMP,
            last_refreshed_at    TEXT,
            raid_enabled         INTEGER DEFAULT 1,
            created_at           TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_twitch_raid_auth_login ON twitch_raid_auth(twitch_login)"
    )

    # Safety: Disable auto-raid for streamer entries without an active OAuth grant.
    try:
        conn.execute(
            """
            UPDATE twitch_streamers
            SET raid_bot_enabled = 0
            WHERE (raid_bot_enabled IS NULL OR raid_bot_enabled = 1)
              AND twitch_user_id IS NOT NULL
              AND twitch_user_id NOT IN (
                  SELECT twitch_user_id FROM twitch_raid_auth WHERE raid_enabled = 1
              )
            """
        )
        conn.commit()
    except Exception:
        log.debug("Could not apply auto-raid safety migration", exc_info=True)

    # 7) Raid-History (Metadaten zu durchgeführten Raids)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_raid_history (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            from_broadcaster_id   TEXT NOT NULL,
            from_broadcaster_login TEXT NOT NULL,
            to_broadcaster_id     TEXT NOT NULL,
            to_broadcaster_login  TEXT NOT NULL,
            viewer_count          INTEGER DEFAULT 0,
            stream_duration_sec   INTEGER,
            reason                TEXT,
            executed_at           TEXT DEFAULT CURRENT_TIMESTAMP,
            success               INTEGER DEFAULT 1,
            error_message         TEXT,
            target_stream_started_at TEXT,
            candidates_count      INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_raid_history_from ON twitch_raid_history(from_broadcaster_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_raid_history_to ON twitch_raid_history(to_broadcaster_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_raid_history_executed ON twitch_raid_history(executed_at)"
    )
