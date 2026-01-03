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

    # 5) Raid-Autorisierung (OAuth User Access Tokens)
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

    # 6) Raid-History (Metadaten zu durchgeführten Raids)
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
