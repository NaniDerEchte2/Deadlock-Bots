#!/usr/bin/env python3
"""
Lossless one-shot migration der Twitch Analytics-Daten von SQLite -> Postgres/Timescale.

Kopiert nur Analyse-Tabellen (keine Tokens/OAuth). Erwartet, dass das Ziel-Schema
bereits mit cogs/twitch/migrations/twitch_analytics_schema.sql angelegt wurde.

Usage:
    python cogs/twitch/migrations/twitch_analytics_migrate.py \\
        --sqlite service/deadlock.sqlite3 \\
        --dsn "postgresql://<username>:<password>@localhost:5432/twitch_analytics"
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Iterable, Sequence

import psycopg

# Reihenfolge respektiert FK-Abhängigkeiten (Streamers/Dim -> Sessions -> Child-Tabellen).
TABLES: Sequence[str] = [
    "twitch_streamers",
    "twitch_stream_sessions",
    "twitch_live_state",
    "twitch_session_viewers",
    "twitch_session_chatters",
    "twitch_chatter_rollup",
    "twitch_chat_messages",
    "twitch_stats_tracked",
    "twitch_stats_category",
    "twitch_link_clicks",
    "twitch_follow_events",
    "twitch_subscription_events",
    "twitch_channel_points_events",
    "twitch_bits_events",
    "twitch_hype_train_events",
    "twitch_ad_break_events",
    "twitch_ban_events",
    "twitch_shoutout_events",
    "twitch_channel_updates",
    "twitch_raid_history",
    "twitch_subscriptions_snapshot",
    "twitch_eventsub_capacity_snapshot",
    "twitch_ads_schedule_snapshot",
    "twitch_clips_social_media",
    "twitch_clips_social_analytics",
    "twitch_clips_upload_queue",
    "clip_templates_global",
    "clip_templates_streamer",
    "clip_last_hashtags",
    "clip_fetch_history",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SQLite -> Postgres Migration (Twitch Analytics)")
    parser.add_argument(
        "--sqlite",
        default=os.environ.get("SQLITE_PATH", "service/deadlock.sqlite3"),
        help="Pfad zur SQLite-Datei (default: service/deadlock.sqlite3)",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("TWITCH_ANALYTICS_DSN"),
        help="Postgres DSN, z. B. postgresql://<username>:<password>@host/db (Env: TWITCH_ANALYTICS_DSN)",
    )
    parser.add_argument(
        "--batch", type=int, default=5000, help="Batch-Größe für COPY (default 5000)"
    )
    parser.add_argument(
        "--no-truncate",
        action="store_true",
        help="Nicht TRUNCATEn vor dem Import (standardmäßig wird TRUNCATE ausgeführt).",
    )
    return parser.parse_args()


def ensure_table_exists_sqlite(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=? COLLATE NOCASE", (table,)
    )
    return cur.fetchone() is not None


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def pg_columns(pg_conn: psycopg.Connection, table: str) -> list[str]:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        cols = [row[0] for row in cur.fetchall()]
    if not cols:
        raise RuntimeError(f"Zieltabelle fehlt in Postgres: {table}")
    return cols


def iter_sqlite_rows(
    conn: sqlite3.Connection, table: str, batch_size: int, select_cols: Sequence[str]
) -> Iterable[list[sqlite3.Row]]:
    if table not in TABLES:
        raise ValueError(f"Unexpected table requested: {table}")
    sql = f"SELECT {', '.join(select_cols)} FROM {table}"  # nosec B608: table names are whitelisted above
    cur = conn.execute(sql)
    while True:
        batch = cur.fetchmany(batch_size)
        if not batch:
            break
        yield batch


def migrate_table(
    table: str,
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection,
    batch_size: int,
    truncate_first: bool,
) -> int:
    if not ensure_table_exists_sqlite(sqlite_conn, table):
        print(f"[skip] {table}: Tabelle fehlt in SQLite")
        return 0

    sqlite_conn.row_factory = sqlite3.Row
    src_cols_list = sqlite_columns(sqlite_conn, table)
    src_cols_set = set(src_cols_list)
    dst_cols = pg_columns(pg_conn, table)

    if truncate_first:
        with pg_conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE;")
        pg_conn.commit()

    inserted = 0
    with pg_conn.cursor() as cur:
        # Use text format (tab-delimited) because psycopg's copy.write_row defaults to text.
        copy_sql = f"COPY {table} ({', '.join(dst_cols)}) FROM STDIN"
        with cur.copy(copy_sql) as copy:
            for batch in iter_sqlite_rows(sqlite_conn, table, batch_size, src_cols_list):
                for row in batch:
                    copy.write_row([row[col] if col in src_cols_set else None for col in dst_cols])
                    inserted += 1
    pg_conn.commit()
    print(f"[ok]   {table}: {inserted} Zeilen migriert")
    return inserted


def main() -> int:
    args = parse_args()
    if not args.dsn:
        print("Fehlender DSN: setze --dsn oder Env TWITCH_ANALYTICS_DSN", file=sys.stderr)
        return 1
    if not os.path.exists(args.sqlite):
        print(f"SQLite-Datei nicht gefunden: {args.sqlite}", file=sys.stderr)
        return 1

    sqlite_conn = sqlite3.connect(args.sqlite)
    sqlite_conn.execute("PRAGMA journal_mode=WAL;")

    with psycopg.connect(args.dsn) as pg_conn:
        pg_conn.execute("SET session_replication_role = 'replica';")
        total = 0
        for table in TABLES:
            total += migrate_table(
                table, sqlite_conn, pg_conn, args.batch, truncate_first=not args.no_truncate
            )
        pg_conn.execute("SET session_replication_role = 'origin';")
        print(f"Fertig. Gesamt migriert: {total} Zeilen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
