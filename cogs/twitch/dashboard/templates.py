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
<style>
  :root {{
    --bg:#0f0f23; --card:#151a28; --bd:#2a3044; --text:#eeeeee; --muted:#9aa4b2;
    --accent:#6d4aff; --accent-2:#9bb0ff; --ok-bg:#15391f; --ok-bd:#1d6b33; --ok-fg:#b6f0c8;
    --err-bg:#3a1a1a; --err-bd:#792e2e; --err-fg:#ffd2d2;
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, Arial, sans-serif; max-width: 1000px; margin: 2rem auto; color:var(--text); background:var(--bg); }}
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
  .chart-panel {{ background:#10162a; border:1px solid var(--bd); border-radius:.7rem; padding:1rem; margin-top:1rem; }}
  .chart-panel h3 {{ margin:0 0 .6rem 0; font-size:1.1rem; color:var(--accent-2); }}
  .chart-panel canvas {{ width:100%; height:320px; max-height:360px; }}
  .chart-note {{ margin-top:.6rem; font-size:.85rem; color:var(--muted); }}
  .chart-empty {{ margin-top:1rem; font-size:.9rem; color:var(--muted); font-style:italic; }}
  .toggle-group {{ display:flex; gap:.4rem; flex-wrap:wrap; }}
  .btn-active {{ background:var(--accent); color:#fff; border:1px solid var(--accent); }}
  .discord-status {{ display:flex; flex-direction:column; gap:.3rem; }}
  .discord-icon {{ font-weight:600; }}
  details.advanced-details {{ margin-top:.4rem; width:100%; }}
  details.advanced-details > summary {{ cursor:pointer; font-size:.85rem; color:var(--accent-2); }}
  details.advanced-details[open] > summary {{ color:#fff; }}
  .advanced-content {{ margin-top:.6rem; display:flex; flex-direction:column; gap:.6rem; background:#10162a; padding:.6rem; border:1px solid var(--bd); border-radius:.5rem; }}
  .advanced-content .form-row {{ display:flex; flex-wrap:wrap; gap:.8rem; align-items:flex-end; }}
  .advanced-content label {{ display:flex; flex-direction:column; gap:.3rem; font-size:.85rem; color:var(--muted); }}
  .advanced-content input[type="text"] {{ background:#0f1422; border:1px solid var(--bd); color:var(--text); padding:.4rem .6rem; border-radius:.4rem; min-width:14rem; }}
  .checkbox-label {{ display:flex; align-items:center; gap:.4rem; font-size:.85rem; color:var(--muted); }}
  .checkbox-label input[type="checkbox"] {{ width:1rem; height:1rem; }}
  .advanced-content .hint {{ font-size:.75rem; color:var(--muted); }}
</style>
{nav_html}
{flash}
{body}
"""


__all__ = ["DashboardTemplateMixin"]
