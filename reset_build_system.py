import sqlite3
import shutil
from datetime import datetime
from pathlib import Path

DB_PATH = Path(r"C:\Users\Nani-Admin\Documents\Deadlock\service\deadlock.sqlite3")

print("=" * 60)
print("BUILD SYSTEM CLEAN RESET")
print("=" * 60)

# 1. Backup erstellen
backup_name = f"deadlock_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite3"
backup_path = DB_PATH.parent / backup_name

print(f"\n[1/5] Creating backup...")
shutil.copy2(DB_PATH, backup_path)
print(f"✓ Backup created: {backup_path}")

# 2. Verbindung zur DB
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# 3. Zähle aktuelle Einträge
print(f"\n[2/5] Current state:")
sources_count = c.execute("SELECT COUNT(*) FROM hero_build_sources").fetchone()[0]
clones_count = c.execute("SELECT COUNT(*) FROM hero_build_clones").fetchone()[0]
tasks_count = c.execute("SELECT COUNT(*) FROM steam_tasks WHERE type = 'BUILD_PUBLISH'").fetchone()[0]

print(f"  - hero_build_sources: {sources_count}")
print(f"  - hero_build_clones: {clones_count}")
print(f"  - BUILD_PUBLISH tasks: {tasks_count}")

# 4. Lösche alle Build-Daten
print(f"\n[3/5] Deleting all build data...")

print("  - Clearing hero_build_clones...")
c.execute("DELETE FROM hero_build_clones")
deleted_clones = c.rowcount
print(f"    ✓ Deleted {deleted_clones} clones")

print("  - Clearing hero_build_sources...")
c.execute("DELETE FROM hero_build_sources")
deleted_sources = c.rowcount
print(f"    ✓ Deleted {deleted_sources} sources")

print("  - Clearing BUILD_PUBLISH tasks...")
c.execute("DELETE FROM steam_tasks WHERE type = 'BUILD_PUBLISH'")
deleted_tasks = c.rowcount
print(f"    ✓ Deleted {deleted_tasks} tasks")

# Optional: MAINTAIN_BUILD_CATALOG tasks auch löschen?
catalog_tasks = c.execute("SELECT COUNT(*) FROM steam_tasks WHERE type = 'MAINTAIN_BUILD_CATALOG'").fetchone()[0]
if catalog_tasks > 0:
    print(f"  - Clearing MAINTAIN_BUILD_CATALOG tasks ({catalog_tasks})...")
    c.execute("DELETE FROM steam_tasks WHERE type = 'MAINTAIN_BUILD_CATALOG'")
    print(f"    ✓ Deleted {catalog_tasks} catalog tasks")

# 5. Commit
conn.commit()
print(f"\n[4/5] Changes committed")

# 6. Verify
print(f"\n[5/5] Verifying clean state...")
sources_after = c.execute("SELECT COUNT(*) FROM hero_build_sources").fetchone()[0]
clones_after = c.execute("SELECT COUNT(*) FROM hero_build_clones").fetchone()[0]
tasks_after = c.execute("SELECT COUNT(*) FROM steam_tasks WHERE type = 'BUILD_PUBLISH'").fetchone()[0]

print(f"  - hero_build_sources: {sources_after} (should be 0)")
print(f"  - hero_build_clones: {clones_after} (should be 0)")
print(f"  - BUILD_PUBLISH tasks: {tasks_after} (should be 0)")

if sources_after == 0 and clones_after == 0 and tasks_after == 0:
    print("\n✅ DATABASE CLEAN RESET SUCCESSFUL!")
else:
    print("\n⚠️ WARNING: Some entries remain!")

conn.close()

print("\n" + "=" * 60)
print("Next steps:")
print("1. BuildPublisher will be enabled")
print("2. Bot will need restart to apply changes")
print("3. BuildMirror will fetch new builds on next run (~4h)")
print("=" * 60)
