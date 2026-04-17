"""Public activity statistics server (port 8768).

Shows aggregated, anonymous statistics about when ranks are active,
which lanes they prefer, and heuristics for rank estimation.

No personal user data is exposed - only aggregated statistics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web

from service import db

log = logging.getLogger(__name__)

PUBLIC_STATS_PORT = int(os.getenv("PUBLIC_STATS_PORT", "8768"))
PUBLIC_STATS_HOST = os.getenv("PUBLIC_STATS_HOST", "0.0.0.0")

_HTML_PATH = Path(__file__).resolve().parent / "static" / "activity_stats.html"

RANK_ORDER = [
    "initiate",
    "seeker",
    "alchemist",
    "arcanist",
    "ritualist",
    "emissary",
    "archon",
    "oracle",
    "phantom",
    "ascendant",
    "eternus",
]

RANK_SHORT = {
    "initiate": "Init",
    "seeker": "Seek",
    "alchemist": "Alc",
    "arcanist": "Arc",
    "ritualist": "Rit",
    "emissary": "Emi",
    "archon": "Arch",
    "oracle": "Ora",
    "phantom": "Pha",
    "ascendant": "Asc",
    "eternus": "Ete",
}

RANK_COLORS = {
    "initiate": "#cb643b",
    "seeker": "#96c86f",
    "alchemist": "#0e5d8b",
    "arcanist": "#3c591e",
    "ritualist": "#b15926",
    "emissary": "#b73f3c",
    "archon": "#705691",
    "oracle": "#a74905",
    "phantom": "#8d7b69",
    "ascendant": "#c59951",
    "eternus": "#1eb6a7",
}


def _load_html() -> str:
    try:
        return _HTML_PATH.read_text(encoding="utf-8")
    except Exception as exc:
        log.error("activity_stats.html nicht lesbar: %s", exc)
        raise


def _estimate_rank_from_co_players(user_id: int, co_players: list[int]) -> str | None:
    """Estimate rank for a player with no rank based on their co-players' ranks."""
    if len(co_players) < 3:
        return None

    rank_scores = []
    for cp_id in co_players:
        row = db.query_one(
            """
            SELECT deadlock_rank_name, deadlock_subrank
            FROM steam_links
            WHERE user_id = ? AND verified = 1
            ORDER BY primary_account DESC, deadlock_rank_updated_at DESC
            LIMIT 1
            """,
            (cp_id,),
        )
        if row and row["deadlock_rank_name"]:
            score = _rank_to_score(row["deadlock_rank_name"], row["deadlock_subrank"] or 3)
            rank_scores.append(score)

    if not rank_scores:
        return None

    avg_score = sum(rank_scores) / len(rank_scores)
    return _score_to_bucket(avg_score)


def _rank_to_score(rank_name: str, subrank: int) -> float:
    try:
        base = RANK_ORDER.index(rank_name.lower()) + 1
    except ValueError:
        base = 0
    return base * 6 + max(1, min(6, subrank))


def _score_to_bucket(score: float) -> str:
    if score <= 18:
        return "low"
    elif score <= 42:
        return "mid"
    return "high"


def _detect_lane_from_name(name: str | None) -> str | None:
    if not name:
        return None
    n = name.lower()
    if "mid" in n:
        return "mid"
    if "off" in n or "offlane" in n or "off-lane" in n:
        return "off"
    if "safe" in n or "carry" in n:
        return "safe"
    if "jungle" in n or "jg" in n:
        return "jungle"
    if "new" in n or "neue" in n or "🆕" in n:
        return "new_player"
    return None


# ── API Handlers ────────────────────────────────────────────────────────────────


async def handle_index(request: web.Request) -> web.Response:
    try:
        html = _load_html()
    except Exception:
        return web.Response(text="Seite nicht verfügbar.", status=503)
    return web.Response(text=html, content_type="text/html")


