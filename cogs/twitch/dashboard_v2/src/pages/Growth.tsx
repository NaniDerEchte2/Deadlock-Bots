import { useMemo } from 'react';
import { motion } from 'framer-motion';
import { TrendingUp, Calendar, Clock, AlertCircle, Loader2 } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from 'recharts';
import { fetchMonthlyStats, fetchWeekdayStats } from '@/api/client';
import { useTagAnalysisExtended, useTitlePerformance } from '@/hooks/useAnalytics';
import { TagPerformanceChart } from '@/components/charts/TagPerformance';
import type { MonthlyStats, WeekdayStats, TimeRange } from '@/types/analytics';

interface GrowthProps {
  streamer: string;
  days: TimeRange;
}

export function Growth({ streamer, days }: GrowthProps) {
  const { data: monthlyData, isLoading: loadingMonthly } = useQuery<MonthlyStats[]>({
    queryKey: ['monthlyStats', streamer, 12],
    queryFn: () => fetchMonthlyStats(streamer, 12),
    enabled: true,
  });

  const { data: weeklyData, isLoading: loadingWeekly } = useQuery<WeekdayStats[]>({
    queryKey: ['weeklyStats', streamer, days],
    queryFn: () => fetchWeekdayStats(streamer, days),
    enabled: true,
  });

  const { data: tagData } = useTagAnalysisExtended(streamer, days);
  const { data: titleData } = useTitlePerformance(streamer, days);

  const chartData = useMemo(() => {
    if (!monthlyData) return [];
    return [...monthlyData].reverse().map(m => ({
      name: `${m.monthLabel} ${m.year}`,
      hoursWatched: Math.round(m.totalHoursWatched),
      airtime: Math.round(m.totalAirtime),
      avgViewers: Math.round(m.avgViewers),
      followers: m.followerDelta,
      streams: m.streamCount,
    }));
  }, [monthlyData]);

  if (loadingMonthly || loadingWeekly) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  if (!monthlyData || monthlyData.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-64">
        <AlertCircle className="w-12 h-12 text-text-secondary mb-4" />
        <p className="text-text-secondary text-lg">Keine Wachstumsdaten verfügbar</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Monthly Overview Cards */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        {monthlyData.slice(0, 4).map((month, i) => (
          <motion.div
            key={`${month.year}-${month.month}`}
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.1 }}
            className={`bg-card rounded-xl border border-border p-5 ${i === 0 ? 'ring-2 ring-primary/30' : ''}`}
          >
            <div className="flex items-center justify-between mb-3">
              <span className="text-sm text-text-secondary">{month.monthLabel} {month.year}</span>
              {i === 0 && <span className="text-xs bg-primary/20 text-primary px-2 py-0.5 rounded">Aktuell</span>}
            </div>
            <div className="space-y-2">
              <MetricRow
                label="Hours Watched"
                value={month.totalHoursWatched.toLocaleString('de-DE', { maximumFractionDigits: 0 })}
                unit="h"
              />
              <MetricRow
                label="Ø Viewer"
                value={month.avgViewers.toLocaleString('de-DE', { maximumFractionDigits: 0 })}
              />
              <MetricRow
                label="Follower"
                value={(month.followerDelta >= 0 ? '+' : '') + month.followerDelta.toLocaleString('de-DE')}
                isPositive={month.followerDelta >= 0}
              />
              <MetricRow
                label="Streams"
                value={month.streamCount.toString()}
              />
            </div>
          </motion.div>
        ))}
      </div>

      {/* Hours Watched Trend */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2 }}
        className="bg-card rounded-xl border border-border p-6"
      >
        <div className="flex items-center gap-3 mb-6">
          <TrendingUp className="w-6 h-6 text-primary" />
          <h2 className="text-xl font-bold text-white">Wachstumstrend (12 Monate)</h2>
        </div>

        <div className="h-[300px]">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis dataKey="name" stroke="#9ca3af" fontSize={12} />
              <YAxis stroke="#9ca3af" fontSize={12} />
              <Tooltip
                contentStyle={{
                  backgroundColor: '#1f2937',
                  border: '1px solid #374151',
                  borderRadius: '8px',
                }}
                labelStyle={{ color: '#fff' }}
              />
              <Legend />
              <Line
                type="monotone"
                dataKey="hoursWatched"
                name="Hours Watched"
                stroke="#7c3aed"
                strokeWidth={2}
                dot={{ fill: '#7c3aed' }}
              />
              <Line
                type="monotone"
                dataKey="avgViewers"
                name="Ø Viewer"
                stroke="#10b981"
                strokeWidth={2}
                dot={{ fill: '#10b981' }}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </motion.div>

      {/* Weekday Analysis */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.3 }}
        className="bg-card rounded-xl border border-border p-6"
      >
        <div className="flex items-center gap-3 mb-6">
          <Calendar className="w-6 h-6 text-accent" />
          <h2 className="text-xl font-bold text-white">Wochentags-Analyse</h2>
        </div>

        {weeklyData && weeklyData.length > 0 ? (
          <>
            <div className="h-[250px] mb-6">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={weeklyData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                  <XAxis dataKey="weekdayLabel" stroke="#9ca3af" fontSize={12} />
                  <YAxis stroke="#9ca3af" fontSize={12} />
                  <Tooltip
                    contentStyle={{
                      backgroundColor: '#1f2937',
                      border: '1px solid #374151',
                      borderRadius: '8px',
                    }}
                    labelStyle={{ color: '#fff' }}
                  />
                  <Legend />
                  <Bar dataKey="avgViewers" name="Ø Viewer" fill="#7c3aed" radius={[4, 4, 0, 0]} />
                  <Bar dataKey="streamCount" name="Streams" fill="#f59e0b" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>

            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              {getBestWorstDays(weeklyData).map((item, i) => (
                <div key={i} className={`p-4 rounded-lg ${item.type === 'best' ? 'bg-success/10' : 'bg-warning/10'}`}>
                  <div className={`text-sm ${item.type === 'best' ? 'text-success' : 'text-warning'}`}>
                    {item.label}
                  </div>
                  <div className="text-lg font-bold text-white mt-1">{item.day}</div>
                  <div className="text-sm text-text-secondary">{item.value}</div>
                </div>
              ))}
            </div>
          </>
        ) : (
          <div className="text-center py-8 text-text-secondary">
            <Calendar className="w-12 h-12 mx-auto mb-3 opacity-50" />
            <p>Keine Wochentags-Daten verfügbar</p>
          </div>
        )}
      </motion.div>

      {/* Stream Schedule Insights */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.4 }}
        className="bg-gradient-to-r from-primary/10 to-accent/10 rounded-xl border border-primary/20 p-6"
      >
        <div className="flex items-center gap-3 mb-4">
          <Clock className="w-6 h-6 text-primary" />
          <h3 className="text-lg font-bold text-white">Schedule-Empfehlungen</h3>
        </div>

        {weeklyData && weeklyData.length > 0 && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {generateScheduleInsights(weeklyData).map((insight, i) => (
              <div key={i} className="flex items-start gap-3 p-3 bg-background/50 rounded-lg">
                <div className={`w-2 h-2 rounded-full mt-2 ${insight.priority === 'high' ? 'bg-success' : 'bg-primary'}`} />
                <div>
                  <div className="text-sm font-medium text-white">{insight.title}</div>
                  <div className="text-sm text-text-secondary">{insight.text}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </motion.div>

      {/* Tag & Title Performance */}
      {(tagData || titleData) && (
        <TagPerformanceChart
          tagData={tagData || mockTagData}
          titleData={titleData}
        />
      )}

      {/* Fallback if no tag data from API */}
      {!tagData && !titleData && (
        <TagPerformanceChart
          tagData={mockTagData}
          titleData={mockTitleData}
        />
      )}
    </div>
  );
}

// Mock data for when API endpoints aren't available yet
const mockTagData = [
  { tagName: 'Deadlock', usageCount: 15, avgViewers: 145, avgRetention10m: 58, avgFollowerGain: 12, trend: 'up' as const, trendValue: 15, bestTimeSlot: '18:00-22:00', avgStreamDuration: 14400, categoryRank: 5 },
  { tagName: 'German', usageCount: 15, avgViewers: 142, avgRetention10m: 55, avgFollowerGain: 10, trend: 'stable' as const, trendValue: 2, bestTimeSlot: '19:00-23:00', avgStreamDuration: 14000, categoryRank: 8 },
  { tagName: 'Competitive', usageCount: 8, avgViewers: 165, avgRetention10m: 62, avgFollowerGain: 15, trend: 'up' as const, trendValue: 22, bestTimeSlot: '20:00-24:00', avgStreamDuration: 12000, categoryRank: 3 },
  { tagName: 'Ranked', usageCount: 6, avgViewers: 158, avgRetention10m: 60, avgFollowerGain: 14, trend: 'down' as const, trendValue: -5, bestTimeSlot: '18:00-22:00', avgStreamDuration: 10800, categoryRank: 6 },
  { tagName: 'Chill', usageCount: 4, avgViewers: 95, avgRetention10m: 48, avgFollowerGain: 6, trend: 'stable' as const, trendValue: 0, bestTimeSlot: '14:00-18:00', avgStreamDuration: 18000, categoryRank: 15 },
];

const mockTitleData = [
  { title: 'Ranked Grind bis Phantom! !discord', usageCount: 5, avgViewers: 168, avgRetention10m: 62, avgFollowerGain: 18, peakViewers: 245, keywords: ['Ranked', 'Grind', 'Phantom'] },
  { title: 'Chill Deadlock mit Zuschauern', usageCount: 4, avgViewers: 125, avgRetention10m: 55, avgFollowerGain: 8, peakViewers: 180, keywords: ['Chill', 'Zuschauer'] },
  { title: 'Road to Top 500 | Tag 42', usageCount: 3, avgViewers: 195, avgRetention10m: 68, avgFollowerGain: 22, peakViewers: 312, keywords: ['Road', 'Top 500', 'Tag'] },
];

interface MetricRowProps {
  label: string;
  value: string;
  unit?: string;
  isPositive?: boolean;
}

function MetricRow({ label, value, unit, isPositive }: MetricRowProps) {
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-text-secondary">{label}</span>
      <span className={`font-medium ${isPositive !== undefined ? (isPositive ? 'text-success' : 'text-error') : 'text-white'}`}>
        {value}{unit && <span className="text-text-secondary ml-0.5">{unit}</span>}
      </span>
    </div>
  );
}

function getBestWorstDays(data: WeekdayStats[]) {
  if (!data.length) return [];

  const sorted = [...data].sort((a, b) => b.avgViewers - a.avgViewers);
  const best = sorted[0];
  const worst = sorted[sorted.length - 1];

  const sortedByStreams = [...data].sort((a, b) => b.streamCount - a.streamCount);
  const mostActive = sortedByStreams[0];

  return [
    { type: 'best', label: 'Beste Viewer', day: best.weekdayLabel, value: `Ø ${Math.round(best.avgViewers)}` },
    { type: 'worst', label: 'Schwächste Viewer', day: worst.weekdayLabel, value: `Ø ${Math.round(worst.avgViewers)}` },
    { type: 'best', label: 'Meist gestreamt', day: mostActive.weekdayLabel, value: `${mostActive.streamCount} Streams` },
    { type: 'best', label: 'Längste Streams', day: data.reduce((a, b) => a.avgHours > b.avgHours ? a : b).weekdayLabel, value: `Ø ${data.reduce((a, b) => a.avgHours > b.avgHours ? a : b).avgHours.toFixed(1)}h` },
  ];
}

function generateScheduleInsights(data: WeekdayStats[]) {
  const insights = [];
  const sorted = [...data].sort((a, b) => b.avgViewers - a.avgViewers);
  const best = sorted[0];
  const underperforming = data.filter(d => d.streamCount > 0 && d.avgViewers < sorted[Math.floor(sorted.length / 2)]?.avgViewers);

  insights.push({
    priority: 'high',
    title: `Fokus auf ${best.weekdayLabel}`,
    text: `${best.weekdayLabel} zeigt die besten Viewer-Zahlen. Plane wichtige Content-Events an diesem Tag.`,
  });

  if (underperforming.length > 0) {
    insights.push({
      priority: 'medium',
      title: 'Optimierungspotential',
      text: `${underperforming.map(d => d.weekdayLabel).join(', ')} haben unterdurchschnittliche Performance. Experimentiere mit anderen Zeiten.`,
    });
  }

  const noStreams = data.filter(d => d.streamCount === 0);
  if (noStreams.length > 0) {
    insights.push({
      priority: 'medium',
      title: 'Ungenutzte Tage',
      text: `Keine Streams an ${noStreams.map(d => d.weekdayLabel).join(', ')}. Teste diese Slots!`,
    });
  }

  return insights;
}

export default Growth;
