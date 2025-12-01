#!/usr/bin/env python3
import sqlite3
from pathlib import Path

db_path = Path(__file__).parent / "service" / "deadlock.sqlite3"
conn = sqlite3.connect(str(db_path))
conn.row_factory = sqlite3.Row

print("=== Checking users with invites but no steam_links ===")
cursor = conn.execute("""
    SELECT
        sbi.discord_id,
        sbi.steam_id64,
        sbi.status,
        datetime(sbi.invite_sent_at, 'unixepoch', 'localtime') as invited_at,
        sl.steam_id as linked_steam_id,
        sl.verified,
        sl.name
    FROM steam_beta_invites sbi
    LEFT JOIN steam_links sl ON sbi.discord_id = sl.user_id
    WHERE sbi.status = 'invite_sent'
    ORDER BY sbi.invite_sent_at DESC
""")

for row in cursor:
    print(f"\nDiscord ID: {row['discord_id']}")
    print(f"  Steam (from beta_invites): {row['steam_id64']}")
    print(f"  Status: {row['status']}")
    print(f"  Invited at: {row['invited_at']}")
    print(f"  Linked Steam ID: {row['linked_steam_id'] or 'NONE'}")
    print(f"  Verified: {row['verified'] if row['linked_steam_id'] else 'N/A'}")
    print(f"  Name: {row['name'] or 'N/A'}")

print("\n=== Check specific user 1149248714234409000 ===")
cursor = conn.execute("""
    SELECT user_id, steam_id, name, verified, primary_account,
           datetime(created_at) as created, datetime(updated_at) as updated
    FROM steam_links
    WHERE user_id = 1149248714234409000
""")
rows = cursor.fetchall()
if rows:
    for row in rows:
        print(f"Found link: Steam {row['steam_id']}, Name: {row['name']}, Verified: {row['verified']}")
else:
    print("No steam_links found for this user!")

print("\n=== Check if user has beta_invite entry ===")
cursor = conn.execute("""
    SELECT discord_id, steam_id64, account_id, status,
           datetime(created_at, 'unixepoch', 'localtime') as created,
           datetime(invite_sent_at, 'unixepoch', 'localtime') as invited
    FROM steam_beta_invites
    WHERE discord_id = 1149248714234409000
""")
row = cursor.fetchone()
if row:
    print(f"Beta invite entry found:")
    print(f"  Discord ID: {row['discord_id']}")
    print(f"  Steam ID64: {row['steam_id64']}")
    print(f"  Account ID: {row['account_id']}")
    print(f"  Status: {row['status']}")
    print(f"  Created: {row['created']}")
    print(f"  Invited: {row['invited']}")
else:
    print("No beta_invite entry found!")

conn.close()
