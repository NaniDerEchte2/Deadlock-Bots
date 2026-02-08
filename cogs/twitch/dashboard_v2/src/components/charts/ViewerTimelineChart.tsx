import { motion } from 'framer-motion';
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Legend,
  Brush,
} from 'recharts';
import type { ViewerTimelinePoint } from '@/types/analytics';
import { formatNumber } from '@/utils/formatters';

interface ViewerTimelineChartProps {
  data: ViewerTimelinePoint[];
  title?: string;
}

export function ViewerTimelineChart({ data, title = 'Viewer Timeline' }: ViewerTimelineChartProps) {
  if (!data || data.length === 0) {
    return null;
  }

  // Format timestamp for display
  const chartData = data.map(point => ({
    ...point,
    label: formatTimestamp(point.timestamp),
  }));

  const showBrush = chartData.length > 100;

  const CustomTooltip = ({ active, payload, label }: any) => {
    if (active && payload && payload.length) {
      return (
        <div className="bg-card border border-border rounded-lg p-3 shadow-xl">
          <p className="text-white font-medium mb-2">{label}</p>
          <div className="space-y-1 text-sm">
            <p className="text-accent">
              Avg: <span className="text-white">{formatNumber(payload[0]?.value)}</span>
            </p>
            <p className="text-blue-400">
              Peak: <span className="text-white">{formatNumber(payload[1]?.value)}</span>
            </p>
            <p className="text-gray-400">
              Min: <span className="text-white">{formatNumber(payload[2]?.value)}</span>
            </p>
          </div>
        </div>
      );
    }
    return null;
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-card rounded-xl border border-border p-5"
    >
      <h3 className="text-lg font-bold text-white mb-4">{title}</h3>
      <div className={showBrush ? 'h-80' : 'h-64'}>
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={chartData} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="tlAvg" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#7c3aed" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#7c3aed" stopOpacity={0} />
              </linearGradient>
              <linearGradient id="tlPeak" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#38bdf8" stopOpacity={0.15} />
                <stop offset="95%" stopColor="#38bdf8" stopOpacity={0} />
              </linearGradient>
              <linearGradient id="tlMin" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#6b7280" stopOpacity={0.1} />
                <stop offset="95%" stopColor="#6b7280" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis
              dataKey="label"
              stroke="#9ca3af"
              fontSize={11}
              tickLine={false}
              axisLine={false}
              interval="preserveStartEnd"
            />
            <YAxis
              stroke="#9ca3af"
              fontSize={12}
              tickLine={false}
              axisLine={false}
              tickFormatter={(v) => formatNumber(v)}
            />
            <Tooltip content={<CustomTooltip />} />
            <Legend
              verticalAlign="top"
              height={36}
              formatter={(value: string) => (
                <span className="text-text-secondary text-sm">
                  {value === 'avgViewers' ? 'Avg' : value === 'peakViewers' ? 'Peak' : 'Min'}
                </span>
              )}
            />
            <Area
              type="monotone"
              dataKey="avgViewers"
              stroke="#7c3aed"
              strokeWidth={2}
              fillOpacity={1}
              fill="url(#tlAvg)"
            />
            <Area
              type="monotone"
              dataKey="peakViewers"
              stroke="#38bdf8"
              strokeWidth={1}
              strokeDasharray="5 5"
              fillOpacity={1}
              fill="url(#tlPeak)"
            />
            <Area
              type="monotone"
              dataKey="minViewers"
              stroke="#6b7280"
              strokeWidth={1}
              strokeDasharray="3 3"
              fillOpacity={1}
              fill="url(#tlMin)"
            />
            {showBrush && (
              <Brush
                dataKey="label"
                height={30}
                stroke="#7c3aed"
                fill="#1f2937"
                travellerWidth={10}
              />
            )}
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </motion.div>
  );
}

function formatTimestamp(ts: string): string {
  // Timestamps look like "2025-12-06 20:05" or "2025-12-06 20:00"
  if (!ts) return '';
  const parts = ts.split(' ');
  if (parts.length >= 2) {
    const datePart = parts[0];
    const timePart = parts[1];
    // Show "DD.MM HH:MM"
    const [, m, d] = datePart.split('-');
    return `${d}.${m} ${timePart}`;
  }
  return ts;
}
