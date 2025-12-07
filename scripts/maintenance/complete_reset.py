"""
COMPLETE RESET - Deletes ALL builds and resets system.
Run this ONLY when the bot is STOPPED!
"""

import sqlite3

DB_PATH = 'service/deadlock.sqlite3'

print("="*60)
print("COMPLETE BUILD SYSTEM RESET")
print("="*60)
print("\nWARNING: This will delete ALL builds!")
print("Make sure the Discord bot is STOPPED before running this!")
print()

response = input("Type 'YES DELETE ALL' to continue: ")

if response != "YES DELETE ALL":
    print("Aborted.")
    exit(0)

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("\nDeleting all builds...")

# Delete everything
c.execute('DELETE FROM hero_build_clones')
deleted_clones = c.rowcount
print(f"  Deleted {deleted_clones} clones")

c.execute('DELETE FROM hero_build_sources')
deleted_sources = c.rowcount
print(f"  Deleted {deleted_sources} sources")

c.execute("DELETE FROM steam_tasks WHERE type = 'BUILD_PUBLISH'")
deleted_tasks = c.rowcount
print(f"  Deleted {deleted_tasks} tasks")

conn.commit()

# Verify
sources_after = c.execute('SELECT COUNT(*) FROM hero_build_sources').fetchone()[0]
clones_after = c.execute('SELECT COUNT(*) FROM hero_build_clones').fetchone()[0]
tasks_after = c.execute("SELECT COUNT(*) FROM steam_tasks WHERE type = 'BUILD_PUBLISH'").fetchone()[0]

print(f"\nVerification:")
print(f"  Sources: {sources_after} (should be 0)")
print(f"  Clones: {clones_after} (should be 0)")
print(f"  Tasks: {tasks_after} (should be 0)")

conn.close()

if sources_after == 0 and clones_after == 0 and tasks_after == 0:
    print("\nReset successful!")
    print("\nNext steps:")
    print("  1. Start the bot: python main_bot.py")
    print("  2. The BuildMirror will automatically fetch new builds (within 4h)")
    print("  3. Or run: python scripts/maintenance/trigger_rebuild.py")
else:
    print("\nERROR: Some data remains!")
