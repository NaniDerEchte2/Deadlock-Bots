#!/usr/bin/env python3
import sqlite3
from pathlib import Path

db_path = Path(__file__).parent / "service" / "deadlock.sqlite3"
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row

print("=== Recent Beta Invites (from audit table) ===")
cursor = conn.execute("""
    SELECT discord_id, discord_name, steam_id64,
           datetime(invited_at, 'unixepoch', 'localtime') as invited_at
    FROM beta_invite_audit
    ORDER BY invited_at DESC
    LIMIT 20
""")
for row in cursor:
    print(f"Discord ID: {row['discord_id']}, Name: {row['discord_name']}, Steam: {row['steam_id64']}, Invited: {row['invited_at']}")

print("\n=== Beta Invites with status 'invite_sent' ===")
cursor = conn.execute("""
    SELECT discord_id, steam_id64, status,
           datetime(created_at, 'unixepoch', 'localtime') as created,
           datetime(invite_sent_at, 'unixepoch', 'localtime') as invited
    FROM steam_beta_invites
    WHERE status = 'invite_sent'
    ORDER BY invite_sent_at DESC
    LIMIT 20
""")
for row in cursor:
    print(f"Discord ID: {row['discord_id']}, Steam: {row['steam_id64']}, Status: {row['status']}, Invited: {row['invited']}")

print("\n=== Steam Links (all) ===")
cursor = conn.execute("""
    SELECT user_id, steam_id, name, verified, primary_account,
           datetime(created_at) as created, datetime(updated_at) as updated
    FROM steam_links
    ORDER BY updated_at DESC
    LIMIT 20
""")
for row in cursor:
    print(f"User ID: {row['user_id']}, Steam: {row['steam_id']}, Name: {row['name']}, Verified: {row['verified']}, Primary: {row['primary_account']}, Updated: {row['updated']}")

conn.close()