async def handle_activity_heatmap(request: web.Request) -> web.Response:
    """Returns 7x24 matrix of avg active users by rank and hour/day."""
    now = datetime.now()
    cutoff = now - timedelta(days=14)

    # Get sessions from last 2 weeks
    rows = db.query_all(
        """
        SELECT started_at, duration_seconds, channel_name, co_player_ids, user_id
        FROM voice_session_log
        WHERE started_at >= ?
        ORDER BY started_at
        """,
        (cutoff.isoformat(),),
    )

    # Build heatmap data: day (0=Mon..6=Sun) x hour (0..23) x rank
    heatmap: dict[str, dict[int, dict[int, list[int]]]] = {}
    for rank in RANK_ORDER:
        heatmap[rank] = {d: {h: [] for h in range(24)} for d in range(7)}

    # Users without rank get assigned via co-player heuristic
    no_rank_users: dict[int, str] = {}  # user_id -> estimated bucket

    for row in rows:
        try:
            started = datetime.fromisoformat(row["started_at"])
        except Exception:
            continue

        day = (started.weekday()) % 7
        hour = started.hour
        user_id = row["user_id"]
        co_players_raw = row["co_player_ids"] or "[]"
        channel_name = row["channel_name"]

        # Determine rank for this user
        user_rank = _get_user_rank(user_id)
        if not user_rank:
            # Try heuristic via co-players
            if user_id in no_rank_users:
                user_rank = no_rank_users[user_id]
            else:
                try:
                    co_ids = json.loads(co_players_raw)
                    if isinstance(co_ids, list):
                        estimated = _estimate_rank_from_co_players(user_id, co_ids)
                        user_rank = estimated
                        if estimated:
                            no_rank_users[user_id] = estimated
                except Exception:
                    user_rank = None

        if not user_rank:
            continue

        # Add to heatmap buckets
        if user_rank in heatmap:
            heatmap[user_rank][day][hour].append(row["user_id"])

    # Convert to avg per hour/day
    result = {}
    for rank in RANK_ORDER:
        result[rank] = []
        for day in range(7):
            for hour in range(24):
                count = len(heatmap[rank][day][hour])
                result[rank].append({"day": day, "hour": hour, "count": count})

    return web.json_response(
        {"heatmap": result, "rank_order": RANK_ORDER, "generated_at": now.isoformat()}
    )


async def handle_rank_distribution(request: web.Request) -> web.Response:
    """Returns current rank distribution and trend over time."""
    now = datetime.now()
    cutoff_30d = now - timedelta(days=30)

    # Current distribution from steam_links
    rank_counts: dict[str, int] = {r: 0 for r in RANK_ORDER}
    rows = db.query_all(
        """
        SELECT deadlock_rank_name, COUNT(*) as cnt
        FROM steam_links
        WHERE verified = 1 AND deadlock_rank_name IS NOT NULL
        GROUP BY deadlock_rank_name
        """
    )
    for row in rows:
        name = (row["deadlock_rank_name"] or "").lower()
        if name in rank_counts:
            rank_counts[name] = row["cnt"]

    # Activity by rank over last 30 days (weekly buckets)
    weekly = []
    for week in range(4):
        week_start = now - timedelta(weeks=week + 1)
        week_end = now - timedelta(weeks=week)
        week_data = {r: 0 for r in RANK_ORDER}
        week_rows = db.query_all(
            """
            SELECT user_id, started_at
            FROM voice_session_log
            WHERE started_at >= ? AND started_at < ?
            """,
            (week_start.isoformat(), week_end.isoformat()),
        )
        seen_users: set[int] = set()
        for row in week_rows:
            uid = row["user_id"]
            if uid in seen_users:
                continue
            rank = _get_user_rank(uid)
            if rank and rank in week_data:
                week_data[rank] += 1
                seen_users.add(uid)
        weekly.append({"week": week + 1, "data": week_data})

    weekly.reverse()  # oldest first

    return web.json_response(
        {
            "distribution": rank_counts,
            "rank_order": RANK_ORDER,
            "weekly_trend": weekly,
            "generated_at": now.isoformat(),
        }
    )


