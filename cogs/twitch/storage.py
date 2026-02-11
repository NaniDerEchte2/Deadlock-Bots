# cogs/twitch/storage.py
import logging
import re
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

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COLUMN_SPEC_RE = re.compile(r"^[A-Za-z0-9_ (),'%.-]+$")


def _quote_identifier(identifier: str) -> str:
    if not _IDENTIFIER_RE.match(identifier):
        raise ValueError(f"Invalid identifier: {identifier!r}")
    return f'"{identifier}"'


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    _quote_identifier(table)
    cur = conn.execute("SELECT name FROM pragma_table_info(?)", (table,))
    return {row[0] for row in cur.fetchall()}

def _build_add_column_statement(table_ident: str, name_ident: str, spec: str) -> str:
    return "".join(["ALTER TABLE ", table_ident, " ADD COLUMN ", name_ident, " ", spec])

def _add_column_if_missing(conn: sqlite3.Connection, table: str, name: str, spec: str) -> None:
    table_ident = _quote_identifier(table)
    name_ident = _quote_identifier(name)
    if not _COLUMN_SPEC_RE.match(spec):
        raise ValueError(f"Invalid column spec: {spec!r}")
    cols = _columns(conn, table)
    if name not in cols:
        statement = _build_add_column_statement(table_ident, name_ident, spec)
        conn.execute(statement)
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
        ("archived_at", "TEXT"),
        ("raid_bot_enabled", "INTEGER DEFAULT 0"),  # Auto-Raid Opt-in/out (default: off)
        ("silent_ban", "INTEGER DEFAULT 0"),  # 1 = suppress auto-ban chat notifications
        ("silent_raid", "INTEGER DEFAULT 0"),  # 1 = suppress raid arrival chat notifications
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
    _add_column_if_missing(conn, "twitch_live_state", "last_deadlock_seen_at", "TEXT")

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
    _add_column_if_missing(conn, "twitch_stats_tracked", "game_name", "TEXT")
    _add_column_if_missing(conn, "twitch_stats_tracked", "stream_title", "TEXT")
    _add_column_if_missing(conn, "twitch_stats_tracked", "tags", "TEXT")
    _add_column_if_missing(conn, "twitch_stats_category", "is_partner", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "twitch_stats_category", "game_name", "TEXT")
    _add_column_if_missing(conn, "twitch_stats_category", "stream_title", "TEXT")
    _add_column_if_missing(conn, "twitch_stats_category", "tags", "TEXT")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_stats_tracked_streamer ON twitch_stats_tracked(streamer)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_stats_category_streamer ON twitch_stats_category(streamer)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_stats_category_ts ON twitch_stats_category(ts_utc)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_stats_tracked_ts ON twitch_stats_tracked(ts_utc)"
    )

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
            stream_title       TEXT,
            notification_text  TEXT,
            language           TEXT,
            is_mature          INTEGER DEFAULT 0,
            tags               TEXT,
            had_deadlock_in_session INTEGER DEFAULT 0,
            game_name          TEXT,
            notes              TEXT
        )
        """
    )
    _add_column_if_missing(conn, "twitch_stream_sessions", "stream_title", "TEXT")
    _add_column_if_missing(conn, "twitch_stream_sessions", "notification_text", "TEXT")
    _add_column_if_missing(conn, "twitch_stream_sessions", "language", "TEXT")
    _add_column_if_missing(conn, "twitch_stream_sessions", "is_mature", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "twitch_stream_sessions", "tags", "TEXT")
    _add_column_if_missing(conn, "twitch_stream_sessions", "had_deadlock_in_session", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "twitch_stream_sessions", "followers_start", "INTEGER")
    _add_column_if_missing(conn, "twitch_stream_sessions", "followers_end", "INTEGER")
    _add_column_if_missing(conn, "twitch_stream_sessions", "follower_delta", "INTEGER")
    _add_column_if_missing(conn, "twitch_stream_sessions", "game_name", "TEXT")

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
            chatter_id      TEXT,
            message_id      TEXT,
            message_ts      TEXT NOT NULL,
            is_command      INTEGER DEFAULT 0,
            content         TEXT
        )
        """
    )
    _add_column_if_missing(conn, "twitch_chat_messages", "chatter_id", "TEXT")
    _add_column_if_missing(conn, "twitch_chat_messages", "message_id", "TEXT")
    _add_column_if_missing(conn, "twitch_chat_messages", "content", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_chat_messages_session ON twitch_chat_messages(session_id, message_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_chat_messages_streamer_ts ON twitch_chat_messages(streamer_login, message_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_chat_messages_chatter ON twitch_chat_messages(streamer_login, chatter_login, message_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_chat_messages_message_id ON twitch_chat_messages(message_id)"
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

    # 7b) Raid-Blacklist (Channels, die keine Raids zulassen)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_raid_blacklist (
            target_id       TEXT,
            target_login    TEXT NOT NULL PRIMARY KEY,
            reason          TEXT,
            added_at        TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 8) Subscription Snapshots
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_subscriptions_snapshot (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            twitch_user_id    TEXT NOT NULL,
            twitch_login      TEXT,
            total             INTEGER DEFAULT 0,
            tier1             INTEGER DEFAULT 0,
            tier2             INTEGER DEFAULT 0,
            tier3             INTEGER DEFAULT 0,
            points            INTEGER DEFAULT 0,
            snapshot_at       TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_subs_user_ts ON twitch_subscriptions_snapshot(twitch_user_id, snapshot_at)"
    )

    # 8b) EventSub Capacity Snapshots
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_eventsub_capacity_snapshot (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc             TEXT DEFAULT CURRENT_TIMESTAMP,
            trigger_reason     TEXT,
            listener_count     INTEGER DEFAULT 0,
            ready_listeners    INTEGER DEFAULT 0,
            failed_listeners   INTEGER DEFAULT 0,
            used_slots         INTEGER DEFAULT 0,
            total_slots        INTEGER DEFAULT 0,
            headroom_slots     INTEGER DEFAULT 0,
            listeners_at_limit INTEGER DEFAULT 0,
            utilization_pct    REAL DEFAULT 0,
            listeners_json     TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_eventsub_capacity_ts ON twitch_eventsub_capacity_snapshot(ts_utc)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_eventsub_capacity_reason ON twitch_eventsub_capacity_snapshot(trigger_reason, ts_utc)"
    )

    # 8c) Ads Schedule Snapshots
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_ads_schedule_snapshot (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            twitch_user_id     TEXT NOT NULL,
            twitch_login       TEXT,
            next_ad_at         TEXT,
            last_ad_at         TEXT,
            duration           INTEGER,
            preroll_free_time  INTEGER,
            snooze_count       INTEGER,
            snooze_refresh_at  TEXT,
            snapshot_at        TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_ads_user_ts ON twitch_ads_schedule_snapshot(twitch_user_id, snapshot_at)"
    )

    # 9) Token Blacklist
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_token_blacklist (
            twitch_user_id TEXT PRIMARY KEY,
            twitch_login TEXT NOT NULL,
            error_message TEXT,
            error_count INTEGER DEFAULT 1,
            first_error_at TEXT NOT NULL,
            last_error_at TEXT NOT NULL,
            notified INTEGER DEFAULT 0
        )
        """
    )

    # 10) Discord Invite Codes Cache
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_invite_codes (
            guild_id      INTEGER NOT NULL,
            invite_code   TEXT NOT NULL,
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, invite_code)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discord_invites_guild ON discord_invite_codes(guild_id)"
    )

    # 11) Streamer-spezifische Discord-Invites (Promo-Tracking)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_streamer_invites (
            streamer_login TEXT PRIMARY KEY,
            guild_id       INTEGER NOT NULL,
            channel_id     INTEGER NOT NULL,
            invite_code    TEXT NOT NULL,
            invite_url     TEXT NOT NULL,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            last_sent_at   TEXT
        )
        """
    )
    _add_column_if_missing(conn, "twitch_streamer_invites", "last_sent_at", "TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_twitch_streamer_invites_code ON twitch_streamer_invites(invite_code)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_streamer_invites_guild ON twitch_streamer_invites(guild_id)"
    )

    # 12) Partner-Outreach Tracking (autonome Ansprache frequenter Streamer)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_partner_outreach (
            streamer_login   TEXT PRIMARY KEY,
            streamer_user_id TEXT,
            detected_at      TEXT NOT NULL,
            contacted_at     TEXT,
            status           TEXT DEFAULT 'pending',
            cooldown_until   TEXT,
            notes            TEXT
        )
        """
    )
