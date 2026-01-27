"""HTML helpers for the Twitch dashboard."""

from __future__ import annotations

import html
from typing import Optional


class DashboardTemplateMixin:
    def _tabs(self, active: str) -> str:
        def anchor(href: str, label: str, key: str) -> str:
            cls = "tab active" if key == active else "tab"
            return f'<a class="{cls}" href="{href}">{label}</a>'

        return (
            '<nav class="tabs">'
            f'{anchor("/twitch", "Live", "live")}'
            f'{anchor("/twitch/stats", "Stats", "stats")}'
            f'{anchor("/twitch/analytics", "Analytics", "analytics")}'
            f'{anchor("/twitch/analyse", "Analyse", "analyse")}'
            f'{anchor("/twitch/compare", "Compare", "compare")}'
            f'<a class="tab tab-admin" href="{self._master_dashboard_href}">Admin</a>'
            "</nav>"
        )

    def _html(
        self,
        body: str,
        active: str,
        msg: str = "",
        err: str = "",
        nav: Optional[str] = None,
    ) -> str:
        flash = ""
        if msg:
            flash = f'<div class="flash ok">{html.escape(msg)}</div>'
        elif err:
            flash = f'<div class="flash err">{html.escape(err)}</div>'
        nav_html = self._tabs(active) if nav is None else nav
        return f"""
<!doctype html>
<meta charset="utf-8">
<title>Deadlock Twitch Posting – Admin</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root {{
    --bg:#0f0f23; --card:#151a28; --bd:#2a3044; --text:#eeeeee; --muted:#9aa4b2;
    --accent:#6d4aff; --accent-2:#9bb0ff; --ok-bg:#15391f; --ok-bd:#1d6b33; --ok-fg:#b6f0c8;
    --err-bg:#3a1a1a; --err-bd:#792e2e; --err-fg:#ffd2d2;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, Arial, sans-serif; max-width: 1250px; margin: 2rem auto; color:var(--text); background:var(--bg); }}
  .tabs {{ display:flex; gap:.5rem; margin-bottom:1rem; }}
  .tab {{ padding:.5rem .8rem; border-radius:.5rem; text-decoration:none; color:#ddd; background:#1a1f2e; border:1px solid var(--bd); }}
  .tab.active {{ background:var(--accent); color:#fff; }}
  .tab.tab-admin {{ margin-left:auto; background:#2d255b; color:#fff; border-color:var(--accent); font-weight:600; }}
  .tab.tab-admin:hover {{ background:var(--accent); color:#fff; }}
  .card {{ background:var(--card); border:1px solid var(--bd); border-radius:.7rem; padding:1rem; }}
  .row {{ display:flex; gap:1rem; align-items:center; flex-wrap:wrap; }}
  .btn {{ background:var(--accent); color:white; border:none; padding:.5rem .8rem; border-radius:.5rem; cursor:pointer; }}
  .btn:hover {{ opacity:.95; }}
  .btn-small {{ padding:.35rem .6rem; font-size:.85rem; }}
  .btn-secondary {{ background:#2a3044; color:var(--text); border:1px solid var(--bd); }}
  .btn-danger {{ background:#792e2e; }}
  .btn-warn {{ background:#b8741a; color:#fff; }}
  table {{ width:100%; border-collapse: collapse; margin-top:1rem; }}
  th, td {{ border-bottom:1px solid var(--bd); padding:.6rem; text-align:left; }}
  th {{ color:var(--accent-2); }}
  input[type="text"] {{ background:#0f1422; border:1px solid var(--bd); color:var(--text); padding:.45rem .6rem; border-radius:.4rem; width:28rem; }}
  input[type="number"], select {{ background:#0f1422; border:1px solid var(--bd); color:var(--text); padding:.45rem .6rem; border-radius:.4rem; }}
  small {{ color:var(--muted); }}
  .flash {{ margin:.7rem 0; padding:.5rem .7rem; border-radius:.4rem; }}
  .flash.ok {{ background:var(--ok-bg); border:1px solid var(--ok-bd); color:var(--ok-fg); }}
  .flash.err {{ background:var(--err-bg); border:1px solid var(--err-bd); color:var(--err-fg); }}
  form.inline {{ display:inline; }}
  .card-header {{ display:flex; justify-content:space-between; align-items:center; gap:.8rem; flex-wrap:wrap; }}
  .badge {{ display:inline-block; padding:.2rem .6rem; border-radius:999px; font-size:.8rem; font-weight:600; }}
  .badge-ok {{ background:var(--ok-bd); color:var(--ok-fg); }}
  .badge-warn {{ background:var(--err-bd); color:var(--err-fg); }}
  .badge-neutral {{ background:#2a3044; color:#ddd; }}
  .status-meta {{ font-size:.8rem; color:var(--muted); margin-top:.2rem; }}
  .action-stack {{ display:flex; flex-wrap:wrap; gap:.4rem; align-items:center; }}
  .countdown-ok {{ color:var(--accent-2); font-weight:600; }}
  .countdown-warn {{ color:var(--err-fg); font-weight:600; }}
  table.sortable-table th[data-sort-type] {{ cursor:pointer; user-select:none; position:relative; padding-right:1.4rem; }}
  table.sortable-table th[data-sort-type]::after {{ content:"⇅"; position:absolute; right:.4rem; color:var(--muted); font-size:.75rem; top:50%; transform:translateY(-50%); }}
  table.sortable-table th[data-sort-type][data-sort-dir="asc"]::after {{ content:"↑"; color:var(--accent-2); }}
  table.sortable-table th[data-sort-type][data-sort-dir="desc"]::after {{ content:"↓"; color:var(--accent-2); }}
  .filter-form {{ margin-top:.6rem; }}
  .filter-form .row {{ align-items:flex-end; gap:1rem; }}
  .filter-label {{ display:flex; flex-direction:column; gap:.3rem; font-size:.9rem; color:var(--muted); }}
  .filter-card {{ margin-top:1rem; }}
  .filter-row {{ align-items:flex-end; gap:1rem; flex-wrap:wrap; }}
  .filter-row .filter-label {{ display:flex; flex-direction:column; gap:.3rem; font-size:.85rem; color:var(--muted); }}
  .filter-row select {{ background:#0f1422; border:1px solid var(--bd); color:var(--text); padding:.4rem .6rem; border-radius:.4rem; min-width:12rem; }}
  .add-streamer-card {{ margin-top:1rem; }}
  .add-streamer-card h2 {{ margin:0 0 .6rem 0; font-size:1.1rem; color:var(--accent-2); }}
  .add-streamer-card form {{ display:flex; flex-direction:column; gap:.8rem; }}
  .add-streamer-card .form-grid {{ display:flex; flex-wrap:wrap; gap:1rem; align-items:flex-end; }}
  .add-streamer-card label {{ display:flex; flex-direction:column; gap:.3rem; font-size:.85rem; color:var(--muted); }}
  .add-streamer-card input[type="text"] {{ min-width:14rem; }}
  .add-streamer-card .form-actions {{ display:flex; gap:.6rem; align-items:center; flex-wrap:wrap; }}
  .add-streamer-card .hint {{ margin-top:.2rem; font-size:.8rem; color:var(--muted); max-width:38rem; }}
  .non-partner-card {{ margin-top:1.8rem; padding:1.2rem; background:linear-gradient(145deg, rgba(36,41,68,.7), rgba(16,24,46,.9)); border-radius:.9rem; border:1px solid rgba(109,74,255,.35); box-shadow:0 12px 24px rgba(12,16,32,.45); }}
  .non-partner-card h2 {{ margin:0 0 .4rem 0; font-size:1.1rem; color:#fff; letter-spacing:.01em; }}
  .non-partner-card p {{ margin:0 0 1rem 0; font-size:.85rem; color:var(--accent-2); opacity:.85; }}
  .non-partner-list {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:.9rem; }}
  .non-partner-item {{ display:flex; flex-direction:column; gap:.6rem; padding:.9rem 1rem; background:rgba(10,14,32,.85); border:1px solid rgba(155,176,255,.2); border-radius:.7rem; position:relative; overflow:hidden; }}
  .non-partner-item::before {{ content:""; position:absolute; inset:0; border-radius:inherit; pointer-events:none; border:1px solid rgba(109,74,255,.25); opacity:0; transition:opacity .2s ease; }}
  .non-partner-item:hover::before {{ opacity:1; }}
  .non-partner-header {{ display:flex; justify-content:space-between; align-items:center; gap:.6rem; flex-wrap:wrap; }}
  .non-partner-header strong {{ font-size:1rem; color:#fff; letter-spacing:.01em; }}
  .non-partner-badges {{ display:flex; gap:.4rem; flex-wrap:wrap; }}
  .non-partner-meta {{ display:flex; flex-direction:column; gap:.25rem; font-size:.8rem; color:var(--muted); padding-left:.2rem; }}
  .non-partner-meta span {{ display:flex; align-items:center; gap:.45rem; flex-wrap:wrap; }}
  .non-partner-meta .meta-label {{ color:var(--accent-2); font-weight:600; min-width:5.4rem; text-transform:uppercase; letter-spacing:.06em; font-size:.7rem; }}
  .non-partner-warning {{ color:var(--err-fg); font-weight:600; font-size:.75rem; }}
  .non-partner-manage {{ background:#0f1422; border:1px solid rgba(109,74,255,.25); border-radius:.6rem; padding:.6rem; }}
  .non-partner-manage > summary {{ cursor:pointer; font-size:.8rem; color:var(--accent-2); font-weight:600; list-style:none; }}
  .non-partner-manage[open] > summary {{ color:#fff; }}
  .non-partner-manage .manage-body {{ margin-top:.5rem; display:flex; flex-direction:column; gap:.6rem; }}
  .non-partner-actions {{ display:flex; flex-wrap:wrap; gap:.4rem; }}
  .non-partner-note {{ font-size:.75rem; color:var(--muted); }}
  .chart-panel {{ background:#10162a; border:1px solid var(--bd); border-radius:.7rem; padding:1rem; margin-top:1rem; }}
  .chart-panel h3 {{ margin:0 0 .6rem 0; font-size:1.1rem; color:var(--accent-2); }}
  .chart-panel canvas {{ width:100%; height:320px; max-height:360px; }}
  .chart-note {{ margin-top:.6rem; font-size:.85rem; color:var(--muted); }}
  .chart-empty {{ margin-top:1rem; font-size:.9rem; color:var(--muted); font-style:italic; }}
  .analysis-controls {{ margin-top:.8rem; }}
  .user-form {{ margin-top:.8rem; }}
  .user-hint {{ margin-top:.4rem; font-size:.8rem; color:var(--muted); }}
  .user-warning {{ margin-top:.6rem; color:var(--err-fg); font-weight:600; }}
  .user-summary {{ display:flex; flex-wrap:wrap; gap:.8rem; margin-top:1rem; }}
  .user-summary-item {{ background:#10162a; border:1px solid var(--bd); border-radius:.6rem; padding:.6rem .9rem; min-width:140px; }}
  .user-summary-item .label {{ display:block; color:var(--muted); font-size:.8rem; }}
  .user-summary-item .value {{ display:block; color:#fff; font-size:1.05rem; font-weight:600; }}
  .user-meta {{ margin-top:.6rem; font-size:.85rem; color:var(--muted); }}
  .user-meta strong {{ color:#fff; font-weight:600; }}
  .user-chart-grid {{ display:flex; flex-wrap:wrap; gap:1rem; margin-top:1rem; }}
  .user-chart-panel {{ flex:1 1 280px; }}
  .user-section-empty {{ margin-top:1rem; font-size:.9rem; color:var(--muted); font-style:italic; }}
  .toggle-group {{ display:flex; gap:.4rem; flex-wrap:wrap; }}
  .btn-active {{ background:var(--accent); color:#fff; border:1px solid var(--accent); }}
  .discord-status {{ display:flex; flex-direction:column; gap:.3rem; }}
  .discord-icon {{ font-weight:600; }}
  .discord-warning {{ color:var(--err-fg); font-size:.8rem; font-weight:600; }}
  .discord-cell {{ display:flex; flex-direction:column; gap:.3rem; align-items:flex-start; }}
  .discord-cell .discord-main {{ display:flex; align-items:center; gap:.4rem; }}
  .discord-cell .discord-flag {{ font-weight:600; }}
  details.discord-inline {{ display:inline-block; }}
  details.discord-inline > summary {{
    cursor:pointer;
    display:inline-flex;
    align-items:center;
    justify-content:center;
    width:1.6rem;
    height:1.6rem;
    border-radius:999px;
    border:1px solid var(--bd);
    background:#1a1f2e;
    color:var(--accent-2);
    font-weight:600;
    margin:0;
  }}
  details.discord-inline[open] > summary {{
    background:var(--accent);
    color:#fff;
    border-color:var(--accent);
  }}
  .discord-inline-body {{
    margin-top:.4rem;
    background:#10162a;
    border:1px solid var(--bd);
    border-radius:.4rem;
    padding:.6rem;
    display:flex;
    flex-direction:column;
    gap:.5rem;
  }}
  .discord-inline-body label {{
    display:flex;
    flex-direction:column;
    gap:.3rem;
    font-size:.8rem;
    color:var(--muted);
  }}
  .discord-inline-body input[type="text"] {{
    min-width:14rem;
  }}
  .discord-inline-body .form-actions {{
    display:flex;
    gap:.4rem;
  }}
  details.advanced-details {{ margin-top:.4rem; width:100%; }}
  details.advanced-details > summary {{ cursor:pointer; font-size:.85rem; color:var(--accent-2); }}
  details.advanced-details[open] > summary {{ color:#fff; }}
  .advanced-content {{ margin-top:.6rem; display:flex; flex-direction:column; gap:.7rem; background:#10162a; padding:.6rem .7rem; border:1px solid var(--bd); border-radius:.5rem; }}
  .advanced-content .form-row {{ display:flex; flex-wrap:wrap; gap:.8rem; align-items:flex-end; }}
  .advanced-content label {{ display:flex; flex-direction:column; gap:.3rem; font-size:.85rem; color:var(--muted); }}
  .advanced-content input[type="text"] {{ background:#0f1422; border:1px solid var(--bd); color:var(--text); padding:.4rem .6rem; border-radius:.4rem; min-width:14rem; }}
  .discord-preview {{ display:flex; flex-direction:column; gap:.35rem; padding:.5rem .6rem; background:#0f1422; border:1px solid var(--bd); border-radius:.4rem; font-size:.8rem; color:var(--muted); }}
  .discord-preview-row {{ display:flex; gap:.6rem; align-items:center; flex-wrap:wrap; }}
  .discord-preview-row .preview-label {{ color:var(--accent-2); font-weight:600; min-width:4.5rem; }}
  .discord-preview-row .preview-empty {{ color:var(--muted); font-style:italic; }}
  .checkbox-label {{ display:flex; align-items:center; gap:.4rem; font-size:.85rem; color:var(--muted); }}
  .checkbox-label input[type="checkbox"] {{ width:1rem; height:1rem; }}
  .advanced-content .hint {{ font-size:.75rem; color:var(--muted); }}
</style>
{nav_html}
{flash}
{body}
"""


    def _streamer_detail_view(self, data: dict, active: str) -> str:
        login = data["login"]

        stats = data.get("stats_30d", {})
        sessions = data.get("recent_sessions", [])

        # Prepare chart data for recent sessions (reversed to show chronological order in chart)
        chart_labels = [s["started_at"][5:16] for s in reversed(sessions)]
        chart_viewers = [s["avg_viewers"] for s in reversed(sessions)]
        chart_peaks = [s["peak_viewers"] for s in reversed(sessions)]

        body = f"""
        <div class="card-header">
            <h1>Analytics: {login}</h1>
            <a href="/twitch/stats" class="btn btn-secondary btn-small">← Back to List</a>
        </div>

        <div class="user-summary">
            <div class="user-summary-item">
                <span class="label">Total Streams (30d)</span>
                <span class="value">{stats.get("total_streams", 0)}</span>
            </div>
            <div class="user-summary-item">
                <span class="label">Avg Viewers</span>
                <span class="value">{int(stats.get("avg_avg_viewers") or 0)}</span>
            </div>
            <div class="user-summary-item">
                <span class="label">Peak Viewers</span>
                <span class="value">{stats.get("max_peak", 0)}</span>
            </div>
             <div class="user-summary-item">
                <span class="label">New Followers</span>
                <span class="value">{stats.get("total_follower_delta", 0)}</span>
            </div>
            <div class="user-summary-item">
                <span class="label">Unique Chatters</span>
                <span class="value">{stats.get("total_unique_chatters", 0)}</span>
            </div>
        </div>

        <div class="chart-panel">
            <h3>Viewer Trends (Recent Sessions)</h3>
            <canvas id="streamerChart"></canvas>
        </div>

        <div class="card" style="margin-top: 1rem;">
            <h3>Recent Sessions</h3>
            <table>
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Title</th>
                        <th>Duration</th>
                        <th>Avg Viewers</th>
                        <th>Peak</th>
                        <th>Followers</th>
                        <th>Details</th>
                    </tr>
                </thead>
                <tbody>
        """
        for s in sessions:
            dur_min = (s["duration_seconds"] or 0) // 60
            body += f"""
                    <tr>
                        <td>{s["started_at"]}</td>
                        <td><small>{html.escape(s["stream_title"] or "")}</small></td>
                        <td>{dur_min} min</td>
                        <td>{s["avg_viewers"]}</td>
                        <td>{s["peak_viewers"]}</td>
                        <td>{s["follower_delta"] or 0}</td>
                        <td><a href="/twitch/session/{s['id']}" class="btn btn-small">Analysis</a></td>
                    </tr>
            """
        body += """
                </tbody>
            </table>
        </div>

        <script>
            const ctx = document.getElementById('streamerChart');
            new Chart(ctx, {
                type: 'line',
                data: {
                    labels: """ + str(chart_labels) + """,
                    datasets: [{
                        label: 'Avg Viewers',
                        data: """ + str(chart_viewers) + """,
                        borderColor: '#6d4aff',
                        backgroundColor: 'rgba(109, 74, 255, 0.1)',
                        tension: 0.3,
                        fill: true
                    }, {
                        label: 'Peak Viewers',
                        data: """ + str(chart_peaks) + """,
                        borderColor: '#9bb0ff',
                        borderDash: [5, 5],
                        tension: 0.3
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { labels: { color: '#eeeeee' } }
                    },
                    scales: {
                        y: { 
                            beginAtZero: true, 
                            grid: { color: '#2a3044' },
                            ticks: { color: '#9aa4b2' }
                        },
                        x: {
                            grid: { display: false },
                            ticks: { color: '#9aa4b2' }
                        }
                    }
                }
            });
        </script>
        """
        return self._html(body, active)

    def _session_detail_view(self, data: dict, active: str) -> str:
        s = data["session"]
        timeline = data.get("timeline", [])
        top_chatters = data.get("top_chatters", [])

        t_labels = [f"{t['minutes_from_start']}m" for t in timeline]
        t_values = [t["viewer_count"] for t in timeline]

        body = f"""
        <div class="card-header">
            <h1>Session Analysis</h1>
            <a href="/twitch/streamer/{s['streamer_login']}" class="btn btn-secondary btn-small">← Back to Streamer</a>
        </div>
        <div class="card" style="margin-top: 1rem;">
             <div class="row">
                <div class="discord-cell">
                    <span class="label" style="color:var(--muted)">Streamer</span>
                    <strong>{s['streamer_login']}</strong>
                </div>
                <div class="discord-cell">
                    <span class="label" style="color:var(--muted)">Date</span>
                    <strong>{s['started_at']}</strong>
                </div>
                 <div class="discord-cell">
                    <span class="label" style="color:var(--muted)">Duration</span>
                    <strong>{(s['duration_seconds'] or 0)//60} min</strong>
                </div>
                 <div class="discord-cell">
                    <span class="label" style="color:var(--muted)">Avg Viewers</span>
                    <strong>{s['avg_viewers']}</strong>
                </div>
                 <div class="discord-cell">
                    <span class="label" style="color:var(--muted)">Max Peak</span>
                    <strong>{s['peak_viewers']}</strong>
                </div>
            </div>
            <div style="margin-top: 1rem; color: var(--accent-2);">
                {html.escape(s['stream_title'] or "")}
            </div>
        </div>

        <div class="chart-panel">
            <h3>Viewer Retention (Timeline)</h3>
            <canvas id="sessionChart"></canvas>
        </div>

        <div class="row" style="align-items: flex-start; margin-top: 1rem;">
            <div class="card" style="flex: 1;">
                <h3>Engagement Metrics</h3>
                <ul>
                    <li><strong>Retention 5m:</strong> {s.get('retention_5m') or '-'}%</li>
                    <li><strong>Retention 10m:</strong> {s.get('retention_10m') or '-'}%</li>
                    <li><strong>Dropoff:</strong> {s.get('dropoff_pct') or '-'}% ({s.get('dropoff_label') or 'N/A'})</li>
                    <li><strong>Unique Chatters:</strong> {s.get('unique_chatters')}</li>
                    <li><strong>New Chatters:</strong> {s.get('first_time_chatters')}</li>
                    <li><strong>Returning Chatters:</strong> {s.get('returning_chatters')}</li>
                </ul>
            </div>
            <div class="card" style="flex: 1;">
                <h3>Top Chatters</h3>
                <table>
                    <thead><tr><th>User</th><th>Messages</th></tr></thead>
                    <tbody>
        """
        for c in top_chatters:
            body += f"<tr><td>{c['chatter_login']}</td><td>{c['messages']}</td></tr>"
        
        body += """
                    </tbody>
                </table>
            </div>
        </div>

        <script>
            const ctx = document.getElementById('sessionChart');
            new Chart(ctx, {
                type: 'line',
                data: {
                    labels: """ + str(t_labels) + """,
                    datasets: [{
                        label: 'Viewers',
                        data: """ + str(t_values) + """,
                        borderColor: '#6d4aff',
                        backgroundColor: 'rgba(109, 74, 255, 0.2)',
                        fill: true,
                        tension: 0.4,
                        pointRadius: 0,
                        pointHoverRadius: 6
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {
                        mode: 'index',
                        intersect: false,
                    },
                    plugins: {
                        legend: { display: false }
                    },
                    scales: {
                        y: { 
                            beginAtZero: true, 
                            grid: { color: '#2a3044' },
                            ticks: { color: '#9aa4b2' }
                        },
                        x: {
                            grid: { display: false },
                            ticks: { maxTicksLimit: 20, color: '#9aa4b2' }
                        }
                    }
                }
            });
        </script>
        """
        return self._html(body, active)

    def _comparison_view(self, data: dict, active: str) -> str:
        cat = data.get("category", {})
        track = data.get("tracked_avg", {})
        top = data.get("top_streamers", [])

        body = f"""
        <div class="card-header">
            <h1>Market Analysis (Last 30 Days)</h1>
        </div>
        
        <div class="user-summary">
            <div class="user-summary-item" style="border-color: var(--accent);">
                <span class="label">Tracked Avg Viewers</span>
                <span class="value">{int(track.get("avg_viewers") or 0)}</span>
            </div>
            <div class="user-summary-item" style="border-color: var(--muted);">
                <span class="label">Deadlock Category Avg</span>
                <span class="value">{int(cat.get("avg_viewers") or 0)}</span>
            </div>
             <div class="user-summary-item">
                <span class="label">Category Peak</span>
                <span class="value">{cat.get("peak_viewers") or 0}</span>
            </div>
        </div>

        <div class="chart-panel">
            <h3>Top 5 Performers (Avg Viewers)</h3>
            <canvas id="topChart"></canvas>
        </div>

        <div class="card" style="margin-top: 1rem;">
            <h3>Top Streamers Table</h3>
            <table>
                <thead>
                    <tr>
                        <th>Rank</th>
                        <th>Streamer</th>
                        <th>Avg Viewers</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody>
        """
        
        chart_labels = []
        chart_data = []

        for i, s in enumerate(top, 1):
            chart_labels.append(s["streamer_login"])
            chart_data.append(s["val"])
            body += f"""
                    <tr>
                        <td>#{i}</td>
                        <td>{s['streamer_login']}</td>
                        <td>{int(s['val'])}</td>
                        <td><a href="/twitch/streamer/{s['streamer_login']}" class="btn btn-small">Stats</a></td>
                    </tr>
            """
        
        body += """
                </tbody>
            </table>
        </div>

        <script>
            const ctx = document.getElementById('topChart');
            new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: """ + str(chart_labels) + """,
                    datasets: [{
                        label: 'Avg Viewers',
                        data: """ + str(chart_data) + """,
                        backgroundColor: [
                            'rgba(109, 74, 255, 0.8)',
                            'rgba(109, 74, 255, 0.6)',
                            'rgba(109, 74, 255, 0.4)',
                            'rgba(109, 74, 255, 0.3)',
                            'rgba(109, 74, 255, 0.2)'
                        ],
                        borderColor: '#6d4aff',
                        borderWidth: 1
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false }
                    },
                    scales: {
                        y: { 
                            beginAtZero: true, 
                            grid: { color: '#2a3044' },
                            ticks: { color: '#9aa4b2' }
                        },
                        x: {
                            grid: { display: false },
                            ticks: { color: '#eeeeee' }
                        }
                    }
                }
            });
        </script>
        """
        return self._html(body, active)
