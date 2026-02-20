"""
Coaching Engine - Datenbasierte, personalisierte Coaching-Empfehlungen fuer Twitch Streamer.

Analysiert 9 Bereiche: Effizienz, Titel, Schedule, Dauer, Community, Tags, Retention,
Doppel-Streams und generiert priorisierte Empfehlungen.

Hybrid-ready: aiSummary Feld vorbereitet fuer spaetere LLM-Integration.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

log = logging.getLogger("TwitchStreams.CoachingEngine")


class CoachingEngine:
    """Generates coaching data from the Twitch analytics database."""

    @staticmethod
    def get_coaching_data(
        conn: sqlite3.Connection, streamer: str, days: int
    ) -> Dict[str, Any]:
        streamer_login = streamer.lower()
        since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Check if streamer has any data
        count = conn.execute(
            "SELECT COUNT(*) FROM twitch_stream_sessions WHERE streamer_login = ? AND started_at >= ?",
            (streamer_login, since_date),
        ).fetchone()[0]

        if count == 0:
            return {
                "streamer": streamer,
                "days": days,
                "empty": True,
                "aiSummary": None,
            }

        result: Dict[str, Any] = {
            "streamer": streamer,
            "days": days,
            "empty": False,
        }

        result["efficiency"] = _efficiency(conn, streamer_login, since_date)
        result["titleAnalysis"] = _title_analysis(conn, streamer_login, since_date)
        result["scheduleOptimizer"] = _schedule_optimizer(
            conn, streamer_login, since_date
        )
        result["durationAnalysis"] = _duration_analysis(
            conn, streamer_login, since_date
        )
        result["crossCommunity"] = _cross_community(conn, streamer_login, since_date)
        result["tagOptimization"] = _tag_optimization(conn, streamer_login, since_date)
        result["retentionCoaching"] = _retention_coaching(
            conn, streamer_login, since_date
        )
        result["doubleStreamDetection"] = _double_stream_detection(
            conn, streamer_login, since_date
        )
        result["chatConcentration"] = _chat_concentration(
            conn, streamer_login, since_date
        )
        result["raidNetwork"] = _raid_network(conn, streamer_login, since_date)
        result["peerComparison"] = _peer_comparison(conn, streamer_login, since_date)
        result["competitionDensity"] = _competition_density(
            conn, streamer_login, since_date
        )
        result["recommendations"] = _build_recommendations(result)
        result["aiSummary"] = None  # Hybrid-ready placeholder

        return result


# ---------------------------------------------------------------------------
# 1. Effizienz-Analyse
# ---------------------------------------------------------------------------


def _efficiency(conn: sqlite3.Connection, streamer: str, since: str) -> Dict[str, Any]:
    # Per-streamer efficiency: viewer-hours / stream-hours
    rows = conn.execute(
        """
        SELECT
            s.streamer_login,
            SUM(s.avg_viewers * s.duration_seconds / 3600.0) as viewer_hours,
            SUM(s.duration_seconds / 3600.0) as stream_hours
        FROM twitch_stream_sessions s
        WHERE s.started_at >= ?
          AND s.duration_seconds > 300
        GROUP BY s.streamer_login
        HAVING stream_hours > 1
        ORDER BY (viewer_hours / stream_hours) DESC
        """,
        (since,),
    ).fetchall()

    ratios = []
    your_ratio = 0.0
    your_vh = 0.0
    your_sh = 0.0
    for r in rows:
        ratio = r[1] / r[2] if r[2] > 0 else 0
        ratios.append((r[0], ratio))
        if r[0] == streamer:
            your_ratio = ratio
            your_vh = r[1]
            your_sh = r[2]

    if not ratios:
        return {
            "viewerHoursPerStreamHour": 0,
            "categoryAvg": 0,
            "topPerformers": [],
            "percentile": 0,
            "totalStreamHours": 0,
            "totalViewerHours": 0,
        }

    all_ratios = [r[1] for r in ratios]
    cat_avg = sum(all_ratios) / len(all_ratios) if all_ratios else 0

    # Percentile
    below = sum(1 for r in all_ratios if r < your_ratio)
    percentile = int(below / len(all_ratios) * 100) if all_ratios else 0

    top_performers = [{"streamer": r[0], "ratio": round(r[1], 1)} for r in ratios[:5]]

    return {
        "viewerHoursPerStreamHour": round(your_ratio, 1),
        "categoryAvg": round(cat_avg, 1),
        "topPerformers": top_performers,
        "percentile": percentile,
        "totalStreamHours": round(your_sh, 1),
        "totalViewerHours": round(your_vh, 1),
    }


# ---------------------------------------------------------------------------
# 2. Titel-Coach
# ---------------------------------------------------------------------------


def _title_analysis(
    conn: sqlite3.Connection, streamer: str, since: str
) -> Dict[str, Any]:
    # Your titles
    your_titles = conn.execute(
        """
        SELECT
            s.stream_title,
            AVG(s.avg_viewers) as avg_v,
            MAX(s.peak_viewers) as peak_v,
            AVG(s.unique_chatters) as chatters,
            COUNT(*) as usage_count
        FROM twitch_stream_sessions s
        WHERE s.streamer_login = ? AND s.started_at >= ?
          AND s.stream_title IS NOT NULL AND s.stream_title != ''
        GROUP BY s.stream_title
        ORDER BY avg_v DESC
        """,
        (streamer, since),
    ).fetchall()

    your_list = [
        {
            "title": r[0],
            "avgViewers": round(r[1], 1),
            "peakViewers": int(r[2] or 0),
            "chatters": round(r[3] or 0, 1),
            "usageCount": r[4],
        }
        for r in your_titles
    ]

    # Category top titles (from all streamers)
    cat_titles = conn.execute(
        """
        SELECT
            s.stream_title,
            s.streamer_login,
            AVG(s.avg_viewers) as avg_v
        FROM twitch_stream_sessions s
        WHERE s.started_at >= ?
          AND s.stream_title IS NOT NULL AND s.stream_title != ''
          AND s.streamer_login != ?
        GROUP BY s.stream_title, s.streamer_login
        HAVING COUNT(*) >= 2
        ORDER BY avg_v DESC
        LIMIT 10
        """,
        (since, streamer),
    ).fetchall()

    cat_list = [
        {"title": r[0], "streamer": r[1], "avgViewers": round(r[2], 1)}
        for r in cat_titles
    ]

    # Extract keyword patterns
    your_words = _extract_keywords([r[0] for r in your_titles])
    top_words = _extract_keywords([r[0] for r in cat_titles])

    missing = [w for w in top_words if w not in your_words][:10]
    top_patterns = list(top_words)[:10]

    # --- Title variety comparison ---
    own_total = (
        conn.execute(
            "SELECT COUNT(*) FROM twitch_stream_sessions WHERE streamer_login = ? AND started_at >= ? AND duration_seconds > 300",
            (streamer, since),
        ).fetchone()[0]
        or 0
    )
    own_unique = len(your_titles)
    own_variety_pct = round(own_unique / own_total * 100, 1) if own_total > 0 else 0

    peer_variety = conn.execute(
        """
        SELECT
            s.streamer_login,
            COUNT(DISTINCT s.stream_title) as unique_t,
            COUNT(*) as total_s,
            ROUND(COUNT(DISTINCT s.stream_title) * 100.0 / COUNT(*), 1) as variety
        FROM twitch_stream_sessions s
        WHERE s.started_at >= ? AND s.duration_seconds > 300
          AND s.streamer_login != ?
          AND s.stream_title IS NOT NULL AND s.stream_title != ''
        GROUP BY s.streamer_login
        HAVING total_s >= 3
        ORDER BY variety DESC
        """,
        (since, streamer),
    ).fetchall()

    peer_pcts = [float(r[3]) for r in peer_variety]
    avg_peer_variety = round(sum(peer_pcts) / len(peer_pcts), 1) if peer_pcts else 0
    peer_variety_list = [
        {
            "streamer": r[0],
            "uniqueTitles": r[1],
            "totalSessions": r[2],
            "varietyPct": float(r[3]),
        }
        for r in peer_variety[:10]
    ]

    return {
        "yourTitles": your_list,
        "categoryTopTitles": cat_list,
        "yourMissingPatterns": missing,
        "topPerformerPatterns": top_patterns,
        "varietyPct": own_variety_pct,
        "uniqueTitleCount": own_unique,
        "totalSessionCount": own_total,
        "avgPeerVarietyPct": avg_peer_variety,
        "peerVariety": peer_variety_list,
    }


def _extract_keywords(titles: List[str]) -> List[str]:
    """Extract meaningful keywords from stream titles."""
    stopwords = {
        "der",
        "die",
        "das",
        "und",
        "oder",
        "mit",
        "in",
        "auf",
        "an",
        "von",
        "the",
        "and",
        "or",
        "with",
        "for",
        "to",
        "a",
        "is",
        "on",
        "at",
        "|",
        "-",
        "!",
        "?",
        "#",
        ":",
        "~",
        "//",
        ">>",
    }
    counter: Counter = Counter()
    for title in titles:
        words = re.findall(r"[A-Za-z0-9äöüÄÖÜß]+", title.lower())
        for w in words:
            if len(w) >= 3 and w not in stopwords:
                counter[w] += 1
    return [w for w, _ in counter.most_common(20)]


# ---------------------------------------------------------------------------
# 3. Schedule-Optimizer
# ---------------------------------------------------------------------------


def _schedule_optimizer(
    conn: sqlite3.Connection, streamer: str, since: str
) -> Dict[str, Any]:
    # Category competition heatmap
    competition = conn.execute(
        """
        SELECT
            CAST(strftime('%w', ts_utc) AS INTEGER) as weekday,
            CAST(strftime('%H', ts_utc) AS INTEGER) as hour,
            COUNT(DISTINCT streamer) as competitors,
            AVG(viewer_count) as cat_viewers
        FROM twitch_stats_category
        WHERE ts_utc >= ?
        GROUP BY weekday, hour
        """,
        (since,),
    ).fetchall()

    heatmap = [
        {
            "weekday": int(r[0]),
            "hour": int(r[1]),
            "competitors": int(r[2]),
            "categoryViewers": round(r[3] or 0, 1),
        }
        for r in competition
    ]

    # Your current stream slots
    your_slots = conn.execute(
        """
        SELECT
            CAST(strftime('%w', started_at) AS INTEGER) as weekday,
            CAST(strftime('%H', started_at) AS INTEGER) as hour,
            COUNT(*) as cnt
        FROM twitch_stream_sessions
        WHERE streamer_login = ? AND started_at >= ?
        GROUP BY weekday, hour
        ORDER BY cnt DESC
        """,
        (streamer, since),
    ).fetchall()

    current_slots = [
        {"weekday": int(r[0]), "hour": int(r[1]), "count": r[2]} for r in your_slots
    ]

    # Sweet spots: high category viewers, low competition
    sweet_spots = []
    for cell in heatmap:
        if cell["competitors"] > 0:
            opportunity = cell["categoryViewers"] / cell["competitors"]
        else:
            opportunity = cell["categoryViewers"]
        sweet_spots.append(
            {
                "weekday": cell["weekday"],
                "hour": cell["hour"],
                "categoryViewers": cell["categoryViewers"],
                "competitors": cell["competitors"],
                "opportunityScore": round(opportunity, 1),
            }
        )

    sweet_spots.sort(key=lambda x: x["opportunityScore"], reverse=True)

    return {
        "sweetSpots": sweet_spots[:15],
        "yourCurrentSlots": current_slots,
        "competitionHeatmap": heatmap,
    }


# ---------------------------------------------------------------------------
# 4. Dauer-Analyse
# ---------------------------------------------------------------------------


def _duration_analysis(
    conn: sqlite3.Connection, streamer: str, since: str
) -> Dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
            s.duration_seconds,
            s.avg_viewers,
            s.unique_chatters,
            s.retention_5m
        FROM twitch_stream_sessions s
        WHERE s.streamer_login = ? AND s.started_at >= ?
          AND s.duration_seconds > 300
        """,
        (streamer, since),
    ).fetchall()

    if not rows:
        return {
            "buckets": [],
            "optimalLabel": "",
            "currentAvgHours": 0,
            "correlation": 0,
        }

    buckets_def = [
        ("< 1h", 0, 3600),
        ("1-2h", 3600, 7200),
        ("2-3h", 7200, 10800),
        ("3-4h", 10800, 14400),
        ("4-5h", 14400, 18000),
        ("5h+", 18000, 999999),
    ]

    buckets = []
    for label, lo, hi in buckets_def:
        subset = [r for r in rows if lo <= r[0] < hi]
        if not subset:
            buckets.append(
                {
                    "label": label,
                    "streamCount": 0,
                    "avgViewers": 0,
                    "avgChatters": 0,
                    "avgRetention5m": 0,
                    "efficiencyRatio": 0,
                }
            )
            continue

        avg_v = sum(r[1] for r in subset) / len(subset)
        avg_c = sum(r[2] or 0 for r in subset) / len(subset)
        ret_vals = [r[3] for r in subset if r[3] is not None]
        avg_ret = sum(ret_vals) / len(ret_vals) if ret_vals else 0
        avg_dur = sum(r[0] for r in subset) / len(subset)
        eff = (avg_v * avg_dur / 3600) / (avg_dur / 3600) if avg_dur > 0 else 0

        buckets.append(
            {
                "label": label,
                "streamCount": len(subset),
                "avgViewers": round(avg_v, 1),
                "avgChatters": round(avg_c, 1),
                "avgRetention5m": round(avg_ret, 1),
                "efficiencyRatio": round(eff, 1),
            }
        )

    # Find optimal bucket (highest avg viewers among buckets with >= 2 streams)
    valid = [b for b in buckets if b["streamCount"] >= 2]
    optimal = max(valid, key=lambda b: b["avgViewers"])["label"] if valid else ""

    total_dur = sum(r[0] for r in rows)
    current_avg = total_dur / len(rows) / 3600 if rows else 0

    # Simple correlation: duration vs viewers
    correlation = _pearson([r[0] for r in rows], [r[1] for r in rows])

    return {
        "buckets": buckets,
        "optimalLabel": optimal,
        "currentAvgHours": round(current_avg, 1),
        "correlation": round(correlation, 3),
    }


