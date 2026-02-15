// React Query hooks for analytics data

import { useQuery } from '@tanstack/react-query';
import {
  fetchOverview,
  fetchMonthlyStats,
  fetchWeekdayStats,
  fetchHourlyHeatmap,
  fetchCalendarHeatmap,
  fetchChatAnalytics,
  fetchViewerOverlap,
  fetchTagAnalysis,
  fetchTagAnalysisExtended,
  fetchTitlePerformance,
  fetchRankings,
  fetchSessionDetail,
  fetchStreamerList,
  fetchCategoryComparison,
  fetchAuthStatus,
  fetchWatchTimeDistribution,
  fetchFollowerFunnel,
  fetchAudienceInsights,
  fetchAudienceDemographics,
  fetchViewerTimeline,
  fetchCategoryLeaderboard,
  fetchCoaching,
  fetchMonetization,
  fetchCategoryTimings,
  fetchCategoryActivitySeries,
} from '@/api/client';
import type { TimeRange } from '@/types/analytics';

// Stale time: 5 minutes
const STALE_TIME = 5 * 60 * 1000;

export function useOverview(streamer: string | null, days: TimeRange) {
  return useQuery({
    queryKey: ['overview', streamer, days],
    queryFn: () => fetchOverview(streamer, days),
    staleTime: STALE_TIME,
    enabled: true,
  });
}

export function useMonthlyStats(streamer: string | null, months = 12) {
  return useQuery({
    queryKey: ['monthly-stats', streamer, months],
    queryFn: () => fetchMonthlyStats(streamer, months),
    staleTime: STALE_TIME,
  });
}

export function useWeekdayStats(streamer: string | null, days: TimeRange) {
  return useQuery({
    queryKey: ['weekday-stats', streamer, days],
    queryFn: () => fetchWeekdayStats(streamer, days),
    staleTime: STALE_TIME,
  });
}

export function useHourlyHeatmap(streamer: string | null, days: TimeRange) {
  return useQuery({
    queryKey: ['hourly-heatmap', streamer, days],
    queryFn: () => fetchHourlyHeatmap(streamer, days),
    staleTime: STALE_TIME,
  });
}

export function useCalendarHeatmap(streamer: string | null, days = 365) {
  return useQuery({
    queryKey: ['calendar-heatmap', streamer, days],
    queryFn: () => fetchCalendarHeatmap(streamer, days),
    staleTime: STALE_TIME,
  });
}

export function useChatAnalytics(streamer: string | null, days: TimeRange) {
  return useQuery({
    queryKey: ['chat-analytics', streamer, days],
    queryFn: () => fetchChatAnalytics(streamer, days),
    staleTime: STALE_TIME,
  });
}

export function useViewerOverlap(streamer: string | null, limit = 20) {
  return useQuery({
    queryKey: ['viewer-overlap', streamer, limit],
    queryFn: () => fetchViewerOverlap(streamer, limit),
    staleTime: STALE_TIME,
    enabled: !!streamer,
  });
}

export function useTagAnalysis(days: TimeRange, limit = 30) {
  return useQuery({
    queryKey: ['tag-analysis', days, limit],
    queryFn: () => fetchTagAnalysis(days, limit),
    staleTime: STALE_TIME,
  });
}

export function useRankings(
  metric: 'viewers' | 'growth' | 'retention' | 'chat',
  days: TimeRange,
  limit = 20
) {
  return useQuery({
    queryKey: ['rankings', metric, days, limit],
    queryFn: () => fetchRankings(metric, days, limit),
    staleTime: STALE_TIME,
  });
}

export function useSessionDetail(sessionId: number | null) {
  return useQuery({
    queryKey: ['session', sessionId],
    queryFn: () => fetchSessionDetail(sessionId!),
    staleTime: STALE_TIME,
    enabled: !!sessionId,
  });
}

export function useStreamerList() {
  return useQuery({
    queryKey: ['streamers'],
    queryFn: fetchStreamerList,
    staleTime: 10 * 60 * 1000, // 10 minutes
  });
}

export function useCategoryComparison(streamer: string | null, days: TimeRange) {
  return useQuery({
    queryKey: ['category-comparison', streamer, days],
    queryFn: () => fetchCategoryComparison(streamer, days),
    staleTime: STALE_TIME,
    enabled: !!streamer,
  });
}

