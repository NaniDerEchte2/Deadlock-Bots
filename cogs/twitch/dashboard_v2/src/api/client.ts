// API Client for Twitch Analytics Dashboard

import type {
  DashboardOverview,
  MonthlyStats,
  WeekdayStats,
  HourlyHeatmapData,
  CalendarHeatmapData,
  ChatAnalytics,
  ViewerOverlap,
  TagPerformance,
  RankingEntry,
  StreamSession,
  TimeRange,
  CategoryComparison,
} from '@/types/analytics';

const API_BASE = '/twitch/api/v2';

// Get partner token from URL or localStorage
function getPartnerToken(): string | null {
  const urlParams = new URLSearchParams(window.location.search);
  const token = urlParams.get('partner_token');
  if (token) {
    localStorage.setItem('partner_token', token);
    return token;
  }
  return localStorage.getItem('partner_token');
}

// Helper to build URL with params
function buildUrl(endpoint: string, params: Record<string, string | number | boolean> = {}): string {
  const url = new URL(`${API_BASE}${endpoint}`, window.location.origin);
  const token = getPartnerToken();
  if (token) {
    url.searchParams.set('partner_token', token);
  }
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null) {
      url.searchParams.set(key, String(value));
    }
  });
  return url.toString();
}

// Generic fetch wrapper
async function fetchApi<T>(endpoint: string, params: Record<string, string | number | boolean> = {}): Promise<T> {
  const url = buildUrl(endpoint, params);
  const response = await fetch(url, {
    headers: {
      'Accept': 'application/json',
    },
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ error: 'Unknown error' }));
    throw new Error(error.error || `HTTP ${response.status}`);
  }

  return response.json();
}

// API Functions

export async function fetchOverview(
  streamer: string | null,
  days: TimeRange
): Promise<DashboardOverview> {
  return fetchApi<DashboardOverview>('/overview', {
    streamer: streamer || '',
    days,
  });
}

export async function fetchMonthlyStats(
  streamer: string | null,
  months: number = 12
): Promise<MonthlyStats[]> {
  return fetchApi<MonthlyStats[]>('/monthly-stats', {
    streamer: streamer || '',
    months,
  });
}

export async function fetchWeekdayStats(
  streamer: string | null,
  days: TimeRange
): Promise<WeekdayStats[]> {
  return fetchApi<WeekdayStats[]>('/weekly-stats', {
    streamer: streamer || '',
    days,
  });
}

export async function fetchHourlyHeatmap(
  streamer: string | null,
  days: TimeRange
): Promise<HourlyHeatmapData[]> {
  return fetchApi<HourlyHeatmapData[]>('/hourly-heatmap', {
    streamer: streamer || '',
    days,
  });
}

export async function fetchCalendarHeatmap(
  streamer: string | null,
  days: number = 365
): Promise<CalendarHeatmapData[]> {
  return fetchApi<CalendarHeatmapData[]>('/calendar-heatmap', {
    streamer: streamer || '',
    days,
  });
}

export async function fetchChatAnalytics(
  streamer: string | null,
  days: TimeRange
): Promise<ChatAnalytics> {
  return fetchApi<ChatAnalytics>('/chat-analytics', {
    streamer: streamer || '',
    days,
  });
}

export async function fetchViewerOverlap(
  streamer: string | null,
  limit: number = 20
): Promise<ViewerOverlap[]> {
  return fetchApi<ViewerOverlap[]>('/viewer-overlap', {
    streamer: streamer || '',
    limit,
  });
}

export async function fetchTagAnalysis(
  days: TimeRange,
  limit: number = 30
): Promise<TagPerformance[]> {
  return fetchApi<TagPerformance[]>('/tag-analysis', {
    days,
    limit,
  });
}

export async function fetchRankings(
  metric: 'viewers' | 'growth' | 'retention' | 'chat',
  days: TimeRange,
  limit: number = 20
): Promise<RankingEntry[]> {
  return fetchApi<RankingEntry[]>('/rankings', {
    metric,
    days,
    limit,
  });
}

export async function fetchSessionDetail(
  sessionId: number
): Promise<StreamSession & { timeline: { minute: number; viewers: number }[]; chatters: { login: string; messages: number }[] }> {
  return fetchApi(`/session/${sessionId}`);
}

export async function fetchStreamerList(): Promise<{ login: string; isPartner: boolean }[]> {
  return fetchApi<{ login: string; isPartner: boolean }[]>('/streamers');
}

// Auth Status
export interface AuthStatus {
  authenticated: boolean;
  level: 'localhost' | 'admin' | 'partner' | 'none';
  isAdmin: boolean;
  isLocalhost: boolean;
  canViewAllStreamers: boolean;
  permissions: {
    viewAllStreamers: boolean;
    viewComparison: boolean;
    viewChatAnalytics: boolean;
    viewOverlap: boolean;
  };
}

export async function fetchAuthStatus(): Promise<AuthStatus> {
  return fetchApi<AuthStatus>('/auth-status');
}

// Category Comparison
export async function fetchCategoryComparison(
  streamer: string | null,
  days: TimeRange
): Promise<CategoryComparison> {
  return fetchApi('/category-comparison', {
    streamer: streamer || '',
    days,
  });
}