# ---------------------------------------------------------------------------
# 5. Cross-Community
# ---------------------------------------------------------------------------


def _cross_community(
    conn: sqlite3.Connection, streamer: str, since: str
) -> Dict[str, Any]:
    # Your unique chatters
    your_chatters_row = conn.execute(
        """
        SELECT COUNT(DISTINCT chatter_login)
        FROM twitch_chatter_rollup
        WHERE streamer_login = ? AND last_seen_at >= ?
        """,
        (streamer, since),
    ).fetchone()
    total_unique = your_chatters_row[0] if your_chatters_row else 0

    if total_unique == 0:
        return {
            "totalUniqueChatters": 0,
            "chatterSources": [],
            "isolatedChatters": 0,
            "isolatedPercentage": 0,
            "ecosystemSummary": "Keine Chatter-Daten verfuegbar.",
        }

    # Shared chatters with other streamers
    shared = conn.execute(
        """
        SELECT
            c2.streamer_login as source_streamer,
            COUNT(DISTINCT c1.chatter_login) as shared
        FROM twitch_chatter_rollup c1
        JOIN twitch_chatter_rollup c2
          ON c1.chatter_login = c2.chatter_login
          AND c2.streamer_login != ?
          AND c2.last_seen_at >= ?
        WHERE c1.streamer_login = ? AND c1.last_seen_at >= ?
        GROUP BY c2.streamer_login
        ORDER BY shared DESC
        LIMIT 15
        """,
        (streamer, since, streamer, since),
    ).fetchall()

    sources = [
        {
            "sourceStreamer": r[0],
            "sharedChatters": r[1],
            "percentage": round(r[1] / total_unique * 100, 1) if total_unique else 0,
        }
        for r in shared
    ]

    # Chatters that ONLY appear in your channel
    all_shared_chatters = conn.execute(
        """
        SELECT COUNT(DISTINCT c1.chatter_login)
        FROM twitch_chatter_rollup c1
        WHERE c1.streamer_login = ? AND c1.last_seen_at >= ?
          AND EXISTS (
            SELECT 1 FROM twitch_chatter_rollup c2
            WHERE c2.chatter_login = c1.chatter_login
              AND c2.streamer_login != ?
              AND c2.last_seen_at >= ?
          )
        """,
        (streamer, since, streamer, since),
    ).fetchone()
    shared_count = all_shared_chatters[0] if all_shared_chatters else 0
    isolated = total_unique - shared_count
    isolated_pct = round(isolated / total_unique * 100, 1) if total_unique > 0 else 0

    if isolated_pct > 60:
        summary = "Deine Community ist stark eigenstaendig - die meisten Chatter sind nur in deinem Channel aktiv."
    elif isolated_pct > 30:
        summary = "Gute Mischung: Ein Teil deiner Zuschauer kommt aus der Deadlock-Community, viele sind aber deine eigenen."
    else:
        summary = "Dein Channel profitiert stark vom Community-Oekoystem. Viele Zuschauer kennst du aus anderen Channels."

    return {
        "totalUniqueChatters": total_unique,
        "chatterSources": sources,
        "isolatedChatters": isolated,
        "isolatedPercentage": isolated_pct,
        "ecosystemSummary": summary,
    }