async def handle_lane_preferences(request: web.Request) -> web.Response:
    """Returns which lanes each rank prefers."""
    now = datetime.now()
    cutoff = now - timedelta(days=30)

    lane_rank_data: dict[str, dict[str, int]] = {}
    for rank in RANK_ORDER:
        lane_rank_data[rank] = {
            "mid": 0,
            "off": 0,
            "safe": 0,
            "jungle": 0,
            "new_player": 0,
            "unknown": 0,
        }

    rows = db.query_all(
        """
        SELECT user_id, channel_name, co_player_ids
        FROM voice_session_log
        WHERE started_at >= ?
        """,
        (cutoff.isoformat(),),
    )

    seen_users_per_lane: dict[str, set[int]] = {
        "mid": set(),
        "off": set(),
        "safe": set(),
        "jungle": set(),
        "new_player": set(),
        "unknown": set(),
    }

    for row in rows:
        lane = _detect_lane_from_name(row["channel_name"]) or "unknown"
        user_id = row["user_id"]

        # Only count each user once per lane category
        if user_id in seen_users_per_lane[lane]:
            continue

        rank = _get_user_rank(user_id)
        if not rank:
            # heuristic
            try:
                co_ids = json.loads(row["co_player_ids"] or "[]")
                rank = _estimate_rank_from_co_players(
                    user_id, co_ids if isinstance(co_ids, list) else []
                )
            except Exception:
                rank = None

        if rank and rank in lane_rank_data:
            lane_rank_data[rank][lane] += 1
            seen_users_per_lane[lane].add(user_id)

    return web.json_response(
        {
            "preferences": lane_rank_data,
            "rank_order": RANK_ORDER,
            "lanes": ["mid", "off", "safe", "jungle", "new_player", "unknown"],
            "generated_at": now.isoformat(),
        }
    )


async def handle_new_player_windows(request: web.Request) -> web.Response:
    """Returns time windows when new players are most active."""
    now = datetime.now()
    cutoff = now - timedelta(days=30)

    # New player lanes detection (channel names with "new", "neue", "🆕")
    hour_new_player_count: dict[int, list[int]] = {h: [] for h in range(24)}
    day_hour_counts: dict[int, dict[int, list[int]]] = {
        d: {h: [] for h in range(24)} for d in range(7)
    }

    rows = db.query_all(
        """
        SELECT started_at, channel_name, user_id
        FROM voice_session_log
        WHERE started_at >= ?
        """,
        (cutoff.isoformat(),),
    )

    for row in rows:
        try:
            started = datetime.fromisoformat(row["started_at"])
        except Exception:
            continue

        lane = _detect_lane_from_name(row["channel_name"])
        if lane == "new_player":
            day = started.weekday() % 7
            hour = started.hour
            day_hour_counts[day][hour].append(row["user_id"])
            hour_new_player_count[hour].append(row["user_id"])

    # Find peak hours and days
    peak_hours = sorted(hour_new_player_count.items(), key=lambda x: len(x[1]), reverse=True)[:5]
    peak_hours = [{"hour": h, "count": len(users)} for h, users in peak_hours]

    peak_days = []
    for day in range(7):
        total = sum(len(day_hour_counts[day][h]) for h in range(24))
        peak_days.append({"day": day, "count": total})
    peak_days.sort(key=lambda x: x["count"], reverse=True)

    return web.json_response(
        {
            "peak_hours": peak_hours,
            "peak_days": peak_days[:3],
            "day_hour_matrix": {
                str(d): {str(h): len(day_hour_counts[d][h]) for h in range(24)} for d in range(7)
            },
            "generated_at": now.isoformat(),
        }
    )


