// Formatting utilities for the dashboard

export function formatNumber(n: number | null | undefined, decimals = 0): string {
  if (n === null || n === undefined) return '-';
  return n.toLocaleString('de-DE', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function formatPercent(n: number | null | undefined, decimals = 1): string {
  if (n === null || n === undefined) return '-';
  return `${n.toFixed(decimals)}%`;
}

export function formatDuration(seconds: number): string {
  if (!seconds || seconds <= 0) return '0m';

  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);

  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes}m`;
}

export function formatDurationShort(seconds: number): string {
  if (!seconds || seconds <= 0) return '0m';

  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);

  if (hours > 0) {
    return minutes > 0 ? `${hours}h${minutes}m` : `${hours}h`;
  }
  return `${minutes}m`;
}

export function formatHours(hours: number): string {
  if (hours < 1) {
    return `${Math.round(hours * 60)}m`;
  }
  if (hours < 100) {
    return `${hours.toFixed(1)}h`;
  }
  return `${Math.round(hours)}h`;
}

export function formatDate(dateStr: string): string {
  if (!dateStr) return '-';
  const date = new Date(dateStr);
  return date.toLocaleDateString('de-DE', {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
  });
}

export function formatDateFull(dateStr: string): string {
  if (!dateStr) return '-';
  const date = new Date(dateStr);
  return date.toLocaleDateString('de-DE', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  });
}

export function formatTime(timeStr: string): string {
  if (!timeStr) return '-';
  // Handle both "HH:MM:SS" and full datetime strings
  const parts = timeStr.split(':');
  if (parts.length >= 2) {
    return `${parts[0]}:${parts[1]}`;
  }
  return timeStr;
}

export function formatTrend(value: number): string {
  if (value > 0) return `+${value.toFixed(1)}%`;
  if (value < 0) return `${value.toFixed(1)}%`;
  return '0%';
}

export function formatCompact(n: number): string {
  if (n >= 1_000_000) {
    return `${(n / 1_000_000).toFixed(1)}M`;
  }
  if (n >= 1_000) {
    return `${(n / 1_000).toFixed(1)}K`;
  }
  return n.toString();
}

export function getWeekdayLabel(weekday: number): string {
  const days = ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'];
  return days[weekday] || '';
}

export function getWeekdayLabelFull(weekday: number): string {
  const days = ['Sonntag', 'Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag'];
  return days[weekday] || '';
}

export function getMonthLabel(month: number): string {
  const months = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez'];
  return months[month - 1] || '';
}

export function getMonthLabelFull(month: number): string {
  const months = [
    'Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
    'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember'
  ];
  return months[month - 1] || '';
}

// Color utilities
export function getTrendColor(value: number): string {
  if (value > 0) return 'text-success';
  if (value < 0) return 'text-danger';
  return 'text-text-secondary';
}

export function getScoreColor(score: number): string {
  if (score >= 80) return '#4ade80'; // green
  if (score >= 60) return '#fbbf24'; // yellow
  if (score >= 40) return '#fb923c'; // orange
  return '#f87171'; // red
}

export function getHeatmapColor(value: number, max: number): string {
  if (!max || !value) return 'rgba(124, 58, 237, 0.05)';
  const intensity = Math.min(value / max, 1);
  return `rgba(124, 58, 237, ${0.1 + intensity * 0.7})`;
}

export function getRetentionColor(retention: number): string {
  if (retention >= 70) return '#4ade80';
  if (retention >= 50) return '#fbbf24';
  if (retention >= 30) return '#fb923c';
  return '#f87171';
}
