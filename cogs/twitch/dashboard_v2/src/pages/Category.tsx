import { useState, useMemo } from 'react';
import { motion } from 'framer-motion';
import {
  Search, Crown, TrendingUp, Users, Star, Filter,
  ArrowUpDown, ChevronUp, ChevronDown, ExternalLink, Loader2,
} from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { fetchCategoryLeaderboard } from '@/api/client';
import { useCategoryActivitySeries } from '@/hooks/useAnalytics';
import { CategoryTimingsChart } from '@/components/charts/CategoryTimingsChart';
import type { TimeRange, TabId } from '@/types/analytics';

interface CategoryProps {
  streamer: string | null;
  days: TimeRange;
  onStreamerSelect: (login: string) => void;
  onNavigate: (tab: TabId) => void;
}

type SortKey = 'rank' | 'avgViewers' | 'peakViewers';
type SortDir = 'asc' | 'desc';
type PartnerFilter = 'all' | 'partner' | 'community';

const ITEMS_PER_PAGE = 50;

export function Category({ streamer, days, onStreamerSelect, onNavigate }: CategoryProps) {
  const [search, setSearch] = useState('');
  const [partnerFilter, setPartnerFilter] = useState<PartnerFilter>('all');
  const [sortKey, setSortKey] = useState<SortKey>('rank');
  const [sortDir, setSortDir] = useState<SortDir>('asc');
  const [page, setPage] = useState(0);
  const [activeView] = useState<'category' | 'tracked'>('category');

  const { data: activitySeries } = useCategoryActivitySeries(days);

  const { data: catLeaderboard, isLoading: loadingCat } = useQuery({
    queryKey: ['category-leaderboard-all', days],
    queryFn: () => fetchCategoryLeaderboard(null, days, 300, 'avg'),
    staleTime: 5 * 60 * 1000,
  });

  const { data: trackedLeaderboard, isLoading: loadingTracked } = useQuery({
    queryKey: ['tracked-leaderboard-all', days],
    queryFn: () => fetchCategoryLeaderboard(null, days, 300, 'avg'),
    staleTime: 5 * 60 * 1000,
  });

  const isLoading = activeView === 'category' ? loadingCat : loadingTracked;
  const rawEntries = (activeView === 'category' ? catLeaderboard : trackedLeaderboard)?.leaderboard ?? [];
  const totalStreamers = (activeView === 'category' ? catLeaderboard : trackedLeaderboard)?.totalStreamers ?? 0;

  const filtered = useMemo(() => {
    let list = [...rawEntries];

    // Partner filter
    if (partnerFilter === 'partner') list = list.filter(e => e.isPartner);
    if (partnerFilter === 'community') list = list.filter(e => !e.isPartner);

    // Search
    const q = search.trim().toLowerCase();
    if (q) list = list.filter(e => e.streamer.toLowerCase().includes(q));

    // Sort
    list.sort((a, b) => {
      let va: number, vb: number;
      if (sortKey === 'avgViewers') { va = a.avgViewers; vb = b.avgViewers; }
      else if (sortKey === 'peakViewers') { va = a.peakViewers; vb = b.peakViewers; }
      else { va = a.rank; vb = b.rank; }
      return sortDir === 'asc' ? va - vb : vb - va;
    });

    return list;
  }, [rawEntries, search, partnerFilter, sortKey, sortDir]);

  const paginated = filtered.slice(page * ITEMS_PER_PAGE, (page + 1) * ITEMS_PER_PAGE);
  const totalPages = Math.ceil(filtered.length / ITEMS_PER_PAGE);

  const partnerCount = rawEntries.filter(e => e.isPartner).length;

  const topPartners = useMemo(() =>
    rawEntries.filter(e => e.isPartner).sort((a, b) => a.rank - b.rank).slice(0, 20),
    [rawEntries]
  );

  const topAll = useMemo(() =>
    [...rawEntries].sort((a, b) => a.rank - b.rank).slice(0, 20),
    [rawEntries]
  );

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortDir(key === 'rank' ? 'asc' : 'desc');
    }
    setPage(0);
  }

  function SortIcon({ k }: { k: SortKey }) {
    if (sortKey !== k) return <ArrowUpDown className="w-3 h-3 opacity-40" />;
    return sortDir === 'asc'
      ? <ChevronUp className="w-3 h-3 text-accent" />
      : <ChevronDown className="w-3 h-3 text-accent" />;
  }

  function handleSelect(login: string) {
    onStreamerSelect(login);
    onNavigate('overview');
  }

  // Summary stats
  const avgViewersAll = rawEntries.length
    ? rawEntries.reduce((s, e) => s + e.avgViewers, 0) / rawEntries.length
    : 0;
  const top1 = rawEntries[0];

  return (
    <div className="space-y-6">
      {/* Summary Row */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <SummaryCard
          label="Streamer gesamt"
          value={totalStreamers.toLocaleString('de-DE')}
          icon={<Users className="w-4 h-4 text-primary" />}
        />
        <SummaryCard
          label="Partner (Tracked)"
          value={partnerCount.toLocaleString('de-DE')}
          icon={<Star className="w-4 h-4 text-accent" />}
        />
        <SummaryCard
          label="Kategorie Ø Viewer"
          value={avgViewersAll.toLocaleString('de-DE', { maximumFractionDigits: 0 })}
          icon={<TrendingUp className="w-4 h-4 text-success" />}
        />
        <SummaryCard
          label="Platz #1"
          value={top1?.streamer ?? '-'}
          sub={top1 ? `${top1.avgViewers.toLocaleString('de-DE', { maximumFractionDigits: 0 })} Ø` : undefined}
          icon={<Crown className="w-4 h-4 text-yellow-400" />}
        />
      </div>

      {/* Timings Charts */}
      {activitySeries && (
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          className="bg-card border border-border rounded-xl p-5"
        >
          <CategoryTimingsChart data={activitySeries} />
        </motion.div>
      )}

      {/* Top Partner Streamer */}
      {!loadingCat && topPartners.length > 0 && (
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          className="bg-card border border-border rounded-xl overflow-hidden"
        >
          <div className="px-5 py-3 border-b border-border bg-background/30 flex items-center gap-2">
            <Star className="w-4 h-4 text-accent" />
            <h3 className="font-semibold text-white text-sm">Top Partner Streamer</h3>
            <span className="text-xs text-text-secondary ml-auto">Top {topPartners.length}</span>
          </div>
          <TopStreamerTable entries={topPartners} selected={streamer} onSelect={handleSelect} />
        </motion.div>
      )}

      {/* Top Deadlock Streamer (Kategorie gesamt) */}
      {!loadingCat && topAll.length > 0 && (
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          className="bg-card border border-border rounded-xl overflow-hidden"
        >
          <div className="px-5 py-3 border-b border-border bg-background/30 flex items-center gap-2">
            <TrendingUp className="w-4 h-4 text-success" />
            <h3 className="font-semibold text-white text-sm">Top Deadlock Streamer (Kategorie gesamt)</h3>
            <span className="text-xs text-text-secondary ml-auto">Top {topAll.length}</span>
          </div>
          <TopStreamerTable entries={topAll} selected={streamer} onSelect={handleSelect} />
        </motion.div>
      )}

      {/* Controls */}
      <div className="bg-card border border-border rounded-xl p-4 space-y-3">
        {/* Search + Filter row */}
        <div className="flex flex-wrap gap-3 items-center">
          {/* Search */}
          <div className="relative flex-1 min-w-[200px]">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-secondary" />
            <input
              type="text"
              placeholder="Streamer suchen…"
              value={search}
              onChange={e => { setSearch(e.target.value); setPage(0); }}
              className="w-full pl-9 pr-4 py-2 bg-background border border-border rounded-lg text-white text-sm placeholder:text-text-secondary focus:outline-none focus:border-accent"
            />
          </div>

          {/* Partner filter */}
          <div className="flex items-center gap-1 bg-background border border-border rounded-lg p-1">
            <Filter className="w-3.5 h-3.5 text-text-secondary ml-1" />
            {(['all', 'partner', 'community'] as PartnerFilter[]).map(f => (
              <button
                key={f}
                onClick={() => { setPartnerFilter(f); setPage(0); }}
                className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
                  partnerFilter === f
                    ? 'bg-accent text-white'
                    : 'text-text-secondary hover:text-white'
                }`}
              >
                {f === 'all' ? 'Alle' : f === 'partner' ? 'Partner' : 'Community'}
              </button>
            ))}
          </div>
        </div>

        {/* Result count */}
        <div className="text-xs text-text-secondary">
          {filtered.length} von {rawEntries.length} Streamern
          {search && ` – Filter: "${search}"`}
        </div>
      </div>

      {/* Table */}
      <div className="bg-card border border-border rounded-xl overflow-hidden">
        {isLoading ? (
          <div className="flex items-center justify-center h-48">
            <Loader2 className="w-6 h-6 animate-spin text-primary" />
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border bg-background/50">
                    <th className="text-left px-4 py-3 text-text-secondary font-medium w-12">
                      <button className="flex items-center gap-1 hover:text-white transition-colors" onClick={() => toggleSort('rank')}>
                        # <SortIcon k="rank" />
                      </button>
                    </th>
                    <th className="text-left px-4 py-3 text-text-secondary font-medium">Streamer</th>
                    <th className="text-right px-4 py-3 text-text-secondary font-medium">
                      <button className="flex items-center gap-1 ml-auto hover:text-white transition-colors" onClick={() => toggleSort('avgViewers')}>
                        Ø Viewer <SortIcon k="avgViewers" />
                      </button>
                    </th>
                    <th className="text-right px-4 py-3 text-text-secondary font-medium">
                      <button className="flex items-center gap-1 ml-auto hover:text-white transition-colors" onClick={() => toggleSort('peakViewers')}>
                        Peak <SortIcon k="peakViewers" />
                      </button>
                    </th>
                    <th className="text-center px-4 py-3 text-text-secondary font-medium w-24">Status</th>
                    <th className="px-4 py-3 w-10" />
                  </tr>
                </thead>
                <tbody>
                  {paginated.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="text-center py-12 text-text-secondary">
                        Keine Streamer gefunden.
                      </td>
                    </tr>
                  ) : (
                    paginated.map((entry, i) => {
                      const isSelected = entry.streamer === streamer;
                      const globalRank = entry.rank;

                      return (
                        <motion.tr
                          key={entry.streamer}
                          initial={{ opacity: 0 }}
                          animate={{ opacity: 1 }}
                          transition={{ delay: Math.min(i * 0.02, 0.3) }}
                          className={`border-b border-border/50 transition-colors ${
                            isSelected
                              ? 'bg-accent/10 border-accent/20'
                              : 'hover:bg-background/60'
                          }`}
                        >
                          {/* Rank */}
                          <td className="px-4 py-3">
                            <RankBadge rank={globalRank} />
                          </td>

                          {/* Name */}
                          <td className="px-4 py-3">
                            <div className="flex items-center gap-2">
                              <span className={`font-medium ${isSelected ? 'text-accent' : 'text-white'}`}>
                                {entry.streamer}
                              </span>
                              {isSelected && (
                                <span className="text-xs px-1.5 py-0.5 bg-accent/20 text-accent rounded">Ausgewählt</span>
                              )}
                            </div>
                          </td>

                          {/* Avg Viewers */}
                          <td className="px-4 py-3 text-right">
                            <span className="font-bold text-white">
                              {entry.avgViewers.toLocaleString('de-DE', { maximumFractionDigits: 0 })}
                            </span>
                          </td>

                          {/* Peak */}
                          <td className="px-4 py-3 text-right text-text-secondary">
                            {entry.peakViewers.toLocaleString('de-DE', { maximumFractionDigits: 0 })}
                          </td>

                          {/* Status */}
                          <td className="px-4 py-3 text-center">
                            {entry.isPartner ? (
                              <span className="text-xs px-2 py-0.5 bg-accent/20 text-accent rounded-full">
                                Partner
                              </span>
                            ) : (
                              <span className="text-xs px-2 py-0.5 bg-border/50 text-text-secondary rounded-full">
                                Community
                              </span>
                            )}
                          </td>

                          {/* Action */}
                          <td className="px-4 py-3 text-center">
                            <button
                              onClick={() => handleSelect(entry.streamer)}
                              title="Analyse öffnen"
                              className="p-1.5 rounded-lg text-text-secondary hover:text-accent hover:bg-accent/10 transition-colors"
                            >
                              <ExternalLink className="w-3.5 h-3.5" />
                            </button>
                          </td>
                        </motion.tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            {totalPages > 1 && (
              <div className="flex items-center justify-between px-4 py-3 border-t border-border bg-background/30">
                <span className="text-xs text-text-secondary">
                  Seite {page + 1} von {totalPages} ({filtered.length} Einträge)
                </span>
                <div className="flex gap-2">
                  <button
                    disabled={page === 0}
                    onClick={() => setPage(p => p - 1)}
                    className="px-3 py-1.5 rounded-lg text-xs bg-background border border-border text-white disabled:opacity-40 hover:border-accent transition-colors"
                  >
                    Zurück
                  </button>
                  <button
                    disabled={page >= totalPages - 1}
                    onClick={() => setPage(p => p + 1)}
                    className="px-3 py-1.5 rounded-lg text-xs bg-background border border-border text-white disabled:opacity-40 hover:border-accent transition-colors"
                  >
                    Weiter
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* Hint */}
      <p className="text-xs text-text-secondary text-center">
        Klick auf <ExternalLink className="w-3 h-3 inline" /> öffnet die vollständige Analyse des Streamers.
      </p>
    </div>
  );
}

function SummaryCard({ label, value, sub, icon }: { label: string; value: string; sub?: string; icon: React.ReactNode }) {
  return (
    <div className="bg-card border border-border rounded-xl p-4 flex items-start gap-3">
      <div className="mt-0.5">{icon}</div>
      <div className="min-w-0">
        <div className="text-xs text-text-secondary mb-0.5">{label}</div>
        <div className="text-lg font-bold text-white truncate">{value}</div>
        {sub && <div className="text-xs text-text-secondary">{sub}</div>}
      </div>
    </div>
  );
}

function TopStreamerTable({
  entries,
  selected,
  onSelect,
}: {
  entries: { rank: number; streamer: string; avgViewers: number; peakViewers: number; isPartner: boolean }[];
  selected: string | null;
  onSelect: (login: string) => void;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border bg-background/20">
            <th className="text-left px-4 py-2 text-text-secondary font-medium w-12 text-xs">#</th>
            <th className="text-left px-4 py-2 text-text-secondary font-medium text-xs">Streamer</th>
            <th className="text-right px-4 py-2 text-text-secondary font-medium text-xs">Ø Viewer</th>
            <th className="text-right px-4 py-2 text-text-secondary font-medium text-xs">Peak</th>
            <th className="px-4 py-2 w-10" />
          </tr>
        </thead>
        <tbody>
          {entries.map((entry, i) => {
            const isSelected = entry.streamer === selected;
            return (
              <tr
                key={entry.streamer}
                className={`border-b border-border/30 transition-colors ${
                  isSelected ? 'bg-accent/10' : 'hover:bg-background/60'
                }`}
              >
                <td className="px-4 py-2">
                  <RankBadge rank={entry.rank} />
                </td>
                <td className="px-4 py-2">
                  <span className={`font-medium text-sm ${isSelected ? 'text-accent' : 'text-white'}`}>
                    {entry.streamer}
                  </span>
                  {i === 0 && (
                    <span className="ml-2 text-xs px-1.5 py-0.5 bg-yellow-400/20 text-yellow-400 rounded">#1</span>
                  )}
                </td>
                <td className="px-4 py-2 text-right font-bold text-white text-sm">
                  {entry.avgViewers.toLocaleString('de-DE', { maximumFractionDigits: 0 })}
                </td>
                <td className="px-4 py-2 text-right text-text-secondary text-sm">
                  {entry.peakViewers.toLocaleString('de-DE', { maximumFractionDigits: 0 })}
                </td>
                <td className="px-4 py-2 text-center">
                  <button
                    onClick={() => onSelect(entry.streamer)}
                    title="Analyse öffnen"
                    className="p-1.5 rounded-lg text-text-secondary hover:text-accent hover:bg-accent/10 transition-colors"
                  >
                    <ExternalLink className="w-3.5 h-3.5" />
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RankBadge({ rank }: { rank: number }) {
  if (rank === 1) return (
    <span className="w-7 h-7 rounded-full bg-gradient-to-br from-yellow-400 to-amber-600 flex items-center justify-center text-xs font-bold text-white">1</span>
  );
  if (rank === 2) return (
    <span className="w-7 h-7 rounded-full bg-gradient-to-br from-gray-300 to-gray-500 flex items-center justify-center text-xs font-bold text-white">2</span>
  );
  if (rank === 3) return (
    <span className="w-7 h-7 rounded-full bg-gradient-to-br from-amber-600 to-amber-800 flex items-center justify-center text-xs font-bold text-white">3</span>
  );
  return <span className="text-text-secondary text-xs font-mono">#{rank}</span>;
}