async def handle_timeline(request: web.Request) -> web.Response:
    """Returns streaming-style timeline of when ranks are most active.

    Query param: metric - 'players' (default) or 'hours'
    """
    metric = request.query.get("metric", "players")
    now = datetime.now()
    cutoff = now - timedelta(days=7)

    rows = db.query_all(
        """
        SELECT started_at, user_id, co_player_ids, channel_name, duration_seconds
        FROM voice_session_log
        WHERE started_at >= ?
        ORDER BY started_at
        """,
        (cutoff.isoformat(),),
    )

    # Build hourly aggregates
    hourly: dict[int, dict[str, int]] = {h: {r: 0 for r in RANK_ORDER} for h in range(24)}
    hourly_hours: dict[int, dict[str, float]] = {h: {r: 0.0 for r in RANK_ORDER} for h in range(24)}
    for row in rows:
        try:
            started = datetime.fromisoformat(row["started_at"])
        except Exception:
            continue

        hour = started.hour
        rank = _get_user_rank(row["user_id"])
        if not rank:
            try:
                co_ids = json.loads(row["co_player_ids"] or "[]")
                rank = _estimate_rank_from_co_players(
                    row["user_id"], co_ids if isinstance(co_ids, list) else []
                )
            except Exception:
                rank = None
        if rank and rank in hourly[hour]:
            hourly[hour][rank] += 1
            duration = row.get("duration_seconds") or 0
            hourly_hours[hour][rank] += float(duration) / 3600  # convert to hours

    timeline = []
    for hour in range(24):
        entry = {"hour": hour, "ranks": {}}
        for rank in RANK_ORDER:
            if metric == "hours":
                entry["ranks"][rank] = round(hourly_hours[hour][rank], 2)
            else:
                entry["ranks"][rank] = hourly[hour][rank]
        timeline.append(entry)

    # Find peak times per rank
    peak_times = {}
    for rank in RANK_ORDER:
        if metric == "hours":
            peaks = sorted(
                enumerate(hourly_hours[h][rank] for h in range(24)),
                key=lambda x: x[1],
                reverse=True,
            )
        else:
            peaks = sorted(
                enumerate(hourly[h][rank] for h in range(24)), key=lambda x: x[1], reverse=True
            )
        peak_times[rank] = [
            {"hour": h, "count": round(c, 2) if metric == "hours" else c}
            for h, c in peaks[:3]
            if c > 0
        ]

    return web.json_response(
        {
            "timeline": timeline,
            "peak_times": peak_times,
            "rank_order": RANK_ORDER,
            "metric": metric,
            "generated_at": now.isoformat(),
        }
    )


