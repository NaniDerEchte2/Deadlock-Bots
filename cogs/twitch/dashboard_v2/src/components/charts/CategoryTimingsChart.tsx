import { useMemo, useState } from 'react';
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Clock, Calendar, Info } from 'lucide-react';
import type { CategoryActivitySeries } from '@/types/analytics';

interface CategoryTimingsChartProps {
  data: CategoryActivitySeries;
}

const TOOLTIP_STYLE = {
  backgroundColor: '#1a1d23',
  border: '1px solid #2d3139',
  borderRadius: '8px',
  fontSize: '12px',
};

function fmt(value: number | null | undefined, digits = 0) {
  if (value === null || value === undefined) return '–';
  return value.toLocaleString('de-DE', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function tickFmt(value: number) {
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

export function CategoryTimingsChart({ data }: CategoryTimingsChartProps) {
  const [view, setView] = useState<'hourly' | 'weekly'>('hourly');
  const rows = view === 'hourly' ? data.hourly : data.weekly;

  const hasPeakSeries = useMemo(
    () => rows.some(row => row.categoryPeak !== null || row.trackedPeak !== null),
    [rows]
  );

  const CustomTooltip = ({ active, payload, label }: any) => {
    if (!active || !payload?.length) return null;
    const d = payload[0]?.payload ?? {};
    return (
      <div style={TOOLTIP_STYLE} className="p-3 space-y-1 min-w-[220px]">
        <div className="font-semibold text-white text-sm">{label}</div>
        <div className="flex justify-between gap-4 text-sm">
          <span className="text-text-secondary">Kategorie Ø</span>
          <span className="font-bold text-white">{fmt(d.categoryAvg, 1)}</span>
        </div>
        <div className="flex justify-between gap-4 text-sm">
          <span className="text-text-secondary">Tracked Ø</span>
          <span className="font-bold text-white">{fmt(d.trackedAvg, 1)}</span>
        </div>
        <div className="flex justify-between gap-4 text-xs">
          <span className="text-text-secondary">Kategorie Peak</span>
          <span className="text-text-secondary">{fmt(d.categoryPeak)}</span>
        </div>
        <div className="flex justify-between gap-4 text-xs">
          <span className="text-text-secondary">Tracked Peak</span>
          <span className="text-text-secondary">{fmt(d.trackedPeak)}</span>
        </div>
        <div className="border-t border-border/50 pt-1 mt-1 flex justify-between gap-4 text-xs">
          <span className="text-text-secondary">Kategorie Samples</span>
          <span className="text-text-secondary">{fmt(d.categorySamples)}</span>
        </div>
        <div className="flex justify-between gap-4 text-xs">
          <span className="text-text-secondary">Tracked Samples</span>
          <span className="text-text-secondary">{fmt(d.trackedSamples)}</span>
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-4">
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
              Legacy Stats Vergleich (Kategorie vs Tracked) · {data.windowDays}d
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

      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={rows} margin={{ top: 10, right: 10, bottom: 0, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#2d3139" vertical={false} />
            <XAxis
              dataKey="label"
              stroke="#6b7280"
              tick={{ fill: '#9ca3af', fontSize: view === 'hourly' ? 10 : 12 }}
              interval={view === 'hourly' ? 1 : 0}
            />
            <YAxis
              yAxisId="yAvg"
              stroke="#6b7280"
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              tickFormatter={tickFmt}
              width={45}
              label={{ value: 'Ø Viewer', angle: -90, position: 'insideLeft', fill: '#9bb0ff' }}
            />
            {hasPeakSeries && (
              <YAxis
                yAxisId="yPeak"
                orientation="right"
                stroke="#6b7280"
                tick={{ fill: '#9ca3af', fontSize: 11 }}
                tickFormatter={tickFmt}
                width={45}
                label={{ value: 'Peak Viewer', angle: 90, position: 'insideRight', fill: '#ffb347' }}
              />
            )}
            <Tooltip content={<CustomTooltip />} cursor={{ stroke: '#475569', strokeOpacity: 0.4 }} />
            <Legend wrapperStyle={{ color: '#dddddd', fontSize: '12px' }} />

            <Area
              yAxisId="yAvg"
              type="monotone"
              dataKey="categoryAvg"
              name="Kategorie Ø Viewer"
              stroke="#6d4aff"
              fill="#6d4aff"
              fillOpacity={0.25}
              strokeWidth={2}
              connectNulls
              isAnimationActive={false}
              dot={{ r: 2 }}
              activeDot={{ r: 4 }}
            />
            <Area
              yAxisId="yAvg"
              type="monotone"
              dataKey="trackedAvg"
              name="Tracked Ø Viewer"
              stroke="#4adede"
              fill="#4adede"
              fillOpacity={0.2}
              strokeWidth={2}
              connectNulls
              isAnimationActive={false}
              dot={{ r: 2 }}
              activeDot={{ r: 4 }}
            />
            {hasPeakSeries && (
              <Line
                yAxisId="yPeak"
                type="monotone"
                dataKey="categoryPeak"
                name="Kategorie Peak Viewer"
                stroke="#ffb347"
                strokeDasharray="6 4"
                strokeWidth={2}
                connectNulls
                isAnimationActive={false}
                dot={{ r: 2 }}
                activeDot={{ r: 4 }}
              />
            )}
            {hasPeakSeries && (
              <Line
                yAxisId="yPeak"
                type="monotone"
                dataKey="trackedPeak"
                name="Tracked Peak Viewer"
                stroke="#ff6f91"
                strokeDasharray="4 4"
                strokeWidth={2}
                connectNulls
                isAnimationActive={false}
                dot={{ r: 2 }}
                activeDot={{ r: 4 }}
              />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
