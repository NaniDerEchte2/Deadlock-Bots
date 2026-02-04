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
  fetchRankings,
  fetchSessionDetail,
  fetchStreamerList,
  fetchCategoryComparison,
  fetchAuthStatus,
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
