"""Modern Analytics Dashboard for Twitch Streamers."""

from __future__ import annotations

import html
import json
from typing import List

from aiohttp import web


class DashboardAnalyticsMixin:
    """Advanced analytics dashboard with retention, discovery, and chat health metrics."""

    @staticmethod
    def _parse_bool_flag(value: object) -> bool:
        if value is None:
            return False
        text = str(value).strip().lower()
        return text in {"1", "true", "yes", "ja", "on", "y", "all"}

    async def analytics_dashboard(self, request: web.Request) -> web.Response:
        """Main analytics dashboard view."""
        self._require_partner_token(request)
        
        # Get query parameters
        streamer_login = request.query.get("streamer", "").strip()
        days = int(request.query.get("days", "30"))
        days = max(7, min(90, days))  # Clamp between 7 and 90
        include_non_partners = self._parse_bool_flag(
            request.query.get("include_non_partners") or request.query.get("non_partners")
        )

        partner_options = ""
        extra_options = ""
        if getattr(self, "_analytics_suggestions", None):
            try:
                suggestions = await self._analytics_suggestions(True)
                partners = suggestions.get("partners") or []
                extras = suggestions.get("extras") or []
                partner_options = self._build_streamer_options(partners, streamer_login)
                extra_options = self._build_streamer_options(extras, streamer_login, label_suffix=" (extern)")
            except Exception:
                partner_options = ""
                extra_options = ""
        elif getattr(self, "_list", None):
            try:
                streamers = await self._list()
                partner_options = self._build_streamer_options(streamers, streamer_login)
            except Exception:
                partner_options = ""
        streamer_options = partner_options
        
        # Build the HTML dashboard
        partner_token = ""
        try:
            partner_token = request.query.get("partner_token", "").strip()
        except Exception:
            partner_token = ""
        body = self._build_analytics_html(
            streamer_login,
            days,
            streamer_options,
            partner_token,
            extra_streamer_options=extra_options,
            include_non_partners=include_non_partners,
        )
        return web.Response(
            text=self._html(body, active="analytics"),
            content_type="text/html"
        )

    async def analytics_data_api(self, request: web.Request) -> web.Response:
        """JSON API endpoint for analytics data."""
        self._require_partner_token(request)
        
        streamer_login = request.query.get("streamer", "").strip()
        days = int(request.query.get("days", "30"))
        days = max(7, min(90, days))
        
        try:
            if not self._streamer_analytics_data:
                return web.json_response({"error": "Analytics callback not available"}, status=500)
            
            data = await self._streamer_analytics_data(streamer_login, days)
            return web.json_response(data)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)



    def _build_analytics_html(
        self,
        streamer_login: str,
        days: int,
        streamer_options: str,
        partner_token: str = "",
        extra_streamer_options: str = "",
        include_non_partners: bool = False,
    ) -> str:
        """Build the analytics dashboard HTML."""
        partner_token = partner_token.strip() if partner_token else ""

        config = json.dumps(
            {
                "streamer": streamer_login,
                "days": days,
                "streamerOptions": streamer_options,
                "partnerToken": partner_token,
                "extraStreamerOptions": extra_streamer_options,
                "includeNonPartners": bool(include_non_partners),
                "hasExtraOptions": bool(extra_streamer_options.strip()) if extra_streamer_options else False,
            }
        )

        return f"""
<!DOCTYPE html>
<html lang="de" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Analytics Dashboard - {streamer_login or 'Dein Kanal'}</title>
    
    <!-- Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    
    <!-- Tailwind CSS -->
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {{
            darkMode: 'class',
            theme: {{
                extend: {{
                    fontFamily: {{
                        sans: ['Outfit', 'sans-serif'],
                        display: ['Outfit', 'sans-serif'],
                    }},
                    colors: {{
                        bg: '#0b0e14',
                        card: '#151a25',
                        accent: {{ DEFAULT: '#7c3aed', hover: '#6d28d9' }}
                    }}
                }}
            }}
        }}
    </script>
    
    <!-- Chart.js -->
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    
    <!-- React & Babel -->
    <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
    <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
    <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
    
    <style>
        body {{ background-color: #0b0e14; color: #e2e8f0; }}
        .bg-card {{ background-color: #151a25; }}
    </style>
</head>
<body class="antialiased min-h-screen p-4 md:p-8">
    <div id="analytics-root"></div>
    
    <!-- Config Injection -->
    <script id="analytics-config" type="application/json">
        {config}
    </script>
    
    <!-- Load Components -->
    <script type="text/babel" src="/twitch/static/js/components/KpiCard.js"></script>
    <script type="text/babel" src="/twitch/static/js/components/ScoreGauge.js"></script>
    <script type="text/babel" src="/twitch/static/js/components/ChartContainer.js"></script>
    <script type="text/babel" src="/twitch/static/js/components/InsightsPanel.js"></script>
    <script type="text/babel" src="/twitch/static/js/components/SessionTable.js"></script>
    <script type="text/babel" src="/twitch/static/js/components/ViewModeTabs.js"></script>
    <script type="text/babel" src="/twitch/static/js/components/ComparisonView.js"></script>
    
    <!-- Main App -->
    <script type="text/babel" src="/twitch/static/js/analytics-new.js"></script>
</body>
</html>
"""


    def _build_streamer_options(self, streamers: List[object], selected: str, label_suffix: str = "") -> str:
        """Build HTML options for streamer dropdown based on stored list."""
        if not streamers:
            return ""
        selected_lower = (selected or "").lower()
        seen = set()
        options = []
        for row in streamers:
            if isinstance(row, str):
                login = row.strip()
                label = login
            else:
                login = (row.get("twitch_login") or row.get("streamer") or row.get("login") or "").strip()  # type: ignore[union-attr]
                label = (row.get("label") or login) if isinstance(row, dict) else login  # type: ignore[union-attr]
            if not login or login.lower() in seen:
                continue
            seen.add(login.lower())
            sel = " selected" if login.lower() == selected_lower else ""
            display = f"{label}{label_suffix}" if label_suffix else label
            options.append(f"<option value='{html.escape(login, quote=True)}'{sel}>{html.escape(display)}</option>")
        return "\n".join(options)

    async def streamer_detail(self, request: web.Request) -> web.Response:
        """Detailed analytics for a specific streamer."""
        self._require_partner_token(request)
        
        login = request.match_info.get("login", "").strip()
        if not login or not self._streamer_overview:
            return web.Response(text="Not found", status=404)
        
        try:
            data = await self._streamer_overview(login)
            body = self._streamer_detail_view(data, "analytics")
            return web.Response(
                text=self._html(body, active="analytics"),
                content_type="text/html"
            )
        except Exception as exc:
            return web.Response(text=f"Error: {exc}", status=500)

    async def session_detail(self, request: web.Request) -> web.Response:
        """Detailed analytics for a specific stream session."""
        self._require_partner_token(request)
        
        session_id_str = request.match_info.get("id", "").strip()
        try:
            session_id = int(session_id_str)
        except ValueError:
            return web.Response(text="Invalid session ID", status=400)
        
        if not self._session_detail:
            return web.Response(text="Session detail not available", status=501)
        
        try:
            data = await self._session_detail(session_id)
            body = self._session_detail_view(data, "analytics")
            return web.Response(
                text=self._html(body, active="analytics"),
                content_type="text/html"
            )
        except Exception as exc:
            return web.Response(text=f"Error: {exc}", status=500)

    async def compare_stats_page(self, request: web.Request) -> web.Response:
        """Comparison view for benchmarking against category."""
        self._require_partner_token(request)
        
        if not self._comparison_stats:
            return web.Response(text="Comparison not available", status=501)
        
        try:
            data = await self._comparison_stats()
            body = self._comparison_view(data, "compare")
            return web.Response(
                text=self._html(body, active="compare"),
                content_type="text/html"
            )
        except Exception as exc:
            return web.Response(text=f"Error: {exc}", status=500)


__all__ = ["DashboardAnalyticsMixin"]
