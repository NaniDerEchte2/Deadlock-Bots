"""
Migrate Twitch auth tables from SQLite → PostgreSQL.

Usage:
    python migrations/twitch_auth_to_pg.py --dry-run     # Zeigt was migriert würde
    python migrations/twitch_auth_to_pg.py               # Führt Migration durch
    python migrations/twitch_auth_to_pg.py --no-drop     # Migration ohne SQLite-Tabellen zu droppen

Tables migrated:
  - twitch_raid_auth          (BLOB access_token_enc/refresh_token_enc → BYTEA)
  - social_media_platform_auth (BLOB *_enc fields → BYTEA)
  - oauth_state_tokens         (plain text)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make sure project root is in path so 'service' and cog imports work
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import sqlite3

from cogs.twitch.storage_pg import ensure_schema
from cogs.twitch.storage_pg import get_conn as pg_get_conn
from service import db as central_db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sqlite_rows(sql: str, params=()) -> list[sqlite3.Row]:
    with central_db.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql, params).fetchall()


def _sqlite_table_exists(table: str) -> bool:
    with central_db.get_conn() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None


def _to_bytea(val) -> bytes | None:
    """Convert SQLite BLOB (bytes or memoryview) to Python bytes for PG BYTEA."""
    if val is None:
        return None
    if isinstance(val, memoryview):
        return bytes(val)
    if isinstance(val, (bytes, bytearray)):
        return bytes(val)
    # Fallback: should not happen for BLOB columns
    return val


# ---------------------------------------------------------------------------
# Per-table migration
# ---------------------------------------------------------------------------


def migrate_twitch_raid_auth(pg_conn, dry_run: bool) -> int:
    if not _sqlite_table_exists("twitch_raid_auth"):
        print("  [SKIP] twitch_raid_auth: table does not exist in SQLite")
        return 0

    rows = _sqlite_rows("SELECT * FROM twitch_raid_auth")
    print(f"  twitch_raid_auth: {len(rows)} rows")
    if dry_run or not rows:
        return len(rows)

    for r in rows:
        pg_conn.execute(
            """
            INSERT INTO twitch_raid_auth (
                twitch_user_id, twitch_login,
                access_token, refresh_token,
                token_expires_at, scopes,
                authorized_at, last_refreshed_at,
                raid_enabled, created_at,
                legacy_access_token, legacy_refresh_token,
                legacy_scopes, legacy_saved_at,
                needs_reauth, reauth_notified_at,
                access_token_enc, refresh_token_enc,
                enc_version, enc_kid, enc_migrated_at
            ) VALUES (
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?
            )
            ON CONFLICT (twitch_user_id) DO UPDATE SET
                twitch_login         = EXCLUDED.twitch_login,
                access_token         = EXCLUDED.access_token,
                refresh_token        = EXCLUDED.refresh_token,
                token_expires_at     = EXCLUDED.token_expires_at,
                scopes               = EXCLUDED.scopes,
                authorized_at        = EXCLUDED.authorized_at,
                last_refreshed_at    = EXCLUDED.last_refreshed_at,
                raid_enabled         = EXCLUDED.raid_enabled,
                legacy_access_token  = EXCLUDED.legacy_access_token,
                legacy_refresh_token = EXCLUDED.legacy_refresh_token,
                legacy_scopes        = EXCLUDED.legacy_scopes,
                legacy_saved_at      = EXCLUDED.legacy_saved_at,
                needs_reauth         = EXCLUDED.needs_reauth,
                reauth_notified_at   = EXCLUDED.reauth_notified_at,
                access_token_enc     = EXCLUDED.access_token_enc,
                refresh_token_enc    = EXCLUDED.refresh_token_enc,
                enc_version          = EXCLUDED.enc_version,
                enc_kid              = EXCLUDED.enc_kid,
                enc_migrated_at      = EXCLUDED.enc_migrated_at
            """,
            (
                r["twitch_user_id"],
                r["twitch_login"],
                r["access_token"],
                r["refresh_token"],
                r["token_expires_at"],
                r["scopes"],
                r["authorized_at"],
                r["last_refreshed_at"],
                bool(r["raid_enabled"]),
                r["created_at"],
                r["legacy_access_token"],
                r["legacy_refresh_token"],
                r["legacy_scopes"],
                r["legacy_saved_at"],
                bool(r["needs_reauth"]) if r["needs_reauth"] is not None else False,
                r["reauth_notified_at"],
                _to_bytea(r["access_token_enc"]),
                _to_bytea(r["refresh_token_enc"]),
                r["enc_version"],
                r["enc_kid"],
                r["enc_migrated_at"],
            ),
        )
    return len(rows)


def migrate_social_media_platform_auth(pg_conn, dry_run: bool) -> int:
    if not _sqlite_table_exists("social_media_platform_auth"):
        print("  [SKIP] social_media_platform_auth: table does not exist in SQLite")
        return 0

    rows = _sqlite_rows("SELECT * FROM social_media_platform_auth")
    print(f"  social_media_platform_auth: {len(rows)} rows")
    if dry_run or not rows:
        return len(rows)

    for r in rows:
        pg_conn.execute(
            """
            INSERT INTO social_media_platform_auth (
                platform, streamer_login,
                access_token_enc, refresh_token_enc,
                client_id, client_secret_enc,
                token_expires_at, scopes,
                platform_user_id, platform_username,
                enc_version, enc_kid,
                authorized_at, last_refreshed_at, enabled
            ) VALUES (
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?
            )
            ON CONFLICT (platform, streamer_login) DO UPDATE SET
                access_token_enc  = EXCLUDED.access_token_enc,
                refresh_token_enc = EXCLUDED.refresh_token_enc,
                client_id         = EXCLUDED.client_id,
                client_secret_enc = EXCLUDED.client_secret_enc,
                token_expires_at  = EXCLUDED.token_expires_at,
                scopes            = EXCLUDED.scopes,
                platform_user_id  = EXCLUDED.platform_user_id,
                platform_username = EXCLUDED.platform_username,
                enc_version       = EXCLUDED.enc_version,
                enc_kid           = EXCLUDED.enc_kid,
                authorized_at     = EXCLUDED.authorized_at,
                last_refreshed_at = EXCLUDED.last_refreshed_at,
                enabled           = EXCLUDED.enabled
            """,
            (
                r["platform"],
                r["streamer_login"],
                _to_bytea(r["access_token_enc"]),
                _to_bytea(r["refresh_token_enc"]),
                r["client_id"],
                _to_bytea(r["client_secret_enc"]),
                r["token_expires_at"],
                r["scopes"],
                r["platform_user_id"],
                r["platform_username"],
                r["enc_version"],
                r["enc_kid"],
                r["authorized_at"],
                r["last_refreshed_at"],
                r["enabled"],
            ),
        )
    return len(rows)


def migrate_oauth_state_tokens(pg_conn, dry_run: bool) -> int:
    if not _sqlite_table_exists("oauth_state_tokens"):
        print("  [SKIP] oauth_state_tokens: table does not exist in SQLite")
        return 0

    rows = _sqlite_rows("SELECT * FROM oauth_state_tokens")
    print(f"  oauth_state_tokens: {len(rows)} rows")
    if dry_run or not rows:
        return len(rows)

    for r in rows:
        pg_conn.execute(
            """
            INSERT INTO oauth_state_tokens (
                state_token, platform, streamer_login,
                redirect_uri, pkce_verifier,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (state_token) DO NOTHING
            """,
            (
                r["state_token"],
                r["platform"],
                r["streamer_login"],
                r["redirect_uri"],
                r["pkce_verifier"],
                r["created_at"],
                r["expires_at"],
            ),
        )
    return len(rows)


# ---------------------------------------------------------------------------
# Drop SQLite tables (optional, run after verification)
# ---------------------------------------------------------------------------


def drop_sqlite_auth_tables() -> None:
    drop_sql_by_table = {
        "oauth_state_tokens": "DROP TABLE IF EXISTS oauth_state_tokens",
        "social_media_platform_auth": "DROP TABLE IF EXISTS social_media_platform_auth",
        "twitch_raid_auth": "DROP TABLE IF EXISTS twitch_raid_auth",
    }
    with central_db.get_conn() as conn:
        for table, drop_sql in drop_sql_by_table.items():
            if _sqlite_table_exists(table):
                conn.execute(drop_sql)
                print(f"  Dropped SQLite table: {table}")
        conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Twitch auth tables SQLite → PostgreSQL")
    parser.add_argument("--dry-run", action="store_true", help="Only show what would be migrated")
    parser.add_argument(
        "--no-drop", action="store_true", help="Skip dropping SQLite tables after migration"
    )
    args = parser.parse_args()

    if args.dry_run:
        print("=== DRY RUN (no changes will be made) ===")

    # Ensure PG schema is up to date
    print("\n[1/2] Ensuring PostgreSQL auth schemas...")
    with pg_get_conn() as pg_conn:
        ensure_schema(pg_conn)
    print("  OK")

    # Migrate
    print("\n[2/2] Migrating rows...")
    with pg_get_conn() as pg_conn:
        migrate_twitch_raid_auth(pg_conn, args.dry_run)
        migrate_social_media_platform_auth(pg_conn, args.dry_run)
        migrate_oauth_state_tokens(pg_conn, args.dry_run)

    if args.dry_run:
        print("\nDry-run complete.")
        print("Run without --dry-run to apply.")
        return

    print("\nMigration complete.")

    if not args.no_drop:
        print("\n[3/3] Dropping SQLite auth tables...")
        drop_sqlite_auth_tables()
    else:
        print("\n[3/3] Skipping SQLite drop (--no-drop).")
        print("      Run without --no-drop after verification to clean up.")

    print("\nDone.")


if __name__ == "__main__":
    main()
