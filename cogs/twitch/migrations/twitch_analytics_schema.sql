-- Twitch Analytics – Postgres/TimescaleDB Schema (lossless, no retention)
-- Run: psql "$TWITCH_ANALYTICS_DSN" -f cogs/twitch/migrations/twitch_analytics_schema.sql
-- Assumptions: Postgres 16+, extension timescaledb installed.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ========= Core Dimension =========
CREATE TABLE IF NOT EXISTS streamer_dim (
    twitch_login           TEXT PRIMARY KEY,
    twitch_user_id         TEXT,
    discord_user_id        TEXT,
    discord_display_name   TEXT,
    is_partner             BOOLEAN DEFAULT FALSE,
    is_monitored_only      BOOLEAN DEFAULT FALSE,
    archived_at            TIMESTAMPTZ,
    updated_at             TIMESTAMPTZ DEFAULT NOW()
);

-- ========= Sessions & Chat =========
CREATE TABLE IF NOT EXISTS twitch_stream_sessions (
    id                  BIGSERIAL PRIMARY KEY,
    streamer_login      TEXT NOT NULL,
    stream_id           TEXT,
    started_at          TIMESTAMPTZ NOT NULL,
    ended_at            TIMESTAMPTZ,
    duration_seconds    INTEGER DEFAULT 0,
    start_viewers       INTEGER DEFAULT 0,
    peak_viewers        INTEGER DEFAULT 0,
    end_viewers         INTEGER DEFAULT 0,
    avg_viewers         DOUBLE PRECISION DEFAULT 0,
    samples             INTEGER DEFAULT 0,
    retention_5m        DOUBLE PRECISION,
    retention_10m       DOUBLE PRECISION,
    retention_20m       DOUBLE PRECISION,
    dropoff_pct         DOUBLE PRECISION,
    dropoff_label       TEXT,
    unique_chatters     INTEGER DEFAULT 0,
    first_time_chatters INTEGER DEFAULT 0,
    returning_chatters  INTEGER DEFAULT 0,
    followers_start     INTEGER,
    followers_end       INTEGER,
    follower_delta      INTEGER,
    stream_title        TEXT,
    notification_text   TEXT,
    language            TEXT,
    is_mature           BOOLEAN DEFAULT FALSE,
    tags                TEXT,
    had_deadlock_in_session BOOLEAN DEFAULT FALSE,
    game_name           TEXT,
    notes               TEXT
);
SELECT create_hypertable('twitch_stream_sessions', 'started_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
SELECT add_compression_policy('twitch_stream_sessions', INTERVAL '7 days', if_not_exists => TRUE);
ALTER TABLE twitch_stream_sessions SET (timescaledb.compress, timescaledb.compress_segmentby = 'streamer_login', timescaledb.compress_orderby = 'started_at DESC');
CREATE INDEX IF NOT EXISTS idx_twitch_sessions_login ON twitch_stream_sessions(streamer_login, started_at);
CREATE INDEX IF NOT EXISTS idx_twitch_sessions_open ON twitch_stream_sessions(streamer_login) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS twitch_session_viewers (
    session_id          BIGINT NOT NULL REFERENCES twitch_stream_sessions(id) ON DELETE CASCADE,
    ts_utc              TIMESTAMPTZ NOT NULL,
    minutes_from_start  INTEGER,
    viewer_count        INTEGER NOT NULL,
    PRIMARY KEY(session_id, ts_utc)
);
SELECT create_hypertable('twitch_session_viewers', 'ts_utc', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '1 day');
ALTER TABLE twitch_session_viewers SET (timescaledb.compress, timescaledb.compress_segmentby = 'session_id', timescaledb.compress_orderby = 'ts_utc DESC');
SELECT add_compression_policy('twitch_session_viewers', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS twitch_session_chatters (
    session_id            BIGINT NOT NULL REFERENCES twitch_stream_sessions(id) ON DELETE CASCADE,
    streamer_login        TEXT NOT NULL,
    chatter_login         TEXT NOT NULL,
    chatter_id            TEXT,
    first_message_at      TIMESTAMPTZ NOT NULL,
    messages              INTEGER DEFAULT 0,
    is_first_time_global  BOOLEAN DEFAULT FALSE,
    seen_via_chatters_api BOOLEAN DEFAULT FALSE,
    last_seen_at          TIMESTAMPTZ,
    PRIMARY KEY (session_id, chatter_login)
);
SELECT create_hypertable('twitch_session_chatters', 'first_message_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_session_chatters SET (timescaledb.compress, timescaledb.compress_segmentby = 'session_id,streamer_login', timescaledb.compress_orderby = 'first_message_at DESC');
SELECT add_compression_policy('twitch_session_chatters', INTERVAL '7 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_twitch_session_chatters_login ON twitch_session_chatters(streamer_login, session_id);

CREATE TABLE IF NOT EXISTS twitch_chat_messages (
    id               BIGSERIAL PRIMARY KEY,
    session_id       BIGINT NOT NULL REFERENCES twitch_stream_sessions(id) ON DELETE CASCADE,
    streamer_login   TEXT NOT NULL,
    chatter_login    TEXT,
    chatter_id       TEXT,
    message_id       TEXT,
    message_ts       TIMESTAMPTZ NOT NULL,
    is_command       BOOLEAN DEFAULT FALSE,
    content          TEXT
);
SELECT create_hypertable('twitch_chat_messages', 'message_ts', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '1 day');
ALTER TABLE twitch_chat_messages SET (timescaledb.compress, timescaledb.compress_segmentby = 'streamer_login,session_id', timescaledb.compress_orderby = 'message_ts DESC');
SELECT add_compression_policy('twitch_chat_messages', INTERVAL '7 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_twitch_chat_messages_session ON twitch_chat_messages(session_id, message_ts);
CREATE INDEX IF NOT EXISTS idx_twitch_chat_messages_streamer_ts ON twitch_chat_messages(streamer_login, message_ts);
CREATE INDEX IF NOT EXISTS idx_twitch_chat_messages_message_id ON twitch_chat_messages(message_id);

-- ========= Periodic Stats =========
CREATE TABLE IF NOT EXISTS twitch_stats_tracked (
    ts_utc       TIMESTAMPTZ NOT NULL,
    streamer     TEXT NOT NULL,
    viewer_count INTEGER,
    is_partner   BOOLEAN DEFAULT FALSE,
    game_name    TEXT,
    stream_title TEXT,
    tags         TEXT
);
SELECT create_hypertable('twitch_stats_tracked', 'ts_utc', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_stats_tracked SET (timescaledb.compress, timescaledb.compress_segmentby = 'streamer', timescaledb.compress_orderby = 'ts_utc DESC');
SELECT add_compression_policy('twitch_stats_tracked', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS twitch_stats_category (
    ts_utc       TIMESTAMPTZ NOT NULL,
    streamer     TEXT NOT NULL,
    viewer_count INTEGER,
    is_partner   BOOLEAN DEFAULT FALSE,
    game_name    TEXT,
    stream_title TEXT,
    tags         TEXT
);
SELECT create_hypertable('twitch_stats_category', 'ts_utc', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_stats_category SET (timescaledb.compress, timescaledb.compress_segmentby = 'streamer', timescaledb.compress_orderby = 'ts_utc DESC');
SELECT add_compression_policy('twitch_stats_category', INTERVAL '7 days', if_not_exists => TRUE);

-- ========= Events =========
CREATE TABLE IF NOT EXISTS twitch_follow_events (
    id             BIGSERIAL PRIMARY KEY,
    streamer_login TEXT NOT NULL,
    twitch_user_id TEXT NOT NULL,
    follower_login TEXT NOT NULL,
    follower_id    TEXT,
    followed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SELECT create_hypertable('twitch_follow_events', 'followed_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_follow_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'streamer_login', timescaledb.compress_orderby = 'followed_at DESC');
SELECT add_compression_policy('twitch_follow_events', INTERVAL '7 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_twitch_follow_events_streamer ON twitch_follow_events(streamer_login, followed_at);

CREATE TABLE IF NOT EXISTS twitch_subscription_events (
    id                 BIGSERIAL PRIMARY KEY,
    session_id         BIGINT,
    twitch_user_id     TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    user_login         TEXT,
    tier               TEXT,
    is_gift            BOOLEAN DEFAULT FALSE,
    gifter_login       TEXT,
    cumulative_months  INTEGER,
    streak_months      INTEGER,
    message            TEXT,
    total_gifted       INTEGER,
    received_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SELECT create_hypertable('twitch_subscription_events', 'received_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_subscription_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'twitch_user_id', timescaledb.compress_orderby = 'received_at DESC');
SELECT add_compression_policy('twitch_subscription_events', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS twitch_channel_points_events (
    id              BIGSERIAL PRIMARY KEY,
    session_id      BIGINT,
    twitch_user_id  TEXT NOT NULL,
    user_login      TEXT,
    reward_id       TEXT,
    reward_title    TEXT,
    reward_cost     INTEGER,
    user_input      TEXT,
    redeemed_at     TIMESTAMPTZ NOT NULL
);
SELECT create_hypertable('twitch_channel_points_events', 'redeemed_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_channel_points_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'twitch_user_id', timescaledb.compress_orderby = 'redeemed_at DESC');
SELECT add_compression_policy('twitch_channel_points_events', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS twitch_bits_events (
    id              BIGSERIAL PRIMARY KEY,
    session_id      BIGINT,
    twitch_user_id  TEXT NOT NULL,
    donor_login     TEXT,
    amount          INTEGER NOT NULL,
    message         TEXT,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SELECT create_hypertable('twitch_bits_events', 'received_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_bits_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'twitch_user_id', timescaledb.compress_orderby = 'received_at DESC');
SELECT add_compression_policy('twitch_bits_events', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS twitch_hype_train_events (
    id               BIGSERIAL PRIMARY KEY,
    session_id       BIGINT,
    twitch_user_id   TEXT NOT NULL,
    started_at       TIMESTAMPTZ,
    ended_at         TIMESTAMPTZ,
    duration_seconds INTEGER,
    level            INTEGER,
    total_progress   INTEGER,
    event_phase      TEXT DEFAULT 'end'
);
SELECT create_hypertable('twitch_hype_train_events', 'started_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_hype_train_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'twitch_user_id', timescaledb.compress_orderby = 'started_at DESC');
SELECT add_compression_policy('twitch_hype_train_events', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS twitch_ad_break_events (
    id               BIGSERIAL PRIMARY KEY,
    session_id       BIGINT,
    twitch_user_id   TEXT NOT NULL,
    duration_seconds INTEGER,
    is_automatic     BOOLEAN DEFAULT FALSE,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SELECT create_hypertable('twitch_ad_break_events', 'started_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_ad_break_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'twitch_user_id', timescaledb.compress_orderby = 'started_at DESC');
SELECT add_compression_policy('twitch_ad_break_events', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS twitch_ban_events (
    id               BIGSERIAL PRIMARY KEY,
    session_id       BIGINT,
    twitch_user_id   TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    target_login     TEXT,
    target_id        TEXT,
    moderator_login  TEXT,
    reason           TEXT,
    ends_at          TIMESTAMPTZ,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SELECT create_hypertable('twitch_ban_events', 'received_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_ban_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'twitch_user_id', timescaledb.compress_orderby = 'received_at DESC');
SELECT add_compression_policy('twitch_ban_events', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS twitch_shoutout_events (
    id                      BIGSERIAL PRIMARY KEY,
    twitch_user_id          TEXT NOT NULL,
    direction               TEXT NOT NULL,
    other_broadcaster_id    TEXT,
    other_broadcaster_login TEXT,
    moderator_login         TEXT,
    viewer_count            INTEGER DEFAULT 0,
    received_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SELECT create_hypertable('twitch_shoutout_events', 'received_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_shoutout_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'twitch_user_id', timescaledb.compress_orderby = 'received_at DESC');
SELECT add_compression_policy('twitch_shoutout_events', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS twitch_channel_updates (
    id              BIGSERIAL PRIMARY KEY,
    twitch_user_id  TEXT NOT NULL,
    title           TEXT,
    game_name       TEXT,
    language        TEXT,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SELECT create_hypertable('twitch_channel_updates', 'recorded_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_channel_updates SET (timescaledb.compress, timescaledb.compress_segmentby = 'twitch_user_id', timescaledb.compress_orderby = 'recorded_at DESC');
SELECT add_compression_policy('twitch_channel_updates', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS twitch_raid_history (
    id                       BIGSERIAL PRIMARY KEY,
    from_broadcaster_id      TEXT NOT NULL,
    from_broadcaster_login   TEXT NOT NULL,
    to_broadcaster_id        TEXT NOT NULL,
    to_broadcaster_login     TEXT NOT NULL,
    viewer_count             INTEGER DEFAULT 0,
    stream_duration_sec      INTEGER,
    reason                   TEXT,
    executed_at              TIMESTAMPTZ DEFAULT NOW(),
    success                  BOOLEAN DEFAULT TRUE,
    error_message            TEXT,
    target_stream_started_at TIMESTAMPTZ,
    candidates_count         INTEGER DEFAULT 0
);
SELECT create_hypertable('twitch_raid_history', 'executed_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_raid_history SET (timescaledb.compress, timescaledb.compress_segmentby = 'from_broadcaster_login', timescaledb.compress_orderby = 'executed_at DESC');
SELECT add_compression_policy('twitch_raid_history', INTERVAL '7 days', if_not_exists => TRUE);

-- ========= Snapshots =========
CREATE TABLE IF NOT EXISTS twitch_subscriptions_snapshot (
    id             BIGSERIAL PRIMARY KEY,
    twitch_user_id TEXT NOT NULL,
    twitch_login   TEXT,
    total          INTEGER DEFAULT 0,
    tier1          INTEGER DEFAULT 0,
    tier2          INTEGER DEFAULT 0,
    tier3          INTEGER DEFAULT 0,
    points         INTEGER DEFAULT 0,
    snapshot_at    TIMESTAMPTZ DEFAULT NOW()
);
SELECT create_hypertable('twitch_subscriptions_snapshot', 'snapshot_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_subscriptions_snapshot SET (timescaledb.compress, timescaledb.compress_segmentby = 'twitch_user_id', timescaledb.compress_orderby = 'snapshot_at DESC');
SELECT add_compression_policy('twitch_subscriptions_snapshot', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS twitch_eventsub_capacity_snapshot (
    id                  BIGSERIAL PRIMARY KEY,
    ts_utc              TIMESTAMPTZ DEFAULT NOW(),
    trigger_reason      TEXT,
    listener_count      INTEGER DEFAULT 0,
    ready_listeners     INTEGER DEFAULT 0,
    failed_listeners    INTEGER DEFAULT 0,
    used_slots          INTEGER DEFAULT 0,
    total_slots         INTEGER DEFAULT 0,
    headroom_slots      INTEGER DEFAULT 0,
    listeners_at_limit  INTEGER DEFAULT 0,
    utilization_pct     DOUBLE PRECISION DEFAULT 0,
    listeners_json      TEXT
);
SELECT create_hypertable('twitch_eventsub_capacity_snapshot', 'ts_utc', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_eventsub_capacity_snapshot SET (timescaledb.compress, timescaledb.compress_segmentby = 'trigger_reason', timescaledb.compress_orderby = 'ts_utc DESC');
SELECT add_compression_policy('twitch_eventsub_capacity_snapshot', INTERVAL '7 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS twitch_ads_schedule_snapshot (
    id                BIGSERIAL PRIMARY KEY,
    twitch_user_id    TEXT NOT NULL,
    twitch_login      TEXT,
    next_ad_at        TIMESTAMPTZ,
    last_ad_at        TIMESTAMPTZ,
    duration          INTEGER,
    preroll_free_time INTEGER,
    snooze_count      INTEGER,
    snooze_refresh_at TIMESTAMPTZ,
    snapshot_at       TIMESTAMPTZ DEFAULT NOW()
);
SELECT create_hypertable('twitch_ads_schedule_snapshot', 'snapshot_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_ads_schedule_snapshot SET (timescaledb.compress, timescaledb.compress_segmentby = 'twitch_user_id', timescaledb.compress_orderby = 'snapshot_at DESC');
SELECT add_compression_policy('twitch_ads_schedule_snapshot', INTERVAL '7 days', if_not_exists => TRUE);

-- ========= Social Media / Clips =========
CREATE TABLE IF NOT EXISTS twitch_clips_social_media (
    id                  BIGSERIAL PRIMARY KEY,
    clip_id             TEXT NOT NULL UNIQUE,
    clip_url            TEXT NOT NULL,
    clip_title          TEXT,
    clip_thumbnail_url  TEXT,
    streamer_login      TEXT NOT NULL,
    twitch_user_id      TEXT,
    created_at          TIMESTAMPTZ NOT NULL,
    duration_seconds    DOUBLE PRECISION,
    view_count          INTEGER DEFAULT 0,
    game_name           TEXT,
    status              TEXT DEFAULT 'pending',
    downloaded_at       TIMESTAMPTZ,
    local_file_path     TEXT,
    converted_file_path TEXT,
    uploaded_tiktok     BOOLEAN DEFAULT FALSE,
    uploaded_youtube    BOOLEAN DEFAULT FALSE,
    uploaded_instagram  BOOLEAN DEFAULT FALSE,
    tiktok_video_id     TEXT,
    youtube_video_id    TEXT,
    instagram_media_id  TEXT,
    tiktok_uploaded_at  TIMESTAMPTZ,
    youtube_uploaded_at TIMESTAMPTZ,
    instagram_uploaded_at TIMESTAMPTZ,
    custom_title        TEXT,
    custom_description  TEXT,
    hashtags            TEXT,
    music_track         TEXT,
    last_analytics_sync TIMESTAMPTZ
);
SELECT create_hypertable('twitch_clips_social_media', 'created_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '30 days');
ALTER TABLE twitch_clips_social_media SET (timescaledb.compress, timescaledb.compress_segmentby = 'streamer_login', timescaledb.compress_orderby = 'created_at DESC');
SELECT add_compression_policy('twitch_clips_social_media', INTERVAL '30 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_twitch_clips_social_media_streamer ON twitch_clips_social_media(streamer_login, created_at);
CREATE INDEX IF NOT EXISTS idx_twitch_clips_social_media_status ON twitch_clips_social_media(status);

CREATE TABLE IF NOT EXISTS twitch_clips_social_analytics (
    id                BIGSERIAL PRIMARY KEY,
    clip_id           BIGINT NOT NULL REFERENCES twitch_clips_social_media(id) ON DELETE CASCADE,
    platform          TEXT NOT NULL,
    platform_video_id TEXT,
    views             INTEGER DEFAULT 0,
    likes             INTEGER DEFAULT 0,
    comments          INTEGER DEFAULT 0,
    shares            INTEGER DEFAULT 0,
    saves             INTEGER DEFAULT 0,
    watch_time_avg    DOUBLE PRECISION,
    completion_rate   DOUBLE PRECISION,
    ctr               DOUBLE PRECISION,
    engagement_rate   DOUBLE PRECISION,
    external_clicks   INTEGER DEFAULT 0,
    new_followers     INTEGER DEFAULT 0,
    synced_at         TIMESTAMPTZ NOT NULL,
    posted_at         TIMESTAMPTZ
);
SELECT create_hypertable('twitch_clips_social_analytics', 'synced_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '30 days');
ALTER TABLE twitch_clips_social_analytics SET (timescaledb.compress, timescaledb.compress_segmentby = 'platform', timescaledb.compress_orderby = 'synced_at DESC');
SELECT add_compression_policy('twitch_clips_social_analytics', INTERVAL '30 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_twitch_clips_social_analytics_clip ON twitch_clips_social_analytics(clip_id, synced_at);
CREATE INDEX IF NOT EXISTS idx_twitch_clips_social_analytics_platform ON twitch_clips_social_analytics(platform, posted_at);

CREATE TABLE IF NOT EXISTS twitch_clips_upload_queue (
    id            BIGSERIAL PRIMARY KEY,
    clip_id       BIGINT NOT NULL REFERENCES twitch_clips_social_media(id) ON DELETE CASCADE,
    platform      TEXT NOT NULL,
    status        TEXT DEFAULT 'pending',
    priority      INTEGER DEFAULT 0,
    title         TEXT,
    description   TEXT,
    hashtags      TEXT,
    scheduled_at  TIMESTAMPTZ,
    attempts      INTEGER DEFAULT 0,
    last_error    TEXT,
    last_attempt_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_twitch_clips_upload_queue_status ON twitch_clips_upload_queue(status, priority DESC);

CREATE TABLE IF NOT EXISTS clip_templates_global (
    id                    BIGSERIAL PRIMARY KEY,
    template_name         TEXT NOT NULL UNIQUE,
    description_template  TEXT NOT NULL,
    hashtags              TEXT NOT NULL,
    category              TEXT,
    usage_count           INTEGER DEFAULT 0,
    created_at            TIMESTAMPTZ DEFAULT NOW(),
    created_by            TEXT
);

CREATE TABLE IF NOT EXISTS clip_templates_streamer (
    id                    BIGSERIAL PRIMARY KEY,
    streamer_login        TEXT NOT NULL,
    template_name         TEXT NOT NULL,
    description_template  TEXT NOT NULL,
    hashtags              TEXT NOT NULL,
    is_default            BOOLEAN DEFAULT FALSE,
    created_at            TIMESTAMPTZ DEFAULT NOW(),
    updated_at            TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(streamer_login, template_name)
);
CREATE INDEX IF NOT EXISTS idx_clip_templates_streamer_login ON clip_templates_streamer(streamer_login);

CREATE TABLE IF NOT EXISTS clip_last_hashtags (
    streamer_login TEXT PRIMARY KEY,
    hashtags       TEXT NOT NULL,
    last_used_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS clip_fetch_history (
    id              BIGSERIAL PRIMARY KEY,
    streamer_login  TEXT NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    clips_found     INTEGER DEFAULT 0,
    clips_new       INTEGER DEFAULT 0,
    fetch_duration_ms INTEGER,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_clip_fetch_history_streamer ON clip_fetch_history(streamer_login, fetched_at DESC);

-- ========= Link & Promo Tracking =========
CREATE TABLE IF NOT EXISTS twitch_link_clicks (
    id               BIGSERIAL PRIMARY KEY,
    clicked_at       TIMESTAMPTZ DEFAULT NOW(),
    streamer_login   TEXT NOT NULL,
    tracking_token   TEXT,
    discord_user_id  TEXT,
    discord_username TEXT,
    guild_id         TEXT,
    channel_id       TEXT,
    message_id       TEXT,
    ref_code         TEXT,
    source_hint      TEXT
);
SELECT create_hypertable('twitch_link_clicks', 'clicked_at', if_not_exists => TRUE, migrate_data => TRUE, chunk_time_interval => INTERVAL '7 days');
ALTER TABLE twitch_link_clicks SET (timescaledb.compress, timescaledb.compress_segmentby = 'streamer_login', timescaledb.compress_orderby = 'clicked_at DESC');
SELECT add_compression_policy('twitch_link_clicks', INTERVAL '7 days', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_twitch_link_clicks_streamer ON twitch_link_clicks(streamer_login);

-- ========= Housekeeping =========
ANALYZE;
