// Core Analytics Types for Twitch Dashboard

export interface StreamSession {
  id: number;
  date: string;
  startTime: string;
  duration: number; // seconds
  startViewers: number;
  peakViewers: number;
  endViewers: number;
  avgViewers: number;
  retention5m: number;
  retention10m: number;
  retention20m: number;
  dropoffPct: number;
  uniqueChatters: number;
  firstTimeChatters: number;
  returningChatters: number;
  followersStart: number;
  followersEnd: number;
  title: string;
}

export interface DailyStats {
  date: string;
  hoursWatched: number;
  airtime: number;
  avgViewers: number;
  peakViewers: number;
  followerDelta: number;
  uniqueChatters: number;
  streamCount: number;
}

export interface MonthlyStats {
  year: number;
  month: number;
  monthLabel: string;
  totalHoursWatched: number;
  totalAirtime: number;
  avgViewers: number;
  peakViewers: number;
  followerDelta: number;
  uniqueChatters: number;
  streamCount: number;
}

export interface WeekdayStats {
  weekday: number; // 0-6 (Sunday-Saturday)
  weekdayLabel: string;
  streamCount: number;
  avgHours: number;
  avgViewers: number;
  avgPeak: number;
  totalFollowers: number;
}

export interface HourlyHeatmapData {
  weekday: number;
  hour: number;
  streamCount: number;
  avgViewers: number;
  avgPeak: number;
}

export interface CalendarHeatmapData {
  date: string;
  value: number; // hours watched or stream count
  streamCount: number;
  hoursWatched: number;
}

export interface ChatAnalytics {
  totalMessages: number;
  uniqueChatters: number;
  firstTimeChatters: number;
  returningChatters: number;
  messagesPerMinute: number;
  chatterReturnRate: number;
  topChatters: ChatterStats[];
}

export interface ChatterStats {
  login: string;
  totalMessages: number;
  totalSessions: number;
  firstSeen: string;
  lastSeen: string;
  loyaltyScore: number;
}

export interface ViewerOverlap {
  streamerA: string;
  streamerB: string;
  sharedChatters: number;
  totalChattersA: number;
  totalChattersB: number;
  overlapPercentage: number;
}

export interface CategoryComparison {
  yourStats: {
    avgViewers: number;
    peakViewers: number;
    retention10m: number;
    chatHealth: number;
  };
  categoryAvg: {
    avgViewers: number;
    peakViewers: number;
    retention10m: number;
    chatHealth: number;
  };
  percentiles: {
    avgViewers: number;
    peakViewers?: number;
    retention10m: number;
    chatHealth: number;
  };
  categoryRank?: number;
  categoryTotal?: number;
}

export interface TagPerformance {
  tagName: string;
  usageCount: number;
  avgViewers: number;
  avgRetention10m: number;
  avgFollowerGain: number;
}

export interface GrowthMetrics {
  followerGrowthRate: number;
  viewerGrowthRate: number;
  newViewerRate: number;
  returningViewerRate: number;
  weeklyTrend: TrendPoint[];
}

export interface TrendPoint {
  date: string;
  value: number;
  change: number;
}

export interface HealthScore {
  total: number;
  reach: number;
  retention: number;
  engagement: number;
  growth: number;
  monetization: number;
  network: number;
}

export interface DashboardOverview {
  streamer: string;
  days: number;
  empty?: boolean;
  error?: string;
  scores: HealthScore;
  summary: {
    avgViewers: number;
    peakViewers: number;
    totalHoursWatched: number;
    totalAirtime: number;
    followersDelta: number;
    followersGained?: number;
    followersPerHour: number;
    followersGainedPerHour?: number;
    retention10m: number;
    retentionReliable?: boolean;
    uniqueChatters: number;
    streamCount: number;
    // Neue Trend-Felder
    avgViewersTrend?: number;      // % Änderung vs. Vorperiode
    peakViewersTrend?: number;
    followersTrend?: number;
    retentionTrend?: number;
  };
  sessions: StreamSession[];
  findings: Insight[];
  actions: ActionItem[];
  correlations: {
    durationVsViewers: number;
    chatVsRetention: number;
  };
  network: {
    sent: number;
    received: number;
    sentViewers: number;
  };
  // Category Ranking
  categoryRank?: number;
  categoryTotal?: number;
  // Neue Audience Insights
  audienceInsights?: AudienceInsights;
}

