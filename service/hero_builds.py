"""Utilities for mirroring hero builds from the public Deadlock API.

Key responsibilities:
- Fetch hero builds for a given author from https://api.deadlock-api.com/v1/builds
- Cache the normalized payloads in the shared SQLite database
- Maintain a clone queue for re-uploading in another language (e.g. German)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import aiohttp

from service import db

log = logging.getLogger(__name__)

DEADLOCK_BUILD_API = "https://api.deadlock-api.com/v1/builds"
STEAM_ID64_OFFSET = 76561197960265728
DEFAULT_TARGET_LANGUAGE = 1  # German
TARGET_NAME = "Deutsche Deadlock Community x EarlySalty"
DISCORD_INVITE = "discord.gg/z5TfVHuQq2"
TWITCH_URL = "www.twitch.tv/earlysalty (deutsch)"


def steam64_to_account_id(raw: int | str | None) -> Optional[int]:
    """Convert a SteamID64 to the numeric account_id used by the builds API."""
    if raw is None:
        return None
    try:
        sid64 = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return sid64 - STEAM_ID64_OFFSET if sid64 >= STEAM_ID64_OFFSET else sid64


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_dump(payload: Any) -> str:
    return json.dumps(payload or {}, ensure_ascii=True, separators=(",", ":"))


@dataclass(slots=True)
class HeroBuildSource:
    hero_build_id: int
    origin_build_id: Optional[int]
    author_account_id: int
    hero_id: int
    language: int
    version: int
    name: str
    description: Optional[str]
    tags: List[int]
    details: Dict[str, Any]
    publish_ts: Optional[int]
    last_updated_ts: Optional[int]

    @classmethod
    def from_api(cls, raw: Dict[str, Any]) -> Optional["HeroBuildSource"]:
        hero = raw.get("hero_build") or {}
        try:
            hero_build_id = int(hero["hero_build_id"])
            author_account_id = int(hero["author_account_id"])
            hero_id = int(hero["hero_id"])
            language = int(hero["language"])
            version = int(hero["version"])
            name = str(hero.get("name", "")).strip()
        except (KeyError, TypeError, ValueError):
            return None

        description = hero.get("description")
        if description is not None:
            description = str(description).strip()
        tags = hero.get("tags") or []
        details = hero.get("details") or {}

        return cls(
            hero_build_id=hero_build_id,
            origin_build_id=_safe_int(hero.get("origin_build_id")),
            author_account_id=author_account_id,
            hero_id=hero_id,
            language=language,
            version=version,
            name=name,
            description=description,
            tags=[_safe_int(t) or 0 for t in tags] if isinstance(tags, list) else [],
            details=details if isinstance(details, dict) else {},
            publish_ts=_safe_int(hero.get("publish_timestamp")),
            last_updated_ts=_safe_int(hero.get("last_updated_timestamp")),
        )

    def to_db_tuple(self, fetched_at: int) -> Tuple[Any, ...]:
        return (
            self.hero_build_id,
            self.origin_build_id,
            self.author_account_id,
            self.hero_id,
            self.language,
            self.version,
            self.name,
            self.description,
            _json_dump(self.tags),
            _json_dump(self.details),
            self.publish_ts,
            self.last_updated_ts,
            fetched_at,
            fetched_at,
        )


async def fetch_builds_for_author(
    author_account_id: int,
    *,
    only_latest: bool = True,
    per_page: int = 100,
    language: Optional[int] = None,
    session: Optional[aiohttp.ClientSession] = None,
    timeout_seconds: float = 12.0,
) -> List[HeroBuildSource]:
    """Fetch builds for a specific author (account_id) with pagination."""
    close_session = False
    if session is None:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        session = aiohttp.ClientSession(timeout=timeout)
        close_session = True

    try:
        start = 0
        builds: List[HeroBuildSource] = []
        while True:
            params = {
                "author_id": author_account_id,
                "limit": per_page,
                "start": start,
                "only_latest": "true" if only_latest else "false",
            }
            if language is not None:
                params["language"] = language

            async with session.get(DEADLOCK_BUILD_API, params=params) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"Build API returned {resp.status}: {text[:200]}")
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"Invalid JSON from build API: {exc}") from exc

            if not isinstance(payload, list):
                raise RuntimeError("Build API returned non-list payload")

            parsed: List[HeroBuildSource] = []
            for entry in payload:
                build = HeroBuildSource.from_api(entry if isinstance(entry, dict) else {})
                if build:
                    parsed.append(build)
            builds.extend(parsed)

            if len(payload) < per_page:
                break
            start += per_page
        return builds
    finally:
        if close_session:
            await session.close()


def upsert_sources(builds: Iterable[HeroBuildSource]) -> int:
    """Store/refresh builds in the cache. Returns the number of upserts."""
    now = int(time.time())
    rows = [b.to_db_tuple(now) for b in builds]
    if not rows:
        return 0

    sql = """
        INSERT INTO hero_build_sources(
          hero_build_id, origin_build_id, author_account_id, hero_id, language,
          version, name, description, tags_json, details_json, publish_ts,
          last_updated_ts, fetched_at, last_seen_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(hero_build_id) DO UPDATE SET
          origin_build_id=excluded.origin_build_id,
          author_account_id=excluded.author_account_id,
          hero_id=excluded.hero_id,
          language=excluded.language,
          version=excluded.version,
          name=excluded.name,
          description=excluded.description,
          tags_json=excluded.tags_json,
          details_json=excluded.details_json,
          publish_ts=excluded.publish_ts,
          last_updated_ts=excluded.last_updated_ts,
          last_seen_at=strftime('%s','now')
    """
    with db.get_conn() as conn:
        conn.executemany(sql, rows)
    return len(rows)


def latest_per_hero(builds: Iterable[HeroBuildSource]) -> Dict[int, HeroBuildSource]:
    """Pick the newest build per hero using last_updated_ts->version->id priority."""
    latest: Dict[int, HeroBuildSource] = {}
    for build in builds:
        current = latest.get(build.hero_id)
        if current is None:
            latest[build.hero_id] = build
            continue

        def score(b: HeroBuildSource) -> Tuple[int, int, int]:
            return (b.last_updated_ts or 0, b.version, b.hero_build_id)

        if score(build) > score(current):
            latest[build.hero_id] = build
    return latest


def top_builds_per_hero_from_db(
    max_per_hero: int = 3,
    target_language: int = DEFAULT_TARGET_LANGUAGE,
    min_days_old: int = 0,
    max_days_old: int = 30,
) -> List[HeroBuildSource]:
    """
    Fetch the top N builds per hero from the database, respecting author priorities.

    This queries hero_build_sources joined with watched_build_authors to get
    priority information, then selects up to max_per_hero builds for each hero,
    ordered by:
    1. Author priority (DESC)
    2. Publish timestamp (DESC)

    Args:
        max_per_hero: Maximum number of builds to return per hero (default: 3)
        target_language: Target language for filtering (default: German/1)
        min_days_old: Minimum age in days (default: 0 = no minimum)
        max_days_old: Maximum age in days (default: 30)

    Returns:
        List of HeroBuildSource objects, up to max_per_hero per hero_id
    """
    now = int(time.time())
    min_ts = now - (max_days_old * 24 * 60 * 60)
    max_ts = now - (min_days_old * 24 * 60 * 60) if min_days_old > 0 else now

    # Query with priority join and ranking
    sql = """
        SELECT
            hbs.hero_build_id,
            hbs.origin_build_id,
            hbs.author_account_id,
            hbs.hero_id,
            hbs.language,
            hbs.version,
            hbs.name,
            hbs.description,
            hbs.tags_json,
            hbs.details_json,
            hbs.publish_ts,
            hbs.last_updated_ts,
            COALESCE(wba.priority, 0) as priority,
            ROW_NUMBER() OVER (
                PARTITION BY hbs.hero_id
                ORDER BY COALESCE(wba.priority, 0) DESC, hbs.publish_ts DESC
            ) as rank
        FROM hero_build_sources hbs
        LEFT JOIN watched_build_authors wba ON hbs.author_account_id = wba.author_account_id
        WHERE hbs.language = 0  -- English builds only
          AND hbs.publish_ts >= ?
          AND hbs.publish_ts <= ?
    """

    with db.get_conn() as conn:
        cursor = conn.execute(sql, (min_ts, max_ts))
        rows = cursor.fetchall()

    # Filter to max_per_hero and convert to HeroBuildSource objects
    builds: List[HeroBuildSource] = []
    for row in rows:
        rank = row["rank"]
        if rank > max_per_hero:
            continue

        try:
            # Parse tags and details from JSON
            tags_json = row["tags_json"]
            tags = json.loads(tags_json) if tags_json else []

            details_json = row["details_json"]
            details = json.loads(details_json) if details_json else {}

            build = HeroBuildSource(
                hero_build_id=row["hero_build_id"],
                origin_build_id=row["origin_build_id"],
                author_account_id=row["author_account_id"],
                hero_id=row["hero_id"],
                language=row["language"],
                version=row["version"],
                name=row["name"],
                description=row["description"],
                tags=tags,
                details=details,
                publish_ts=row["publish_ts"],
                last_updated_ts=row["last_updated_ts"],
            )
            builds.append(build)
        except Exception as exc:
            log.warning("Failed to parse build %s: %s", row.get("hero_build_id"), exc)

    return builds


def queue_clone(
    build: HeroBuildSource,
    target_language: int = DEFAULT_TARGET_LANGUAGE,
    *,
    allow_retry_failed: bool = True,
) -> str:
    """
    Enqueue a build for cloning. Returns one of: queued|exists|requeued.
    """
    now = int(time.time())
    target_name, target_description = build_clone_metadata(build, target_language)
    with db.get_conn() as conn:
        row = conn.execute(
            """
            SELECT status FROM hero_build_clones
             WHERE origin_hero_build_id=? AND target_language=?
            """,
            (build.hero_build_id, target_language),
        ).fetchone()

        if row:
            status = str(row["status"]) if row["status"] is not None else ""
            if allow_retry_failed and status.lower() in {"failed", "error"}:
                conn.execute(
                    """
                    UPDATE hero_build_clones
                       SET status='pending',
                           status_info=NULL,
                           target_name=?,
                           target_description=?,
                           updated_at=?,
                           last_attempt_at=NULL
                     WHERE origin_hero_build_id=? AND target_language=?
                    """,
                    (
                        target_name,
                        target_description,
                        now,
                        build.hero_build_id,
                        target_language,
                    ),
                )
                return "requeued"
            return "exists"

        conn.execute(
            """
            INSERT INTO hero_build_clones(
              origin_hero_build_id, origin_build_id, hero_id, author_account_id,
              source_language, source_version, source_last_updated_ts,
              target_language, target_name, target_description,
              status, status_info, uploaded_build_id,
              uploaded_version, created_at, updated_at, last_attempt_at, attempts
            ) VALUES(?,?,?,?,?,?,?,?, ?, ?, 'pending', NULL, NULL, NULL, ?, ?, NULL, 0)
            """,
            (
                build.hero_build_id,
                build.origin_build_id,
                build.hero_id,
                build.author_account_id,
                build.language,
                build.version,
                build.last_updated_ts,
                target_language,
                target_name,
                target_description,
                now,
                now,
            ),
        )
    return "queued"


def count_sources(author_ids: Optional[Sequence[int]] = None) -> int:
    sql = "SELECT COUNT(*) AS c FROM hero_build_sources"
    params: Tuple[Any, ...] = ()
    if author_ids:
        placeholders = ",".join(["?"] * len(author_ids))
        sql += f" WHERE author_account_id IN ({placeholders})"
        params = tuple(int(a) for a in author_ids)
    row = db.query_one(sql, params)
    return int(row[0]) if row else 0


def clone_stats(target_language: Optional[int] = None) -> Dict[str, int]:
    sql = "SELECT status, COUNT(*) AS c FROM hero_build_clones"
    params: Tuple[Any, ...] = ()
    if target_language is not None:
        sql += " WHERE target_language=?"
        params = (target_language,)
    sql += " GROUP BY status"

    stats: Dict[str, int] = {}
    for row in db.query_all(sql, params):
        status = str(row["status"]) if row["status"] is not None else "unknown"
        stats[status] = int(row["c"])
    return stats


def select_pending_clones(target_language: Optional[int] = None, limit: int = 20):
    sql = """
        SELECT origin_hero_build_id, target_language, hero_id, target_name, target_description
          FROM hero_build_clones
         WHERE status='pending'
    """
    params: List[Any] = []
    if target_language is not None:
        sql += " AND target_language=?"
        params.append(target_language)
    sql += " ORDER BY created_at ASC LIMIT ?"
    params.append(int(limit))
    return db.query_all(sql, params)


async def periodic_sync(
    author_account_ids: Sequence[int],
    *,
    target_language: int = DEFAULT_TARGET_LANGUAGE,
    interval_seconds: float = 4 * 60 * 60,
    only_latest: bool = True,
    session: Optional[aiohttp.ClientSession] = None,
) -> None:
    """
    Helper loop for standalone use. Fetches builds, stores them, queues clones
    and sleeps for `interval_seconds`.
    """
    while True:
        try:
            for author_id in author_account_ids:
                builds = await fetch_builds_for_author(
                    author_id,
                    only_latest=only_latest,
                    session=session,
                )
                upsert_sources(builds)
            for build in latest_per_hero(builds).values():
                queue_clone(build, target_language=target_language)
        except Exception:
            log.exception("Build mirror sync failed")
        await asyncio.sleep(max(60.0, interval_seconds))


def build_clone_metadata(build: HeroBuildSource, target_language: int = DEFAULT_TARGET_LANGUAGE) -> tuple[str, str]:
    """
    Build the German-facing name/description for the cloned build.
    - Name is fixed and trimmed to 50 chars
    - Description includes Discord/Twitch info and original attribution
    """
    name = TARGET_NAME[:50]

    desc_parts = []
    if build.description:
        desc_parts.append(str(build.description).strip())
    desc_parts.append(TWITCH_URL)
    desc_parts.append(f"Deutsche Deadlock Community: {DISCORD_INVITE}")
    desc_parts.append("In Discord auf das + druecken -> Server hinzufuegen -> Code: z5TfVHuQq2")
    desc_parts.append(f"Original Build ID: {build.hero_build_id}")
    desc_parts.append(f"Original Build Autor: {build.author_account_id}")
    description = "\n".join(desc_parts)

    return name, description
