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

        stats = await self._stats(hour_from=hour_from, hour_to=hour_to)
        tracked = stats.get("tracked", {}) or {}
        category = stats.get("category", {}) or {}

        min_samples = _parse_int("min_samples", "samples")
        min_avg = _parse_float("min_avg", "avg")
        partner_filter = (request.query.get("partner") or "any").lower()
        if partner_filter not in {"only", "exclude", "any"}:
            partner_filter = "any"

        base_path = request.rel_url.path

        preserved_params = {}
        for key in ("view", "display", "min_samples", "min_avg", "partner", "hour", "hour_from", "hour_to"):
            if key in request.query:
                preserved_params[key] = request.query[key]

        def _build_url(**updates) -> str:
            params = {**preserved_params, **updates}
            merged = {k: v for k, v in params.items() if v not in {None, ""}}
            query = "&".join(f"{k}={html.escape(str(v), quote=True)}" for k, v in merged.items())
            if query:
                return f"{base_path}?{query}"
            return base_path

        tracked_items = tracked.get("top", []) if show_all else tracked.get("top_partner", [])
        category_items = category.get("top", [])

        if min_samples is not None:
            tracked_items = [item for item in tracked_items if int(item.get("samples") or 0) >= min_samples]
            category_items = [item for item in category_items if int(item.get("samples") or 0) >= min_samples]
        if min_avg is not None:
            tracked_items = [item for item in tracked_items if float(item.get("avg_viewers") or 0.0) >= min_avg]
            category_items = [item for item in category_items if float(item.get("avg_viewers") or 0.0) >= min_avg]

        if partner_filter == "only":
            tracked_items = [item for item in tracked_items if bool(item.get("is_partner"))]
            category_items = [item for item in category_items if bool(item.get("is_partner"))]
        elif partner_filter == "exclude":
            tracked_items = [item for item in tracked_items if not bool(item.get("is_partner"))]
            category_items = [item for item in category_items if not bool(item.get("is_partner"))]

        tracked_items = tracked_items or []
        category_items = category_items or []

        def render_table(items: List[dict]) -> str:
            if not items:
                return "<tr><td colspan=5><i>Keine Daten für die aktuellen Filter.</i></td></tr>"
            rows = []
            for item in items:
                streamer = html.escape(str(item.get("streamer", "")))
                samples = int(item.get("samples") or 0)
                avg_viewers = float(item.get("avg_viewers") or 0.0)
                max_viewers = int(item.get("max_viewers") or 0)
                is_partner = bool(item.get("is_partner"))
                partner_text = "Ja" if is_partner else "Nein"
                partner_value = "1" if is_partner else "0"
                rows.append(
                    "<tr>"
                    f"<td>{streamer}</td>"
                    f"<td data-value=\"{samples}\">{samples}</td>"
                    f"<td data-value=\"{avg_viewers:.4f}\">{avg_viewers:.1f}</td>"
                    f"<td data-value=\"{max_viewers}\">{max_viewers}</td>"
                    f"<td data-value=\"{partner_value}\">{partner_text}</td>"
                    "</tr>"
                )
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
            '          <th data-sort-type="number">Samples</th>'
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
            '          <th data-sort-type="number">Samples</th>'
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
            '          <th data-sort-type="number">Samples</th>'
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
            '          <th data-sort-type="number">Samples</th>'
            '          <th data-sort-type="number">Ø Viewer</th>'
            '          <th data-sort-type="number">Peak Viewer</th>'
            '        </tr>'
            '      </thead>'
            f'      <tbody>{tracked_weekday_rows}</tbody>'
            '    </table>'
            '  </div>'
            '</div>'
        )

        if display_mode == "charts":
            hour_section = hour_chart_block
            weekday_section = weekday_chart_block
        else:
            hour_section = hour_tables_block
            weekday_section = weekday_tables_block

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
            filter_descriptions.append(f"Samples ≥ {min_samples}")
        if min_avg is not None:
            filter_descriptions.append(f"Ø Viewer ≥ {min_avg:.1f}")
        if partner_filter == "only":
            filter_descriptions.append("Nur Partner")
        elif partner_filter == "exclude":
            filter_descriptions.append("Ohne Partner")
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

        display_toggle_html = (
            '<div class="toggle-group">'
            f'  <a class="btn btn-small{" btn-active" if display_mode == "charts" else " btn-secondary"}" href="{_build_url(display="charts")}">Charts</a>'
            f'  <a class="btn btn-small{" btn-active" if display_mode == "raw" else " btn-secondary"}" href="{_build_url(display="raw")}">Tabelle</a>'
            '</div>'
        )

        current_view_label = "Alle Streamer" if show_all else "Partner-Streamer"
        toggle_label = "Alle Streamer zeigen" if not show_all else "Nur Partner zeigen"
        toggle_href = _build_url(view="all" if not show_all else "top")

        clear_url = _build_url(
            min_samples=None,
            min_avg=None,
            partner="any",
            hour=None,
            hour_from=None,
            hour_to=None,
        )

        body = f"""
<h1 style="margin:.2rem 0 1rem 0;">Twitch Stats</h1>

<div class="card">
  <form method="get" class="row" style="gap:1rem; flex-wrap:wrap; align-items:flex-end;">
    <div>
      <label class="filter-label">
        Min. Samples
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

<div class="card" style="margin-top:1.2rem;">
  <div class="card-header">
    <h2>Zeitliche Trends (UTC)</h2>
    {display_toggle_html}
  </div>
  {hour_section}
</div>

<div class="card" style="margin-top:1.2rem;">
  <h2>Tagestrends</h2>
  {weekday_section}
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
        <th data-sort-type="number">Samples</th>
        <th data-sort-type="number">Ø Viewer</th>
        <th data-sort-type="number">Peak Viewer</th>
        <th data-sort-type="number">Partner</th>
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
        <th data-sort-type="number">Samples</th>
        <th data-sort-type="number">Ø Viewer</th>
        <th data-sort-type="number">Peak Viewer</th>
        <th data-sort-type="number">Partner</th>
      </tr>
    </thead>
    <tbody>{render_table(category_items)}</tbody>
  </table>
</div>
{script}
"""

        nav_html = None
        if partner_view:
            nav_html = "<nav class=\"tabs\"><span class=\"tab active\">Stats</span></nav>"

        return web.Response(text=self._html(body, active="stats", nav=nav_html), content_type="text/html")

    async def stats(self, request: web.Request):
        self._require_token(request)
        return await self._render_stats_page(request, partner_view=False)

    async def partner_stats(self, request: web.Request):
        self._require_partner_token(request)
        return await self._render_stats_page(request, partner_view=True)


__all__ = ["DashboardStatsMixin"]
