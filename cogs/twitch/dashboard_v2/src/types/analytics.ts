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

export type TimeRange = 7 | 30 | 90 | 365;