# ---------------------------------------------------------------------------
# 6. Tag-Optimierung
# ---------------------------------------------------------------------------


def _tag_optimization(
    conn: sqlite3.Connection, streamer: str, since: str
) -> Dict[str, Any]:
    # Your tags
    your_rows = conn.execute(
        """
        SELECT s.tags, AVG(s.avg_viewers) as avg_v, COUNT(*) as cnt
        FROM twitch_stream_sessions s
        WHERE s.streamer_login = ? AND s.started_at >= ?
          AND s.tags IS NOT NULL AND s.tags != ''
        GROUP BY s.tags
        ORDER BY avg_v DESC
        """,
        (streamer, since),
    ).fetchall()

    your_tags = [
        {"tags": r[0], "avgViewers": round(r[1], 1), "usageCount": r[2]}
        for r in your_rows
    ]

    # Extract individual tags from your streams
    your_individual = _split_tags_from_rows(your_rows)

    # Category best tags (all streamers)
    cat_rows = conn.execute(
        """
        SELECT s.tags, AVG(s.avg_viewers) as avg_v, COUNT(DISTINCT s.streamer_login) as streamer_cnt
        FROM twitch_stream_sessions s
        WHERE s.started_at >= ?
          AND s.tags IS NOT NULL AND s.tags != ''
        GROUP BY s.tags
        HAVING COUNT(*) >= 3
        ORDER BY avg_v DESC
        LIMIT 15
        """,
        (since,),
    ).fetchall()

    cat_tags = [
        {"tags": r[0], "avgViewers": round(r[1], 1), "streamerCount": r[2]}
        for r in cat_rows
    ]

    cat_individual = _split_tags_from_rows(cat_rows)

    # Find missing high performers
    missing = [t for t in cat_individual if t not in your_individual][:10]

    # Find underperforming tags (your tags that perform below your average)
    if your_tags:
        your_avg = sum(t["avgViewers"] for t in your_tags) / len(your_tags)
        underperforming = [
            t["tags"] for t in your_tags if t["avgViewers"] < your_avg * 0.8
        ]
    else:
        underperforming = []

    return {
        "yourTags": your_tags,
        "categoryBestTags": cat_tags,
        "missingHighPerformers": missing,
        "underperformingTags": underperforming[:5],
    }


def _split_tags_from_rows(rows: list) -> set:
    """Extract unique individual tag names from grouped tag strings."""
    tags = set()
    for r in rows:
        raw = r[0] if isinstance(r[0], str) else ""
        for part in re.split(r"[,;|]", raw):
            cleaned = part.strip().lower()
            if cleaned:
                tags.add(cleaned)
    return tags


# ---------------------------------------------------------------------------
# 7. Retention-Coaching
# ---------------------------------------------------------------------------


