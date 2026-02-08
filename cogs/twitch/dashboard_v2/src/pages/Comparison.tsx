import { useState } from 'react';
import { motion } from 'framer-motion';
import { Scale, Users, TrendingUp, Target, AlertCircle, Loader2, Crown } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { fetchCategoryComparison, fetchViewerOverlap, fetchCategoryLeaderboard } from '@/api/client';
import type { CategoryComparison, ViewerOverlap, CategoryLeaderboard } from '@/types/analytics';

import type { TimeRange } from '@/types/analytics';

interface ComparisonProps {
  streamer: string;
  days: TimeRange;
}

export function Comparison({ streamer, days }: ComparisonProps) {
  const [leaderboardSort, setLeaderboardSort] = useState<'avg' | 'peak'>('avg');

  const { data: comparison, isLoading: loadingComparison } = useQuery<CategoryComparison>({
    queryKey: ['categoryComparison', streamer, days],
    queryFn: () => fetchCategoryComparison(streamer, days),
    enabled: !!streamer,
  });

  const { data: overlap, isLoading: loadingOverlap } = useQuery<ViewerOverlap[]>({
    queryKey: ['viewerOverlap', streamer],
    queryFn: () => fetchViewerOverlap(streamer, 20),
    enabled: !!streamer,
  });

  const { data: leaderboard } = useQuery<CategoryLeaderboard>({
    queryKey: ['categoryLeaderboard', streamer, days, leaderboardSort],
    queryFn: () => fetchCategoryLeaderboard(streamer, days, 25, leaderboardSort),
    enabled: !!streamer,
  });

  if (!streamer) {
    return (
      <div className="flex flex-col items-center justify-center h-64">
        <AlertCircle className="w-12 h-12 text-text-secondary mb-4" />
        <p className="text-text-secondary text-lg">Wähle einen Streamer aus</p>
      </div>
    );
  }

  if (loadingComparison || loadingOverlap) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Category Comparison Section */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="bg-card rounded-xl border border-border p-6"
      >
        <div className="flex items-center gap-3 mb-6">
          <Scale className="w-6 h-6 text-primary" />
          <h2 className="text-xl font-bold text-white">Deadlock Kategorie-Vergleich</h2>
        </div>

        {comparison && (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            <ComparisonMetric
              label="Ø Viewer"
              yourValue={comparison.yourStats.avgViewers}
              categoryValue={comparison.categoryAvg.avgViewers}
              percentile={comparison.percentiles.avgViewers}
              format="number"
            />
            <ComparisonMetric
              label="Peak Viewer"
              yourValue={comparison.yourStats.peakViewers}
              categoryValue={comparison.categoryAvg.peakViewers}
              percentile={50}
              format="number"
            />
            <ComparisonMetric
              label="10-Min Retention"
              yourValue={comparison.yourStats.retention10m}
              categoryValue={comparison.categoryAvg.retention10m}
              percentile={comparison.percentiles.retention10m}
              format="percent"
            />
            <ComparisonMetric
              label="Chat Health"
              yourValue={comparison.yourStats.chatHealth}
              categoryValue={comparison.categoryAvg.chatHealth}
              percentile={comparison.percentiles.chatHealth}
              format="decimal"
            />
          </div>
        )}

        {/* Percentile Ranking */}
        {comparison && comparison.percentiles.avgViewers > 0 && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.3 }}
            className="mt-6 p-4 bg-background rounded-lg"
          >
            <div className="flex items-center gap-2 mb-3">
              <Target className="w-5 h-5 text-primary" />
              <span className="font-medium text-white">Dein Ranking</span>
            </div>
            <div className="text-3xl font-bold text-transparent bg-gradient-to-r from-primary to-accent bg-clip-text">
              Top {100 - comparison.percentiles.avgViewers}%
            </div>
            <p className="text-sm text-text-secondary mt-1">
              aller Deadlock-Streamer nach Ø Viewern
            </p>
          </motion.div>
        )}
      </motion.div>

      {/* Viewer Overlap Section */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2 }}
        className="bg-card rounded-xl border border-border p-6"
      >
        <div className="flex items-center gap-3 mb-6">
          <Users className="w-6 h-6 text-accent" />
          <h2 className="text-xl font-bold text-white">Viewer-Überschneidung</h2>
        </div>

        {overlap && overlap.length > 0 ? (
          <div className="space-y-3">
            {overlap.slice(0, 10).map((item, i) => (
              <OverlapBar
                key={item.streamerB}
                rank={i + 1}
                streamer={item.streamerB}
                sharedChatters={item.sharedChatters}
                percentage={item.overlapPercentage}
              />
            ))}
          </div>
        ) : (
          <div className="text-center py-8 text-text-secondary">
            <Users className="w-12 h-12 mx-auto mb-3 opacity-50" />
            <p>Keine Überschneidungsdaten vorhanden</p>
            <p className="text-sm mt-1">Sammle mehr Chat-Daten</p>
          </div>
        )}

        {overlap && overlap.length > 0 && (
          <div className="mt-6 p-4 bg-gradient-to-r from-accent/10 to-primary/10 rounded-lg border border-accent/20">
            <div className="flex items-center gap-2 mb-2">
              <TrendingUp className="w-5 h-5 text-accent" />
              <span className="font-medium text-white">Raid-Empfehlung</span>
            </div>
            <p className="text-text-secondary text-sm">
              <span className="text-white font-medium">{overlap[0]?.streamerB}</span> hat die höchste
              Viewer-Überschneidung ({overlap[0]?.overlapPercentage.toFixed(1)}%).
              Ein Raid könnte für beide Communities wertvoll sein!
            </p>
          </div>
        )}
      </motion.div>

      {/* Category Leaderboard */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.3 }}
        className="bg-card rounded-xl border border-border p-6"
      >
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <Crown className="w-6 h-6 text-yellow-400" />
            <h2 className="text-xl font-bold text-white">Kategorie-Leaderboard</h2>
            {leaderboard && (
              <span className="text-sm text-text-secondary">
                ({leaderboard.totalStreamers} Streamer)
              </span>
            )}
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => setLeaderboardSort('avg')}
              className={`px-3 py-1 rounded-lg text-sm transition-colors ${
                leaderboardSort === 'avg'
                  ? 'bg-primary text-white'
                  : 'bg-background text-text-secondary hover:text-white'
              }`}
            >
              Ø Viewer
            </button>
            <button
              onClick={() => setLeaderboardSort('peak')}
              className={`px-3 py-1 rounded-lg text-sm transition-colors ${
                leaderboardSort === 'peak'
                  ? 'bg-primary text-white'
                  : 'bg-background text-text-secondary hover:text-white'
              }`}
            >
              Peak
            </button>
          </div>
        </div>

        {leaderboard && leaderboard.leaderboard.length > 0 ? (
          <div className="space-y-2">
            {leaderboard.leaderboard.map((entry) => (
              <LeaderboardRow key={entry.streamer} entry={entry} sort={leaderboardSort} />
            ))}
          </div>
        ) : (
          <div className="text-center py-8 text-text-secondary">
            <Crown className="w-12 h-12 mx-auto mb-3 opacity-50" />
            <p>Keine Leaderboard-Daten vorhanden</p>
          </div>
        )}

        {leaderboard?.yourRank && (
          <div className="mt-4 p-3 bg-primary/10 rounded-lg border border-primary/20 text-sm text-text-secondary">
            Dein Platz: <span className="text-white font-bold">#{leaderboard.yourRank}</span> von {leaderboard.totalStreamers}
          </div>
        )}
      </motion.div>
    </div>
  );
}

