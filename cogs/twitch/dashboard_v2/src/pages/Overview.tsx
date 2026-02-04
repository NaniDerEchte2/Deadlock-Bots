import { Users, TrendingUp, Clock, MessageSquare, Target } from 'lucide-react';
import { useOverview, useHourlyHeatmap, useCalendarHeatmap } from '@/hooks/useAnalytics';
import { KpiCard } from '@/components/cards/KpiCard';
import { HealthScoreCard } from '@/components/cards/HealthScoreCard';
import { ScoreGauge } from '@/components/cards/ScoreGauge';
import { ViewerTrendChart } from '@/components/charts/ViewerTrendChart';
import { RetentionRadar } from '@/components/charts/RetentionRadar';
import { SessionTable } from '@/components/tables/SessionTable';
import { HourlyHeatmap } from '@/components/heatmaps/HourlyHeatmap';
import { CalendarHeatmap } from '@/components/heatmaps/CalendarHeatmap';
import { InsightsPanel } from '@/components/cards/InsightsPanel';
import { formatNumber, formatPercent, formatHours } from '@/utils/formatters';
import type { TimeRange } from '@/types/analytics';

interface OverviewProps {
  streamer: string | null;
  days: TimeRange;
  onSessionClick?: (sessionId: number) => void;
}

export function Overview({ streamer, days, onSessionClick }: OverviewProps) {
  const { data: overview, isLoading, error } = useOverview(streamer, days);
  const { data: hourlyData } = useHourlyHeatmap(streamer, days);
  const { data: calendarData } = useCalendarHeatmap(streamer, 365);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <div className="flex items-center gap-3 text-accent">
          <div className="w-8 h-8 border-2 border-accent border-t-transparent rounded-full animate-spin" />
          <span>Lade Analytics...</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-8 text-center">
        <h2 className="text-danger text-xl font-bold mb-2">Fehler beim Laden</h2>
        <p className="text-text-secondary">{(error as Error).message}</p>
      </div>
    );
  }

  if (!overview) {
    return (
      <div className="p-8 text-center text-text-secondary">
        Keine Daten verfügbar
      </div>
    );
  }

  const { scores, summary, sessions, findings, actions, network } = overview;

  return (
    <div className="space-y-8">
      {/* Top KPIs */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4">
        <HealthScoreCard scores={scores} />

        <KpiCard
          title="Ø Viewers"
          value={formatNumber(summary.avgViewers, 1)}
          subValue={`Peak: ${formatNumber(summary.peakViewers)}`}
          trend={summary.avgViewersTrend}
          icon={Users}
          color="blue"
        />

        <KpiCard
          title="Hours Watched"
          value={formatHours(summary.totalHoursWatched)}
          subValue={`${formatHours(summary.totalAirtime)} Airtime`}
          icon={Clock}
          color="purple"
        />

        <KpiCard
          title="Follower"
          value={`+${formatNumber(summary.followersDelta)}`}
          subValue={`${summary.followersPerHour.toFixed(2)} / Stunde`}
          trend={summary.followersTrend}
          icon={TrendingUp}
          color="green"
        />

        <KpiCard
          title="Retention (10m)"
          value={formatPercent(summary.retention10m)}
          subValue="Ziel: >40%"
          trend={summary.retentionTrend}
          icon={Target}
          color="yellow"
        />
      </div>

      {/* Insights */}
      {findings && findings.length > 0 && (
        <InsightsPanel findings={findings} actions={actions} />
      )}

      {/* Charts Row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <ViewerTrendChart sessions={sessions} title="Viewer Entwicklung" />
        </div>
        <div>
          <RetentionRadar scores={scores} title="Performance Mix" />
        </div>
      </div>

      {/* Heatmaps Row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {hourlyData && <HourlyHeatmap data={hourlyData} />}
        {calendarData && <CalendarHeatmap data={calendarData} />}
      </div>

      {/* Score Gauges & Network */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Score Gauges */}
        <div className="lg:col-span-2 grid grid-cols-2 sm:grid-cols-4 gap-4">
          <ScoreGauge score={scores.growth} label="Growth" />
          <ScoreGauge score={scores.monetization} label="Revenue" />
          <ScoreGauge score={scores.network} label="Network" />
          <ScoreGauge score={scores.retention} label="Retention" />
        </div>

        {/* Network Activity */}
        <div className="bg-card p-5 rounded-xl border border-border">
          <h4 className="text-sm font-bold text-text-secondary mb-4 uppercase tracking-wide flex items-center gap-2">
            <MessageSquare className="w-4 h-4" />
            Raid-Aktivität
          </h4>
          <div className="space-y-3">
            <div className="flex justify-between items-center p-3 bg-black/20 rounded-lg">
              <span className="text-text-secondary text-sm">Raids gesendet</span>
              <span className="text-white font-bold">{network?.sent ?? 0}</span>
            </div>
            <div className="flex justify-between items-center p-3 bg-black/20 rounded-lg">
              <span className="text-text-secondary text-sm">Raids erhalten</span>
              <span className="text-white font-bold">{network?.received ?? 0}</span>
            </div>
            <div className="flex justify-between items-center p-3 bg-black/20 rounded-lg">
              <span className="text-text-secondary text-sm">Viewer weitergeleitet</span>
              <span className="text-white font-bold">{formatNumber(network?.sentViewers ?? 0)}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Sessions Table */}
      <SessionTable
        sessions={sessions}
        limit={10}
        onSessionClick={onSessionClick}
      />
    </div>
  );
}