def _retention_coaching(
    conn: sqlite3.Connection, streamer: str, since: str
) -> Dict[str, Any]:
    # Your 5-min retention average
    your_ret = conn.execute(
        """
        SELECT AVG(retention_5m)
        FROM twitch_stream_sessions
        WHERE streamer_login = ? AND started_at >= ?
          AND retention_5m IS NOT NULL
        """,
        (streamer, since),
    ).fetchone()
    your_5m = round(your_ret[0], 1) if your_ret and your_ret[0] else 0

    # Category average
    cat_ret = conn.execute(
        """
        SELECT AVG(retention_5m)
        FROM twitch_stream_sessions
        WHERE started_at >= ? AND retention_5m IS NOT NULL
        """,
        (since,),
    ).fetchone()
    cat_5m = round(cat_ret[0], 1) if cat_ret and cat_ret[0] else 0

    # Your viewer curve (minute-by-minute, normalized)
    your_sessions = conn.execute(
        """
        SELECT s.id, s.peak_viewers
        FROM twitch_stream_sessions s
        WHERE s.streamer_login = ? AND s.started_at >= ?
          AND s.peak_viewers > 0
        ORDER BY s.started_at DESC
        LIMIT 20
        """,
        (streamer, since),
    ).fetchall()

    your_curve = _build_viewer_curve(
        conn, [r[0] for r in your_sessions], [r[1] for r in your_sessions]
    )

    # Top performer curve (top 5 by avg_viewers)
    top_sessions = conn.execute(
        """
        SELECT s.id, s.peak_viewers
        FROM twitch_stream_sessions s
        WHERE s.started_at >= ?
          AND s.streamer_login != ?
          AND s.peak_viewers > 0
        ORDER BY s.avg_viewers DESC
        LIMIT 20
        """,
        (since, streamer),
    ).fetchall()

    top_curve = _build_viewer_curve(
        conn, [r[0] for r in top_sessions], [r[1] for r in top_sessions]
    )

    # Find critical drop-off minute
    critical_minute = 0
    for i in range(1, len(your_curve)):
        if your_curve[i]["avgViewerPct"] < your_curve[i - 1]["avgViewerPct"] * 0.9:
            critical_minute = your_curve[i]["minute"]
            break

    return {
        "your5mRetention": your_5m,
        "category5mRetention": cat_5m,
        "yourViewerCurve": your_curve,
        "topPerformerCurve": top_curve,
        "criticalDropoffMinute": critical_minute,
    }


def _build_viewer_curve(
    conn: sqlite3.Connection, session_ids: List[int], peak_viewers: List[int]
) -> List[Dict[str, Any]]:
    """Build normalized viewer curve from session_viewers data."""
    if not session_ids:
        return []

    normalized_pairs: List[Tuple[int, int]] = []
    for sid, peak in zip(session_ids, peak_viewers):
        try:
            normalized_pairs.append((int(sid), int(peak)))
        except (TypeError, ValueError):
            continue
    normalized_session_ids = [sid for sid, _ in normalized_pairs]
    if not normalized_session_ids:
        return []

    # Keep SQL static and pass session ids as data to avoid dynamic query construction.
    session_id_csv = ",".join(str(sid) for sid in normalized_session_ids)
    rows = conn.execute(
        """
        SELECT session_id, minutes_from_start, viewer_count
        FROM twitch_session_viewers
        WHERE instr(',' || ? || ',', ',' || CAST(session_id AS TEXT) || ',') > 0
          AND minutes_from_start <= 60
        ORDER BY session_id, minutes_from_start
        """,
        (session_id_csv,),
    ).fetchall()

    peak_map = {sid: peak for sid, peak in normalized_pairs}
    by_minute: Dict[int, List[float]] = defaultdict(list)

    for sid, minute, vc in rows:
        peak = peak_map.get(sid, 1)
        if peak > 0:
            by_minute[minute].append(vc / peak * 100)

    curve = []
    for m in range(0, 61, 5):
        vals = by_minute.get(m, [])
        avg_pct = sum(vals) / len(vals) if vals else 0
        curve.append({"minute": m, "avgViewerPct": round(avg_pct, 1)})

    return curve


# ---------------------------------------------------------------------------
# 8. Doppel-Stream-Erkennung
# ---------------------------------------------------------------------------


def _double_stream_detection(
    conn: sqlite3.Connection, streamer: str, since: str
) -> Dict[str, Any]:
    rows = conn.execute(
        """
        SELECT
            DATE(started_at) as stream_date,
            COUNT(*) as session_count,
            AVG(avg_viewers) as avg_v
        FROM twitch_stream_sessions
        WHERE streamer_login = ? AND started_at >= ?
          AND duration_seconds > 300
        GROUP BY stream_date
        HAVING session_count > 1
        ORDER BY stream_date DESC
        """,
        (streamer, since),
    ).fetchall()

    occurrences = [
        {
            "date": r[0],
            "sessionCount": r[1],
            "avgViewers": round(r[2], 1),
        }
        for r in rows
    ]

    # Compare: average viewers on single-stream days vs double-stream days
    single_avg_row = conn.execute(
        """
        SELECT AVG(day_avg) FROM (
            SELECT DATE(started_at) as d, AVG(avg_viewers) as day_avg
            FROM twitch_stream_sessions
            WHERE streamer_login = ? AND started_at >= ? AND duration_seconds > 300
            GROUP BY d
            HAVING COUNT(*) = 1
        )
        """,
        (streamer, since),
    ).fetchone()
    single_avg = (
        round(single_avg_row[0], 1) if single_avg_row and single_avg_row[0] else 0
    )

    double_avg_row = conn.execute(
        """
        SELECT AVG(day_avg) FROM (
            SELECT DATE(started_at) as d, AVG(avg_viewers) as day_avg
            FROM twitch_stream_sessions
            WHERE streamer_login = ? AND started_at >= ? AND duration_seconds > 300
            GROUP BY d
            HAVING COUNT(*) > 1
        )
        """,
        (streamer, since),
    ).fetchone()
    double_avg = (
        round(double_avg_row[0], 1) if double_avg_row and double_avg_row[0] else 0
    )

    return {
        "detected": len(occurrences) > 0,
        "count": len(occurrences),
        "occurrences": occurrences[:10],
        "singleDayAvg": single_avg,
        "doubleDayAvg": double_avg,
    }


# ---------------------------------------------------------------------------
# 9. Chat-Konzentration & Loyalty
# ---------------------------------------------------------------------------