interface ComparisonMetricProps {
  label: string;
  yourValue: number;
  categoryValue: number;
  percentile: number;
  format: 'number' | 'percent' | 'decimal';
}

function ComparisonMetric({ label, yourValue, categoryValue, percentile, format }: ComparisonMetricProps) {
  const formatValue = (val: number) => {
    if (format === 'percent') return `${val.toFixed(1)}%`;
    if (format === 'decimal') return val.toFixed(1);
    return val.toLocaleString('de-DE', { maximumFractionDigits: 0 });
  };

  const diff = categoryValue > 0 ? ((yourValue - categoryValue) / categoryValue) * 100 : 0;
  const isPositive = diff >= 0;

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      className="p-4 bg-background rounded-lg"
    >
      <div className="text-sm text-text-secondary mb-2">{label}</div>

      <div className="flex items-end justify-between mb-3">
        <div>
          <div className="text-2xl font-bold text-white">{formatValue(yourValue)}</div>
          <div className="text-xs text-text-secondary">Dein Wert</div>
        </div>
        <div className="text-right">
          <div className="text-lg text-text-secondary">{formatValue(categoryValue)}</div>
          <div className="text-xs text-text-secondary">Kategorie Ø</div>
        </div>
      </div>

      {/* Progress bar showing position relative to category */}
      <div className="h-2 bg-border rounded-full overflow-hidden mb-2">
        <motion.div
          initial={{ width: 0 }}
          animate={{ width: `${Math.min(100, Math.max(0, percentile))}%` }}
          transition={{ delay: 0.3, duration: 0.5 }}
          className="h-full bg-gradient-to-r from-primary to-accent"
        />
      </div>

      <div className={`text-sm font-medium ${isPositive ? 'text-success' : 'text-error'}`}>
        {isPositive ? '+' : ''}{diff.toFixed(1)}% vs Kategorie
      </div>
    </motion.div>
  );
}