async def handle_voice_history(request: web.Request) -> web.Response:
    """Returns aggregated voice activity stats - no user data exposed.

    Returns:
    - daily: daily totals for the last N days
    - hourly: hourly breakdown by mode (hour/day/week/month)
    - summary: total sessions, total time, unique users, avg session length
    """
    range_raw = request.query.get("range")
    mode_raw = request.query.get("mode") or "hour"
    try:
        days = int(range_raw) if range_raw else 14
        days = min(max(days, 1), 90)
    except ValueError:
        days = 14

    mode = mode_raw.strip().lower()
    if mode not in {"hour", "day", "week", "month"}:
        mode = "hour"

    cutoff = f"-{days} day"
    now = datetime.now()

    try:
        # Daily summary (last N days)
        daily_rows = db.query_all(
            """
            SELECT date(started_at) AS day,
                   SUM(duration_seconds) AS total_seconds,
                   COUNT(*) AS sessions,
                   COUNT(DISTINCT user_id) AS unique_users
            FROM voice_session_log
            WHERE started_at >= datetime('now', ?)
            GROUP BY date(started_at)
            ORDER BY day DESC
            """,
            (cutoff,),
        )

        # Overall summary
        summary_rows = db.query_all(
            """
            SELECT COUNT(*) AS total_sessions,
                   COALESCE(SUM(duration_seconds), 0) AS total_seconds,
                   COUNT(DISTINCT user_id) AS total_users
            FROM voice_session_log
            WHERE started_at >= datetime('now', ?)
            """,
            (cutoff,),
        )
        summary = (
            summary_rows[0]
            if summary_rows
            else {"total_sessions": 0, "total_seconds": 0, "total_users": 0}
        )

        # Hourly/weekly breakdown
        hourly_rows = db.query_all(
            """
            WITH grouped AS (
                SELECT
                    CASE
                        WHEN ? = 'hour' THEN strftime('%H', started_at)
                        WHEN ? = 'day' THEN strftime('%w', started_at)
                        WHEN ? = 'week' THEN strftime('%Y-%W', started_at)
                        ELSE strftime('%Y-%m', started_at)
                    END AS bucket,
                    duration_seconds,
                    COALESCE(peak_users, 0) AS peak_users
                FROM voice_session_log
                WHERE started_at >= datetime('now', ?)
            )
            SELECT bucket,
                   SUM(duration_seconds) AS total_seconds,
                   COUNT(*) AS sessions,
                   SUM(peak_users) AS sum_peak
            FROM grouped
            GROUP BY bucket
            ORDER BY bucket
            """,
            (mode, mode, mode, cutoff),
        )

    except Exception as exc:
        log.exception("Failed to load voice history: %s", exc)
        return web.json_response({"error": "Voice history unavailable"}, status=500)

    # Format hourly data
    hourly_data = []
    for row in hourly_rows:
        total_s = int(row["total_seconds"] or 0)
        sessions = int(row["sessions"] or 0)
        hourly_data.append(
            {
                "bucket": row["bucket"],
                "total_seconds": total_s,
                "sessions": sessions,
                "avg_session_minutes": round(total_s / sessions / 60, 1) if sessions > 0 else 0,
            }
        )

    # Format daily data
    daily_data = []
    for row in daily_rows:
        total_s = int(row["total_seconds"] or 0)
        sessions = int(row["sessions"] or 0)
        daily_data.append(
            {
                "day": row["day"],
                "total_seconds": total_s,
                "sessions": sessions,
                "unique_users": int(row["unique_users"] or 0),
            }
        )

    total_s = int(summary["total_seconds"] or 0)
    total_sessions = int(summary["total_sessions"] or 0)
    total_users = int(summary["total_users"] or 0)

    return web.json_response(
        {
            "summary": {
                "total_sessions": total_sessions,
                "total_seconds": total_s,
                "total_users": total_users,
                "avg_session_minutes": round(total_s / total_sessions / 60, 1)
                if total_sessions > 0
                else 0,
                "days": days,
            },
            "daily": daily_data,
            "hourly": hourly_data,
            "mode": mode,
            "generated_at": now.isoformat(),
        }
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "ts": int(datetime.now().timestamp())})


async def handle_rank_colors(request: web.Request) -> web.Response:
    """Returns rank colors for the UI."""
    return web.json_response({"colors": RANK_COLORS, "generated_at": datetime.now().isoformat()})


