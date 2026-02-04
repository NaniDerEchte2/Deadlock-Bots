import { motion } from 'framer-motion';
import { Clock, TrendingUp, TrendingDown, Minus, Users, Timer } from 'lucide-react';
import type { WatchTimeDistribution as WatchTimeDistributionType } from '@/types/analytics';

interface WatchTimeDistributionProps {
  data: WatchTimeDistributionType;
  previousPeriodAvg?: number; // Für Trend-Vergleich
}

export function WatchTimeDistribution({ data, previousPeriodAvg }: WatchTimeDistributionProps) {
  const segments = [
    { label: '< 5 Min', value: data.under5min, color: 'from-red-500 to-red-600', description: 'Schnelle Absprünge' },
    { label: '5-15 Min', value: data.min5to15, color: 'from-orange-500 to-orange-600', description: 'Kurze Sessions' },
    { label: '15-30 Min', value: data.min15to30, color: 'from-yellow-500 to-yellow-600', description: 'Mittlere Sessions' },
    { label: '30-60 Min', value: data.min30to60, color: 'from-green-500 to-green-600', description: 'Längere Sessions' },
    { label: '> 60 Min', value: data.over60min, color: 'from-emerald-500 to-emerald-600', description: 'Loyale Zuschauer' },
  ];

  const totalLoyalViewers = data.min30to60 + data.over60min;
  const trend = previousPeriodAvg ? ((data.avgWatchTime - previousPeriodAvg) / previousPeriodAvg) * 100 : null;

  const TrendIcon = trend === null ? Minus : trend >= 0 ? TrendingUp : TrendingDown;
  const trendColor = trend === null ? 'text-text-secondary' : trend >= 0 ? 'text-success' : 'text-error';

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
            {trend !== null && (
              <span className={`flex items-center gap-1 text-sm ${trendColor}`}>
                <TrendIcon className="w-4 h-4" />
                {Math.abs(trend).toFixed(1)}%
              </span>
            )}
          </div>
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
        </div>
      </div>

      {/* Distribution Bar */}
      <div className="mb-4">
        <div className="flex h-8 rounded-lg overflow-hidden">
          {segments.map((segment, i) => (
            <motion.div
              key={segment.label}
              initial={{ width: 0 }}
              animate={{ width: `${segment.value}%` }}
              transition={{ delay: i * 0.1, duration: 0.5 }}
              className={`bg-gradient-to-r ${segment.color} relative group cursor-pointer`}
              style={{ minWidth: segment.value > 0 ? '2px' : '0' }}
            >
              {/* Tooltip */}
              <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 opacity-0 group-hover:opacity-100 transition-opacity z-20 pointer-events-none">
                <div className="bg-card border border-border rounded-lg px-3 py-2 text-xs whitespace-nowrap shadow-xl">
                  <div className="font-medium text-white">{segment.label}</div>
                  <div className="text-text-secondary">{segment.value.toFixed(1)}% der Viewer</div>
                  <div className="text-text-secondary">{segment.description}</div>
                </div>
              </div>
            </motion.div>
          ))}
        </div>
      </div>

      {/* Legend */}
      <div className="grid grid-cols-5 gap-2">
        {segments.map((segment, i) => (
          <motion.div
            key={segment.label}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.3 + i * 0.05 }}
            className="text-center"
          >
            <div className={`w-3 h-3 rounded-full bg-gradient-to-r ${segment.color} mx-auto mb-1`} />
            <div className="text-xs text-text-secondary">{segment.label}</div>
            <div className="text-sm font-medium text-white">{segment.value.toFixed(1)}%</div>
          </motion.div>
        ))}
      </div>

      {/* Insights */}
      <div className="mt-6 pt-4 border-t border-border">
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
          {data.over60min > 20 && (
            <InsightBadge
              type="success"
              text={`${data.over60min.toFixed(0)}% schauen über 1h - deine Hardcore-Fans!`}
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