def _chat_concentration(
    conn: sqlite3.Connection, streamer: str, since: str
) -> Dict[str, Any]:
    """Analyse chat dependency and chatter loyalty distribution."""
    # Loyalty buckets
    buckets_raw = conn.execute(
        """
        SELECT
            CASE
                WHEN total_sessions = 1 THEN 'oneTimer'
                WHEN total_sessions BETWEEN 2 AND 3 THEN 'casual'
                WHEN total_sessions BETWEEN 4 AND 10 THEN 'regular'
                ELSE 'loyal'
            END as bucket,
            COUNT(*) as cnt,
            SUM(total_messages) as msgs
        FROM twitch_chatter_rollup
        WHERE streamer_login = ? AND last_seen_at >= ?
        GROUP BY bucket
        """,
        (streamer, since),
    ).fetchall()

    total_chatters = sum(r[1] for r in buckets_raw) or 1
    total_msgs = sum(r[2] or 0 for r in buckets_raw) or 1
    buckets = {}
    for r in buckets_raw:
        buckets[r[0]] = {
            "count": r[1],
            "pct": round(r[1] / total_chatters * 100, 1),
            "messages": r[2] or 0,
        }

    # Top chatters and concentration
    top = conn.execute(
        """
        SELECT chatter_login, total_messages, total_sessions
        FROM twitch_chatter_rollup
        WHERE streamer_login = ? AND last_seen_at >= ?
        ORDER BY total_messages DESC
        LIMIT 15
        """,
        (streamer, since),
    ).fetchall()

    top_chatters = []
    cumulative = 0.0
    for r in top:
        share = round(r[1] / total_msgs * 100, 1) if total_msgs > 0 else 0
        cumulative += share
        top_chatters.append(
            {
                "login": r[0],
                "messages": r[1],
                "sessions": r[2],
                "sharePct": share,
                "cumulativePct": round(cumulative, 1),
            }
        )

    # HHI index (higher = more concentrated, 10000 = monopoly)
    hhi = sum((r[1] / total_msgs) ** 2 for r in top) * 10000 if total_msgs > 0 else 0

    top1_pct = top_chatters[0]["sharePct"] if top_chatters else 0
    top3_pct = top_chatters[2]["cumulativePct"] if len(top_chatters) >= 3 else top1_pct

    # Peer comparison: one-timer rate
    peer_loyalty = conn.execute(
        """
        SELECT
            streamer_login,
            COUNT(*) as total,
            SUM(CASE WHEN total_sessions = 1 THEN 1 ELSE 0 END) as one_timers
        FROM twitch_chatter_rollup
        WHERE last_seen_at >= ?
        GROUP BY streamer_login
        HAVING total >= 5
        """,
        (since,),
    ).fetchall()

    own_one_timer_pct = buckets.get("oneTimer", {}).get("pct", 0)
    peer_one_timer_pcts = [
        round(r[2] / r[1] * 100, 1)
        for r in peer_loyalty
        if r[0] != streamer and r[1] > 0
    ]
    avg_peer_one_timer = (
        round(sum(peer_one_timer_pcts) / len(peer_one_timer_pcts), 1)
        if peer_one_timer_pcts
        else 0
    )

    return {
        "totalChatters": total_chatters,
        "totalMessages": total_msgs,
        "msgsPerChatter": round(total_msgs / total_chatters, 1),
        "loyaltyBuckets": buckets,
        "topChatters": top_chatters[:10],
        "concentrationIndex": round(hhi, 0),
        "top1Pct": top1_pct,
        "top3Pct": top3_pct,
        "ownOneTimerPct": own_one_timer_pct,
        "avgPeerOneTimerPct": avg_peer_one_timer,
    }


# ---------------------------------------------------------------------------
# 10. Raid-Netzwerk
# ---------------------------------------------------------------------------


def _raid_network(
    conn: sqlite3.Connection, streamer: str, since: str
) -> Dict[str, Any]:
    """Analyse raid send/receive balance and partner reciprocity."""
    sent = conn.execute(
        """
        SELECT LOWER(to_broadcaster_login), COUNT(*), AVG(viewer_count), SUM(viewer_count)
        FROM twitch_raid_history
        WHERE LOWER(from_broadcaster_login) = ? AND executed_at >= ? AND success = 1
        GROUP BY LOWER(to_broadcaster_login)
        ORDER BY COUNT(*) DESC
        """,
        (streamer, since),
    ).fetchall()

    received = conn.execute(
        """
        SELECT LOWER(from_broadcaster_login), COUNT(*), AVG(viewer_count), SUM(viewer_count)
        FROM twitch_raid_history
        WHERE LOWER(to_broadcaster_login) = ? AND executed_at >= ? AND success = 1
        GROUP BY LOWER(from_broadcaster_login)
        ORDER BY COUNT(*) DESC
        """,
        (streamer, since),
    ).fetchall()

    sent_map = {
        r[0]: {
            "count": r[1],
            "avgViewers": round(r[2] or 0, 1),
            "totalViewers": int(r[3] or 0),
        }
        for r in sent
    }
    recv_map = {
        r[0]: {
            "count": r[1],
            "avgViewers": round(r[2] or 0, 1),
            "totalViewers": int(r[3] or 0),
        }
        for r in received
    }

    all_partners = set(sent_map.keys()) | set(recv_map.keys())
    partners = []
    for p in all_partners:
        s = sent_map.get(p, {"count": 0, "avgViewers": 0, "totalViewers": 0})
        r = recv_map.get(p, {"count": 0, "avgViewers": 0, "totalViewers": 0})
        partners.append(
            {
                "login": p,
                "sentCount": s["count"],
                "sentAvgViewers": s["avgViewers"],
                "receivedCount": r["count"],
                "receivedAvgViewers": r["avgViewers"],
                "reciprocity": "mutual"
                if s["count"] > 0 and r["count"] > 0
                else ("sentOnly" if s["count"] > 0 else "receivedOnly"),
                "balance": r["count"] - s["count"],
            }
        )
    partners.sort(key=lambda x: x["sentCount"] + x["receivedCount"], reverse=True)

    total_sent = sum(v["count"] for v in sent_map.values())
    total_recv = sum(v["count"] for v in recv_map.values())
    total_sent_v = sum(v["totalViewers"] for v in sent_map.values())
    total_recv_v = sum(v["totalViewers"] for v in recv_map.values())
    mutual = sum(1 for p in partners if p["reciprocity"] == "mutual")

    return {
        "totalSent": total_sent,
        "totalReceived": total_recv,
        "totalSentViewers": total_sent_v,
        "totalReceivedViewers": total_recv_v,
        "avgSentViewers": round(total_sent_v / total_sent, 1) if total_sent > 0 else 0,
        "avgReceivedViewers": round(total_recv_v / total_recv, 1)
        if total_recv > 0
        else 0,
        "reciprocityRatio": round(total_recv / total_sent, 2) if total_sent > 0 else 0,
        "mutualPartners": mutual,
        "totalPartners": len(partners),
        "partners": partners[:15],
    }


# ---------------------------------------------------------------------------
# 11. Peer-Vergleich (Detail-Tabelle)
# ---------------------------------------------------------------------------


