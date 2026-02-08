"""
Analytics API v2 - Backend endpoints for the new React TypeScript dashboard.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urlsplit

from aiohttp import web

from . import storage

log = logging.getLogger("TwitchStreams.AnalyticsV2")


def _is_loopback_host(raw_value: str) -> bool:
    value = (raw_value or "").strip()
    if not value:
        return False

    token = value.split(",")[0].strip()
    if token.startswith("["):
        end = token.find("]")
        if end != -1:
            token = token[1:end]
    elif token.count(":") == 1:
        host_part, port_part = token.rsplit(":", 1)
        if port_part.isdigit():
            token = host_part

    token = token.strip().lower()
    if token == "localhost":
        return True

    try:
        return ipaddress.ip_address(token).is_loopback
    except ValueError:
        return False


def _is_localhost(request: web.Request) -> bool:
    """Check if request comes from localhost."""
    context_header = (request.headers.get("X-Dashboard-Context") or "").strip().lower()
    if context_header == "public":
        return False
    if context_header == "local":
        return True

    forwarded_for = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded_for:
        return _is_loopback_host(forwarded_for)

    host_header = (
        request.headers.get("X-Forwarded-Host")
        or request.headers.get("Host")
        or request.host
        or ""
    )
    host = host_header.split(",")[0].strip()
    if _is_loopback_host(host):
        return True

    # Peer address fallback is only relevant for direct (non-proxied) requests.
    transport = getattr(request, "transport", None)
    if transport is not None:
        peer = transport.get_extra_info("peername")
        if isinstance(peer, tuple) and peer:
            peer_host = str(peer[0]).strip()
            if _is_loopback_host(peer_host):
                return True
    return False


class AnalyticsV2Mixin:
    """Mixin providing v2 analytics API endpoints for the dashboard."""

    # Reusable SQL: filter out sessions where Twitch API returned 0 followers (missing token)
    _FOLLOWER_DELTA_SUM = """SUM(CASE WHEN s.follower_delta IS NOT NULL
         AND NOT (s.followers_end = 0 AND s.followers_start > 0)
         THEN s.follower_delta ELSE 0 END)"""
    _FOLLOWER_DELTA_AVG = """AVG(CASE WHEN s.follower_delta IS NOT NULL
         AND NOT (s.followers_end = 0 AND s.followers_start > 0)
         THEN s.follower_delta ELSE NULL END)"""

    def _get_dashboard_session(self, request: web.Request) -> Optional[Dict[str, Any]]:
        getter = getattr(self, "_get_dashboard_auth_session", None)
        if not callable(getter):
            return None
        try:
            session = getter(request)
        except Exception:
            log.debug("Could not resolve dashboard OAuth session", exc_info=True)
            return None
        return session if isinstance(session, dict) else None

    @staticmethod
    def _normalize_dashboard_next_path(raw_path: Optional[str]) -> str:
        fallback = "/twitch/dashboard-v2"
        candidate = (raw_path or "").strip()
        if not candidate:
            return fallback
        try:
            parts = urlsplit(candidate)
        except Exception:
            return fallback
        if parts.scheme or parts.netloc:
            return fallback
        if not candidate.startswith("/") or not candidate.startswith("/twitch"):
            return fallback
        return candidate

    @staticmethod
    def _safe_internal_login_redirect(candidate: Optional[str]) -> str:
        fallback = "/twitch/auth/login?next=%2Ftwitch%2Fdashboard-v2"
        value = (candidate or "").strip()
        if not value:
            return fallback
        try:
            parts = urlsplit(value)
        except Exception:
            return fallback
        if parts.scheme or parts.netloc:
            return fallback
        if not value.startswith("/"):
            return fallback
        return value

    def _get_dashboard_login_url(self, request: web.Request) -> str:
        builder = getattr(self, "_build_dashboard_login_url", None)
        if callable(builder):
            try:
                url = builder(request)
                if url:
                    return self._safe_internal_login_redirect(str(url))
            except Exception:
                log.debug("Could not build dashboard login URL via host class", exc_info=True)
        next_path = self._normalize_dashboard_next_path(
            request.rel_url.path_qs if request.rel_url else "/twitch/dashboard-v2"
        )
        return self._safe_internal_login_redirect(f"/twitch/auth/login?{urlencode({'next': next_path})}")

    def _check_v2_auth(self, request: web.Request) -> bool:
        """Check if request is authorized for v2 API.

        Returns True if:
        - Request is from localhost (no auth needed)
        - noauth mode is enabled
        - Valid Twitch OAuth partner session exists
        - Valid partner_token or admin token is provided
        """
        # Localhost = always allowed (dev mode)
        if _is_localhost(request):
            return True

        # Check noauth mode from parent
        if getattr(self, "_noauth", False):
            return True

        # Twitch OAuth session (partner access)
        if self._get_dashboard_session(request):
            return True

        # Check tokens
        partner_token = getattr(self, "_partner_token", None)
        admin_token = getattr(self, "_token", None)

        partner_header = request.headers.get("X-Partner-Token")
        partner_query = request.query.get("partner_token")
        admin_header = request.headers.get("X-Admin-Token")
        admin_query = request.query.get("token")

        # Partner token check
        if partner_token:
            if partner_header == partner_token or partner_query == partner_token:
                return True

        # Admin token check (admin can access everything)
        if admin_token:
            if admin_header == admin_token or admin_query == admin_token:
                return True

        return False

    def _require_v2_auth(self, request: web.Request):
        """Require authentication for v2 API, but allow localhost."""
        if not self._check_v2_auth(request):
            login_url = self._get_dashboard_login_url(request)
            if request.path.startswith("/twitch/api/"):
                login_url = f"/twitch/auth/login?{urlencode({'next': '/twitch/dashboard-v2'})}"
            payload = {
                "error": "Authentication required. Use Twitch login, partner_token, or access from localhost.",
                "loginUrl": login_url,
            }
            if request.path.startswith("/twitch/api/"):
                raise web.HTTPUnauthorized(text=json.dumps(payload), content_type="application/json")
            raise web.HTTPUnauthorized(text=payload["error"])

    def _get_auth_level(self, request: web.Request) -> str:
        """Get the authentication level for the request.

        Returns:
        - 'localhost': Local development access (full admin)
        - 'admin': Admin token (full access)
        - 'partner': Partner token (partner access)
        - 'none': No authentication
        """
        # Localhost = admin level
        if _is_localhost(request):
            return "localhost"

        # Check noauth mode
        if getattr(self, "_noauth", False):
            return "localhost"

        if self._get_dashboard_session(request):
            return "partner"

        admin_token = getattr(self, "_token", None)
        partner_token = getattr(self, "_partner_token", None)

        admin_header = request.headers.get("X-Admin-Token")
        admin_query = request.query.get("token")
        partner_header = request.headers.get("X-Partner-Token")
        partner_query = request.query.get("partner_token")

        # Admin token = full access
        if admin_token and (admin_header == admin_token or admin_query == admin_token):
            return "admin"

        # Partner token
        if partner_token and (partner_header == partner_token or partner_query == partner_token):
            return "partner"

        return "none"

    def _register_v2_routes(self, router: web.UrlDispatcher) -> None:
        """Register all v2 API routes."""
        router.add_get("/twitch/api/v2/overview", self._api_v2_overview)
        router.add_get("/twitch/api/v2/monthly-stats", self._api_v2_monthly_stats)
        router.add_get("/twitch/api/v2/weekly-stats", self._api_v2_weekly_stats)
        router.add_get("/twitch/api/v2/hourly-heatmap", self._api_v2_hourly_heatmap)
        router.add_get("/twitch/api/v2/calendar-heatmap", self._api_v2_calendar_heatmap)
        router.add_get("/twitch/api/v2/chat-analytics", self._api_v2_chat_analytics)
        router.add_get("/twitch/api/v2/viewer-overlap", self._api_v2_viewer_overlap)
        router.add_get("/twitch/api/v2/tag-analysis", self._api_v2_tag_analysis)
        router.add_get("/twitch/api/v2/rankings", self._api_v2_rankings)
        router.add_get("/twitch/api/v2/category-comparison", self._api_v2_category_comparison)
        router.add_get("/twitch/api/v2/streamers", self._api_v2_streamers)
        router.add_get("/twitch/api/v2/session/{id}", self._api_v2_session_detail)
        router.add_get("/twitch/api/v2/auth-status", self._api_v2_auth_status)
        # New Audience Analytics Endpoints
        router.add_get("/twitch/api/v2/watch-time-distribution", self._api_v2_watch_time_distribution)
        router.add_get("/twitch/api/v2/follower-funnel", self._api_v2_follower_funnel)
        router.add_get("/twitch/api/v2/tag-analysis-extended", self._api_v2_tag_analysis_extended)
        router.add_get("/twitch/api/v2/title-performance", self._api_v2_title_performance)
        router.add_get("/twitch/api/v2/audience-insights", self._api_v2_audience_insights)
        router.add_get("/twitch/api/v2/audience-demographics", self._api_v2_audience_demographics)
        # Stats-Data Endpoints (from twitch_stats_tracked / twitch_stats_category)
        router.add_get("/twitch/api/v2/viewer-timeline", self._api_v2_viewer_timeline)
        router.add_get("/twitch/api/v2/category-leaderboard", self._api_v2_category_leaderboard)
        # Serve the dashboard
        router.add_get("/twitch/dashboard-v2", self._serve_dashboard_v2)
        router.add_get("/twitch/dashboard-v2/{path:.*}", self._serve_dashboard_v2_assets)

    async def _serve_dashboard_v2(self, request: web.Request) -> web.Response:
        """Serve the main dashboard HTML."""
        if not self._check_v2_auth(request):
            raise web.HTTPFound("/twitch/auth/login?next=%2Ftwitch%2Fdashboard-v2")
        import pathlib
        dist_path = pathlib.Path(__file__).parent / "dashboard_v2" / "dist" / "index.html"
        if dist_path.exists():
            return web.FileResponse(dist_path)
        return web.Response(text="Dashboard not built. Run npm run build in dashboard_v2/", status=404)

    async def _serve_dashboard_v2_assets(self, request: web.Request) -> web.Response:
        """Serve static assets for the dashboard."""
        import pathlib
        raw_path = request.match_info.get("path", "")
        if not raw_path:
            return web.Response(text="Not found", status=404)

        dist_root = (pathlib.Path(__file__).resolve().parent / "dashboard_v2" / "dist").resolve()
        candidate: pathlib.Path = dist_root

        # Resolve each path segment against actual directory entries to avoid
        # using untrusted input directly in filesystem path expressions.
        for segment in raw_path.split("/"):
            if not segment or segment in {".", ".."} or "\\" in segment:
                return web.Response(text="Not found", status=404)
            if not candidate.is_dir():
                return web.Response(text="Not found", status=404)

            next_candidate = None
            for entry in candidate.iterdir():
                if entry.name == segment:
                    next_candidate = entry
                    break
            if next_candidate is None:
                return web.Response(text="Not found", status=404)
            candidate = next_candidate

        try:
            candidate.resolve().relative_to(dist_root)
        except ValueError:
            return web.Response(text="Not found", status=404)

        if candidate.is_file():
            return web.FileResponse(candidate)
        return web.Response(text="Not found", status=404)

    async def _api_v2_overview(self, request: web.Request) -> web.Response:
        """Main overview endpoint with all dashboard data."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip() or None
        days = min(max(int(request.query.get("days", "30")), 7), 365)

        try:
            data = await self._get_overview_data(streamer, days)
            return web.json_response(data)
        except Exception as exc:
            log.exception("Error in overview API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _get_overview_data(self, streamer: Optional[str], days: int) -> Dict[str, Any]:
        """Get comprehensive overview data for the dashboard."""
        with storage.get_conn() as conn:
            since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            prev_since_date = (datetime.now(timezone.utc) - timedelta(days=days * 2)).isoformat()

            # Build WHERE clause
            if streamer:
                where = "AND LOWER(s.streamer_login) = ?"
                params = [since_date, streamer.lower()]
                prev_params = [prev_since_date, since_date, streamer.lower()]
            else:
                where = ""
                params = [since_date]
                prev_params = [prev_since_date, since_date]

            # Check data exists
            count = conn.execute(  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                f"SELECT COUNT(*) FROM twitch_stream_sessions s WHERE s.started_at >= ? AND s.ended_at IS NOT NULL {where}",  # nosec B608
                params
            ).fetchone()[0]

            if count == 0:
                return {"empty": True, "error": "Keine Daten für den Zeitraum"}

            # Get sessions
            sessions = self._get_sessions(conn, since_date, streamer, 50)

            # Calculate metrics
            metrics = self._calculate_overview_metrics(conn, since_date, streamer)

            # Calculate previous period metrics for trends
            prev_where = where.replace("s.started_at >= ?", "s.started_at >= ? AND s.started_at < ?")
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
            prev_metrics = conn.execute(f"""
                SELECT
                    AVG(s.avg_viewers) as avg_viewers,
                    {self._FOLLOWER_DELTA_SUM} as followers,
                    AVG(CASE
                        WHEN s.avg_viewers >= 3 AND s.peak_viewers > 0
                        THEN MIN(s.retention_10m, s.avg_viewers * 1.0 / s.peak_viewers, 1.0)
                        ELSE NULL
                    END) as retention
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ? AND s.started_at < ? AND s.ended_at IS NOT NULL {where}
            """, prev_params).fetchone()

            # Calculate trends
            def calc_trend(curr, prev):
                if not prev or prev == 0:
                    return None
                return round(((curr - prev) / abs(prev)) * 100, 1)

            prev_avg = float(prev_metrics[0]) if prev_metrics and prev_metrics[0] else 0
            prev_fol = int(prev_metrics[1]) if prev_metrics and prev_metrics[1] else 0
            prev_ret = float(prev_metrics[2]) * 100 if prev_metrics and prev_metrics[2] else 0

            avg_viewers_trend = calc_trend(metrics.get("avg_avg_viewers", 0), prev_avg)
            # Follower trend: use abs(prev) to avoid inversion with negative base
            followers_trend = calc_trend(metrics.get("total_followers", 0), prev_fol)
            retention_trend = calc_trend(metrics.get("avg_retention_10m", 0), prev_ret)

            # Calculate category percentile for health score
            category_percentile = None
            category_rank = None
            category_total = None
            if streamer:
                cat_data = self._get_category_percentiles(conn, since_date)
                if cat_data["sorted_avgs"]:
                    streamer_avg = cat_data["streamer_map"].get(streamer.lower())
                    if streamer_avg is not None:
                        category_percentile = self._percentile_of(cat_data["sorted_avgs"], streamer_avg)
                        category_total = cat_data["total"]
                        # Rank = total - position (1 = best)
                        category_rank = category_total - int(category_percentile * category_total)

            # Calculate scores
            scores = self._calculate_health_scores(metrics, category_percentile)

            # Generate insights
            findings = self._generate_insights(metrics)
            actions = self._generate_actions(metrics)

            # Get network stats
            network = self._get_network_stats(conn, since_date, streamer)

            # Correlations
            correlations = self._calculate_correlations(sessions)

            result: Dict[str, Any] = {
                "streamer": streamer,
                "days": days,
                "scores": scores,
                "summary": {
                    "avgViewers": metrics.get("avg_avg_viewers", 0),
                    "peakViewers": metrics.get("max_peak_viewers", 0),
                    "totalHoursWatched": metrics.get("total_hours_watched", 0),
                    "totalAirtime": metrics.get("total_airtime_hours", 0),
                    "followersDelta": metrics.get("total_followers", 0),
                    "followersGained": metrics.get("gained_followers", 0),
                    "followersPerHour": metrics.get("followers_per_hour", 0),
                    "followersGainedPerHour": metrics.get("gained_followers_per_hour", 0),
                    "retention10m": metrics.get("avg_retention_10m", 0),
                    "retentionReliable": metrics.get("retention_sample_count", 0) >= 3,
                    "uniqueChatters": metrics.get("total_unique_chatters", 0),
                    "streamCount": count,
                    # Trend indicators
                    "avgViewersTrend": avg_viewers_trend,
                    "followersTrend": followers_trend,
                    "retentionTrend": retention_trend,
                },
                "sessions": sessions,
                "findings": findings,
                "actions": actions,
                "correlations": correlations,
                "network": network,
            }
            if category_rank is not None:
                result["categoryRank"] = category_rank
                result["categoryTotal"] = category_total
            return result

    def _get_sessions(self, conn, since_date: str, streamer: Optional[str], limit: int = 50) -> List[Dict]:
        """Get list of sessions with metrics."""
        where = "AND LOWER(s.streamer_login) = ?" if streamer else ""
        params = [since_date, streamer.lower()] if streamer else [since_date]

        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
        rows = conn.execute(f"""
            SELECT
                s.id, DATE(s.started_at), TIME(s.started_at), s.duration_seconds,
                s.start_viewers, s.peak_viewers, s.end_viewers, s.avg_viewers,
                COALESCE(s.retention_5m, 0), COALESCE(s.retention_10m, 0), COALESCE(s.retention_20m, 0),
                COALESCE(s.dropoff_pct, 0), COALESCE(s.unique_chatters, 0),
                COALESCE(s.first_time_chatters, 0), COALESCE(s.returning_chatters, 0),
                COALESCE(s.followers_start, 0), COALESCE(s.followers_end, 0),
                COALESCE(s.stream_title, '')
            FROM twitch_stream_sessions s
            WHERE s.started_at >= ? AND s.ended_at IS NOT NULL {where}
            ORDER BY s.started_at DESC
            LIMIT {limit}
        """, params).fetchall()

        sessions: List[Dict[str, Any]] = []
        for r in rows:
            peak_viewers = int(r[5]) if r[5] else 0
            avg_viewers = float(r[7]) if r[7] else 0.0
            retention_cap = min(1.0, max(0.0, (avg_viewers / peak_viewers))) if peak_viewers > 0 else 1.0

            raw_ret_5m = float(r[8]) if r[8] else 0.0
            raw_ret_10m = float(r[9]) if r[9] else 0.0
            raw_ret_20m = float(r[10]) if r[10] else 0.0
            ret_5m = max(0.0, min(raw_ret_5m, retention_cap))
            ret_10m = max(0.0, min(raw_ret_10m, retention_cap))
            ret_20m = max(0.0, min(raw_ret_20m, retention_cap))

            sessions.append({
                "id": r[0],
                "date": r[1] or "",
                "startTime": r[2] or "",
                "duration": r[3] or 0,
                "startViewers": r[4] or 0,
                "peakViewers": peak_viewers,
                "endViewers": r[6] or 0,
                "avgViewers": avg_viewers,
                "retention5m": ret_5m * 100,
                "retention10m": ret_10m * 100,
                "retention20m": ret_20m * 100,
                "dropoffPct": float(r[11]) * 100 if r[11] else 0,
                "uniqueChatters": r[12] or 0,
                "firstTimeChatters": r[13] or 0,
                "returningChatters": r[14] or 0,
                "followersStart": r[15] or 0,
                "followersEnd": r[16] or 0,
                "title": r[17] or "",
            })

        return sessions

    def _calculate_overview_metrics(self, conn, since_date: str, streamer: Optional[str]) -> Dict[str, Any]:
        """Calculate all overview metrics."""
        where = "AND LOWER(s.streamer_login) = ?" if streamer else ""
        params = [since_date, streamer.lower()] if streamer else [since_date]

        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
        row = conn.execute(f"""
            SELECT
                AVG(s.avg_viewers) as avg_avg_viewers,
                MAX(s.peak_viewers) as max_peak_viewers,
                SUM(s.avg_viewers * s.duration_seconds / 3600.0) as total_hours_watched,
                SUM(s.duration_seconds / 3600.0) as total_airtime_hours,
                {self._FOLLOWER_DELTA_SUM} as total_followers,
                AVG(CASE
                    WHEN s.avg_viewers >= 3 AND s.peak_viewers > 0
                    THEN MIN(s.retention_5m, s.avg_viewers * 1.0 / s.peak_viewers, 1.0)
                    ELSE NULL
                END) as avg_retention_5m,
                AVG(CASE
                    WHEN s.avg_viewers >= 3 AND s.peak_viewers > 0
                    THEN MIN(s.retention_10m, s.avg_viewers * 1.0 / s.peak_viewers, 1.0)
                    ELSE NULL
                END) as avg_retention_10m,
                AVG(CASE
                    WHEN s.avg_viewers >= 3 AND s.peak_viewers > 0
                    THEN MIN(s.retention_20m, s.avg_viewers * 1.0 / s.peak_viewers, 1.0)
                    ELSE NULL
                END) as avg_retention_20m,
                AVG(s.dropoff_pct) as avg_dropoff,
                SUM(s.unique_chatters) as total_unique_chatters,
                AVG(CASE
                    WHEN s.avg_viewers >= 3 AND s.peak_viewers > 0
                    THEN MIN(s.unique_chatters * 100.0 / s.peak_viewers, 100.0)
                    ELSE NULL
                END) as chat_per_100
            FROM twitch_stream_sessions s
            WHERE s.started_at >= ? AND s.ended_at IS NOT NULL {where}
        """, params).fetchone()

        total_airtime = float(row[3]) if row[3] else 0
        total_followers = int(row[4]) if row[4] else 0  # NET (can be negative)

        # Gained followers = only positive session deltas (ignores unfollows)
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
        gained_row = conn.execute(f"""
            SELECT COALESCE(SUM(CASE WHEN s.follower_delta > 0
                 AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                 THEN s.follower_delta ELSE 0 END), 0)
            FROM twitch_stream_sessions s
            WHERE s.started_at >= ? AND s.ended_at IS NOT NULL {where}
        """, params).fetchone()
        gained_followers = int(gained_row[0]) if gained_row and gained_row[0] else 0

        # True unique chatters from rollup table (not SUM of per-session counts)
        unique_chatters_sum = int(row[9]) if row[9] else 0
        if streamer:
            true_unique = conn.execute("""
                SELECT COUNT(DISTINCT chatter_login)
                FROM twitch_chatter_rollup
                WHERE LOWER(streamer_login) = ?
            """, [streamer.lower()]).fetchone()
            unique_chatters = int(true_unique[0]) if true_unique and true_unique[0] else unique_chatters_sum
        else:
            unique_chatters = unique_chatters_sum

        # Sample counts for data quality gating
        # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
        sample_row = conn.execute(f"""
            SELECT
                COUNT(CASE
                    WHEN s.avg_viewers >= 3 AND s.peak_viewers > 0 AND s.retention_10m IS NOT NULL THEN 1
                END),
                COUNT(CASE
                    WHEN s.avg_viewers >= 3 AND s.peak_viewers > 0 AND s.unique_chatters IS NOT NULL THEN 1
                END),
                COUNT(CASE WHEN s.follower_delta IS NOT NULL
                     AND NOT (s.followers_end = 0 AND s.followers_start > 0) THEN 1 END)
            FROM twitch_stream_sessions s
            WHERE s.started_at >= ? AND s.ended_at IS NOT NULL {where}
        """, params).fetchone()
        retention_sample_count = int(sample_row[0]) if sample_row else 0
        chat_sample_count = int(sample_row[1]) if sample_row else 0
        follower_valid_count = int(sample_row[2]) if sample_row else 0

        return {
            "avg_avg_viewers": float(row[0]) if row[0] else 0,
            "max_peak_viewers": int(row[1]) if row[1] else 0,
            "total_hours_watched": float(row[2]) if row[2] else 0,
            "total_airtime_hours": total_airtime,
            "total_followers": total_followers,
            "gained_followers": gained_followers,
            "followers_per_hour": total_followers / total_airtime if total_airtime > 0 else 0,
            "gained_followers_per_hour": gained_followers / total_airtime if total_airtime > 0 else 0,
            "avg_retention_5m": float(row[5]) * 100 if row[5] else 0,
            "avg_retention_10m": float(row[6]) * 100 if row[6] else 0,
            "avg_retention_20m": float(row[7]) * 100 if row[7] else 0,
            "avg_dropoff": float(row[8]) * 100 if row[8] else 0,
            "total_unique_chatters": unique_chatters,
            "chat_per_100": float(row[10]) if row[10] else 0,
            "retention_sample_count": retention_sample_count,
            "chat_sample_count": chat_sample_count,
            "follower_valid_count": follower_valid_count,
        }

    def _get_category_percentiles(self, conn, since_date: str) -> Dict[str, Any]:
        """Get per-streamer AVG viewer_count from stats_category and compute percentiles."""
        rows = conn.execute("""
            SELECT streamer, AVG(viewer_count) as avg_vc
            FROM twitch_stats_category
            WHERE ts_utc >= ?
            GROUP BY streamer
            ORDER BY avg_vc
        """, [since_date]).fetchall()

        if not rows:
            return {"sorted_avgs": [], "streamer_map": {}, "total": 0}

        sorted_avgs = [float(r[1]) for r in rows]
        streamer_map = {r[0].lower(): float(r[1]) for r in rows}
        return {"sorted_avgs": sorted_avgs, "streamer_map": streamer_map, "total": len(rows)}

    def _percentile_of(self, sorted_avgs: List[float], value: float) -> float:
        """Return the percentile (0-1) of value within sorted_avgs."""
        if not sorted_avgs:
            return 0.5
        below = sum(1 for v in sorted_avgs if v < value)
        return below / len(sorted_avgs)

    def _calculate_health_scores(self, metrics: Dict[str, Any], category_percentile: Optional[float] = None) -> Dict[str, int]:
        """Calculate health scores from metrics."""
        avg_viewers = metrics.get("avg_avg_viewers", 0)

        # Reach: Based on percentile ranking in category if available, else fallback
        if category_percentile is not None:
            reach = min(100, int(20 + category_percentile * 80))
        else:
            reach = min(100, int(avg_viewers / 5))  # fallback

        # Retention: Based on 10m retention (neutral if insufficient data)
        ret_10m = metrics.get("avg_retention_10m", 0)
        if metrics.get("retention_sample_count", 0) < 3:
            retention = 50
        else:
            retention = min(100, int(ret_10m * 1.5))  # 66% = 100

        # Engagement: Based on chat per 100 viewers (neutral if insufficient data)
        chat_100 = metrics.get("chat_per_100", 0)
        if metrics.get("chat_sample_count", 0) < 3:
            engagement = 50
        else:
            engagement = min(100, int(chat_100 * 3))  # ~33 chatters/100 peak-viewer = 100

        # Growth: Based on followers per hour (floor at 0, negative fph = 0 growth)
        fph = max(0, metrics.get("followers_per_hour", 0))
        growth = min(100, int(fph * 20))  # 5 fph = 100

        # Monetization: Placeholder (would need sub data)
        monetization = min(100, max(0, int(avg_viewers / 10)))

        # Network: Placeholder
        network = 50

        # Total: Weighted average
        total = int(
            reach * 0.2 +
            retention * 0.25 +
            engagement * 0.2 +
            growth * 0.15 +
            monetization * 0.1 +
            network * 0.1
        )

        return {
            "total": total,
            "reach": reach,
            "retention": retention,
            "engagement": engagement,
            "growth": growth,
            "monetization": monetization,
            "network": network,
        }

    def _generate_insights(self, metrics: Dict[str, Any]) -> List[Dict[str, str]]:
        """Generate findings/insights from metrics."""
        insights = []

        # Retention
        ret_10m = metrics.get("avg_retention_10m", 0)
        if metrics.get("retention_sample_count", 0) < 3:
            insights.append({
                "type": "info",
                "title": "Retention-Daten unzureichend",
                "text": "Zu wenige Sessions mit ≥3 Viewern für aussagekräftige Retention-Werte."
            })
        elif ret_10m < 40:
            insights.append({
                "type": "neg",
                "title": "Niedrige Retention",
                "text": f"10-Min Retention bei {ret_10m:.1f}%. Verbessere den Stream-Einstieg."
            })
        elif ret_10m > 65:
            insights.append({
                "type": "pos",
                "title": "Starke Retention",
                "text": f"Exzellente {ret_10m:.1f}% Retention. Dein Content fesselt!"
            })

        # Chat
        chat_100 = metrics.get("chat_per_100", 0)
        if metrics.get("chat_sample_count", 0) < 3:
            insights.append({
                "type": "info",
                "title": "Chat-Daten unzureichend",
                "text": "Zu wenige Sessions mit ≥3 Viewern für aussagekräftige Chat-Metriken."
            })
        elif chat_100 < 5:
            insights.append({
                "type": "warn",
                "title": "Niedrige Chat-Aktivität",
                "text": f"Nur {chat_100:.1f} Chatter/100 Peak-Viewer (Proxy). Mehr Interaktion fördern!"
            })
        elif chat_100 > 30:
            insights.append({
                "type": "pos",
                "title": "Aktive Community",
                "text": f"{chat_100:.1f} Chatter/100 Peak-Viewer (Proxy) - sehr engagiert!"
            })

        # Followers (skip when no valid follower data)
        fph = metrics.get("followers_per_hour", 0)
        gained_fph = metrics.get("gained_followers_per_hour", 0)
        follower_data_valid = metrics.get("follower_valid_count", 0) > 0
        if not follower_data_valid:
            pass  # No reliable follower data — skip all follower insights
        elif fph < 0:
            insights.append({
                "type": "neg",
                "title": "Follower-Verlust",
                "text": f"Netto {fph:.2f} Follower/Stunde ({metrics.get('total_followers', 0):+d} gesamt). "
                        f"Gewonnen: {gained_fph:.2f}/h. Unfollows überwiegen."
            })
        elif fph < 0.5:
            insights.append({
                "type": "warn",
                "title": "Langsames Follower-Wachstum",
                "text": f"Nur {fph:.2f} Follower/Stunde. Regelmäßig an Follows erinnern!"
            })
        elif fph > 3:
            insights.append({
                "type": "pos",
                "title": "Starkes Wachstum",
                "text": f"{fph:.1f} Follower/Stunde - ausgezeichnet!"
            })

        return insights

    def _generate_actions(self, metrics: Dict[str, Any]) -> List[Dict[str, str]]:
        """Generate action recommendations."""
        actions = []

        ret_10m = metrics.get("avg_retention_10m", 0)
        if metrics.get("retention_sample_count", 0) >= 3 and ret_10m < 50:
            actions.append({
                "tag": "Retention",
                "text": "Starte mit einem starken Hook in den ersten 2 Minuten.",
                "priority": "high"
            })

        chat_100 = metrics.get("chat_per_100", 0)
        if metrics.get("chat_sample_count", 0) >= 3 and chat_100 < 10:
            actions.append({
                "tag": "Engagement",
                "text": "Stelle alle 5-10 Minuten eine direkte Frage an den Chat.",
                "priority": "medium"
            })

        fph = metrics.get("followers_per_hour", 0)
        follower_data_valid = metrics.get("follower_valid_count", 0) > 0
        if follower_data_valid and fph < 0:
            actions.append({
                "tag": "Growth",
                "text": "Follower-Verlust! Prüfe ob Content-Wechsel oder lange Pausen Unfollows verursachen.",
                "priority": "high"
            })
        elif follower_data_valid and fph < 1:
            actions.append({
                "tag": "Growth",
                "text": "Erinnere alle 20-30 Minuten an Follow mit konkretem Grund.",
                "priority": "medium"
            })

        return actions

    def _get_network_stats(self, conn, since_date: str, streamer: Optional[str]) -> Dict[str, int]:
        """Get raid network statistics."""
        if not streamer:
            return {"sent": 0, "received": 0, "sentViewers": 0}

        sent = conn.execute("""
            SELECT COUNT(*), COALESCE(SUM(viewer_count), 0)
            FROM twitch_raid_history
            WHERE LOWER(from_broadcaster_login) = ? AND executed_at >= ? AND success = 1
        """, [streamer.lower(), since_date]).fetchone()

        received = conn.execute("""
            SELECT COUNT(*)
            FROM twitch_raid_history
            WHERE LOWER(to_broadcaster_login) = ? AND executed_at >= ? AND success = 1
        """, [streamer.lower(), since_date]).fetchone()

        return {
            "sent": sent[0] if sent else 0,
            "sentViewers": int(sent[1]) if sent else 0,
            "received": received[0] if received else 0,
        }

    def _calculate_correlations(self, sessions: List[Dict]) -> Dict[str, float]:
        """Calculate metric correlations."""
        if len(sessions) < 3:
            return {"durationVsViewers": 0, "chatVsRetention": 0}

        # Simple correlation approximation
        durations = [s["duration"] for s in sessions]
        viewers = [s["avgViewers"] for s in sessions]
        chatters = [s["uniqueChatters"] for s in sessions]
        retention = [s["retention10m"] for s in sessions]

        def corr(a, b):
            if len(a) < 2:
                return 0
            mean_a = sum(a) / len(a)
            mean_b = sum(b) / len(b)
            num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
            den_a = sum((x - mean_a) ** 2 for x in a) ** 0.5
            den_b = sum((y - mean_b) ** 2 for y in b) ** 0.5
            if den_a == 0 or den_b == 0:
                return 0
            return round(num / (den_a * den_b), 2)

        return {
            "durationVsViewers": corr(durations, viewers),
            "chatVsRetention": corr(chatters, retention),
        }

    async def _api_v2_hourly_heatmap(self, request: web.Request) -> web.Response:
        """Get hourly heatmap data."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip() or None
        days = min(max(int(request.query.get("days", "30")), 7), 365)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                where = "AND LOWER(s.streamer_login) = ?" if streamer else ""
                params = [since_date, streamer.lower()] if streamer else [since_date]

                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                rows = conn.execute(f"""
                    SELECT
                        CAST(strftime('%w', s.started_at) AS INTEGER) as weekday,
                        CAST(strftime('%H', s.started_at) AS INTEGER) as hour,
                        COUNT(*) as stream_count,
                        AVG(s.avg_viewers) as avg_viewers,
                        AVG(s.peak_viewers) as avg_peak
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL {where}
                    GROUP BY weekday, hour
                """, params).fetchall()

                data = [
                    {
                        "weekday": r[0],
                        "hour": r[1],
                        "streamCount": r[2],
                        "avgViewers": float(r[3]) if r[3] else 0,
                        "avgPeak": float(r[4]) if r[4] else 0,
                    }
                    for r in rows
                ]

                return web.json_response(data)
        except Exception as exc:
            log.exception("Error in hourly heatmap API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_calendar_heatmap(self, request: web.Request) -> web.Response:
        """Get calendar heatmap data."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip() or None
        days = min(max(int(request.query.get("days", "365")), 30), 365)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                where = "AND LOWER(s.streamer_login) = ?" if streamer else ""
                params = [since_date, streamer.lower()] if streamer else [since_date]

                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                rows = conn.execute(f"""
                    SELECT
                        DATE(s.started_at) as date,
                        COUNT(*) as stream_count,
                        SUM(s.avg_viewers * s.duration_seconds / 3600.0) as hours_watched
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL {where}
                    GROUP BY DATE(s.started_at)
                """, params).fetchall()

                data = [
                    {
                        "date": r[0],
                        "streamCount": r[1],
                        "hoursWatched": float(r[2]) if r[2] else 0,
                        "value": float(r[2]) if r[2] else 0,
                    }
                    for r in rows
                ]

                return web.json_response(data)
        except Exception as exc:
            log.exception("Error in calendar heatmap API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_monthly_stats(self, request: web.Request) -> web.Response:
        """Get monthly aggregated stats."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip() or None
        months = min(max(int(request.query.get("months", "12")), 1), 24)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=months * 30)).isoformat()
                where = "AND LOWER(s.streamer_login) = ?" if streamer else ""
                params = [since_date, streamer.lower()] if streamer else [since_date]

                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                rows = conn.execute(f"""
                    SELECT
                        CAST(strftime('%Y', s.started_at) AS INTEGER) as year,
                        CAST(strftime('%m', s.started_at) AS INTEGER) as month,
                        SUM(s.avg_viewers * s.duration_seconds / 3600.0) as hours_watched,
                        SUM(s.duration_seconds / 3600.0) as airtime,
                        AVG(s.avg_viewers) as avg_viewers,
                        MAX(s.peak_viewers) as peak_viewers,
                        {self._FOLLOWER_DELTA_SUM} as follower_delta,
                        SUM(s.unique_chatters) as unique_chatters,
                        COUNT(*) as stream_count
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL {where}
                    GROUP BY year, month
                    ORDER BY year DESC, month DESC
                """, params).fetchall()

                month_names = ["", "Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
                data = [
                    {
                        "year": r[0],
                        "month": r[1],
                        "monthLabel": month_names[r[1]] if r[1] else "",
                        "totalHoursWatched": float(r[2]) if r[2] else 0,
                        "totalAirtime": float(r[3]) if r[3] else 0,
                        "avgViewers": float(r[4]) if r[4] else 0,
                        "peakViewers": int(r[5]) if r[5] else 0,
                        "followerDelta": int(r[6]) if r[6] else 0,
                        "uniqueChatters": int(r[7]) if r[7] else 0,
                        "streamCount": r[8],
                    }
                    for r in rows
                ]

                return web.json_response(data)
        except Exception as exc:
            log.exception("Error in monthly stats API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_weekly_stats(self, request: web.Request) -> web.Response:
        """Get weekday analysis stats."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip() or None
        days = min(max(int(request.query.get("days", "30")), 7), 365)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                where = "AND LOWER(s.streamer_login) = ?" if streamer else ""
                params = [since_date, streamer.lower()] if streamer else [since_date]

                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                rows = conn.execute(f"""
                    SELECT
                        CAST(strftime('%w', s.started_at) AS INTEGER) as weekday,
                        COUNT(*) as stream_count,
                        AVG(s.duration_seconds / 3600.0) as avg_hours,
                        AVG(s.avg_viewers) as avg_viewers,
                        AVG(s.peak_viewers) as avg_peak,
                        {self._FOLLOWER_DELTA_SUM} as total_followers
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL {where}
                    GROUP BY weekday
                    ORDER BY weekday
                """, params).fetchall()

                weekday_names = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"]
                data = [
                    {
                        "weekday": r[0],
                        "weekdayLabel": weekday_names[r[0]] if r[0] is not None else "",
                        "streamCount": r[1],
                        "avgHours": float(r[2]) if r[2] else 0,
                        "avgViewers": float(r[3]) if r[3] else 0,
                        "avgPeak": float(r[4]) if r[4] else 0,
                        "totalFollowers": int(r[5]) if r[5] else 0,
                    }
                    for r in rows
                ]

                return web.json_response(data)
        except Exception as exc:
            log.exception("Error in weekly stats API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_chat_analytics(self, request: web.Request) -> web.Response:
        """Get chat analytics."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip() or None
        days = min(max(int(request.query.get("days", "30")), 7), 365)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

                if not streamer:
                    return web.json_response({"error": "Streamer required"}, status=400)

                # Aggregate chat stats
                stats = conn.execute("""
                    SELECT
                        SUM(s.unique_chatters) as total_unique,
                        SUM(s.first_time_chatters) as total_first,
                        SUM(s.returning_chatters) as total_returning
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """, [since_date, streamer.lower()]).fetchone()

                # Top chatters
                top = conn.execute("""
                    SELECT
                        chatter_login,
                        SUM(total_messages) as messages,
                        SUM(total_sessions) as sessions,
                        MIN(first_seen_at) as first_seen,
                        MAX(last_seen_at) as last_seen
                    FROM twitch_chatter_rollup
                    WHERE LOWER(streamer_login) = ?
                    GROUP BY chatter_login
                    ORDER BY messages DESC
                    LIMIT 20
                """, [streamer.lower()]).fetchall()

                return web.json_response({
                    "totalMessages": 0,  # Would need message count
                    "uniqueChatters": int(stats[0]) if stats[0] else 0,
                    "firstTimeChatters": int(stats[1]) if stats[1] else 0,
                    "returningChatters": int(stats[2]) if stats[2] else 0,
                    "messagesPerMinute": 0,
                    "chatterReturnRate": (int(stats[2]) / int(stats[0]) * 100) if stats[0] else 0,
                    "topChatters": [
                        {
                            "login": r[0],
                            "totalMessages": r[1],
                            "totalSessions": r[2],
                            "firstSeen": r[3],
                            "lastSeen": r[4],
                            "loyaltyScore": min(100, r[2] * 10),  # Simplified
                        }
                        for r in top
                    ]
                })
        except Exception as exc:
            log.exception("Error in chat analytics API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_viewer_overlap(self, request: web.Request) -> web.Response:
        """Get viewer overlap with other channels."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip()
        limit = min(max(int(request.query.get("limit", "20")), 5), 50)

        if not streamer:
            return web.json_response({"error": "Streamer required"}, status=400)

        try:
            with storage.get_conn() as conn:
                # Find shared chatters
                rows = conn.execute("""
                    SELECT
                        c2.streamer_login as other_streamer,
                        COUNT(DISTINCT c1.chatter_login) as shared_chatters
                    FROM twitch_chatter_rollup c1
                    JOIN twitch_chatter_rollup c2 ON c1.chatter_login = c2.chatter_login
                    WHERE LOWER(c1.streamer_login) = ?
                      AND LOWER(c2.streamer_login) != ?
                    GROUP BY c2.streamer_login
                    ORDER BY shared_chatters DESC
                    LIMIT ?
                """, [streamer.lower(), streamer.lower(), limit]).fetchall()

                # Get total chatters for percentage
                total = conn.execute("""
                    SELECT COUNT(DISTINCT chatter_login)
                    FROM twitch_chatter_rollup
                    WHERE LOWER(streamer_login) = ?
                """, [streamer.lower()]).fetchone()[0] or 1

                data = [
                    {
                        "streamerA": streamer,
                        "streamerB": r[0],
                        "sharedChatters": r[1],
                        "totalChattersA": total,
                        "totalChattersB": 0,  # Would need separate query
                        "overlapPercentage": round(r[1] / total * 100, 1),
                    }
                    for r in rows
                ]

                return web.json_response(data)
        except Exception as exc:
            log.exception("Error in viewer overlap API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_tag_analysis(self, request: web.Request) -> web.Response:
        """Get tag performance analysis."""
        self._require_v2_auth(request)

        days = min(max(int(request.query.get("days", "30")), 7), 365)
        limit = min(max(int(request.query.get("limit", "30")), 5), 100)

        try:
            # Tags are stored as JSON in the tags column
            # This is a simplified version - full implementation would parse JSON
            return web.json_response([])
        except Exception as exc:
            log.exception("Error in tag analysis API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_rankings(self, request: web.Request) -> web.Response:
        """Get streamer rankings."""
        self._require_v2_auth(request)

        metric = request.query.get("metric", "viewers")
        days = min(max(int(request.query.get("days", "30")), 7), 365)
        limit = min(max(int(request.query.get("limit", "20")), 5), 50)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

                if metric == "viewers":
                    order_by = "AVG(s.avg_viewers)"
                elif metric == "retention":
                    order_by = "AVG(s.retention_10m)"
                elif metric == "growth":
                    order_by = self._FOLLOWER_DELTA_SUM
                else:
                    order_by = "AVG(s.avg_viewers)"

                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                rows = conn.execute(f"""
                    SELECT
                        s.streamer_login,
                        {order_by} as value
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL
                    GROUP BY s.streamer_login
                    HAVING COUNT(*) >= 3
                    ORDER BY value DESC
                    LIMIT ?
                """, [since_date, limit]).fetchall()

                data = [
                    {
                        "rank": i + 1,
                        "login": r[0],
                        "value": (float(r[1]) * 100 if metric == "retention" else float(r[1])) if r[1] else 0,
                        "trend": "same",
                        "trendValue": 0,
                    }
                    for i, r in enumerate(rows)
                ]

                return web.json_response(data)
        except Exception as exc:
            log.exception("Error in rankings API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_category_comparison(self, request: web.Request) -> web.Response:
        """Compare streamer to category averages."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip()
        days = min(max(int(request.query.get("days", "30")), 7), 365)

        if not streamer:
            return web.json_response({"error": "Streamer required"}, status=400)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

                # Your stats from stats_tracked (higher accuracy for tracked streamers)
                your_tracked = conn.execute("""
                    SELECT AVG(viewer_count), MAX(viewer_count)
                    FROM twitch_stats_tracked
                    WHERE ts_utc >= ? AND LOWER(streamer) = ?
                """, [since_date, streamer.lower()]).fetchone()

                # Fallback to session data
                your_session = conn.execute("""
                    SELECT
                        AVG(s.avg_viewers) as avg_viewers,
                        MAX(s.peak_viewers) as peak_viewers,
                        AVG(s.retention_10m) as retention10m,
                        AVG(CASE WHEN s.avg_viewers > 0 THEN s.unique_chatters * 100.0 / s.avg_viewers ELSE 0 END) as chat_health
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """, [since_date, streamer.lower()]).fetchone()

                # Use tracked data if available, else session data
                your_avg = float(your_tracked[0]) if your_tracked and your_tracked[0] else (float(your_session[0]) if your_session and your_session[0] else 0)
                your_peak = int(your_tracked[1]) if your_tracked and your_tracked[1] else (int(your_session[1]) if your_session and your_session[1] else 0)
                your_ret = float(your_session[2]) * 100 if your_session and your_session[2] else 0
                your_chat = float(your_session[3]) if your_session and your_session[3] else 0

                # Category stats from stats_category (per-streamer aggregates)
                cat_data = self._get_category_percentiles(conn, since_date)
                sorted_avgs = cat_data["sorted_avgs"]
                category_total = cat_data["total"]

                # Category averages
                cat_avg_viewers = sum(sorted_avgs) / len(sorted_avgs) if sorted_avgs else 0

                # Peak viewers per streamer from category
                cat_peak = conn.execute("""
                    SELECT AVG(max_vc) FROM (
                        SELECT MAX(viewer_count) as max_vc
                        FROM twitch_stats_category
                        WHERE ts_utc >= ?
                        GROUP BY streamer
                    )
                """, [since_date]).fetchone()
                cat_avg_peak = float(cat_peak[0]) if cat_peak and cat_peak[0] else 0

                # Category-wide retention and chat health from session data
                cat_session_avgs = conn.execute("""
                    SELECT
                        AVG(s.retention_10m) as avg_ret,
                        AVG(CASE WHEN s.avg_viewers > 0 THEN s.unique_chatters * 100.0 / s.avg_viewers ELSE 0 END) as avg_chat
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL
                """, [since_date]).fetchone()
                cat_avg_ret = float(cat_session_avgs[0]) * 100 if cat_session_avgs and cat_session_avgs[0] else 0
                cat_avg_chat = float(cat_session_avgs[1]) if cat_session_avgs and cat_session_avgs[1] else 0

                # Per-streamer retention and chat for percentile ranking
                per_streamer_ret = conn.execute("""
                    SELECT AVG(s.retention_10m) as ret
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL
                    GROUP BY LOWER(s.streamer_login)
                    ORDER BY ret
                """, [since_date]).fetchall()
                ret_sorted = [float(r[0]) * 100 for r in per_streamer_ret if r[0] is not None]

                per_streamer_chat = conn.execute("""
                    SELECT AVG(CASE WHEN s.avg_viewers > 0 THEN s.unique_chatters * 100.0 / s.avg_viewers ELSE 0 END) as ch
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL
                    GROUP BY LOWER(s.streamer_login)
                    ORDER BY ch
                """, [since_date]).fetchall()
                chat_sorted = [float(r[0]) for r in per_streamer_chat if r[0] is not None]

                # Percentiles for avgViewers
                avg_percentile = int(self._percentile_of(sorted_avgs, your_avg) * 100) if sorted_avgs else 0

                # Percentile for peakViewers
                peak_avgs = conn.execute("""
                    SELECT MAX(viewer_count) as peak
                    FROM twitch_stats_category
                    WHERE ts_utc >= ?
                    GROUP BY streamer
                    ORDER BY peak
                """, [since_date]).fetchall()
                peak_sorted = [float(r[0]) for r in peak_avgs] if peak_avgs else []
                peak_percentile = int(self._percentile_of(peak_sorted, your_peak) * 100) if peak_sorted else 50

                # Percentiles for retention and chat
                ret_percentile = int(self._percentile_of(ret_sorted, your_ret) * 100) if ret_sorted else 50
                chat_percentile = int(self._percentile_of(chat_sorted, your_chat) * 100) if chat_sorted else 50

                # Category rank (1 = best)
                category_rank = category_total - int(avg_percentile / 100 * category_total) if category_total else 0

                return web.json_response({
                    "yourStats": {
                        "avgViewers": round(your_avg, 1),
                        "peakViewers": your_peak,
                        "retention10m": round(your_ret, 1),
                        "chatHealth": round(your_chat, 1),
                    },
                    "categoryAvg": {
                        "avgViewers": round(cat_avg_viewers, 1),
                        "peakViewers": round(cat_avg_peak, 0),
                        "retention10m": round(cat_avg_ret, 1),
                        "chatHealth": round(cat_avg_chat, 1),
                    },
                    "percentiles": {
                        "avgViewers": avg_percentile,
                        "peakViewers": peak_percentile,
                        "retention10m": ret_percentile,
                        "chatHealth": chat_percentile,
                    },
                    "categoryRank": category_rank,
                    "categoryTotal": category_total,
                })
        except Exception as exc:
            log.exception("Error in category comparison API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_streamers(self, request: web.Request) -> web.Response:
        """Get list of streamers for dropdown."""
        self._require_v2_auth(request)

        try:
            with storage.get_conn() as conn:
                # Partners (verified)
                partners = conn.execute("""
                    SELECT twitch_login
                    FROM twitch_streamers
                    WHERE archived_at IS NULL
                      AND (manual_verified_permanent = 1 OR manual_verified_until > datetime('now'))
                    ORDER BY twitch_login
                """).fetchall()

                # Others with recent activity
                others = conn.execute("""
                    SELECT DISTINCT s.streamer_login
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= datetime('now', '-30 days')
                      AND s.streamer_login NOT IN (
                          SELECT twitch_login FROM twitch_streamers
                          WHERE archived_at IS NULL
                            AND (manual_verified_permanent = 1 OR manual_verified_until > datetime('now'))
                      )
                    ORDER BY s.streamer_login
                """).fetchall()

                data = [
                    {"login": r[0], "isPartner": True}
                    for r in partners
                ] + [
                    {"login": r[0], "isPartner": False}
                    for r in others
                ]

                return web.json_response(data)
        except Exception as exc:
            log.exception("Error in streamers API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_session_detail(self, request: web.Request) -> web.Response:
        """Get detailed session data."""
        self._require_v2_auth(request)

        session_id = request.match_info.get("id", "")
        try:
            session_id = int(session_id)
        except ValueError:
            return web.json_response({"error": "Invalid session ID"}, status=400)

        try:
            with storage.get_conn() as conn:
                # Session data
                row = conn.execute("""
                    SELECT
                        s.id, s.streamer_login, s.started_at, s.ended_at,
                        s.duration_seconds, s.start_viewers, s.peak_viewers, s.end_viewers,
                        s.avg_viewers, s.retention_5m, s.retention_10m, s.retention_20m,
                        s.dropoff_pct, s.unique_chatters, s.first_time_chatters,
                        s.returning_chatters, s.stream_title
                    FROM twitch_stream_sessions s
                    WHERE s.id = ?
                """, [session_id]).fetchone()

                if not row:
                    return web.json_response({"error": "Session not found"}, status=404)

                # Timeline
                timeline = conn.execute("""
                    SELECT minutes_from_start, viewer_count
                    FROM twitch_session_viewers
                    WHERE session_id = ?
                    ORDER BY minutes_from_start
                """, [session_id]).fetchall()

                # Top chatters
                chatters = conn.execute("""
                    SELECT chatter_login, messages
                    FROM twitch_session_chatters
                    WHERE session_id = ?
                    ORDER BY messages DESC
                    LIMIT 20
                """, [session_id]).fetchall()

                return web.json_response({
                    "id": row[0],
                    "streamerLogin": row[1],
                    "startedAt": row[2],
                    "endedAt": row[3],
                    "duration": row[4] or 0,
                    "startViewers": row[5] or 0,
                    "peakViewers": row[6] or 0,
                    "endViewers": row[7] or 0,
                    "avgViewers": float(row[8]) if row[8] else 0,
                    "retention5m": float(row[9]) * 100 if row[9] else 0,
                    "retention10m": float(row[10]) * 100 if row[10] else 0,
                    "retention20m": float(row[11]) * 100 if row[11] else 0,
                    "dropoffPct": float(row[12]) * 100 if row[12] else 0,
                    "uniqueChatters": row[13] or 0,
                    "firstTimeChatters": row[14] or 0,
                    "returningChatters": row[15] or 0,
                    "title": row[16] or "",
                    "timeline": [{"minute": t[0], "viewers": t[1]} for t in timeline],
                    "chatters": [{"login": c[0], "messages": c[1]} for c in chatters],
                })
        except Exception as exc:
            log.exception("Error in session detail API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_auth_status(self, request: web.Request) -> web.Response:
        """Get current authentication status and permissions."""
        auth_level = self._get_auth_level(request)
        session = self._get_dashboard_session(request) or {}
        is_authenticated = auth_level != "none"
        can_view_all_streamers = is_authenticated

        return web.json_response({
            "authenticated": is_authenticated,
            "level": auth_level,
            "isAdmin": auth_level in ("localhost", "admin"),
            "isLocalhost": auth_level == "localhost",
            "canViewAllStreamers": can_view_all_streamers,
            "twitchLogin": session.get("twitch_login"),
            "displayName": session.get("display_name"),
            "permissions": {
                "viewAllStreamers": can_view_all_streamers,
                "viewComparison": is_authenticated,
                "viewChatAnalytics": is_authenticated,
                "viewOverlap": is_authenticated,
            }
        })

    # ==================== NEW AUDIENCE ANALYTICS ENDPOINTS ====================

    def _calc_watch_distribution(self, sessions) -> Dict[str, Any]:
        """Calculate watch time distribution from session retention data."""
        if not sessions:
            return {
                "under5min": 0, "min5to15": 0, "min15to30": 0,
                "min30to60": 0, "over60min": 0,
                "avgWatchTime": 0, "medianWatchTime": 0,
                "sessionCount": 0,
            }

        total_sessions = len(sessions)
        ret_5m_avg = sum((s[1] or 0) * 100 for s in sessions) / total_sessions
        ret_10m_avg = sum((s[2] or 0) * 100 for s in sessions) / total_sessions
        ret_20m_avg = sum((s[3] or 0) * 100 for s in sessions) / total_sessions

        # Clamp to 0 — noisy data can invert retention values (10m > 5m)
        under_5min = max(0, 100 - ret_5m_avg)
        min_5_to_15 = max(0, ret_5m_avg - ret_10m_avg)
        min_15_to_30 = max(0, ret_10m_avg - ret_20m_avg)
        min_30_to_60 = max(0, ret_20m_avg * 0.4)
        over_60min = max(0, ret_20m_avg * 0.6)

        total = under_5min + min_5_to_15 + min_15_to_30 + min_30_to_60 + over_60min
        if total > 0:
            under_5min = (under_5min / total) * 100
            min_5_to_15 = (min_5_to_15 / total) * 100
            min_15_to_30 = (min_15_to_30 / total) * 100
            min_30_to_60 = (min_30_to_60 / total) * 100
            over_60min = (over_60min / total) * 100

        avg_durations = [s[0] or 0 for s in sessions]
        avg_duration_mins = sum(avg_durations) / len(avg_durations) / 60 if avg_durations else 0

        avg_watch_time = (
            (under_5min / 100) * 2.5 +
            (min_5_to_15 / 100) * 10 +
            (min_15_to_30 / 100) * 22.5 +
            (min_30_to_60 / 100) * 45 +
            (over_60min / 100) * min(90, avg_duration_mins)
        )

        return {
            "under5min": round(max(0, under_5min), 1),
            "min5to15": round(max(0, min_5_to_15), 1),
            "min15to30": round(max(0, min_15_to_30), 1),
            "min30to60": round(max(0, min_30_to_60), 1),
            "over60min": round(max(0, over_60min), 1),
            "avgWatchTime": round(avg_watch_time, 1),
            "medianWatchTime": round(avg_watch_time * 0.85, 1),  # Estimate: ~85% of avg
            "sessionCount": total_sessions,
        }

    async def _api_v2_watch_time_distribution(self, request: web.Request) -> web.Response:
        """Get watch time distribution with previous period comparison."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip() or None
        days = min(max(int(request.query.get("days", "30")), 7), 365)

        if not streamer:
            return web.json_response({"error": "Streamer required"}, status=400)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                prev_since_date = (datetime.now(timezone.utc) - timedelta(days=days * 2)).isoformat()

                session_cols = """s.duration_seconds, s.retention_5m, s.retention_10m,
                           s.retention_20m, s.avg_viewers, s.start_viewers, s.end_viewers"""

                # Current period
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                current_sessions = conn.execute(f"""
                    SELECT {session_cols}
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """, [since_date, streamer.lower()]).fetchall()

                # Previous period
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                prev_sessions = conn.execute(f"""
                    SELECT {session_cols}
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.started_at < ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """, [prev_since_date, since_date, streamer.lower()]).fetchall()

                current = self._calc_watch_distribution(current_sessions)
                previous = self._calc_watch_distribution(prev_sessions)

                # Calculate deltas
                deltas = {}
                for key in ["under5min", "min5to15", "min15to30", "min30to60", "over60min", "avgWatchTime"]:
                    curr_val = current.get(key, 0)
                    prev_val = previous.get(key, 0)
                    if prev_val > 0:
                        deltas[key] = round(((curr_val - prev_val) / prev_val) * 100, 1)
                    else:
                        deltas[key] = None

                return web.json_response({
                    **current,
                    "previous": previous,
                    "deltas": deltas,
                })
        except Exception as exc:
            log.exception("Error in watch time distribution API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_follower_funnel(self, request: web.Request) -> web.Response:
        """Get follower conversion funnel data."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip() or None
        days = min(max(int(request.query.get("days", "30")), 7), 365)

        if not streamer:
            return web.json_response({"error": "Streamer required"}, status=400)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

                # Get session stats — separate net delta from gained-only
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                stats = conn.execute(f"""
                    SELECT
                        SUM(s.unique_chatters) as total_chatters,
                        SUM(s.returning_chatters) as returning_chatters,
                        {self._FOLLOWER_DELTA_SUM} as net_followers,
                        SUM(CASE WHEN s.follower_delta > 0
                             AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                             THEN s.follower_delta ELSE 0 END) as gained_followers,
                        SUM(s.duration_seconds) as total_duration,
                        AVG(s.avg_viewers) as avg_viewers,
                        COUNT(*) as session_count
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """, [since_date, streamer.lower()]).fetchone()

                if not stats or not stats[0]:
                    return web.json_response({
                        "uniqueViewers": 0, "returningViewers": 0, "newFollowers": 0,
                        "netFollowerDelta": 0,
                        "conversionRate": 0, "avgTimeToFollow": 0,
                        "followersBySource": {"organic": 0, "raids": 0, "hosts": 0, "other": 0}
                    })

                total_chatters = int(stats[0]) if stats[0] else 0
                returning = int(stats[1]) if stats[1] else 0
                net_followers = int(stats[2]) if stats[2] else 0
                gained_followers = int(stats[3]) if stats[3] else 0
                total_duration = float(stats[4]) if stats[4] else 0
                avg_viewers = float(stats[5]) if stats[5] else 0
                session_count = int(stats[6]) if stats[6] else 1

                # Unique viewer estimation:
                # avg_viewers = average concurrent viewers per session
                # Multiply by ~2.5 for viewer turnover (people join/leave during stream)
                viewer_estimate = int(avg_viewers * 2.5)
                chatter_estimate = total_chatters * 6
                unique_viewers = max(viewer_estimate, chatter_estimate, 1)

                # Conversion rate uses gained followers only (not net delta)
                conversion_rate = (gained_followers / unique_viewers * 100) if unique_viewers > 0 else 0

                # Estimate time to follow (based on avg session length)
                avg_session_mins = (total_duration / session_count / 60) if session_count > 0 else 60
                avg_time_to_follow = min(avg_session_mins * 0.4, 45)  # Usually in first 40% of stream

                # Get raid follower estimate
                raids_received = conn.execute("""
                    SELECT COUNT(*), COALESCE(SUM(viewer_count), 0)
                    FROM twitch_raid_history
                    WHERE LOWER(to_broadcaster_login) = ? AND executed_at >= ? AND success = 1
                """, [streamer.lower(), since_date]).fetchone()

                raid_count = raids_received[0] if raids_received else 0
                raid_viewers = int(raids_received[1]) if raids_received else 0

                # Estimate follower sources (based on gained, not net)
                raid_followers = min(int(raid_viewers * 0.05), gained_followers)
                organic_followers = max(0, gained_followers - raid_followers)

                return web.json_response({
                    "uniqueViewers": unique_viewers,
                    "returningViewers": returning,
                    "newFollowers": gained_followers,
                    "netFollowerDelta": net_followers,
                    "conversionRate": round(conversion_rate, 2),
                    "avgTimeToFollow": round(avg_time_to_follow, 0),
                    "followersBySource": {
                        "organic": organic_followers,
                        "raids": raid_followers,
                        "hosts": 0,  # Would need host tracking
                        "other": 0
                    }
                })
        except Exception as exc:
            log.exception("Error in follower funnel API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_tag_analysis_extended(self, request: web.Request) -> web.Response:
        """Get extended tag performance with trends."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip() or None
        days = min(max(int(request.query.get("days", "30")), 7), 365)
        limit = min(max(int(request.query.get("limit", "20")), 5), 50)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                prev_since = (datetime.now(timezone.utc) - timedelta(days=days * 2)).isoformat()

                where = "AND LOWER(s.streamer_login) = ?" if streamer else ""
                params = [since_date, streamer.lower()] if streamer else [since_date]

                # Get tags from sessions (tags stored as JSON or comma-separated in tags column)
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                rows = conn.execute(f"""
                    SELECT
                        s.tags,
                        AVG(s.avg_viewers) as avg_viewers,
                        AVG(s.retention_10m) as avg_retention,
                        {self._FOLLOWER_DELTA_AVG} as avg_followers,
                        COUNT(*) as usage_count,
                        AVG(s.duration_seconds) as avg_duration,
                        strftime('%H', s.started_at) as start_hour
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL AND s.tags IS NOT NULL {where}
                    GROUP BY s.tags
                    ORDER BY avg_viewers DESC
                    LIMIT ?
                """, params + [limit]).fetchall()

                # Parse and aggregate tags
                tag_stats: Dict[str, Dict[str, Any]] = {}
                for row in rows:
                    tags_str = row[0] or ""
                    # Handle JSON array or comma-separated
                    if tags_str.startswith("["):
                        import json
                        try:
                            tags = json.loads(tags_str)
                        except:
                            tags = [tags_str]
                    else:
                        tags = [t.strip() for t in tags_str.split(",") if t.strip()]

                    for tag in tags[:5]:  # Max 5 tags per session
                        if tag not in tag_stats:
                            tag_stats[tag] = {
                                "viewers": [], "retention": [], "followers": [],
                                "count": 0, "durations": [], "hours": []
                            }
                        tag_stats[tag]["viewers"].append(float(row[1]) if row[1] else 0)
                        tag_stats[tag]["retention"].append(float(row[2]) * 100 if row[2] else 0)
                        tag_stats[tag]["followers"].append(float(row[3]) if row[3] else 0)
                        tag_stats[tag]["count"] += row[4] or 1
                        tag_stats[tag]["durations"].append(float(row[5]) if row[5] else 0)
                        if row[6]:
                            tag_stats[tag]["hours"].append(int(row[6]))

                # Build response
                result = []
                sorted_tags = sorted(
                    tag_stats.items(),
                    key=lambda x: sum(x[1]["viewers"]) / len(x[1]["viewers"]) if x[1]["viewers"] else 0,
                    reverse=True
                )

                for rank, (tag, data) in enumerate(sorted_tags[:limit], 1):
                    avg_v = sum(data["viewers"]) / len(data["viewers"]) if data["viewers"] else 0
                    avg_r = sum(data["retention"]) / len(data["retention"]) if data["retention"] else 0
                    avg_f = sum(data["followers"]) / len(data["followers"]) if data["followers"] else 0
                    avg_d = sum(data["durations"]) / len(data["durations"]) if data["durations"] else 0

                    # Best time slot
                    if data["hours"]:
                        from collections import Counter
                        hour_counts = Counter(data["hours"])
                        best_hour = hour_counts.most_common(1)[0][0]
                        best_slot = f"{best_hour:02d}:00-{(best_hour + 4) % 24:02d}:00"
                    else:
                        best_slot = "18:00-22:00"

                    result.append({
                        "tagName": tag,
                        "usageCount": data["count"],
                        "avgViewers": round(avg_v, 1),
                        "avgRetention10m": round(avg_r, 1),
                        "avgFollowerGain": round(avg_f, 1),
                        "trend": "stable",  # Would need historical comparison
                        "trendValue": 0,
                        "bestTimeSlot": best_slot,
                        "avgStreamDuration": round(avg_d, 0),
                        "categoryRank": rank
                    })

                return web.json_response(result)
        except Exception as exc:
            log.exception("Error in tag analysis extended API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_title_performance(self, request: web.Request) -> web.Response:
        """Get stream title performance analysis."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip() or None
        days = min(max(int(request.query.get("days", "30")), 7), 365)
        limit = min(max(int(request.query.get("limit", "20")), 5), 50)

        if not streamer:
            return web.json_response({"error": "Streamer required"}, status=400)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                rows = conn.execute(f"""
                    SELECT
                        s.stream_title,
                        COUNT(*) as usage_count,
                        AVG(s.avg_viewers) as avg_viewers,
                        AVG(s.retention_10m) as avg_retention,
                        {self._FOLLOWER_DELTA_AVG} as avg_followers,
                        MAX(s.peak_viewers) as peak_viewers
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ?
                      AND s.ended_at IS NOT NULL AND s.stream_title IS NOT NULL AND s.stream_title != ''
                    GROUP BY s.stream_title
                    ORDER BY avg_viewers DESC
                    LIMIT ?
                """, [since_date, streamer.lower(), limit]).fetchall()

                def extract_keywords(title: str) -> List[str]:
                    """Extract meaningful keywords from title."""
                    import re
                    # Remove common words and punctuation
                    stop_words = {'der', 'die', 'das', 'und', 'oder', 'mit', 'für', 'the', 'and', 'or', 'with', 'for', 'to', 'a', 'an'}
                    words = re.findall(r'\b\w{3,}\b', title.lower())
                    keywords = [w.capitalize() for w in words if w not in stop_words]
                    return keywords[:5]  # Max 5 keywords

                result = [
                    {
                        "title": row[0] or "",
                        "usageCount": row[1],
                        "avgViewers": round(float(row[2]), 1) if row[2] else 0,
                        "avgRetention10m": round(float(row[3]) * 100, 1) if row[3] else 0,
                        "avgFollowerGain": round(float(row[4]), 1) if row[4] else 0,
                        "peakViewers": int(row[5]) if row[5] else 0,
                        "keywords": extract_keywords(row[0] or ""),
                    }
                    for row in rows
                ]

                return web.json_response(result)
        except Exception as exc:
            log.exception("Error in title performance API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_audience_insights(self, request: web.Request) -> web.Response:
        """Get combined audience insights (all in one call)."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip() or None
        days = min(max(int(request.query.get("days", "30")), 7), 365)

        if not streamer:
            return web.json_response({"error": "Streamer required"}, status=400)

        try:
            # Fetch all data in parallel-ish (reuse endpoints)
            watch_time_req = type('Request', (), {'query': {'streamer': streamer, 'days': str(days)}})()
            watch_time_req.headers = request.headers
            funnel_req = type('Request', (), {'query': {'streamer': streamer, 'days': str(days)}})()
            funnel_req.headers = request.headers
            tags_req = type('Request', (), {'query': {'streamer': streamer, 'days': str(days), 'limit': '10'}})()
            tags_req.headers = request.headers
            titles_req = type('Request', (), {'query': {'streamer': streamer, 'days': str(days), 'limit': '10'}})()
            titles_req.headers = request.headers

            # Call internal methods directly
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                prev_since = (datetime.now(timezone.utc) - timedelta(days=days * 2)).isoformat()

                # Current period metrics
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                current = conn.execute(f"""
                    SELECT
                        AVG(s.retention_10m) as retention,
                        {self._FOLLOWER_DELTA_SUM} as followers,
                        SUM(s.returning_chatters) as returning,
                        SUM(s.unique_chatters) as unique_chatters
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """, [since_date, streamer.lower()]).fetchone()

                # Previous period for comparison
                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                prev = conn.execute(f"""
                    SELECT
                        AVG(s.retention_10m) as retention,
                        {self._FOLLOWER_DELTA_SUM} as followers,
                        SUM(s.returning_chatters) as returning,
                        SUM(s.unique_chatters) as unique_chatters
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.started_at < ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """, [prev_since, since_date, streamer.lower()]).fetchone()

                # Calculate trends
                def calc_trend(curr, prev):
                    if not prev or prev == 0:
                        return 0
                    return round(((curr - prev) / prev) * 100, 1)

                curr_retention = float(current[0]) * 100 if current and current[0] else 0
                prev_retention = float(prev[0]) * 100 if prev and prev[0] else 0
                curr_unique = int(current[3]) if current and current[3] else 0
                prev_unique = int(prev[3]) if prev and prev[3] else 0
                curr_returning = int(current[2]) if current and current[2] else 0
                prev_returning = int(prev[2]) if prev and prev[2] else 0

                return_rate = (curr_returning / curr_unique * 100) if curr_unique > 0 else 0
                prev_return_rate = (prev_returning / prev_unique * 100) if prev_unique > 0 else 0

                return web.json_response({
                    "trends": {
                        "watchTimeChange": calc_trend(curr_retention, prev_retention),
                        "conversionChange": 0,  # Would need follower tracking improvement
                        "viewerReturnRate": round(return_rate, 1),
                        "viewerReturnChange": calc_trend(return_rate, prev_return_rate),
                    }
                })
        except Exception as exc:
            log.exception("Error in audience insights API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_audience_demographics(self, request: web.Request) -> web.Response:
        """Get estimated audience demographics based on available data."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip() or None
        days = min(max(int(request.query.get("days", "30")), 7), 365)

        if not streamer:
            return web.json_response({"error": "Streamer required"}, status=400)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

                # Analyze stream times to estimate audience timezone/region
                time_stats = conn.execute("""
                    SELECT
                        CAST(strftime('%H', s.started_at) AS INTEGER) as hour,
                        AVG(s.avg_viewers) as avg_viewers,
                        COUNT(*) as stream_count
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                    GROUP BY hour
                    ORDER BY avg_viewers DESC
                """, [since_date, streamer.lower()]).fetchall()

                # Peak hours analysis for region estimation
                peak_hours = [r[0] for r in time_stats[:3]] if time_stats else [20, 21, 19]

                # German stream = DACH region primarily (UTC+1/+2)
                # If peak is 18-23 UTC, likely European audience
                europe_score = sum(1 for h in peak_hours if 17 <= h <= 23)
                us_score = sum(1 for h in peak_hours if 0 <= h <= 6 or 23 <= h <= 24)
                asia_score = sum(1 for h in peak_hours if 8 <= h <= 16)

                total = europe_score + us_score + asia_score or 1
                regions = [
                    {"region": "DACH", "percentage": round(europe_score / total * 70, 1)},  # Primary
                    {"region": "Rest EU", "percentage": round(europe_score / total * 15, 1)},
                    {"region": "NA", "percentage": round(us_score / total * 10, 1)},
                    {"region": "Other", "percentage": round(asia_score / total * 5, 1)},
                ]

                # Chat activity analysis for engagement type
                chat_stats = conn.execute("""
                    SELECT
                        AVG(s.unique_chatters) as avg_chatters,
                        AVG(s.avg_viewers) as avg_viewers,
                        AVG(s.returning_chatters * 1.0 / NULLIF(s.unique_chatters, 0)) as return_rate
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """, [since_date, streamer.lower()]).fetchone()

                chatters = float(chat_stats[0]) if chat_stats and chat_stats[0] else 0
                viewers = float(chat_stats[1]) if chat_stats and chat_stats[1] else 1
                return_rate = float(chat_stats[2]) if chat_stats and chat_stats[2] else 0

                chat_rate = chatters / viewers if viewers > 0 else 0

                # Estimate viewer types
                # High chat rate + high return = dedicated community
                # Low chat rate + low return = casual viewers
                if chat_rate > 0.15 and return_rate > 0.4:
                    viewer_type = [
                        {"label": "Dedicated Fans", "percentage": 45},
                        {"label": "Regular Viewers", "percentage": 35},
                        {"label": "Casual Viewers", "percentage": 15},
                        {"label": "New Visitors", "percentage": 5},
                    ]
                elif chat_rate > 0.1:
                    viewer_type = [
                        {"label": "Dedicated Fans", "percentage": 25},
                        {"label": "Regular Viewers", "percentage": 40},
                        {"label": "Casual Viewers", "percentage": 25},
                        {"label": "New Visitors", "percentage": 10},
                    ]
                else:
                    viewer_type = [
                        {"label": "Dedicated Fans", "percentage": 15},
                        {"label": "Regular Viewers", "percentage": 30},
                        {"label": "Casual Viewers", "percentage": 35},
                        {"label": "New Visitors", "percentage": 20},
                    ]

                # Activity pattern based on stream schedule
                schedule_stats = conn.execute("""
                    SELECT
                        CAST(strftime('%w', s.started_at) AS INTEGER) as weekday,
                        COUNT(*) as count
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                    GROUP BY weekday
                """, [since_date, streamer.lower()]).fetchall()

                weekday_counts = {r[0]: r[1] for r in schedule_stats}
                weekend_streams = weekday_counts.get(0, 0) + weekday_counts.get(6, 0)
                weekday_streams = sum(weekday_counts.get(i, 0) for i in range(1, 6))

                if weekend_streams > weekday_streams:
                    activity_pattern = "weekend-heavy"
                elif weekday_streams > weekend_streams * 2:
                    activity_pattern = "weekday-focused"
                else:
                    activity_pattern = "balanced"

                return web.json_response({
                    "estimatedRegions": regions,
                    "viewerTypes": viewer_type,
                    "activityPattern": activity_pattern,
                    "primaryLanguage": "German",
                    "languageConfidence": 85,  # Based on being tracked as German streamer
                    "peakActivityHours": peak_hours,
                    "interactiveRate": round(chat_rate * 100, 1),
                    "loyaltyScore": round(return_rate * 100, 1),
                })
        except Exception as exc:
            log.exception("Error in audience demographics API")
            return web.json_response({"error": str(exc)}, status=500)


    # ==================== STATS-DATA ENDPOINTS ====================

    async def _api_v2_viewer_timeline(self, request: web.Request) -> web.Response:
        """Return bucketed viewer data from twitch_stats_tracked."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip()
        days = min(max(int(request.query.get("days", "7")), 1), 365)

        if not streamer:
            return web.json_response({"error": "Streamer required"}, status=400)

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

                # Determine bucket size based on range
                if days <= 7:
                    bucket_minutes = 5
                    bucket_fmt = "%Y-%m-%d %H:" + "00"  # will be refined below
                elif days <= 30:
                    bucket_minutes = 30
                else:
                    bucket_minutes = 60

                # Use SQLite strftime to bucket timestamps
                # For 5-min: floor to 5-min intervals; for 30-min/60-min: use hour or half-hour
                if bucket_minutes == 5:
                    # Group by 5-minute intervals: YYYY-MM-DD HH:M0 where M0 = (minute/5)*5
                    bucket_expr = "strftime('%Y-%m-%d %H:', ts_utc) || PRINTF('%02d', (CAST(strftime('%M', ts_utc) AS INTEGER) / 5) * 5)"
                elif bucket_minutes == 30:
                    bucket_expr = "strftime('%Y-%m-%d %H:', ts_utc) || CASE WHEN CAST(strftime('%M', ts_utc) AS INTEGER) < 30 THEN '00' ELSE '30' END"
                else:
                    bucket_expr = "strftime('%Y-%m-%d %H:00', ts_utc)"

                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                rows = conn.execute(f"""
                    SELECT
                        {bucket_expr} as bucket,
                        AVG(viewer_count) as avg_vc,
                        MAX(viewer_count) as peak_vc,
                        MIN(viewer_count) as min_vc,
                        COUNT(*) as samples
                    FROM twitch_stats_tracked
                    WHERE ts_utc >= ? AND LOWER(streamer) = ?
                    GROUP BY bucket
                    ORDER BY bucket
                """, [since_date, streamer.lower()]).fetchall()

                data = [
                    {
                        "timestamp": r[0],
                        "avgViewers": round(float(r[1]), 1) if r[1] else 0,
                        "peakViewers": int(r[2]) if r[2] else 0,
                        "minViewers": int(r[3]) if r[3] else 0,
                        "samples": r[4] or 0,
                    }
                    for r in rows
                ]

                return web.json_response(data)
        except Exception as exc:
            log.exception("Error in viewer timeline API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_category_leaderboard(self, request: web.Request) -> web.Response:
        """Top-N streamers from twitch_stats_category."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip()
        days = min(max(int(request.query.get("days", "30")), 1), 365)
        limit = min(max(int(request.query.get("limit", "25")), 5), 100)
        sort = request.query.get("sort", "avg")  # avg or peak

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

                order_col = "avg_vc" if sort == "avg" else "peak_vc"

                # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                rows = conn.execute(f"""
                    SELECT
                        c.streamer,
                        AVG(c.viewer_count) as avg_vc,
                        MAX(c.viewer_count) as peak_vc,
                        MAX(c.is_partner) as is_partner
                    FROM twitch_stats_category c
                    WHERE c.ts_utc >= ?
                    GROUP BY c.streamer
                    ORDER BY {order_col} DESC
                """, [since_date]).fetchall()

                total_streamers = len(rows)

                # Build ranked list
                leaderboard = []
                your_rank = None
                streamer_lower = streamer.lower() if streamer else ""
                your_entry = None

                for i, r in enumerate(rows):
                    rank = i + 1
                    entry = {
                        "rank": rank,
                        "streamer": r[0],
                        "avgViewers": round(float(r[1]), 1) if r[1] else 0,
                        "peakViewers": int(r[2]) if r[2] else 0,
                        "isPartner": bool(r[3]),
                        "isYou": r[0].lower() == streamer_lower,
                    }
                    if r[0].lower() == streamer_lower:
                        your_rank = rank
                        your_entry = entry
                    if rank <= limit:
                        leaderboard.append(entry)

                # If streamer is not in top-N, append them
                if your_entry and your_rank and your_rank > limit:
                    leaderboard.append(your_entry)

                return web.json_response({
                    "leaderboard": leaderboard,
                    "totalStreamers": total_streamers,
                    "yourRank": your_rank,
                })
        except Exception as exc:
            log.exception("Error in category leaderboard API")
            return web.json_response({"error": str(exc)}, status=500)


__all__ = ["AnalyticsV2Mixin"]
