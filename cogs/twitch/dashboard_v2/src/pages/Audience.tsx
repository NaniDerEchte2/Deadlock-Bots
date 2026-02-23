import { motion } from 'framer-motion';
import { Users, AlertCircle, Loader2, TrendingUp, TrendingDown, Target, Clock, UserPlus } from 'lucide-react';
import { useWatchTimeDistribution, useFollowerFunnel, useTagAnalysisExtended, useTitlePerformance, useAudienceDemographics } from '@/hooks/useAnalytics';
import { WatchTimeDistribution } from '@/components/charts/WatchTimeDistribution';
import { FollowerFunnel } from '@/components/charts/FollowerFunnel';
import { TagPerformanceChart } from '@/components/charts/TagPerformance';
import { AudienceDemographics } from '@/components/charts/AudienceDemographics';
import type { TimeRange } from '@/types/analytics';

interface AudienceProps {
  streamer: string;
  days: TimeRange;
}

export function Audience({ streamer, days }: AudienceProps) {
  const {
    data: watchTime,
    isLoading: loadingWatchTime,
    isError: watchTimeError,
    error: watchTimeErrorDetail,
    refetch: refetchWatchTime,
  } = useWatchTimeDistribution(streamer, days);
  const {
    data: funnel,
    isLoading: loadingFunnel,
    isError: funnelError,
    error: funnelErrorDetail,
    refetch: refetchFunnel,
  } = useFollowerFunnel(streamer, days);
  const {
    data: tags,
    isLoading: loadingTags,
    isError: tagsError,
    error: tagsErrorDetail,
    refetch: refetchTags,
  } = useTagAnalysisExtended(streamer, days);
  const {
    data: titles,
    isLoading: loadingTitles,
    isError: titlesError,
    error: titlesErrorDetail,
    refetch: refetchTitles,
  } = useTitlePerformance(streamer, days);
  const {
    data: demographics,
    isLoading: loadingDemographics,
    isError: demographicsError,
    error: demographicsErrorDetail,
    refetch: refetchDemographics,
  } = useAudienceDemographics(streamer, days);

  const isLoading = loadingWatchTime || loadingFunnel || loadingTags || loadingTitles || loadingDemographics;
  const failedQueries = [
    { label: 'Watch Time', isError: watchTimeError, error: watchTimeErrorDetail, retry: refetchWatchTime },
    { label: 'Follower-Funnel', isError: funnelError, error: funnelErrorDetail, retry: refetchFunnel },
    { label: 'Tags', isError: tagsError, error: tagsErrorDetail, retry: refetchTags },
    { label: 'Titel', isError: titlesError, error: titlesErrorDetail, retry: refetchTitles },
    { label: 'Demographics', isError: demographicsError, error: demographicsErrorDetail, retry: refetchDemographics },
  ].filter(q => q.isError);
  const formatError = (err: unknown) => (err instanceof Error ? err.message : 'Unbekannter Fehler');
  const retryFailed = () => failedQueries.forEach(q => q.retry());

  if (!streamer) {
    return (
      <div className="flex flex-col items-center justify-center h-64">
        <AlertCircle className="w-12 h-12 text-text-secondary mb-4" />
        <p className="text-text-secondary text-lg">Wähle einen Streamer aus</p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  const watchTimeData = watchTime ?? null;
  const funnelData = funnel ?? null;
  const tagData = tags ?? null;
  const titleData = titles ?? null;
  const demographicsData = demographics ?? null;
  const noData =
    !watchTimeData && !funnelData && !tagData && !titleData && !demographicsData && failedQueries.length === 0;

  return (
    <div className="space-y-6">
      {failedQueries.length > 0 && (
        <div className="bg-error/10 border border-error/30 rounded-lg p-4 text-error space-y-2">
          <div className="flex items-center gap-2 font-semibold">
            <AlertCircle className="w-5 h-5" />
            <span>Daten konnten nicht vollständig geladen werden.</span>
          </div>
          <ul className="text-sm text-error/80 list-disc pl-5 space-y-1">
            {failedQueries.map(q => (
              <li key={q.label}>{q.label}: {formatError(q.error)}</li>
            ))}
          </ul>
          <button
            onClick={retryFailed}
            className="px-3 py-1.5 rounded-md bg-error/20 text-error text-sm font-semibold hover:bg-error/30 transition"
          >
            Erneut laden
          </button>
        </div>
      )}

      {noData && (
        <div className="flex flex-col items-center justify-center h-48 text-center space-y-2 border border-border rounded-lg">
          <AlertCircle className="w-8 h-8 text-text-secondary" />
          <p className="text-white font-medium">Keine Daten für diesen Zeitraum.</p>
          <p className="text-sm text-text-secondary">Bitte Zeitraum oder Streamer anpassen und erneut versuchen.</p>
        </div>
      )}

      {/* Header Stats */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="grid grid-cols-2 md:grid-cols-4 gap-4"
      >
        <QuickStatCard
          icon={<Clock className="w-5 h-5" />}
          label="Ø Watch Time"
          value={watchTimeData ? `${watchTimeData.avgWatchTime.toFixed(0)} Min` : '—'}
          color="primary"
        />
        <QuickStatCard
          icon={<Target className="w-5 h-5" />}
          label="Conversion Rate"
          value={funnelData ? `${funnelData.conversionRate.toFixed(2)}%` : '—'}
          color="success"
        />
        <QuickStatCard
          icon={<Users className="w-5 h-5" />}
          label="Unique Viewer"
          value={funnelData ? funnelData.uniqueViewers.toLocaleString('de-DE') : '—'}
          color="accent"
        />
        <QuickStatCard
          icon={<UserPlus className="w-5 h-5" />}
          label="Neue Follower"
          value={funnelData ? `+${funnelData.newFollowers}` : '—'}
          color="warning"
        />
      </motion.div>

      {/* Watch Time & Funnel Side by Side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {watchTimeData ? (
          <WatchTimeDistribution data={watchTimeData} />
        ) : (
          <ErrorPlaceholder label="Watch Time" onRetry={refetchWatchTime} />
        )}
        {funnelData ? (
          <FollowerFunnel data={funnelData} />
        ) : (
          <ErrorPlaceholder label="Follower-Funnel" onRetry={refetchFunnel} />
        )}
      </div>

      {/* Tag & Title Performance */}
      {tagData ? (
        <TagPerformanceChart tagData={tagData} titleData={titleData || undefined} />
      ) : (
        <ErrorPlaceholder label="Tag & Title Performance" onRetry={refetchTags} />
      )}

      {/* Audience Demographics */}
      {demographicsData ? (
        <AudienceDemographics data={demographicsData} />
      ) : (
        <ErrorPlaceholder label="Audience Demographics" onRetry={refetchDemographics} />
      )}

      {/* Audience Insights Summary */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.3 }}
        className="bg-gradient-to-r from-primary/10 to-accent/10 rounded-xl border border-primary/20 p-6"
      >
        <h3 className="text-lg font-bold text-white mb-4 flex items-center gap-2">
          <Target className="w-5 h-5 text-primary" />
          Audience Insights Zusammenfassung
        </h3>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {/* Watch Time Insight */}
          <InsightCard
            title="Viewer Engagement"
            description={
              watchTimeData && watchTimeData.avgWatchTime > 0
                ? watchTimeData.avgWatchTime > 25
                  ? `Starkes Engagement! Deine Viewer bleiben im Schnitt ${watchTimeData.avgWatchTime.toFixed(0)} Minuten.`
                  : `Verbesserungspotential: Viewer bleiben nur ${watchTimeData.avgWatchTime.toFixed(0)} Min - teste interaktive Segmente.`
                : 'Keine Watch-Time-Daten verfügbar.'
            }
            type={watchTimeData && watchTimeData.avgWatchTime > 25 ? 'success' : 'warning'}
          />

          {/* Conversion Insight */}
          <InsightCard
            title="Follower Conversion"
            description={
              funnelData && funnelData.conversionRate > 0
                ? funnelData.conversionRate > 5
                  ? `Exzellente Conversion von ${funnelData.conversionRate.toFixed(2)}%! Dein Content überzeugt.`
                  : `Conversion bei ${funnelData.conversionRate.toFixed(2)}% - nutze mehr Call-to-Actions.`
                : 'Keine Conversion-Daten verfügbar.'
            }
            type={funnelData && funnelData.conversionRate > 5 ? 'success' : 'info'}
          />

          {/* Tag Insight */}
          {tagData && tagData.length > 0 && (
            <InsightCard
              title="Content Strategie"
              description={`"${tagData[0].tagName}" performt am besten. Fokussiere dich auf diesen Content-Typ für maximale Reichweite.`}
              type="info"
            />
          )}
        </div>
      </motion.div>
    </div>
  );
}

interface QuickStatCardProps {
  icon: React.ReactNode;
  label: string;
  value: string;
  color: 'primary' | 'success' | 'accent' | 'warning';
  trend?: number;
}

function QuickStatCard({ icon, label, value, color, trend }: QuickStatCardProps) {
  const colorClasses = {
    primary: 'bg-primary/10 text-primary',
    success: 'bg-success/10 text-success',
    accent: 'bg-accent/10 text-accent',
    warning: 'bg-warning/10 text-warning',
  };

  const TrendIcon = trend === undefined ? null : trend >= 0 ? TrendingUp : TrendingDown;
  const trendColor = trend === undefined ? '' : trend >= 0 ? 'text-success' : 'text-error';

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      className="bg-card rounded-xl border border-border p-4"
    >
      <div className={`w-10 h-10 rounded-lg ${colorClasses[color]} flex items-center justify-center mb-3`}>
        {icon}
      </div>
      <div className="text-sm text-text-secondary mb-1">{label}</div>
      <div className="flex items-center gap-2">
        <span className="text-xl font-bold text-white">{value}</span>
        {TrendIcon && (
          <span className={`flex items-center gap-1 text-xs ${trendColor}`}>
            <TrendIcon className="w-3 h-3" />
            {Math.abs(trend!).toFixed(1)}%
          </span>
        )}
      </div>
    </motion.div>
  );
}

interface InsightCardProps {
  title: string;
  description: string;
  type: 'success' | 'warning' | 'info';
}

function InsightCard({ title, description, type }: InsightCardProps) {
  const styles = {
    success: 'bg-success/10 border-success/20',
    warning: 'bg-warning/10 border-warning/20',
    info: 'bg-primary/10 border-primary/20',
  };

  const iconStyles = {
    success: 'text-success',
    warning: 'text-warning',
    info: 'text-primary',
  };

  const Icon = type === 'success' ? TrendingUp : type === 'warning' ? AlertCircle : Target;

  return (
    <div className={`p-4 rounded-lg border ${styles[type]}`}>
      <div className="flex items-center gap-2 mb-2">
        <Icon className={`w-4 h-4 ${iconStyles[type]}`} />
        <span className="font-medium text-white text-sm">{title}</span>
      </div>
      <p className="text-sm text-text-secondary">{description}</p>
    </div>
  );
}

function ErrorPlaceholder({ label, onRetry }: { label: string; onRetry: () => void }) {
  return (
    <div className="flex items-center justify-between h-full bg-card border border-border rounded-xl p-4 text-text-secondary">
      <div className="flex items-center gap-2">
        <AlertCircle className="w-5 h-5" />
        <span>{label} konnten nicht geladen werden.</span>
      </div>
      <button
        onClick={onRetry}
        className="px-3 py-1 rounded-md bg-primary/10 text-primary text-sm font-semibold hover:bg-primary/20 transition"
      >
        Retry
      </button>
    </div>
  );
}

export default Audience;
