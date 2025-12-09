import sqlite3
from datetime import datetime

DB_PATH = r"C:\Users\Nani-Admin\Documents\Deadlock\service\deadlock.sqlite3"
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("=" * 60)
print("BUILD SYSTEM GAP ANALYSIS")
print("=" * 60)

# Timeline
print("\n=== Build Fetch Timeline ===")
row = c.execute("SELECT COUNT(*), MIN(fetched_at), MAX(fetched_at) FROM hero_build_sources").fetchone()
print(f"Total sources: {row[0]}")
print(f"Oldest fetch: {datetime.fromtimestamp(row[1])}")
print(f"Newest fetch: {datetime.fromtimestamp(row[2])}")

print("\n=== Clone Timeline ===")
row = c.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at), MAX(updated_at) FROM hero_build_clones").fetchone()
if row[0] > 0:
    print(f"Total clones: {row[0]}")
    print(f"First created: {datetime.fromtimestamp(row[1])}")
    print(f"Last created: {datetime.fromtimestamp(row[2])}")
    print(f"Last updated: {datetime.fromtimestamp(row[3])}")

# Gap analysis
print("\n=== Clones vs Sources ===")
sources = c.execute("SELECT COUNT(*) FROM hero_build_sources").fetchone()[0]
clones = c.execute("SELECT COUNT(*) FROM hero_build_clones").fetchone()[0]
print(f"Sources: {sources}")
print(f"Clones: {clones}")
print(f"Gap: {sources - clones} builds NOT cloned")

# Check if there are sources without clones
print("\n=== Sources WITHOUT Clones ===")
orphans = c.execute("""
    SELECT hbs.hero_build_id, hbs.name, hbs.hero_id, hbs.fetched_at
    FROM hero_build_sources hbs
    LEFT JOIN hero_build_clones hbc ON hbs.hero_build_id = hbc.origin_hero_build_id
    WHERE hbc.id IS NULL
    ORDER BY hbs.fetched_at DESC
    LIMIT 10
""").fetchall()

if orphans:
    print(f"Found {len(orphans)} sources without clones (showing first 10):")
    for row in orphans:
        print(f"  - Build {row[0]}: {row[1]} (Hero {row[2]}, fetched {datetime.fromtimestamp(row[3])})")
else:
    print("All sources have clones!")

# Check builds by status
print("\n=== Clone Status Breakdown ===")
for row in c.execute("SELECT status, COUNT(*) FROM hero_build_clones GROUP BY status"):
    print(f"  {row[0]}: {row[1]}")

# Check latest BuildMirror activity
print("\n=== BuildMirror Activity ===")
latest_fetch = c.execute("SELECT MAX(fetched_at) FROM hero_build_sources").fetchone()[0]
if latest_fetch:
    time_since = datetime.now().timestamp() - latest_fetch
    hours_ago = time_since / 3600
    print(f"Last fetch: {datetime.fromtimestamp(latest_fetch)} ({hours_ago:.1f} hours ago)")
    if hours_ago > 4:
        print("⚠️ WARNING: No fetches in over 4 hours! BuildMirror may be stuck.")
    else:
        print("✓ BuildMirror is active (fetches every ~4h)")

# Check latest BuildPublisher activity
print("\n=== BuildPublisher Activity ===")
latest_clone = c.execute("SELECT MAX(created_at) FROM hero_build_clones").fetchone()[0]
if latest_clone:
    time_since = datetime.now().timestamp() - latest_clone
    hours_ago = time_since / 3600
    print(f"Last clone created: {datetime.fromtimestamp(latest_clone)} ({hours_ago:.1f} hours ago)")
    if hours_ago > 1:
        print("⚠️ WARNING: No new clones in over 1 hour! BuildPublisher may be stuck.")
    else:
        print("✓ BuildPublisher is active")

conn.close()
print("\n" + "=" * 60)
