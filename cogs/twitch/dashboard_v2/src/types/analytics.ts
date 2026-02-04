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
    retention10m: number;
    chatHealth: number;
  };
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
  scores: HealthScore;
  summary: {
    avgViewers: number;
    peakViewers: number;
    totalHoursWatched: number;
    totalAirtime: number;
    followersDelta: number;
    followersPerHour: number;
    retention10m: number;
    uniqueChatters: number;
    streamCount: number;
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

// API Response Types
export interface ApiResponse<T> {
  data: T;
  error?: string;
  empty?: boolean;
}

export type TimeRange = 7 | 30 | 90 | 365;
