"""Backend analytics queries and data processing for the dashboard."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .. import storage

log = logging.getLogger("TwitchStreams.AnalyticsBackend")


class AnalyticsBackend:
    """Backend queries for advanced analytics dashboard."""

    @staticmethod
    async def get_streamer_analytics_data(streamer_login: str, days: int = 30) -> Dict[str, Any]:
        """
        Comprehensive analytics data for a specific streamer or all tracked streamers.
        
        Returns:
        {
            "metrics": {...},           # KPI summary cards
            "retention_timeline": [...], # Daily retention metrics
            "discovery_timeline": [...], # Daily discovery/growth metrics
            "chat_timeline": [...],      # Daily chat health metrics
            "insights": [...],           # Actionable recommendations
            "empty": bool                # True if no data available
        }
        """
        try:
            with storage.get_conn() as conn:
                # Determine filter
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

                # Check if we have any data
                if streamer_login:
                    normalized_login = streamer_login.lower().strip()
                    row = conn.execute(
                        """
                        SELECT COUNT(*) as cnt
                        FROM twitch_stream_sessions s
                        WHERE s.started_at >= ?
                          AND LOWER(s.streamer_login) = ?
                        """,
                        [since_date, normalized_login],
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT COUNT(*) as cnt
                        FROM twitch_stream_sessions s
                        WHERE s.started_at >= ?
                        """,
                        [since_date],
                    ).fetchone()
                session_count = row[0] if row else 0
                
                if session_count == 0:
                    return {"empty": True}
                
                # Get metrics
                metrics = AnalyticsBackend._calculate_metrics(conn, since_date, streamer_login)
                
                # Get timelines
                retention_timeline = AnalyticsBackend._get_retention_timeline(conn, since_date, streamer_login, days)
                discovery_timeline = AnalyticsBackend._get_discovery_timeline(conn, since_date, streamer_login, days)
                chat_timeline = AnalyticsBackend._get_chat_timeline(conn, since_date, streamer_login, days)
                
                # Generate insights
                insights = AnalyticsBackend._generate_insights(metrics, retention_timeline, discovery_timeline, chat_timeline)
                
                return {
                    "metrics": metrics,
                    "retention_timeline": retention_timeline,
                    "discovery_timeline": discovery_timeline,
                    "chat_timeline": chat_timeline,
                    "insights": insights,
                    "empty": False
                }
        except Exception:
            log.exception("Failed to get analytics data for %s", streamer_login)
            return {"error": "Internal error", "empty": True}

    @staticmethod
    def _calculate_metrics(conn, since_date: str, streamer_login: Optional[str]) -> Dict[str, Any]:
        """Calculate summary metrics for KPI cards."""
        normalized_login = streamer_login.lower().strip() if streamer_login else None
        
        # Retention metrics
        if normalized_login:
            row = conn.execute(
                """
                SELECT
                    AVG(s.retention_5m) as avg_ret_5m,
                    AVG(s.retention_10m) as avg_ret_10m,
                    AVG(s.retention_20m) as avg_ret_20m,
                    AVG(s.dropoff_pct) as avg_dropoff
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ?
                  AND s.retention_5m IS NOT NULL
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
                    AVG(s.dropoff_pct) as avg_dropoff
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ?
                  AND s.retention_5m IS NOT NULL
                """,
                [since_date],
            ).fetchone()
        
        ret_5m = float(row[0]) if row and row[0] else 0.0
        ret_10m = float(row[1]) if row and row[1] else 0.0
        ret_20m = float(row[2]) if row and row[2] else 0.0
        avg_dropoff = float(row[3]) if row and row[3] else 0.0
        
        # Discovery metrics
        if normalized_login:
            row = conn.execute(
                """
                SELECT
                    AVG(s.peak_viewers) as avg_peak,
                    SUM(COALESCE(s.follower_delta, 0)) as total_followers,
                    COUNT(*) as session_count,
                    SUM(s.duration_seconds) as total_duration_sec
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
                    AVG(s.peak_viewers) as avg_peak,
                    SUM(COALESCE(s.follower_delta, 0)) as total_followers,
                    COUNT(*) as session_count,
                    SUM(s.duration_seconds) as total_duration_sec
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ?
                  AND s.ended_at IS NOT NULL
                """,
                [since_date],
            ).fetchone()
        
        avg_peak = float(row[0]) if row and row[0] else 0.0
        total_followers = int(row[1]) if row and row[1] else 0
        session_count = int(row[2]) if row and row[2] else 0
        total_duration_hours = (int(row[3]) if row and row[3] else 0) / 3600.0
        
        followers_per_session = total_followers / session_count if session_count > 0 else 0.0
        followers_per_hour = total_followers / total_duration_hours if total_duration_hours > 0 else 0.0
        
        # Chat metrics
        if normalized_login:
            row = conn.execute(
                """
                SELECT
                    AVG(s.unique_chatters) as avg_unique,
                    AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100,
                    SUM(s.first_time_chatters) as total_first_time,
                    SUM(s.returning_chatters) as total_returning
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ?
                  AND s.ended_at IS NOT NULL
                  AND s.avg_viewers > 0
                  AND LOWER(s.streamer_login) = ?
                """,
                [since_date, normalized_login],
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT
                    AVG(s.unique_chatters) as avg_unique,
                    AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100,
                    SUM(s.first_time_chatters) as total_first_time,
                    SUM(s.returning_chatters) as total_returning
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ?
                  AND s.ended_at IS NOT NULL
                  AND s.avg_viewers > 0
                """,
                [since_date],
            ).fetchone()
        
        avg_unique_chatters = float(row[0]) if row and row[0] else 0.0
        chat_per_100 = float(row[1]) if row and row[1] else 0.0
        total_first_time = int(row[2]) if row and row[2] else 0
        total_returning = int(row[3]) if row and row[3] else 0
        
        # Calculate trends (compare to previous period)
        prev_since = (datetime.fromisoformat(since_date.replace('Z', '+00:00')) - timedelta(days=30)).isoformat()
        if normalized_login:
            prev_row = conn.execute(
                """
                SELECT AVG(s.retention_5m) as avg_ret_5m
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ? AND s.started_at < ?
                  AND s.retention_5m IS NOT NULL
                  AND LOWER(s.streamer_login) = ?
                """,
                [prev_since, since_date, normalized_login],
            ).fetchone()
        else:
            prev_row = conn.execute(
                """
                SELECT AVG(s.retention_5m) as avg_ret_5m
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ? AND s.started_at < ?
                  AND s.retention_5m IS NOT NULL
                """,
                [prev_since, since_date],
            ).fetchone()
        prev_ret_5m = float(prev_row[0]) if prev_row and prev_row[0] else 0.0
        
        retention_trend = ((ret_5m - prev_ret_5m) / prev_ret_5m * 100) if prev_ret_5m > 0 else 0.0
        
        return {
            "retention_5m": ret_5m / 100.0,  # Convert to 0-1 range
            "retention_10m": ret_10m / 100.0,
            "retention_20m": ret_20m / 100.0,
            "avg_dropoff": avg_dropoff / 100.0,
            "retention_5m_trend": retention_trend,
            "avg_peak_viewers": avg_peak,
            "total_followers_delta": total_followers,
            "followers_per_session": followers_per_session,
            "followers_per_hour": followers_per_hour,
            "peak_viewers_trend": 0.0,  # TODO: Calculate
            "followers_trend": 0.0,  # TODO: Calculate
            "unique_chatters_per_100": chat_per_100,
            "avg_unique_chatters": avg_unique_chatters,
            "total_first_time_chatters": total_first_time,
            "total_returning_chatters": total_returning,
            "chat_engagement_trend": 0.0,  # TODO: Calculate
        }

    @staticmethod
    def _get_retention_timeline(conn, since_date: str, streamer_login: Optional[str], days: int) -> List[Dict[str, Any]]:
        """Get daily retention metrics timeline."""
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
                GROUP BY DATE(s.started_at)
                ORDER BY date ASC
                """,
                [since_date],
            ).fetchall()
        
        timeline = []
        for row in rows:
            timeline.append({
                "date": row[0] if row[0] else "",
                "retention_5m": float(row[1]) if row[1] else 0.0,
                "retention_10m": float(row[2]) if row[2] else 0.0,
                "retention_20m": float(row[3]) if row[3] else 0.0,
                "dropoff": float(row[4]) if row[4] else 0.0,
            })
        
        return timeline

    @staticmethod
    def _get_discovery_timeline(conn, since_date: str, streamer_login: Optional[str], days: int) -> List[Dict[str, Any]]:
        """Get daily discovery/growth metrics timeline."""
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
        
        timeline = []
        for row in rows:
            timeline.append({
                "date": row[0] if row[0] else "",
                "peak_viewers": int(row[1]) if row[1] else 0,
                "followers_delta": int(row[2]) if row[2] else 0,
                "avg_viewers": float(row[3]) if row[3] else 0.0,
            })
        
        return timeline

    @staticmethod
    def _get_chat_timeline(conn, since_date: str, streamer_login: Optional[str], days: int) -> List[Dict[str, Any]]:
        """Get daily chat health metrics timeline."""
        if streamer_login:
            rows = conn.execute(
                """
                SELECT
                    DATE(s.started_at) as date,
                    AVG(s.unique_chatters) as unique_chatters,
                    AVG(CASE WHEN s.avg_viewers > 0 THEN (s.unique_chatters * 100.0 / s.avg_viewers) ELSE 0 END) as chat_per_100,
                    SUM(s.first_time_chatters) as first_time,
                    SUM(s.returning_chatters) as returning_chatters
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
                    SUM(s.returning_chatters) as returning_chatters
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ?
                  AND s.ended_at IS NOT NULL
                  AND s.avg_viewers > 0
                GROUP BY DATE(s.started_at)
                ORDER BY date ASC
                """,
                [since_date],
            ).fetchall()
        
        timeline = []
        for row in rows:
            timeline.append({
                "date": row[0] if row[0] else "",
                "unique_chatters": float(row[1]) if row[1] else 0.0,
                "chat_per_100": float(row[2]) if row[2] else 0.0,
                "first_time": int(row[3]) if row[3] else 0,
                "returning": int(row[4]) if row[4] else 0,
            })
        
        return timeline

    @staticmethod
    def _generate_insights(
        metrics: Dict[str, Any],
        retention_timeline: List[Dict[str, Any]],
        discovery_timeline: List[Dict[str, Any]],
        chat_timeline: List[Dict[str, Any]]
    ) -> List[Dict[str, str]]:
        """Generate actionable insights based on metrics."""
        insights = []
        
        # Retention insights
        ret_5m = metrics.get("retention_5m", 0.0)
        if ret_5m < 0.50:  # Below 50%
            insights.append({
                "type": "warning",
                "title": "Niedrige 5-Minuten-Retention",
                "description": (
                    f"Deine 5-Minuten-Retention liegt bei {ret_5m*100:.1f}%. "
                    "Empfehlung: Verbessere deinen Stream-Einstieg. Setze einen stärkeren Hook in den ersten 2-3 Minuten. "
                    "Vermeide lange Intros oder Setup-Phasen."
                )
            })
        elif ret_5m > 0.70:  # Above 70%
            insights.append({
                "type": "success",
                "title": "Starke Retention",
                "description": (
                    f"Sehr gut! Deine 5-Minuten-Retention liegt bei {ret_5m*100:.1f}%. "
                    "Dein Content fesselt die Zuschauer von Anfang an."
                )
            })
        
        # Drop-off insights
        avg_dropoff = metrics.get("avg_dropoff", 0.0)
        if avg_dropoff > 0.30:  # Above 30%
            insights.append({
                "type": "warning",
                "title": "Hoher Viewer-Drop-Off",
                "description": (
                    f"Durchschnittlich verlierst du {avg_dropoff*100:.1f}% der Peak-Viewer während des Streams. "
                    "Prüfe, ob es wiederkehrende Zeitpunkte gibt (z.B. nach 30-45 Min) und strukturiere deinen Content neu."
                )
            })
        
        # Discovery insights
        total_followers = metrics.get("total_followers_delta", 0)
        avg_peak = metrics.get("avg_peak_viewers", 0.0)
        if total_followers < avg_peak * 0.05:  # Less than 5% conversion
            insights.append({
                "type": "warning",
                "title": "Niedrige Follower-Conversion",
                "description": (
                    f"Bei durchschnittlich {avg_peak:.0f} Peak-Viewern hast du nur {total_followers} neue Follower gewonnen. "
                    "Empfehlung: Erinnere Zuschauer regelmäßig daran zu folgen. Setze Follow-Goals und belohne neue Follower."
                )
            })
        elif total_followers > avg_peak * 0.15:  # Above 15%
            insights.append({
                "type": "success",
                "title": "Exzellente Follower-Conversion",
                "description": (
                    f"Stark! Du gewinnst {total_followers} Follower bei {avg_peak:.0f} durchschnittlichen Peak-Viewern. "
                    "Dein Content motiviert neue Zuschauer, dir zu folgen."
                )
            })
        
        # Chat health insights
        chat_per_100 = metrics.get("unique_chatters_per_100", 0.0)
        if chat_per_100 < 5.0:  # Below 5 chatters per 100 viewers
            insights.append({
                "type": "warning",
                "title": "Niedrige Chat-Aktivität",
                "description": (
                    f"Nur {chat_per_100:.1f} Unique Chatters pro 100 Viewer. "
                    "Empfehlung: Stelle mehr Fragen an den Chat, starte Umfragen, reagiere aktiv auf Nachrichten. "
                    "Baue Interaktions-Momente in deinen Stream ein."
                )
            })
        elif chat_per_100 > 15.0:  # Above 15
            insights.append({
                "type": "success",
                "title": "Sehr engagierte Community",
                "description": (
                    f"Wow! {chat_per_100:.1f} Unique Chatters pro 100 Viewer zeigen eine sehr aktive Community. "
                    "Deine Zuschauer fühlen sich eingebunden."
                )
            })
        
        # Timeline trend insights
        if len(retention_timeline) >= 7:
            recent_ret = sum(t["retention_5m"] for t in retention_timeline[-7:]) / 7
            older_ret = sum(t["retention_5m"] for t in retention_timeline[:-7]) / len(retention_timeline[:-7]) if len(retention_timeline) > 7 else recent_ret
            
            if recent_ret > older_ret * 1.10:  # 10% improvement
                insights.append({
                    "type": "success",
                    "title": "Retention-Trend steigt",
                    "description": "Deine Retention verbessert sich in den letzten 7 Tagen. Was auch immer du änderst - mach weiter so!"
                })
            elif recent_ret < older_ret * 0.90:  # 10% decline
                insights.append({
                    "type": "warning",
                    "title": "Retention-Trend sinkt",
                    "description": "Deine Retention nimmt in den letzten 7 Tagen ab. Prüfe, ob du Content-Änderungen vorgenommen hast."
                })
        
        return insights

    @staticmethod
    async def get_streamer_overview(login: str) -> Dict[str, Any]:
        """Get comprehensive overview data for a specific streamer."""
        try:
            with storage.get_conn() as conn:
                normalized_login = login.lower().strip()
                
                # Get streamer metadata
                meta_query = """
                    SELECT 
                        s.twitch_login,
                        s.twitch_user_id,
                        s.discord_display_name,
                        s.discord_user_id,
                        s.is_on_discord
                    FROM twitch_streamers s
                    WHERE LOWER(s.twitch_login) = ?
                """
                meta_row = conn.execute(meta_query, [normalized_login]).fetchone()
                
                if not meta_row:
                    return {"error": "Streamer not found"}
                
                meta = {
                    "login": meta_row[0] if meta_row[0] else login,
                    "user_id": meta_row[1] if meta_row[1] else "",
                    "discord_name": meta_row[2] if meta_row[2] else "",
                    "discord_id": meta_row[3] if meta_row[3] else "",
                    "is_on_discord": bool(meta_row[4]) if meta_row[4] else False,
                }
                
                # Get 30-day stats
                since_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                
                stats_query = """
                    SELECT 
                        COUNT(*) as total_streams,
                        AVG(s.avg_viewers) as avg_avg_viewers,
                        MAX(s.peak_viewers) as max_peak,
                        SUM(COALESCE(s.follower_delta, 0)) as total_follower_delta,
                        SUM(s.unique_chatters) as total_unique_chatters,
                        AVG(s.retention_5m) as avg_ret_5m,
                        AVG(s.retention_10m) as avg_ret_10m
                    FROM twitch_stream_sessions s
                    WHERE LOWER(s.streamer_login) = ?
                      AND s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                """
                stats_row = conn.execute(stats_query, [normalized_login, since_30d]).fetchone()
                
                stats_30d = {
                    "total_streams": int(stats_row[0]) if stats_row and stats_row[0] else 0,
                    "avg_avg_viewers": float(stats_row[1]) if stats_row and stats_row[1] else 0.0,
                    "max_peak": int(stats_row[2]) if stats_row and stats_row[2] else 0,
                    "total_follower_delta": int(stats_row[3]) if stats_row and stats_row[3] else 0,
                    "total_unique_chatters": int(stats_row[4]) if stats_row and stats_row[4] else 0,
                    "avg_ret_5m": float(stats_row[5]) if stats_row and stats_row[5] else 0.0,
                    "avg_ret_10m": float(stats_row[6]) if stats_row and stats_row[6] else 0.0,
                }
                
                # Get recent sessions
                sessions_query = """
                    SELECT 
                        s.id,
                        s.started_at,
                        s.stream_title,
                        s.duration_seconds,
                        s.avg_viewers,
                        s.peak_viewers,
                        s.follower_delta,
                        s.unique_chatters,
                        s.retention_5m
                    FROM twitch_stream_sessions s
                    WHERE LOWER(s.streamer_login) = ?
                      AND s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                    ORDER BY s.started_at DESC
                    LIMIT 20
                """
                session_rows = conn.execute(sessions_query, [normalized_login, since_30d]).fetchall()
                
                recent_sessions = []
                for row in session_rows:
                    recent_sessions.append({
                        "id": int(row[0]) if row[0] else 0,
                        "started_at": row[1] if row[1] else "",
                        "stream_title": row[2] if row[2] else "",
                        "duration_seconds": int(row[3]) if row[3] else 0,
                        "avg_viewers": float(row[4]) if row[4] else 0.0,
                        "peak_viewers": int(row[5]) if row[5] else 0,
                        "follower_delta": int(row[6]) if row[6] else 0,
                        "unique_chatters": int(row[7]) if row[7] else 0,
                        "retention_5m": float(row[8]) if row[8] else 0.0,
                    })
                
                return {
                    "login": meta["login"],
                    "meta": meta,
                    "stats_30d": stats_30d,
                    "recent_sessions": recent_sessions
                }
        except Exception:
            log.exception("Failed to get streamer overview for %s", login)
            return {"error": "Internal error"}

    @staticmethod
    async def get_session_detail(session_id: int) -> Dict[str, Any]:
        """Get detailed analytics for a specific stream session."""
        try:
            with storage.get_conn() as conn:
                # Get session data
                session_query = """
                    SELECT 
                        s.id,
                        s.streamer_login,
                        s.started_at,
                        s.ended_at,
                        s.stream_title,
                        s.duration_seconds,
                        s.avg_viewers,
                        s.peak_viewers,
                        s.start_viewers,
                        s.end_viewers,
                        s.retention_5m,
                        s.retention_10m,
                        s.retention_20m,
                        s.dropoff_pct,
                        s.dropoff_label,
                        s.unique_chatters,
                        s.first_time_chatters,
                        s.returning_chatters,
                        s.follower_delta
                    FROM twitch_stream_sessions s
                    WHERE s.id = ?
                """
                session_row = conn.execute(session_query, [session_id]).fetchone()
                
                if not session_row:
                    return {"error": "Session not found"}
                
                session = {
                    "id": int(session_row[0]) if session_row[0] else 0,
                    "streamer_login": session_row[1] if session_row[1] else "",
                    "started_at": session_row[2] if session_row[2] else "",
                    "ended_at": session_row[3] if session_row[3] else "",
                    "stream_title": session_row[4] if session_row[4] else "",
                    "duration_seconds": int(session_row[5]) if session_row[5] else 0,
                    "avg_viewers": float(session_row[6]) if session_row[6] else 0.0,
                    "peak_viewers": int(session_row[7]) if session_row[7] else 0,
                    "start_viewers": int(session_row[8]) if session_row[8] else 0,
                    "end_viewers": int(session_row[9]) if session_row[9] else 0,
                    "retention_5m": float(session_row[10]) if session_row[10] else 0.0,
                    "retention_10m": float(session_row[11]) if session_row[11] else 0.0,
                    "retention_20m": float(session_row[12]) if session_row[12] else 0.0,
                    "dropoff_pct": float(session_row[13]) if session_row[13] else 0.0,
                    "dropoff_label": session_row[14] if session_row[14] else "",
                    "unique_chatters": int(session_row[15]) if session_row[15] else 0,
                    "first_time_chatters": int(session_row[16]) if session_row[16] else 0,
                    "returning_chatters": int(session_row[17]) if session_row[17] else 0,
                    "follower_delta": int(session_row[18]) if session_row[18] else 0,
                }
                
                # Get viewer timeline
                timeline_query = """
                    SELECT 
                        sv.minutes_from_start,
                        sv.viewer_count
                    FROM twitch_session_viewers sv
                    WHERE sv.session_id = ?
                    ORDER BY sv.minutes_from_start ASC
                """
                timeline_rows = conn.execute(timeline_query, [session_id]).fetchall()
                
                timeline = []
                for row in timeline_rows:
                    timeline.append({
                        "minutes_from_start": int(row[0]) if row[0] else 0,
                        "viewer_count": int(row[1]) if row[1] else 0,
                    })
                
                # Get top chatters
                chatters_query = """
                    SELECT 
                        sc.chatter_login,
                        sc.messages,
                        sc.is_first_time_global
                    FROM twitch_session_chatters sc
                    WHERE sc.session_id = ?
                    ORDER BY sc.messages DESC
                    LIMIT 10
                """
                chatter_rows = conn.execute(chatters_query, [session_id]).fetchall()
                
                top_chatters = []
                for row in chatter_rows:
                    top_chatters.append({
                        "chatter_login": row[0] if row[0] else "",
                        "messages": int(row[1]) if row[1] else 0,
                        "is_first_time": bool(row[2]) if row[2] else False,
                    })
                
                return {
                    "session": session,
                    "timeline": timeline,
                    "top_chatters": top_chatters
                }
        except Exception:
            log.exception("Failed to get session detail for ID %s", session_id)
            return {"error": "Internal error"}

    @staticmethod
    async def get_comparison_stats() -> Dict[str, Any]:
        """Get comparison statistics for benchmarking."""
        try:
            with storage.get_conn() as conn:
                since_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
                
                # Category average (Deadlock)
                category_query = """
                    SELECT 
                        AVG(viewer_count) as avg_viewers,
                        MAX(viewer_count) as peak_viewers,
                        COUNT(DISTINCT streamer) as streamer_count
                    FROM twitch_stats_category
                    WHERE ts_utc >= ?
                """
                cat_row = conn.execute(category_query, [since_30d]).fetchone()
                
                category = {
                    "avg_viewers": float(cat_row[0]) if cat_row and cat_row[0] else 0.0,
                    "peak_viewers": int(cat_row[1]) if cat_row and cat_row[1] else 0,
                    "streamer_count": int(cat_row[2]) if cat_row and cat_row[2] else 0,
                }
                
                # Tracked average
                tracked_query = """
                    SELECT 
                        AVG(viewer_count) as avg_viewers,
                        MAX(viewer_count) as peak_viewers
                    FROM twitch_stats_tracked
                    WHERE ts_utc >= ?
                """
                track_row = conn.execute(tracked_query, [since_30d]).fetchone()
                
                tracked_avg = {
                    "avg_viewers": float(track_row[0]) if track_row and track_row[0] else 0.0,
                    "peak_viewers": int(track_row[1]) if track_row and track_row[1] else 0,
                }
                
                # Top streamers
                top_query = """
                    SELECT 
                        streamer as streamer_login,
                        AVG(viewer_count) as val
                    FROM twitch_stats_tracked
                    WHERE ts_utc >= ?
                    GROUP BY streamer
                    ORDER BY val DESC
                    LIMIT 10
                """
                top_rows = conn.execute(top_query, [since_30d]).fetchall()
                
                top_streamers = []
                for row in top_rows:
                    top_streamers.append({
                        "streamer_login": row[0] if row[0] else "",
                        "val": float(row[1]) if row[1] else 0.0,
                    })
                
                return {
                    "category": category,
                    "tracked_avg": tracked_avg,
                    "top_streamers": top_streamers
                }
        except Exception:
            log.exception("Failed to get comparison stats")
            return {"error": "Internal error"}


__all__ = ["AnalyticsBackend"]