async def handle_best_times(request: web.Request) -> web.Response:
    """Returns the best time windows for a specific rank.

    Query param: rank (e.g. 'initiate', 'seeker', etc.)
    Returns top 3 peak hours with day distribution.
    """
    rank = request.query.get("rank", "")
    if rank not in RANK_ORDER:
        return web.json_response({"error": "Invalid rank"}, status=400)

    now = datetime.now()
    cutoff = now - timedelta(days=7)

    # Get timeline data for the rank
    rows = db.query_all(
        """
        SELECT started_at, user_id, co_player_ids
        FROM voice_session_log
        WHERE started_at >= ?
        ORDER BY started_at
        """,
        (cutoff.isoformat(),),
    )

    # Build hourly counts for this rank
    hourly_counts: dict[int, int] = {h: 0 for h in range(24)}
    day_hour_counts: dict[int, dict[int, list[int]]] = {
        d: {h: [] for h in range(24)} for d in range(7)
    }

    for row in rows:
        try:
            started = datetime.fromisoformat(row["started_at"])
        except Exception:
            continue

        hour = started.hour
        day = started.weekday() % 7
        user_rank = _get_user_rank(row["user_id"])
        if not user_rank:
            try:
                co_ids = json.loads(row["co_player_ids"] or "[]")
                user_rank = _estimate_rank_from_co_players(
                    row["user_id"], co_ids if isinstance(co_ids, list) else []
                )
            except Exception:
                user_rank = None

        if user_rank == rank:
            hourly_counts[hour] += 1
            day_hour_counts[day][hour].append(row["user_id"])

    # Find top 3 peak hours
    peaks = sorted(hourly_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    peak_hours = [{"hour": h, "count": c} for h, c in peaks if c > 0]

    # Day distribution for peak hours
    day_distribution = []
    for d in range(7):
        total = sum(len(day_hour_counts[d][h]) for h in range(24))
        day_distribution.append({"day": d, "count": total})

    return web.json_response(
        {
            "rank": rank,
            "peak_hours": peak_hours,
            "day_distribution": day_distribution,
            "generated_at": now.isoformat(),
        }
    )


# ── Server class ──────────────────────────────────────────────────────────────


class PublicStatsServer:
    def __init__(self, *, host: str = PUBLIC_STATS_HOST, port: int = PUBLIC_STATS_PORT) -> None:
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None

        self.app = web.Application(middlewares=[self._security_mw])
        self.app.router.add_get("/", handle_index)
        self.app.router.add_get("/api/activity-heatmap", handle_activity_heatmap)
        self.app.router.add_get("/api/rank-distribution", handle_rank_distribution)
        self.app.router.add_get("/api/lane-preferences", handle_lane_preferences)
        self.app.router.add_get("/api/new-player-windows", handle_new_player_windows)
        self.app.router.add_get("/api/timeline", handle_timeline)
        self.app.router.add_get("/api/rank-colors", handle_rank_colors)
        self.app.router.add_get("/api/best-times", handle_best_times)
        self.app.router.add_get("/api/voice-history", handle_voice_history)
        self.app.router.add_get("/health", handle_health)

        # Serve rank icons
        from pathlib import Path

        icons_path = Path(__file__).resolve().parent / "static" / "rank_icons"
        if icons_path.is_dir():
            self.app.router.add_static("/rank_icons/", icons_path)

    @web.middleware
    async def _security_mw(self, request: web.Request, handler):
        try:
            resp = await handler(request)
        except web.HTTPException as ex:
            resp = ex
        except Exception:
            log.exception("Unhandled error in public stats server")
            resp = web.Response(
                text='{"error":"internal"}', content_type="application/json", status=500
            )

        if isinstance(resp, web.Response):
            resp.headers["Cache-Control"] = "public, max-age=60"
            resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()

        max_retries = 5
        retry_delay = 0.5
        for attempt in range(max_retries):
            try:
                site = web.TCPSite(self._runner, host=self.host, port=self.port)
                await site.start()
                log.info("PublicStatsServer läuft auf %s:%s", self.host, self.port)
                return
            except OSError as e:
                import errno

                is_in_use = e.errno == errno.EADDRINUSE
                if is_in_use and attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                log.exception("PublicStatsServer konnte nicht starten")
                break

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            log.info("PublicStatsServer gestoppt")


# ── Helper ─────────────────────────────────────────────────────────────────────


def _get_user_rank(user_id: int) -> str | None:
    row = db.query_one(
        """
        SELECT deadlock_rank_name
        FROM steam_links
        WHERE user_id = ? AND verified = 1
        ORDER BY primary_account DESC, deadlock_rank_updated_at DESC
        LIMIT 1
        """,
        (user_id,),
    )
    if row and row["deadlock_rank_name"]:
        name = row["deadlock_rank_name"].lower()
        if name in RANK_ORDER:
            return name
    return None
