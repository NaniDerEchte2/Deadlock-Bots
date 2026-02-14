import { useState } from 'react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';
import { Clock, Calendar, Info } from 'lucide-react';
import type { CategoryTimings } from '@/types/analytics';

interface CategoryTimingsChartProps {
  data: CategoryTimings;
}

const TOOLTIP_STYLE = {
  backgroundColor: '#1a1d23',
  border: '1px solid #2d3139',
  borderRadius: '8px',
  fontSize: '12px',
};

function fmt(n: number | null | undefined) {
  if (n === null || n === undefined) return '–';
  return n.toLocaleString('de-DE', { maximumFractionDigits: 0 });
}

function tickFmt(v: number) {
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
  return String(v);
}

export function CategoryTimingsChart({ data }: CategoryTimingsChartProps) {
  const [view, setView] = useState<'hourly' | 'weekly'>('hourly');

  const hourRows = [...data.hourly]
    .sort((a, b) => a.hour - b.hour)
    .map(s => ({
      label: `${String(s.hour).padStart(2, '0')}:00`,
      median: s.median,
      p75: s.p75,
      p25: s.p25,
      streamers: s.streamer_count,
      samples: s.sample_count,
    }));

  const weekRows = data.weekly.map(s => ({
    label: s.label,
    median: s.median,
    p75: s.p75,
    p25: s.p25,
    streamers: s.streamer_count,
    samples: s.sample_count,
  }));

  const activeRows = view === 'hourly' ? hourRows : weekRows;

  // Overall median (median of all medians) as reference line
  const validMedians = activeRows.map(r => r.median).filter((v): v is number => v !== null);
  const overallMedian = validMedians.length
    ? validMedians.sort((a, b) => a - b)[Math.floor(validMedians.length / 2)]
    : null;

  const CustomTooltip = ({ active, payload, label }: any) => {
    if (!active || !payload?.length) return null;
    const d = payload[0]?.payload;
    return (
      <div style={TOOLTIP_STYLE} className="p-3 space-y-1 min-w-[160px]">
        <div className="font-semibold text-white text-sm">{label}</div>
        <div className="flex justify-between gap-4 text-sm">
          <span className="text-text-secondary">Median</span>
          <span className="font-bold text-white">{fmt(d?.median)}</span>
        </div>
        {d?.p25 != null && d?.p75 != null && (
          <div className="flex justify-between gap-4 text-xs">
            <span className="text-text-secondary">P25–P75</span>
            <span className="text-text-secondary">{fmt(d.p25)} – {fmt(d.p75)}</span>
          </div>
        )}
        <div className="border-t border-border/50 pt-1 mt-1 flex justify-between gap-4 text-xs">
          <span className="text-text-secondary">Streamer</span>
          <span className="text-text-secondary">{fmt(d?.streamers)}</span>
        </div>
        <div className="flex justify-between gap-4 text-xs">
          <span className="text-text-secondary">Messwerte</span>
          <span className="text-text-secondary">{fmt(d?.samples)}</span>
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-4">
      {/* Header + Toggle */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <div className="flex items-center gap-2">
            <Clock className="w-4 h-4 text-accent" />
            <h3 className="font-semibold text-white">
              {view === 'hourly' ? 'Aktivität nach Uhrzeit (UTC)' : 'Aktivität nach Wochentag'}
            </h3>
          </div>
          <div className="flex items-center gap-1.5 mt-1 text-xs text-text-secondary">
            <Info className="w-3 h-3" />
            <span>
              Median (outlier-resistent) · {data.total_streamers} Streamer · {data.window_days}d
              {overallMedian != null && <> · Ø Median: <strong className="text-white">{fmt(overallMedian)}</strong></>}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-1 bg-background border border-border rounded-lg p-1">
          <button
            onClick={() => setView('hourly')}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
              view === 'hourly' ? 'bg-accent text-white' : 'text-text-secondary hover:text-white'
            }`}
          >
            <Clock className="w-3 h-3" /> Stunde
          </button>
          <button
            onClick={() => setView('weekly')}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
              view === 'weekly' ? 'bg-accent text-white' : 'text-text-secondary hover:text-white'
            }`}
          >
            <Calendar className="w-3 h-3" /> Wochentag
          </button>
        </div>
      </div>

      {/* Chart */}
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={activeRows}
            margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
            barCategoryGap="20%"
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#2d3139" vertical={false} />
            <XAxis
              dataKey="label"
              stroke="#6b7280"
              tick={{ fill: '#9ca3af', fontSize: view === 'hourly' ? 10 : 12 }}
              interval={view === 'hourly' ? 1 : 0}
            />
            <YAxis
              stroke="#6b7280"
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              width={45}
              tickFormatter={tickFmt}
            />
            <Tooltip content={<CustomTooltip />} cursor={{ fill: 'rgba(255,255,255,0.04)' }} />

            {/* Median als Hauptbalken */}
            <Bar
              dataKey="median"
              name="Median Viewer"
              fill="#818cf8"
              radius={[3, 3, 0, 0]}
              isAnimationActive={false}
            />

            {/* P75 als schmaler Indikator-Balken (zeigt Obergrenze) */}
            <Bar
              dataKey="p75"
              name="P75 (oberes Quartil)"
              fill="#7c3aed"
              fillOpacity={0.35}
              radius={[3, 3, 0, 0]}
              isAnimationActive={false}
            />

            {/* Referenzlinie: Gesamtmedian */}
            {overallMedian != null && (
              <ReferenceLine
                y={overallMedian}
                stroke="#f59e0b"
                strokeDasharray="4 3"
                strokeWidth={1.5}
                label={{ value: `Ø ${fmt(overallMedian)}`, fill: '#f59e0b', fontSize: 10, position: 'insideTopRight' }}
              />
            )}
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Legende */}
      <div className="flex flex-wrap gap-4 text-xs text-text-secondary">
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded-sm bg-indigo-400 inline-block" />
          Median Viewer (robust, Ausreißer herausgefiltert)
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded-sm bg-violet-700/50 inline-block" />
          P75 (oberes Quartil – 75% der Streamer liegen darunter)
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-4 border-t-2 border-dashed border-amber-400 inline-block" />
          Gesamtmedian
        </div>
      </div>
    </div>
  );
}