export interface Insight {
  type: 'pos' | 'neg' | 'warn' | 'info';
  title: string;
  text: string;
}

export interface ActionItem {
  tag: string;
  text: string;
  priority: 'high' | 'medium' | 'low';
}

export interface StreamerInfo {
  login: string;
  displayName: string;
  isPartner: boolean;
  isOnDiscord: boolean;
  lastDeadlockStream: string | null;
}

export interface RankingEntry {
  rank: number;
  login: string;
  value: number;
  trend: 'up' | 'down' | 'same';
  trendValue: number;
}

export interface AudienceBreakdown {
  interactive: number;
  passive: number;
  interactionRate: number;
  estimatedLanguage: string;
  languageConfidence: number;
}

// Watch Time Distribution - Wie lange bleiben Viewer?
export interface WatchTimeDistribution {
  under5min: number;      // Schnelle Absprünge (%)
  min5to15: number;       // Kurze Sessions (%)
  min15to30: number;      // Mittlere Sessions (%)
  min30to60: number;      // Längere Sessions (%)
  over60min: number;      // Loyale Zuschauer (%)
  avgWatchTime: number;   // Durchschnittliche Watch Time in Minuten
  medianWatchTime: number; // Median Watch Time in Minuten
  sessionCount?: number;
  previous?: {
    under5min: number;
    min5to15: number;
    min15to30: number;
    min30to60: number;
    over60min: number;
    avgWatchTime: number;
    medianWatchTime: number;
    sessionCount?: number;
  };
  deltas?: {
    under5min: number | null;
    min5to15: number | null;
    min15to30: number | null;
    min30to60: number | null;
    over60min: number | null;
    avgWatchTime: number | null;
  };
}

// Follower Conversion Funnel - Von Viewer zu Follower
export interface FollowerFunnel {
  uniqueViewers: number;        // Einzigartige Viewer im Zeitraum
  returningViewers: number;     // Wiederkehrende Viewer (nicht gefolgt)
  newFollowers: number;         // Gewonnene Follower (nur positive Session-Deltas)
  netFollowerDelta: number;     // Netto-Änderung (kann negativ sein: Follows - Unfollows)
  conversionRate: number;       // newFollowers / uniqueViewers * 100
  avgTimeToFollow: number;      // Durchschnittliche Zeit bis Follow (Minuten)
  followersBySource: {
    organic: number;            // Direkt über Stream
    raids: number;              // Über Raids
    hosts: number;              // Über Hosts
    other: number;              // Sonstige
  };
}

// Erweiterte Tag Performance mit Trends
export interface TagPerformanceExtended extends TagPerformance {
  trend: 'up' | 'down' | 'stable';
  trendValue: number;           // % Änderung
  bestTimeSlot: string;         // z.B. "18:00-22:00"
  avgStreamDuration: number;    // Durchschnittliche Stream-Dauer mit diesem Tag
  categoryRank: number;         // Rang in der Kategorie für diesen Tag
}

// Title Performance - Welche Titel performen besser?
export interface TitlePerformance {
  title: string;
  usageCount: number;
  avgViewers: number;
  avgRetention10m: number;
  avgFollowerGain: number;
  peakViewers: number;
  keywords: string[];           // Extrahierte Keywords
}

// Kombinierte Funnel & Distribution Daten
export interface AudienceInsights {
  watchTimeDistribution: WatchTimeDistribution;
  followerFunnel: FollowerFunnel;
  tagPerformance: TagPerformanceExtended[];
  titlePerformance: TitlePerformance[];
  // Trends im Vergleich zur Vorperiode
  trends: {
    watchTimeChange: number;      // % Änderung avg watch time
    conversionChange: number;     // % Änderung conversion rate
    viewerReturnRate: number;     // % der Viewer die zurückkommen
    viewerReturnChange: number;   // % Änderung return rate
  };
}

// API Response Types
export interface ApiResponse<T> {
  data: T;
  error?: string;
  empty?: boolean;
}

// Viewer Timeline (from twitch_stats_tracked)
export interface ViewerTimelinePoint {
  timestamp: string;
  avgViewers: number;
  peakViewers: number;
  minViewers: number;
  samples: number;
}

