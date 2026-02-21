"""
Analytics API v2 - Backend endpoints for the new React TypeScript dashboard.
"""

from __future__ import annotations

import collections
import ipaddress
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

from aiohttp import web

from .. import storage
from .coaching_engine import CoachingEngine

log = logging.getLogger("TwitchStreams.AnalyticsV2")
DASHBOARD_V2_LOGIN_URL = "/twitch/auth/login?next=%2Ftwitch%2Fdashboard-v2"
DASHBOARD_V2_DISCORD_LOGIN_URL = "/twitch/auth/discord/login?next=%2Ftwitch%2Fdashboard-v2"


def _is_loopback_host(raw_value: str) -> bool:
    value = (raw_value or "").strip()
    if not value:
        return False

    token = value.split(",")[0].strip()  # noqa: S105
    if token.startswith("["):
        end = token.find("]")
        if end != -1:
            token = token[1:end]  # noqa: S105
    elif token.count(":") == 1:
        host_part, port_part = token.rsplit(":", 1)
        if port_part.isdigit():
            token = host_part  # noqa: S105

    token = token.strip().lower()  # noqa: S105
    if token == "localhost":  # noqa: S105
        return True

    try:
        return ipaddress.ip_address(token).is_loopback
    except ValueError:
        return False


def _is_localhost(request: web.Request) -> bool:
    """Allow localhost bypass only for local host+peer socket metadata."""
    host_header = request.headers.get("Host") or request.host or ""
    if not _is_loopback_host(host_header):
        return False

    remote = (request.remote or "").strip() if hasattr(request, "remote") else ""
    if remote and _is_loopback_host(remote):
        return True

    transport = getattr(request, "transport", None)
    if transport is not None:
        peer = transport.get_extra_info("peername")
        if isinstance(peer, tuple) and peer:
            peer_host = str(peer[0]).strip()
            if _is_loopback_host(peer_host):
                return True
        if isinstance(peer, str) and _is_loopback_host(peer.strip()):
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

    def _classify_message(self, content: str) -> str:
        if not content:
            return "Other"
        content_lower = content.lower()

        if content.startswith("!"):
            return "Command"

        if any(
            w in content_lower
            for w in [
                "hi",
                "hello",
                "hey",
                "moin",
                "nabend",
                "guten",
                "welcome",
                "hallo",
            ]
        ):
            return "Greeting"

        if "?" in content or any(
            w in content_lower
            for w in [
                "was",
                "wo",
                "wer",
                "wie",
                "wann",
                "why",
                "how",
                "warum",
                "weshalb",
            ]
        ):
            return "Question"

        if any(
            w in content_lower
            for w in [
                "lol",
                "lmao",
                "haha",
                "gg",
                "pog",
                "lul",
                "kek",
                "xd",
                ":)",
                ":d",
                "f",
                "o7",
                "wow",
                "omg",
            ]
        ):
            return "Reaction"

        if any(
            w in content_lower
            for w in [
                "deadlock",
                "hero",
                "build",
                "skill",
                "rank",
                "elo",
                "match",
                "play",
                "game",
                "win",
                "lose",
                "mmr",
                "lane",
                "ult",
            ]
        ):
            return "Game-Related"

        if any(
            w in content_lower
            for w in [
                "follow",
                "sub",
                "prime",
                "raid",
                "host",
                "danke",
                "thanks",
                "thx",
                "discord",
                "social",
            ]
        ):
            return "Engagement"

        return "Other"

    def _get_dashboard_session(self, request: web.Request) -> dict[str, Any] | None:
        admin_getter = getattr(self, "_get_discord_admin_session", None)
        if callable(admin_getter):
            try:
                admin_session = admin_getter(request)
            except Exception:
                log.debug("Could not resolve Discord admin dashboard session", exc_info=True)
                admin_session = None
            if isinstance(admin_session, dict):
                session_copy = dict(admin_session)
                session_copy.setdefault("auth_type", "discord_admin")
                return session_copy

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
    def _normalize_dashboard_next_path(raw_path: str | None) -> str:
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
    def _safe_internal_login_redirect(candidate: str | None) -> str:
        fallback = DASHBOARD_V2_LOGIN_URL
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
        return self._safe_internal_login_redirect(
            f"/twitch/auth/login?{urlencode({'next': next_path})}"
        )

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
                should_use_discord = getattr(self, "_should_use_discord_admin_login", None)
                if callable(should_use_discord) and bool(should_use_discord(request)):
                    login_url = "/twitch/auth/discord/login?next=%2Ftwitch%2Fdashboard-v2"
                else:
                    login_url = "/twitch/auth/login?next=%2Ftwitch%2Fdashboard-v2"
            payload = {
                "error": "Authentication required. Use Twitch login, partner_token, or access from localhost.",
                "loginUrl": login_url,
            }
            if request.path.startswith("/twitch/api/"):
                raise web.HTTPUnauthorized(
                    text=json.dumps(payload), content_type="application/json"
                )
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

        dashboard_session = self._get_dashboard_session(request)
        if dashboard_session:
            auth_type = str(dashboard_session.get("auth_type") or "").strip().lower()
            if auth_type == "discord_admin":
                return "admin"
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
        router.add_get(
            "/twitch/api/v2/watch-time-distribution",
            self._api_v2_watch_time_distribution,
        )
        router.add_get("/twitch/api/v2/follower-funnel", self._api_v2_follower_funnel)
        router.add_get("/twitch/api/v2/tag-analysis-extended", self._api_v2_tag_analysis_extended)
        router.add_get("/twitch/api/v2/title-performance", self._api_v2_title_performance)
        router.add_get("/twitch/api/v2/audience-insights", self._api_v2_audience_insights)
        router.add_get("/twitch/api/v2/audience-demographics", self._api_v2_audience_demographics)
        # Stats-Data Endpoints (from twitch_stats_tracked / twitch_stats_category)
        router.add_get("/twitch/api/v2/viewer-timeline", self._api_v2_viewer_timeline)
        router.add_get("/twitch/api/v2/category-leaderboard", self._api_v2_category_leaderboard)
        router.add_get("/twitch/api/v2/coaching", self._api_v2_coaching)
        router.add_get("/twitch/api/v2/monetization", self._api_v2_monetization)
        router.add_get("/twitch/api/v2/category-timings", self._api_v2_category_timings)
        router.add_get(
            "/twitch/api/v2/category-activity-series",
            self._api_v2_category_activity_series,
        )
        # Serve the dashboard
        router.add_get("/twitch/dashboard-v2", self._serve_dashboard_v2)
        router.add_get("/twitch/dashboard-v2/{path:.*}", self._serve_dashboard_v2_assets)
        # Public demo (no auth required)
        self._register_demo_routes(router)

    def _register_demo_routes(self, router: web.UrlDispatcher) -> None:
        """Register public demo endpoints – no authentication required."""
        from .demo_data import (
            get_audience_demographics,
            get_audience_insights,
            get_auth_status,
            get_calendar_heatmap,
            get_category_activity_series,
            get_category_comparison,
            get_category_leaderboard,
            get_category_timings,
            get_chat_analytics,
            get_coaching,
            get_follower_funnel,
            get_hourly_heatmap,
            get_monetization,
            get_monthly_stats,
            get_overview,
            get_rankings,
            get_streamers,
            get_tag_analysis,
            get_tag_analysis_extended,
            get_title_performance,
            get_viewer_overlap,
            get_viewer_timeline,
            get_watch_time_distribution,
            get_weekday_stats,
        )

        def _j(data):
            async def _handler(request: web.Request) -> web.Response:
                return web.json_response(data() if callable(data) else data)

            return _handler

        def _days_j(fn):
            async def _handler(request: web.Request) -> web.Response:
                try:
                    days = int(request.query.get("days", "30"))
                except ValueError:
                    days = 30
                return web.json_response(fn(days))

            return _handler

        def _metric_j(fn):
            async def _handler(request: web.Request) -> web.Response:
                metric = request.query.get("metric", "viewers")
                return web.json_response(fn(metric))

            return _handler

        base = "/twitch/demo/api/v2"
        router.add_get(f"{base}/auth-status", _j(get_auth_status))
        router.add_get(f"{base}/streamers", _j(get_streamers))
        router.add_get(f"{base}/overview", _days_j(get_overview))
        router.add_get(f"{base}/monthly-stats", _j(get_monthly_stats))
        router.add_get(f"{base}/weekly-stats", _j(get_weekday_stats))
        router.add_get(f"{base}/hourly-heatmap", _j(get_hourly_heatmap))
        router.add_get(f"{base}/calendar-heatmap", _j(get_calendar_heatmap))
        router.add_get(f"{base}/chat-analytics", _j(get_chat_analytics))
        router.add_get(f"{base}/viewer-overlap", _j(get_viewer_overlap))
        router.add_get(f"{base}/tag-analysis", _j(get_tag_analysis))
        router.add_get(f"{base}/tag-analysis-extended", _j(get_tag_analysis_extended))
        router.add_get(f"{base}/title-performance", _j(get_title_performance))
        router.add_get(f"{base}/rankings", _metric_j(get_rankings))
        router.add_get(f"{base}/category-comparison", _j(get_category_comparison))
        router.add_get(f"{base}/watch-time-distribution", _j(get_watch_time_distribution))
        router.add_get(f"{base}/follower-funnel", _j(get_follower_funnel))
        router.add_get(f"{base}/audience-insights", _j(get_audience_insights))
        router.add_get(f"{base}/audience-demographics", _j(get_audience_demographics))
        router.add_get(f"{base}/viewer-timeline", _days_j(get_viewer_timeline))
        router.add_get(f"{base}/category-leaderboard", _j(get_category_leaderboard))
        router.add_get(f"{base}/coaching", _j(get_coaching))
        router.add_get(f"{base}/monetization", _j(get_monetization))
        router.add_get(f"{base}/category-timings", _j(get_category_timings))
        router.add_get(f"{base}/category-activity-series", _j(get_category_activity_series))
        # Demo dashboard HTML
        router.add_get("/twitch/demo/", self._serve_demo_dashboard)
        router.add_get("/twitch/demo", self._serve_demo_dashboard)

    async def _serve_demo_dashboard(self, request: web.Request) -> web.Response:
        """Serve the demo dashboard HTML without authentication."""
        import pathlib

        dist_path = pathlib.Path(__file__).parent / "dashboard_v2" / "dist" / "index.html"
        if not dist_path.exists():
            return web.Response(
                text="Dashboard not built. Run npm run build in dashboard_v2/",
                status=404,
            )
        html = dist_path.read_text(encoding="utf-8")
        # Inject demo config + fetch interceptor before the app boots.
        # The built JS has the API base hardcoded as "/twitch/api/v2", so we
        # intercept fetch() to transparently rewrite those calls to the public
        # demo endpoints at "/twitch/demo/api/v2".
        inject = (
            "<script>"
            "window.__DEMO_MODE__=true;"
            'window.__DEMO_STREAMER__="deadlock_de_demo";'
            "(function(){"
            "var _f=window.fetch;"
            "window.fetch=function(u,o){"
            'if(typeof u==="string"&&u.indexOf("/twitch/api/v2/")!==-1){'
            'u=u.replace("/twitch/api/v2/","/twitch/demo/api/v2/");'
            "}"
            "return _f.call(this,u,o);"
            "};"
            "})();"
            "</script>"
        )
        html = html.replace("</head>", f"{inject}\n  </head>", 1)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _serve_dashboard_v2(self, request: web.Request) -> web.Response:
        """Serve the main dashboard HTML."""
        if not self._check_v2_auth(request):
            should_use_discord = getattr(self, "_should_use_discord_admin_login", None)
            if callable(should_use_discord) and bool(should_use_discord(request)):
                login_url = DASHBOARD_V2_DISCORD_LOGIN_URL
            else:
                login_url = DASHBOARD_V2_LOGIN_URL
            raise web.HTTPFound(login_url)
        import pathlib

        dist_path = pathlib.Path(__file__).parent / "dashboard_v2" / "dist" / "index.html"
        if dist_path.exists():
            return web.FileResponse(dist_path)
        return web.Response(
            text="Dashboard not built. Run npm run build in dashboard_v2/", status=404
        )

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

    async def _get_overview_data(self, streamer: str | None, days: int) -> dict[str, Any]:
        """Get comprehensive overview data for the dashboard."""
        with storage.get_conn() as conn:
            since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            prev_since_date = (datetime.now(UTC) - timedelta(days=days * 2)).isoformat()

            streamer_login = streamer.lower() if streamer else None

            # Check data exists
            count = conn.execute(
                """
                SELECT COUNT(*)
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ?
                  AND s.ended_at IS NOT NULL
                  AND (? IS NULL OR LOWER(s.streamer_login) = ?)
                """,
                [since_date, streamer_login, streamer_login],
            ).fetchone()[0]

            if count == 0:
                return {"empty": True, "error": "Keine Daten für den Zeitraum"}

            # Get sessions
            sessions = self._get_sessions(conn, since_date, streamer, 50)

            # Calculate metrics
            metrics = self._calculate_overview_metrics(conn, since_date, streamer)

            # Calculate previous period metrics for trends
            prev_metrics = conn.execute(
                """
                SELECT
                    AVG(s.avg_viewers) as avg_viewers,
                    SUM(CASE WHEN s.follower_delta IS NOT NULL
                         AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                         THEN s.follower_delta ELSE 0 END) as followers,
                    AVG(CASE
                        WHEN s.avg_viewers >= 3 AND s.peak_viewers > 0
                        THEN MIN(s.retention_10m, s.avg_viewers * 1.0 / s.peak_viewers, 1.0)
                        ELSE NULL
                    END) as retention
                FROM twitch_stream_sessions s
                WHERE s.started_at >= ? AND s.started_at < ?
                  AND s.ended_at IS NOT NULL
                  AND (? IS NULL OR LOWER(s.streamer_login) = ?)
                """,
                [prev_since_date, since_date, streamer_login, streamer_login],
            ).fetchone()

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
                        category_percentile = self._percentile_of(
                            cat_data["sorted_avgs"], streamer_avg
                        )
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

            result: dict[str, Any] = {
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

    def _get_sessions(
        self, conn, since_date: str, streamer: str | None, limit: int = 50
    ) -> list[dict]:
        """Get list of sessions with metrics."""
        streamer_login = streamer.lower() if streamer else None
        rows = conn.execute(
            """
            SELECT
                s.id, DATE(s.started_at), TIME(s.started_at), s.duration_seconds,
                s.start_viewers, s.peak_viewers, s.end_viewers, s.avg_viewers,
                COALESCE(s.retention_5m, 0), COALESCE(s.retention_10m, 0), COALESCE(s.retention_20m, 0),
                COALESCE(s.dropoff_pct, 0), COALESCE(s.unique_chatters, 0),
                COALESCE(s.first_time_chatters, 0), COALESCE(s.returning_chatters, 0),
                COALESCE(s.followers_start, 0), COALESCE(s.followers_end, 0),
                COALESCE(s.stream_title, '')
            FROM twitch_stream_sessions s
            WHERE s.started_at >= ?
              AND s.ended_at IS NOT NULL
              AND (? IS NULL OR LOWER(s.streamer_login) = ?)
            ORDER BY s.started_at DESC
            LIMIT ?
        """,
            [since_date, streamer_login, streamer_login, limit],
        ).fetchall()

        sessions: list[dict[str, Any]] = []
        for r in rows:
            peak_viewers = int(r[5]) if r[5] else 0
            avg_viewers = float(r[7]) if r[7] else 0.0
            retention_cap = (
                min(1.0, max(0.0, (avg_viewers / peak_viewers))) if peak_viewers > 0 else 1.0
            )

            raw_ret_5m = float(r[8]) if r[8] else 0.0
            raw_ret_10m = float(r[9]) if r[9] else 0.0
            raw_ret_20m = float(r[10]) if r[10] else 0.0
            ret_5m = max(0.0, min(raw_ret_5m, retention_cap))
            ret_10m = max(0.0, min(raw_ret_10m, retention_cap))
            ret_20m = max(0.0, min(raw_ret_20m, retention_cap))

            sessions.append(
                {
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
                }
            )

        return sessions

    def _calculate_overview_metrics(
        self, conn, since_date: str, streamer: str | None
    ) -> dict[str, Any]:
        """Calculate all overview metrics."""
        streamer_login = streamer.lower() if streamer else None

        row = conn.execute(
            """
            SELECT
                AVG(s.avg_viewers) as avg_avg_viewers,
                MAX(s.peak_viewers) as max_peak_viewers,
                SUM(s.avg_viewers * s.duration_seconds / 3600.0) as total_hours_watched,
                SUM(s.duration_seconds / 3600.0) as total_airtime_hours,
                SUM(CASE WHEN s.follower_delta IS NOT NULL
                     AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                     THEN s.follower_delta ELSE 0 END) as total_followers,
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
            WHERE s.started_at >= ?
              AND s.ended_at IS NOT NULL
              AND (? IS NULL OR LOWER(s.streamer_login) = ?)
        """,
            [since_date, streamer_login, streamer_login],
        ).fetchone()

        total_airtime = float(row[3]) if row[3] else 0
        total_followers = int(row[4]) if row[4] else 0  # NET (can be negative)

        # Gained followers = only positive session deltas (ignores unfollows)
        gained_row = conn.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN s.follower_delta > 0
                 AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                 THEN s.follower_delta ELSE 0 END), 0)
            FROM twitch_stream_sessions s
            WHERE s.started_at >= ?
              AND s.ended_at IS NOT NULL
              AND (? IS NULL OR LOWER(s.streamer_login) = ?)
        """,
            [since_date, streamer_login, streamer_login],
        ).fetchone()
        gained_followers = int(gained_row[0]) if gained_row and gained_row[0] else 0

        # True unique chatters from rollup table (not SUM of per-session counts)
        unique_chatters_sum = int(row[9]) if row[9] else 0
        if streamer:
            true_unique = conn.execute(
                """
                SELECT COUNT(DISTINCT chatter_login)
                FROM twitch_chatter_rollup
                WHERE LOWER(streamer_login) = ?
            """,
                [streamer.lower()],
            ).fetchone()
            unique_chatters = (
                int(true_unique[0]) if true_unique and true_unique[0] else unique_chatters_sum
            )
        else:
            unique_chatters = unique_chatters_sum

        # Sample counts for data quality gating
        sample_row = conn.execute(
            """
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
            WHERE s.started_at >= ?
              AND s.ended_at IS NOT NULL
              AND (? IS NULL OR LOWER(s.streamer_login) = ?)
        """,
            [since_date, streamer_login, streamer_login],
        ).fetchone()
        retention_sample_count = int(sample_row[0]) if sample_row else 0
        chat_sample_count = int(sample_row[1]) if sample_row else 0
        follower_valid_count = int(sample_row[2]) if sample_row else 0

        # Active chatters (at least 1 message) for engagement rate calculation
        active_chatters_row = conn.execute(
            """
            SELECT COUNT(DISTINCT COALESCE(NULLIF(sc.chatter_login, ''), sc.chatter_id))
            FROM twitch_session_chatters sc
            JOIN twitch_stream_sessions s ON s.id = sc.session_id
            WHERE s.started_at >= ?
              AND s.ended_at IS NOT NULL
              AND (? IS NULL OR LOWER(s.streamer_login) = ?)
              AND sc.messages > 0
            """,
            [since_date, streamer_login, streamer_login],
        ).fetchone()
        active_chatters = (
            int(active_chatters_row[0]) if active_chatters_row and active_chatters_row[0] else 0
        )

        avg_viewers = float(row[0]) if row[0] else 0
        engagement_rate = (active_chatters / avg_viewers * 100) if avg_viewers > 0 else 0

        return {
            "avg_avg_viewers": avg_viewers,
            "max_peak_viewers": int(row[1]) if row[1] else 0,
            "total_hours_watched": float(row[2]) if row[2] else 0,
            "total_airtime_hours": total_airtime,
            "total_followers": total_followers,
            "gained_followers": gained_followers,
            "followers_per_hour": total_followers / total_airtime if total_airtime > 0 else 0,
            "gained_followers_per_hour": gained_followers / total_airtime
            if total_airtime > 0
            else 0,
            "avg_retention_5m": float(row[5]) * 100 if row[5] else 0,
            "avg_retention_10m": float(row[6]) * 100 if row[6] else 0,
            "avg_retention_20m": float(row[7]) * 100 if row[7] else 0,
            "avg_dropoff": float(row[8]) * 100 if row[8] else 0,
            "total_unique_chatters": unique_chatters,
            "active_chatters": active_chatters,
            "engagement_rate": round(engagement_rate, 2),
            "chat_per_100": float(row[10]) if row[10] else 0,
            "retention_sample_count": retention_sample_count,
            "chat_sample_count": chat_sample_count,
            "follower_valid_count": follower_valid_count,
        }

    def _get_category_percentiles(self, conn, since_date: str) -> dict[str, Any]:
        """Get per-streamer AVG viewer_count from stats_category and compute percentiles."""
        rows = conn.execute(
            """
            SELECT streamer, AVG(viewer_count) as avg_vc
            FROM twitch_stats_category
            WHERE ts_utc >= ?
            GROUP BY streamer
            ORDER BY avg_vc
        """,
            [since_date],
        ).fetchall()

        if not rows:
            return {"sorted_avgs": [], "streamer_map": {}, "total": 0}

        sorted_avgs = [float(r[1]) for r in rows]
        streamer_map = {r[0].lower(): float(r[1]) for r in rows}
        return {
            "sorted_avgs": sorted_avgs,
            "streamer_map": streamer_map,
            "total": len(rows),
        }

    def _percentile_of(self, sorted_avgs: list[float], value: float) -> float:
        """Return the percentile (0-1) of value within sorted_avgs."""
        if not sorted_avgs:
            return 0.5
        below = sum(1 for v in sorted_avgs if v < value)
        return below / len(sorted_avgs)

    def _calculate_health_scores(
        self, metrics: dict[str, Any], category_percentile: float | None = None
    ) -> dict[str, int]:
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

        # Engagement: % of avg viewers who actively chatted (0-10% bad, 10-20% ok, 20%+ gut)
        engagement_rate = metrics.get("engagement_rate", 0)
        if metrics.get("chat_sample_count", 0) < 3:
            engagement = 50
        else:
            engagement = min(100, int(engagement_rate * 5))  # 20% engagement_rate = 100

        # Growth: Based on followers per hour (floor at 0, negative fph = 0 growth)
        fph = max(0, metrics.get("followers_per_hour", 0))
        growth = min(100, int(fph * 20))  # 5 fph = 100

        # Monetization: Placeholder (would need sub data)
        monetization = min(100, max(0, int(avg_viewers / 10)))

        # Network: Placeholder
        network = 50

        # Total: Weighted average
        total = int(
            reach * 0.2
            + retention * 0.25
            + engagement * 0.2
            + growth * 0.15
            + monetization * 0.1
            + network * 0.1
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

    def _generate_insights(self, metrics: dict[str, Any]) -> list[dict[str, str]]:
        """Generate findings/insights from metrics."""
        insights = []

        # Retention
        ret_10m = metrics.get("avg_retention_10m", 0)
        if metrics.get("retention_sample_count", 0) < 3:
            insights.append(
                {
                    "type": "info",
                    "title": "Retention-Daten unzureichend",
                    "text": "Zu wenige Sessions mit ≥3 Viewern für aussagekräftige Retention-Werte.",
                }
            )
        elif ret_10m < 40:
            insights.append(
                {
                    "type": "neg",
                    "title": "Niedrige Retention",
                    "text": f"10-Min Retention bei {ret_10m:.1f}%. Verbessere den Stream-Einstieg.",
                }
            )
        elif ret_10m > 65:
            insights.append(
                {
                    "type": "pos",
                    "title": "Starke Retention",
                    "text": f"Exzellente {ret_10m:.1f}% Retention. Dein Content fesselt!",
                }
            )

        # Chat
        chat_100 = metrics.get("chat_per_100", 0)
        if metrics.get("chat_sample_count", 0) < 3:
            insights.append(
                {
                    "type": "info",
                    "title": "Chat-Daten unzureichend",
                    "text": "Zu wenige Sessions mit ≥3 Viewern für aussagekräftige Chat-Metriken.",
                }
            )
        elif chat_100 < 5:
            insights.append(
                {
                    "type": "warn",
                    "title": "Niedrige Chat-Aktivität",
                    "text": f"Nur {chat_100:.1f} Chatter/100 Peak-Viewer (Proxy). Mehr Interaktion fördern!",
                }
            )
        elif chat_100 > 30:
            insights.append(
                {
                    "type": "pos",
                    "title": "Aktive Community",
                    "text": f"{chat_100:.1f} Chatter/100 Peak-Viewer (Proxy) - sehr engagiert!",
                }
            )

        # Followers (skip when no valid follower data)
        fph = metrics.get("followers_per_hour", 0)
        gained_fph = metrics.get("gained_followers_per_hour", 0)
        follower_data_valid = metrics.get("follower_valid_count", 0) > 0
        if not follower_data_valid:
            pass  # No reliable follower data — skip all follower insights
        elif fph < 0:
            insights.append(
                {
                    "type": "neg",
                    "title": "Follower-Verlust",
                    "text": f"Netto {fph:.2f} Follower/Stunde ({metrics.get('total_followers', 0):+d} gesamt). "
                    f"Gewonnen: {gained_fph:.2f}/h. Unfollows überwiegen.",
                }
            )
        elif fph < 0.5:
            insights.append(
                {
                    "type": "warn",
                    "title": "Langsames Follower-Wachstum",
                    "text": f"Nur {fph:.2f} Follower/Stunde. Regelmäßig an Follows erinnern!",
                }
            )
        elif fph > 3:
            insights.append(
                {
                    "type": "pos",
                    "title": "Starkes Wachstum",
                    "text": f"{fph:.1f} Follower/Stunde - ausgezeichnet!",
                }
            )

        return insights

    def _generate_actions(self, metrics: dict[str, Any]) -> list[dict[str, str]]:
        """Generate action recommendations."""
        actions = []

        ret_10m = metrics.get("avg_retention_10m", 0)
        if metrics.get("retention_sample_count", 0) >= 3 and ret_10m < 50:
            actions.append(
                {
                    "tag": "Retention",
                    "text": "Starte mit einem starken Hook in den ersten 2 Minuten.",
                    "priority": "high",
                }
            )

        chat_100 = metrics.get("chat_per_100", 0)
        if metrics.get("chat_sample_count", 0) >= 3 and chat_100 < 10:
            actions.append(
                {
                    "tag": "Engagement",
                    "text": "Stelle alle 5-10 Minuten eine direkte Frage an den Chat.",
                    "priority": "medium",
                }
            )

        fph = metrics.get("followers_per_hour", 0)
        follower_data_valid = metrics.get("follower_valid_count", 0) > 0
        if follower_data_valid and fph < 0:
            actions.append(
                {
                    "tag": "Growth",
                    "text": "Follower-Verlust! Prüfe ob Content-Wechsel oder lange Pausen Unfollows verursachen.",
                    "priority": "high",
                }
            )
        elif follower_data_valid and fph < 1:
            actions.append(
                {
                    "tag": "Growth",
                    "text": "Erinnere alle 20-30 Minuten an Follow mit konkretem Grund.",
                    "priority": "medium",
                }
            )

        return actions

    def _get_network_stats(self, conn, since_date: str, streamer: str | None) -> dict[str, int]:
        """Get raid network statistics."""
        if not streamer:
            return {"sent": 0, "received": 0, "sentViewers": 0}

        sent = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(viewer_count), 0)
            FROM twitch_raid_history
            WHERE LOWER(from_broadcaster_login) = ? AND executed_at >= ? AND success = 1
        """,
            [streamer.lower(), since_date],
        ).fetchone()

        received = conn.execute(
            """
            SELECT COUNT(*)
            FROM twitch_raid_history
            WHERE LOWER(to_broadcaster_login) = ? AND executed_at >= ? AND success = 1
        """,
            [streamer.lower(), since_date],
        ).fetchone()

        return {
            "sent": sent[0] if sent else 0,
            "sentViewers": int(sent[1]) if sent else 0,
            "received": received[0] if received else 0,
        }

    def _calculate_correlations(self, sessions: list[dict]) -> dict[str, float]:
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
            num = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=False))
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
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()
                streamer_login = streamer.lower() if streamer else None
                rows = conn.execute(
                    """
                    SELECT
                        CAST(strftime('%w', s.started_at) AS INTEGER) as weekday,
                        CAST(strftime('%H', s.started_at) AS INTEGER) as hour,
                        COUNT(*) as stream_count,
                        AVG(s.avg_viewers) as avg_viewers,
                        AVG(s.peak_viewers) as avg_peak
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                      AND (? IS NULL OR LOWER(s.streamer_login) = ?)
                    GROUP BY weekday, hour
                """,
                    [since_date, streamer_login, streamer_login],
                ).fetchall()

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
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()
                streamer_login = streamer.lower() if streamer else None
                rows = conn.execute(
                    """
                    SELECT
                        DATE(s.started_at) as date,
                        COUNT(*) as stream_count,
                        SUM(s.avg_viewers * s.duration_seconds / 3600.0) as hours_watched
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                      AND (? IS NULL OR LOWER(s.streamer_login) = ?)
                    GROUP BY DATE(s.started_at)
                """,
                    [since_date, streamer_login, streamer_login],
                ).fetchall()

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
                since_date = (datetime.now(UTC) - timedelta(days=months * 30)).isoformat()
                streamer_login = streamer.lower() if streamer else None
                rows = conn.execute(
                    """
                    SELECT
                        CAST(strftime('%Y', s.started_at) AS INTEGER) as year,
                        CAST(strftime('%m', s.started_at) AS INTEGER) as month,
                        SUM(s.avg_viewers * s.duration_seconds / 3600.0) as hours_watched,
                        SUM(s.duration_seconds / 3600.0) as airtime,
                        AVG(s.avg_viewers) as avg_viewers,
                        MAX(s.peak_viewers) as peak_viewers,
                        SUM(CASE WHEN s.follower_delta IS NOT NULL
                             AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                             THEN s.follower_delta ELSE 0 END) as follower_delta,
                        SUM(s.unique_chatters) as unique_chatters,
                        COUNT(*) as stream_count
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                      AND (? IS NULL OR LOWER(s.streamer_login) = ?)
                    GROUP BY year, month
                    ORDER BY year DESC, month DESC
                """,
                    [since_date, streamer_login, streamer_login],
                ).fetchall()

                month_names = [
                    "",
                    "Jan",
                    "Feb",
                    "Mär",
                    "Apr",
                    "Mai",
                    "Jun",
                    "Jul",
                    "Aug",
                    "Sep",
                    "Okt",
                    "Nov",
                    "Dez",
                ]
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
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()
                streamer_login = streamer.lower() if streamer else None
                rows = conn.execute(
                    """
                    SELECT
                        CAST(strftime('%w', s.started_at) AS INTEGER) as weekday,
                        COUNT(*) as stream_count,
                        AVG(s.duration_seconds / 3600.0) as avg_hours,
                        AVG(s.avg_viewers) as avg_viewers,
                        AVG(s.peak_viewers) as avg_peak,
                        SUM(CASE WHEN s.follower_delta IS NOT NULL
                             AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                             THEN s.follower_delta ELSE 0 END) as total_followers
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                      AND (? IS NULL OR LOWER(s.streamer_login) = ?)
                    GROUP BY weekday
                    ORDER BY weekday
                """,
                    [since_date, streamer_login, streamer_login],
                ).fetchall()

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
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()

                if not streamer:
                    return web.json_response({"error": "Streamer required"}, status=400)
                streamer_login = streamer.lower()

                # Session context for normalization (e.g. messages/minute, loyalty score).
                session_stats = conn.execute(
                    """
                    SELECT
                        COUNT(*) as session_count,
                        COALESCE(SUM(s.duration_seconds), 0) as total_duration_seconds
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND LOWER(s.streamer_login) = ?
                      AND s.ended_at IS NOT NULL
                    """,
                    [since_date, streamer_login],
                ).fetchone()
                session_count = int(session_stats[0]) if session_stats and session_stats[0] else 0
                total_duration_seconds = (
                    float(session_stats[1]) if session_stats and session_stats[1] else 0.0
                )

                # True message counts from raw chat events in the selected time range.
                all_messages = conn.execute(
                    """
                    SELECT message_ts, content, is_command, chatter_login, chatter_id
                    FROM twitch_chat_messages
                    WHERE message_ts >= ?
                      AND LOWER(streamer_login) = ?
                    """,
                    [since_date, streamer_login],
                ).fetchall()

                total_messages = len(all_messages)
                command_messages = 0
                distinct_chatters_set = set()

                type_counts = collections.Counter()
                hour_counts = collections.Counter()

                for r in all_messages:
                    ts_str = r[0]
                    content = r[1] or ""
                    is_cmd = r[2]
                    chatter_key = r[3] or r[4] or ""

                    if is_cmd:
                        command_messages += 1
                    if chatter_key:
                        distinct_chatters_set.add(chatter_key)

                    # Type Analysis
                    msg_type = self._classify_message(content)
                    type_counts[msg_type] += 1

                    # Hourly Analysis
                    try:
                        # Assumes ISO format YYYY-MM-DDTHH:MM:SS...
                        if "T" in ts_str:
                            # Extract HH
                            hour_str = ts_str.split("T")[1][:2]
                            hour_counts[int(hour_str)] += 1
                        elif " " in ts_str:
                            # Fallback for YYYY-MM-DD HH:MM:SS
                            hour_str = ts_str.split(" ")[1][:2]
                            hour_counts[int(hour_str)] += 1
                    except (TypeError, ValueError, IndexError):
                        log.debug(
                            "Skipping invalid chat message timestamp: %r",
                            ts_str,
                            exc_info=True,
                        )

                distinct_chatters_from_messages = len(distinct_chatters_set)

                # Chatter cohort split + lurker stats from session-level chatter table.
                cohort_stats = conn.execute(
                    """
                    SELECT
                        COUNT(DISTINCT COALESCE(NULLIF(sc.chatter_login, ''), sc.chatter_id)) as unique_chatters,
                        COUNT(
                            DISTINCT CASE
                                WHEN sc.is_first_time_global = 1
                                THEN COALESCE(NULLIF(sc.chatter_login, ''), sc.chatter_id)
                            END
                        ) as first_time_chatters,
                        COUNT(DISTINCT sc.session_id) as sessions_with_chat,
                        COUNT(*) as total_unique_viewers,
                        SUM(CASE WHEN sc.messages = 0 AND sc.seen_via_chatters_api = 1 THEN 1 ELSE 0 END) as lurkers,
                        SUM(CASE WHEN sc.messages > 0 THEN 1 ELSE 0 END) as active_chatters_count,
                        ROUND(AVG(CASE WHEN sc.messages > 0 THEN sc.messages ELSE NULL END), 1) as avg_messages_per_chatter,
                        SUM(CASE WHEN sc.seen_via_chatters_api = 1 THEN 1 ELSE 0 END) as chatters_api_seen
                    FROM twitch_session_chatters sc
                    JOIN twitch_stream_sessions s ON s.id = sc.session_id
                    WHERE s.started_at >= ?
                      AND LOWER(s.streamer_login) = ?
                      AND s.ended_at IS NOT NULL
                    """,
                    [since_date, streamer_login],
                ).fetchone()
                unique_chatters = int(cohort_stats[0]) if cohort_stats and cohort_stats[0] else 0
                first_time_chatters = (
                    int(cohort_stats[1]) if cohort_stats and cohort_stats[1] else 0
                )
                sessions_with_chat = int(cohort_stats[2]) if cohort_stats and cohort_stats[2] else 0
                total_unique_viewers = (
                    int(cohort_stats[3]) if cohort_stats and cohort_stats[3] else 0
                )
                lurker_count = int(cohort_stats[4]) if cohort_stats and cohort_stats[4] else 0
                active_chatters_count = (
                    int(cohort_stats[5]) if cohort_stats and cohort_stats[5] else 0
                )
                avg_messages_per_chatter = (
                    float(cohort_stats[6]) if cohort_stats and cohort_stats[6] else 0.0
                )
                chatters_api_seen = int(cohort_stats[7]) if cohort_stats and cohort_stats[7] else 0
                lurker_ratio = (
                    round(lurker_count / total_unique_viewers, 3)
                    if total_unique_viewers > 0
                    else 0.0
                )
                active_ratio = (
                    round(active_chatters_count / total_unique_viewers, 3)
                    if total_unique_viewers > 0
                    else 0.0
                )
                chatters_api_coverage = (
                    round(chatters_api_seen / total_unique_viewers, 3)
                    if total_unique_viewers > 0
                    else 0.0
                )

                # Fallback for older rows where session_chatters may be sparse.
                if unique_chatters == 0 and distinct_chatters_from_messages > 0:
                    unique_chatters = distinct_chatters_from_messages
                    first_time_chatters = 0

                returning_chatters = max(0, unique_chatters - first_time_chatters)
                total_minutes = total_duration_seconds / 60.0 if total_duration_seconds > 0 else 0.0
                messages_per_minute = (total_messages / total_minutes) if total_minutes > 0 else 0.0
                chatter_return_rate = (
                    (returning_chatters / unique_chatters) * 100.0 if unique_chatters > 0 else 0.0
                )

                # Top chatters in selected period (not all-time rollup).
                top = conn.execute(
                    """
                    SELECT
                        COALESCE(NULLIF(cm.chatter_login, ''), cm.chatter_id, 'unknown') as chatter_key,
                        COUNT(*) as messages,
                        COUNT(DISTINCT cm.session_id) as sessions,
                        MIN(cm.message_ts) as first_seen,
                        MAX(cm.message_ts) as last_seen
                    FROM twitch_chat_messages cm
                    WHERE cm.message_ts >= ?
                      AND LOWER(cm.streamer_login) = ?
                    GROUP BY COALESCE(NULLIF(cm.chatter_login, ''), cm.chatter_id, 'unknown')
                    ORDER BY messages DESC
                    LIMIT 20
                    """,
                    [since_date, streamer_login],
                ).fetchall()

                return web.json_response(
                    {
                        "totalMessages": total_messages,
                        "uniqueChatters": unique_chatters,
                        "firstTimeChatters": first_time_chatters,
                        "returningChatters": returning_chatters,
                        "messagesPerMinute": round(messages_per_minute, 2),
                        "chatterReturnRate": round(chatter_return_rate, 1),
                        "commandMessages": command_messages,
                        "nonCommandMessages": max(0, total_messages - command_messages),
                        "lurkerRatio": lurker_ratio,
                        "lurkerCount": lurker_count,
                        "activeChatters": active_chatters_count,
                        "activeRatio": active_ratio,
                        "avgMessagesPerChatter": avg_messages_per_chatter,
                        "topChatters": [
                            {
                                "login": r[0],
                                "totalMessages": int(r[1]) if r[1] else 0,
                                "totalSessions": int(r[2]) if r[2] else 0,
                                "firstSeen": r[3],
                                "lastSeen": r[4],
                                "loyaltyScore": round(
                                    min(
                                        100.0,
                                        ((int(r[2]) if r[2] else 0) / max(1, session_count))
                                        * 100.0,
                                    ),
                                    1,
                                ),
                            }
                            for r in top
                        ],
                        "messageTypes": [
                            {
                                "type": k,
                                "count": v,
                                "percentage": round(v / total_messages * 100, 1)
                                if total_messages > 0
                                else 0,
                            }
                            for k, v in type_counts.most_common()
                        ],
                        "hourlyActivity": [
                            {"hour": h, "count": hour_counts.get(h, 0)} for h in range(24)
                        ],
                        "dataQuality": {
                            "sessions": session_count,
                            "sessionsWithChat": sessions_with_chat,
                            "chatSessionCoverage": round(
                                (sessions_with_chat / session_count) * 100.0, 1
                            )
                            if session_count > 0
                            else 0.0,
                            "chattersApiCoverage": chatters_api_coverage,
                        },
                    }
                )
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
                rows = conn.execute(
                    """
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
                """,
                    [streamer.lower(), streamer.lower(), limit],
                ).fetchall()

                # Get total chatters for percentage
                total = (
                    conn.execute(
                        """
                    SELECT COUNT(DISTINCT chatter_login)
                    FROM twitch_chatter_rollup
                    WHERE LOWER(streamer_login) = ?
                """,
                        [streamer.lower()],
                    ).fetchone()[0]
                    or 1
                )

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
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()

                if metric == "retention":
                    ranking_sql = """
                    SELECT
                        s.streamer_login,
                        AVG(s.retention_10m) as value
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL
                    GROUP BY s.streamer_login
                    HAVING COUNT(*) >= 3
                    ORDER BY value DESC
                    LIMIT ?
                    """
                elif metric == "growth":
                    ranking_sql = """
                    SELECT
                        s.streamer_login,
                        SUM(CASE WHEN s.follower_delta IS NOT NULL
                             AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                             THEN s.follower_delta ELSE 0 END) as value
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL
                    GROUP BY s.streamer_login
                    HAVING COUNT(*) >= 3
                    ORDER BY value DESC
                    LIMIT ?
                    """
                else:
                    metric = "viewers"
                    ranking_sql = """
                    SELECT
                        s.streamer_login,
                        AVG(s.avg_viewers) as value
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL
                    GROUP BY s.streamer_login
                    HAVING COUNT(*) >= 3
                    ORDER BY value DESC
                    LIMIT ?
                    """

                rows = conn.execute(ranking_sql, [since_date, limit]).fetchall()

                data = [
                    {
                        "rank": i + 1,
                        "login": r[0],
                        "value": (float(r[1]) * 100 if metric == "retention" else float(r[1]))
                        if r[1]
                        else 0,
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
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()

                # Your stats from stats_tracked (higher accuracy for tracked streamers)
                your_tracked = conn.execute(
                    """
                    SELECT AVG(viewer_count), MAX(viewer_count)
                    FROM twitch_stats_tracked
                    WHERE ts_utc >= ? AND LOWER(streamer) = ?
                """,
                    [since_date, streamer.lower()],
                ).fetchone()

                # Fallback to session data
                your_session = conn.execute(
                    """
                    SELECT
                        AVG(s.avg_viewers) as avg_viewers,
                        MAX(s.peak_viewers) as peak_viewers,
                        AVG(s.retention_10m) as retention10m,
                        AVG(CASE WHEN s.avg_viewers > 0 THEN s.unique_chatters * 100.0 / s.avg_viewers ELSE 0 END) as chat_health
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """,
                    [since_date, streamer.lower()],
                ).fetchone()

                # Use tracked data if available, else session data
                your_avg = (
                    float(your_tracked[0])
                    if your_tracked and your_tracked[0]
                    else (float(your_session[0]) if your_session and your_session[0] else 0)
                )
                your_peak = (
                    int(your_tracked[1])
                    if your_tracked and your_tracked[1]
                    else (int(your_session[1]) if your_session and your_session[1] else 0)
                )
                your_ret = float(your_session[2]) * 100 if your_session and your_session[2] else 0
                your_chat = float(your_session[3]) if your_session and your_session[3] else 0

                # Category stats from stats_category (per-streamer aggregates)
                cat_data = self._get_category_percentiles(conn, since_date)
                sorted_avgs = cat_data["sorted_avgs"]
                category_total = cat_data["total"]

                # Category averages
                cat_avg_viewers = sum(sorted_avgs) / len(sorted_avgs) if sorted_avgs else 0

                # Peak viewers per streamer from category
                cat_peak = conn.execute(
                    """
                    SELECT AVG(max_vc) FROM (
                        SELECT MAX(viewer_count) as max_vc
                        FROM twitch_stats_category
                        WHERE ts_utc >= ?
                        GROUP BY streamer
                    )
                """,
                    [since_date],
                ).fetchone()
                cat_avg_peak = float(cat_peak[0]) if cat_peak and cat_peak[0] else 0

                # Category-wide retention and chat health from session data
                cat_session_avgs = conn.execute(
                    """
                    SELECT
                        AVG(s.retention_10m) as avg_ret,
                        AVG(CASE WHEN s.avg_viewers > 0 THEN s.unique_chatters * 100.0 / s.avg_viewers ELSE 0 END) as avg_chat
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL
                """,
                    [since_date],
                ).fetchone()
                cat_avg_ret = (
                    float(cat_session_avgs[0]) * 100
                    if cat_session_avgs and cat_session_avgs[0]
                    else 0
                )
                cat_avg_chat = (
                    float(cat_session_avgs[1]) if cat_session_avgs and cat_session_avgs[1] else 0
                )

                # Per-streamer retention and chat for percentile ranking
                per_streamer_ret = conn.execute(
                    """
                    SELECT AVG(s.retention_10m) as ret
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL
                    GROUP BY LOWER(s.streamer_login)
                    ORDER BY ret
                """,
                    [since_date],
                ).fetchall()
                ret_sorted = [float(r[0]) * 100 for r in per_streamer_ret if r[0] is not None]

                per_streamer_chat = conn.execute(
                    """
                    SELECT AVG(CASE WHEN s.avg_viewers > 0 THEN s.unique_chatters * 100.0 / s.avg_viewers ELSE 0 END) as ch
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.ended_at IS NOT NULL
                    GROUP BY LOWER(s.streamer_login)
                    ORDER BY ch
                """,
                    [since_date],
                ).fetchall()
                chat_sorted = [float(r[0]) for r in per_streamer_chat if r[0] is not None]

                # Percentiles for avgViewers
                avg_percentile = (
                    int(self._percentile_of(sorted_avgs, your_avg) * 100) if sorted_avgs else 0
                )

                # Percentile for peakViewers
                peak_avgs = conn.execute(
                    """
                    SELECT MAX(viewer_count) as peak
                    FROM twitch_stats_category
                    WHERE ts_utc >= ?
                    GROUP BY streamer
                    ORDER BY peak
                """,
                    [since_date],
                ).fetchall()
                peak_sorted = [float(r[0]) for r in peak_avgs] if peak_avgs else []
                peak_percentile = (
                    int(self._percentile_of(peak_sorted, your_peak) * 100) if peak_sorted else 50
                )

                # Percentiles for retention and chat
                ret_percentile = (
                    int(self._percentile_of(ret_sorted, your_ret) * 100) if ret_sorted else 50
                )
                chat_percentile = (
                    int(self._percentile_of(chat_sorted, your_chat) * 100) if chat_sorted else 50
                )

                # Category rank (1 = best)
                category_rank = (
                    category_total - int(avg_percentile / 100 * category_total)
                    if category_total
                    else 0
                )

                return web.json_response(
                    {
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
                    }
                )
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
                    FROM twitch_streamers_partner_state
                    WHERE is_partner_active = 1
                    ORDER BY twitch_login
                """).fetchall()

                # Others with recent activity
                others = conn.execute("""
                    SELECT DISTINCT s.streamer_login
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= datetime('now', '-30 days')
                      AND LOWER(s.streamer_login) NOT IN (
                          SELECT LOWER(twitch_login)
                            FROM twitch_streamers_partner_state
                           WHERE is_partner_active = 1
                      )
                    ORDER BY s.streamer_login
                """).fetchall()

                data = [{"login": r[0], "isPartner": True} for r in partners] + [
                    {"login": r[0], "isPartner": False} for r in others
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
                row = conn.execute(
                    """
                    SELECT
                        s.id, s.streamer_login, s.started_at, s.ended_at,
                        s.duration_seconds, s.start_viewers, s.peak_viewers, s.end_viewers,
                        s.avg_viewers, s.retention_5m, s.retention_10m, s.retention_20m,
                        s.dropoff_pct, s.unique_chatters, s.first_time_chatters,
                        s.returning_chatters, s.stream_title
                    FROM twitch_stream_sessions s
                    WHERE s.id = ?
                """,
                    [session_id],
                ).fetchone()

                if not row:
                    return web.json_response({"error": "Session not found"}, status=404)

                # Timeline
                timeline = conn.execute(
                    """
                    SELECT minutes_from_start, viewer_count
                    FROM twitch_session_viewers
                    WHERE session_id = ?
                    ORDER BY minutes_from_start
                """,
                    [session_id],
                ).fetchall()

                # Top chatters
                chatters = conn.execute(
                    """
                    SELECT chatter_login, messages
                    FROM twitch_session_chatters
                    WHERE session_id = ?
                    ORDER BY messages DESC
                    LIMIT 20
                """,
                    [session_id],
                ).fetchall()

                return web.json_response(
                    {
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
                    }
                )
        except Exception as exc:
            log.exception("Error in session detail API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_auth_status(self, request: web.Request) -> web.Response:
        """Get current authentication status and permissions."""
        auth_level = self._get_auth_level(request)
        session = self._get_dashboard_session(request) or {}
        is_authenticated = auth_level != "none"
        can_view_all_streamers = is_authenticated

        return web.json_response(
            {
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
                },
            }
        )

    # ==================== NEW AUDIENCE ANALYTICS ENDPOINTS ====================

    def _calc_watch_distribution(
        self, sessions, conn=None, session_ids: list = None
    ) -> dict[str, Any]:
        """Calculate watch time distribution.

        Tries to use real per-viewer watch-time from chatters-API snapshots
        (last_seen_at - first_message_at) when sufficient data is available.
        Falls back to a retention-curve proxy otherwise.
        """
        if not sessions:
            return {
                "under5min": 0,
                "min5to15": 0,
                "min15to30": 0,
                "min30to60": 0,
                "over60min": 0,
                "avgWatchTime": 0,
                "medianWatchTime": 0,
                "sessionCount": 0,
                "dataQuality": {"method": "no_data", "coverage": 0.0},
            }

        total_sessions = len(sessions)

        # --- Attempt: real watch-time from chatters-API snapshots ---
        real_minutes: list[float] = []
        if conn is not None and session_ids:
            if session_ids:
                session_ids_json = json.dumps([int(sid) for sid in session_ids])
                rows = conn.execute(
                    """
                    SELECT
                        ROUND((julianday(COALESCE(last_seen_at, first_message_at))
                               - julianday(first_message_at)) * 24 * 60) as watch_minutes
                    FROM twitch_session_chatters
                    WHERE session_id IN (
                        SELECT CAST(value AS INTEGER) FROM json_each(?)
                    )
                      AND last_seen_at IS NOT NULL
                    """,
                    (session_ids_json,),
                ).fetchall()
                real_minutes = [float(r[0]) for r in rows if r[0] is not None and r[0] >= 0]

        # Decide whether real data is sufficient (≥30% coverage vs session count)
        use_real = len(real_minutes) >= total_sessions * 0.3 and len(real_minutes) >= 3

        if use_real:
            total_viewers = len(real_minutes)
            under_5min = sum(1 for m in real_minutes if m < 5) / total_viewers * 100
            min_5_to_15 = sum(1 for m in real_minutes if 5 <= m < 15) / total_viewers * 100
            min_15_to_30 = sum(1 for m in real_minutes if 15 <= m < 30) / total_viewers * 100
            min_30_to_60 = sum(1 for m in real_minutes if 30 <= m < 60) / total_viewers * 100
            over_60min = sum(1 for m in real_minutes if m >= 60) / total_viewers * 100
            avg_watch_time = sum(real_minutes) / total_viewers
            # Median
            sorted_m = sorted(real_minutes)
            mid = len(sorted_m) // 2
            median_watch_time = (
                sorted_m[mid] if len(sorted_m) % 2 == 1 else (sorted_m[mid - 1] + sorted_m[mid]) / 2
            )
            # Approximate total-session coverage ratio
            total_viewers_in_sessions = sum(int(s[4] or 0) for s in sessions)  # avg_viewers col
            coverage = (
                min(1.0, total_viewers / max(1, total_viewers_in_sessions))
                if total_viewers_in_sessions > 0
                else 0.0
            )
            data_quality = {
                "method": "chatters_api_snapshots",
                "coverage": round(coverage, 2),
            }
        else:
            # Retention-curve proxy (original method)
            ret_5m_avg = sum((s[1] or 0) * 100 for s in sessions) / total_sessions
            ret_10m_avg = sum((s[2] or 0) * 100 for s in sessions) / total_sessions
            ret_20m_avg = sum((s[3] or 0) * 100 for s in sessions) / total_sessions

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
                (under_5min / 100) * 2.5
                + (min_5_to_15 / 100) * 10
                + (min_15_to_30 / 100) * 22.5
                + (min_30_to_60 / 100) * 45
                + (over_60min / 100) * min(90, avg_duration_mins)
            )
            median_watch_time = avg_watch_time * 0.85
            data_quality = {
                "method": "retention_curve_proxy",
                "coverage": round(len(real_minutes) / max(1, total_sessions), 2),
            }

        return {
            "under5min": round(max(0, under_5min), 1),
            "min5to15": round(max(0, min_5_to_15), 1),
            "min15to30": round(max(0, min_15_to_30), 1),
            "min30to60": round(max(0, min_30_to_60), 1),
            "over60min": round(max(0, over_60min), 1),
            "avgWatchTime": round(avg_watch_time, 1),
            "medianWatchTime": round(median_watch_time, 1),
            "sessionCount": total_sessions,
            "dataQuality": data_quality,
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
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()
                prev_since_date = (datetime.now(UTC) - timedelta(days=days * 2)).isoformat()

                # Current period
                current_sessions = conn.execute(
                    """
                    SELECT s.id, s.retention_5m, s.retention_10m,
                           s.retention_20m, s.avg_viewers, s.start_viewers, s.end_viewers,
                           s.duration_seconds
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """,
                    [since_date, streamer.lower()],
                ).fetchall()
                # Remap: (duration_seconds, ret5, ret10, ret20, avg_viewers, ...) for _calc
                current_sessions_remapped = [
                    (r[7], r[1], r[2], r[3], r[4]) for r in current_sessions
                ]

                # Previous period
                prev_sessions = conn.execute(
                    """
                    SELECT s.id, s.retention_5m, s.retention_10m,
                           s.retention_20m, s.avg_viewers, s.start_viewers, s.end_viewers,
                           s.duration_seconds
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.started_at < ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """,
                    [prev_since_date, since_date, streamer.lower()],
                ).fetchall()
                prev_sessions_remapped = [(r[7], r[1], r[2], r[3], r[4]) for r in prev_sessions]

                current_ids = [r[0] for r in current_sessions]
                prev_ids = [r[0] for r in prev_sessions]
                current = self._calc_watch_distribution(
                    current_sessions_remapped, conn=conn, session_ids=current_ids
                )
                previous = self._calc_watch_distribution(
                    prev_sessions_remapped, conn=conn, session_ids=prev_ids
                )

                # Calculate deltas
                deltas = {}
                for key in [
                    "under5min",
                    "min5to15",
                    "min15to30",
                    "min30to60",
                    "over60min",
                    "avgWatchTime",
                ]:
                    curr_val = current.get(key, 0)
                    prev_val = previous.get(key, 0)
                    if prev_val > 0:
                        deltas[key] = round(((curr_val - prev_val) / prev_val) * 100, 1)
                    else:
                        deltas[key] = None

                session_count = int(current.get("sessionCount", 0) or 0)
                if session_count >= 20:
                    confidence = "medium"
                elif session_count >= 8:
                    confidence = "low"
                else:
                    confidence = "very_low"

                dq = current.get("dataQuality", {})
                return web.json_response(
                    {
                        **current,
                        "previous": previous,
                        "deltas": deltas,
                        "dataQuality": {
                            "confidence": confidence,
                            "sessions": session_count,
                            "method": dq.get("method", "retention_curve_proxy"),
                            "coverage": dq.get("coverage", 0.0),
                        },
                    }
                )
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
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()
                streamer_login = streamer.lower()

                # Base session and follower stats.
                stats = conn.execute(
                    """
                    SELECT
                        COUNT(*) as session_count,
                        COALESCE(SUM(s.duration_seconds), 0) as total_duration,
                        AVG(s.avg_viewers) as avg_viewers,
                        COALESCE(SUM(s.avg_viewers * s.duration_seconds / 3600.0), 0) as total_hours_watched,
                        AVG(s.retention_10m) as avg_retention_10m,
                        SUM(CASE WHEN s.follower_delta IS NOT NULL
                             AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                             THEN s.follower_delta ELSE 0 END) as net_followers,
                        SUM(CASE WHEN s.follower_delta > 0
                             AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                             THEN s.follower_delta ELSE 0 END) as gained_followers,
                        COUNT(CASE WHEN s.follower_delta IS NOT NULL
                             AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                             THEN 1 END) as follower_valid_samples
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND LOWER(s.streamer_login) = ?
                      AND s.ended_at IS NOT NULL
                    """,
                    [since_date, streamer_login],
                ).fetchone()

                session_count = int(stats[0]) if stats and stats[0] else 0
                if session_count == 0:
                    return web.json_response(
                        {
                            "uniqueViewers": 0,
                            "returningViewers": 0,
                            "newFollowers": 0,
                            "followsDuringStream": 0,
                            "netFollowerDelta": 0,
                            "conversionRate": 0,
                            "conversionDataSource": "session_delta_fallback",
                            "avgTimeToFollow": 0,
                            "followersBySource": {
                                "organic": 0,
                                "raids": 0,
                                "hosts": 0,
                                "other": 0,
                            },
                            "dataQuality": {
                                "confidence": "low",
                                "reason": "no_sessions",
                            },
                        }
                    )

                total_duration = float(stats[1]) if stats[1] else 0.0
                avg_viewers = float(stats[2]) if stats[2] else 0.0
                total_hours_watched = float(stats[3]) if stats[3] else 0.0
                avg_retention_10m = float(stats[4]) if stats[4] else 0.0  # 0..1
                net_followers = int(stats[5]) if stats[5] else 0
                gained_followers = int(stats[6]) if stats[6] else 0
                follower_valid_samples = int(stats[7]) if stats[7] else 0

                # Distinct chatter cohorts in selected period (less inflation than SUM per session).
                chatter_stats = conn.execute(
                    """
                    SELECT
                        COUNT(DISTINCT COALESCE(NULLIF(sc.chatter_login, ''), sc.chatter_id)) as unique_chatters,
                        COUNT(
                            DISTINCT CASE
                                WHEN sc.is_first_time_global = 0
                                THEN COALESCE(NULLIF(sc.chatter_login, ''), sc.chatter_id)
                            END
                        ) as returning_chatters,
                        COUNT(*) as total_viewers_tracked
                    FROM twitch_session_chatters sc
                    JOIN twitch_stream_sessions s ON s.id = sc.session_id
                    WHERE s.started_at >= ?
                      AND LOWER(s.streamer_login) = ?
                      AND s.ended_at IS NOT NULL
                    """,
                    [since_date, streamer_login],
                ).fetchone()
                unique_chatters = int(chatter_stats[0]) if chatter_stats and chatter_stats[0] else 0
                returning_chatters = (
                    int(chatter_stats[1]) if chatter_stats and chatter_stats[1] else 0
                )
                total_viewers_tracked = (
                    int(chatter_stats[2]) if chatter_stats and chatter_stats[2] else 0
                )

                # Real follow events during streams (primary source)
                follow_events_row = conn.execute(
                    """
                    SELECT COUNT(*) as follows_during_stream
                    FROM twitch_follow_events fe
                    JOIN twitch_stream_sessions ss
                        ON ss.streamer_login = fe.streamer_login
                       AND fe.followed_at BETWEEN ss.started_at
                           AND COALESCE(ss.ended_at, datetime('now'))
                    WHERE LOWER(ss.streamer_login) = ?
                      AND ss.started_at >= ?
                      AND ss.ended_at IS NOT NULL
                    """,
                    [streamer_login, since_date],
                ).fetchone()
                follows_during_stream = (
                    int(follow_events_row[0]) if follow_events_row and follow_events_row[0] else 0
                )

                # Use chatters-API total_viewers_tracked as unique viewer count when available
                unique_viewers = max(total_viewers_tracked, unique_chatters, 1)
                if total_viewers_tracked == 0:
                    # Fallback: estimate unique viewers
                    avg_watch_minutes_proxy = max(
                        8.0, min(75.0, 8.0 + (avg_retention_10m * 100.0 * 0.55))
                    )
                    unique_from_hours = int(
                        (total_hours_watched * 60.0) / max(1.0, avg_watch_minutes_proxy)
                    )
                    unique_from_chat = int(unique_chatters * 2.0)
                    unique_from_concurrency = int(
                        avg_viewers
                        * max(
                            1.5,
                            min(4.0, (total_duration / 3600.0) / max(1.0, session_count)),
                        )
                    )
                    unique_viewers = max(
                        unique_from_hours, unique_from_chat, unique_from_concurrency, 1
                    )

                # Follow conversion: prefer real events, fallback to session delta
                if follows_during_stream > 0:
                    conversion_source = "follow_events"
                    gained_followers_for_conversion = follows_during_stream
                else:
                    conversion_source = "session_delta_fallback"
                    gained_followers_for_conversion = gained_followers

                conversion_rate = (
                    (gained_followers_for_conversion / unique_viewers * 100.0)
                    if unique_viewers > 0
                    else 0.0
                )

                # Estimated time-to-follow: scaled to session length, bounded to practical range.
                avg_session_mins = (
                    (total_duration / max(1, session_count) / 60.0) if total_duration > 0 else 0.0
                )
                avg_time_to_follow = max(5.0, min(45.0, avg_session_mins * 0.4))

                # Raid inflow proxy for source split.
                raids_received = conn.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(viewer_count), 0)
                    FROM twitch_raid_history
                    WHERE LOWER(to_broadcaster_login) = ?
                      AND executed_at >= ?
                      AND success = 1
                    """,
                    [streamer_login, since_date],
                ).fetchone()
                raid_count = int(raids_received[0]) if raids_received and raids_received[0] else 0
                raid_viewers = int(raids_received[1]) if raids_received and raids_received[1] else 0

                # Conservative conversion assumption for raid-origin follows.
                raid_followers = min(int(raid_viewers * 0.05), gained_followers_for_conversion)
                organic_followers = max(0, gained_followers_for_conversion - raid_followers)

                if follower_valid_samples >= max(3, int(session_count * 0.6)):
                    confidence = "high"
                elif follower_valid_samples >= 1:
                    confidence = "medium"
                else:
                    confidence = "low"

                return web.json_response(
                    {
                        "uniqueViewers": unique_viewers,
                        "returningViewers": returning_chatters,
                        "newFollowers": gained_followers_for_conversion,
                        "followsDuringStream": follows_during_stream,
                        "netFollowerDelta": net_followers,
                        "conversionRate": round(conversion_rate, 2),
                        "conversionDataSource": conversion_source,
                        "avgTimeToFollow": round(avg_time_to_follow, 0),
                        "followersBySource": {
                            "organic": organic_followers,
                            "raids": raid_followers,
                            "hosts": 0,  # Host-specific attribution is not tracked.
                            "other": 0,
                        },
                        "dataQuality": {
                            "confidence": confidence,
                            "sessions": session_count,
                            "followerValidSamples": follower_valid_samples,
                            "raidEvents": raid_count,
                            "uniqueViewersMethod": "chatters_api_snapshots"
                            if total_viewers_tracked > 0
                            else "estimated_from_viewer_hours_and_chatters",
                        },
                    }
                )
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
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()
                streamer_login = streamer.lower() if streamer else None

                # Get tags from sessions (tags stored as JSON or comma-separated in tags column)
                rows = conn.execute(
                    """
                    SELECT
                        s.tags,
                        AVG(s.avg_viewers) as avg_viewers,
                        AVG(s.retention_10m) as avg_retention,
                        AVG(CASE WHEN s.follower_delta IS NOT NULL
                             AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                             THEN s.follower_delta ELSE NULL END) as avg_followers,
                        COUNT(*) as usage_count,
                        AVG(s.duration_seconds) as avg_duration,
                        strftime('%H', s.started_at) as start_hour
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ?
                      AND s.ended_at IS NOT NULL
                      AND s.tags IS NOT NULL
                      AND (? IS NULL OR LOWER(s.streamer_login) = ?)
                    GROUP BY s.tags
                    ORDER BY avg_viewers DESC
                    LIMIT ?
                """,
                    [since_date, streamer_login, streamer_login, limit],
                ).fetchall()

                # Parse and aggregate tags
                tag_stats: dict[str, dict[str, Any]] = {}
                for row in rows:
                    tags_str = row[0] or ""
                    # Handle JSON array or comma-separated
                    if tags_str.startswith("["):
                        try:
                            tags = json.loads(tags_str)
                        except json.JSONDecodeError:
                            tags = [tags_str]
                    else:
                        tags = [t.strip() for t in tags_str.split(",") if t.strip()]

                    for tag in tags[:5]:  # Max 5 tags per session
                        if tag not in tag_stats:
                            tag_stats[tag] = {
                                "viewers": [],
                                "retention": [],
                                "followers": [],
                                "count": 0,
                                "durations": [],
                                "hours": [],
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
                    key=lambda x: (
                        sum(x[1]["viewers"]) / len(x[1]["viewers"]) if x[1]["viewers"] else 0
                    ),
                    reverse=True,
                )

                for rank, (tag, data) in enumerate(sorted_tags[:limit], 1):
                    avg_v = sum(data["viewers"]) / len(data["viewers"]) if data["viewers"] else 0
                    avg_r = (
                        sum(data["retention"]) / len(data["retention"]) if data["retention"] else 0
                    )
                    avg_f = (
                        sum(data["followers"]) / len(data["followers"]) if data["followers"] else 0
                    )
                    avg_d = (
                        sum(data["durations"]) / len(data["durations"]) if data["durations"] else 0
                    )

                    # Best time slot
                    if data["hours"]:
                        hour_counts = collections.Counter(data["hours"])
                        best_hour = hour_counts.most_common(1)[0][0]
                        best_slot = f"{best_hour:02d}:00-{(best_hour + 4) % 24:02d}:00"
                    else:
                        best_slot = "18:00-22:00"

                    result.append(
                        {
                            "tagName": tag,
                            "usageCount": data["count"],
                            "avgViewers": round(avg_v, 1),
                            "avgRetention10m": round(avg_r, 1),
                            "avgFollowerGain": round(avg_f, 1),
                            "trend": "stable",  # Would need historical comparison
                            "trendValue": 0,
                            "bestTimeSlot": best_slot,
                            "avgStreamDuration": round(avg_d, 0),
                            "categoryRank": rank,
                        }
                    )

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
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()

                rows = conn.execute(
                    """
                    SELECT
                        s.stream_title,
                        COUNT(*) as usage_count,
                        AVG(s.avg_viewers) as avg_viewers,
                        AVG(s.retention_10m) as avg_retention,
                        AVG(CASE WHEN s.follower_delta IS NOT NULL
                             AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                             THEN s.follower_delta ELSE NULL END) as avg_followers,
                        MAX(s.peak_viewers) as peak_viewers
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ?
                      AND s.ended_at IS NOT NULL AND s.stream_title IS NOT NULL AND s.stream_title != ''
                    GROUP BY s.stream_title
                    ORDER BY avg_viewers DESC
                    LIMIT ?
                """,
                    [since_date, streamer.lower(), limit],
                ).fetchall()

                def extract_keywords(title: str) -> list[str]:
                    """Extract meaningful keywords from title."""
                    import re

                    # Remove common words and punctuation
                    stop_words = {
                        "der",
                        "die",
                        "das",
                        "und",
                        "oder",
                        "mit",
                        "für",
                        "the",
                        "and",
                        "or",
                        "with",
                        "for",
                        "to",
                        "a",
                        "an",
                    }
                    words = re.findall(r"\b\w{3,}\b", title.lower())
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
            watch_time_req = type(
                "Request", (), {"query": {"streamer": streamer, "days": str(days)}}
            )()
            watch_time_req.headers = request.headers
            funnel_req = type("Request", (), {"query": {"streamer": streamer, "days": str(days)}})()
            funnel_req.headers = request.headers
            tags_req = type(
                "Request",
                (),
                {"query": {"streamer": streamer, "days": str(days), "limit": "10"}},
            )()
            tags_req.headers = request.headers
            titles_req = type(
                "Request",
                (),
                {"query": {"streamer": streamer, "days": str(days), "limit": "10"}},
            )()
            titles_req.headers = request.headers

            # Call internal methods directly
            with storage.get_conn() as conn:
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()
                prev_since = (datetime.now(UTC) - timedelta(days=days * 2)).isoformat()

                # Current period metrics
                current = conn.execute(
                    """
                    SELECT
                        AVG(s.retention_10m) as retention,
                        SUM(CASE WHEN s.follower_delta IS NOT NULL
                             AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                             THEN s.follower_delta ELSE 0 END) as followers,
                        SUM(s.returning_chatters) as returning,
                        SUM(s.unique_chatters) as unique_chatters
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """,
                    [since_date, streamer.lower()],
                ).fetchone()

                # Previous period for comparison
                prev = conn.execute(
                    """
                    SELECT
                        AVG(s.retention_10m) as retention,
                        SUM(CASE WHEN s.follower_delta IS NOT NULL
                             AND NOT (s.followers_end = 0 AND s.followers_start > 0)
                             THEN s.follower_delta ELSE 0 END) as followers,
                        SUM(s.returning_chatters) as returning,
                        SUM(s.unique_chatters) as unique_chatters
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND s.started_at < ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """,
                    [prev_since, since_date, streamer.lower()],
                ).fetchone()

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

                return web.json_response(
                    {
                        "trends": {
                            "watchTimeChange": calc_trend(curr_retention, prev_retention),
                            "conversionChange": 0,  # Would need follower tracking improvement
                            "viewerReturnRate": round(return_rate, 1),
                            "viewerReturnChange": calc_trend(return_rate, prev_return_rate),
                        }
                    }
                )
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
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()

                # Analyze stream times to estimate audience timezone/region
                time_stats = conn.execute(
                    """
                    SELECT
                        CAST(strftime('%H', s.started_at) AS INTEGER) as hour,
                        AVG(s.avg_viewers) as avg_viewers,
                        COUNT(*) as stream_count
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                    GROUP BY hour
                    ORDER BY avg_viewers DESC
                """,
                    [since_date, streamer.lower()],
                ).fetchall()

                # Peak hours analysis for region estimation
                peak_hours = [r[0] for r in time_stats[:3]] if time_stats else [20, 21, 19]

                # German stream = DACH region primarily (UTC+1/+2)
                # If peak is 18-23 UTC, likely European audience
                europe_score = sum(1 for h in peak_hours if 17 <= h <= 23)
                us_score = sum(1 for h in peak_hours if 0 <= h <= 6 or 23 <= h <= 24)
                asia_score = sum(1 for h in peak_hours if 8 <= h <= 16)

                total = europe_score + us_score + asia_score or 1
                regions = [
                    {
                        "region": "DACH",
                        "percentage": round(europe_score / total * 70, 1),
                    },  # Primary
                    {
                        "region": "Rest EU",
                        "percentage": round(europe_score / total * 15, 1),
                    },
                    {"region": "NA", "percentage": round(us_score / total * 10, 1)},
                    {"region": "Other", "percentage": round(asia_score / total * 5, 1)},
                ]

                # Chat activity analysis for engagement type
                chat_stats = conn.execute(
                    """
                    SELECT
                        AVG(s.unique_chatters) as avg_chatters,
                        AVG(s.avg_viewers) as avg_viewers,
                        AVG(s.returning_chatters * 1.0 / NULLIF(s.unique_chatters, 0)) as return_rate
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                """,
                    [since_date, streamer.lower()],
                ).fetchone()

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
                schedule_stats = conn.execute(
                    """
                    SELECT
                        CAST(strftime('%w', s.started_at) AS INTEGER) as weekday,
                        COUNT(*) as count
                    FROM twitch_stream_sessions s
                    WHERE s.started_at >= ? AND LOWER(s.streamer_login) = ? AND s.ended_at IS NOT NULL
                    GROUP BY weekday
                """,
                    [since_date, streamer.lower()],
                ).fetchall()

                weekday_counts = {r[0]: r[1] for r in schedule_stats}
                weekend_streams = weekday_counts.get(0, 0) + weekday_counts.get(6, 0)
                weekday_streams = sum(weekday_counts.get(i, 0) for i in range(1, 6))

                if weekend_streams > weekday_streams:
                    activity_pattern = "weekend-heavy"
                elif weekday_streams > weekend_streams * 2:
                    activity_pattern = "weekday-focused"
                else:
                    activity_pattern = "balanced"

                total_sessions = int(sum(weekday_counts.values()))
                if total_sessions >= 20:
                    confidence = "medium"
                elif total_sessions >= 8:
                    confidence = "low"
                else:
                    confidence = "very_low"

                return web.json_response(
                    {
                        "estimatedRegions": regions,
                        "viewerTypes": viewer_type,
                        "activityPattern": activity_pattern,
                        "primaryLanguage": "German",
                        "languageConfidence": 85,  # Based on being tracked as German streamer
                        "peakActivityHours": peak_hours,
                        "interactiveRate": round(chat_rate * 100, 1),
                        "loyaltyScore": round(return_rate * 100, 1),
                        "dataQuality": {
                            "confidence": confidence,
                            "sessions": total_sessions,
                            "method": "heuristic_from_stream_times_and_chat_behavior",
                        },
                    }
                )
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
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()

                # Determine bucket size based on range
                if days <= 7:
                    bucket_minutes = 5
                elif days <= 30:
                    bucket_minutes = 30
                else:
                    bucket_minutes = 60

                # Use static SQL variants per bucket size to avoid dynamic query construction.
                if bucket_minutes == 5:
                    timeline_sql = """
                    SELECT
                        strftime('%Y-%m-%d %H:', ts_utc) || PRINTF('%02d', (CAST(strftime('%M', ts_utc) AS INTEGER) / 5) * 5) as bucket,
                        AVG(viewer_count) as avg_vc,
                        MAX(viewer_count) as peak_vc,
                        MIN(viewer_count) as min_vc,
                        COUNT(*) as samples
                    FROM twitch_stats_tracked
                    WHERE ts_utc >= ? AND LOWER(streamer) = ?
                    GROUP BY bucket
                    ORDER BY bucket
                    """
                elif bucket_minutes == 30:
                    timeline_sql = """
                    SELECT
                        strftime('%Y-%m-%d %H:', ts_utc) || CASE WHEN CAST(strftime('%M', ts_utc) AS INTEGER) < 30 THEN '00' ELSE '30' END as bucket,
                        AVG(viewer_count) as avg_vc,
                        MAX(viewer_count) as peak_vc,
                        MIN(viewer_count) as min_vc,
                        COUNT(*) as samples
                    FROM twitch_stats_tracked
                    WHERE ts_utc >= ? AND LOWER(streamer) = ?
                    GROUP BY bucket
                    ORDER BY bucket
                    """
                else:
                    timeline_sql = """
                    SELECT
                        strftime('%Y-%m-%d %H:00', ts_utc) as bucket,
                        AVG(viewer_count) as avg_vc,
                        MAX(viewer_count) as peak_vc,
                        MIN(viewer_count) as min_vc,
                        COUNT(*) as samples
                    FROM twitch_stats_tracked
                    WHERE ts_utc >= ? AND LOWER(streamer) = ?
                    GROUP BY bucket
                    ORDER BY bucket
                    """

                rows = conn.execute(timeline_sql, [since_date, streamer.lower()]).fetchall()

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
        sort_mode = request.query.get("sort", "avg")  # avg or peak

        try:
            with storage.get_conn() as conn:
                since_date = (datetime.now(UTC) - timedelta(days=days)).isoformat()

                if sort_mode == "peak":
                    leaderboard_sql = """
                    SELECT
                        c.streamer,
                        AVG(c.viewer_count) as avg_vc,
                        MAX(c.viewer_count) as peak_vc,
                        MAX(c.is_partner) as is_partner
                    FROM twitch_stats_category c
                    WHERE c.ts_utc >= ?
                    GROUP BY c.streamer
                    ORDER BY peak_vc DESC
                    """
                else:
                    leaderboard_sql = """
                    SELECT
                        c.streamer,
                        AVG(c.viewer_count) as avg_vc,
                        MAX(c.viewer_count) as peak_vc,
                        MAX(c.is_partner) as is_partner
                    FROM twitch_stats_category c
                    WHERE c.ts_utc >= ?
                    GROUP BY c.streamer
                    ORDER BY avg_vc DESC
                    """

                rows = conn.execute(leaderboard_sql, [since_date]).fetchall()

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

                return web.json_response(
                    {
                        "leaderboard": leaderboard,
                        "totalStreamers": total_streamers,
                        "yourRank": your_rank,
                    }
                )
        except Exception as exc:
            log.exception("Error in category leaderboard API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_coaching(self, request: web.Request) -> web.Response:
        """Get personalized coaching data for a streamer."""
        self._require_v2_auth(request)

        streamer = request.query.get("streamer", "").strip()
        days = min(max(int(request.query.get("days", "30")), 7), 365)

        if not streamer:
            return web.json_response({"error": "Streamer required"}, status=400)

        try:
            with storage.get_conn() as conn:
                data = CoachingEngine.get_coaching_data(conn, streamer, days)
                return web.json_response(data)
        except Exception as exc:
            log.exception("Error in coaching API")
            return web.json_response({"error": str(exc)}, status=500)

    async def _api_v2_category_timings(self, request: web.Request) -> web.Response:
        """
        Outlier-resistente Stunden- und Wochentags-Analyse für die gesamte Kategorie.
        Methode: Median der Streamer-Mediane (zweistufig) + P25/P75 Konfidenzband.
        Einzelne Streamer mit extrem hohen Viewerzahlen verzerren so das Ergebnis nicht.
        """
        self._require_v2_auth(request)
        days = min(max(int(request.query.get("days", "30")), 7), 90)
        source = request.query.get("source", "category")  # 'category' | 'tracked'

        from datetime import datetime, timedelta
        from statistics import median, quantiles

        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        try:
            with storage.get_conn() as c:
                if source == "tracked":
                    rows = c.execute(
                        """
                        SELECT streamer,
                               CAST(strftime('%H', ts_utc) AS INTEGER) AS hour,
                               CAST(strftime('%w', ts_utc) AS INTEGER) AS weekday,
                               viewer_count
                          FROM twitch_stats_tracked
                         WHERE ts_utc >= ?
                           AND viewer_count IS NOT NULL
                           AND viewer_count > 0
                        """,
                        (cutoff,),
                    ).fetchall()
                else:
                    rows = c.execute(
                        """
                        SELECT streamer,
                               CAST(strftime('%H', ts_utc) AS INTEGER) AS hour,
                               CAST(strftime('%w', ts_utc) AS INTEGER) AS weekday,
                               viewer_count
                          FROM twitch_stats_category
                         WHERE ts_utc >= ?
                           AND viewer_count IS NOT NULL
                           AND viewer_count > 0
                        """,
                        (cutoff,),
                    ).fetchall()
        except Exception as exc:
            log.exception("category-timings query failed")
            return web.json_response({"error": str(exc)}, status=500)

        # --- Stunde: Streamer → Stunde → Liste Viewerzahlen ---
        # hour_data[hour][streamer] = [viewer_counts...]
        hour_data: dict = collections.defaultdict(lambda: collections.defaultdict(list))
        weekday_data: dict = collections.defaultdict(lambda: collections.defaultdict(list))

        for row in rows:
            streamer = row[0]
            hour = int(row[1])
            wd = int(row[2])
            vc = float(row[3])
            hour_data[hour][streamer].append(vc)
            weekday_data[wd][streamer].append(vc)

        def _robust_stats(slot_data: dict) -> dict:
            """Für einen Slot (Stunde/Wochentag): Median der Streamer-Mediane + P25/P75."""
            if not slot_data:
                return {
                    "median": None,
                    "p25": None,
                    "p75": None,
                    "streamer_count": 0,
                    "sample_count": 0,
                }
            # Schritt 1: pro Streamer einen Median berechnen
            per_streamer = [median(vals) for vals in slot_data.values() if vals]
            per_streamer.sort()
            n = len(per_streamer)
            sample_count = sum(len(v) for v in slot_data.values())
            if n == 0:
                return {
                    "median": None,
                    "p25": None,
                    "p75": None,
                    "streamer_count": 0,
                    "sample_count": 0,
                }
            # Schritt 2: Median der Mediane
            med = median(per_streamer)
            # P25 / P75
            if n >= 4:
                qs = quantiles(per_streamer, n=4)
                p25, p75 = qs[0], qs[2]
            elif n >= 2:
                p25 = per_streamer[0]
                p75 = per_streamer[-1]
            else:
                p25 = p75 = per_streamer[0]
            return {
                "median": round(med, 1),
                "p25": round(p25, 1),
                "p75": round(p75, 1),
                "streamer_count": n,
                "sample_count": sample_count,
            }

        weekday_names = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"]

        hourly = []
        for h in range(24):
            s = _robust_stats(hour_data.get(h, {}))
            s["hour"] = h
            hourly.append(s)

        weekday_order = [1, 2, 3, 4, 5, 6, 0]  # Mo-So
        weekly = []
        for wd in weekday_order:
            s = _robust_stats(weekday_data.get(wd, {}))
            s["weekday"] = wd
            s["label"] = weekday_names[wd]
            weekly.append(s)

        total_streamers = len({row[0] for row in rows})

        return web.json_response(
            {
                "hourly": hourly,
                "weekly": weekly,
                "total_streamers": total_streamers,
                "window_days": days,
                "method": "median_of_medians",
            }
        )

    async def _api_v2_category_activity_series(self, request: web.Request) -> web.Response:
        """
        Legacy stats-style comparison series for category vs tracked.
        Provides hourly and weekday rows with average and peak values.
        """
        self._require_v2_auth(request)
        days = min(max(int(request.query.get("days", "30")), 7), 365)
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        hourly_rows: list[Any] = []
        weekday_rows: list[Any] = []

        try:
            with storage.get_conn() as conn:
                hourly_rows = conn.execute(
                    """
                    WITH source_rows AS (
                        SELECT 'tracked' AS source_key, viewer_count, ts_utc
                          FROM twitch_stats_tracked
                        UNION ALL
                        SELECT 'category' AS source_key, viewer_count, ts_utc
                          FROM twitch_stats_category
                    )
                    SELECT source_key,
                           CAST(strftime('%H', ts_utc) AS INTEGER) AS hour,
                           AVG(viewer_count) AS avg_viewers,
                           MAX(viewer_count) AS max_viewers,
                           COUNT(*) AS samples
                      FROM source_rows
                     WHERE ts_utc >= ?
                     GROUP BY source_key, hour
                     ORDER BY source_key, hour
                    """,
                    (cutoff,),
                ).fetchall()

                weekday_rows = conn.execute(
                    """
                    WITH source_rows AS (
                        SELECT 'tracked' AS source_key, viewer_count, ts_utc
                          FROM twitch_stats_tracked
                        UNION ALL
                        SELECT 'category' AS source_key, viewer_count, ts_utc
                          FROM twitch_stats_category
                    )
                    SELECT source_key,
                           CAST(strftime('%w', ts_utc) AS INTEGER) AS weekday,
                           AVG(viewer_count) AS avg_viewers,
                           MAX(viewer_count) AS max_viewers,
                           COUNT(*) AS samples
                      FROM source_rows
                     WHERE ts_utc >= ?
                     GROUP BY source_key, weekday
                     ORDER BY source_key, weekday
                    """,
                    (cutoff,),
                ).fetchall()
        except Exception as exc:
            log.exception("category-activity-series query failed")
            return web.json_response({"error": str(exc)}, status=500)

        def _float_or_none(value: Any, *, digits: int = 1) -> float | None:
            if value is None:
                return None
            try:
                return round(float(value), digits)
            except (TypeError, ValueError):
                return None

        def _int_or_none(value: Any) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        hourly_map: dict[str, dict[int, dict[str, Any]]] = {
            "category": {},
            "tracked": {},
        }
        weekday_map: dict[str, dict[int, dict[str, Any]]] = {
            "category": {},
            "tracked": {},
        }

        for row in hourly_rows:
            source_key = str(row[0] or "").strip().lower()
            hour = _int_or_none(row[1])
            if source_key not in hourly_map or hour is None:
                continue
            hourly_map[source_key][hour] = {
                "avg": _float_or_none(row[2]),
                "peak": _int_or_none(row[3]),
                "samples": _int_or_none(row[4]) or 0,
            }

        for row in weekday_rows:
            source_key = str(row[0] or "").strip().lower()
            weekday = _int_or_none(row[1])
            if source_key not in weekday_map or weekday is None:
                continue
            weekday_map[source_key][weekday] = {
                "avg": _float_or_none(row[2]),
                "peak": _int_or_none(row[3]),
                "samples": _int_or_none(row[4]) or 0,
            }

        hourly: list[dict[str, Any]] = []
        for hour in range(24):
            category_point = hourly_map["category"].get(hour, {})
            tracked_point = hourly_map["tracked"].get(hour, {})
            hourly.append(
                {
                    "hour": hour,
                    "label": f"{hour:02d}:00",
                    "categoryAvg": category_point.get("avg"),
                    "trackedAvg": tracked_point.get("avg"),
                    "categoryPeak": category_point.get("peak"),
                    "trackedPeak": tracked_point.get("peak"),
                    "categorySamples": int(category_point.get("samples") or 0),
                    "trackedSamples": int(tracked_point.get("samples") or 0),
                }
            )

        weekday_labels = {
            0: "Sonntag",
            1: "Montag",
            2: "Dienstag",
            3: "Mittwoch",
            4: "Donnerstag",
            5: "Freitag",
            6: "Samstag",
        }
        weekday_order = [1, 2, 3, 4, 5, 6, 0]  # Mo-So

        weekly: list[dict[str, Any]] = []
        for weekday in weekday_order:
            category_point = weekday_map["category"].get(weekday, {})
            tracked_point = weekday_map["tracked"].get(weekday, {})
            weekly.append(
                {
                    "weekday": weekday,
                    "label": weekday_labels.get(weekday, str(weekday)),
                    "categoryAvg": category_point.get("avg"),
                    "trackedAvg": tracked_point.get("avg"),
                    "categoryPeak": category_point.get("peak"),
                    "trackedPeak": tracked_point.get("peak"),
                    "categorySamples": int(category_point.get("samples") or 0),
                    "trackedSamples": int(tracked_point.get("samples") or 0),
                }
            )

        return web.json_response(
            {
                "hourly": hourly,
                "weekly": weekly,
                "windowDays": days,
                "source": "legacy_stats_chart",
            }
        )

    async def _api_v2_monetization(self, request: web.Request) -> web.Response:
        """Monetization & Hype Train overview for the last N days."""
        self._require_v2_auth(request)
        streamer = request.query.get("streamer", "").strip().lower()
        days = min(max(int(request.query.get("days", "30")), 7), 90)

        from datetime import datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

        ads: dict = {
            "total": 0,
            "auto": 0,
            "manual": 0,
            "sessions_with_ads": 0,
            "avg_duration_s": 0.0,
            "avg_viewer_drop_pct": None,
            "worst_ads": [],
        }
        hype_train: dict = {
            "total": 0,
            "avg_level": 0.0,
            "max_level": 0,
            "avg_duration_s": 0.0,
        }
        bits: dict = {"total": 0, "cheer_events": 0}
        subs: dict = {"total_events": 0, "gifted": 0}

        try:
            with storage.get_conn() as c:
                # --- Ad Break overview ---
                ad_agg = c.execute(
                    """
                    SELECT COUNT(*) AS total_ads,
                           SUM(CASE WHEN a.is_automatic=1 THEN 1 ELSE 0 END) AS auto_ads,
                           AVG(a.duration_seconds) AS avg_duration,
                           COUNT(DISTINCT a.session_id) AS sessions_with_ads
                      FROM twitch_ad_break_events a
                      LEFT JOIN twitch_stream_sessions s ON s.id = a.session_id
                     WHERE a.started_at >= ?
                       AND (? = '' OR LOWER(s.streamer_login) = ?)
                    """,
                    (cutoff, streamer, streamer),
                ).fetchone()
                if ad_agg:
                    total = int(ad_agg["total_ads"] or 0)
                    auto = int(ad_agg["auto_ads"] or 0)
                    ads["total"] = total
                    ads["auto"] = auto
                    ads["manual"] = total - auto
                    ads["sessions_with_ads"] = int(ad_agg["sessions_with_ads"] or 0)
                    ads["avg_duration_s"] = round(float(ad_agg["avg_duration"] or 0.0), 1)

                # --- Viewer impact ---
                ad_rows = c.execute(
                    """
                    SELECT a.id, a.session_id, a.started_at, a.duration_seconds, a.is_automatic,
                           s.started_at AS session_start
                      FROM twitch_ad_break_events a
                      JOIN twitch_stream_sessions s ON s.id = a.session_id
                     WHERE a.started_at >= ?
                       AND a.session_id IS NOT NULL
                       AND (? = '' OR LOWER(s.streamer_login) = ?)
                     ORDER BY a.started_at DESC
                     LIMIT 200
                    """,
                    (cutoff, streamer, streamer),
                ).fetchall()

                timeline_map: dict = {}
                if ad_rows:
                    session_ids = list({int(r["session_id"]) for r in ad_rows if r["session_id"]})
                    if session_ids:
                        session_ids_json = json.dumps(session_ids)
                        vrows = c.execute(
                            """
                            SELECT session_id, minutes_from_start, viewer_count
                              FROM twitch_session_viewers
                             WHERE session_id IN (
                                SELECT CAST(value AS INTEGER) FROM json_each(?)
                             )
                             ORDER BY session_id, minutes_from_start
                            """,
                            (session_ids_json,),
                        ).fetchall()
                        for vr in vrows:
                            sid = int(vr["session_id"])
                            timeline_map.setdefault(sid, []).append(
                                (
                                    float(vr["minutes_from_start"] or 0),
                                    int(vr["viewer_count"] or 0),
                                )
                            )

                drop_pcts: list = []
                worst_ads: list = []
                for ad in ad_rows:
                    sid = int(ad["session_id"] or 0)
                    dur_s = float(ad["duration_seconds"] or 30)
                    try:
                        ad_dt = datetime.fromisoformat(str(ad["started_at"]).replace("Z", "+00:00"))
                        sess_dt = datetime.fromisoformat(
                            str(ad["session_start"]).replace("Z", "+00:00")
                        )
                        min_into = (ad_dt - sess_dt).total_seconds() / 60.0
                    except Exception:
                        continue
                    tl = timeline_map.get(sid, [])
                    if not tl:
                        continue
                    dur_min = dur_s / 60.0
                    pre = [v for m, v in tl if (min_into - 5) <= m < min_into]
                    post_start = min_into + dur_min
                    post = [v for m, v in tl if post_start <= m < (post_start + 5)]
                    if not pre or not post:
                        continue
                    pre_avg = sum(pre) / len(pre)
                    if pre_avg <= 0:
                        continue
                    drop = (sum(post) / len(post) - pre_avg) / pre_avg * 100.0
                    drop_pcts.append(drop)
                    worst_ads.append(
                        {
                            "started_at": str(ad["started_at"] or "")[:16],
                            "duration_s": int(dur_s),
                            "drop_pct": round(drop, 1),
                            "is_automatic": bool(ad["is_automatic"]),
                        }
                    )

                if drop_pcts:
                    ads["avg_viewer_drop_pct"] = round(sum(drop_pcts) / len(drop_pcts), 1)
                worst_ads.sort(key=lambda x: x["drop_pct"])
                ads["worst_ads"] = worst_ads[:5]

                # --- Hype Train ---
                try:
                    ht = c.execute(
                        """
                        SELECT COUNT(*) AS total, AVG(h.level) AS avg_level,
                               MAX(h.level) AS max_level, AVG(h.duration_seconds) AS avg_dur
                          FROM twitch_hype_train_events h
                          LEFT JOIN twitch_stream_sessions s ON s.id = h.session_id
                         WHERE h.started_at >= ?
                           AND h.ended_at IS NOT NULL
                           AND (? = '' OR LOWER(s.streamer_login) = ?)
                        """,
                        (cutoff, streamer, streamer),
                    ).fetchone()
                    if ht:
                        hype_train = {
                            "total": int(ht["total"] or 0),
                            "avg_level": round(float(ht["avg_level"] or 0), 1),
                            "max_level": int(ht["max_level"] or 0),
                            "avg_duration_s": round(float(ht["avg_dur"] or 0), 0),
                        }
                except Exception:
                    log.debug("Hype train query failed", exc_info=True)

                # --- Bits ---
                try:
                    br = c.execute(
                        """
                        SELECT SUM(amount) AS total, COUNT(*) AS events
                          FROM twitch_bits_events
                         WHERE received_at >= ?
                           AND (? = '' OR LOWER(streamer_login) = ?)
                        """,
                        (cutoff, streamer, streamer),
                    ).fetchone()
                    if br:
                        bits = {
                            "total": int(br["total"] or 0),
                            "cheer_events": int(br["events"] or 0),
                        }
                except Exception:
                    log.debug("Bits query failed", exc_info=True)

                # --- Subs ---
                try:
                    sr = c.execute(
                        """
                        SELECT COUNT(*) AS total,
                               SUM(CASE WHEN is_gift=1 THEN 1 ELSE 0 END) AS gifted
                          FROM twitch_subscription_events
                         WHERE received_at >= ?
                           AND (? = '' OR LOWER(streamer_login) = ?)
                        """,
                        (cutoff, streamer, streamer),
                    ).fetchone()
                    if sr:
                        subs = {
                            "total_events": int(sr["total"] or 0),
                            "gifted": int(sr["gifted"] or 0),
                        }
                except Exception:
                    log.debug("Subs query failed", exc_info=True)

        except Exception as exc:
            log.exception("Error in monetization API")
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response(
            {
                "ads": ads,
                "hype_train": hype_train,
                "bits": bits,
                "subs": subs,
                "window_days": days,
            }
        )


__all__ = ["AnalyticsV2Mixin"]
