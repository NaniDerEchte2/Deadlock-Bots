import { motion } from 'framer-motion';
import { Clock, TrendingUp, TrendingDown, Users, Timer } from 'lucide-react';
import type { WatchTimeDistribution as WatchTimeDistributionType } from '@/types/analytics';

interface WatchTimeDistributionProps {
  data: WatchTimeDistributionType;
}

const SEGMENTS = [
  { key: 'under5min' as const, label: '< 5 Min', shortLabel: '<5m', color: '#ef4444', description: 'Schnelle Absprünge' },
  { key: 'min5to15' as const, label: '5-15 Min', shortLabel: '5-15m', color: '#f97316', description: 'Kurze Sessions' },
  { key: 'min15to30' as const, label: '15-30 Min', shortLabel: '15-30m', color: '#eab308', description: 'Mittlere Sessions' },
  { key: 'min30to60' as const, label: '30-60 Min', shortLabel: '30-60m', color: '#22c55e', description: 'Längere Sessions' },
  { key: 'over60min' as const, label: '> 60 Min', shortLabel: '60m+', color: '#10b981', description: 'Loyale Zuschauer' },
] as const;

export function WatchTimeDistribution({ data }: WatchTimeDistributionProps) {
  const totalLoyalViewers = data.min30to60 + data.over60min;
  const hasPrevious = data.previous && data.previous.sessionCount !== undefined && data.previous.sessionCount > 0;
  const prevLoyalViewers = hasPrevious ? (data.previous!.min30to60 + data.previous!.over60min) : 0;

  const avgDelta = data.deltas?.avgWatchTime;
  const avgTrendUp = avgDelta != null && avgDelta >= 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-primary/20 flex items-center justify-center">
            <Clock className="w-5 h-5 text-primary" />
          </div>
          <div>
            <h3 className="text-lg font-bold text-white">Watch Time Verteilung</h3>
            <p className="text-sm text-text-secondary">Wie lange bleiben deine Viewer?</p>
          </div>
        </div>
        {hasPrevious && (
          <div className="text-xs text-text-secondary bg-background px-2 py-1 rounded">
            vs. Vorperiode ({data.previous!.sessionCount} Sessions)
          </div>
        )}
      </div>

      {/* Main Stats */}
      <div className="grid grid-cols-2 gap-4 mb-6">
        <div className="bg-background rounded-lg p-4">
          <div className="flex items-center gap-2 text-text-secondary text-sm mb-1">
            <Timer className="w-4 h-4" />
            Ø Watch Time
          </div>
          <div className="flex items-baseline gap-2">
            <span className="text-2xl font-bold text-white">{data.avgWatchTime.toFixed(1)}</span>
            <span className="text-text-secondary">Min</span>
            {avgDelta != null && (
              <span className={`flex items-center gap-1 text-sm ${avgTrendUp ? 'text-success' : 'text-error'}`}>
                {avgTrendUp ? <TrendingUp className="w-4 h-4" /> : <TrendingDown className="w-4 h-4" />}
                {Math.abs(avgDelta).toFixed(1)}%
              </span>
            )}
          </div>
          {hasPrevious && (
            <div className="text-xs text-text-secondary mt-1">
              Vorher: {data.previous!.avgWatchTime.toFixed(1)} Min
            </div>
          )}
        </div>
        <div className="bg-background rounded-lg p-4">
          <div className="flex items-center gap-2 text-text-secondary text-sm mb-1">
            <Users className="w-4 h-4" />
            Loyale Viewer
          </div>
          <div className="flex items-baseline gap-2">
            <span className="text-2xl font-bold text-success">{totalLoyalViewers.toFixed(1)}%</span>
            <span className="text-text-secondary text-sm">&gt; 30 Min</span>
          </div>
          {hasPrevious && (
            <div className="text-xs text-text-secondary mt-1">
              Vorher: {prevLoyalViewers.toFixed(1)}%
              {totalLoyalViewers !== prevLoyalViewers && (
                <span className={totalLoyalViewers > prevLoyalViewers ? 'text-success ml-1' : 'text-error ml-1'}>
                  ({totalLoyalViewers > prevLoyalViewers ? '+' : ''}{(totalLoyalViewers - prevLoyalViewers).toFixed(1)}pp)
                </span>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Distribution Comparison Bars */}
      <div className="space-y-3 mb-6">
        {SEGMENTS.map((segment, i) => {
          const currVal = data[segment.key];
          const prevVal = hasPrevious ? data.previous![segment.key] : null;
          const maxVal = Math.max(
            currVal,
            prevVal ?? 0,
            ...SEGMENTS.map(s => data[s.key]),
            ...(hasPrevious ? SEGMENTS.map(s => data.previous![s.key]) : [0]),
          );
          const barMax = Math.max(maxVal, 1);

          return (
            <motion.div
              key={segment.key}
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: i * 0.06 }}
            >
              <div className="flex items-center gap-3">
                {/* Label */}
                <div className="w-16 text-xs text-text-secondary text-right shrink-0">
                  {segment.shortLabel}
                </div>

                {/* Bars container */}
                <div className="flex-1 space-y-1">
                  {/* Current bar */}
                  <div className="flex items-center gap-2">
                    <div className="flex-1 h-5 bg-background rounded overflow-hidden">
                      <motion.div
                        initial={{ width: 0 }}
                        animate={{ width: `${(currVal / barMax) * 100}%` }}
                        transition={{ delay: 0.2 + i * 0.06, duration: 0.5 }}
                        className="h-full rounded"
                        style={{ backgroundColor: segment.color, minWidth: currVal > 0 ? '2px' : '0' }}
                      />
                    </div>
                    <span className="text-xs font-medium text-white w-12 text-right">{currVal.toFixed(1)}%</span>
                  </div>

                  {/* Previous bar (if available) */}
                  {hasPrevious && prevVal != null && (
                    <div className="flex items-center gap-2">
                      <div className="flex-1 h-3 bg-background rounded overflow-hidden">
                        <motion.div
                          initial={{ width: 0 }}
                          animate={{ width: `${(prevVal / barMax) * 100}%` }}
                          transition={{ delay: 0.3 + i * 0.06, duration: 0.5 }}
                          className="h-full rounded opacity-35"
                          style={{ backgroundColor: segment.color, minWidth: prevVal > 0 ? '2px' : '0' }}
                        />
                      </div>
                      <span className="text-[10px] text-text-secondary w-12 text-right">{prevVal.toFixed(1)}%</span>
                    </div>
                  )}
                </div>

                {/* Delta */}
                <div className="w-14 text-right shrink-0">
                  {hasPrevious && prevVal != null && prevVal > 0 ? (
                    <DeltaBadge current={currVal} previous={prevVal} inverted={segment.key === 'under5min'} />
                  ) : (
                    <span className="text-xs text-text-secondary">—</span>
                  )}
                </div>
              </div>
            </motion.div>
          );
        })}
      </div>

      {/* Legend */}
      {hasPrevious && (
        <div className="flex items-center gap-4 text-xs text-text-secondary mb-4">
          <span className="flex items-center gap-1.5">
            <div className="w-3 h-3 rounded bg-primary" /> Aktuell ({data.sessionCount ?? '?'} Sessions)
          </span>
          <span className="flex items-center gap-1.5">
            <div className="w-3 h-2 rounded bg-primary opacity-35" /> Vorperiode ({data.previous!.sessionCount ?? '?'} Sessions)
          </span>
        </div>
      )}

      {/* Insights */}
      <div className="pt-4 border-t border-border">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {data.under5min > 30 && (
            <InsightBadge
              type="warning"
              text={`${data.under5min.toFixed(0)}% springen in den ersten 5 Min ab - optimiere deinen Stream-Start!`}
            />
          )}
          {totalLoyalViewers > 40 && (
            <InsightBadge
              type="success"
              text={`${totalLoyalViewers.toFixed(0)}% bleiben über 30 Min - starke Community-Bindung!`}
            />
          )}
          {hasPrevious && avgDelta != null && avgDelta > 10 && (
            <InsightBadge
              type="success"
              text={`Watch Time um ${avgDelta.toFixed(0)}% gestiegen vs. Vorperiode!`}
            />
          )}
          {hasPrevious && avgDelta != null && avgDelta < -10 && (
            <InsightBadge
              type="warning"
              text={`Watch Time um ${Math.abs(avgDelta).toFixed(0)}% gesunken - prüfe was sich geändert hat.`}
            />
          )}
          {data.avgWatchTime < 15 && (
            <InsightBadge
              type="warning"
              text="Durchschnittliche Watch Time unter 15 Min - teste längere Engagement-Segmente"
            />
          )}
        </div>
      </div>
    </motion.div>
  );
}

function DeltaBadge({ current, previous, inverted = false }: { current: number; previous: number; inverted?: boolean }) {
  const diff = current - previous;
  // For "under5min", less is better (inverted)
  const isPositive = inverted ? diff < 0 : diff > 0;
  const color = Math.abs(diff) < 0.5 ? 'text-text-secondary' : isPositive ? 'text-success' : 'text-error';

  return (
    <span className={`text-[11px] font-medium ${color}`}>
      {diff > 0 ? '+' : ''}{diff.toFixed(1)}pp
    </span>
  );
}

interface InsightBadgeProps {
  type: 'success' | 'warning' | 'info';
  text: string;
}

function InsightBadge({ type, text }: InsightBadgeProps) {
  const styles = {
    success: 'bg-success/10 border-success/20 text-success',
    warning: 'bg-warning/10 border-warning/20 text-warning',
    info: 'bg-primary/10 border-primary/20 text-primary',
  };

  return (
    <div className={`px-3 py-2 rounded-lg border text-xs ${styles[type]}`}>
      {text}
    </div>
  );
}

export default WatchTimeDistribution;
