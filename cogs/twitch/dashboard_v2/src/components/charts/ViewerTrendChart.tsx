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
} from 'recharts';
import type { StreamSession } from '@/types/analytics';
import { formatNumber, formatDate } from '@/utils/formatters';

interface ViewerTrendChartProps {
  sessions: StreamSession[];
  title?: string;
}

export function ViewerTrendChart({ sessions, title = 'Viewer Trend' }: ViewerTrendChartProps) {
  // Sort sessions chronologically
  const data = [...sessions]
    .sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime())
    .map(session => ({
      date: formatDate(session.date),
      avgViewers: Math.round(session.avgViewers),
      peakViewers: session.peakViewers,
    }));

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
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="colorAvg" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#7c3aed" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#7c3aed" stopOpacity={0} />
              </linearGradient>
              <linearGradient id="colorPeak" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#38bdf8" stopOpacity={0.2} />
                <stop offset="95%" stopColor="#38bdf8" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis
              dataKey="date"
              stroke="#9ca3af"
              fontSize={12}
              tickLine={false}
              axisLine={false}
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
              formatter={(value) => (
                <span className="text-text-secondary text-sm">
                  {value === 'avgViewers' ? 'Avg Viewers' : 'Peak Viewers'}
                </span>
              )}
            />
            <Area
              type="monotone"
              dataKey="avgViewers"
              stroke="#7c3aed"
              strokeWidth={2}
              fillOpacity={1}
              fill="url(#colorAvg)"
            />
            <Area
              type="monotone"
              dataKey="peakViewers"
              stroke="#38bdf8"
              strokeWidth={2}
              strokeDasharray="5 5"
              fillOpacity={1}
              fill="url(#colorPeak)"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </motion.div>
  );
}