// Category Leaderboard (from twitch_stats_category)
export interface LeaderboardEntry {
  rank: number;
  streamer: string;
  avgViewers: number;
  peakViewers: number;
  isPartner: boolean;
  isYou?: boolean;
}

export interface CategoryLeaderboard {
  leaderboard: LeaderboardEntry[];
  totalStreamers: number;
  yourRank: number | null;
}

// Coaching Types

export interface CoachingEfficiency {
  viewerHoursPerStreamHour: number;
  categoryAvg: number;
  topPerformers: { streamer: string; ratio: number }[];
  percentile: number;
  totalStreamHours: number;
  totalViewerHours: number;
}

export interface CoachingTitleEntry {
  title: string;
  avgViewers: number;
  peakViewers: number;
  chatters: number;
  usageCount: number;
}

export interface CoachingTitleAnalysis {
  yourTitles: CoachingTitleEntry[];
  categoryTopTitles: { title: string; streamer: string; avgViewers: number }[];
  yourMissingPatterns: string[];
  topPerformerPatterns: string[];
  varietyPct: number;
  uniqueTitleCount: number;
  totalSessionCount: number;
  avgPeerVarietyPct: number;
  peerVariety: { streamer: string; uniqueTitles: number; totalSessions: number; varietyPct: number }[];
}

export interface CoachingSweetSpot {
  weekday: number;
  hour: number;
  categoryViewers: number;
  competitors: number;
  opportunityScore: number;
}

export interface CoachingScheduleOptimizer {
  sweetSpots: CoachingSweetSpot[];
  yourCurrentSlots: { weekday: number; hour: number; count: number }[];
  competitionHeatmap: { weekday: number; hour: number; competitors: number; categoryViewers: number }[];
}

export interface CoachingDurationBucket {
  label: string;
  streamCount: number;
  avgViewers: number;
  avgChatters: number;
  avgRetention5m: number;
  efficiencyRatio: number;
}

export interface CoachingDurationAnalysis {
  buckets: CoachingDurationBucket[];
  optimalLabel: string;
  currentAvgHours: number;
  correlation: number;
}

export interface CoachingCrossCommunity {
  totalUniqueChatters: number;
  chatterSources: { sourceStreamer: string; sharedChatters: number; percentage: number }[];
  isolatedChatters: number;
  isolatedPercentage: number;
  ecosystemSummary: string;
}

export interface CoachingTagOptimization {
  yourTags: { tags: string; avgViewers: number; usageCount: number }[];
  categoryBestTags: { tags: string; avgViewers: number; streamerCount: number }[];
  missingHighPerformers: string[];
  underperformingTags: string[];
}

export interface CoachingRetention {
  your5mRetention: number;
  category5mRetention: number;
  yourViewerCurve: { minute: number; avgViewerPct: number }[];
  topPerformerCurve: { minute: number; avgViewerPct: number }[];
  criticalDropoffMinute: number;
}

export interface CoachingDoubleStream {
  detected: boolean;
  count: number;
  occurrences: { date: string; sessionCount: number; avgViewers: number }[];
  singleDayAvg: number;
  doubleDayAvg: number;
}

export interface CoachingRecommendation {
  priority: 'critical' | 'high' | 'medium' | 'low';
  category: string;
  title: string;
  description: string;
  estimatedImpact: string;
  evidence: string;
  icon: string;
}

export interface CoachingChatConcentration {
  totalChatters: number;
  totalMessages: number;
  msgsPerChatter: number;
  loyaltyBuckets: Record<string, { count: number; pct: number; messages: number }>;
  topChatters: { login: string; messages: number; sessions: number; sharePct: number; cumulativePct: number }[];
  concentrationIndex: number;
  top1Pct: number;
  top3Pct: number;
  ownOneTimerPct: number;
  avgPeerOneTimerPct: number;
}

export interface CoachingRaidPartner {
  login: string;
  sentCount: number;
  sentAvgViewers: number;
  receivedCount: number;
  receivedAvgViewers: number;
  reciprocity: 'mutual' | 'sentOnly' | 'receivedOnly';
  balance: number;
}

export interface CoachingRaidNetwork {
  totalSent: number;
  totalReceived: number;
  totalSentViewers: number;
  totalReceivedViewers: number;
  avgSentViewers: number;
  avgReceivedViewers: number;
  reciprocityRatio: number;
  mutualPartners: number;
  totalPartners: number;
  partners: CoachingRaidPartner[];
}