def _peer_comparison(
    conn: sqlite3.Connection, streamer: str, since: str
) -> Dict[str, Any]:
    """Multi-metric comparison vs similar and aspirational peers."""
    all_rows = conn.execute(
        """
        SELECT
            s.streamer_login,
            COUNT(*) as sessions,
            AVG(s.avg_viewers) as avg_v,
            MAX(s.peak_viewers) as max_peak,
            AVG(s.duration_seconds / 3600.0) as avg_hours,
            AVG(s.unique_chatters) as avg_chat,
            AVG(s.retention_5m) as avg_ret5m,
            SUM(s.duration_seconds / 3600.0) as total_hours,
            SUM(CASE WHEN s.follower_delta > 0 THEN s.follower_delta ELSE 0 END) as follows_gained,
            COUNT(DISTINCT s.stream_title) as unique_titles
        FROM twitch_stream_sessions s
        WHERE s.started_at >= ? AND s.duration_seconds > 300 AND s.ended_at IS NOT NULL
        GROUP BY s.streamer_login
        HAVING sessions >= 3
        ORDER BY avg_v DESC
        """,
        (since,),
    ).fetchall()

    peers = []
    own_data = None
    own_rank = 0
    for i, r in enumerate(all_rows):
        entry = {
            "login": r[0],
            "sessions": r[1],
            "avgViewers": round(r[2] or 0, 1),
            "maxPeak": int(r[3] or 0),
            "avgHours": round(r[4] or 0, 1),
            "avgChatters": round(r[5] or 0, 1),
            "retention5m": round((r[6] or 0) * 100, 1),
            "totalHours": round(r[7] or 0, 1),
            "followsGained": int(r[8] or 0),
            "uniqueTitles": r[9] or 0,
            "titleVariety": round(r[9] / r[1] * 100, 1) if r[1] > 0 and r[9] else 0,
        }
        peers.append(entry)
        if r[0] == streamer:
            own_data = entry
            own_rank = i + 1

    total = len(peers)

    # Per-metric ranks
    metrics_ranked = {}
    if own_data:
        for metric in [
            "avgViewers",
            "maxPeak",
            "avgChatters",
            "retention5m",
            "titleVariety",
            "sessions",
        ]:
            sorted_list = sorted(peers, key=lambda p: p[metric], reverse=True)
            for j, p in enumerate(sorted_list):
                if p["login"] == streamer:
                    metrics_ranked[metric] = {
                        "rank": j + 1,
                        "total": total,
                        "value": p[metric],
                    }
                    break

    # Similar peers (within 50% avg viewers)
    similar = []
    aspirational = []
    if own_data:
        own_avg = own_data["avgViewers"]
        for p in peers:
            if p["login"] == streamer:
                continue
            if own_avg * 0.5 <= p["avgViewers"] <= own_avg * 1.5:
                similar.append(p)
            elif own_avg * 1.5 < p["avgViewers"] <= own_avg * 4:
                aspirational.append(p)

    # Gap to next
    gap = None
    if own_data and own_rank > 1:
        nxt = peers[own_rank - 2]
        gap = {
            "login": nxt["login"],
            "avgViewersDiff": round(nxt["avgViewers"] - own_data["avgViewers"], 1),
            "chatDiff": round(nxt["avgChatters"] - own_data["avgChatters"], 1),
            "retentionDiff": round(nxt["retention5m"] - own_data["retention5m"], 1),
        }

    return {
        "ownData": own_data,
        "ownRank": own_rank,
        "totalStreamers": total,
        "similarPeers": similar[:5],
        "aspirationalPeers": aspirational[:5],
        "metricsRanked": metrics_ranked,
        "gapToNext": gap,
    }


# ---------------------------------------------------------------------------
# 12. Konkurrenz-Dichte nach Uhrzeit
# ---------------------------------------------------------------------------


def _competition_density(
    conn: sqlite3.Connection, streamer: str, since: str
) -> Dict[str, Any]:
    """Competition density using actual stream sessions (not aggregated stats)."""
    # How many streamers are active per hour-of-day?
    density = conn.execute(
        """
        SELECT
            CAST(strftime('%H', s.started_at) AS INTEGER) as hour,
            COUNT(DISTINCT s.streamer_login) as active_streamers,
            AVG(s.avg_viewers) as avg_viewers,
            AVG(s.peak_viewers) as avg_peak
        FROM twitch_stream_sessions s
        WHERE s.started_at >= ? AND s.duration_seconds > 300 AND s.ended_at IS NOT NULL
        GROUP BY hour
        ORDER BY hour
        """,
        (since,),
    ).fetchall()

    # Your performance per hour
    own_hours = conn.execute(
        """
        SELECT
            CAST(strftime('%H', s.started_at) AS INTEGER) as hour,
            COUNT(*) as cnt,
            AVG(s.avg_viewers) as avg_v,
            AVG(s.peak_viewers) as avg_peak,
            AVG(s.unique_chatters) as avg_chat
        FROM twitch_stream_sessions s
        WHERE s.streamer_login = ? AND s.started_at >= ? AND s.duration_seconds > 300
        GROUP BY hour
        ORDER BY hour
        """,
        (streamer, since),
    ).fetchall()

    own_map = {
        r[0]: {
            "count": r[1],
            "avgViewers": round(r[2] or 0, 1),
            "avgPeak": round(r[3] or 0, 1),
            "avgChatters": round(r[4] or 0, 1),
        }
        for r in own_hours
    }

    hourly = []
    for r in density:
        hour = r[0]
        streamers = r[1]
        avg_v = round(r[2] or 0, 1)
        avg_p = round(r[3] or 0, 1)
        opp = round(avg_v / streamers, 2) if streamers > 0 else 0
        hourly.append(
            {
                "hour": hour,
                "activeStreamers": streamers,
                "avgViewers": avg_v,
                "avgPeak": avg_p,
                "opportunityScore": opp,
                "yourData": own_map.get(hour),
            }
        )

    # Same but by weekday
    weekday_density = conn.execute(
        """
        SELECT
            CAST(strftime('%w', s.started_at) AS INTEGER) as weekday,
            COUNT(DISTINCT s.streamer_login) as active_streamers,
            AVG(s.avg_viewers) as avg_viewers
        FROM twitch_stream_sessions s
        WHERE s.started_at >= ? AND s.duration_seconds > 300 AND s.ended_at IS NOT NULL
        GROUP BY weekday
        ORDER BY weekday
        """,
        (since,),
    ).fetchall()

    own_weekdays = conn.execute(
        """
        SELECT
            CAST(strftime('%w', s.started_at) AS INTEGER) as weekday,
            COUNT(*) as cnt,
            AVG(s.avg_viewers) as avg_v,
            AVG(s.peak_viewers) as avg_peak
        FROM twitch_stream_sessions s
        WHERE s.streamer_login = ? AND s.started_at >= ? AND s.duration_seconds > 300
        GROUP BY weekday
        """,
        (streamer, since),
    ).fetchall()

    own_wd_map = {
        r[0]: {
            "count": r[1],
            "avgViewers": round(r[2] or 0, 1),
            "avgPeak": round(r[3] or 0, 1),
        }
        for r in own_weekdays
    }

    weekday_names = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"]
    weekly = []
    for r in weekday_density:
        wd = r[0]
        weekly.append(
            {
                "weekday": wd,
                "weekdayLabel": weekday_names[wd],
                "activeStreamers": r[1],
                "avgViewers": round(r[2] or 0, 1),
                "yourData": own_wd_map.get(wd),
            }
        )

    # Best opportunity hours (low competition, high viewers)
    sweet_spots = sorted(hourly, key=lambda x: x["opportunityScore"], reverse=True)[:5]

    return {
        "hourly": hourly,
        "weekly": weekly,
        "sweetSpots": sweet_spots,
    }


# ---------------------------------------------------------------------------
# 13. Priorisierte Empfehlungen
# ---------------------------------------------------------------------------


