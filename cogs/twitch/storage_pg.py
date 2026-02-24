"""PostgreSQL/TimescaleDB storage layer for Twitch analytics (Windows Tresor friendly).

- DSN lookup order: env TWITCH_ANALYTICS_DSN, then Windows Credential Manager (service: DeadlockBot, key: TWITCH_ANALYTICS_DSN).
- Provides a sqlite-like interface: get_conn() yields a psycopg connection; execute() etc. available via conn.
- Supports sqlite-style '?' placeholders by translating to '%s'.
- Adds minimal compatibility functions (strftime, printf) inside the target DB so existing analytics SQL keeps running.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterable, Sequence

import psycopg

log = logging.getLogger("TwitchStreams.StoragePG")

KEYRING_SERVICE = "DeadlockBot"
ENV_DSN = "TWITCH_ANALYTICS_DSN"


class RowCompat:
    """Row that supports both numeric and name-based access."""

    __slots__ = ("_values", "_map")

    def __init__(self, names: Sequence[str], values: Sequence[object]):
        self._values = tuple(values)
        self._map = {name: val for name, val in zip(names, values, strict=False)}

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._map[key]

    def get(self, key, default=None):
        return self._map.get(key, default)

    def keys(self):
        return self._map.keys()

    def values(self):
        return self._map.values()

    def items(self):
        return self._map.items()

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"RowCompat({self._map})"


def _compat_row_factory(cursor: psycopg.Cursor) -> psycopg.rows.RowMaker[RowCompat]:
    """Row factory returning RowCompat with both index and name access."""
    names = [col.name for col in cursor.description] if cursor.description else []

    def _maker(values: Sequence[object]) -> RowCompat:
        return RowCompat(names, values)

    return _maker


def _load_dsn() -> str:
    dsn = os.environ.get(ENV_DSN)
    if dsn:
        return dsn
    try:
        import keyring  # type: ignore

        val = keyring.get_password(KEYRING_SERVICE, ENV_DSN) or keyring.get_password(
            f"{ENV_DSN}@{KEYRING_SERVICE}", ENV_DSN
        )
        if val:
            return val
    except Exception as exc:  # pragma: no cover - best-effort Tresor lookup
        log.debug("Keyring lookup failed: %s", exc)
    raise RuntimeError(f"{ENV_DSN} not set (env or Windows Credential Manager '{KEYRING_SERVICE}')")


def _placeholder_sql(sql: str) -> str:
    """Escape literal '%' and convert sqlite-style '?' to psycopg placeholders."""
    # Escape all percent signs first; psycopg treats '%%' as literal '%'.
    sql = sql.replace("%", "%%")
    # Restore the valid placeholder forms.
    sql = sql.replace("%%s", "%s").replace("%%b", "%b").replace("%%t", "%t")
    # Translate sqlite-style '?' placeholders to '%s'.
    return sql.replace("?", "%s")


class _CompatCursor:
    """Lightweight wrapper to apply placeholder translation on execute calls."""

    def __init__(self, cursor: psycopg.Cursor):
        self._cursor = cursor

    def execute(self, sql: str, params=None, *args, **kwargs):
        return self._cursor.execute(_placeholder_sql(sql), params or (), *args, **kwargs)

    def executemany(self, sql: str, params_seq, *args, **kwargs):
        return self._cursor.executemany(_placeholder_sql(sql), params_seq, *args, **kwargs)

    # Passthrough for fetch* and iteration
    def __getattr__(self, item):
        return getattr(self._cursor, item)

    def __iter__(self):
        return iter(self._cursor)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._cursor.__exit__(exc_type, exc, tb)


class _CompatConnection:
    """Wrapper exposing a psycopg connection with sqlite-style execute()."""

    def __init__(self, conn: psycopg.Connection):
        self._conn = conn

    # Basic helpers expected by callers
    def execute(self, sql: str, params=None, *args, **kwargs):
        return self._conn.execute(_placeholder_sql(sql), params or (), *args, **kwargs)

    def cursor(self, *args, **kwargs):
        return _CompatCursor(self._conn.cursor(*args, **kwargs))

    # Context manager support
    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)

    # Delegate everything else to the real connection
    def __getattr__(self, item):
        return getattr(self._conn, item)


def _ensure_compat_functions(conn: psycopg.Connection) -> None:
    """Install lightweight sqlite compatibility helpers (strftime, printf, julianday, datetime) once per process."""
    if getattr(_ensure_compat_functions, "_installed", False):
        return
    with conn.cursor() as _cur:
        cur = _CompatCursor(_cur)
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION strftime(fmt text, ts timestamptz)
            RETURNS text
            LANGUAGE plpgsql IMMUTABLE AS $$
            DECLARE p text := fmt;
            BEGIN
              IF fmt = '%w' THEN
                RETURN (EXTRACT(dow FROM ts))::int::text; -- 0=Sonntag wie SQLite
              END IF;
              p := replace(p, '%Y', 'YYYY');
              p := replace(p, '%m', 'MM');
              p := replace(p, '%d', 'DD');
              p := replace(p, '%H', 'HH24');
              p := replace(p, '%M', 'MI');
              RETURN to_char(ts, p);
            END;
            $$;
            """
        )
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION printf(fmt text, arg numeric)
            RETURNS text
            LANGUAGE plpgsql IMMUTABLE AS $$
            BEGIN
              IF fmt = '%02d' THEN
                RETURN lpad((arg::int)::text, 2, '0');
              END IF;
              RETURN format(fmt, arg);
            END;
            $$;
            """
        )
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION julianday(ts timestamptz)
            RETURNS double precision
            LANGUAGE plpgsql IMMUTABLE AS $$
            BEGIN
              RETURN EXTRACT(EPOCH FROM ts) / 86400.0 + 2440587.5;
            END;
            $$;
            """
        )
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION julianday(ts text)
            RETURNS double precision
            LANGUAGE sql IMMUTABLE AS $$
              SELECT julianday(ts::timestamptz);
            $$;
            """
        )
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION datetime(ts text, modifier text DEFAULT NULL)
            RETURNS timestamptz
            LANGUAGE plpgsql STABLE AS $$
            DECLARE
              base_ts timestamptz;
            BEGIN
              IF ts IS NULL THEN
                RETURN NULL;
              END IF;
              IF lower(ts) = 'now' THEN
                base_ts := NOW();
              ELSE
                base_ts := ts::timestamptz;
              END IF;
              IF modifier IS NOT NULL THEN
                base_ts := base_ts + modifier::interval;
              END IF;
              RETURN base_ts;
            END;
            $$;
            """
        )
    conn.commit()
    _ensure_compat_functions._installed = True


