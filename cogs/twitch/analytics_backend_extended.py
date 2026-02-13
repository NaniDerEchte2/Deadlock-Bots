"""
Extended Analytics Backend
Provides comprehensive data aggregation and insights generation
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import storage

log = logging.getLogger("TwitchStreams.AnalyticsBackendExtended")


class AnalyticsBackendExtended:
    """Extended analytics with session-level details and comparison data."""

    @staticmethod
    async def get_comprehensive_analytics(
        streamer_login: Optional[str] = None, 
        days: int = 30
    ) -> Dict[str, Any]:
        """
        Get comprehensive analytics including metrics, timelines, insights, and sessions.
        
        Returns structure compatible with the new React dashboard.
        """
        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

                # Check for data
                if streamer_login:
                    row = conn.execute(
                        """
                        SELECT COUNT(*) as cnt
                        FROM twitch_stream_sessions s
                        WHERE s.started_at >= ?
                          AND s.ended_at IS NOT NULL
                          AND LOWER(s.streamer_login) = ?
                        """,
                        [since_date, streamer_login.lower().strip()],
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT COUNT(*) as cnt
                        FROM twitch_stream_sessions s
                        WHERE s.started_at >= ?
                          AND s.ended_at IS NOT NULL
                        """,
                        [since_date],
                    ).fetchone()
                session_count = row[0] if row else 0
                
                if session_count == 0:
                    return {"empty": True, "message": "Keine Daten für den gewählten Zeitraum"}
                
                # Get all components
                metrics = AnalyticsBackendExtended._calculate_comprehensive_metrics(
                    conn, since_date, streamer_login
                )
                retention_timeline = AnalyticsBackendExtended._get_retention_timeline(
                    conn, since_date, streamer_login
                )
                discovery_timeline = AnalyticsBackendExtended._get_discovery_timeline(
                    conn, since_date, streamer_login
                )
                chat_timeline = AnalyticsBackendExtended._get_chat_timeline(
                    conn, since_date, streamer_login
                )
                sessions = AnalyticsBackendExtended._get_session_list(
                    conn, since_date, streamer_login
                )
                insights = AnalyticsBackendExtended._generate_comprehensive_insights(
                    metrics, retention_timeline, discovery_timeline, chat_timeline
                )
                comparison = AnalyticsBackendExtended._get_comparison_data(
                    conn, since_date, streamer_login
                )
                
                return {
                    "empty": False,
                    "metrics": metrics,
                    "retention_timeline": retention_timeline,
                    "discovery_timeline": discovery_timeline,
                    "chat_timeline": chat_timeline,
                    "sessions": sessions,
                    "insights": insights,
                    "comparison": comparison,
                    "streamer": streamer_login,
                    "days": days
                }
        except Exception:
            log.exception("Failed to get comprehensive analytics for %s", streamer_login)
            return {"error": "Internal error", "empty": True}

    @staticmethod
    def _calculate_comprehensive_metrics(
        conn, since_date: str, streamer_login: Optional[str]
    ) -> Dict[str, Any]:
        """Calculate all metrics needed for the dashboard."""
        normalized_login = streamer_login.lower().strip() if streamer_login else None
        
        # BUGFIX: Prüfe ob follower_delta Spalte existiert
        has_follower_delta = False
        try:
            conn.execute("SELECT follower_delta FROM twitch_stream_sessions LIMIT 1")
            has_follower_delta = True
        except Exception:
            log.debug("follower_delta Spalte fehlt - verwende 0")

        if has_follower_delta:
            if normalized_login:
                row = conn.execute(
                    """
                    SELECT
                        AVG(s.retention_5m) as avg_ret_5m,
                        AVG(s.retention_10m) as avg_ret_10m,
                        AVG(s.retention_20m) as avg_ret_20m,
                        AVG(s.dropoff_pct) as avg_dropoff,
                        AVG(s.peak_viewers) as avg_peak,
                        SUM(COALESCE(s.follower_delta, 0)) as total_followers,
                        COUNT(*) as session_count,
                        SUM(s.duration_seconds) as total_duration_sec,
                        AVG(s.unique_chatters) as avg_unique_chatters,
                        AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100,
                        SUM(s.first_time_chatters) as total_first_time,
                        SUM(s.returning_chatters) as total_returning,
                        AVG(s.avg_viewers) as avg_avg_viewers
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                      AND LOWER(s.streamer_login) = ?
                    """,
                    [since_date, normalized_login],
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT
                        AVG(s.retention_5m) as avg_ret_5m,
                        AVG(s.retention_10m) as avg_ret_10m,
                        AVG(s.retention_20m) as avg_ret_20m,
                        AVG(s.dropoff_pct) as avg_dropoff,
                        AVG(s.peak_viewers) as avg_peak,
                        SUM(COALESCE(s.follower_delta, 0)) as total_followers,
                        COUNT(*) as session_count,
                        SUM(s.duration_seconds) as total_duration_sec,
                        AVG(s.unique_chatters) as avg_unique_chatters,
                        AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100,
                        SUM(s.first_time_chatters) as total_first_time,
                        SUM(s.returning_chatters) as total_returning,
                        AVG(s.avg_viewers) as avg_avg_viewers
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                    """,
                    [since_date],
                ).fetchone()
        else:
            if normalized_login:
                row = conn.execute(
                    """
                    SELECT
                        AVG(s.retention_5m) as avg_ret_5m,
                        AVG(s.retention_10m) as avg_ret_10m,
                        AVG(s.retention_20m) as avg_ret_20m,
                        AVG(s.dropoff_pct) as avg_dropoff,
                        AVG(s.peak_viewers) as avg_peak,
                        0 as total_followers,
                        COUNT(*) as session_count,
                        SUM(s.duration_seconds) as total_duration_sec,
                        AVG(s.unique_chatters) as avg_unique_chatters,
                        AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100,
                        SUM(s.first_time_chatters) as total_first_time,
                        SUM(s.returning_chatters) as total_returning,
                        AVG(s.avg_viewers) as avg_avg_viewers
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                      AND LOWER(s.streamer_login) = ?
                    """,
                    [since_date, normalized_login],
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT
                        AVG(s.retention_5m) as avg_ret_5m,
                        AVG(s.retention_10m) as avg_ret_10m,
                        AVG(s.retention_20m) as avg_ret_20m,
                        AVG(s.dropoff_pct) as avg_dropoff,
                        AVG(s.peak_viewers) as avg_peak,
                        0 as total_followers,
                        COUNT(*) as session_count,
                        SUM(s.duration_seconds) as total_duration_sec,
                        AVG(s.unique_chatters) as avg_unique_chatters,
                        AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100,
                        SUM(s.first_time_chatters) as total_first_time,
                        SUM(s.returning_chatters) as total_returning,
                        AVG(s.avg_viewers) as avg_avg_viewers
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                    """,
                    [since_date],
                ).fetchone()
        
        if not row:
            return {}
        
        # Extract values
        ret_5m = float(row[0]) if row[0] else 0.0
        ret_10m = float(row[1]) if row[1] else 0.0
        ret_20m = float(row[2]) if row[2] else 0.0
        avg_dropoff = float(row[3]) if row[3] else 0.0
        avg_peak = float(row[4]) if row[4] else 0.0
        total_followers = int(row[5]) if row[5] else 0
        session_count = int(row[6]) if row[6] else 0
        total_duration_hours = (int(row[7]) if row[7] else 0) / 3600.0
        avg_unique_chatters = float(row[8]) if row[8] else 0.0
        chat_per_100 = float(row[9]) if row[9] else 0.0
        total_first_time = int(row[10]) if row[10] else 0
        total_returning = int(row[11]) if row[11] else 0
        avg_avg_viewers = float(row[12]) if row[12] else 0.0
        
        # Calculate derived metrics
        followers_per_session = total_followers / session_count if session_count > 0 else 0.0
        followers_per_hour = total_followers / total_duration_hours if total_duration_hours > 0 else 0.0
        
        # Calculate trends (compare to previous period)
        prev_since = (
            datetime.fromisoformat(since_date.replace('Z', '+00:00')) - timedelta(days=30)
        ).isoformat()
        if has_follower_delta:
            if normalized_login:
                prev_row = conn.execute(
                    """
                    SELECT
                        AVG(s.retention_5m) as avg_ret_5m,
                        AVG(s.peak_viewers) as avg_peak,
                        SUM(COALESCE(s.follower_delta, 0)) as total_followers,
                        AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.started_at < ?
                      AND s.ended_at IS NOT NULL
                      AND LOWER(s.streamer_login) = ?
                    """,
                    [prev_since, since_date, normalized_login],
                ).fetchone()
            else:
                prev_row = conn.execute(
                    """
                    SELECT
                        AVG(s.retention_5m) as avg_ret_5m,
                        AVG(s.peak_viewers) as avg_peak,
                        SUM(COALESCE(s.follower_delta, 0)) as total_followers,
                        AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.started_at < ?
                      AND s.ended_at IS NOT NULL
                    """,
                    [prev_since, since_date],
                ).fetchone()
        else:
            if normalized_login:
                prev_row = conn.execute(
                    """
                    SELECT
                        AVG(s.retention_5m) as avg_ret_5m,
                        AVG(s.peak_viewers) as avg_peak,
                        0 as total_followers,
                        AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.started_at < ?
                      AND s.ended_at IS NOT NULL
                      AND LOWER(s.streamer_login) = ?
                    """,
                    [prev_since, since_date, normalized_login],
                ).fetchone()
            else:
                prev_row = conn.execute(
                    """
                    SELECT
                        AVG(s.retention_5m) as avg_ret_5m,
                        AVG(s.peak_viewers) as avg_peak,
                        0 as total_followers,
                        AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.started_at < ?
                      AND s.ended_at IS NOT NULL
                    """,
                    [prev_since, since_date],
                ).fetchone()
        
        prev_ret_5m = float(prev_row[0]) if prev_row and prev_row[0] else 0.0
        prev_peak = float(prev_row[1]) if prev_row and prev_row[1] else 0.0
        prev_followers = int(prev_row[2]) if prev_row and prev_row[2] else 0
        prev_chat = float(prev_row[3]) if prev_row and prev_row[3] else 0.0
        
        # Calculate percentage changes
        retention_trend = ((ret_5m - prev_ret_5m) / prev_ret_5m * 100) if prev_ret_5m > 0 else 0.0
        peak_trend = ((avg_peak - prev_peak) / prev_peak * 100) if prev_peak > 0 else 0.0
        followers_trend = ((total_followers - prev_followers) / prev_followers * 100) if prev_followers > 0 else 0.0
        chat_trend = ((chat_per_100 - prev_chat) / prev_chat * 100) if prev_chat > 0 else 0.0
        
        return {
            # Retention
            "retention_5m": ret_5m / 100.0,
            "retention_10m": ret_10m / 100.0,
            "retention_20m": ret_20m / 100.0,
            "avg_dropoff": avg_dropoff / 100.0,
            "retention_5m_trend": retention_trend,
            
            # Discovery
            "avg_peak_viewers": avg_peak,
            "avg_avg_viewers": avg_avg_viewers,
            "total_followers_delta": total_followers,
            "followers_per_session": followers_per_session,
            "followers_per_hour": followers_per_hour,
            "peak_viewers_trend": peak_trend,
            "followers_trend": followers_trend,
            
            # Chat
            "unique_chatters_per_100": chat_per_100,
            "avg_unique_chatters": avg_unique_chatters,
            "total_first_time_chatters": total_first_time,
            "total_returning_chatters": total_returning,
            "chat_engagement_trend": chat_trend,
            
            # Meta
            "session_count": session_count,
            "total_duration_hours": total_duration_hours
        }

    @staticmethod
    def _get_retention_timeline(
        conn, since_date: str, streamer_login: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Get daily retention metrics."""
        if streamer_login:
            rows = conn.execute(
                """
                SELECT
                    DATE(s.started_at) as date,
                    AVG(s.retention_5m) as ret_5m,
                    AVG(s.retention_10m) as ret_10m,
                    AVG(s.retention_20m) as ret_20m,
                    AVG(s.dropoff_pct) as dropoff
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ?
                  AND s.retention_5m IS NOT NULL
                  AND s.ended_at IS NOT NULL
                  AND LOWER(s.streamer_login) = ?
                GROUP BY DATE(s.started_at)
                ORDER BY date ASC
                """,
                [since_date, streamer_login.lower().strip()],
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT
                    DATE(s.started_at) as date,
                    AVG(s.retention_5m) as ret_5m,
                    AVG(s.retention_10m) as ret_10m,
                    AVG(s.retention_20m) as ret_20m,
                    AVG(s.dropoff_pct) as dropoff
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ?
                  AND s.retention_5m IS NOT NULL
                  AND s.ended_at IS NOT NULL
                GROUP BY DATE(s.started_at)
                ORDER BY date ASC
                """,
                [since_date],
            ).fetchall()
        
        return [
            {
                "date": row[0] if row[0] else "",
                "retention_5m": float(row[1]) if row[1] else 0.0,
                "retention_10m": float(row[2]) if row[2] else 0.0,
                "retention_20m": float(row[3]) if row[3] else 0.0,
                "dropoff": float(row[4]) if row[4] else 0.0
            }
            for row in rows
        ]

    @staticmethod
    def _get_discovery_timeline(
        conn, since_date: str, streamer_login: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Get daily discovery/growth metrics."""
        # BUGFIX: Handle missing follower_delta column
        has_follower_delta = False
        try:
            conn.execute("SELECT follower_delta FROM twitch_stream_sessions LIMIT 1")
            has_follower_delta = True
        except Exception as exc:
            log.debug("follower_delta Spalte fehlt in discovery_timeline - verwende Fallback", exc_info=exc)

        if has_follower_delta:
            if streamer_login:
                rows = conn.execute(
                    """
                    SELECT
                        DATE(s.started_at) as date,
                        AVG(s.peak_viewers) as peak_viewers,
                        SUM(COALESCE(s.follower_delta, 0)) as followers_delta,
                        AVG(s.avg_viewers) as avg_viewers
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                      AND LOWER(s.streamer_login) = ?
                    GROUP BY DATE(s.started_at)
                    ORDER BY date ASC
                    """,
                    [since_date, streamer_login.lower().strip()],
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        DATE(s.started_at) as date,
                        AVG(s.peak_viewers) as peak_viewers,
                        SUM(COALESCE(s.follower_delta, 0)) as followers_delta,
                        AVG(s.avg_viewers) as avg_viewers
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                    GROUP BY DATE(s.started_at)
                    ORDER BY date ASC
                    """,
                    [since_date],
                ).fetchall()
        else:
            if streamer_login:
                rows = conn.execute(
                    """
                    SELECT
                        DATE(s.started_at) as date,
                        AVG(s.peak_viewers) as peak_viewers,
                        0 as followers_delta,
                        AVG(s.avg_viewers) as avg_viewers
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                      AND LOWER(s.streamer_login) = ?
                    GROUP BY DATE(s.started_at)
                    ORDER BY date ASC
                    """,
                    [since_date, streamer_login.lower().strip()],
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        DATE(s.started_at) as date,
                        AVG(s.peak_viewers) as peak_viewers,
                        0 as followers_delta,
                        AVG(s.avg_viewers) as avg_viewers
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                    GROUP BY DATE(s.started_at)
                    ORDER BY date ASC
                    """,
                    [since_date],
                ).fetchall()
        
        return [
            {
                "date": row[0] if row[0] else "",
                "peak_viewers": int(row[1]) if row[1] else 0,
                "followers_delta": int(row[2]) if row[2] else 0,
                "avg_viewers": float(row[3]) if row[3] else 0.0
            }
            for row in rows
        ]

    @staticmethod
    def _get_chat_timeline(
        conn, since_date: str, streamer_login: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Get daily chat health metrics."""
        if streamer_login:
            rows = conn.execute(
                """
                SELECT
                    DATE(s.started_at) as date,
                    AVG(s.unique_chatters) as unique_chatters,
                    AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100,
                    SUM(s.first_time_chatters) as first_time,
                    SUM(s.returning_chatters) as "returning"
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ?
                  AND s.ended_at IS NOT NULL
                  AND s.avg_viewers > 0
                  AND LOWER(s.streamer_login) = ?
                GROUP BY DATE(s.started_at)
                ORDER BY date ASC
                """,
                [since_date, streamer_login.lower().strip()],
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT
                    DATE(s.started_at) as date,
                    AVG(s.unique_chatters) as unique_chatters,
                    AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100,
                    SUM(s.first_time_chatters) as first_time,
                    SUM(s.returning_chatters) as "returning"
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ?
                  AND s.ended_at IS NOT NULL
                  AND s.avg_viewers > 0
                GROUP BY DATE(s.started_at)
                ORDER BY date ASC
                """,
                [since_date],
            ).fetchall()
        
        return [
            {
                "date": row[0] if row[0] else "",
                "unique_chatters": float(row[1]) if row[1] else 0.0,
                "chat_per_100": float(row[2]) if row[2] else 0.0,
                "first_time": int(row[3]) if row[3] else 0,
                "returning": int(row[4]) if row[4] else 0
            }
            for row in rows
        ]

    @staticmethod
    def _get_session_list(
        conn, since_date: str, streamer_login: Optional[str], limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get list of recent sessions with key metrics."""
        normalized_login = streamer_login.lower().strip() if streamer_login else None
        safe_limit = max(1, min(int(limit), 200))
        
        # BUGFIX: Prüfe ob follower_* Spalten existieren, sonst verwende 0
        try:
            conn.execute("SELECT follower_start FROM twitch_stream_sessions LIMIT 1")
            has_follower_cols = True
        except Exception:
            has_follower_cols = False
            log.warning("Follower-Spalten fehlen in DB - verwende Default-Werte (0)")

        if has_follower_cols:
            if normalized_login:
                rows = conn.execute(
                    """
                    SELECT
                        s.id,
                        DATE(s.started_at) as date,
                        TIME(s.started_at) as start_time,
                        s.duration_seconds,
                        s.start_viewers,
                        s.peak_viewers,
                        s.end_viewers,
                        s.avg_viewers,
                        s.retention_5m,
                        s.retention_10m,
                        s.retention_20m,
                        s.dropoff_pct,
                        s.unique_chatters,
                        s.first_time_chatters,
                        s.returning_chatters,
                        COALESCE(s.follower_start, 0) as follower_start,
                        COALESCE(s.follower_end, 0) as follower_end,
                        s.stream_title
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                      AND LOWER(s.streamer_login) = ?
                    ORDER BY s.started_at DESC
                    LIMIT ?
                    """,
                    [since_date, normalized_login, safe_limit],
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        s.id,
                        DATE(s.started_at) as date,
                        TIME(s.started_at) as start_time,
                        s.duration_seconds,
                        s.start_viewers,
                        s.peak_viewers,
                        s.end_viewers,
                        s.avg_viewers,
                        s.retention_5m,
                        s.retention_10m,
                        s.retention_20m,
                        s.dropoff_pct,
                        s.unique_chatters,
                        s.first_time_chatters,
                        s.returning_chatters,
                        COALESCE(s.follower_start, 0) as follower_start,
                        COALESCE(s.follower_end, 0) as follower_end,
                        s.stream_title
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                    ORDER BY s.started_at DESC
                    LIMIT ?
                    """,
                    [since_date, safe_limit],
                ).fetchall()
        else:
            if normalized_login:
                rows = conn.execute(
                    """
                    SELECT
                        s.id,
                        DATE(s.started_at) as date,
                        TIME(s.started_at) as start_time,
                        s.duration_seconds,
                        s.start_viewers,
                        s.peak_viewers,
                        s.end_viewers,
                        s.avg_viewers,
                        s.retention_5m,
                        s.retention_10m,
                        s.retention_20m,
                        s.dropoff_pct,
                        s.unique_chatters,
                        s.first_time_chatters,
                        s.returning_chatters,
                        0 as follower_start,
                        0 as follower_end,
                        s.stream_title
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                      AND LOWER(s.streamer_login) = ?
                    ORDER BY s.started_at DESC
                    LIMIT ?
                    """,
                    [since_date, normalized_login, safe_limit],
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        s.id,
                        DATE(s.started_at) as date,
                        TIME(s.started_at) as start_time,
                        s.duration_seconds,
                        s.start_viewers,
                        s.peak_viewers,
                        s.end_viewers,
                        s.avg_viewers,
                        s.retention_5m,
                        s.retention_10m,
                        s.retention_20m,
                        s.dropoff_pct,
                        s.unique_chatters,
                        s.first_time_chatters,
                        s.returning_chatters,
                        0 as follower_start,
                        0 as follower_end,
                        s.stream_title
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                    ORDER BY s.started_at DESC
                    LIMIT ?
                    """,
                    [since_date, safe_limit],
                ).fetchall()
        
        sessions = []
        for row in rows:
            sessions.append({
                "id": int(row[0]) if row[0] else 0,
                "date": row[1] if row[1] else "",
                "startTime": row[2] if row[2] else "",
                "duration": int(row[3]) if row[3] else 0,
                "startViewers": int(row[4]) if row[4] else 0,
                "peakViewers": int(row[5]) if row[5] else 0,
                "endViewers": int(row[6]) if row[6] else 0,
                "avgViewers": float(row[7]) if row[7] else 0.0,
                "retention5m": float(row[8]) if row[8] else 0.0,
                "retention10m": float(row[9]) if row[9] else 0.0,
                "retention20m": float(row[10]) if row[10] else 0.0,
                "dropoffPct": float(row[11]) if row[11] else 0.0,
                "uniqueChatters": int(row[12]) if row[12] else 0,
                "firstTimeChatters": int(row[13]) if row[13] else 0,
                "returningChatters": int(row[14]) if row[14] else 0,
                "followersStart": int(row[15]) if row[15] else 0,
                "followersEnd": int(row[16]) if row[16] else 0,
                "title": row[17] if row[17] else ""
            })
        
        return sessions

    @staticmethod
    def _generate_comprehensive_insights(
        metrics: Dict[str, Any],
        retention_timeline: List[Dict[str, Any]],
        discovery_timeline: List[Dict[str, Any]],
        chat_timeline: List[Dict[str, Any]]
    ) -> List[Dict[str, str]]:
        """Generate actionable insights based on all metrics."""
        insights = []
        
        # Retention insights
        ret_10m = metrics.get("retention_10m", 0.0) * 100
        if ret_10m < 40:
            insights.append({
                "type": "warning",
                "title": "Niedrige Retention",
                "description": f"Deine 10-Minuten-Retention liegt bei {ret_10m:.1f}%. Zuschauer verlassen früh. Verbessere deinen Stream-Einstieg mit stärkeren Hooks in den ersten Minuten."
            })
        elif ret_10m > 70:
            insights.append({
                "type": "success",
                "title": "Exzellente Retention",
                "description": f"Sehr stark! {ret_10m:.1f}% Retention nach 10 Minuten. Dein Content fesselt die Zuschauer."
            })
        
        # Follower conversion
        avg_peak = metrics.get("avg_peak_viewers", 0)
        total_followers = metrics.get("total_followers_delta", 0)
        if avg_peak > 0:
            conversion_rate = (total_followers / avg_peak) * 100
            if conversion_rate < 5:
                insights.append({
                    "type": "warning",
                    "title": "Niedrige Follower-Conversion",
                    "description": f"Bei {avg_peak:.0f} Ø Peak-Viewern nur {total_followers} neue Follower ({conversion_rate:.1f}%). Erinnere Zuschauer regelmäßig zu folgen."
                })
            elif conversion_rate > 15:
                insights.append({
                    "type": "success",
                    "title": "Starke Follower-Conversion",
                    "description": f"Exzellent! {conversion_rate:.1f}% Conversion-Rate. Dein Content motiviert zum Folgen."
                })
        
        # Chat health
        chat_per_100 = metrics.get("unique_chatters_per_100", 0)
        if chat_per_100 < 5:
            insights.append({
                "type": "warning",
                "title": "Niedrige Chat-Aktivität",
                "description": f"Nur {chat_per_100:.1f} Chatter/100 Viewer. Stelle mehr Fragen, reagiere aktiv, baue Interaktions-Momente ein."
            })
        elif chat_per_100 > 15:
            insights.append({
                "type": "success",
                "title": "Sehr aktive Community",
                "description": f"Wow! {chat_per_100:.1f} Chatter/100 Viewer. Deine Community ist sehr engagiert."
            })
        
        # Trend analysis
        if len(retention_timeline) >= 7:
            recent_ret = sum(t["retention_10m"] for t in retention_timeline[-7:]) / 7
            older_ret_list = retention_timeline[:-7]
            if older_ret_list:
                older_ret = sum(t["retention_10m"] for t in older_ret_list) / len(older_ret_list)
                
                if recent_ret > older_ret * 1.10:
                    insights.append({
                        "type": "success",
                        "title": "Positiver Trend",
                        "description": "Deine Retention verbessert sich in den letzten 7 Tagen. Mach weiter so!"
                    })
                elif recent_ret < older_ret * 0.90:
                    insights.append({
                        "type": "warning",
                        "title": "Negativer Trend",
                        "description": "Deine Retention nimmt ab. Prüfe, ob du Content-Änderungen vorgenommen hast."
                    })
        
        return insights

    @staticmethod
    def _get_comparison_data(
        conn, since_date: str, streamer_login: Optional[str]
    ) -> Dict[str, Any]:
        """Get comparison data for benchmarking."""
        # Category averages
        category_query = """
            SELECT 
                AVG(viewer_count) as avg_viewers,
                MAX(viewer_count) as peak_viewers
            FROM twitch_stats_category
            WHERE ts_utc >= ?
        """
        cat_row = conn.execute(category_query, [since_date]).fetchone()
        
        category_avg = {
            "avgViewers": float(cat_row[0]) if cat_row and cat_row[0] else 0.0,
            "peakViewers": int(cat_row[1]) if cat_row and cat_row[1] else 0,
            "retention10m": 65.0,  # Benchmark
            "chatHealth": 8.5  # Benchmark
        }
        
        # Top streamers
        top_query = """
            SELECT 
                streamer as login,
                AVG(viewer_count) as avg_viewers,
                MAX(viewer_count) as peak_viewers
            FROM twitch_stats_tracked
            WHERE ts_utc >= ?
            GROUP BY streamer
            ORDER BY avg_viewers DESC
            LIMIT 10
        """
        top_rows = conn.execute(top_query, [since_date]).fetchall()
        
        top_streamers = [
            {
                "login": row[0] if row[0] else "",
                "avgViewers": int(row[1]) if row[1] else 0,
                "peakViewers": int(row[2]) if row[2] else 0
            }
            for row in top_rows
        ]
        
        # Your stats (if specific streamer)
        your_stats = {}
        if streamer_login:
            your_query = """
                SELECT 
                    AVG(s.avg_viewers) as avg_viewers,
                    AVG(s.peak_viewers) as peak_viewers,
                    AVG(s.retention_10m) as retention10m,
                    AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_health
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ?
                  AND LOWER(s.streamer_login) = ?
                  AND s.ended_at IS NOT NULL
            """
            your_row = conn.execute(your_query, [since_date, streamer_login.lower().strip()]).fetchone()
            
            if your_row:
                your_stats = {
                    "avgViewers": float(your_row[0]) if your_row[0] else 0.0,
                    "peakViewers": int(your_row[1]) if your_row[1] else 0,
                    "retention10m": float(your_row[2]) if your_row[2] else 0.0,
                    "chatHealth": float(your_row[3]) if your_row[3] else 0.0
                }
        
        return {
            "topStreamers": top_streamers,
            "categoryAvg": category_avg,
            "yourStats": your_stats
        }


__all__ = ["AnalyticsBackendExtended"]