def _build_recommendations(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rule-based recommendation engine."""
    recs: List[Dict[str, Any]] = []

    # Double streams detected
    ds = data.get("doubleStreamDetection", {})
    if ds.get("detected") and ds.get("count", 0) > 0:
        impact = ""
        if ds.get("singleDayAvg") and ds.get("doubleDayAvg"):
            diff = ds["singleDayAvg"] - ds["doubleDayAvg"]
            if diff > 0:
                impact = f"An Single-Stream-Tagen hast du {diff:.0f} mehr Ø Viewer."
        recs.append(
            {
                "priority": "critical",
                "category": "Schedule",
                "title": "Doppel-Streams erkannt",
                "description": f"{ds['count']}x hast du an einem Tag mehrfach gestreamt. Das kann dein Wachstum bremsen, weil Viewer nicht wissen, wann du ON bist.",
                "estimatedImpact": impact
                or "Konsistenter Schedule = bessere Zuschauerbindung",
                "evidence": f"{ds['count']} Tage mit mehreren Sessions",
                "icon": "AlertTriangle",
            }
        )

    # Efficiency < 25th percentile
    eff = data.get("efficiency", {})
    if eff.get("percentile", 50) < 25 and eff.get("totalStreamHours", 0) > 5:
        recs.append(
            {
                "priority": "critical",
                "category": "Effizienz",
                "title": "Unterdurchschnittliche Effizienz",
                "description": f"Deine Viewer-Hours pro Stream-Hour ({eff['viewerHoursPerStreamHour']}) liegen unter dem 25. Perzentil. Der Kategorie-Durchschnitt liegt bei {eff['categoryAvg']}.",
                "estimatedImpact": "Kuerzere, fokussiertere Streams koennten deine Effizienz deutlich steigern",
                "evidence": f"Perzentil {eff['percentile']}% | Du: {eff['viewerHoursPerStreamHour']} vs Kat: {eff['categoryAvg']}",
                "icon": "TrendingDown",
            }
        )

    # Retention < 50% of category avg
    ret = data.get("retentionCoaching", {})
    if ret.get("category5mRetention") and ret.get("your5mRetention"):
        if (
            ret["your5mRetention"] < ret["category5mRetention"] * 0.5
            and ret["category5mRetention"] > 0
        ):
            recs.append(
                {
                    "priority": "high",
                    "category": "Retention",
                    "title": "Niedrige 5-Minuten-Retention",
                    "description": f"Deine 5-Min-Retention ({ret['your5mRetention']}%) liegt deutlich unter dem Kategorie-Schnitt ({ret['category5mRetention']}%). Viewer springen frueh ab.",
                    "estimatedImpact": "Bessere Intros und fruehe Interaktion koennen die Retention verdoppeln",
                    "evidence": f"Du: {ret['your5mRetention']}% vs Kategorie: {ret['category5mRetention']}%",
                    "icon": "UserMinus",
                }
            )

    # Duration > 150% of optimal
    dur = data.get("durationAnalysis", {})
    if dur.get("optimalLabel") and dur.get("currentAvgHours"):
        optimal_hours_map = {
            "< 1h": 0.5,
            "1-2h": 1.5,
            "2-3h": 2.5,
            "3-4h": 3.5,
            "4-5h": 4.5,
            "5h+": 6,
        }
        optimal_mid = optimal_hours_map.get(dur["optimalLabel"], 3)
        if dur["currentAvgHours"] > optimal_mid * 1.5 and dur["currentAvgHours"] > 2:
            recs.append(
                {
                    "priority": "high",
                    "category": "Dauer",
                    "title": "Streams zu lang",
                    "description": f"Dein Ø Stream dauert {dur['currentAvgHours']:.1f}h, aber dein Sweet-Spot liegt bei {dur['optimalLabel']}. Laengere Streams verwassern deine Metriken.",
                    "estimatedImpact": f"Kuerze auf {dur['optimalLabel']} fuer bessere Viewer-Zahlen",
                    "evidence": f"Optimaler Bucket: {dur['optimalLabel']} | Aktuell: {dur['currentAvgHours']:.1f}h",
                    "icon": "Clock",
                }
            )

    # Schedule mismatch
    sched = data.get("scheduleOptimizer", {})
    if sched.get("yourCurrentSlots") and sched.get("competitionHeatmap"):
        your_slots_set = {(s["weekday"], s["hour"]) for s in sched["yourCurrentSlots"]}
        # Find high-competition slots
        sorted_comp = sorted(
            sched["competitionHeatmap"], key=lambda x: x["competitors"], reverse=True
        )
        top_competition = sorted_comp[: len(sorted_comp) // 4] if sorted_comp else []
        high_comp_set = {(s["weekday"], s["hour"]) for s in top_competition}

        if your_slots_set and high_comp_set:
            overlap = your_slots_set & high_comp_set
            overlap_pct = (
                len(overlap) / len(your_slots_set) * 100 if your_slots_set else 0
            )
            if overlap_pct > 70:
                recs.append(
                    {
                        "priority": "high",
                        "category": "Schedule",
                        "title": "Zu viel Konkurrenz in deinen Slots",
                        "description": f"{overlap_pct:.0f}% deiner Streams laufen in den konkurrenzstaerksten Zeitfenstern. Verschiebe einige Streams in Sweet-Spots.",
                        "estimatedImpact": "Weniger Konkurrenz = mehr Discovery durch Browse-Tab",
                        "evidence": f"{len(overlap)}/{len(your_slots_set)} Slots in Top-25% Konkurrenz",
                        "icon": "Calendar",
                    }
                )

    # Missing top tags
    tags = data.get("tagOptimization", {})
    if tags.get("missingHighPerformers"):
        missing = tags["missingHighPerformers"][:3]
        recs.append(
            {
                "priority": "high",
                "category": "Tags",
                "title": "Erfolgreiche Tags fehlen",
                "description": f"Dir fehlen Tags, die in der Kategorie gut performen: {', '.join(missing)}. Tags beeinflussen die Sichtbarkeit im Browse-Tab.",
                "estimatedImpact": "Bessere Tags = mehr Discovery ueber Twitch Browse",
                "evidence": f"{len(tags['missingHighPerformers'])} fehlende High-Performer Tags",
                "icon": "Tag",
            }
        )

    # Same title used > 5 times
    titles = data.get("titleAnalysis", {})
    if titles.get("yourTitles"):
        max_reuse = max((t["usageCount"] for t in titles["yourTitles"]), default=0)
        if max_reuse > 5:
            reused = [t for t in titles["yourTitles"] if t["usageCount"] > 5]
            recs.append(
                {
                    "priority": "medium",
                    "category": "Titel",
                    "title": "Titel-Wiederholung",
                    "description": f"Du nutzt denselben Titel zu oft ({max_reuse}x). Variiere deine Titel, um im Browse-Tab aufzufallen.",
                    "estimatedImpact": "Einzigartige Titel erhoehen die Klickrate",
                    "evidence": f"'{reused[0]['title'][:40]}...' wurde {max_reuse}x benutzt"
                    if reused
                    else "",
                    "icon": "Type",
                }
            )

    # Community dependency > 80% external
    comm = data.get("crossCommunity", {})
    if comm.get("isolatedPercentage") is not None:
        if comm["isolatedPercentage"] < 20 and comm.get("totalUniqueChatters", 0) > 10:
            recs.append(
                {
                    "priority": "medium",
                    "category": "Community",
                    "title": "Hohe Abhaengigkeit vom Oekosystem",
                    "description": f"Nur {comm['isolatedPercentage']:.0f}% deiner Chatter sind exklusiv in deinem Channel. Du bist stark vom Deadlock-Oekosystem abhaengig.",
                    "estimatedImpact": "Eigene Community aufbauen = nachhaltiges Wachstum",
                    "evidence": f"{comm['isolatedChatters']} von {comm['totalUniqueChatters']} Chattern nur bei dir",
                    "icon": "Users",
                }
            )

    # Missing title keywords
    if titles.get("yourMissingPatterns"):
        keywords = titles["yourMissingPatterns"][:5]
        recs.append(
            {
                "priority": "low",
                "category": "Titel",
                "title": "Fehlende Titel-Keywords",
                "description": f"Erfolgreiche Streamer nutzen Keywords, die du nicht verwendest: {', '.join(keywords)}.",
                "estimatedImpact": "Kleine Optimierung fuer bessere Discoverability",
                "evidence": f"{len(titles['yourMissingPatterns'])} fehlende Patterns",
                "icon": "Search",
            }
        )

    # Duration-Viewers correlation warning
    if dur.get("correlation") is not None and dur["correlation"] < -0.3:
        recs.append(
            {
                "priority": "medium",
                "category": "Dauer",
                "title": "Negative Korrelation: Laenge vs Viewer",
                "description": f"Je laenger du streamst, desto weniger Viewer hast du (r={dur['correlation']:.2f}). Das deutet auf Ermuedung hin.",
                "estimatedImpact": "Kuerzere Streams koennen den Viewer-Schnitt steigern",
                "evidence": f"Pearson r = {dur['correlation']:.2f}",
                "icon": "TrendingDown",
            }
        )

    # --- NEW: Title variety ---
    if titles.get("varietyPct") is not None and titles.get("avgPeerVarietyPct"):
        own_var = titles["varietyPct"]
        peer_var = titles["avgPeerVarietyPct"]
        if own_var < peer_var * 0.5 and titles.get("totalSessionCount", 0) >= 5:
            recs.append(
                {
                    "priority": "critical",
                    "category": "Titel",
                    "title": "Extrem geringe Titel-Vielfalt",
                    "description": f"Nur {own_var}% einzigartige Titel vs {peer_var}% Peer-Durchschnitt. "
                    f"{titles.get('uniqueTitleCount', 0)} verschiedene bei {titles.get('totalSessionCount', 0)} Sessions.",
                    "estimatedImpact": "Verschiedene Titel locken verschiedene Zielgruppen in der Browse-Page an",
                    "evidence": f"Du: {own_var}% | Peers: {peer_var}%",
                    "icon": "Type",
                }
            )

    # --- NEW: Chat concentration ---
    chat = data.get("chatConcentration", {})
    if chat.get("top1Pct", 0) > 50 and chat.get("totalMessages", 0) > 50:
        recs.append(
            {
                "priority": "high",
                "category": "Community",
                "title": "Chat abhaengig von einer Person",
                "description": f"Ein einzelner Chatter macht {chat['top1Pct']}% aller Nachrichten aus. "
                f"Wenn diese Person wegfaellt, stirbt der Chat.",
                "estimatedImpact": "Neue Chatter aktiv einbinden, Fragen stellen, namentlich ansprechen",
                "evidence": f"Top-Chatter: {chat['topChatters'][0]['login']} ({chat['top1Pct']}%)"
                if chat.get("topChatters")
                else "",
                "icon": "Users",
            }
        )
    elif chat.get("top3Pct", 0) > 70 and chat.get("totalMessages", 0) > 50:
        recs.append(
            {
                "priority": "high",
                "category": "Community",
                "title": "Chat von Top-3 dominiert",
                "description": f"Top 3 Chatter machen {chat['top3Pct']}% aller Nachrichten. "
                f"Dein Chat ist fragil - diversifiziere die Beteiligung.",
                "estimatedImpact": "Interaktive Formate (Coaching, Q&A) bringen neue Stimmen",
                "evidence": f"HHI-Index: {chat.get('concentrationIndex', 0):.0f} (>2500 = hochkonzentriert)",
                "icon": "Users",
            }
        )

    # One-timer rate
    if (
        chat.get("ownOneTimerPct", 0) > chat.get("avgPeerOneTimerPct", 0) + 10
        and chat.get("totalChatters", 0) >= 5
    ):
        recs.append(
            {
                "priority": "medium",
                "category": "Community",
                "title": "Zu viele Einmal-Chatter",
                "description": f"{chat['ownOneTimerPct']}% deiner Chatter kommen nur einmal (Peers: {chat['avgPeerOneTimerPct']}%). "
                f"Follow-up Strategien einsetzen: Discord, Social Media, persoenliche Begruessung.",
                "estimatedImpact": "Senkung der One-Timer-Rate um 10% = deutlich stabilerer Chat",
                "evidence": f"Du: {chat['ownOneTimerPct']}% | Peers: {chat['avgPeerOneTimerPct']}%",
                "icon": "UserMinus",
            }
        )

    # --- NEW: Raid network ---
    raids = data.get("raidNetwork", {})
    if raids.get("totalSent", 0) > 5 and raids.get("reciprocityRatio", 0) < 0.3:
        recs.append(
            {
                "priority": "medium",
                "category": "Netzwerk",
                "title": "Einseitiges Raid-Netzwerk",
                "description": f"Du sendest {raids['totalSent']} Raids, erhaeltst aber nur {raids['totalReceived']}. "
                f"Reziprozitaet: {raids['reciprocityRatio']}x. Aktiv gegenseitige Raid-Partnerschaften aufbauen.",
                "estimatedImpact": "Gegenseitige Raids bringen neue Viewer in deinen Channel",
                "evidence": f"Gesendet: {raids['totalSent']} | Erhalten: {raids['totalReceived']} | Mutual: {raids.get('mutualPartners', 0)}",
                "icon": "Users",
            }
        )

    # --- NEW: Competition density ---
    comp = data.get("competitionDensity", {})
    sweet = comp.get("sweetSpots", [])
    hourly_data = comp.get("hourly", [])
    if sweet and hourly_data:
        best_hour = sweet[0]
        # Check if streamer streams at the best hour
        if not best_hour.get("yourData"):
            recs.append(
                {
                    "priority": "high",
                    "category": "Schedule",
                    "title": f"Ungenutzter Sweet-Spot: {best_hour['hour']:02d}:00 UTC",
                    "description": f"Um {best_hour['hour']:02d}:00 UTC gibt es nur {best_hour['activeStreamers']} aktive Streamer "
                    f"bei {best_hour['avgViewers']} Ø Viewern. Du streamst dort nicht.",
                    "estimatedImpact": f"Opportunity-Score {best_hour['opportunityScore']} - hoechstes Viewer/Konkurrenz-Verhaeltnis",
                    "evidence": f"{best_hour['activeStreamers']} Streamer | {best_hour['avgViewers']} Ø Viewer",
                    "icon": "Calendar",
                }
            )

    # Sort by priority
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    recs.sort(key=lambda r: priority_order.get(r["priority"], 4))

    return recs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pearson(x: List[float], y: List[float]) -> float:
    """Simple Pearson correlation coefficient."""
    n = len(x)
    if n < 3:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    sx = math.sqrt(sum((xi - mx) ** 2 for xi in x) / n)
    sy = math.sqrt(sum((yi - my) ** 2 for yi in y) / n)
    if sx == 0 or sy == 0:
        return 0.0
    cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / n
    return cov / (sx * sy)