@contextlib.contextmanager
def get_conn():
    """Context manager returning a psycopg connection with dict rows and autocommit."""
    dsn = _load_dsn()
    conn = psycopg.connect(dsn, row_factory=_compat_row_factory, autocommit=True)
    try:
        _ensure_compat_functions(conn)
        yield _CompatConnection(conn)
    finally:
        conn.close()


def execute(sql: str, params: Iterable | None = None):
    with get_conn() as conn:
        return conn.execute(sql, params or [])


def query_one(sql: str, params: Iterable | None = None):
    with get_conn() as conn:
        return conn.execute(sql, params or []).fetchone()


def query_all(sql: str, params: Iterable | None = None):
    with get_conn() as conn:
        return conn.execute(sql, params or []).fetchall()


def backfill_tracked_stats_from_category(conn, login: str) -> int:
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


# ---------------------------------------------------------------------------
# Schema: all non-auth Twitch tables (auth tables stay in SQLite)
# ---------------------------------------------------------------------------

def _pg_add_col_if_missing(conn, table: str, column: str, col_type: str) -> None:
    conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}")


def _seed_default_templates_pg(conn) -> None:
    existing = conn.execute("SELECT COUNT(*) FROM clip_templates_global").fetchone()[0]
    if existing and int(existing) > 0:
        return
    templates = [
        ("Gaming Highlight", "Epic {{game}} moment by {{streamer}}! 🎮", '["gaming","twitch","{{game}}"]', "Gaming", "system"),
        ("Funny Moment", "😂 {{title}} | {{streamer}}", '["funny","gaming","twitch"]', "Entertainment", "system"),
        ("Pro Play", "Insane {{game}} play by {{streamer}} 🔥", '["esports","progaming","{{game}}"]', "Competitive", "system"),
        ("Clutch Moment", "CLUTCH! {{title}} 💪", '["clutch","gaming","{{game}}"]', "Gaming", "system"),
        ("Fails & Funnies", "This didn't go as planned 😅 | {{streamer}}", '["fail","funny","gaming"]', "Entertainment", "system"),
    ]
    conn.execute(
        """
        INSERT INTO clip_templates_global (template_name, description_template, hashtags, category, created_by)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (template_name) DO NOTHING
        """,
        templates[0],
    )
    for t in templates[1:]:
        conn.execute(
            """
            INSERT INTO clip_templates_global (template_name, description_template, hashtags, category, created_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (template_name) DO NOTHING
            """,
            t,
        )


