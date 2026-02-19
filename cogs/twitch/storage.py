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
        ("is_monitored_only", "INTEGER DEFAULT 0"),  # 1 = read-only market research (no bot/mod actions)
        ("created_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
    ]:
        _add_column_if_missing(conn, "twitch_streamers", col, spec)

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_twitch_streamers_user_id ON twitch_streamers(twitch_user_id)"
    )

    # 1b) Zentrale Partner-Flags (Single Source of Truth)
    conn.execute(
        """
        CREATE VIEW IF NOT EXISTS twitch_streamers_partner_state AS
        SELECT
            base.*,
            CASE
                WHEN base.is_verified = 1
                     AND COALESCE(base.manual_partner_opt_out, 0) = 0
                     AND COALESCE(base.is_monitored_only, 0) = 0
                THEN 1 ELSE 0
            END AS is_partner,
            CASE
                WHEN base.is_verified = 1
                     AND COALESCE(base.manual_partner_opt_out, 0) = 0
                     AND COALESCE(base.is_monitored_only, 0) = 0
                     AND base.archived_at IS NULL
                THEN 1 ELSE 0
            END AS is_partner_active
        FROM (
            SELECT
                s.*,
                CASE
                    WHEN (
                        COALESCE(s.manual_verified_permanent, 0) = 1
                        OR (
                            s.manual_verified_until IS NOT NULL
                            AND julianday(s.manual_verified_until) >= julianday('now')
                        )
                        OR s.manual_verified_at IS NOT NULL
                    )
                    THEN 1 ELSE 0
                END AS is_verified
            FROM twitch_streamers s
        ) AS base
        """
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
    # Lurker-Tracking: Chatters die per API gefunden wurden aber nie geschrieben haben
    _add_column_if_missing(conn, "twitch_session_chatters", "seen_via_chatters_api", "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "twitch_session_chatters", "last_seen_at", "TEXT")

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
    _add_column_if_missing(conn, "twitch_raid_auth", "legacy_access_token",  "TEXT")
    _add_column_if_missing(conn, "twitch_raid_auth", "legacy_refresh_token", "TEXT")
    _add_column_if_missing(conn, "twitch_raid_auth", "legacy_scopes",        "TEXT")
    _add_column_if_missing(conn, "twitch_raid_auth", "legacy_saved_at",      "TEXT")
    _add_column_if_missing(conn, "twitch_raid_auth", "needs_reauth",         "INTEGER DEFAULT 0")
    _add_column_if_missing(conn, "twitch_raid_auth", "reauth_notified_at",   "TEXT")
    # Encrypted token storage (Phase 0: Encryption Foundation)
    _add_column_if_missing(conn, "twitch_raid_auth", "access_token_enc",     "BLOB")
    _add_column_if_missing(conn, "twitch_raid_auth", "refresh_token_enc",    "BLOB")
    _add_column_if_missing(conn, "twitch_raid_auth", "enc_version",          "INTEGER DEFAULT 1")
    _add_column_if_missing(conn, "twitch_raid_auth", "enc_kid",              "TEXT DEFAULT 'v1'")
    _add_column_if_missing(conn, "twitch_raid_auth", "enc_migrated_at",      "TEXT")

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

    # 7c) Social Media Platform OAuth (encrypted credentials)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS social_media_platform_auth (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,  -- 'tiktok', 'youtube', 'instagram'
            streamer_login TEXT,  -- NULL = bot-global

            -- OAuth Data (encrypted)
            access_token_enc BLOB NOT NULL,
            refresh_token_enc BLOB,
            client_id TEXT,  -- Public, not encrypted
            client_secret_enc BLOB,

            -- Metadata
            token_expires_at TEXT,
            scopes TEXT,
            platform_user_id TEXT,
            platform_username TEXT,

            -- Encryption metadata
            enc_version INTEGER DEFAULT 1,
            enc_kid TEXT DEFAULT 'v1',

            -- Timestamps
            authorized_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_refreshed_at TEXT,
            enabled INTEGER DEFAULT 1,

            UNIQUE(platform, streamer_login)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_social_platform_auth
        ON social_media_platform_auth(platform, streamer_login, enabled)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_social_platform_auth_expires
        ON social_media_platform_auth(token_expires_at) WHERE enabled = 1
        """
    )

    # 7d) OAuth State Tokens (CSRF protection)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_state_tokens (
            state_token TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            streamer_login TEXT,
            redirect_uri TEXT,
            pkce_verifier TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_oauth_state_expires
        ON oauth_state_tokens(expires_at)
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_bits_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER,
            twitch_user_id  TEXT NOT NULL,
            donor_login     TEXT,
            amount          INTEGER NOT NULL,
            message         TEXT,
            received_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_twitch_bits_events_session
        ON twitch_bits_events (session_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_hype_train_events (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id       INTEGER,
            twitch_user_id   TEXT NOT NULL,
            started_at       TEXT,
            ended_at         TEXT,
            duration_seconds INTEGER,
            level            INTEGER,
            total_progress   INTEGER
        )
        """
    )
    _add_column_if_missing(conn, "twitch_hype_train_events", "event_phase", "TEXT DEFAULT 'end'")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_twitch_hype_train_events_session
        ON twitch_hype_train_events (session_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_subscription_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER,
            twitch_user_id  TEXT NOT NULL,
            event_type      TEXT NOT NULL,  -- 'subscribe', 'gift', 'resub'
            user_login      TEXT,           -- Subscriber oder Gifter
            tier            TEXT,           -- '1000', '2000', '3000'
            is_gift         INTEGER DEFAULT 0,
            gifter_login    TEXT,
            cumulative_months INTEGER,
            streak_months   INTEGER,
            message         TEXT,
            total_gifted    INTEGER,        -- Nur bei gift: Gesamtanzahl
            received_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_twitch_subscription_events_session
        ON twitch_subscription_events (session_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_channel_updates (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            twitch_user_id  TEXT NOT NULL,
            title           TEXT,
            game_name       TEXT,
            language        TEXT,
            recorded_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_twitch_channel_updates_user
        ON twitch_channel_updates (twitch_user_id, recorded_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_ad_break_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER,
            twitch_user_id  TEXT NOT NULL,
            duration_seconds INTEGER,
            is_automatic    INTEGER DEFAULT 0,
            started_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_twitch_ad_break_events_session
        ON twitch_ad_break_events (session_id)
        """
    )

    # --- Ban / Unban Events (moderator:manage:banned_users) ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_ban_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER,
            twitch_user_id  TEXT NOT NULL,
            event_type      TEXT NOT NULL,   -- 'ban' | 'unban'
            target_login    TEXT,
            target_id       TEXT,
            moderator_login TEXT,
            reason          TEXT,
            ends_at         TEXT,            -- NULL = permanent ban, sonst timeout-Ablauf
            received_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_ban_events_user ON twitch_ban_events(twitch_user_id, received_at)"
    )

    # --- Shoutout Events (moderator:manage:shoutouts) ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_shoutout_events (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            twitch_user_id      TEXT NOT NULL,
            direction           TEXT NOT NULL,  -- 'sent' | 'received'
            other_broadcaster_id   TEXT,
            other_broadcaster_login TEXT,
            moderator_login     TEXT,           -- Wer den Shoutout ausgelöst hat (bei sent)
            viewer_count        INTEGER DEFAULT 0,
            received_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_shoutout_events_user ON twitch_shoutout_events(twitch_user_id, received_at)"
    )

    # --- Follow Events (channel.follow EventSub) ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_follow_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            streamer_login TEXT NOT NULL,
            twitch_user_id TEXT NOT NULL,
            follower_login TEXT NOT NULL,
            follower_id    TEXT,
            followed_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_follow_events_streamer ON twitch_follow_events(streamer_login, followed_at)"
    )

    # --- Channel Point Redemption Events ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_channel_points_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     INTEGER,
            twitch_user_id TEXT NOT NULL,
            user_login     TEXT,
            reward_id      TEXT,
            reward_title   TEXT,
            reward_cost    INTEGER,
            user_input     TEXT,
            redeemed_at    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_channel_points_events_user ON twitch_channel_points_events(twitch_user_id, redeemed_at)"
    )

    # --- Social Media Clip Publisher ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_clips_social_media (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id             TEXT NOT NULL UNIQUE,
            clip_url            TEXT NOT NULL,
            clip_title          TEXT,
            clip_thumbnail_url  TEXT,
            streamer_login      TEXT NOT NULL,
            twitch_user_id      TEXT,
            created_at          TEXT NOT NULL,
            duration_seconds    REAL,
            view_count          INTEGER DEFAULT 0,
            game_name           TEXT,

            -- Processing State
            status              TEXT DEFAULT 'pending',  -- pending, processing, ready, failed
            downloaded_at       TEXT,
            local_file_path     TEXT,
            converted_file_path TEXT,  -- 9:16 version für TikTok/Reels

            -- Upload State
            uploaded_tiktok     INTEGER DEFAULT 0,
            uploaded_youtube    INTEGER DEFAULT 0,
            uploaded_instagram  INTEGER DEFAULT 0,

            -- External IDs
            tiktok_video_id     TEXT,
            youtube_video_id    TEXT,
            instagram_media_id  TEXT,

            -- Upload Timestamps
            tiktok_uploaded_at  TEXT,
            youtube_uploaded_at TEXT,
            instagram_uploaded_at TEXT,

            -- Custom Settings
            custom_title        TEXT,
            custom_description  TEXT,
            hashtags            TEXT,  -- JSON Array
            music_track         TEXT,

            -- Analytics
            last_analytics_sync TEXT,

            FOREIGN KEY(streamer_login) REFERENCES twitch_streamers(twitch_login) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_clips_social_media_streamer ON twitch_clips_social_media(streamer_login, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_clips_social_media_status ON twitch_clips_social_media(status)"
    )

    # --- Social Media Analytics ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_clips_social_analytics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id         INTEGER NOT NULL,
            platform        TEXT NOT NULL,  -- tiktok, youtube, instagram
            platform_video_id TEXT,

            -- Metrics
            views           INTEGER DEFAULT 0,
            likes           INTEGER DEFAULT 0,
            comments        INTEGER DEFAULT 0,
            shares          INTEGER DEFAULT 0,
            saves           INTEGER DEFAULT 0,  -- Instagram/TikTok
            watch_time_avg  REAL,  -- Average watch time percentage
            completion_rate REAL,  -- % who watched to end

            -- Engagement
            ctr             REAL,  -- Click-through rate
            engagement_rate REAL,  -- (likes+comments+shares)/views

            -- Traffic
            external_clicks INTEGER DEFAULT 0,  -- Clicks to Twitch profile
            new_followers   INTEGER DEFAULT 0,  -- Attributed to this clip

            -- Timestamps
            synced_at       TEXT NOT NULL,
            posted_at       TEXT,

            FOREIGN KEY(clip_id) REFERENCES twitch_clips_social_media(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_clips_social_analytics_clip ON twitch_clips_social_analytics(clip_id, synced_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_clips_social_analytics_platform ON twitch_clips_social_analytics(platform, posted_at)"
    )

    # --- Social Media Upload Queue ---
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_clips_upload_queue (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            clip_id     INTEGER NOT NULL,
            platform    TEXT NOT NULL,  -- tiktok, youtube, instagram
            status      TEXT DEFAULT 'pending',  -- pending, processing, completed, failed
            priority    INTEGER DEFAULT 0,  -- Higher = Upload first

            -- Settings für diesen Upload
            title       TEXT,
            description TEXT,
            hashtags    TEXT,  -- JSON Array
            scheduled_at TEXT,  -- Optional: Scheduled post time

            -- Error Handling
            attempts    INTEGER DEFAULT 0,
            last_error  TEXT,
            last_attempt_at TEXT,

            -- Timestamps
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,

            FOREIGN KEY(clip_id) REFERENCES twitch_clips_social_media(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_clips_upload_queue_status ON twitch_clips_upload_queue(status, priority DESC)"
    )

    # ========== Social Media Templates & Fetch History ==========

    # Global Template Library (Recommended Templates)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clip_templates_global (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_name TEXT NOT NULL UNIQUE,
            description_template TEXT NOT NULL,
            hashtags TEXT NOT NULL,
            category TEXT,
            usage_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT
        )
        """
    )

    # Per-Streamer Custom Templates
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clip_templates_streamer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            streamer_login TEXT NOT NULL,
            template_name TEXT NOT NULL,
            description_template TEXT NOT NULL,
            hashtags TEXT NOT NULL,
            is_default INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(streamer_login, template_name),
            FOREIGN KEY(streamer_login) REFERENCES twitch_streamers(twitch_login) ON DELETE CASCADE
        )
        """
    )

    # Last-Used Hashtags Cache
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clip_last_hashtags (
            streamer_login TEXT PRIMARY KEY,
            hashtags TEXT NOT NULL,
            last_used_at TEXT NOT NULL,
            FOREIGN KEY(streamer_login) REFERENCES twitch_streamers(twitch_login) ON DELETE CASCADE
        )
        """
    )

    # Clip Fetch History
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clip_fetch_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            streamer_login TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            clips_found INTEGER DEFAULT 0,
            clips_new INTEGER DEFAULT 0,
            fetch_duration_ms INTEGER,
            error TEXT,
            FOREIGN KEY(streamer_login) REFERENCES twitch_streamers(twitch_login) ON DELETE CASCADE
        )
        """
    )

    # Indexes for templates and fetch history
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clip_templates_streamer_login ON clip_templates_streamer(streamer_login)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clip_fetch_history_streamer ON clip_fetch_history(streamer_login, fetched_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clip_templates_global_category ON clip_templates_global(category)"
    )

    # Seed default global templates
    _seed_default_templates(conn)


def _seed_default_templates(conn: sqlite3.Connection) -> None:
    """Seed default global templates if they don't exist."""
    # Check if templates already exist
    existing = conn.execute("SELECT COUNT(*) FROM clip_templates_global").fetchone()[0]
    if existing > 0:
        return  # Already seeded

    templates = [
        (
            "Gaming Highlight",
            "Epic {{game}} moment by {{streamer}}! 🎮",
            '["gaming","twitch","{{game}}"]',
            "Gaming",
            "system"
        ),
        (
            "Funny Moment",
            "😂 {{title}} | {{streamer}}",
            '["funny","gaming","twitch"]',
            "Entertainment",
            "system"
        ),
        (
            "Pro Play",
            "Insane {{game}} play by {{streamer}} 🔥",
            '["esports","progaming","{{game}}"]',
            "Competitive",
            "system"
        ),
        (
            "Clutch Moment",
            "CLUTCH! {{title}} 💪",
            '["clutch","gaming","{{game}}"]',
            "Gaming",
            "system"
        ),
        (
            "Fails & Funnies",
            "This didn't go as planned 😅 | {{streamer}}",
            '["fail","funny","gaming"]',
            "Entertainment",
            "system"
        ),
    ]

    conn.executemany(
        """
        INSERT INTO clip_templates_global
        (template_name, description_template, hashtags, category, created_by)
        VALUES (?, ?, ?, ?, ?)
        """,
        templates
    )

    log.info("Seeded %d default global templates", len(templates))


def backfill_tracked_stats_from_category(conn: sqlite3.Connection, login: str) -> int:
    """Copy historic category stats into tracked stats for one streamer (idempotent)."""
    normalized = (login or "").strip().lower()
    if not normalized:
        return 0

    cur = conn.execute(
        """
        INSERT INTO twitch_stats_tracked
            (ts_utc, streamer, viewer_count, is_partner, game_name, stream_title, tags)
        SELECT c.ts_utc, c.streamer, c.viewer_count, c.is_partner,
               c.game_name, c.stream_title, c.tags
          FROM twitch_stats_category c
         WHERE LOWER(c.streamer) = ?
           AND NOT EXISTS (
               SELECT 1
                 FROM twitch_stats_tracked t
                WHERE LOWER(t.streamer) = LOWER(c.streamer)
                  AND t.ts_utc = c.ts_utc
           )
        """,
        (normalized,),
    )
    return int(cur.rowcount or 0)


def delete_streamer(conn: sqlite3.Connection, login: str) -> int:
    """Delete a streamer and all dependent records in correct FK order.

    Handles the cascade that SQLite's ON DELETE CASCADE would provide on new
    installs, but which existing production tables may lack (schema was updated
    to add CASCADE but existing tables cannot be altered in SQLite without
    recreation).

    Returns the number of streamer rows deleted (0 or 1).
    """
    # Grandchild tables (reference twitch_clips_social_media.id)
    conn.execute(
        """DELETE FROM twitch_clips_social_analytics
           WHERE clip_id IN (
               SELECT id FROM twitch_clips_social_media WHERE streamer_login = ?
           )""",
        (login,),
    )
    conn.execute(
        """DELETE FROM twitch_clips_upload_queue
           WHERE clip_id IN (
               SELECT id FROM twitch_clips_social_media WHERE streamer_login = ?
           )""",
        (login,),
    )
    # Child tables (reference twitch_streamers.twitch_login)
    conn.execute("DELETE FROM twitch_clips_social_media WHERE streamer_login = ?", (login,))
    conn.execute("DELETE FROM clip_templates_streamer WHERE streamer_login = ?", (login,))
    conn.execute("DELETE FROM clip_last_hashtags WHERE streamer_login = ?", (login,))
    conn.execute("DELETE FROM clip_fetch_history WHERE streamer_login = ?", (login,))
    # The streamer itself
    cur = conn.execute("DELETE FROM twitch_streamers WHERE twitch_login = ?", (login,))
    return cur.rowcount or 0

