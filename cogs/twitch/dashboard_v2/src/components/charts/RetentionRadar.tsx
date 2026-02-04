import { motion } from 'framer-motion';
import {
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  Radar,
  ResponsiveContainer,
  Legend,
  Tooltip,
} from 'recharts';
import type { HealthScore } from '@/types/analytics';

interface RetentionRadarProps {
  scores: HealthScore;
  categoryAvg?: HealthScore;
  title?: string;
}

export function RetentionRadar({
  scores,
  categoryAvg,
  title = 'Performance Mix',
}: RetentionRadarProps) {
  const data = [
    {
      metric: 'Reach',
      you: scores.reach,
      category: categoryAvg?.reach ?? 50,
    },
    {
      metric: 'Retention',
      you: scores.retention,
      category: categoryAvg?.retention ?? 50,
    },
    {
      metric: 'Engagement',
      you: scores.engagement,
      category: categoryAvg?.engagement ?? 50,
    },
    {
      metric: 'Growth',
      you: scores.growth,
      category: categoryAvg?.growth ?? 50,
    },
    {
      metric: 'Monetization',
      you: scores.monetization,
      category: categoryAvg?.monetization ?? 50,
    },
    {
      metric: 'Network',
      you: scores.network,
      category: categoryAvg?.network ?? 50,
    },
  ];

  const CustomTooltip = ({ active, payload }: any) => {
    if (active && payload && payload.length) {
      return (
        <div className="bg-card border border-border rounded-lg p-3 shadow-xl">
          <p className="text-white font-medium mb-2">{payload[0]?.payload?.metric}</p>
          <div className="space-y-1 text-sm">
            <p className="text-accent">
              Du: <span className="text-white">{payload[0]?.value}</span>
            </p>
            {categoryAvg && (
              <p className="text-gray-400">
                Kategorie: <span className="text-white">{payload[1]?.value}</span>
              </p>
            )}
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
      className="bg-card p-6 rounded-xl border border-border"
    >
      <h3 className="text-lg font-bold text-white mb-4">{title}</h3>
      <div className="h-64 relative">
        <ResponsiveContainer width="100%" height="100%">
          <RadarChart data={data} margin={{ top: 20, right: 30, bottom: 20, left: 30 }}>
            <PolarGrid stroke="#374151" />
            <PolarAngleAxis
              dataKey="metric"
              tick={{ fill: '#d1d5db', fontSize: 12 }}
            />
            <PolarRadiusAxis
              angle={30}
              domain={[0, 100]}
              tick={{ fill: '#6b7280', fontSize: 10 }}
              axisLine={false}
            />
            <Tooltip content={<CustomTooltip />} />
            <Legend
              verticalAlign="bottom"
              height={36}
              formatter={(value) => (
                <span className="text-text-secondary text-sm">
                  {value === 'you' ? 'Dein Kanal' : 'Kategorie Ø'}
                </span>
              )}
            />
            <Radar
              name="you"
              dataKey="you"
              stroke="#7c3aed"
              fill="#7c3aed"
              fillOpacity={0.3}
              strokeWidth={2}
            />
            {categoryAvg && (
              <Radar
                name="category"
                dataKey="category"
                stroke="#6b7280"
                fill="#6b7280"
                fillOpacity={0.1}
                strokeWidth={1}
                strokeDasharray="5 5"
              />
            )}
          </RadarChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-4 text-xs text-center text-text-secondary">
        Basierend auf Benchmarks vs. Deadlock Kategorie
      </div>
    </motion.div>
  );
}
