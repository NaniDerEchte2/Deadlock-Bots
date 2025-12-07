"""
Trigger a manual rebuild of the hero builds system.

This script simulates what BuildMirror.run_sync() does:
1. Fetches all builds from all watched authors
2. Stores them in hero_build_sources
3. Gets top 3 builds per hero (by priority)
4. Queues them for cloning
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path to import service modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from service.hero_builds import (
    fetch_builds_for_author,
    top_builds_per_hero_from_db,
    queue_clone,
    upsert_sources,
)
from service import db

async def main():
    print("="*60)
    print("MANUAL BUILD SYSTEM REBUILD")
    print("="*60)

    # Get all watched authors
    conn = db.connect()
    cursor = conn.execute("""
        SELECT author_account_id, notes, priority
        FROM watched_build_authors
        WHERE is_active = 1
        ORDER BY priority DESC
    """)
    authors = cursor.fetchall()

    print(f"\nFound {len(authors)} active authors:")
    for row in authors:
        priority = row["priority"] if row["priority"] is not None else "N/A"
        print(f"  [{priority:3}] {row['notes']} (ID: {row['author_account_id']})")

    # Step 1: Fetch all builds from all authors
    print(f"\nStep 1: Fetching builds from {len(authors)} authors...")
    total_fetched = 0
    total_stored = 0

    for row in authors:
        author_id = row["author_account_id"]
        author_name = row["notes"]
        print(f"  Fetching from {author_name}...")

        try:
            builds = await fetch_builds_for_author(
                author_id,
                only_latest=True,
                session=None,
            )
            fetched_count = len(builds)
            stored_count = upsert_sources(builds)

            total_fetched += fetched_count
            total_stored += stored_count
            print(f"    Fetched {fetched_count} builds, stored {stored_count}")

        except Exception as exc:
            print(f"    ERROR: {exc}")

    print(f"\nStep 1 complete: {total_fetched} fetched, {total_stored} stored")

    # Step 2: Get top 3 builds per hero from DB
    print(f"\nStep 2: Getting top 3 builds per hero (by priority)...")

    top_builds = top_builds_per_hero_from_db(
        max_per_hero=3,
        target_language=1,  # German
        max_days_old=30,
    )

    # Count builds per hero
    builds_per_hero = {}
    for build in top_builds:
        hero_id = build.hero_id
        builds_per_hero[hero_id] = builds_per_hero.get(hero_id, 0) + 1

    print(f"  Selected {len(top_builds)} builds across {len(builds_per_hero)} heroes")
    print(f"  Builds per hero breakdown:")
    for hero_id in sorted(builds_per_hero.keys()):
        count = builds_per_hero[hero_id]
        print(f"    Hero {hero_id}: {count} builds")

    # Step 3: Queue for cloning
    print(f"\nStep 3: Queueing builds for cloning...")

    queued_count = 0
    exists_count = 0

    for build in top_builds:
        status = queue_clone(build, target_language=1)
        if status in {"queued", "requeued"}:
            queued_count += 1
        elif status == "exists":
            exists_count += 1

    print(f"  Queued: {queued_count}")
    print(f"  Already exists: {exists_count}")

    # Final stats
    print(f"\n{'='*60}")
    print("REBUILD COMPLETE")
    print(f"{'='*60}")
    print(f"  Fetched: {total_fetched} builds")
    print(f"  Stored: {total_stored} builds")
    print(f"  Selected: {len(top_builds)} builds (max 3 per hero)")
    print(f"  Queued: {queued_count} builds for publishing")
    print(f"\nThe BuildPublisher will pick these up within 10 minutes.")

if __name__ == '__main__':
    asyncio.run(main())
