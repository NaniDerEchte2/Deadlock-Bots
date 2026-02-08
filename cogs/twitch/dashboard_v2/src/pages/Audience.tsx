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
  const { data: watchTime, isLoading: loadingWatchTime } = useWatchTimeDistribution(streamer, days);
  const { data: funnel, isLoading: loadingFunnel } = useFollowerFunnel(streamer, days);
  const { data: tags, isLoading: loadingTags } = useTagAnalysisExtended(streamer, days);
  const { data: titles, isLoading: loadingTitles } = useTitlePerformance(streamer, days);
  const { data: demographics, isLoading: loadingDemographics } = useAudienceDemographics(streamer, days);

  const isLoading = loadingWatchTime || loadingFunnel || loadingTags || loadingTitles || loadingDemographics;

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

  // Generate mock data if API endpoints don't exist yet
  const mockWatchTime = watchTime || {
    under5min: 25,
    min5to15: 20,
    min15to30: 18,
    min30to60: 22,
    over60min: 15,
    avgWatchTime: 28.5,
    medianWatchTime: 22,
  };

  const mockFunnel = funnel || {
    uniqueViewers: 1250,
    returningViewers: 480,
    newFollowers: 85,
    netFollowerDelta: 85,
    conversionRate: 6.8,
    avgTimeToFollow: 45,
    followersBySource: {
      organic: 52,
      raids: 18,
      hosts: 8,
      other: 7,
    },
  };

  const mockTags = tags || [
    { tagName: 'Deadlock', usageCount: 15, avgViewers: 145, avgRetention10m: 58, avgFollowerGain: 12, trend: 'up' as const, trendValue: 15, bestTimeSlot: '18:00-22:00', avgStreamDuration: 14400, categoryRank: 5 },
    { tagName: 'German', usageCount: 15, avgViewers: 142, avgRetention10m: 55, avgFollowerGain: 10, trend: 'stable' as const, trendValue: 2, bestTimeSlot: '19:00-23:00', avgStreamDuration: 14000, categoryRank: 8 },
    { tagName: 'Competitive', usageCount: 8, avgViewers: 165, avgRetention10m: 62, avgFollowerGain: 15, trend: 'up' as const, trendValue: 22, bestTimeSlot: '20:00-24:00', avgStreamDuration: 12000, categoryRank: 3 },
    { tagName: 'Ranked', usageCount: 6, avgViewers: 158, avgRetention10m: 60, avgFollowerGain: 14, trend: 'down' as const, trendValue: -5, bestTimeSlot: '18:00-22:00', avgStreamDuration: 10800, categoryRank: 6 },
    { tagName: 'Chill', usageCount: 4, avgViewers: 95, avgRetention10m: 48, avgFollowerGain: 6, trend: 'stable' as const, trendValue: 0, bestTimeSlot: '14:00-18:00', avgStreamDuration: 18000, categoryRank: 15 },
  ];

  const mockTitles = titles || [
    { title: 'Ranked Grind bis Phantom! !discord', usageCount: 5, avgViewers: 168, avgRetention10m: 62, avgFollowerGain: 18, peakViewers: 245, keywords: ['Ranked', 'Grind', 'Phantom'] },
    { title: 'Chill Deadlock mit Zuschauern', usageCount: 4, avgViewers: 125, avgRetention10m: 55, avgFollowerGain: 8, peakViewers: 180, keywords: ['Chill', 'Zuschauer'] },
    { title: 'Road to Top 500 | Tag 42', usageCount: 3, avgViewers: 195, avgRetention10m: 68, avgFollowerGain: 22, peakViewers: 312, keywords: ['Road', 'Top 500', 'Tag'] },
  ];

  const mockDemographics = demographics || {
    estimatedRegions: [
      { region: 'DACH', percentage: 65 },
      { region: 'Rest EU', percentage: 20 },
      { region: 'NA', percentage: 10 },
      { region: 'Other', percentage: 5 },
    ],
    viewerTypes: [
      { label: 'Dedicated Fans', percentage: 30 },
      { label: 'Regular Viewers', percentage: 35 },
      { label: 'Casual Viewers', percentage: 25 },
      { label: 'New Visitors', percentage: 10 },
    ],
    activityPattern: 'weekday-focused' as const,
    primaryLanguage: 'German',
    languageConfidence: 85,
    peakActivityHours: [19, 20, 21],
    interactiveRate: 12.5,
    loyaltyScore: 42,
  };

  return (
    <div className="space-y-6">
      {/* Header Stats */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="grid grid-cols-2 md:grid-cols-4 gap-4"
      >
        <QuickStatCard
          icon={<Clock className="w-5 h-5" />}
          label="Ø Watch Time"
          value={`${mockWatchTime.avgWatchTime.toFixed(0)} Min`}
          color="primary"
        />
        <QuickStatCard
          icon={<Target className="w-5 h-5" />}
          label="Conversion Rate"
          value={`${mockFunnel.conversionRate.toFixed(2)}%`}
          color="success"
        />
        <QuickStatCard
          icon={<Users className="w-5 h-5" />}
          label="Unique Viewer"
          value={mockFunnel.uniqueViewers.toLocaleString('de-DE')}
          color="accent"
        />
        <QuickStatCard
          icon={<UserPlus className="w-5 h-5" />}
          label="Neue Follower"
          value={`+${mockFunnel.newFollowers}`}
          color="warning"
        />
      </motion.div>

      {/* Watch Time & Funnel Side by Side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <WatchTimeDistribution data={mockWatchTime} />
        <FollowerFunnel data={mockFunnel} />
      </div>

      {/* Tag & Title Performance */}
      <TagPerformanceChart tagData={mockTags} titleData={mockTitles} />

      {/* Audience Demographics */}
      <AudienceDemographics data={mockDemographics} />

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
              mockWatchTime.avgWatchTime > 25
                ? `Starkes Engagement! Deine Viewer bleiben im Schnitt ${mockWatchTime.avgWatchTime.toFixed(0)} Minuten.`
                : `Verbesserungspotential: Viewer bleiben nur ${mockWatchTime.avgWatchTime.toFixed(0)} Min - teste interaktive Segmente.`
            }
            type={mockWatchTime.avgWatchTime > 25 ? 'success' : 'warning'}
          />

          {/* Conversion Insight */}
          <InsightCard
            title="Follower Conversion"
            description={
              mockFunnel.conversionRate > 5
                ? `Exzellente Conversion von ${mockFunnel.conversionRate.toFixed(2)}%! Dein Content überzeugt.`
                : `Conversion bei ${mockFunnel.conversionRate.toFixed(2)}% - nutze mehr Call-to-Actions.`
            }
            type={mockFunnel.conversionRate > 5 ? 'success' : 'info'}
          />

          {/* Tag Insight */}
          {mockTags.length > 0 && (
            <InsightCard
              title="Content Strategie"
              description={`"${mockTags[0].tagName}" performt am besten. Fokussiere dich auf diesen Content-Typ für maximale Reichweite.`}
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

export default Audience;