interface OverlapBarProps {
  rank: number;
  streamer: string;
  sharedChatters: number;
  percentage: number;
}

function OverlapBar({ rank, streamer, sharedChatters, percentage }: OverlapBarProps) {
  return (
    <motion.div
      initial={{ opacity: 0, x: -20 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay: rank * 0.05 }}
      className="flex items-center gap-4"
    >
      <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold ${
        rank <= 3 ? 'bg-gradient-to-br from-primary to-accent text-white' : 'bg-border text-text-secondary'
      }`}>
        {rank}
      </div>

      <div className="flex-1">
        <div className="flex items-center justify-between mb-1">
          <span className="font-medium text-white">{streamer}</span>
          <span className="text-sm text-text-secondary">
            {sharedChatters.toLocaleString('de-DE')} Chatter
          </span>
        </div>
        <div className="h-2 bg-border rounded-full overflow-hidden">
          <motion.div
            initial={{ width: 0 }}
            animate={{ width: `${percentage}%` }}
            transition={{ delay: 0.3 + rank * 0.05, duration: 0.5 }}
            className={`h-full ${rank <= 3 ? 'bg-gradient-to-r from-accent to-primary' : 'bg-primary/60'}`}
          />
        </div>
      </div>

      <div className="w-16 text-right">
        <span className="text-sm font-medium text-white">{percentage.toFixed(1)}%</span>
      </div>
    </motion.div>
  );
}

interface LeaderboardRowProps {
  entry: {
    rank: number;
    streamer: string;
    avgViewers: number;
    peakViewers: number;
    isPartner: boolean;
    isYou?: boolean;
  };
  sort: 'avg' | 'peak';
}

function LeaderboardRow({ entry, sort }: LeaderboardRowProps) {
  const mainValue = sort === 'avg' ? entry.avgViewers : entry.peakViewers;
  const subValue = sort === 'avg' ? entry.peakViewers : entry.avgViewers;
  const subLabel = sort === 'avg' ? 'Peak' : 'Avg';

  return (
    <motion.div
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay: Math.min(entry.rank * 0.03, 0.5) }}
      className={`flex items-center gap-3 p-3 rounded-lg ${
        entry.isYou
          ? 'bg-primary/15 border border-primary/30'
          : 'bg-background hover:bg-background/80'
      }`}
    >
      <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold shrink-0 ${
        entry.rank === 1 ? 'bg-gradient-to-br from-yellow-400 to-amber-600 text-white' :
        entry.rank === 2 ? 'bg-gradient-to-br from-gray-300 to-gray-500 text-white' :
        entry.rank === 3 ? 'bg-gradient-to-br from-amber-600 to-amber-800 text-white' :
        'bg-border text-text-secondary'
      }`}>
        {entry.rank}
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className={`font-medium truncate ${entry.isYou ? 'text-primary' : 'text-white'}`}>
            {entry.streamer}
          </span>
          {entry.isPartner && (
            <span className="text-xs px-1.5 py-0.5 bg-accent/20 text-accent rounded">Partner</span>
          )}
          {entry.isYou && (
            <span className="text-xs px-1.5 py-0.5 bg-primary/20 text-primary rounded">Du</span>
          )}
        </div>
      </div>

      <div className="text-right shrink-0">
        <div className="text-white font-bold">{mainValue.toLocaleString('de-DE', { maximumFractionDigits: 0 })}</div>
        <div className="text-xs text-text-secondary">{subLabel}: {subValue.toLocaleString('de-DE', { maximumFractionDigits: 0 })}</div>
      </div>
    </motion.div>
  );
}

export default Comparison;
