"""Statistics views for the Twitch dashboard."""

from __future__ import annotations

import html
import json
from typing import List, Optional

from aiohttp import web


class DashboardStatsMixin:
    async def _render_stats_page(self, request: web.Request, *, partner_view: bool) -> web.Response:
        view_mode = (request.query.get("view") or "top").lower()
        show_all = view_mode == "all"
        display_mode = (request.query.get("display") or "charts").lower()
        if display_mode not in {"charts", "raw"}:
            display_mode = "charts"

        focus_mode = (request.query.get("focus") or "time").lower()
        if focus_mode not in {"time", "weekday", "user"}:
            focus_mode = "time"

        show_discord_private = self._is_local_request(request)

        streamer_query = request.query.get("streamer") or ""
        normalized_streamer: Optional[str] = None
        streamer_warning = ""
        if focus_mode == "user":
            query_clean = streamer_query.strip()
            if query_clean:
                normalized_streamer = self._normalize_login(streamer_query)
                if normalized_streamer is None:
                    streamer_warning = "Ungültiger Twitch-Login."
            else:
                normalized_streamer = None

        def _parse_int(*names: str) -> Optional[int]:
            for name in names:
                raw = request.query.get(name)
                if raw is None or raw == "":
                    continue
                try:
                    value = int(raw)
                except ValueError:
                    continue
                return max(0, value)
            return None

        def _parse_float(*names: str) -> Optional[float]:
            for name in names:
                raw = request.query.get(name)
                if raw is None or raw == "":
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    continue
                return max(0.0, value)
            return None

        def _clamp_hour(value: Optional[int]) -> Optional[int]:
            if value is None:
                return None
            if value < 0:
                return 0
            if value > 23:
                return 23
            return value

        stats_hour = _clamp_hour(_parse_int("hour"))
        hour_from = _clamp_hour(_parse_int("hour_from", "from_hour", "start_hour"))
        hour_to = _clamp_hour(_parse_int("hour_to", "to_hour", "end_hour"))

        if stats_hour is not None:
            if hour_from is None:
                hour_from = stats_hour
            if hour_to is None:
                hour_to = stats_hour

        stats = await self._stats(
            hour_from=hour_from,
            hour_to=hour_to,
            streamer=normalized_streamer if focus_mode == "user" else None,
        )
        tracked = stats.get("tracked", {}) or {}
        category = stats.get("category", {}) or {}
        retention_stats = stats.get("retention") or {}
        discovery_stats = stats.get("discovery") or {}
        chat_stats = stats.get("chat") or {}

        min_samples = _parse_int("min_samples", "samples")
        min_avg = _parse_float("min_avg", "avg")
        partner_filter = (request.query.get("partner") or "any").lower()
        if partner_filter not in {"only", "exclude", "any"}:
            partner_filter = "any"

        discord_filter = (request.query.get("discord") or "any").lower()
        if discord_filter not in {"any", "yes", "no"}:
            discord_filter = "any"
        if not show_discord_private:
            discord_filter = "any"

        base_path = request.rel_url.path

        preserved_params = {}
        for key in (
            "view",
            "display",
            "min_samples",
            "min_avg",
            "partner",
            "discord",
            "hour",
            "hour_from",
            "hour_to",
            "focus",
            "streamer",
        ):
            if key in request.query:
                preserved_params[key] = request.query[key]

        def _build_url(**updates) -> str:
            params = {**preserved_params, **updates}
            merged = {k: v for k, v in params.items() if v not in {None, ""}}
            query = "&".join(f"{k}={html.escape(str(v), quote=True)}" for k, v in merged.items())
            if query:
                return f"{base_path}?{query}"
            return base_path

        tracked_items = tracked.get("top", []) or []
        category_items = category.get("top", []) or []

        def _passes_filters(item: dict) -> bool:
            samples = int(item.get("samples") or 0)
            avg_viewers = float(item.get("avg_viewers") or 0.0)
            is_partner_flag = bool(item.get("is_partner"))
            is_on_discord = bool(item.get("is_on_discord"))
            if min_samples is not None and samples < min_samples:
                return False
            if min_avg is not None and avg_viewers < min_avg:
                return False
            if partner_filter == "only" and not is_partner_flag:
                return False
            if partner_filter == "exclude" and is_partner_flag:
                return False
            if show_discord_private:
                if discord_filter == "yes" and not is_on_discord:
                    return False
                if discord_filter == "no" and is_on_discord:
                    return False
            return True

        tracked_items = [item for item in tracked_items if _passes_filters(item)]
        category_items = [item for item in category_items if _passes_filters(item)]

        if not show_all:
            tracked_items = tracked_items[:10]
            category_items = category_items[:10]

        def render_table(items: List[dict]) -> str:
            column_count = 6 if show_discord_private else 5
            if not items:
                return f"<tr><td colspan={column_count}><i>Keine Daten für die aktuellen Filter.</i></td></tr>"
            rows = []
            for item in items:
                streamer = html.escape(str(item.get("streamer", "")))
                streamer_raw = str(item.get("streamer", ""))
                escaped_login = html.escape(streamer_raw, quote=True)
                samples = int(item.get("samples") or 0)
                avg_viewers = float(item.get("avg_viewers") or 0.0)
                max_viewers = int(item.get("max_viewers") or 0)
                is_partner = bool(item.get("is_partner"))
                partner_text = "Ja" if is_partner else "Nein"
                partner_value = "1" if is_partner else "0"
                discord_member = bool(item.get("is_on_discord"))
                discord_value = "1" if discord_member else "0"
                discord_text = "Ja" if discord_member else "Nein"
                discord_user_id = str(item.get("discord_user_id") or "").strip()
                discord_display_name = str(item.get("discord_display_name") or "").strip()
                if not show_discord_private:
                    discord_user_id = ""
                    discord_display_name = ""
                discord_meta_parts: List[str] = []
                if show_discord_private and discord_display_name:
                    discord_meta_parts.append(html.escape(discord_display_name))
                if show_discord_private and discord_user_id:
                    discord_meta_parts.append(f"ID: {html.escape(discord_user_id)}")
                discord_meta_html = (
                    f"<div class='status-meta'>{' \a '.join(discord_meta_parts)}</div>"
                    if discord_meta_parts
                    else ""
                )
                inline_link_html = ""
                if show_discord_private and (not is_partner) and not discord_member:
                    inline_link_html = (
                        "<details class='discord-inline'>"
                        "  <summary title='Discord verknüpfen'>+</summary>"
                        "  <div class='discord-inline-body'>"
                        "    <form method='post' action='/twitch/discord_link'>"
                        f"      <input type='hidden' name='login' value='{escaped_login}'>"
                        "      <label>Discord User ID<input type='text' name='discord_user_id' placeholder='123456789012345678'></label>"
                        "      <label>Discord Anzeigename<input type='text' name='discord_display_name' placeholder='Discord-Name'></label>"
                        "      <input type='hidden' name='member_flag' value='1'>"
                        "      <div class='form-actions'><button class='btn btn-small'>Speichern</button></div>"
                        "    </form>"
                        "  </div>"
                        "</details>"
                    )
                row_parts = [
                    "<tr>",
                    f"<td>{streamer}</td>",
                    f"<td data-value=\"{samples}\">{samples}</td>",
                    f"<td data-value=\"{avg_viewers:.4f}\">{avg_viewers:.1f}</td>",
                    f"<td data-value=\"{max_viewers}\">{max_viewers}</td>",
                    f"<td data-value=\"{partner_value}\">{partner_text}</td>",
                ]
                if show_discord_private:
                    discord_main = (
                        "<div class='discord-main'>"
                        f"  <span class='discord-flag'>{discord_text}</span>"
                        f"  {inline_link_html}"
                        "</div>"
                    )
                    discord_cell = (
                        "<div class='discord-cell'>"
                        f"  {discord_main}"
                        f"  {discord_meta_html}"
                        "</div>"
                    )
                    row_parts.append(f"<td data-value=\"{discord_value}\">{discord_cell}</td>")
                row_parts.append("</tr>")
                rows.append("".join(row_parts))
            return "".join(rows)
        tracked_hourly = tracked.get("hourly", []) or []
        category_hourly = category.get("hourly", []) or []
        tracked_weekday = tracked.get("weekday", []) or []
        category_weekday = category.get("weekday", []) or []

        def _format_float(value: float) -> str:
            return f"{value:.1f}"

        def _float_or_none(value, *, digits: int = 1):
            if value is None:
                return None
            try:
                return round(float(value), digits)
            except (TypeError, ValueError):
                return None

        def _int_or_none(value):
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def render_hour_table(items: List[dict]) -> str:
            if not items:
                return "<tr><td colspan=4><i>Keine Daten verfügbar.</i></td></tr>"
            rows = []
            for item in sorted(items, key=lambda d: int(d.get("hour") or 0)):
                hour = int(item.get("hour") or 0)
                samples = int(item.get("samples") or 0)
                avg_viewers = float(item.get("avg_viewers") or 0.0)
                max_viewers = int(item.get("max_viewers") or 0)
                rows.append(
                    "<tr>"
                    f"<td data-value=\"{hour}\">{hour:02d}:00</td>"
                    f"<td data-value=\"{samples}\">{samples}</td>"
                    f"<td data-value=\"{avg_viewers:.4f}\">{_format_float(avg_viewers)}</td>"
                    f"<td data-value=\"{max_viewers}\">{max_viewers}</td>"
                    "</tr>"
                )
            return "".join(rows)

        weekday_labels = {
            0: "Sonntag",
            1: "Montag",
            2: "Dienstag",
            3: "Mittwoch",
            4: "Donnerstag",
            5: "Freitag",
            6: "Samstag",
        }
        weekday_order = [1, 2, 3, 4, 5, 6, 0]

        def render_weekday_table(items: List[dict]) -> str:
            if not items:
                return "<tr><td colspan=4><i>Keine Daten verfügbar.</i></td></tr>"
            by_day = {int(d.get("weekday") or 0): d for d in items}
            rows = []
            for idx in weekday_order:
                item = by_day.get(idx)
                if not item:
                    continue
                samples = int(item.get("samples") or 0)
                avg_viewers = float(item.get("avg_viewers") or 0.0)
                max_viewers = int(item.get("max_viewers") or 0)
                label = weekday_labels.get(idx, str(idx))
                rows.append(
                    "<tr>"
                    f"<td data-value=\"{idx}\">{html.escape(label)}</td>"
                    f"<td data-value=\"{samples}\">{samples}</td>"
                    f"<td data-value=\"{avg_viewers:.4f}\">{_format_float(avg_viewers)}</td>"
                    f"<td data-value=\"{max_viewers}\">{max_viewers}</td>"
                    "</tr>"
                )
            if not rows:
                return "<tr><td colspan=4><i>Keine Daten verfügbar.</i></td></tr>"
            return "".join(rows)

        category_hour_rows = render_hour_table(category_hourly)
        tracked_hour_rows = render_hour_table(tracked_hourly)
        category_weekday_rows = render_weekday_table(category_weekday)
        tracked_weekday_rows = render_weekday_table(tracked_weekday)

        category_hour_map = {
            int(item.get("hour") or 0): item for item in category_hourly if isinstance(item, dict)
        }
        tracked_hour_map = {
            int(item.get("hour") or 0): item for item in tracked_hourly if isinstance(item, dict)
        }

        def _build_dataset(
            data_points,
            *,
            label: str,
            color: str,
            background: str,
            axis: str = "yAvg",
            fill: bool = True,
            dash: Optional[List[int]] = None,
            tension: float = 0.35,
        ) -> Optional[dict]:
            if not data_points or not any(value is not None for value in data_points):
                return None
            dataset = {
                "label": label,
                "data": data_points,
                "borderColor": color,
                "backgroundColor": background,
                "fill": fill,
                "tension": tension,
                "spanGaps": True,
                "borderWidth": 2,
                "yAxisID": axis,
                "pointRadius": 3,
                "pointHoverRadius": 4,
            }
            if dash:
                dataset["borderDash"] = dash
            return dataset

        hour_labels = [f"{hour:02d}:00" for hour in range(24)]
        category_hour_avg = [
            _float_or_none((category_hour_map.get(hour) or {}).get("avg_viewers")) for hour in range(24)
        ]
        tracked_hour_avg = [
            _float_or_none((tracked_hour_map.get(hour) or {}).get("avg_viewers")) for hour in range(24)
        ]
        category_hour_peak = [
            _int_or_none((category_hour_map.get(hour) or {}).get("max_viewers")) for hour in range(24)
        ]
        tracked_hour_peak = [
            _int_or_none((tracked_hour_map.get(hour) or {}).get("max_viewers")) for hour in range(24)
        ]

        hour_datasets = [
            ds
            for ds in (
                _build_dataset(
                    category_hour_avg,
                    label="Kategorie Ø Viewer",
                    color="#6d4aff",
                    background="rgba(109, 74, 255, 0.25)",
                ),
                _build_dataset(
                    tracked_hour_avg,
                    label="Tracked Ø Viewer",
                    color="#4adede",
                    background="rgba(74, 222, 222, 0.2)",
                ),
                _build_dataset(
                    category_hour_peak,
                    label="Kategorie Peak Viewer",
                    color="#ffb347",
                    background="rgba(255, 179, 71, 0.1)",
                    axis="yPeak",
                    fill=False,
                    dash=[6, 4],
                    tension=0.25,
                ),
                _build_dataset(
                    tracked_hour_peak,
                    label="Tracked Peak Viewer",
                    color="#ff6f91",
                    background="rgba(255, 111, 145, 0.1)",
                    axis="yPeak",
                    fill=False,
                    dash=[4, 4],
                    tension=0.25,
                ),
            )
            if ds
        ]

        category_weekday_map = {
            int(item.get("weekday") or 0): item for item in category_weekday if isinstance(item, dict)
        }
        tracked_weekday_map = {
            int(item.get("weekday") or 0): item for item in tracked_weekday if isinstance(item, dict)
        }

        weekday_labels_list = [weekday_labels.get(idx, str(idx)) for idx in weekday_order]
        category_weekday_avg = [
            _float_or_none((category_weekday_map.get(idx) or {}).get("avg_viewers"))
            for idx in weekday_order
        ]
        tracked_weekday_avg = [
            _float_or_none((tracked_weekday_map.get(idx) or {}).get("avg_viewers"))
            for idx in weekday_order
        ]
        category_weekday_peak = [
            _int_or_none((category_weekday_map.get(idx) or {}).get("max_viewers"))
            for idx in weekday_order
        ]
        tracked_weekday_peak = [
            _int_or_none((tracked_weekday_map.get(idx) or {}).get("max_viewers"))
            for idx in weekday_order
        ]

        weekday_datasets = [
            ds
            for ds in (
                _build_dataset(
                    category_weekday_avg,
                    label="Kategorie Ø Viewer",
                    color="#6d4aff",
                    background="rgba(109, 74, 255, 0.25)",
                ),
                _build_dataset(
                    tracked_weekday_avg,
                    label="Tracked Ø Viewer",
                    color="#4adede",
                    background="rgba(74, 222, 222, 0.2)",
                ),
                _build_dataset(
                    category_weekday_peak,
                    label="Kategorie Peak Viewer",
                    color="#ffb347",
                    background="rgba(255, 179, 71, 0.1)",
                    axis="yPeak",
                    fill=False,
                    dash=[6, 4],
                    tension=0.25,
                ),
                _build_dataset(
                    tracked_weekday_peak,
                    label="Tracked Peak Viewer",
                    color="#ff6f91",
                    background="rgba(255, 111, 145, 0.1)",
                    axis="yPeak",
                    fill=False,
                    dash=[4, 4],
                    tension=0.25,
                ),
            )
            if ds
        ]

        hour_chart_block = (
            '<div class="chart-panel">'
            '  <h3>Ø Viewer nach Stunde</h3>'
            '  <canvas id="hourly-viewers-chart"></canvas>'
            '  <div class="chart-note">Zeiten in UTC. Datenpunkte ohne Werte werden ausgeblendet.</div>'
            '</div>'
        )

        weekday_chart_block = (
            '<div class="chart-panel">'
            '  <h3>Ø Viewer nach Wochentag</h3>'
            '  <canvas id="weekday-viewers-chart"></canvas>'
            '  <div class="chart-note">Zeiten in UTC. Datenpunkte ohne Werte werden ausgeblendet.</div>'
            '</div>'
        )

        hour_tables_block = (
            '<div class="row" style="gap:1.2rem; flex-wrap:wrap;">'
            '  <div style="flex:1 1 260px;">'
            '    <h3>Deadlock Kategorie — nach Stunde</h3>'
            '    <table class="sortable-table" data-table="category-hour">'
            '      <thead>'
            '        <tr>'
            '          <th data-sort-type="number">Stunde</th>'
            '          <th data-sort-type="number">Stichproben</th>'
            '          <th data-sort-type="number">Ø Viewer</th>'
            '          <th data-sort-type="number">Peak Viewer</th>'
            '        </tr>'
            '      </thead>'
            f'      <tbody>{category_hour_rows}</tbody>'
            '    </table>'
            '  </div>'
            '  <div style="flex:1 1 260px;">'
            '    <h3>Tracked Streamer — nach Stunde</h3>'
            '    <table class="sortable-table" data-table="tracked-hour">'
            '      <thead>'
            '        <tr>'
            '          <th data-sort-type="number">Stunde</th>'
            '          <th data-sort-type="number">Stichproben</th>'
            '          <th data-sort-type="number">Ø Viewer</th>'
            '          <th data-sort-type="number">Peak Viewer</th>'
            '        </tr>'
            '      </thead>'
            f'      <tbody>{tracked_hour_rows}</tbody>'
            '    </table>'
            '  </div>'
            '</div>'
        )

        weekday_tables_block = (
            '<div class="row" style="gap:1.2rem; flex-wrap:wrap;">'
            '  <div style="flex:1 1 260px;">'
            '    <h3>Deadlock Kategorie — nach Wochentag</h3>'
            '    <table class="sortable-table" data-table="category-weekday">'
            '      <thead>'
            '        <tr>'
            '          <th data-sort-type="number">Tag</th>'
            '          <th data-sort-type="number">Stichproben</th>'
            '          <th data-sort-type="number">Ø Viewer</th>'
            '          <th data-sort-type="number">Peak Viewer</th>'
            '        </tr>'
            '      </thead>'
            f'      <tbody>{category_weekday_rows}</tbody>'
            '    </table>'
            '  </div>'
            '  <div style="flex:1 1 260px;">'
            '    <h3>Tracked Streamer — nach Wochentag</h3>'
            '    <table class="sortable-table" data-table="tracked-weekday">'
            '      <thead>'
            '        <tr>'
            '          <th data-sort-type="number">Tag</th>'
            '          <th data-sort-type="number">Stichproben</th>'
            '          <th data-sort-type="number">Ø Viewer</th>'
            '          <th data-sort-type="number">Peak Viewer</th>'
            '        </tr>'
            '      </thead>'
            f'      <tbody>{tracked_weekday_rows}</tbody>'
            '    </table>'
            '  </div>'
            '</div>'
        )

        streamer_stats = stats.get("streamer")
        if not isinstance(streamer_stats, dict):
            streamer_stats = {}
        streamer_summary = streamer_stats.get("summary")
        if not isinstance(streamer_summary, dict):
            streamer_summary = {}
        streamer_has_data = bool(streamer_stats.get("had_results"))
        selected_streamer_login = (
            str(
                streamer_stats.get("display_login")
                or streamer_stats.get("login")
                or (normalized_streamer or streamer_query.strip())
                or ""
            ).strip()
        )
        user_hour_data = streamer_stats.get("hourly")
        if not isinstance(user_hour_data, list):
            user_hour_data = []
        user_weekday_data = streamer_stats.get("weekday")
        if not isinstance(user_weekday_data, list):
            user_weekday_data = []

        user_hour_map = {
            int(item.get("hour") or 0): item for item in user_hour_data if isinstance(item, dict)
        }
        user_weekday_map = {
            int(item.get("weekday") or 0): item for item in user_weekday_data if isinstance(item, dict)
        }

        user_hour_avg = [
            _float_or_none((user_hour_map.get(hour) or {}).get("avg_viewers")) for hour in range(24)
        ]
        user_hour_peak = [
            _int_or_none((user_hour_map.get(hour) or {}).get("max_viewers")) for hour in range(24)
        ]
        user_hour_datasets = []
        if streamer_has_data:
            label_prefix = selected_streamer_login or "Streamer"
            user_hour_datasets = [
                ds
                for ds in (
                    _build_dataset(
                        user_hour_avg,
                        label=f"{label_prefix} Ø Viewer",
                        color="#9bb0ff",
                        background="rgba(155, 176, 255, 0.25)",
                    ),
                    _build_dataset(
                        user_hour_peak,
                        label=f"{label_prefix} Peak Viewer",
                        color="#ff6f91",
                        background="rgba(255, 111, 145, 0.1)",
                        axis="yPeak",
                        fill=False,
                        dash=[4, 4],
                        tension=0.25,
                    ),
                )
                if ds
            ]
        user_hour_chart_payload = None
        if user_hour_datasets:
            user_hour_chart_payload = {
                "labels": hour_labels,
                "datasets": user_hour_datasets,
                "xTitle": "Stunde (UTC)",
            }

        user_weekday_avg = [
            _float_or_none((user_weekday_map.get(idx) or {}).get("avg_viewers"))
            for idx in weekday_order
        ]
        user_weekday_peak = [
            _int_or_none((user_weekday_map.get(idx) or {}).get("max_viewers"))
            for idx in weekday_order
        ]
        user_weekday_datasets = []
        if streamer_has_data:
            label_prefix = selected_streamer_login or "Streamer"
            user_weekday_datasets = [
                ds
                for ds in (
                    _build_dataset(
                        user_weekday_avg,
                        label=f"{label_prefix} Ø Viewer",
                        color="#9bb0ff",
                        background="rgba(155, 176, 255, 0.25)",
                    ),
                    _build_dataset(
                        user_weekday_peak,
                        label=f"{label_prefix} Peak Viewer",
                        color="#ff6f91",
                        background="rgba(255, 111, 145, 0.1)",
                        axis="yPeak",
                        fill=False,
                        dash=[4, 4],
                        tension=0.25,
                    ),
                )
                if ds
            ]
        user_weekday_chart_payload = None
        if user_weekday_datasets:
            user_weekday_chart_payload = {
                "labels": weekday_labels_list,
                "datasets": user_weekday_datasets,
                "xTitle": "Wochentag",
            }

        streamer_is_tracked = bool(streamer_stats.get("is_tracked"))
        streamer_is_on_discord = bool(streamer_stats.get("is_on_discord"))
        streamer_discord_name = str(streamer_stats.get("discord_display_name") or "").strip()
        streamer_discord_id = str(streamer_stats.get("discord_user_id") or "").strip()
        if not show_discord_private:
            streamer_discord_name = ""
            streamer_discord_id = ""
        streamer_source = streamer_stats.get("source")

        if display_mode == "charts":
            hour_section = hour_chart_block
            weekday_section = weekday_chart_block
        else:
            hour_section = hour_tables_block
            weekday_section = weekday_tables_block

        display_toggle_html = (
            '<div class="toggle-group">'
            f'  <a class="btn btn-small{" btn-active" if display_mode == "charts" else " btn-secondary"}" href="{_build_url(display="charts")}">Charts</a>'
            f'  <a class="btn btn-small{" btn-active" if display_mode == "raw" else " btn-secondary"}" href="{_build_url(display="raw")}">Tabelle</a>'
            '</div>'
        )
        analysis_controls_html = ""
        if focus_mode in {"time", "weekday"}:
            analysis_controls_html = f"<div class='analysis-controls'>{display_toggle_html}</div>"

        hidden_inputs = []
        for key, value in request.query.items():
            if key in {"focus", "streamer"}:
                continue
            hidden_inputs.append(
                f"<input type='hidden' name='{html.escape(key, quote=True)}' value='{html.escape(value, quote=True)}'>"
            )
        hidden_inputs_html = "".join(hidden_inputs)

        suggestions_map: dict[str, str] = {}
        if focus_mode == "user":
            try:
                stored_streamers = await self._list()
            except Exception:
                stored_streamers = []
            for row in stored_streamers:
                login = str(row.get("twitch_login") or "").strip()
                if not login:
                    continue
                lower = login.lower()
                suggestions_map.setdefault(lower, login)
        if focus_mode == "user":
            for entry in list(tracked_items) + list(category_items):
                login = str(entry.get("streamer") or "").strip()
                if not login:
                    continue
                lower = login.lower()
                suggestions_map.setdefault(lower, login)
        if selected_streamer_login:
            lower = selected_streamer_login.lower()
            suggestions_map.setdefault(lower, selected_streamer_login)
        if streamer_query.strip():
            lower = streamer_query.strip().lower()
            suggestions_map.setdefault(lower, streamer_query.strip())

        sorted_suggestions = [suggestions_map[key] for key in sorted(suggestions_map)]
        streamer_options = "".join(
            f"<option value='{html.escape(value, quote=True)}'></option>" for value in sorted_suggestions
        )
        datalist_html = f"<datalist id='twitch-streamers'>{streamer_options}</datalist>"

        reset_href = _build_url(focus="user", streamer=None)
        user_form_html = f"""
  <form method=\"get\" class=\"user-form\">
    <input type=\"hidden\" name=\"focus\" value=\"user\">
    {hidden_inputs_html}
    <div class=\"row\" style=\"gap:1rem; align-items:flex-end; flex-wrap:wrap;\">
      <label class=\"filter-label\">
        Streamer
        <input type=\"text\" name=\"streamer\" list=\"twitch-streamers\" placeholder=\"z. B. streamername\" value=\"{html.escape(streamer_query, quote=True)}\">
      </label>
      <div style=\"display:flex; gap:.6rem; align-items:center; flex-wrap:wrap;\">
        <button class=\"btn\">Anzeigen</button>
        <a class=\"btn btn-secondary\" href=\"{html.escape(reset_href)}\">Reset</a>
      </div>
    </div>
  </form>
  {datalist_html}
"""

        user_hint_html = ""
        if focus_mode == "user":
            user_hint_html = "<div class='user-hint'>Suche nach einem Twitch-Login (z. B. streamername).</div>"

        user_notice_html = ""
        if streamer_warning:
            user_notice_html = f"<div class='user-warning'>{html.escape(streamer_warning)}</div>"

        user_summary_html = ""
        if streamer_has_data:
            samples = int(streamer_summary.get("samples") or 0)
            avg_viewers = float(streamer_summary.get("avg_viewers") or 0.0)
            max_viewers = int(streamer_summary.get("max_viewers") or 0)
            partner_text = "Ja" if bool(streamer_summary.get("is_partner")) else "Nein"
            discord_text = "Ja" if streamer_is_on_discord else "Nein"

            subs_data = streamer_stats.get("subs") or {}
            sub_total = subs_data.get("total")
            sub_points = subs_data.get("points")
            sub_text = "-"
            if sub_total is not None:
                sub_text = f"{sub_total} (P: {sub_points})"

            summary_items = [
                ("Stichproben", f"{samples}"),
                ("Ø Viewer", f"{avg_viewers:.1f}"),
                ("Peak Viewer", f"{max_viewers}"),
                ("Subs", sub_text),
                ("Partner", partner_text),
            ]
            if show_discord_private:
                summary_items.append(("Auf Discord?", discord_text))
            summary_cells = "".join(
                f"<div class='user-summary-item'><span class='label'>{html.escape(label)}</span><span class='value'>{html.escape(value)}</span></div>"
                for label, value in summary_items
            )

            shared_audience = streamer_stats.get("shared_audience") or []
            shared_html = ""
            if shared_audience:
                shared_rows = "".join(
                    f"<li>{html.escape(entry.get('streamer', ''))}: {entry.get('overlap', 0)} gemeinsame Chatter</li>"
                    for entry in shared_audience[:5]
                )
                shared_html = f"<div class='status-meta' style='margin-top:0.8rem;'><strong>Shared Audience (Top 5):</strong><ul style='margin:0.2rem 0 0 1.2rem; padding:0;'>{shared_rows}</ul></div>"

            user_summary_html = f"<div class='user-summary'>{summary_cells}</div>{shared_html}"

        user_meta_html = ""
        if streamer_has_data or streamer_is_tracked or streamer_discord_name or streamer_discord_id:
            meta_parts = []
            if streamer_source:
                source_label = "Tracked" if streamer_source == "tracked" else "Kategorie"
                meta_parts.append(
                    f"<strong>Datenbasis:</strong> {html.escape(source_label)}"
                )
            tracked_text = "Ja" if streamer_is_tracked else "Nein"
            meta_parts.append(f"<strong>Partnerliste:</strong> {tracked_text}")
            if show_discord_private and (streamer_discord_name or streamer_discord_id):
                discord_bits = []
                if streamer_discord_name:
                    discord_bits.append(html.escape(streamer_discord_name))
                if streamer_discord_id:
                    discord_bits.append(f"ID: {html.escape(streamer_discord_id)}")
                meta_parts.append(
                    "<strong>Discord:</strong> " + " • ".join(discord_bits)
                )
            if meta_parts:
                user_meta_html = "<div class='user-meta'>" + "<br>".join(meta_parts) + "</div>"

        user_charts_html = ""
        if streamer_has_data:
            user_hour_chart_block = (
                '<div class="chart-panel user-chart-panel">'
                '  <h3>Ø Viewer nach Stunde</h3>'
                '  <canvas id="user-hourly-chart"></canvas>'
                '  <div class="chart-note">Zeiten in UTC. Datenpunkte ohne Werte werden ausgeblendet.</div>'
                '</div>'
            )
            user_weekday_chart_block = (
                '<div class="chart-panel user-chart-panel">'
                '  <h3>Ø Viewer nach Wochentag</h3>'
                '  <canvas id="user-weekday-chart"></canvas>'
                '  <div class="chart-note">Zeiten in UTC. Datenpunkte ohne Werte werden ausgeblendet.</div>'
                '</div>'
            )
            user_charts_html = (
                '<div class="user-chart-grid">'
                f"{user_hour_chart_block}{user_weekday_chart_block}"
                '</div>'
            )

        user_empty_html = ""
        if focus_mode == "user" and not streamer_warning:
            if not streamer_query.strip():
                user_empty_html = "<div class='user-section-empty'>Bitte oben einen Streamer auswählen.</div>"
            elif normalized_streamer and not streamer_has_data:
                user_empty_html = "<div class='user-section-empty'>Keine Daten für diesen Streamer in den letzten 30 Tagen.</div>"

        user_section_parts = [user_form_html]
        if user_hint_html:
            user_section_parts.append(user_hint_html)
        if user_notice_html:
            user_section_parts.append(user_notice_html)
        if user_summary_html:
            user_section_parts.append(user_summary_html)
        if user_meta_html:
            user_section_parts.append(user_meta_html)
        if user_charts_html:
            user_section_parts.append(user_charts_html)
        if user_empty_html:
            user_section_parts.append(user_empty_html)
        user_section = "".join(user_section_parts)

        chart_payload = {
            "hour": {
                "labels": hour_labels,
                "datasets": hour_datasets,
                "xTitle": "Stunde (UTC)",
            },
            "weekday": {
                "labels": weekday_labels_list,
                "datasets": weekday_datasets,
                "xTitle": "Wochentag",
            },
        }

        if user_hour_chart_payload:
            chart_payload["userHour"] = user_hour_chart_payload
        if user_weekday_chart_payload:
            chart_payload["userWeekday"] = user_weekday_chart_payload

        chart_payload_json = json.dumps(chart_payload, ensure_ascii=False)

        script = """
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
(function () {
  const chartData = __CHART_DATA__;

  function hasRenderableData(dataset) {
    if (!dataset || !Array.isArray(dataset.data)) {
      return false;
    }
    return dataset.data.some((value) => value !== null && value !== undefined);
  }

  function renderLineChart(config) {
    if (typeof Chart === "undefined") {
      return;
    }
    const canvas = document.getElementById(config.id);
    if (!canvas) {
      return;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      return;
    }
    const datasets = (config.data.datasets || [])
      .filter((dataset) => hasRenderableData(dataset))
      .map((dataset) => ({
        ...dataset,
        data: dataset.data.map((value) =>
          value === null || value === undefined ? null : Number(value)
        ),
      }));
    if (!datasets.length) {
      return;
    }
    const gridColor = "rgba(154, 164, 178, 0.2)";
    const options = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          labels: { color: "#dddddd" },
        },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              const value = ctx.parsed.y;
              if (value === null || value === undefined || Number.isNaN(value)) {
                return ctx.dataset.label + ": –";
              }
              const isAverage = /Ø/.test(ctx.dataset.label);
              const digits = isAverage ? 1 : 0;
              return (
                ctx.dataset.label +
                ": " +
                Number(value).toLocaleString("de-DE", {
                  minimumFractionDigits: digits,
                  maximumFractionDigits: digits,
                })
              );
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: "#dddddd" },
          grid: { color: gridColor },
        },
        yAvg: {
          type: "linear",
          position: "left",
          ticks: { color: "#dddddd" },
          grid: { color: gridColor },
          title: { display: true, text: "Ø Viewer", color: "#9bb0ff" },
        },
      },
      elements: {
        point: {
          hitRadius: 6,
        },
      },
    };

    if (config.data && config.data.xTitle) {
      options.scales.x.title = {
        display: true,
        text: config.data.xTitle,
        color: "#dddddd",
      };
    }

    const hasPeakDataset = datasets.some((dataset) => dataset.yAxisID === "yPeak");
    if (hasPeakDataset) {
      options.scales.yPeak = {
        type: "linear",
        position: "right",
        ticks: { color: "#dddddd" },
        grid: { drawOnChartArea: false },
        title: { display: true, text: "Peak Viewer", color: "#ffb347" },
      };
    }

    new Chart(ctx, {
      type: "line",
      data: {
        labels: config.data.labels || [],
        datasets,
      },
      options,
    });
  }

  if (
    chartData.hour &&
    Array.isArray(chartData.hour.datasets) &&
    chartData.hour.datasets.length
  ) {
    renderLineChart({ id: "hourly-viewers-chart", data: chartData.hour });
  }

  if (
    chartData.weekday &&
    Array.isArray(chartData.weekday.datasets) &&
    chartData.weekday.datasets.length
  ) {
    renderLineChart({ id: "weekday-viewers-chart", data: chartData.weekday });
  }

  if (
    chartData.userHour &&
    Array.isArray(chartData.userHour.datasets) &&
    chartData.userHour.datasets.length
  ) {
    renderLineChart({ id: "user-hourly-chart", data: chartData.userHour });
  }

  if (
    chartData.userWeekday &&
    Array.isArray(chartData.userWeekday.datasets) &&
    chartData.userWeekday.datasets.length
  ) {
    renderLineChart({ id: "user-weekday-chart", data: chartData.userWeekday });
  }

  const tables = document.querySelectorAll("table.sortable-table");
  tables.forEach((table) => {
    const headers = table.querySelectorAll("th[data-sort-type]");
    const tbody = table.querySelector("tbody");
    if (!tbody) {
      return;
    }
    headers.forEach((header, index) => {
      header.addEventListener("click", () => {
        const sortType = header.dataset.sortType || "string";
        const currentDir = header.dataset.sortDir === "asc" ? "desc" : "asc";
        headers.forEach((h) => h.removeAttribute("data-sort-dir"));
        header.dataset.sortDir = currentDir;
        const rows = Array.from(tbody.querySelectorAll("tr"));
        const multiplier = currentDir === "asc" ? 1 : -1;
        rows.sort((rowA, rowB) => {
          const cellA = rowA.children[index];
          const cellB = rowB.children[index];
          const rawA = cellA ? cellA.getAttribute("data-value") || cellA.textContent.trim() : "";
          const rawB = cellB ? cellB.getAttribute("data-value") || cellB.textContent.trim() : "";
          let valA = rawA;
          let valB = rawB;
          if (sortType === "number") {
            valA = Number(String(rawA).replace(/[^0-9.-]+/g, "")) || 0;
            valB = Number(String(rawB).replace(/[^0-9.-]+/g, "")) || 0;
          } else {
            valA = String(rawA).toLowerCase();
            valB = String(rawB).toLowerCase();
          }
          if (valA < valB) {
            return -1 * multiplier;
          }
          if (valA > valB) {
            return 1 * multiplier;
          }
          return 0;
        });
        rows.forEach((row) => tbody.appendChild(row));
      });
    });
  });
})();
</script>
""".replace("__CHART_DATA__", chart_payload_json)

        filter_descriptions = []
        if min_samples is not None:
            filter_descriptions.append(f"Stichproben ≥ {min_samples}")
        if min_avg is not None:
            filter_descriptions.append(f"Ø Viewer ≥ {min_avg:.1f}")
        if partner_filter == "only":
            filter_descriptions.append("Nur Partner")
        elif partner_filter == "exclude":
            filter_descriptions.append("Ohne Partner")
        if show_discord_private:
            if discord_filter == "yes":
                filter_descriptions.append("Nur Discord-Mitglieder")
            elif discord_filter == "no":
                filter_descriptions.append("Ohne Discord")
        if hour_from is not None or hour_to is not None:
            start = hour_from if hour_from is not None else hour_to
            end = hour_to if hour_to is not None else hour_from
            if start is None:
                start = 0
            if end is None:
                end = start
            if start == end:
                filter_descriptions.append(f"Stunde {start:02d} UTC")
            else:
                wrap_hint = " (über Mitternacht)" if start > end else ""
                filter_descriptions.append(f"Stunden {start:02d}–{end:02d} UTC{wrap_hint}")
        if not filter_descriptions:
            filter_descriptions.append("Keine Filter aktiv")

        def _focus_href(mode: str) -> str:
            updates = {"focus": mode}
            if mode != "user":
                updates["streamer"] = None
            return _build_url(**updates)

        focus_toggle_html = (
            '<div class="toggle-group">'
            f'  <a class="btn btn-small{" btn-active" if focus_mode == "time" else " btn-secondary"}" href="{_focus_href("time")}">Zeit</a>'
            f'  <a class="btn btn-small{" btn-active" if focus_mode == "weekday" else " btn-secondary"}" href="{_focus_href("weekday")}">Tag</a>'
            f'  <a class="btn btn-small{" btn-active" if focus_mode == "user" else " btn-secondary"}" href="{_focus_href("user")}">User</a>'
            '</div>'
        )

        current_view_label = "Alle Streamer" if show_all else "Top 10"
        toggle_label = "Alle Streamer zeigen" if not show_all else "Nur Top 10 anzeigen"
        toggle_href = _build_url(view="all" if not show_all else "top")

        clear_url = _build_url(
            min_samples=None,
            min_avg=None,
            partner="any",
            discord="any",
            hour=None,
            hour_from=None,
            hour_to=None,
        )

        discord_filter_field_html = ""
        if show_discord_private:
            discord_filter_options = [
                ("any", "Alle"),
                ("yes", "Nur Discord-Mitglieder"),
                ("no", "Ohne Discord"),
            ]
            discord_filter_html = "".join(
                f"<option value='{html.escape(value, quote=True)}'{' selected' if discord_filter == value else ''}>{html.escape(label)}</option>"
                for value, label in discord_filter_options
            )
            discord_filter_field_html = f"""
    <div>
      <label class="filter-label">
        Discord Filter
        <select name="discord">{discord_filter_html}</select>
      </label>
    </div>
"""

        if focus_mode == "time":
            analysis_content = f"{analysis_controls_html}{hour_section}"
        elif focus_mode == "weekday":
            analysis_content = f"{analysis_controls_html}{weekday_section}"
        else:
            analysis_content = user_section

        discord_header_html = '<th data-sort-type="number">Auf Discord?</th>' if show_discord_private else ""

        def _fmt_pct(value) -> str:
            try:
                return f"{float(value) * 100:.0f}%"
            except (TypeError, ValueError):
                return "-"

        def _fmt_int(value) -> str:
            try:
                return f"{int(value):,}".replace(",", ".")
            except (TypeError, ValueError):
                return "-"

        def _fmt_float(value, *, digits: int = 1) -> str:
            try:
                return f"{float(value):.{digits}f}"
            except (TypeError, ValueError):
                return "-"

        ret_sessions = int(retention_stats.get("sessions") or 0)
        retention_tiles = "".join(
            f"<div class='user-summary-item'><span class='label'>{html.escape(label)}</span><span class='value'>{value}</span></div>"
            for label, value in [
                ("5 Min", _fmt_pct(retention_stats.get("ret5"))),
                ("10 Min", _fmt_pct(retention_stats.get("ret10"))),
                ("20 Min", _fmt_pct(retention_stats.get("ret20"))),
                ("Avg Drop-Off", _fmt_pct(retention_stats.get("avg_drop"))),
            ]
        )
        drop_rows = retention_stats.get("examples") or []
        if drop_rows:
            drop_table_rows = "".join(
                "<tr>"
                f"<td>{html.escape(str(row.get('streamer', '')))}</td>"
                f"<td>{html.escape(str(row.get('started_at', '')))}</td>"
                f"<td data-sort-type='number' data-value='{row.get('dropoff_pct', 0)}'>{_fmt_pct(row.get('dropoff_pct'))}</td>"
                f"<td>{html.escape(str(row.get('label') or '-'))}</td>"
                "</tr>"
                for row in drop_rows
            )
        else:
            drop_table_rows = "<tr><td colspan=4><i>Keine markanten Drop-Offs.</i></td></tr>"

        retention_card = f"""
<div class="card" style="margin-top:1.2rem;">
  <div class="card-header">
    <h2>Retention & Drop-Off</h2>
    <div class="status-meta">Letzte 30 Tage • Sessions: {ret_sessions}</div>
  </div>
  <div class="user-summary">{retention_tiles}</div>
  <table class="sortable-table" style="margin-top:1rem;">
    <thead>
      <tr>
        <th data-sort-type="string">Streamer</th>
        <th data-sort-type="string">Start</th>
        <th data-sort-type="number">Größter Drop</th>
        <th data-sort-type="string">Hinweis</th>
      </tr>
    </thead>
    <tbody>{drop_table_rows}</tbody>
  </table>
</div>
"""

        disc = discovery_stats or {}
        disc_ret7 = disc.get("returning_7d") or {}
        disc_ret30 = disc.get("returning_30d") or {}
        discovery_tiles = "".join(
            f"<div class='user-summary-item'><span class='label'>{html.escape(label)}</span><span class='value'>{value}</span></div>"
            for label, value in [
                ("Avg Peak Viewer", _fmt_float(disc.get("unique_viewers_estimate"), digits=1)),
                ("Follower Δ (30d)", _fmt_int(disc.get("followers_total_delta"))),
                ("Avg Follower/Session", _fmt_float(disc.get("followers_per_session"), digits=2)),
                ("Avg Follower/Stunde", _fmt_float(disc.get("followers_per_hour"), digits=2)),
            ]
        )
        discovery_meta = (
            f"7d Returning: {_fmt_int(disc_ret7.get('returning'))}/{_fmt_int(disc_ret7.get('total'))} • "
            f"30d Returning: {_fmt_int(disc_ret30.get('returning'))}/{_fmt_int(disc_ret30.get('total'))}"
        )
        discovery_card = f"""
<div class="card" style="margin-top:1.0rem;">
  <div class="card-header">
    <h2>Discovery Funnel</h2>
    <div class="status-meta">{discovery_meta}</div>
  </div>
  <div class="user-summary">{discovery_tiles}</div>
</div>
"""

        chat = chat_stats or {}
        chat_tiles = "".join(
            f"<div class='user-summary-item'><span class='label'>{html.escape(label)}</span><span class='value'>{value}</span></div>"
            for label, value in [
                ("Unique Chat/100 Viewer", _fmt_float(chat.get("unique_per_100"), digits=1)),
                ("First-Time Anteil", _fmt_pct(chat.get("first_share"))),
                ("Returning Anteil", _fmt_pct(chat.get("returning_share"))),
                ("Total Unique (30d)", _fmt_int(chat.get("total_unique"))),
            ]
        )
        peak_rows = chat.get("peaks") or []
        if peak_rows:
            peak_html = "".join(
                "<li>"
                f"<strong>{html.escape(str(row.get('streamer', '')))}</strong> – "
                f"{html.escape(str(row.get('minute', '')))} "
                f"({html.escape(str(row.get('messages', 0)))} Messages)"
                "</li>"
                for row in peak_rows
            )
            peak_list = f"<ul class='status-meta' style='margin:.6rem 0 0 .2rem; padding-left:1rem;'>{peak_html}</ul>"
        else:
            peak_list = "<div class='status-meta' style='margin-top:.4rem;'>Keine Chat-Peaks in den letzten 30 Tagen.</div>"

        chat_card = f"""
<div class="card" style="margin-top:1.0rem;">
  <div class="card-header">
    <h2>Chat-Gesundheit</h2>
    <div class="status-meta">Engagement pro Session</div>
  </div>
  <div class="user-summary">{chat_tiles}</div>
  {peak_list}
</div>
"""

        content_perf = stats.get("content_performance") or []
        if content_perf:
            perf_rows = "".join(
                "<tr>"
                f"<td>{html.escape(str(row.get('streamer', '')))}</td>"
                f"<td><div style='font-weight:bold;'>{html.escape(str(row.get('title', '')))}</div><div style='font-size:0.85em; color:var(--muted);'>{html.escape(str(row.get('notification', '')))}</div></td>"
                f"<td data-sort-type='number' data-value='{row.get('peak_viewers', 0)}'>{row.get('peak_viewers')}</td>"
                f"<td data-sort-type='number' data-value='{row.get('engagement_ratio', 0) or 0}'>{_fmt_pct((row.get('engagement_ratio') or 0)/100.0)}</td>"
                "</tr>"
                for row in content_perf
            )
            content_card = f"""
<div class="card" style="margin-top:1.0rem;">
  <div class="card-header">
    <h2>Content Performance (Top 20 Sessions)</h2>
    <div class="status-meta">Titel & Benachrichtigungen vs. Peak Viewer & Engagement (Peak/Follower)</div>
  </div>
  <table class="sortable-table" style="margin-top:1rem;">
    <thead>
      <tr>
        <th data-sort-type="string">Streamer</th>
        <th data-sort-type="string">Titel / Notification</th>
        <th data-sort-type="number">Peak</th>
        <th data-sort-type="number">Eng.%</th>
      </tr>
    </thead>
    <tbody>{perf_rows}</tbody>
  </table>
</div>
"""
        else:
            content_card = ""

        insights_html = f"{retention_card}{discovery_card}{content_card}{chat_card}"

        body = f"""
<h1 style="margin:.2rem 0 1rem 0;">Twitch Stats</h1>

<div class="card">
  <form method="get" class="row" style="gap:1rem; flex-wrap:wrap; align-items:flex-end;">
    <div>
      <label class="filter-label">
        Min. Stichproben
        <input type="number" name="min_samples" min="0" value="{html.escape(str(min_samples) if min_samples is not None else '', quote=True)}">
      </label>
    </div>
    <div>
      <label class="filter-label">
        Min. Ø Viewer
        <input type="number" step="0.1" name="min_avg" min="0" value="{html.escape(str(min_avg) if min_avg is not None else '', quote=True)}">
      </label>
    </div>
    <div>
      <label class="filter-label">
        Partner Filter
        <select name="partner">
          <option value="any"{' selected' if partner_filter == 'any' else ''}>Alle</option>
          <option value="only"{' selected' if partner_filter == 'only' else ''}>Nur Partner</option>
          <option value="exclude"{' selected' if partner_filter == 'exclude' else ''}>Ohne Partner</option>
        </select>
      </label>
    </div>
    {discord_filter_field_html}
    <div>
      <label class="filter-label">
        Einzelne Stunde (UTC)
        <input type="number" name="hour" min="0" max="23" value="{html.escape(str(stats_hour) if stats_hour is not None else '', quote=True)}">
      </label>
    </div>
    <div>
      <label class="filter-label">
        Stundenbereich (UTC)
        <div class="row" style="gap:.6rem;">
          <input type="number" name="hour_from" min="0" max="23" placeholder="von" value="{html.escape(str(hour_from) if hour_from is not None else '', quote=True)}">
          <input type="number" name="hour_to" min="0" max="23" placeholder="bis" value="{html.escape(str(hour_to) if hour_to is not None else '', quote=True)}">
        </div>
      </label>
    </div>
    <div style="display:flex; gap:.6rem;">
      <button class="btn">Anwenden</button>
      <a class="btn btn-secondary" href="{html.escape(clear_url)}">Reset</a>
    </div>
  </form>
  <div class="status-meta" style="margin-top:.4rem;">Hinweis: Stundenangaben beziehen sich auf UTC.</div>
  <div class="status-meta" style="margin-top:.8rem;">Aktive Filter: {' • '.join(filter_descriptions)}</div>
</div>

{insights_html}

<div class="card" style="margin-top:1.2rem;">
  <div class="card-header">
    <h2>Analyse</h2>
    {focus_toggle_html}
  </div>
  {analysis_content}
</div>

<div class="card" style="margin-top:1.2rem;">
  <div class="card-header">
    <h2>Top Partner Streamer (Tracked)</h2>
    <div class="row" style="gap:.6rem; align-items:center;">
      <div style="color:var(--muted); font-size:.9rem;">Ansicht: {current_view_label}</div>
      <a class="btn" href="{html.escape(toggle_href)}">{toggle_label}</a>
    </div>
  </div>
  <table class="sortable-table" data-table="tracked">
    <thead>
      <tr>
        <th data-sort-type="string">Streamer</th>
        <th data-sort-type="number">Stichproben</th>
        <th data-sort-type="number">Ø Viewer</th>
        <th data-sort-type="number">Peak Viewer</th>
        <th data-sort-type="number">Partner</th>
        {discord_header_html}
      </tr>
    </thead>
    <tbody>{render_table(tracked_items)}</tbody>
  </table>
</div>

<div class="card" style="margin-top:1.2rem;">
  <div class="card-header">
    <h2>Top Deadlock Streamer (Kategorie gesamt)</h2>
    <div style="color:var(--muted); font-size:.9rem;">Ansicht: {current_view_label}</div>
  </div>
  <table class="sortable-table" data-table="category">
    <thead>
      <tr>
        <th data-sort-type="string">Streamer</th>
        <th data-sort-type="number">Stichproben</th>
        <th data-sort-type="number">Ø Viewer</th>
        <th data-sort-type="number">Peak Viewer</th>
        <th data-sort-type="number">Partner</th>
        {discord_header_html}
      </tr>
    </thead>
    <tbody>{render_table(category_items)}</tbody>
  </table>
</div>
{script}
"""

        nav_html: Optional[str] = None
        if not show_discord_private:
            nav_html = '<nav class="tabs"><span class="tab active">Stats</span></nav>'
        if partner_view:
            nav_html = '<nav class="tabs"><span class="tab active">Stats</span></nav>'

        return web.Response(text=self._html(body, active="stats", nav=nav_html), content_type="text/html")

    async def stats(self, request: web.Request):
        self._require_token(request)
        return await self._render_stats_page(request, partner_view=False)

    async def partner_stats(self, request: web.Request):
        self._require_partner_token(request)
        return await self._render_stats_page(request, partner_view=True)


__all__ = ["DashboardStatsMixin"]