def ensure_schema(conn) -> None:
    """Create/update all non-auth Twitch tables in PostgreSQL. Idempotent."""

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
            created_at                 TEXT DEFAULT CURRENT_TIMESTAMP,
            archived_at                TEXT,
            raid_bot_enabled           INTEGER DEFAULT 0,
            silent_ban                 INTEGER DEFAULT 0,
            silent_raid                INTEGER DEFAULT 0,
            is_monitored_only          INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_twitch_streamers_user_id ON twitch_streamers(twitch_user_id)"
    )

    # View: partner state (single source of truth)
    conn.execute(
        """
        CREATE OR REPLACE VIEW twitch_streamers_partner_state AS
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
                            AND s.manual_verified_until::timestamptz >= NOW()
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
            twitch_user_id              TEXT PRIMARY KEY,
            streamer_login              TEXT NOT NULL,
            last_stream_id              TEXT,
            last_started_at             TEXT,
            last_title                  TEXT,
            last_game_id                TEXT,
            last_discord_message_id     TEXT,
            last_notified_at            TEXT,
            is_live                     INTEGER DEFAULT 0,
            last_seen_at                TEXT,
            last_game                   TEXT,
            last_viewer_count           INTEGER DEFAULT 0,
            last_tracking_token         TEXT,
            active_session_id           INTEGER,
            had_deadlock_in_session     INTEGER DEFAULT 0,
            last_deadlock_seen_at       TEXT
        )
        """
    )

    # 3) Stats logs
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_stats_tracked (
            ts_utc       TEXT,
            streamer     TEXT,
            viewer_count INTEGER,
            is_partner   INTEGER DEFAULT 0,
            game_name    TEXT,
            stream_title TEXT,
            tags         TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_stats_category (
            ts_utc       TEXT,
            streamer     TEXT,
            viewer_count INTEGER,
            is_partner   INTEGER DEFAULT 0,
            game_name    TEXT,
            stream_title TEXT,
            tags         TEXT
        )
        """
    )
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

    # 4) Link click tracking
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_link_clicks (
            id               SERIAL PRIMARY KEY,
            clicked_at       TEXT DEFAULT CURRENT_TIMESTAMP,
            streamer_login   TEXT NOT NULL,
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

    # 5) Stream sessions & engagement
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_stream_sessions (
            id                      SERIAL PRIMARY KEY,
            streamer_login          TEXT NOT NULL,
            stream_id               TEXT,
            started_at              TEXT NOT NULL,
            ended_at                TEXT,
            duration_seconds        INTEGER DEFAULT 0,
            start_viewers           INTEGER DEFAULT 0,
            peak_viewers            INTEGER DEFAULT 0,
            end_viewers             INTEGER DEFAULT 0,
            avg_viewers             REAL    DEFAULT 0,
            samples                 INTEGER DEFAULT 0,
            retention_5m            REAL,
            retention_10m           REAL,
            retention_20m           REAL,
            dropoff_pct             REAL,
            dropoff_label           TEXT,
            unique_chatters         INTEGER DEFAULT 0,
            first_time_chatters     INTEGER DEFAULT 0,
            returning_chatters      INTEGER DEFAULT 0,
            followers_start         INTEGER,
            followers_end           INTEGER,
            follower_delta          INTEGER,
            stream_title            TEXT,
            notification_text       TEXT,
            language                TEXT,
            is_mature               INTEGER DEFAULT 0,
            tags                    TEXT,
            had_deadlock_in_session INTEGER DEFAULT 0,
            game_name               TEXT,
            notes                   TEXT
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
            session_id         INTEGER NOT NULL,
            ts_utc             TEXT    NOT NULL,
            minutes_from_start INTEGER,
            viewer_count       INTEGER NOT NULL,
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
            session_id               INTEGER NOT NULL,
            streamer_login           TEXT    NOT NULL,
            chatter_login            TEXT    NOT NULL,
            chatter_id               TEXT,
            first_message_at         TEXT    NOT NULL,
            messages                 INTEGER DEFAULT 0,
            is_first_time_global     INTEGER DEFAULT 0,
            seen_via_chatters_api    INTEGER DEFAULT 0,
            last_seen_at             TEXT,
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
            streamer_login  TEXT NOT NULL,
            chatter_login   TEXT NOT NULL,
            chatter_id      TEXT,
            first_seen_at   TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            total_messages  INTEGER DEFAULT 0,
            total_sessions  INTEGER DEFAULT 0,
            PRIMARY KEY (streamer_login, chatter_login)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_chat_messages (
            id             SERIAL PRIMARY KEY,
            session_id     INTEGER NOT NULL,
            streamer_login TEXT    NOT NULL,
            chatter_login  TEXT,
            chatter_id     TEXT,
            message_id     TEXT,
            message_ts     TEXT    NOT NULL,
            is_command     INTEGER DEFAULT 0,
            content        TEXT
        )
        """
    )
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

    # 6) Raid history & blacklist
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_raid_history (
            id                       SERIAL PRIMARY KEY,
            from_broadcaster_id      TEXT NOT NULL,
            from_broadcaster_login   TEXT NOT NULL,
            to_broadcaster_id        TEXT NOT NULL,
            to_broadcaster_login     TEXT NOT NULL,
            viewer_count             INTEGER DEFAULT 0,
            stream_duration_sec      INTEGER,
            reason                   TEXT,
            executed_at              TEXT DEFAULT CURRENT_TIMESTAMP,
            success                  INTEGER DEFAULT 1,
            error_message            TEXT,
            target_stream_started_at TEXT,
            candidates_count         INTEGER DEFAULT 0
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

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_raid_blacklist (
            target_id    TEXT,
            target_login TEXT NOT NULL PRIMARY KEY,
            reason       TEXT,
            added_at     TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 7) Token blacklist
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_token_blacklist (
            twitch_user_id   TEXT PRIMARY KEY,
            twitch_login     TEXT NOT NULL,
            error_message    TEXT,
            error_count      INTEGER DEFAULT 1,
            first_error_at   TEXT NOT NULL,
            last_error_at    TEXT NOT NULL,
            notified         INTEGER DEFAULT 0,
            grace_expires_at TEXT,
            user_dm_sent     INTEGER DEFAULT 0,
            reminder_sent    INTEGER DEFAULT 0,
            role_removed     INTEGER DEFAULT 0
        )
        """
    )

    # 8) Subscription / EventSub / Ads snapshots
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_subscriptions_snapshot (
            id             SERIAL PRIMARY KEY,
            twitch_user_id TEXT NOT NULL,
            twitch_login   TEXT,
            total          INTEGER DEFAULT 0,
            tier1          INTEGER DEFAULT 0,
            tier2          INTEGER DEFAULT 0,
            tier3          INTEGER DEFAULT 0,
            points         INTEGER DEFAULT 0,
            snapshot_at    TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_subs_user_ts ON twitch_subscriptions_snapshot(twitch_user_id, snapshot_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_eventsub_capacity_snapshot (
            id                 SERIAL PRIMARY KEY,
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

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_ads_schedule_snapshot (
            id                SERIAL PRIMARY KEY,
            twitch_user_id    TEXT NOT NULL,
            twitch_login      TEXT,
            next_ad_at        TEXT,
            last_ad_at        TEXT,
            duration          INTEGER,
            preroll_free_time INTEGER,
            snooze_count      INTEGER,
            snooze_refresh_at TEXT,
            snapshot_at       TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_ads_user_ts ON twitch_ads_schedule_snapshot(twitch_user_id, snapshot_at)"
    )

    # 9) Discord invite codes & streamer invites
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_invite_codes (
            guild_id     INTEGER NOT NULL,
            invite_code  TEXT    NOT NULL,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, invite_code)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_discord_invites_guild ON discord_invite_codes(guild_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_streamer_invites (
            streamer_login TEXT PRIMARY KEY,
            guild_id       INTEGER NOT NULL,
            channel_id     INTEGER NOT NULL,
            invite_code    TEXT    NOT NULL,
            invite_url     TEXT    NOT NULL,
            created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
            last_sent_at   TEXT
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_twitch_streamer_invites_code ON twitch_streamer_invites(invite_code)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_streamer_invites_guild ON twitch_streamer_invites(guild_id)"
    )

    # 10) Partner outreach
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

    # 11) Event tables
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_bits_events (
            id             SERIAL PRIMARY KEY,
            session_id     INTEGER,
            twitch_user_id TEXT    NOT NULL,
            donor_login    TEXT,
            amount         INTEGER NOT NULL,
            message        TEXT,
            received_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_bits_events_session ON twitch_bits_events(session_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_hype_train_events (
            id               SERIAL PRIMARY KEY,
            session_id       INTEGER,
            twitch_user_id   TEXT NOT NULL,
            started_at       TEXT,
            ended_at         TEXT,
            duration_seconds INTEGER,
            level            INTEGER,
            total_progress   INTEGER,
            event_phase      TEXT DEFAULT 'end'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_hype_train_events_session ON twitch_hype_train_events(session_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_subscription_events (
            id                SERIAL PRIMARY KEY,
            session_id        INTEGER,
            twitch_user_id    TEXT NOT NULL,
            event_type        TEXT NOT NULL,
            user_login        TEXT,
            tier              TEXT,
            is_gift           INTEGER DEFAULT 0,
            gifter_login      TEXT,
            cumulative_months INTEGER,
            streak_months     INTEGER,
            message           TEXT,
            total_gifted      INTEGER,
            received_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_subscription_events_session ON twitch_subscription_events(session_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_channel_updates (
            id             SERIAL PRIMARY KEY,
            twitch_user_id TEXT NOT NULL,
            title          TEXT,
            game_name      TEXT,
            language       TEXT,
            recorded_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_channel_updates_user ON twitch_channel_updates(twitch_user_id, recorded_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_ad_break_events (
            id               SERIAL PRIMARY KEY,
            session_id       INTEGER,
            twitch_user_id   TEXT NOT NULL,
            duration_seconds INTEGER,
            is_automatic     INTEGER DEFAULT 0,
            started_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_ad_break_events_session ON twitch_ad_break_events(session_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_ban_events (
            id              SERIAL PRIMARY KEY,
            session_id      INTEGER,
            twitch_user_id  TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            target_login    TEXT,
            target_id       TEXT,
            moderator_login TEXT,
            reason          TEXT,
            ends_at         TEXT,
            received_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_ban_events_user ON twitch_ban_events(twitch_user_id, received_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_shoutout_events (
            id                       SERIAL PRIMARY KEY,
            twitch_user_id           TEXT NOT NULL,
            direction                TEXT NOT NULL,
            other_broadcaster_id     TEXT,
            other_broadcaster_login  TEXT,
            moderator_login          TEXT,
            viewer_count             INTEGER DEFAULT 0,
            received_at              TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_shoutout_events_user ON twitch_shoutout_events(twitch_user_id, received_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_follow_events (
            id             SERIAL PRIMARY KEY,
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

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_channel_points_events (
            id             SERIAL PRIMARY KEY,
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

    # 12) Social media clips
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_clips_social_media (
            id                    SERIAL PRIMARY KEY,
            clip_id               TEXT   NOT NULL UNIQUE,
            clip_url              TEXT   NOT NULL,
            clip_title            TEXT,
            clip_thumbnail_url    TEXT,
            streamer_login        TEXT   NOT NULL,
            twitch_user_id        TEXT,
            created_at            TEXT   NOT NULL,
            duration_seconds      REAL,
            view_count            INTEGER DEFAULT 0,
            game_name             TEXT,
            status                TEXT DEFAULT 'pending',
            downloaded_at         TEXT,
            local_file_path       TEXT,
            converted_file_path   TEXT,
            uploaded_tiktok       INTEGER DEFAULT 0,
            uploaded_youtube      INTEGER DEFAULT 0,
            uploaded_instagram    INTEGER DEFAULT 0,
            tiktok_video_id       TEXT,
            youtube_video_id      TEXT,
            instagram_media_id    TEXT,
            tiktok_uploaded_at    TEXT,
            youtube_uploaded_at   TEXT,
            instagram_uploaded_at TEXT,
            custom_title          TEXT,
            custom_description    TEXT,
            hashtags              TEXT,
            music_track           TEXT,
            last_analytics_sync   TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_clips_social_media_streamer ON twitch_clips_social_media(streamer_login, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_clips_social_media_status ON twitch_clips_social_media(status)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_clips_social_analytics (
            id                SERIAL PRIMARY KEY,
            clip_id           INTEGER NOT NULL,
            platform          TEXT    NOT NULL,
            platform_video_id TEXT,
            views             INTEGER DEFAULT 0,
            likes             INTEGER DEFAULT 0,
            comments          INTEGER DEFAULT 0,
            shares            INTEGER DEFAULT 0,
            saves             INTEGER DEFAULT 0,
            watch_time_avg    REAL,
            completion_rate   REAL,
            ctr               REAL,
            engagement_rate   REAL,
            external_clicks   INTEGER DEFAULT 0,
            new_followers     INTEGER DEFAULT 0,
            synced_at         TEXT    NOT NULL,
            posted_at         TEXT,
            FOREIGN KEY (clip_id) REFERENCES twitch_clips_social_media(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_clips_social_analytics_clip ON twitch_clips_social_analytics(clip_id, synced_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_clips_social_analytics_platform ON twitch_clips_social_analytics(platform, posted_at)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS twitch_clips_upload_queue (
            id              SERIAL PRIMARY KEY,
            clip_id         INTEGER NOT NULL,
            platform        TEXT    NOT NULL,
            status          TEXT DEFAULT 'pending',
            priority        INTEGER DEFAULT 0,
            title           TEXT,
            description     TEXT,
            hashtags        TEXT,
            scheduled_at    TEXT,
            attempts        INTEGER DEFAULT 0,
            last_error      TEXT,
            last_attempt_at TEXT,
            created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at    TEXT,
            FOREIGN KEY (clip_id) REFERENCES twitch_clips_social_media(id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_twitch_clips_upload_queue_status ON twitch_clips_upload_queue(status, priority DESC)"
    )

    # 13) Templates & clip fetch history
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clip_templates_global (
            id                   SERIAL PRIMARY KEY,
            template_name        TEXT NOT NULL UNIQUE,
            description_template TEXT NOT NULL,
            hashtags             TEXT NOT NULL,
            category             TEXT,
            usage_count          INTEGER DEFAULT 0,
            created_at           TEXT DEFAULT CURRENT_TIMESTAMP,
            created_by           TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clip_templates_streamer (
            id                   SERIAL PRIMARY KEY,
            streamer_login       TEXT NOT NULL,
            template_name        TEXT NOT NULL,
            description_template TEXT NOT NULL,
            hashtags             TEXT NOT NULL,
            is_default           INTEGER DEFAULT 0,
            created_at           TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at           TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (streamer_login, template_name)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clip_templates_streamer_login ON clip_templates_streamer(streamer_login)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clip_last_hashtags (
            streamer_login TEXT PRIMARY KEY,
            hashtags       TEXT NOT NULL,
            last_used_at   TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clip_fetch_history (
            id               SERIAL PRIMARY KEY,
            streamer_login   TEXT NOT NULL,
            fetched_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            clips_found      INTEGER DEFAULT 0,
            clips_new        INTEGER DEFAULT 0,
            fetch_duration_ms INTEGER,
            error            TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clip_fetch_history_streamer ON clip_fetch_history(streamer_login, fetched_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clip_templates_global_category ON clip_templates_global(category)"
    )

    _seed_default_templates_pg(conn)
