import sqlite3
from datetime import datetime

DB_PATH = r"C:\Users\Nani-Admin\Documents\Deadlock\service\deadlock.sqlite3"
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("=" * 60)
print("LETZTER ERFOLGREICHER BUILD UPLOAD")
print("=" * 60)

# Check last successful clone
print("\n=== Letzte erfolgreiche Clones ===")
last_clones = c.execute("""
    SELECT hbc.id, hbs.name, hbc.status, hbc.uploaded_build_id,
           hbc.created_at, hbc.updated_at
    FROM hero_build_clones hbc
    JOIN hero_build_sources hbs ON hbc.origin_hero_build_id = hbs.hero_build_id
    WHERE hbc.status IN ('done', 'uploaded')
    ORDER BY hbc.updated_at DESC
    LIMIT 5
""").fetchall()

if last_clones:
    for row in last_clones:
        clone_id, name, status, uploaded_id, created, updated = row
        print(f"\nClone #{clone_id}: {name}")
        print(f"  Status: {status}")
        print(f"  Uploaded Build ID: {uploaded_id}")
        print(f"  Created: {datetime.fromtimestamp(created)}")
        print(f"  Updated: {datetime.fromtimestamp(updated)}")

    # Get the very last one
    last = last_clones[0]
    hours_ago = (datetime.now().timestamp() - last[5]) / 3600
    print(f"\n>>> LETZTER UPLOAD: vor {hours_ago:.1f} Stunden")
    print(f"    Zeitpunkt: {datetime.fromtimestamp(last[5])}")
else:
    print("Keine erfolgreichen Clones gefunden!")

# Check last successful BUILD_PUBLISH task
print("\n\n=== Letzte BUILD_PUBLISH Tasks ===")
last_tasks = c.execute("""
    SELECT id, type, status, created_at, finished_at, result
    FROM steam_tasks
    WHERE type = 'BUILD_PUBLISH'
    AND status = 'DONE'
    ORDER BY finished_at DESC
    LIMIT 5
""").fetchall()

if last_tasks:
    for row in last_tasks:
        task_id, task_type, status, created, finished, result = row
        print(f"\nTask #{task_id}")
        print(f"  Status: {status}")
        print(f"  Created: {datetime.fromtimestamp(created)}")
        print(f"  Finished: {datetime.fromtimestamp(finished)}")
        if result:
            import json
            try:
                res = json.loads(result)
                if 'build_id' in res:
                    print(f"  Build ID: {res['build_id']}")
            except:
                pass

    last_task = last_tasks[0]
    hours_ago = (datetime.now().timestamp() - last_task[4]) / 3600
    print(f"\n>>> LETZTER TASK ABSCHLUSS: vor {hours_ago:.1f} Stunden")
    print(f"    Zeitpunkt: {datetime.fromtimestamp(last_task[4])}")
else:
    print("Keine erfolgreichen Tasks gefunden!")

# Check if there are any failed/processing
print("\n\n=== Status aller Clones ===")
for row in c.execute("SELECT status, COUNT(*) FROM hero_build_clones GROUP BY status"):
    print(f"  {row[0]}: {row[1]}")

print("\n\n=== Status aller BUILD_PUBLISH Tasks ===")
for row in c.execute("SELECT status, COUNT(*) FROM steam_tasks WHERE type = 'BUILD_PUBLISH' GROUP BY status"):
    print(f"  {row[0]}: {row[1]}")

conn.close()
print("\n" + "=" * 60)