export function useAuthStatus() {
  return useQuery({
    queryKey: ['auth-status'],
    queryFn: fetchAuthStatus,
    staleTime: 60 * 1000, // 1 minute
    retry: false,
  });
}

// Watch Time Distribution Hook
export function useWatchTimeDistribution(streamer: string | null, days: TimeRange) {
  return useQuery({
    queryKey: ['watch-time-distribution', streamer, days],
    queryFn: () => fetchWatchTimeDistribution(streamer, days),
    staleTime: STALE_TIME,
    enabled: !!streamer,
  });
}

// Follower Funnel Hook
export function useFollowerFunnel(streamer: string | null, days: TimeRange) {
  return useQuery({
    queryKey: ['follower-funnel', streamer, days],
    queryFn: () => fetchFollowerFunnel(streamer, days),
    staleTime: STALE_TIME,
    enabled: !!streamer,
  });
}

// Extended Tag Analysis Hook
export function useTagAnalysisExtended(streamer: string | null, days: TimeRange, limit = 20) {
  return useQuery({
    queryKey: ['tag-analysis-extended', streamer, days, limit],
    queryFn: () => fetchTagAnalysisExtended(streamer, days, limit),
    staleTime: STALE_TIME,
    enabled: !!streamer,
  });
}

// Title Performance Hook
export function useTitlePerformance(streamer: string | null, days: TimeRange, limit = 20) {
  return useQuery({
    queryKey: ['title-performance', streamer, days, limit],
    queryFn: () => fetchTitlePerformance(streamer, days, limit),
    staleTime: STALE_TIME,
    enabled: !!streamer,
  });
}

// Combined Audience Insights Hook
export function useAudienceInsights(streamer: string | null, days: TimeRange) {
  return useQuery({
    queryKey: ['audience-insights', streamer, days],
    queryFn: () => fetchAudienceInsights(streamer, days),
    staleTime: STALE_TIME,
    enabled: !!streamer,
  });
}

// Audience Demographics Hook
export function useAudienceDemographics(streamer: string | null, days: TimeRange) {
  return useQuery({
    queryKey: ['audience-demographics', streamer, days],
    queryFn: () => fetchAudienceDemographics(streamer, days),
    staleTime: STALE_TIME,
    enabled: !!streamer,
  });
}

// Viewer Timeline Hook (stats_tracked bucketed data)
export function useViewerTimeline(streamer: string | null, days: number) {
  return useQuery({
    queryKey: ['viewer-timeline', streamer, days],
    queryFn: () => fetchViewerTimeline(streamer, days),
    staleTime: STALE_TIME,
    enabled: !!streamer,
  });
}

// Coaching Hook
export function useCoaching(streamer: string | null, days: TimeRange) {
  return useQuery({
    queryKey: ['coaching', streamer, days],
    queryFn: () => fetchCoaching(streamer!, days),
    staleTime: STALE_TIME,
    enabled: !!streamer,
  });
}

// Category Timings Hook
export function useCategoryTimings(days: TimeRange, source: 'category' | 'tracked' = 'category') {
  return useQuery({
    queryKey: ['category-timings', days, source],
    queryFn: () => fetchCategoryTimings(days, source),
    staleTime: STALE_TIME,
  });
}

export function useCategoryActivitySeries(days: TimeRange) {
  return useQuery({
    queryKey: ['category-activity-series', days],
    queryFn: () => fetchCategoryActivitySeries(days),
    staleTime: STALE_TIME,
  });
}

// Monetization Hook
export function useMonetization(streamer: string | null, days: TimeRange) {
  return useQuery({
    queryKey: ['monetization', streamer, days],
    queryFn: () => fetchMonetization(streamer, days),
    staleTime: STALE_TIME,
  });
}

// Category Leaderboard Hook
export function useCategoryLeaderboard(
  streamer: string | null,
  days: number,
  limit = 25,
  sort: 'avg' | 'peak' = 'avg'
) {
  return useQuery({
    queryKey: ['category-leaderboard', streamer, days, limit, sort],
    queryFn: () => fetchCategoryLeaderboard(streamer, days, limit, sort),
    staleTime: STALE_TIME,
  });
}
