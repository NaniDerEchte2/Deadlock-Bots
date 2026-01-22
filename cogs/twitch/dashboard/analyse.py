"""Deep Analysis Dashboard for Twitch."""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

from .. import storage
from .base import DashboardBase, log


class DashboardAnalyseMixin:
    """
    Advanced Analysis Dashboard.
    Focuses on 'Sellable Insights': Actionable advice, growth comparison, and content optimization.
    """

    async def analyse(self, request: web.Request) -> web.Response:
        self._require_token(request)
        
        # 1. Parse Parameters
        streamer_query = request.query.get("streamer", "").strip()
        compare_query = request.query.get("compare", "").strip() # Optional: Other streamer to compare
        days_raw = request.query.get("days", "30")
        
        try:
            days = max(7, min(90, int(days_raw)))
        except ValueError:
            days = 30

        # 2. Resolve Streamer
        normalized_streamer = self._normalize_login(streamer_query)
        
        # 3. Data Gathering
        if not normalized_streamer:
            return self._render_analyse_search(request, "Bitte einen Streamer auswählen.")

        data = await self._compute_deep_analysis(normalized_streamer, days, compare_query)
        
        return self._render_analyse_view(request, normalized_streamer, data, days)

    async def _compute_deep_analysis(self, login: str, days: int, compare_login: str = "") -> Dict[str, Any]:
        """
        Aggregates deep stats: 
        - User Performance vs Category Average
        - Title/Notification NLP (Simple word correlation)
        - Retention benchmarking
        """
        login = login.lower()
        compare_login = compare_login.lower() if compare_login else ""
        
        with storage.get_conn() as conn:
            # A. User Sessions
            user_sessions = conn.execute(
                """
                SELECT * FROM twitch_stream_sessions 
                WHERE streamer_login = ? 
                  AND started_at >= datetime('now', ?)
                  AND ended_at IS NOT NULL
                  AND duration_seconds > 600 -- Ignore short test streams
                ORDER BY started_at DESC
                """,
                (login, f"-{days} days")
            ).fetchall()

            # B. Category Average (Benchmarks)
            # We aggregate ALL tracked sessions in the same timeframe
            category_stats = conn.execute(
                """
                SELECT 
                    AVG(avg_viewers) as cat_avg,
                    AVG(peak_viewers) as cat_peak,
                    AVG(retention_10m) as cat_ret10,
                    AVG(dropoff_pct) as cat_drop
                FROM twitch_stream_sessions
                WHERE started_at >= datetime('now', ?)
                  AND duration_seconds > 600
                """,
                (f"-{days} days",)
            ).fetchone()

            # C. Comparison Target (if any)
            comp_sessions = []
            if compare_login:
                comp_sessions = conn.execute(
                    """
                    SELECT * FROM twitch_stream_sessions 
                    WHERE streamer_login = ? 
                      AND started_at >= datetime('now', ?)
                      AND ended_at IS NOT NULL
                      AND duration_seconds > 600
                    """,
                    (compare_login, f"-{days} days")
                ).fetchall()

            # D. Subs
            sub_row = conn.execute(
                "SELECT total, points FROM twitch_subscriptions_snapshot WHERE twitch_login = ? ORDER BY snapshot_at DESC LIMIT 1",
                (login,)
            ).fetchone()
            
            # E. Audience Overlap (Shared)
            shared_rows = conn.execute(
                """
                SELECT other.streamer_login, COUNT(DISTINCT t1.chatter_login) as overlap
                FROM twitch_chatter_rollup t1
                JOIN twitch_chatter_rollup other ON t1.chatter_login = other.chatter_login
                WHERE t1.streamer_login = ? 
                  AND other.streamer_login != ?
                  AND t1.last_seen_at >= datetime('now', '-30 days')
                GROUP BY other.streamer_login
                ORDER BY overlap DESC
                LIMIT 5
                """,
                (login, login)
            ).fetchall()

        # --- Processing ---
        
        # 1. Basic Metrics
        if not user_sessions:
            return {"error": "Keine Daten für diesen Zeitraum verfügbar."}

        def _get_col(rows, col_idx, col_name):
            # Helper to handle sqlite3.Row or tuple
            res = []
            for r in rows:
                if hasattr(r, "keys"):
                    res.append(r[col_name])
                else:
                    res.append(r[col_idx])
            return res

        # Column indices in twitch_stream_sessions (check storage.py for order if using tuple)
        # avg_viewers=9, peak_viewers=7, retention_10m=12, stream_title=last?, notification_text=last?
        # Safer to use dictionary access if Row factory is set, but we handle both.
        
        avg_viewers_list = [float(x or 0) for x in _get_col(user_sessions, 9, "avg_viewers")]
        peak_viewers_list = [int(x or 0) for x in _get_col(user_sessions, 7, "peak_viewers")]
        ret10_list = [float(x or 0) for x in _get_col(user_sessions, 12, "retention_10m")]
        
        my_avg = statistics.mean(avg_viewers_list) if avg_viewers_list else 0
        my_peak = statistics.mean(peak_viewers_list) if peak_viewers_list else 0
        my_ret10 = statistics.mean(ret10_list) if ret10_list else 0
        
        cat_avg = float(category_stats[0] or 0) if category_stats else 0
        cat_ret10 = float(category_stats[2] or 0) if category_stats else 0

        # 2. Content Analysis (Title & Tags Impact)
        # Identify "High Performing" sessions (Peak > Median Peak)
        median_peak = statistics.median(peak_viewers_list) if peak_viewers_list else 0
        
        high_perf_titles = []
        high_perf_tags = []
        
        for sess in user_sessions:
            peak = int(sess["peak_viewers"] if hasattr(sess, "keys") else sess[7] or 0)
            title = str(sess["stream_title"] if hasattr(sess, "keys") else sess[20] or "").lower() # col 20 is stream_title
            tags_str = str(sess["tags"] if hasattr(sess, "keys") else sess[23] or "").lower() # col 23 is tags
            
            if peak >= median_peak * 1.1: # 10% better than median
                if title: high_perf_titles.append(title)
                if tags_str: high_perf_tags.extend(tags_str.split(","))

        # Simple NLP: Tokenize
        def _get_keywords(text_list):
            words = []
            for t in text_list:
                # Remove common stop words (German/English mixed)
                cleaned = ''.join(c for c in t if c.isalnum() or c.isspace())
                for w in cleaned.split():
                    if len(w) > 3 and w not in ["deadlock", "mit", "und", "oder", "with", "stream", "live", "playing", "zocken", "german", "deutsch"]:
                        words.append(w)
            return Counter(words).most_common(5)

        good_keywords = _get_keywords(high_perf_titles)
        good_tags = Counter(high_perf_tags).most_common(3)
        
        # 3. Actionable Insights Generator
        insights = []
        
        # A. Retention Insight
        if my_ret10 < cat_ret10 - 0.05: # 5% worse than category
            insights.append({
                "type": "warning",
                "title": "Kritischer Drop-Off am Anfang",
                "text": f"Deine Retention nach 10 Minuten ({my_ret10:.1%}) liegt deutlich unter dem Kategorie-Schnitt ({cat_ret10:.1%}).",
                "advice": "Überprüfe dein Intro. Startest du sofort mit Content oder 'wartest' du zu lange? Technische Probleme beim Start?"
            })
        elif my_ret10 > cat_ret10 + 0.05:
            insights.append({
                "type": "success",
                "title": "Starker Hook!",
                "text": "Du hältst Zuschauer am Anfang besser als der Durchschnitt. Deine Intros funktionieren.",
                "advice": "Nutze die hohen Anfangszahlen für wichtige Calls-to-Action (Discord, Follows)."
            })

        # B. Engagement/Conversion Insight
        # Estimate: Active Chatters / Avg Viewers
        # We need sum of unique chatters per session
        unique_chatters_list = [int(x or 0) for x in _get_col(user_sessions, 16, "unique_chatters")] # 16 is unique_chatters
        avg_chat_ratio = 0
        if my_avg > 0:
            avg_chatters = statistics.mean(unique_chatters_list) if unique_chatters_list else 0
            avg_chat_ratio = avg_chatters / my_avg
        
        if avg_chat_ratio < 0.15: # < 15% chat interaction
            insights.append({
                "type": "info",
                "title": "Ruhiger Chat",
                "text": f"Nur ca. {avg_chat_ratio:.1%} deiner Zuschauer schreiben im Chat.",
                "advice": "Stelle mehr offene Fragen an den Chat. Nutze Predictions oder Channel Points, um Interaktion zu erzwingen."
            })
        
        # C. Monetization Gap (if Subs data available)
        if sub_row:
            subs = sub_row[0] or 0
            if my_avg > 10:
                sub_ratio = subs / my_avg
                if sub_ratio < 0.5: # Less than 0.5 subs per average viewer (rough metric)
                    insights.append({
                        "type": "money",
                        "title": "Monetarisierungspotenzial",
                        "text": "Du hast viele Zuschauer im Verhältnis zu deinen Subs.",
                        "advice": "Erinnerst du an Prime-Subs? Bietest du genug Emote-Value? Deine Community scheint da zu sein, aber zahlt noch nicht."
                    })

        # D. Content Insight (Keywords & Tags)
        content_text_parts = []
        if good_keywords:
            words = ", ".join([w[0] for w in good_keywords])
            content_text_parts.append(f"Titel-Keywords: <strong>{words}</strong>")
        if good_tags:
            tags_display = ", ".join([t[0] for t in good_tags])
            content_text_parts.append(f"Top Tags: <strong>{tags_display}</strong>")
            
        if content_text_parts:
            insights.append({
                "type": "success",
                "title": "Was funktioniert (Content)",
                "text": " &bull; ".join(content_text_parts),
                "advice": "Kombiniere diese Tags mit den Titel-Keywords für maximale Discoverability."
            })

        # E. Tag/Meta hygiene (Check if tags are missing)
        any_tags_missing = any(not str(s["tags"] if hasattr(s, "keys") else s[23] or "") for s in user_sessions)
        if any_tags_missing:
             insights.append({
                "type": "warning",
                "title": "Fehlende Tags",
                "text": "Einige deiner Streams hatten keine Tags gesetzt.",
                "advice": "Tags sind essentiell für die 'Empfohlen'-Seite. Nutze immer alle 5 Tag-Slots (z.B. Sprache, Spielstil)."
            })

        return {
            "my_stats": {
                "avg": my_avg,
                "peak": my_peak,
                "ret10": my_ret10,
                "sessions": len(user_sessions),
                "chat_ratio": avg_chat_ratio,
                "subs": sub_row[0] if sub_row else 0
            },
            "cat_stats": {
                "avg": cat_avg,
                "ret10": cat_ret10,
            },
            "shared_audience": [{"name": r[0], "count": r[1]} for r in shared_rows],
            "insights": insights,
            # Graph Data (Last 10 sessions)
            "history": {
                "labels": [s["started_at"][5:10] for s in user_sessions[:10]][::-1], # MM-DD
                "viewers": [s["avg_viewers"] for s in user_sessions[:10]][::-1],
                "retention": [float(s["retention_10m"] or 0)*100 for s in user_sessions[:10]][::-1]
            }
        }

    def _render_analyse_search(self, request: web.Request, error: str = "") -> web.Response:
        html_content = f"""
        <div class="card" style="max-width: 600px; margin: 2rem auto;">
            <div class="card-header"><h2>Channel Analyse</h2></div>
            <div style="padding: 1rem;">
                {f'<div class="user-warning" style="margin-bottom:1rem;">{error}</div>' if error else ''}
                <form method="get" action="/twitch/analyse">
                    <label class="filter-label">Streamer Name (Twitch Login)
                    <input type="text" name="streamer" placeholder="z.B. nani" required style="width:100%; padding:0.8rem; font-size:1.1rem; margin-top:0.5rem;">
                    </label>
                    <div style="margin-top:1rem;">
                        <button class="btn" style="width:100%;">Analysieren</button>
                    </div>
                </form>
            </div>
        </div>
        """
        return web.Response(text=self._html(html_content, active="analyse"), content_type="text/html")

    def _render_analyse_view(self, request: web.Request, streamer: str, data: Dict, days: int) -> web.Response:
        if "error" in data:
            return self._render_analyse_search(request, data["error"])

        stats = data["my_stats"]
        cat = data["cat_stats"]
        insights = data["insights"]
        
        # --- HTML Components ---

        # 1. Header & Score
        # Calculate a pseudo "Growth Score"
        score_retention = min(100, (stats['ret10'] / (cat['ret10'] or 1)) * 50)
        score_activity = min(100, stats['chat_ratio'] * 200) # 50% chat ratio = 100 pts
        total_score = int((score_retention + score_activity) / 2)
        score_color = "#4ade80" if total_score > 70 else "#facc15" if total_score > 40 else "#ef4444"

        # 2. Comparison Cards
        def _delta_card(label, my_val, cat_val, format_pct=False, suffix=""):
            diff = my_val - cat_val
            pct_diff = (diff / cat_val * 100) if cat_val > 0 else 0
            color = "#4ade80" if diff >= 0 else "#ef4444"
            arrow = "▲" if diff >= 0 else "▼"
            
            val_str = f"{my_val:.1%}" if format_pct else f"{my_val:.1f}{suffix}"
            cat_str = f"{cat_val:.1%}" if format_pct else f"{cat_val:.1f}{suffix}"
            
            return f"""
            <div class="stat-card">
                <div class="stat-label">{label}</div>
                <div class="stat-value">{val_str}</div>
                <div class="stat-meta" style="color:{color}">
                    {arrow} {abs(pct_diff):.0f}% vs. Kategorie ({cat_str})
                </div>
            </div>
            """

        cards_html = _delta_card("Ø Viewer", stats['avg'], cat['avg'])
        cards_html += _delta_card("Retention (10m)", stats['ret10'], cat['ret10'], format_pct=True)
        
        # 3. Insights List
        insights_html = ""
        for item in insights:
            icon = "💡"
            color = "var(--border)"
            if item['type'] == "warning": icon = "⚠️"; color = "#ef4444"
            if item['type'] == "success": icon = "✅"; color = "#4ade80"
            if item['type'] == "money": icon = "💰"; color = "#facc15"
            
            insights_html += f"""
            <div style="border-left: 4px solid {color}; background: rgba(255,255,255,0.03); padding: 1rem; margin-bottom: 1rem; border-radius: 4px;">
                <div style="font-weight:bold; font-size:1.1rem; margin-bottom:0.4rem;">{icon} {item['title']}</div>
                <div style="margin-bottom:0.6rem;">{item['text']}</div>
                <div style="font-style:italic; color: var(--muted); font-size:0.95rem;">👉 Tipp: {item['advice']}</div>
            </div>
            """
        
        if not insights_html:
            insights_html = "<div style='padding:1rem; text-align:center; color:var(--muted);'>Keine spezifischen Auffälligkeiten gefunden. Keep grinding!</div>"

        # 4. Shared Audience
        shared_list = "".join([f"<li><strong>{x['name']}</strong> ({x['count']} Chatter)</li>" for x in data['shared_audience']])
        
        # 5. Charts Script
        chart_data = json.dumps(data["history"])
        
        body = f"""
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:2rem;">
            <div>
                <h1 style="margin:0;">Channel Analyse: <span style="color:#9146FF">{streamer}</span></h1>
                <div class="status-meta">Zeitraum: Letzte {days} Tage</div>
            </div>
            <div style="text-align:right;">
                <div style="font-size:0.9rem; color:var(--muted);">Health Score</div>
                <div style="font-size:2.5rem; font-weight:bold; color:{score_color}">{total_score}/100</div>
            </div>
        </div>

        <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap:1.5rem; margin-bottom:2rem;">
            <!-- Performance -->
            <div class="card">
                <div class="card-header"><h2>Performance vs. Deadlock-Schnitt</h2></div>
                <div style="display:grid; grid-template-columns: 1fr 1fr; gap:1rem; padding:1rem;">
                    {cards_html}
                </div>
            </div>

            <!-- Audience -->
            <div class="card">
                <div class="card-header"><h2>Audience & Monetarisierung</h2></div>
                <div style="padding:1rem;">
                    <div class="user-summary-item">
                        <span class="label">Aktive Subs</span>
                        <span class="value">{stats['subs']}</span>
                    </div>
                    <div class="user-summary-item">
                        <span class="label">Chat-Aktivität</span>
                        <span class="value">{stats['chat_ratio']:.1%}</span>
                    </div>
                    <div style="margin-top:1rem; border-top:1px solid var(--border); padding-top:1rem;">
                        <strong>Gemeinsame Zuschauer mit:</strong>
                        <ul style="margin:0.5rem 0 0 1.5rem; padding:0; color:var(--muted);">{shared_list}</ul>
                    </div>
                </div>
            </div>
        </div>

        <div class="card" style="margin-bottom:2rem;">
            <div class="card-header"><h2>💡 Actionable Insights & Tipps</h2></div>
            <div style="padding:1rem;">
                {insights_html}
            </div>
        </div>

        <div class="card">
            <div class="card-header"><h2>Verlauf (Letzte 10 Streams)</h2></div>
            <div class="chart-panel" style="height:300px;">
                <canvas id="historyChart"></canvas>
            </div>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <script>
            const histData = {chart_data};
            new Chart(document.getElementById('historyChart'), {{
                type: 'line',
                data: {{
                    labels: histData.labels,
                    datasets: [
                        {{
                            label: 'Ø Viewer',
                            data: histData.viewers,
                            borderColor: '#9146FF',
                            yAxisID: 'y'
                        }},
                        {{
                            label: 'Retention (10m) %',
                            data: histData.retention,
                            borderColor: '#4ade80',
                            borderDash: [5, 5],
                            yAxisID: 'y1'
                        }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {{ mode: 'index', intersect: false }},
                    scales: {{
                        y: {{ type: 'linear', display: true, position: 'left', title: {{display:true, text:'Viewer'}} }},
                        y1: {{ type: 'linear', display: true, position: 'right', title: {{display:true, text:'Retention %'}}, grid: {{drawOnChartArea: false}} }}
                    }}
                }}
            }});
        </script>
        """
        
        return web.Response(text=self._html(body, active="analyse"), content_type="text/html")
