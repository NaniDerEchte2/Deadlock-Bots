import sqlite3
import json
from datetime import datetime, timedelta

DB_PATH = r"C:\Users\Nani-Admin\Documents\Deadlock\service\deadlock.sqlite3"

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("=" * 60)
print("BUILD SYSTEM STATUS CHECK")
print("=" * 60)

# Check build sources
print("\n=== Build Sources ===")
total_sources = c.execute("SELECT COUNT(*) FROM hero_build_sources").fetchone()[0]
print(f"Total build sources: {total_sources}")

# Get most recent source
recent = c.execute("""
    SELECT name, fetched_at
    FROM hero_build_sources
    ORDER BY fetched_at DESC
    LIMIT 1
""").fetchone()
if recent:
    print(f"Most recent: {recent[0]} at {datetime.fromtimestamp(recent[1])}")

# Check clone queue status
print("\n=== Clone Queue Status ===")
for row in c.execute("SELECT status, COUNT(*) FROM hero_build_clones GROUP BY status"):
    print(f"  {row[0]}: {row[1]}")

# Get recent activity
print("\n=== Recent Clone Activity (last 24h) ===")
cutoff = int((datetime.now() - timedelta(hours=24)).timestamp())
recent_clones = c.execute("""
    SELECT status, COUNT(*)
    FROM hero_build_clones
    WHERE created_at > ?
    GROUP BY status
""", (cutoff,)).fetchall()
if recent_clones:
    for row in recent_clones:
        print(f"  {row[0]}: {row[1]}")
else:
    print("  No activity in last 24h")

# Check pending builds
pending_count = c.execute("SELECT COUNT(*) FROM hero_build_clones WHERE status = 'pending'").fetchone()[0]
print(f"\n=== Pending Builds ===")
print(f"Total pending: {pending_count}")

if pending_count > 0:
    print("\nSample pending builds:")
    for row in c.execute("""
        SELECT hbs.name, hbc.created_at
        FROM hero_build_clones hbc
        JOIN hero_build_sources hbs ON hbc.origin_hero_build_id = hbs.hero_build_id
        WHERE hbc.status = 'pending'
        ORDER BY hbc.created_at DESC
        LIMIT 5
    """):
        print(f"  - {row[0]} (created {datetime.fromtimestamp(row[1])})")

# Check Steam Bridge status
print("\n=== Steam Bridge Status ===")
steam_row = c.execute("SELECT payload, updated_at FROM standalone_bot_state WHERE bot = 'steam'").fetchone()
if steam_row:
    try:
        payload = json.loads(steam_row[0])
        print(f"Last update: {steam_row[1]}")
        print(f"deadlock_gc_ready: {payload.get('deadlock_gc_ready', False)}")
        print(f"steam_connected: {payload.get('steam_connected', False)}")
        print(f"gc_connected: {payload.get('gc_connected', False)}")
    except:
        print("Error parsing Steam Bridge payload")
else:
    print("Steam Bridge not found in DB")

# Check steam tasks
print("\n=== Steam Tasks ===")
task_stats = c.execute("""
    SELECT type, status, COUNT(*)
    FROM steam_tasks
    WHERE type = 'BUILD_PUBLISH'
    GROUP BY type, status
""").fetchall()
if task_stats:
    for row in task_stats:
        print(f"  {row[0]} - {row[1]}: {row[2]}")
else:
    print("  No BUILD_PUBLISH tasks found")

# Recent tasks
print("\n=== Recent Steam Tasks (last 24h) ===")
recent_tasks = c.execute("""
    SELECT status, COUNT(*)
    FROM steam_tasks
    WHERE created_at > ?
    AND type = 'BUILD_PUBLISH'
    GROUP BY status
""", (cutoff,)).fetchall()
if recent_tasks:
    for row in recent_tasks:
        print(f"  {row[0]}: {row[1]}")
else:
    print("  No tasks in last 24h")

conn.close()
print("\n" + "=" * 60)
