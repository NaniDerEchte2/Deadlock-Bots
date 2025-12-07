from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

import discord
from discord.ext import commands

from service import db
from service.hero_builds import (
    DEFAULT_TARGET_LANGUAGE,
    HeroBuildSource,
    build_clone_metadata,
    fetch_builds_for_author,
    latest_per_hero,
    queue_clone,
    steam64_to_account_id,
    top_builds_per_hero_from_db,
    upsert_sources,
)

log = logging.getLogger(__name__)


def _parse_author_ids(raw: str) -> List[int]:
    ids: List[int] = []
    for chunk in raw.split(","):
        s = chunk.strip()
        if not s:
            continue
        acc = steam64_to_account_id(s)
        if acc is None:
            continue
        try:
            acc_int = int(acc)
        except (TypeError, ValueError):
            continue
        if acc_int > 0:
            ids.append(acc_int)
    return ids


class BuildMirror(commands.Cog):
    """Fetch builds from a source creator and queue German clones."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.enabled = True

        # Load all active authors from watched_build_authors table
        self.author_account_ids = self._load_watched_authors()

        self.target_language = DEFAULT_TARGET_LANGUAGE
        self.interval_seconds = 4 * 60 * 60  # 4h
        self.only_latest = True

        self.export_dir = Path(__file__).resolve().parent.parent / "data" / "hero_builds"

        self._task: Optional[asyncio.Task] = None
        self._sync_lock = asyncio.Lock()
        self.last_sync_ts: Optional[int] = None
        self.last_error: Optional[str] = None

        if self.enabled:
            self._task = bot.loop.create_task(self._loop())
            log.info(
                "Build mirror enabled (authors=%s, target_lang=%s, interval=%ss)",
                ",".join(str(a) for a in self.author_account_ids),
                self.target_language,
                self.interval_seconds,
            )

    def _load_watched_authors(self) -> List[int]:
        """Load all active authors from watched_build_authors table."""
        try:
            conn = db.connect()
            cursor = conn.execute("""
                SELECT author_account_id FROM watched_build_authors
                WHERE is_active = 1
                ORDER BY priority DESC
            """)
            authors = [row["author_account_id"] for row in cursor.fetchall()]
            log.info("Loaded %d active build authors from database", len(authors))
            return authors
        except Exception as exc:
            log.error("Failed to load watched authors: %s", exc)
            # Fallback to hardcoded author
            default_author = steam64_to_account_id("76561198866277376")
            return [default_author] if default_author else []

    def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.run_sync(triggered_by="auto")
            except Exception:
                log.exception("Build mirror sync iteration failed")
            await asyncio.sleep(self.interval_seconds)

    async def run_sync(self, *, triggered_by: str = "manual") -> Dict[str, int]:
        if not self.enabled:
            return {"skipped": 1}
        async with self._sync_lock:
            stats = {"fetched": 0, "stored": 0, "queued": 0}
            try:
                # Step 1: Fetch and store ALL builds from all authors
                for author_id in self.author_account_ids:
                    builds = await fetch_builds_for_author(
                        author_id,
                        only_latest=self.only_latest,
                        session=None,
                    )
                    stats["fetched"] += len(builds)
                    stats["stored"] += upsert_sources(builds)

                # Step 2: Get top 3 builds per hero from DB (respecting priorities)
                top_builds = top_builds_per_hero_from_db(
                    max_per_hero=3,
                    target_language=self.target_language,
                    max_days_old=30,
                )

                # Step 3: Queue these builds for cloning
                for build in top_builds:
                    status = queue_clone(build, target_language=self.target_language)
                    if status in {"queued", "requeued"}:
                        stats["queued"] += 1
                        self._export_build(build)
                self.last_sync_ts = int(time.time())
                self.last_error = None
                db.set_kv("hero_build_mirror", "last_sync_ts", str(self.last_sync_ts))
                db.set_kv("hero_build_mirror", "last_sync_stats", json.dumps(stats))
                db.set_kv("hero_build_mirror", "last_sync_trigger", triggered_by)
            except Exception as exc:
                self.last_error = str(exc)
                db.set_kv("hero_build_mirror", "last_sync_error", self.last_error)
                log.exception("Build mirror sync failed")
            return stats

    def _export_build(self, build: HeroBuildSource) -> Optional[Path]:
        try:
            target_dir = self.export_dir / f"lang{self.target_language}"
            target_dir.mkdir(parents=True, exist_ok=True)
            target_name, target_description = build_clone_metadata(build, self.target_language)
            payload = {
                "hero_build": {
                    "hero_build_id": build.hero_build_id,
                    "origin_build_id": build.origin_build_id,
                    "author_account_id": build.author_account_id,
                    "hero_id": build.hero_id,
                    "language": self.target_language,
                    "version": build.version,
                    "name": target_name,
                    "description": target_description,
                    "tags": build.tags,
                    "details": build.details,
                    "publish_timestamp": build.publish_ts,
                    "last_updated_timestamp": build.last_updated_ts,
                },
                "source_language": build.language,
                "target_language": self.target_language,
                "source_name": build.name,
                "source_description": build.description,
            }
            path = target_dir / f"{build.hero_build_id}.json"
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return path
        except Exception:
            log.exception("Failed to export build %s", build.hero_build_id)
            return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BuildMirror(bot))
