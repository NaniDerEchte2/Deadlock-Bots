"""Check bot and build system status."""

import sqlite3
from datetime import datetime

DB_PATH = 'service/deadlock.sqlite3'

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("="*60)
print("BOT & BUILD SYSTEM STATUS")
print("="*60)

# Check BuildMirror status
print("\n=== BuildMirror Status ===")
for row in c.execute("SELECT key, value FROM key_value WHERE namespace = 'hero_build_mirror' ORDER BY key").fetchall():
    key = row[0]
    value = row[1]

    if 'ts' in key and value.isdigit():
        # Convert timestamp to readable date
        ts = int(value)
        date = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
        print(f"  {key}: {date} ({value})")
    else:
        print(f"  {key}: {value}")

# Check BuildPublisher status
print("\n=== BuildPublisher Status ===")
for row in c.execute("SELECT key, value FROM key_value WHERE namespace = 'build_publisher' ORDER BY key").fetchall():
    key = row[0]
    value = row[1]

    if 'ts' in key and value.isdigit():
        ts = int(value)
        date = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
        print(f"  {key}: {date} ({value})")
    else:
        print(f"  {key}: {value}")

# Check Steam Bridge status
print("\n=== Steam Bridge Status ===")
for row in c.execute("SELECT bot, payload FROM standalone_bot_state WHERE bot = 'steam'").fetchall():
    import json
    payload = json.loads(row[1]) if row[1] else {}
    runtime = payload.get('runtime', {})

    print(f"  logged_on: {runtime.get('logged_on', False)}")
    print(f"  deadlock_gc_ready: {runtime.get('deadlock_gc_ready', False)}")

    heartbeat = payload.get('heartbeat')
    if heartbeat:
        date = datetime.fromtimestamp(heartbeat).strftime('%Y-%m-%d %H:%M:%S')
        print(f"  last_heartbeat: {date}")

conn.close()
