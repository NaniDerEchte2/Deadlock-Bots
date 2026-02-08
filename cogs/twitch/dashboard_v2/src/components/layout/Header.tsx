import { useState } from 'react';
import { Activity, ChevronDown } from 'lucide-react';
import type { TimeRange } from '@/types/analytics';

interface HeaderProps {
  streamer: string | null;
  streamers: { login: string; isPartner: boolean }[];
  days: TimeRange;
  onStreamerChange: (streamer: string | null) => void;
  onDaysChange: (days: TimeRange) => void;
  isLoading?: boolean;
  canViewAllStreamers?: boolean;
}

export function Header({
  streamer,
  streamers,
  days,
  onStreamerChange,
  onDaysChange,
  isLoading,
  canViewAllStreamers = false,
}: HeaderProps) {
  const [dropdownOpen, setDropdownOpen] = useState(false);

  const timeRanges: { value: TimeRange; label: string }[] = [
    { value: 7, label: '7d' },
    { value: 30, label: '30d' },
    { value: 90, label: '90d' },
  ];

  const partners = streamers.filter(s => s.isPartner);
  const others = streamers.filter(s => !s.isPartner);
  const allLabel = canViewAllStreamers ? 'Alle Streamer' : 'Alle Partner';

  // In Beta: Partner koennen vorerst alle Streamer sehen.
  const visiblePartners = partners;
  const visibleOthers = canViewAllStreamers ? others : [];

  return (
    <header className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-8">
      {/* Logo & Title */}
      <div className="flex items-center gap-4">
        <div className="p-2 rounded-xl bg-accent/20">
          <Activity className="w-6 h-6 text-accent" />
        </div>
        <div>
          <h1 className="text-2xl font-bold text-white flex items-center gap-2">
            Channel Analytics
            {isLoading && (
              <span className="w-2 h-2 rounded-full bg-accent animate-pulse" />
            )}
          </h1>
          <p className="text-text-secondary text-sm">
            {streamer || allLabel} • Letzte {days} Tage
          </p>
        </div>
      </div>

      {/* Controls */}
      <div className="flex items-center gap-3">
        {/* Streamer Dropdown */}
        <div className="relative">
          <button
            onClick={() => setDropdownOpen(!dropdownOpen)}
            className="flex items-center gap-2 px-4 py-2 bg-card rounded-lg border border-border hover:border-border-hover transition-colors"
          >
            <span className="text-white">
              {streamer || allLabel}
            </span>
            <ChevronDown className="w-4 h-4 text-text-secondary" />
          </button>

          {dropdownOpen && (
            <>
              <div
                className="fixed inset-0 z-40"
                onClick={() => setDropdownOpen(false)}
              />
              <div className="absolute top-full right-0 mt-2 w-64 max-h-96 overflow-y-auto bg-card border border-border rounded-lg shadow-xl z-50">
                {/* All Partners Option */}
                <button
                  onClick={() => {
                    onStreamerChange(null);
                    setDropdownOpen(false);
                  }}
                  className={`w-full px-4 py-2 text-left hover:bg-white/5 transition-colors ${
                    !streamer ? 'bg-accent/20 text-accent' : 'text-white'
                  }`}
                >
                  {allLabel}
                </button>

                {/* Partners */}
                {visiblePartners.length > 0 && (
                  <>
                    <div className="px-4 py-1 text-xs text-text-secondary uppercase tracking-wider bg-black/20">
                      Partner
                    </div>
                    {visiblePartners.map(s => (
                      <button
                        key={s.login}
                        onClick={() => {
                          onStreamerChange(s.login);
                          setDropdownOpen(false);
                        }}
                        className={`w-full px-4 py-2 text-left hover:bg-white/5 transition-colors ${
                          streamer === s.login ? 'bg-accent/20 text-accent' : 'text-white'
                        }`}
                      >
                        {s.login}
                      </button>
                    ))}
                  </>
                )}

                {/* Others (Admin only) */}
                {visibleOthers.length > 0 && (
                  <>
                    <div className="px-4 py-1 text-xs text-text-secondary uppercase tracking-wider bg-black/20">
                      Weitere Streamer
                    </div>
                    {visibleOthers.map(s => (
                      <button
                        key={s.login}
                        onClick={() => {
                          onStreamerChange(s.login);
                          setDropdownOpen(false);
                        }}
                        className={`w-full px-4 py-2 text-left hover:bg-white/5 transition-colors ${
                          streamer === s.login ? 'bg-accent/20 text-accent' : 'text-white'
                        }`}
                      >
                        {s.login}
                        <span className="ml-2 text-text-secondary text-xs">(extern)</span>
                      </button>
                    ))}
                  </>
                )}
              </div>
            </>
          )}
        </div>

        {/* Time Range Selector */}
        <div className="flex items-center bg-card rounded-lg border border-border p-1">
          {timeRanges.map(range => (
            <button
              key={range.value}
              onClick={() => onDaysChange(range.value)}
              className={`px-4 py-1.5 rounded-md text-sm font-medium transition-all ${
                days === range.value
                  ? 'bg-accent text-white shadow-lg'
                  : 'text-text-secondary hover:text-white'
              }`}
            >
              {range.label}
            </button>
          ))}
        </div>
      </div>
    </header>
  );
}
