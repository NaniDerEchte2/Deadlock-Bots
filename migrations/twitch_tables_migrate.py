#!/usr/bin/env python3
"""
Einmalige Migration: Twitch-Tabellen SQLite → PostgreSQL.

Auth-Tabellen BLEIBEN in SQLite (werden nicht migriert, nicht gelöscht):
  - twitch_raid_auth
  - social_media_platform_auth
  - oauth_state_tokens

Alle anderen Tabellen → PostgreSQL, danach DROP in SQLite.

Aufruf:
    python migrations/twitch_tables_migrate.py [--dry-run] [--no-drop]

Flags:
    --dry-run   Nur Zeilenzahlen zeigen, nichts schreiben/löschen
    --no-drop   Migriere Daten, lösche aber SQLite-Tabellen NICHT
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Projekt-Root ins sys.path aufnehmen
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("twitch_migrate")

# ---------------------------------------------------------------------------
# Auth-Tabellen: NIEMALS migrieren/löschen
# ---------------------------------------------------------------------------
AUTH_TABLES = {"twitch_raid_auth", "social_media_platform_auth", "oauth_state_tokens"}

# ---------------------------------------------------------------------------
# Reihenfolge: FK-Abhängigkeiten beachten (Parent vor Child)
# ---------------------------------------------------------------------------
TABLES_TO_MIGRATE: list[str] = [
    # Base
    "twitch_streamers",
    "twitch_live_state",
    "twitch_stats_tracked",
    "twitch_stats_category",
    "twitch_link_clicks",
    # Sessions
    "twitch_stream_sessions",
    "twitch_session_viewers",
    "twitch_session_chatters",
    "twitch_chatter_rollup",
    "twitch_chat_messages",
    # Raid
    "twitch_raid_history",
    "twitch_raid_blacklist",
    # Token
    "twitch_token_blacklist",
    # Snapshots
    "twitch_subscriptions_snapshot",
    "twitch_eventsub_capacity_snapshot",
    "twitch_ads_schedule_snapshot",
    # Invites / outreach
    "discord_invite_codes",
    "twitch_streamer_invites",
    "twitch_partner_outreach",
    # Events
    "twitch_bits_events",
    "twitch_hype_train_events",
    "twitch_subscription_events",
    "twitch_channel_updates",
    "twitch_ad_break_events",
    "twitch_ban_events",
    "twitch_shoutout_events",
    "twitch_follow_events",
    "twitch_channel_points_events",
    # Social media clips (parent before children)
    "twitch_clips_social_media",
    "twitch_clips_social_analytics",
    "twitch_clips_upload_queue",
    # Templates
    "clip_templates_global",
    "clip_templates_streamer",
    "clip_last_hashtags",
    "clip_fetch_history",
]

# Reihenfolge beim Löschen: Children vor Parents
DROP_ORDER = list(reversed(TABLES_TO_MIGRATE))


def _sqlite_table_exists(sqlite_conn, table: str) -> bool:
    row = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row)


def _sqlite_row_count(sqlite_conn, table: str) -> int:
    row = sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
    return int(row[0]) if row else 0


def _pg_row_count(pg_conn, table: str) -> int:
    row = pg_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
    return int(row[0]) if row else 0


def _pg_table_exists(pg_conn, table: str) -> bool:
    row = pg_conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _get_columns(sqlite_conn, table: str) -> list[str]:
    rows = sqlite_conn.execute(f"PRAGMA table_info({table})").fetchall()  # noqa: S608
    return [row[1] for row in rows]


def _migrate_table(sqlite_conn, pg_conn, table: str, dry_run: bool) -> tuple[int, int]:
    """Migrate one table. Returns (sqlite_count, migrated_count)."""
    if not _sqlite_table_exists(sqlite_conn, table):
        log.info("  SKIP  %s (not in SQLite)", table)
        return 0, 0

    sqlite_count = _sqlite_row_count(sqlite_conn, table)
    if sqlite_count == 0:
        log.info("  SKIP  %s (0 rows)", table)
        return 0, 0

    if not _pg_table_exists(pg_conn, table):
        log.warning("  WARN  %s not in PG yet — run ensure_schema() first", table)
        return sqlite_count, 0

    cols = _get_columns(sqlite_conn, table)
    if not cols:
        log.warning("  WARN  %s: no columns found", table)
        return sqlite_count, 0

    col_list = ", ".join(cols)
    placeholders = ", ".join(["?"] * len(cols))

    rows = sqlite_conn.execute(f"SELECT {col_list} FROM {table}").fetchall()  # noqa: S608

    if dry_run:
        log.info("  DRY   %s: %d rows to migrate", table, len(rows))
        return sqlite_count, len(rows)

    inserted = 0
    skipped = 0
    for row in rows:
        try:
            pg_conn.execute(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",  # noqa: S608
                tuple(row),
            )
            inserted += 1
        except Exception as exc:
            log.debug("  ROW   %s insert failed: %s", table, exc)
            skipped += 1

    log.info(
        "  OK    %s: %d inserted, %d skipped (sqlite had %d)",
        table,
        inserted,
        skipped,
        sqlite_count,
    )
    return sqlite_count, inserted


def run_migration(dry_run: bool = False, no_drop: bool = False) -> None:
    from cogs.twitch.storage_pg import ensure_schema
    from cogs.twitch.storage_pg import get_conn as pg_get_conn
    from service import db as central_db

    log.info("=== Twitch Tables Migration: SQLite → PostgreSQL ===")
    if dry_run:
        log.info("DRY-RUN mode: nothing will be written or deleted")

    # Ensure PG schema exists
    log.info("Ensuring PG schema...")
    with pg_get_conn() as pg_conn:
        ensure_schema(pg_conn)
    log.info("PG schema ready.")

    # Open SQLite
    with central_db.get_conn() as sqlite_conn:
        totals: dict[str, tuple[int, int]] = {}

        with pg_get_conn() as pg_conn:
            for table in TABLES_TO_MIGRATE:
                assert table not in AUTH_TABLES, f"BUG: {table} is auth table!"
                sqlite_n, pg_n = _migrate_table(sqlite_conn, pg_conn, table, dry_run)
                totals[table] = (sqlite_n, pg_n)

        # Summary
        log.info("")
        log.info("=== Migration Summary ===")
        total_sqlite = total_pg = 0
        for table, (s, p) in totals.items():
            if s > 0 or p > 0:
                match = "✓" if (dry_run or s == 0 or p >= 0) else "✗"
                log.info("  %s %-40s  sqlite=%d  pg=%d", match, table, s, p)
            total_sqlite += s
            total_pg += p
        log.info("  TOTAL: sqlite=%d  pg=%d", total_sqlite, total_pg)

        if dry_run or no_drop:
            if dry_run:
                log.info("DRY-RUN: no data written, no tables dropped.")
            else:
                log.info("--no-drop: tables NOT dropped from SQLite.")
            return

        # Verify row counts before dropping
        log.info("")
        log.info("=== Verifying PG row counts before DROP ===")
        ok = True
        with pg_get_conn() as pg_conn:
            for table in TABLES_TO_MIGRATE:
                if not _sqlite_table_exists(sqlite_conn, table):
                    continue
                s_count = _sqlite_row_count(sqlite_conn, table)
                if s_count == 0:
                    continue
                p_count = _pg_row_count(pg_conn, table)
                if p_count < s_count:
                    log.error(
                        "  FAIL  %s: sqlite=%d pg=%d — WILL NOT DROP",
                        table,
                        s_count,
                        p_count,
                    )
                    ok = False
                else:
                    log.info("  OK    %s: sqlite=%d pg=%d", table, s_count, p_count)

        if not ok:
            log.error("Some tables have fewer rows in PG than SQLite. Aborting DROP step.")
            sys.exit(1)

        # Drop migrated tables from SQLite (reverse order for FK safety)
        log.info("")
        log.info("=== Dropping migrated tables from SQLite ===")
        for table in DROP_ORDER:
            if _sqlite_table_exists(sqlite_conn, table):
                sqlite_conn.execute(f"DROP TABLE IF EXISTS {table}")  # noqa: S608
                log.info("  DROPPED %s", table)
        sqlite_conn.commit()
        log.info(
            "Done. Auth tables (twitch_raid_auth, social_media_platform_auth, oauth_state_tokens) remain in SQLite."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate Twitch tables from SQLite to PostgreSQL")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen, don't write anything"
    )
    parser.add_argument(
        "--no-drop", action="store_true", help="Migrate data but keep SQLite tables"
    )
    args = parser.parse_args()
    run_migration(dry_run=args.dry_run, no_drop=args.no_drop)