export interface CoachingPeerEntry {
  login: string;
  sessions: number;
  avgViewers: number;
  maxPeak: number;
  avgHours: number;
  avgChatters: number;
  retention5m: number;
  totalHours: number;
  followsGained: number;
  uniqueTitles: number;
  titleVariety: number;
}

export interface CoachingPeerComparison {
  ownData: CoachingPeerEntry | null;
  ownRank: number;
  totalStreamers: number;
  similarPeers: CoachingPeerEntry[];
  aspirationalPeers: CoachingPeerEntry[];
  metricsRanked: Record<string, { rank: number; total: number; value: number }>;
  gapToNext: { login: string; avgViewersDiff: number; chatDiff: number; retentionDiff: number } | null;
}

export interface CoachingCompetitionHourly {
  hour: number;
  activeStreamers: number;
  avgViewers: number;
  avgPeak: number;
  opportunityScore: number;
  yourData: { count: number; avgViewers: number; avgPeak: number; avgChatters: number } | null;
}

export interface CoachingCompetitionWeekly {
  weekday: number;
  weekdayLabel: string;
  activeStreamers: number;
  avgViewers: number;
  yourData: { count: number; avgViewers: number; avgPeak: number } | null;
}

export interface CoachingCompetitionDensity {
  hourly: CoachingCompetitionHourly[];
  weekly: CoachingCompetitionWeekly[];
  sweetSpots: CoachingCompetitionHourly[];
}

export interface CoachingData {
  streamer: string;
  days: number;
  empty?: boolean;
  efficiency: CoachingEfficiency;
  titleAnalysis: CoachingTitleAnalysis;
  scheduleOptimizer: CoachingScheduleOptimizer;
  durationAnalysis: CoachingDurationAnalysis;
  crossCommunity: CoachingCrossCommunity;
  tagOptimization: CoachingTagOptimization;
  retentionCoaching: CoachingRetention;
  doubleStreamDetection: CoachingDoubleStream;
  chatConcentration: CoachingChatConcentration;
  raidNetwork: CoachingRaidNetwork;
  peerComparison: CoachingPeerComparison;
  competitionDensity: CoachingCompetitionDensity;
  recommendations: CoachingRecommendation[];
  aiSummary: string | null;
}

export type TimeRange = 7 | 30 | 90 | 365;
export type { TabId } from '@/components/layout/TabNavigation';

// Category Timings (Median-basiert)
export interface TimingSlot {
  median: number | null;
  p25: number | null;
  p75: number | null;
  streamer_count: number;
  sample_count: number;
}

export interface HourlyTimingSlot extends TimingSlot {
  hour: number;
}

export interface WeeklyTimingSlot extends TimingSlot {
  weekday: number;
  label: string;
}

export interface CategoryTimings {
  hourly: HourlyTimingSlot[];
  weekly: WeeklyTimingSlot[];
  total_streamers: number;
  window_days: number;
  method: string;
}

export interface CategoryActivitySeriesRow {
  label: string;
  categoryAvg: number | null;
  trackedAvg: number | null;
  categoryPeak: number | null;
  trackedPeak: number | null;
  categorySamples: number;
  trackedSamples: number;
}

export interface CategoryActivityHourlyRow extends CategoryActivitySeriesRow {
  hour: number;
}

export interface CategoryActivityWeeklyRow extends CategoryActivitySeriesRow {
  weekday: number;
}

export interface CategoryActivitySeries {
  hourly: CategoryActivityHourlyRow[];
  weekly: CategoryActivityWeeklyRow[];
  windowDays: number;
  source: string;
}

// Monetization & Hype Train
export interface WorstAd {
  started_at: string;
  duration_s: number;
  drop_pct: number;
  is_automatic: boolean;
}

export interface MonetizationStats {
  ads: {
    total: number;
    auto: number;
    manual: number;
    sessions_with_ads: number;
    avg_duration_s: number;
    avg_viewer_drop_pct: number | null;
    worst_ads: WorstAd[];
  };
  hype_train: {
    total: number;
    avg_level: number;
    max_level: number;
    avg_duration_s: number;
  };
  bits: {
    total: number;
    cheer_events: number;
  };
  subs: {
    total_events: number;
    gifted: number;
  };
  window_days: number;
}
