"""
Reset and rebuild the hero builds system from scratch.

This script:
1. Deletes ALL existing builds from hero_build_clones and hero_build_sources
2. Triggers a fresh sync via BuildMirror that will respect the 3-builds-per-hero cap
"""

import sqlite3
import time

DB_PATH = 'service/deadlock.sqlite3'

def main():
    print("="*60)
    print("RESET AND REBUILD BUILD SYSTEM")
    print("="*60)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Step 1: Check current counts
    sources_count = c.execute('SELECT COUNT(*) FROM hero_build_sources').fetchone()[0]
    clones_count = c.execute('SELECT COUNT(*) FROM hero_build_clones').fetchone()[0]

    print(f"\nCurrent state:")
    print(f"  Sources: {sources_count}")
    print(f"  Clones: {clones_count}")

    # Step 2: Auto-proceed (user already confirmed)
    print(f"\nWARNING: Deleting ALL {sources_count} sources and {clones_count} clones...")
    print("Proceeding automatically...")

    # Step 3: Delete all builds
    print("\nDeleting all builds...")

    # Delete clones first (foreign key constraints)
    c.execute('DELETE FROM hero_build_clones')
    deleted_clones = c.rowcount
    print(f"  Deleted {deleted_clones} clones")

    # Delete sources
    c.execute('DELETE FROM hero_build_sources')
    deleted_sources = c.rowcount
    print(f"  Deleted {deleted_sources} sources")

    # Delete any pending/processing tasks
    c.execute("DELETE FROM steam_tasks WHERE type = 'BUILD_PUBLISH' AND status IN ('PENDING', 'PROCESSING')")
    deleted_tasks = c.rowcount
    print(f"  Deleted {deleted_tasks} pending BUILD_PUBLISH tasks")

    conn.commit()

    # Step 4: Reset the last sync timestamp to trigger immediate resync (if table exists)
    try:
        c.execute("DELETE FROM key_value WHERE namespace = 'hero_build_mirror'")
        conn.commit()
    except sqlite3.OperationalError:
        # Table doesn't exist, that's fine
        pass

    print("\nDatabase cleaned!")
    print(f"\nNext steps:")
    print(f"  1. The BuildMirror cog will automatically fetch new builds within 4 hours")
    print(f"  2. Or trigger manual sync via Discord command if available")
    print(f"  3. The system will now respect the 3-builds-per-hero cap")

    # Step 5: Verify cleanup
    sources_after = c.execute('SELECT COUNT(*) FROM hero_build_sources').fetchone()[0]
    clones_after = c.execute('SELECT COUNT(*) FROM hero_build_clones').fetchone()[0]

    print(f"\nFinal verification:")
    print(f"  Sources: {sources_after} (should be 0)")
    print(f"  Clones: {clones_after} (should be 0)")

    conn.close()

    if sources_after == 0 and clones_after == 0:
        print("\nReset completed successfully!")
    else:
        print("\nWarning: Some builds remain. Check database!")

if __name__ == '__main__':
    main()
