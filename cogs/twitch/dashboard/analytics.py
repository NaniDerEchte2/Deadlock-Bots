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

        parts: List[str] = []
        parts.append(
            """
<div id="analytics-root"></div>
<script id="analytics-config" type="application/json">__CONFIG__</script>

<style>
  @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@500;600;700&family=Space+Grotesk:wght@500;600&display=swap');
  :root {
    --bg: #070a12;
    --card: #0f1526;
    --panel: #0c1220;
    --border: #1f2942;
    --text: #e7ecf3;
    --muted: #9aa7bd;
    --accent: #53b0f8;
    --accent-2: #7df1c5;
    --warn: #f0c674;
    --danger: #f87171;
    --ok: #22d3ee;
  }
"""
        )
        parts.append(
            """
  body { background: var(--bg); }
  #analytics-root { font-family: 'Manrope', 'Space Grotesk', system-ui, sans-serif; color: var(--text); }
  .shell {
    background: radial-gradient(circle at 20% 20%, rgba(83,176,248,0.12), transparent 32%),
                radial-gradient(circle at 80% 0%, rgba(125,241,197,0.1), transparent 28%),
                linear-gradient(145deg, #070a12 0%, #0b1020 50%, #0a0f1c 100%);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 1.4rem;
    box-shadow: 0 22px 46px rgba(0,0,0,0.45);
  }
  .grid { display:grid; gap:1.2rem; }
  .kpi-grid { display:grid; gap:1.2rem; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); }
  .kpi { background:var(--panel); border:1px solid var(--border); border-radius:14px; padding:0.9rem 1rem; position:relative; overflow:hidden; }
  .kpi::after { content:''; position:absolute; inset:0; background:linear-gradient(145deg, rgba(83,176,248,0.06), rgba(125,241,197,0.05)); opacity:0.6; pointer-events:none; }
  .kpi h4 { margin:0 0 .25rem 0; font-size:0.95rem; color:var(--muted); letter-spacing:0.01em; }
  .kpi .value { font-size:1.75rem; font-weight:800; letter-spacing:0.01em; }
  .kpi .sub { color:var(--muted); font-size:0.85rem; margin-top:0.2rem; }
  .pill { display:inline-flex; align-items:center; gap:0.4rem; padding:0.55rem 0.85rem; border-radius:12px; border:1px solid var(--border); background:#0d1427; color:var(--text); font-weight:700; letter-spacing:0.01em; }
  .pill.primary { background:linear-gradient(120deg, #53b0f8, #7df1c5); color:#04101b; border:none; }
  .pill.ghost { background:transparent; }
  .panel { background:var(--panel); border:1px solid var(--border); border-radius:16px; padding:1.2rem 1.4rem; }
  .panel-header { display:flex; justify-content:space-between; align-items:flex-start; gap:1.2rem; margin-bottom:1rem; }
  .panel-header .meta { display:flex; flex-direction:column; gap:0.2rem; }
  .eyebrow { text-transform:uppercase; letter-spacing:0.09em; font-size:0.75rem; color:var(--muted); margin:0; }
  .title { margin:0; font-size:1.18rem; letter-spacing:0.01em; }
  .status { padding:0.75rem 0.9rem; border-radius:12px; border:1px solid var(--border); background:#0b1222; display:flex; align-items:center; gap:0.6rem; font-weight:700; }
  .status.ok { border-color:rgba(125,241,197,0.6); color:#c9ffe9; background:rgba(125,241,197,0.08); }
  .status.warn { border-color:rgba(240,198,116,0.6); color:#f5d9a3; background:rgba(240,198,116,0.1); }
  .status.err { border-color:rgba(248,113,113,0.6); color:#ffd7d7; background:rgba(248,113,113,0.1); }
  .badge { padding:0.2rem 0.55rem; border-radius:999px; font-size:0.78rem; font-weight:800; text-transform:uppercase; letter-spacing:0.05em; border:1px solid var(--border); color:var(--muted); }
  .badge.ok { color:#8ff2cd; border-color:rgba(125,241,197,0.5); }
  .badge.warn { color:#f0c674; border-color:rgba(240,198,116,0.4); }
  .badge.err { color:#f87171; border-color:rgba(248,113,113,0.5); }
  .controls { display:flex; flex-wrap:wrap; gap:0.9rem; align-items:flex-end; }
  .field { display:flex; flex-direction:column; gap:0.3rem; font-size:0.9rem; color:var(--muted); }
  .field input, .field select { background:#0a1020; border:1px solid var(--border); color:var(--text); padding:0.55rem 0.65rem; border-radius:10px; min-width:11rem; }
  .stack { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:1.4rem; }
  .insights { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:1rem; }
  .insight { background:#0a101d; border:1px solid var(--border); border-radius:12px; padding:0.75rem 0.85rem; }
  .insight h5 { margin:0 0 .2rem 0; font-size:1rem; }
  .insight p { margin:0; color:var(--muted); font-size:0.9rem; line-height:1.4; }
  .heatmap { display:grid; grid-template-columns:repeat(25, minmax(16px,1fr)); gap:2px; }
  .heatmap .label { grid-column:span 1; text-align:right; font-size:0.7rem; color:var(--muted); padding-right:4px; }
  .heatmap .cell { height:18px; border-radius:4px; }
  table { width:100%; border-collapse:collapse; color:var(--text); }
  th, td { padding:0.55rem 0.45rem; border-bottom:1px solid var(--border); text-align:left; }
  th { color:var(--muted); font-size:0.85rem; letter-spacing:0.01em; }
  tr:hover td { background:rgba(83,176,248,0.05); }
  .scorecard { display:flex; flex-direction:column; gap:0.25rem; }
  .scorecard strong { font-size:1.4rem; }
  .row { display:flex; flex-wrap:wrap; gap:0.8rem; align-items:center; }
  .tiny { font-size:0.82rem; color:var(--muted); }
  .chip { background:rgba(83,176,248,0.08); color:#c9e9ff; border:1px solid rgba(83,176,248,0.35); border-radius:999px; padding:0.25rem 0.6rem; font-weight:700; font-size:0.8rem; }
  .seg-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:1rem; }
  .seg-card { border:1px dashed var(--border); border-radius:10px; padding:0.7rem 0.8rem; background:#0b111f; }
  .seg-card h6 { margin:0; font-size:0.95rem; }
  .seg-card .value { font-size:1.2rem; font-weight:800; }
  .divider { height:1px; background:linear-gradient(90deg, transparent, rgba(255,255,255,0.12), transparent); margin:0.6rem 0; }
</style>
<script src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
<script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
<script>
// analytics bundle (precompiled)
const {
  useEffect,
  useMemo,
  useState
} = React;
const dayLabels = ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag'];
const fmtNumber = (val, digits = 0) => {
  const num = Number(val);
  if (!Number.isFinite(num)) return '-';
  return num.toLocaleString('de-DE', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits
  });
};
const fmtPercent = (val, digits = 1) => {
  const num = Number(val);
  if (!Number.isFinite(num)) return '-';
  return `${num.toFixed(digits)}%`;
};
const fmtDuration = seconds => {
  const totalMinutes = Math.floor((seconds || 0) / 60);
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
};
const deltaBadge = (current, prev, opts) => {
  if (prev === undefined || prev === null) return null;
  const diff = Number(current || 0) - Number(prev || 0);
  if (!Number.isFinite(diff) || diff === 0) return null;
  const digits = opts?.digits ?? 1;
  const isPercent = opts?.percent;
  const formatted = isPercent ? fmtPercent(Math.abs(diff), digits) : fmtNumber(Math.abs(diff), digits);
  const cls = diff > 0 ? 'badge ok' : 'badge warn';
  const arrow = diff > 0 ? '▲' : '▼';
  return /*#__PURE__*/React.createElement("span", {
    className: cls,
    style: {
      marginLeft: '0.35rem'
    }
  }, arrow, " ", formatted);
};
const ChartBox = ({
  config,
  className
}) => {
  const ref = React.useRef(null);
  const chartRef = React.useRef(null);
  const signature = JSON.stringify(config);
  useEffect(() => {
    if (!ref.current || typeof Chart === 'undefined') return;
    if (chartRef.current) chartRef.current.destroy?.();
    chartRef.current = new Chart(ref.current, config);
    return () => chartRef.current?.destroy?.();
  }, [signature]);
  return /*#__PURE__*/React.createElement("canvas", {
    ref: ref,
    className: className
  });
};
const Heatmap = ({
  data
}) => {
  if (!data || Object.keys(data).length === 0) return /*#__PURE__*/React.createElement("div", {
    className: "tiny"
  }, "Keine Heatmap-Daten.");
  const hours = Array.from({
    length: 24
  }, (_, i) => i);
  const values = Object.values(data).flatMap(row => Object.values(row || {}).map(Number));
  const maxVal = Math.max(...values, 1);
  return /*#__PURE__*/React.createElement("div", {
    className: "heatmap"
  }, "       ", [1, 2, 3, 4, 5, 6, 0].map(d => {
    const row = data?.[d] || {};
    return /*#__PURE__*/React.createElement(React.Fragment, {
      key: d
    }, "             ", /*#__PURE__*/React.createElement("div", {
      className: "label"
    }, dayLabels[d].slice(0, 2)), "             ", hours.map(h => {
      const val = Number(row?.[h] || 0);
      const strength = Math.min(1, val / (maxVal || 1));
      const color = `rgba(83,176,248,${0.08 + strength * 0.65})`;
      return /*#__PURE__*/React.createElement("div", {
        key: `${d}-${h}`,
        className: "cell",
        title: `${dayLabels[d]} ${String(h).padStart(2, '0')}:00 | ${fmtNumber(val, 0)}`,
        style: {
          background: color
        }
      });
    }), "           ");
  }), "     ");
};
const deriveInsights = payload => {
  const insights = [];
  const retention = payload.retention || {};
  const chat = payload.chat || {};
  const discovery = payload.discovery || {};
  const audience = payload.audience || {};
  const sessions = payload.sessions || [];
  const avgDuration = sessions.length ? sessions.reduce((a, s) => a + (s.duration || 0), 0) / sessions.length : 0;
  if (retention.avg10m && retention.avg10m < 60) {
    insights.push({
      tone: 'warn',
      title: 'Hook optimieren',
      desc: '10-Minuten-Retention unter 60%. Hook/Intro straffen und klaren Programmstart in Minute 1-3 setzen.'
    });
  }
  if (retention.avgDropoff && retention.avgDropoff > 30) {
    insights.push({
      tone: 'warn',
      title: 'Drop-Off reduzieren',
      desc: 'Peak-Verlust >30%. Pausen kürzen, Interaktion an Drop-Minuten planen, Reminder platzieren.'
    });
  }
  if (chat.uniquePer100 && chat.uniquePer100 < 8) {
    insights.push({
      tone: 'warn',
      title: 'Chat-Engagement niedrig',
      desc: 'Unter 8 aktive Chatter pro 100 Viewer. Alle 10-15 Minuten Fragen/Polls einbauen.'
    });
  }
  if (chat.returningShare && chat.returningShare < 35) {
    insights.push({
      tone: 'warn',
      title: 'Rückkehrrate ausbauen',
      desc: 'Returning-Quote <35%. Wiederkehrende Namen callouten, Serienformate mit Cliffhangers planen.'
    });
  }
  if (discovery.followersPerHour && discovery.followersPerHour < 5) {
    insights.push({
      tone: 'warn',
      title: 'Follower-Conversion schwach',
      desc: 'Unter 5 Follower/Std. CTA bei Peaks, klare Value Proposition und sichtbare Ziele.'
    });
  }
  if (audience.watchTimePerViewerMin && audience.watchTimePerViewerMin < 20) {
    insights.push({
      tone: 'warn',
      title: 'Watch Time ausbauen',
      desc: 'Avg Watch Time <20 Min. Straffere Segmente, weniger Leerlauf, Cliffhanger nach 10-15 Minuten.'
    });
  }
  if (audience.returningRate7d && audience.returningRate7d < 30) {
    insights.push({
      tone: 'warn',
      title: 'Rückkehrrate niedrig',
      desc: '<30% 7d Returning. Serienformate, feste Slots und aktive Begrüßungen testen.'
    });
  }
  if (avgDuration > 5 * 3600) {
    insights.push({
      tone: 'warn',
      title: 'Streams sehr lang',
      desc: '>5h durchschnittlich. Nachlassende Retention prüfen und ggf. kompaktere Blöcke testen.'
    });
  } else if (avgDuration && avgDuration < 90 * 60) {
    insights.push({
      tone: 'info',
      title: 'Kurz-Streams',
      desc: '<90 Minuten. Mehr Zeit für Discovery einkalkulieren oder Slots buendeln.'
    });
  }
  if (!insights.length) {
    insights.push({
      tone: 'ok',
      title: 'Stabiles Fundament',
      desc: 'Kern-KPIs solide. Jetzt gezielt Zeit-Slots und Kategorien A/B testen.'
    });
  }
  return insights;
};
const fetchAnalytics = async (streamer, days, partnerToken) => {
  const params = new URLSearchParams();
  params.set('days', String(days));
  if (streamer) params.set('streamer', streamer);
  if (partnerToken) params.set('partner_token', partnerToken);
  const resp = await fetch(`/twitch/api/analytics?${params}`);
  const json = await resp.json();
  if (!resp.ok) throw new Error(json?.error || `HTTP ${resp.status}`);
  return json;
};
const bucketSessions = sessions => {
  const buckets = [{
    key: 'short',
    label: 'Kurz <2h',
    filter: s => s.duration < 7200
  }, {
    key: 'mid',
    label: 'Mittel 2-4h',
    filter: s => s.duration >= 7200 && s.duration < 14400
  }, {
    key: 'long',
    label: 'Lang >4h',
    filter: s => s.duration >= 14400
  }];
  return buckets.map(bucket => {
    const list = sessions.filter(bucket.filter);
    const avg = arr => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : 0;
    return {
      key: bucket.key,
      label: bucket.label,
      count: list.length,
      avgViewers: avg(list.map(s => s.avgViewers)),
      peak: avg(list.map(s => s.peakViewers)),
      retention10: avg(list.map(s => s.retention10m)),
      followers: avg(list.map(s => s.followerDelta || 0))
    };
  });
};
const App = ({
  config
}) => {
  const [streamer, setStreamer] = useState(config.streamer || '');
  const [days, setDays] = useState(config.days || 30);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [lastUpdated, setLastUpdated] = useState('');
  const [weekdayFilter, setWeekdayFilter] = useState('all');
  const [slotFilter, setSlotFilter] = useState('all');
  const [categoryFilter, setCategoryFilter] = useState('all');
  const [includeNonPartners, setIncludeNonPartners] = useState(Boolean(config.includeNonPartners && config.hasExtraOptions));
  const optionsHtml = useMemo(() => {
    const base = config.streamerOptions || '';
    const extra = includeNonPartners ? (config.extraStreamerOptions || '') : '';
    return [base, extra].filter(Boolean).join('\\n');
  }, [config.streamerOptions, config.extraStreamerOptions, includeNonPartners]);
  const partnerToken = useMemo(() => {
    const fromCfg = config.partnerToken || '';
    const urlToken = new URLSearchParams(window.location.search).get('partner_token') || '';
    return fromCfg || urlToken;
  }, [config]);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    fetchAnalytics(streamer, days, partnerToken).then(payload => {
      if (!active) return;
      if (!payload || payload.empty || (payload.sessions || []).length === 0) {
        setError('Keine Sessions im gewählten Zeitraum.');
        setData(null);
      } else {
        setData(payload);
        setLastUpdated(new Date().toLocaleString('de-DE'));
      }
    }).catch(err => {
      if (active) setError(err?.message || 'Fehler beim Laden');
      setData(null);
    }).finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [streamer, days, partnerToken]);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (streamer) params.set('streamer', streamer); else params.delete('streamer');
    if (days) params.set('days', String(days)); else params.delete('days');
    if (includeNonPartners) params.set('include_non_partners', '1'); else params.delete('include_non_partners');
    const existingToken = new URLSearchParams(window.location.search).get('partner_token') || '';
    if (partnerToken || existingToken) {
      params.set('partner_token', partnerToken || existingToken);
    }
    const qs = params.toString();
    const newUrl = `${window.location.pathname}${qs ? `?${qs}` : ''}`;
    window.history.replaceState({}, '', newUrl);
  }, [streamer, days, includeNonPartners, partnerToken]);
  const sessions = data?.sessions || [];
  const retention = data?.retention || {};
  const chat = data?.chat || {};
  const audience = data?.audience || {};
  const discovery = data?.discovery || {};
  const summary = data?.summary || {};
  const benchmark = data?.benchmark || {};
  const analysis = data?.analysis || {};
  const timing = data?.timing || {};
  const categories = data?.categories || {};
  const compare = data?.compare || {};
  const comparePrev = compare?.prev || {};
  const gameOptions = useMemo(() => {
    const set = new Set();
    (sessions || []).forEach(s => {
      const name = (s.game || '').trim();
      if (name) set.add(name);
    });
    return Array.from(set).sort();
  }, [sessions]);
  const visibleSessions = useMemo(() => {
    const slotCheck = {
      morning: h => h >= 6 && h < 12,
      afternoon: h => h >= 12 && h < 17,
      evening: h => h >= 17 && h < 23,
      night: h => h >= 23 || h < 6
    };
    return sessions.filter(s => {
      if (weekdayFilter !== 'all' && s.weekday !== undefined && s.weekday !== null && Number(weekdayFilter) !== Number(s.weekday)) return false;
      if (slotFilter !== 'all' && s.hour !== undefined && s.hour !== null) {
        const fn = slotCheck[slotFilter];
        if (fn && !fn(Number(s.hour))) return false;
      }
      if (categoryFilter !== 'all') {
        const gameName = (s.game || '').toLowerCase();
        if (gameName !== categoryFilter.toLowerCase()) return false;
      }
      return true;
    });
  }, [sessions, weekdayFilter, slotFilter, categoryFilter]);
  const scopedSessions = visibleSessions.length ? visibleSessions : sessions;
  const derived = useMemo(() => {
    const avgAvgViewers = scopedSessions.length ? scopedSessions.reduce((a, s) => a + (s.avgViewers || 0), 0) / scopedSessions.length : 0;
    const avgDuration = scopedSessions.length ? scopedSessions.reduce((a, s) => a + (s.duration || 0), 0) / scopedSessions.length : 0;
    const perWeek = days ? scopedSessions.length / days * 7 : 0;
    const engagementScore = Math.min(1, (chat.uniquePer100 || 0) / 20);
    const viewerBenchmark = benchmark?.categoryQuantiles?.q75 || benchmark?.categoryQuantiles?.q50 || avgAvgViewers || 1;
    const viewerScore = Math.min(1, avgAvgViewers / Math.max(viewerBenchmark, 1));
    const retentionScore = Math.min(1, (retention.avg10m || 0) / 100);
    const streamScore = Math.round((retentionScore * 0.5 + viewerScore * 0.3 + engagementScore * 0.2) * 100);
    const uniqueAudience = audience.uniqueEstimateAvg || (chat.totalUnique30d || 0) / Math.max(sessions.length || 1, 1);
    return {
      avgAvgViewers,
      avgDuration,
      perWeek,
      engagementScore,
      viewerScore,
      retentionScore,
      streamScore,
      uniqueAudience
    };
  }, [scopedSessions, sessions, days, chat, benchmark, retention, audience]);
  const insightList = useMemo(() => deriveInsights(data || {}), [data]);
  const segments = useMemo(() => bucketSessions(scopedSessions), [scopedSessions]);
  const viewerSeries = useMemo(() => {
    const list = scopedSessions.slice(0, 32).reverse();
    let cumulative = 0;
    return list.map(item => {
      const delta = Number(item.followerDelta || 0);
      cumulative += delta;
      return {
        label: item.date || '',
        peak: Number(item.peakViewers || 0),
        avg: Number(item.avgViewers || 0),
        delta,
        cumulative
      };
    });
  }, [scopedSessions]);
  const radarData = useMemo(() => {
    const norm = (val, target) => Math.max(0, Math.min(100, target > 0 ? val / target * 100 : 0));
    const durHours = (derived.avgDuration || 0) / 3600;
    const concurrencyTarget = benchmark?.categoryQuantiles?.q75 || benchmark?.categoryQuantiles?.q50 || derived.avgAvgViewers || 1;
    return {
      labels: ['Retention', 'Concurrency', 'Engagement', 'Follower/h', 'Session Länge'],
      values: [norm(retention.avg10m || 0, 100), norm(derived.avgAvgViewers || 0, concurrencyTarget || 1), norm(chat.uniquePer100 || 0, 25), norm(discovery.followersPerHour || 0, 10), norm(durHours, 4)]
    };
  }, [retention, derived, chat, discovery, benchmark]);
  return /*#__PURE__*/React.createElement("div", {
    className: "shell grid"
  }, "       ", /*#__PURE__*/React.createElement("div", {
    className: "panel"
  }, "         ", /*#__PURE__*/React.createElement("div", {
    className: "panel-header"
  }, "           ", /*#__PURE__*/React.createElement("div", {
    className: "meta"
  }, "             ", /*#__PURE__*/React.createElement("p", {
    className: "eyebrow"
  }, "Deadlock Twitch Intelligence"), "             ", /*#__PURE__*/React.createElement("h2", {
    className: "title"
  }, "Analytics Dashboard \xD4\xC7\xF4 Wachstum nach KPIs"), "             ", /*#__PURE__*/React.createElement("div", {
    className: "row tiny"
  }, "               ", /*#__PURE__*/React.createElement("span", {
    className: "chip"
  }, "Retention-first"), "               ", /*#__PURE__*/React.createElement("span", {
    className: "chip"
  }, "Engagement gewichtet"), "               ", /*#__PURE__*/React.createElement("span", {
    className: "chip"
  }, "Sessions & Timing"), "             "), "           "), "           ", /*#__PURE__*/React.createElement("div", {
    className: "scorecard"
  }, "             ", /*#__PURE__*/React.createElement("span", {
    className: "tiny"
  }, "Stream Score (50% Retention / 30% Viewers / 20% Engagement)"), "             ", /*#__PURE__*/React.createElement("strong", null, derived.streamScore, "%"), "             ", /*#__PURE__*/React.createElement("span", {
    className: "tiny"
  }, "Letztes Update: ", lastUpdated || '–'), "           "), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    className: "controls"
  }, "           ", /*#__PURE__*/React.createElement("label", {
    className: "field"
  }, "             ", /*#__PURE__*/React.createElement("span", null, "Streamer"), "             ", /*#__PURE__*/React.createElement("input", {
    list: "streamerOptions",
    defaultValue: streamer,
    placeholder: "Login oder leer für alle",
    onChange: e => setStreamer(e.target.value.trim())
  }), "             ", /*#__PURE__*/React.createElement("datalist", {
    id: "streamerOptions",
    dangerouslySetInnerHTML: {
      __html: optionsHtml || ''
    }
  }), "           "), "           ", /*#__PURE__*/React.createElement("label", {
    className: "field"
  }, "             ", /*#__PURE__*/React.createElement("span", null, "Vorschl\u00e4ge"), "             ", /*#__PURE__*/React.createElement("div", {
    className: "row",
    style: {
      alignItems: 'center',
      gap: '0.45rem'
    }
  }, "               ", /*#__PURE__*/React.createElement("input", {
    type: "checkbox",
    checked: includeNonPartners && (config.hasExtraOptions || (config.extraStreamerOptions || '').length > 0),
    onChange: e => setIncludeNonPartners(e.target.checked)
  }), "               ", /*#__PURE__*/React.createElement("span", {
    className: "tiny"
  }, "Auch Nicht-Partner anzeigen"), "             "), "             ", /*#__PURE__*/React.createElement("span", {
    className: "tiny",
    style: {
      color: 'var(--muted)'
    }
  }, config.hasExtraOptions ? 'Inklusive Streams aus Deadlock-Kategorie' : 'Keine externen Vorschl\u00e4ge gefunden'), "           "), "           ", /*#__PURE__*/React.createElement("label", {
    className: "field"
  }, "             ", /*#__PURE__*/React.createElement("span", null, "Zeitraum"), "             ", /*#__PURE__*/React.createElement("select", {
    value: days,
    onChange: e => setDays(Number(e.target.value))
  }, "               ", /*#__PURE__*/React.createElement("option", {
    value: 7
  }, "7 Tage"), "               ", /*#__PURE__*/React.createElement("option", {
    value: 30
  }, "30 Tage"), "               ", /*#__PURE__*/React.createElement("option", {
    value: 60
  }, "60 Tage"), "               ", /*#__PURE__*/React.createElement("option", {
    value: 90
  }, "90 Tage"), "             "), "           "), "           ", /*#__PURE__*/React.createElement("label", {
    className: "field"
  }, "             ", /*#__PURE__*/React.createElement("span", null, "Wochentag"), "             ", /*#__PURE__*/React.createElement("select", {
    value: weekdayFilter,
    onChange: e => setWeekdayFilter(e.target.value)
  }, "               ", /*#__PURE__*/React.createElement("option", {
    value: "all"
  }, "Alle"), "               ", dayLabels.map((d, idx) => /*#__PURE__*/React.createElement("option", {
    key: idx,
    value: idx
  }, d)), "             "), "           "), "           ", /*#__PURE__*/React.createElement("label", {
    className: "field"
  }, "             ", /*#__PURE__*/React.createElement("span", null, "Slot"), "             ", /*#__PURE__*/React.createElement("select", {
    value: slotFilter,
    onChange: e => setSlotFilter(e.target.value)
  }, "               ", /*#__PURE__*/React.createElement("option", {
    value: "all"
  }, "Alle Slots"), "               ", /*#__PURE__*/React.createElement("option", {
    value: "morning"
  }, "Morgen (06-12)"), "               ", /*#__PURE__*/React.createElement("option", {
    value: "afternoon"
  }, "Tag (12-17)"), "               ", /*#__PURE__*/React.createElement("option", {
    value: "evening"
  }, "Prime (17-23)"), "               ", /*#__PURE__*/React.createElement("option", {
    value: "night"
  }, "Late (23-06)"), "             "), "           "), "           ", /*#__PURE__*/React.createElement("label", {
    className: "field"
  }, "             ", /*#__PURE__*/React.createElement("span", null, "Kategorie"), "             ", /*#__PURE__*/React.createElement("select", {
    value: categoryFilter,
    onChange: e => setCategoryFilter(e.target.value)
  }, "               ", /*#__PURE__*/React.createElement("option", {
    value: "all"
  }, "Alle"), "               ", gameOptions.map(g => /*#__PURE__*/React.createElement("option", {
    key: g,
    value: g
  }, g)), "             "), "           "), "           ", /*#__PURE__*/React.createElement("button", {
    className: "pill primary",
    onClick: () => setDays(d => d)
  }, "Neu laden"), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    style: {
      marginTop: '0.7rem',
      display: 'flex',
      gap: '0.6rem',
      flexWrap: 'wrap'
    }
  }, "           ", /*#__PURE__*/React.createElement("div", {
    className: `status ${error ? 'err' : loading ? 'warn' : 'ok'}`
  }, "             ", loading ? 'Lade Daten...' : error ? error : `${sessions.length} Sessions geladen`, "           "), "           ", /*#__PURE__*/React.createElement("div", {
    className: "row tiny"
  }, "             ", /*#__PURE__*/React.createElement("span", {
    className: "badge ok"
  }, "Avg Concurrency"), "             ", /*#__PURE__*/React.createElement("span", {
    className: "badge warn"
  }, "Retention-Kurve"), "             ", /*#__PURE__*/React.createElement("span", {
    className: "badge ok"
  }, "Chat Health"), "             ", /*#__PURE__*/React.createElement("span", {
    className: "badge"
  }, "Timing Heatmap"), "           "), "           ", /*#__PURE__*/React.createElement("div", {
    className: "tiny",
    style: {
      display: 'flex',
      gap: '0.4rem',
      flexWrap: 'wrap'
    }
  }, "             ", /*#__PURE__*/React.createElement("span", null, "Aktive Filter: ", weekdayFilter !== 'all' ? dayLabels[Number(weekdayFilter)] : 'Alle Tage', " \u252C\xC0 ", slotFilter !== 'all' ? slotFilter : 'Alle Slots', " \u252C\xC0 ", categoryFilter !== 'all' ? categoryFilter : 'Alle Games'), "             ", /*#__PURE__*/React.createElement("span", null, "\u252C\xC0"), "             ", /*#__PURE__*/React.createElement("span", null, "Im Fokus: ", scopedSessions.length, " / ", sessions.length, " Sessions"), "           "), "         "), "       "), "        ", /*#__PURE__*/React.createElement("div", {
    className: "kpi-grid"
  }, "         ", /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, "           ", /*#__PURE__*/React.createElement("h4", null, "Avg Concurrent"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "value"
  }, fmtNumber(derived.avgAvgViewers, 1), " ", deltaBadge(derived.avgAvgViewers, comparePrev.avgViewers)), "           ", /*#__PURE__*/React.createElement("div", {
    className: "sub"
  }, "Benchmark P75: ", fmtNumber(benchmark?.categoryQuantiles?.q75 || 0, 0)), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, "           ", /*#__PURE__*/React.createElement("h4", null, "Peak Viewer"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "value"
  }, fmtNumber(discovery.avgPeak || summary.avgPeakViewers || 0, 1)), "           ", /*#__PURE__*/React.createElement("div", {
    className: "sub"
  }, "Stabile Peaks bringen Reichweite"), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, "           ", /*#__PURE__*/React.createElement("h4", null, "Unique Reach (est)"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "value"
  }, fmtNumber(audience.uniqueEstimateAvg || audience.uniqueEstimateTotal || derived.uniqueAudience || 0, 1)), "           ", /*#__PURE__*/React.createElement("div", {
    className: "sub"
  }, "Avg/Unique: ", fmtPercent(audience.avgToUniqueRatio || 0, 1)), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, "           ", /*#__PURE__*/React.createElement("h4", null, "Retention 10m"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "value"
  }, fmtPercent(retention.avg10m || summary.retention10m || 0, 1), " ", deltaBadge(retention.avg10m || summary.retention10m, comparePrev.retention10m, {
    percent: true,
    digits: 1
  })), "           ", /*#__PURE__*/React.createElement("div", {
    className: "sub"
  }, "5/10/20m: ", fmtPercent(retention.avg5m || 0, 0), " / ", fmtPercent(retention.avg10m || 0, 0), " / ", fmtPercent(retention.avg20m || 0, 0)), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, "           ", /*#__PURE__*/React.createElement("h4", null, "Chat / 100 Viewer"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "value"
  }, fmtNumber(chat.uniquePer100 || chat.uniqueChatPer100 || 0, 1), " ", deltaBadge(chat.uniquePer100 || chat.uniqueChatPer100, comparePrev.chatPer100, {
    digits: 1
  })), "           ", /*#__PURE__*/React.createElement("div", {
    className: "sub"
  }, "Returning: ", fmtPercent(chat.returningShare || 0, 1), " \u252C\xC0 First: ", fmtPercent(chat.firstShare || 0, 1)), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, "           ", /*#__PURE__*/React.createElement("h4", null, "Follower / Stunde"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "value"
  }, fmtNumber(discovery.followersPerHour || 0, 2), " ", deltaBadge(discovery.followersPerHour, comparePrev.followersPerHour, {
    digits: 2
  })), "           ", /*#__PURE__*/React.createElement("div", {
    className: "sub"
  }, "Delta: +", fmtNumber(discovery.followersDelta || summary.followersDelta || 0), " in ", days, " Tagen"), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, "           ", /*#__PURE__*/React.createElement("h4", null, "Avg Session-L\u251C\xF1nge"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "value"
  }, fmtDuration(derived.avgDuration || 0), " ", deltaBadge((derived.avgDuration || 0) / 60, comparePrev.avgDurationMin, {
    digits: 1
  })), "           ", /*#__PURE__*/React.createElement("div", {
    className: "sub"
  }, "Sessions/Woche: ", fmtNumber(derived.perWeek || 0, 1)), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, "           ", /*#__PURE__*/React.createElement("h4", null, "Watch Time"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "value"
  }, fmtNumber(audience.watchTimeHours || summary.avgWatchTimeHours || 0, 2), "h"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "sub"
  }, "Avg/Viewer: ", fmtNumber(audience.watchTimePerViewerMin || 0, 1), " Min ", deltaBadge(audience.watchTimePerViewerMin, comparePrev.watchTimePerViewerMin, {
    digits: 1
  })), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, "           ", /*#__PURE__*/React.createElement("h4", null, "Rückkehrrate 7d"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "value"
  }, fmtPercent(discovery.returningRate7d || audience.returningRate7d || 0, 1)), "           ", /*#__PURE__*/React.createElement("div", {
    className: "sub"
  }, "30d: ", fmtPercent(discovery.returningRate30d || audience.returningRate30d || 0, 1)), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, "           ", /*#__PURE__*/React.createElement("h4", null, "Chat RPM"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "value"
  }, fmtNumber(chat.messagesPerMin || 0, 2)), "           ", /*#__PURE__*/React.createElement("div", {
    className: "sub"
  }, "Msgs/Viewer: ", fmtNumber(chat.messagesPerViewer || 0, 2)), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    className: "kpi"
  }, "           ", /*#__PURE__*/React.createElement("h4", null, "Stream Score"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "value"
  }, derived.streamScore, "%"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "sub"
  }, "Retention gewichtet x2 gegen\u251C\u255Dber Peak"), "         "), "       "), "        ", /*#__PURE__*/React.createElement("div", {
    className: "panel"
  }, "         ", /*#__PURE__*/React.createElement("div", {
    className: "panel-header"
  }, "           ", /*#__PURE__*/React.createElement("div", {
    className: "meta"
  }, /*#__PURE__*/React.createElement("p", {
    className: "eyebrow"
  }, "Gewichtung"), /*#__PURE__*/React.createElement("h3", {
    className: "title"
  }, "Performance Radar (50/30/20)")), "         "), "         ", /*#__PURE__*/React.createElement(ChartBox, {
    config: {
      type: 'radar',
      data: {
        labels: radarData.labels,
        datasets: [{
          label: 'Aktuell',
          data: radarData.values,
          borderColor: '#7df1c5',
          backgroundColor: 'rgba(125,241,197,0.18)',
          pointBackgroundColor: '#7df1c5'
        }]
      },
      options: {
        responsive: true,
        scales: {
          r: {
            angleLines: {
              color: '#1f2942'
            },
            grid: {
              color: '#1f2942'
            },
            ticks: {
              display: false
            },
            pointLabels: {
              color: '#cbd5e1',
              font: {
                size: 11
              }
            }
          }
        },
        plugins: {
          legend: {
            labels: {
              color: '#cbd5e1'
            }
          }
        }
      }
    }
  }), "         ", /*#__PURE__*/React.createElement("div", {
    className: "row tiny",
    style: {
      marginTop: '0.6rem'
    }
  }, "           ", /*#__PURE__*/React.createElement("span", null, "Basis: Retention ", fmtPercent(retention.avg10m || 0, 1), " ", deltaBadge(retention.avg10m || 0, comparePrev.retention10m, {
    percent: true
  })), "           ", /*#__PURE__*/React.createElement("span", null, "\u252C\xC0"), "           ", /*#__PURE__*/React.createElement("span", null, "Avg Viewer ", fmtNumber(derived.avgAvgViewers, 1), " ", deltaBadge(derived.avgAvgViewers, comparePrev.avgViewers)), "           ", /*#__PURE__*/React.createElement("span", null, "\u252C\xC0"), "           ", /*#__PURE__*/React.createElement("span", null, "Chat/100 ", fmtNumber(chat.uniquePer100 || 0, 1), " ", deltaBadge(chat.uniquePer100, comparePrev.chatPer100, {
    digits: 1
  })), "         "), "       "), "        ", /*#__PURE__*/React.createElement("div", {
    className: "stack"
  }, "         ", /*#__PURE__*/React.createElement("div", {
    className: "panel"
  }, "           ", /*#__PURE__*/React.createElement("div", {
    className: "panel-header"
  }, "             ", /*#__PURE__*/React.createElement("div", {
    className: "meta"
  }, /*#__PURE__*/React.createElement("p", {
    className: "eyebrow"
  }, "Qualit\u251C\xF1t"), /*#__PURE__*/React.createElement("h3", {
    className: "title"
  }, "Retention-Kurve & Drop-Off")), "           "), "           ", /*#__PURE__*/React.createElement(ChartBox, {
    config: {
      type: 'line',
      data: {
        labels: (retention.trend || []).slice(0, 18).reverse().map(t => t.label),
        datasets: [{
          label: '5m',
          data: (retention.trend || []).slice(0, 18).reverse().map(t => t.r5),
          borderColor: '#53b0f8',
          backgroundColor: 'rgba(83,176,248,0.18)',
          tension: 0.35,
          fill: true
        }, {
          label: '10m',
          data: (retention.trend || []).slice(0, 18).reverse().map(t => t.r10),
          borderColor: '#7df1c5',
          tension: 0.35
        }, {
          label: '20m',
          data: (retention.trend || []).slice(0, 18).reverse().map(t => t.r20),
          borderColor: '#f0c674',
          borderDash: [6, 4],
          tension: 0.35
        }]
      },
      options: {
        responsive: true,
        scales: {
          y: {
            suggestedMax: 100,
            ticks: {
              color: '#cbd5e1',
              callback: v => `${v}%`
            },
            grid: {
              color: '#1f2942'
            }
          },
          x: {
            ticks: {
              color: '#cbd5e1'
            }
          }
        },
        plugins: {
          legend: {
            labels: {
              color: '#cbd5e1'
            }
          }
        }
      }
    }
  }), "           ", /*#__PURE__*/React.createElement("div", {
    className: "divider"
  }), "           ", /*#__PURE__*/React.createElement("div", {
    className: "tiny"
  }, "Drop-Off Schnitt: ", fmtPercent(retention.avgDropoff || 0, 1), " \u252C\xC0 Ziel: ", /*#__PURE__*/React.createElement("strong", null, "<25%"), " Verlust nach Peaks."), "           ", /*#__PURE__*/React.createElement("div", {
    className: "insights",
    style: {
      marginTop: '0.5rem'
    }
  }, "             ", (retention.drops || []).slice(0, 5).map((d, idx) => /*#__PURE__*/React.createElement("div", {
    className: "insight",
    key: idx
  }, "                 ", /*#__PURE__*/React.createElement("h5", null, d.start || 'Stream', " \u252C\xC0 ", fmtPercent(d.dropPct || 0, 1), " Verlust"), "                 ", /*#__PURE__*/React.createElement("p", null, d.dropLabel || 'Drop erkannt', " \u252C\xC0 Minute ", d.minute ?? '–'), "               ")), "           "), "         "), "          ", /*#__PURE__*/React.createElement("div", {
    className: "panel"
  }, "           ", /*#__PURE__*/React.createElement("div", {
    className: "panel-header"
  }, "             ", /*#__PURE__*/React.createElement("div", {
    className: "meta"
  }, /*#__PURE__*/React.createElement("p", {
    className: "eyebrow"
  }, "Growth"), /*#__PURE__*/React.createElement("h3", {
    className: "title"
  }, "Viewer- & Follower-Trend")), "           "), "           ", /*#__PURE__*/React.createElement(ChartBox, {
    config: {
      data: {
        labels: viewerSeries.map(p => p.label),
        datasets: [{
          type: 'line',
          label: 'Avg Viewer',
          data: viewerSeries.map(p => p.avg),
          borderColor: '#53b0f8',
          backgroundColor: 'rgba(83,176,248,0.12)',
          tension: 0.35,
          fill: true
        }, {
          type: 'line',
          label: 'Peak',
          data: viewerSeries.map(p => p.peak),
          borderColor: '#7df1c5',
          tension: 0.3
        }, {
          type: 'bar',
          label: 'Follower Δ',
          data: viewerSeries.map(p => p.delta),
          backgroundColor: 'rgba(240,198,116,0.55)',
          yAxisID: 'y1'
        }, {
          type: 'line',
          label: 'Cumulative Follower',
          data: viewerSeries.map(p => p.cumulative),
          borderColor: '#22d3ee',
          yAxisID: 'y1',
          tension: 0.25
        }]
      },
      options: {
        responsive: true,
        interaction: {
          mode: 'index',
          intersect: false
        },
        scales: {
          y: {
            grid: {
              color: '#1f2942'
            },
            ticks: {
              color: '#cbd5e1'
            }
          },
          y1: {
            position: 'right',
            grid: {
              drawOnChartArea: false
            },
            ticks: {
              color: '#cbd5e1'
            }
          },
          x: {
            ticks: {
              color: '#cbd5e1'
            }
          }
        },
        plugins: {
          legend: {
            labels: {
              color: '#cbd5e1'
            }
          }
        }
      }
    }
  }), "           ", /*#__PURE__*/React.createElement("div", {
    className: "divider"
  }), "           ", /*#__PURE__*/React.createElement("div", {
    className: "tiny"
  }, "Kontext: Konstante Avg-Viewer wichtiger als Peak-Spikes. CTAs bei Peaks, Retention sichern."), "         "), "       "), "        ", /*#__PURE__*/React.createElement("div", {
    className: "stack"
  }, "         ", /*#__PURE__*/React.createElement("div", {
    className: "panel"
  }, "           ", /*#__PURE__*/React.createElement("div", {
    className: "panel-header"
  }, /*#__PURE__*/React.createElement("div", {
    className: "meta"
  }, /*#__PURE__*/React.createElement("p", {
    className: "eyebrow"
  }, "Engagement & Community"), /*#__PURE__*/React.createElement("h3", {
    className: "title"
  }, "Chat-Aktivit\u251C\xF1t & R\u251C\u255Dckkehrrate"))), "           ", /*#__PURE__*/React.createElement(ChartBox, {
    config: {
      type: 'bar',
      data: {
        labels: scopedSessions.slice(0, 14).reverse().map(s => s.date),
        datasets: [{
          label: 'Unique Chat',
          data: scopedSessions.slice(0, 14).reverse().map(s => s.uniqueChatters || 0),
          backgroundColor: 'rgba(83,176,248,0.4)',
          stack: 'chat'
        }, {
          label: 'First-Time',
          data: scopedSessions.slice(0, 14).reverse().map(s => s.firstTimeChatters || 0),
          backgroundColor: 'rgba(240,198,116,0.6)',
          stack: 'chat'
        }, {
          label: 'Returning',
          data: scopedSessions.slice(0, 14).reverse().map(s => s.returningChatters || 0),
          backgroundColor: 'rgba(125,241,197,0.55)',
          stack: 'chat'
        }]
      },
      options: {
        responsive: true,
        plugins: {
          legend: {
            labels: {
              color: '#cbd5e1'
            }
          }
        },
        scales: {
          x: {
            stacked: true,
            ticks: {
              color: '#cbd5e1'
            }
          },
          y: {
            stacked: true,
            grid: {
              color: '#1f2942'
            },
            ticks: {
              color: '#cbd5e1'
            }
          }
        }
      }
    }
  }), "           ", /*#__PURE__*/React.createElement("div", {
    className: "divider"
  }), "           ", /*#__PURE__*/React.createElement(ChartBox, {
    config: {
      type: 'line',
      data: {
        labels: scopedSessions.slice(0, 12).reverse().map(s => s.date),
        datasets: [{
          label: 'Returning %',
          data: scopedSessions.slice(0, 12).reverse().map(s => s.returningRate || 0),
          borderColor: '#7df1c5',
          tension: 0.3
        }, {
          label: 'First %',
          data: scopedSessions.slice(0, 12).reverse().map(s => s.firstRate || 0),
          borderColor: '#f0c674',
          borderDash: [6, 4],
          tension: 0.3
        }]
      },
      options: {
        responsive: true,
        plugins: {
          legend: {
            labels: {
              color: '#cbd5e1'
            }
          }
        },
        scales: {
          x: {
            ticks: {
              color: '#cbd5e1'
            }
          },
          y: {
            grid: {
              color: '#1f2942'
            },
            ticks: {
              color: '#cbd5e1',
              callback: v => `${v}%`
            }
          }
        }
      }
    }
  }), "           ", /*#__PURE__*/React.createElement("div", {
    className: "divider"
  }), "           ", /*#__PURE__*/React.createElement("div", {
    className: "row tiny"
  }, "             ", /*#__PURE__*/React.createElement("span", null, "Returning Chatters (30d): ", fmtNumber(discovery.returning30d || 0)), "             ", /*#__PURE__*/React.createElement("span", null, "\u252C\xC0"), "             ", /*#__PURE__*/React.createElement("span", null, "Engagement-Rate: ", fmtPercent(chat.uniquePer100 || 0, 1), " pro 100 Viewer"), "             ", /*#__PURE__*/React.createElement("span", null, "\u252C\xC0"), "             ", /*#__PURE__*/React.createElement("span", null, "Chat Health Score: ", fmtNumber(chat.chatHealthScore || 0, 1)), "           "), "         "), "          ", /*#__PURE__*/React.createElement("div", {
    className: "panel"
  }, "           ", /*#__PURE__*/React.createElement("div", {
    className: "panel-header"
  }, /*#__PURE__*/React.createElement("div", {
    className: "meta"
  }, /*#__PURE__*/React.createElement("p", {
    className: "eyebrow"
  }, "Timing & Slots"), /*#__PURE__*/React.createElement("h3", {
    className: "title"
  }, "Beste Tageszeiten & Heatmap"))), "           ", /*#__PURE__*/React.createElement(ChartBox, {
    config: {
      type: 'line',
      data: {
        labels: Object.keys(analysis.hourlySelf || {}).sort((a, b) => Number(a) - Number(b)).map(h => `${String(h).padStart(2, '0')}:00`),
        datasets: [{
          label: 'Du',
          data: Object.keys(analysis.hourlySelf || {}).sort((a, b) => Number(a) - Number(b)).map(h => (analysis.hourlySelf || {})[h] || 0),
          borderColor: '#53b0f8',
          tension: 0.3,
          fill: false
        }, {
          label: 'Kategorie',
          data: Object.keys(analysis.hourlyCategory || {}).sort((a, b) => Number(a) - Number(b)).map(h => (analysis.hourlyCategory || {})[h] || 0),
          borderColor: '#9aa7bd',
          borderDash: [6, 4],
          tension: 0.3,
          fill: false
        }, {
          label: 'Tracked',
          data: Object.keys(analysis.hourlyTracked || {}).sort((a, b) => Number(a) - Number(b)).map(h => (analysis.hourlyTracked || {})[h] || 0),
          borderColor: '#7df1c5',
          tension: 0.3,
          fill: false
        }]
      },
      options: {
        responsive: true,
        plugins: {
          legend: {
            labels: {
              color: '#cbd5e1'
            }
          }
        },
        scales: {
          y: {
            grid: {
              color: '#1f2942'
            },
            ticks: {
              color: '#cbd5e1'
            }
          },
          x: {
            ticks: {
              color: '#cbd5e1'
            }
          }
        }
      }
    }
  }), "           ", /*#__PURE__*/React.createElement("div", {
    className: "divider"
  }), "           ", /*#__PURE__*/React.createElement(Heatmap, {
    data: analysis.heatmap
  }), "           ", /*#__PURE__*/React.createElement("div", {
    className: "tiny"
  }, "Heatmap: Dunkler = mehr Durchschnittszuschauer. Nutze grüne Slots regelmäßig."), "           ", /*#__PURE__*/React.createElement("div", {
    className: "divider"
  }), "           ", /*#__PURE__*/React.createElement("div", {
    className: "tiny"
  }, "Top-Slots (\u251C\xFF Viewer):"), "           ", /*#__PURE__*/React.createElement("div", {
    className: "row tiny"
  }, "             ", (timing.topSlots || []).map((slot, idx) => /*#__PURE__*/React.createElement("span", {
    key: idx,
    className: "chip"
  }, dayLabels[slot.weekday].slice(0, 2), " ", String(slot.hour).padStart(2, '0'), ":00 \u251C\u2551 ", fmtNumber(slot.avgViewers, 1))), "           "), "         "), "       "), "        ", /*#__PURE__*/React.createElement("div", {
    className: "panel"
  }, "         ", /*#__PURE__*/React.createElement("div", {
    className: "panel-header"
  }, /*#__PURE__*/React.createElement("div", {
    className: "meta"
  }, /*#__PURE__*/React.createElement("p", {
    className: "eyebrow"
  }, "Session-L\u251C\xF1ngen"), /*#__PURE__*/React.createElement("h3", {
    className: "title"
  }, "Kurz vs. Mittel vs. Lang"))), "         ", /*#__PURE__*/React.createElement("div", {
    className: "seg-grid"
  }, "           ", segments.map(seg => /*#__PURE__*/React.createElement("div", {
    className: "seg-card",
    key: seg.key
  }, "               ", /*#__PURE__*/React.createElement("h6", null, seg.label), "               ", /*#__PURE__*/React.createElement("div", {
    className: "value"
  }, seg.count, "x"), "               ", /*#__PURE__*/React.createElement("div", {
    className: "tiny"
  }, "Avg Viewer: ", fmtNumber(seg.avgViewers || 0, 1)), "               ", /*#__PURE__*/React.createElement("div", {
    className: "tiny"
  }, "Peak: ", fmtNumber(seg.peak || 0, 1), " \u252C\xC0 Ret10: ", fmtPercent(seg.retention10 || 0, 0)), "               ", /*#__PURE__*/React.createElement("div", {
    className: "tiny"
  }, "Follower/Session: ", fmtNumber(seg.followers || 0, 1)), "             ")), "         "), "       "), "        ", /*#__PURE__*/React.createElement("div", {
    className: "panel"
  }, "         ", /*#__PURE__*/React.createElement("div", {
    className: "panel-header"
  }, /*#__PURE__*/React.createElement("div", {
    className: "meta"
  }, /*#__PURE__*/React.createElement("p", {
    className: "eyebrow"
  }, "Content"), /*#__PURE__*/React.createElement("h3", {
    className: "title"
  }, "Top-Kategorien"))), "         ", /*#__PURE__*/React.createElement("div", {
    style: {
      overflowX: 'auto'
    }
  }, "           ", /*#__PURE__*/React.createElement("table", null, "             ", /*#__PURE__*/React.createElement("thead", null, "               ", /*#__PURE__*/React.createElement("tr", null, /*#__PURE__*/React.createElement("th", null, "Game/Kategorie"), /*#__PURE__*/React.createElement("th", null, "Sessions"), /*#__PURE__*/React.createElement("th", null, "Avg Viewer"), /*#__PURE__*/React.createElement("th", null, "Peak"), /*#__PURE__*/React.createElement("th", null, "Follower/h"), /*#__PURE__*/React.createElement("th", null, "Chat/Session")), "             "), "             ", /*#__PURE__*/React.createElement("tbody", null, "               ", (categories.top || []).map((c, idx) => /*#__PURE__*/React.createElement("tr", {
    key: idx
  }, "                   ", /*#__PURE__*/React.createElement("td", null, c.name), "                   ", /*#__PURE__*/React.createElement("td", null, c.sessions), "                   ", /*#__PURE__*/React.createElement("td", null, fmtNumber(c.avgViewers, 1)), "                   ", /*#__PURE__*/React.createElement("td", null, fmtNumber(c.peakViewers, 1)), "                   ", /*#__PURE__*/React.createElement("td", null, fmtNumber(c.followersPerHour, 2)), "                   ", /*#__PURE__*/React.createElement("td", null, fmtNumber(c.chatPerSession, 1)), "                 ")), "               ", (!categories.top || categories.top.length === 0) && /*#__PURE__*/React.createElement("tr", null, /*#__PURE__*/React.createElement("td", {
    colSpan: 6
  }, /*#__PURE__*/React.createElement("i", null, "Keine Kategoriedaten"))), "             "), "           "), "         "), "       "), "        ", /*#__PURE__*/React.createElement("div", {
    className: "panel"
  }, "         ", /*#__PURE__*/React.createElement("div", {
    className: "panel-header"
  }, /*#__PURE__*/React.createElement("div", {
    className: "meta"
  }, /*#__PURE__*/React.createElement("p", {
    className: "eyebrow"
  }, "Sessions"), /*#__PURE__*/React.createElement("h3", {
    className: "title"
  }, "Detail-Tabelle"))), "         ", /*#__PURE__*/React.createElement("div", {
    style: {
      overflowX: 'auto'
    }
  }, "           ", /*#__PURE__*/React.createElement("table", null, "             ", /*#__PURE__*/React.createElement("thead", null, "               ", /*#__PURE__*/React.createElement("tr", null, "                 ", /*#__PURE__*/React.createElement("th", null, "Datum"), /*#__PURE__*/React.createElement("th", null, "Start"), /*#__PURE__*/React.createElement("th", null, "Dauer"), /*#__PURE__*/React.createElement("th", null, "Avg"), /*#__PURE__*/React.createElement("th", null, "Peak"), /*#__PURE__*/React.createElement("th", null, "Game"), /*#__PURE__*/React.createElement("th", null, "Ret 10m"), /*#__PURE__*/React.createElement("th", null, "Chat"), /*#__PURE__*/React.createElement("th", null, "Returning%"), /*#__PURE__*/React.createElement("th", null, "First%"), /*#__PURE__*/React.createElement("th", null, "Msgs/min"), /*#__PURE__*/React.createElement("th", null, "+Follow"), /*#__PURE__*/React.createElement("th", null, "Drop"), /*#__PURE__*/React.createElement("th", null, "Notes"), /*#__PURE__*/React.createElement("th", null, "Details"), "               "), "             "), "             ", /*#__PURE__*/React.createElement("tbody", null, "               ", scopedSessions.slice(0, 40).map(s => /*#__PURE__*/React.createElement("tr", {
    key: s.id
  }, "                   ", /*#__PURE__*/React.createElement("td", null, s.date), "                   ", /*#__PURE__*/React.createElement("td", null, s.startTime), "                   ", /*#__PURE__*/React.createElement("td", null, fmtDuration(s.duration || 0)), "                   ", /*#__PURE__*/React.createElement("td", null, fmtNumber(s.avgViewers || 0)), "                   ", /*#__PURE__*/React.createElement("td", null, fmtNumber(s.peakViewers || 0)), "                   ", /*#__PURE__*/React.createElement("td", null, s.game || '-'), "                   ", /*#__PURE__*/React.createElement("td", null, fmtPercent(s.retention10m || 0, 0)), "                   ", /*#__PURE__*/React.createElement("td", null, fmtNumber(s.uniqueChatters || 0)), "                   ", /*#__PURE__*/React.createElement("td", null, fmtPercent(s.returningRate || 0, 1)), "                   ", /*#__PURE__*/React.createElement("td", null, fmtPercent(s.firstRate || 0, 1)), "                   ", /*#__PURE__*/React.createElement("td", null, fmtNumber(s.rpm || 0, 2)), "                   ", /*#__PURE__*/React.createElement("td", null, s.followerDelta ? '+' + fmtNumber(s.followerDelta, 0) : fmtNumber(s.followerDelta || 0)), "                   ", /*#__PURE__*/React.createElement("td", null, fmtPercent(s.dropoffPct || 0, 1), " ", s.dropMinute ? `@${s.dropMinute}m` : ''), "                   ", /*#__PURE__*/React.createElement("td", null, s.notes ? s.notes : '-'), "                   ", /*#__PURE__*/React.createElement("td", null, /*#__PURE__*/React.createElement("a", {
    className: "badge ok",
    href: `/twitch/session/${s.id}`
  }, "Open")), "                 ")), "             "), "           "), "         "), "       "), "        ", /*#__PURE__*/React.createElement("div", {
    className: "panel"
  }, "         ", /*#__PURE__*/React.createElement("div", {
    className: "panel-header"
  }, /*#__PURE__*/React.createElement("div", {
    className: "meta"
  }, /*#__PURE__*/React.createElement("p", {
    className: "eyebrow"
  }, "Benchmarks"), /*#__PURE__*/React.createElement("h3", {
    className: "title"
  }, "Du vs. Kategorie"))), "         ", /*#__PURE__*/React.createElement(ChartBox, {
    config: {
      type: 'bar',
      data: {
        labels: ['Avg Viewer', 'Retention 10m', 'Chat/100', 'Follower/h'],
        datasets: [{
          label: 'Du',
          data: [derived.avgAvgViewers, retention.avg10m || 0, chat.uniquePer100 || 0, discovery.followersPerHour || 0],
          backgroundColor: 'rgba(83,176,248,0.8)'
        }, {
          label: 'Benchmark',
          data: [benchmark?.categoryQuantiles?.q50 || 0, 60, 8, 5],
          backgroundColor: 'rgba(154,167,189,0.6)'
        }]
      },
      options: {
        responsive: true,
        plugins: {
          legend: {
            labels: {
              color: '#cbd5e1'
            }
          }
        },
        scales: {
          y: {
            grid: {
              color: '#1f2942'
            },
            ticks: {
              color: '#cbd5e1'
            }
          },
          x: {
            ticks: {
              color: '#cbd5e1'
            }
          }
        }
      }
    }
  }), "       "), "        ", /*#__PURE__*/React.createElement("div", {
    className: "panel"
  }, "         ", /*#__PURE__*/React.createElement("div", {
    className: "panel-header"
  }, /*#__PURE__*/React.createElement("div", {
    className: "meta"
  }, /*#__PURE__*/React.createElement("p", {
    className: "eyebrow"
  }, "Actionables"), /*#__PURE__*/React.createElement("h3", {
    className: "title"
  }, "Empfehlungen & Gewichtung"))), "         ", /*#__PURE__*/React.createElement("div", {
    className: "insights"
  }, "           ", insightList.map((ins, idx) => /*#__PURE__*/React.createElement("div", {
    key: idx,
    className: "insight",
    style: {
      borderColor: ins.tone === 'warn' ? 'rgba(240,198,116,0.5)' : ins.tone === 'err' ? 'rgba(248,113,113,0.6)' : 'rgba(125,241,197,0.5)'
    }
  }, "               ", /*#__PURE__*/React.createElement("h5", null, ins.title), "               ", /*#__PURE__*/React.createElement("p", null, ins.desc), "             ")), "         "), "         ", /*#__PURE__*/React.createElement("div", {
    className: "divider"
  }), "         ", /*#__PURE__*/React.createElement("div", {
    className: "tiny"
  }, "Gewichtung: Retention 50% \u252C\xC0 Concurrency 30% \u252C\xC0 Engagement/Follow 20%. Fokus auf Bindung & Chat \xD4\xC7\xF6 reine Peaks zählen weniger."), "       "), "     ");
};
const cfgEl = document.getElementById('analytics-config');
const cfg = cfgEl ? JSON.parse(cfgEl.textContent || '{}') : {};
const root = document.getElementById('analytics-root');
if (root) {
  ReactDOM.createRoot(root).render(/*#__PURE__*/React.createElement(App, {
    config: cfg
  }));
}
</script>
"""
        )
        parts.append(
            """
<script>
// Lightweight fallback renderer: shows basic stats if React/Babel fail to mount.
(function () {
  const root = document.getElementById('analytics-root');
  if (!root) return;

  // If React populated the root, skip fallback.
  setTimeout(() => {
    if (root.children.length > 0) return;

    const cfgEl = document.getElementById('analytics-config');
    let cfg = {};
    try { cfg = cfgEl ? JSON.parse(cfgEl.textContent || '{}') : {}; } catch (_) { cfg = {}; }

    const params = new URLSearchParams();
    params.set('days', String(cfg.days || 30));
    if (cfg.streamer) params.set('streamer', cfg.streamer);
    const urlToken = new URLSearchParams(window.location.search).get('partner_token') || '';
    const partnerToken = (cfg.partnerToken || urlToken || '').trim();
    if (partnerToken) params.set('partner_token', partnerToken);

    root.innerHTML = '';
    const status = document.createElement('div');
    status.style.padding = '0.8rem';
    status.textContent = 'Lade Analytics...';
    root.appendChild(status);

    fetch('/twitch/api/analytics?' + params.toString())
      .then(async (resp) => {
        const json = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(json.error || 'HTTP ' + resp.status);
        return json;
      })
      .then((data) => {
        root.innerHTML = '';
        const summary = data.summary || {};
        const retention = data.retention || {};
        const sessions = data.sessions || [];

        const heading = document.createElement('h2');
        heading.textContent = 'Analytics (Fallback)';
        root.appendChild(heading);

        const info = document.createElement('div');
        info.style.margin = '0.3rem 0 0.6rem 0';
        info.textContent = `Sessions: ${sessions.length} · Avg Peak: ${summary.avgPeakViewers ?? '-'} · Ret10m: ${retention.avg10m ?? '-'}`;
        root.appendChild(info);

        const table = document.createElement('table');
        table.style.width = '100%';
        table.style.borderCollapse = 'collapse';
        table.innerHTML = '<thead><tr><th>Datum</th><th>Start</th><th>Avg</th><th>Peak</th><th>Game</th></tr></thead>';
        const tbody = document.createElement('tbody');
        sessions.slice(0, 20).forEach((s) => {
          const tr = document.createElement('tr');
          tr.innerHTML = `<td>${s.date || '-'}</td><td>${s.startTime || '-'}</td><td>${Math.round(s.avgViewers || 0)}</td><td>${Math.round(s.peakViewers || 0)}</td><td>${s.game || ''}</td>`;
          tbody.appendChild(tr);
        });
        if (!tbody.children.length) {
          const tr = document.createElement('tr');
          tr.innerHTML = '<td colspan="5"><i>Keine Daten geladen.</i></td>';
          tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        Array.from(table.querySelectorAll('th,td')).forEach((el) => {
          el.style.padding = '0.4rem';
          el.style.borderBottom = '1px solid #1f2942';
          el.style.textAlign = 'left';
          el.style.color = '#e7ecf3';
        });
        table.style.background = '#0c1220';
        table.style.border = '1px solid #1f2942';
        table.style.borderRadius = '8px';
        root.appendChild(table);
      })
      .catch((err) => {
        status.textContent = 'Fehler: ' + (err && err.message ? err.message : err);
        status.style.color = '#f87171';
      });
  }, 400);
})();
</script>
"""
        )

        body = "".join(parts).replace("__CONFIG__", config)
        return body


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
