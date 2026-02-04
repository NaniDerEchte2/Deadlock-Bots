import { motion } from 'framer-motion';
import { UserPlus, Users, Heart, TrendingUp, TrendingDown, Zap, Radio, Share2 } from 'lucide-react';
import type { FollowerFunnel as FollowerFunnelType } from '@/types/analytics';

interface FollowerFunnelProps {
  data: FollowerFunnelType;
  previousConversionRate?: number;
}

export function FollowerFunnel({ data, previousConversionRate }: FollowerFunnelProps) {
  const conversionTrend = previousConversionRate
    ? ((data.conversionRate - previousConversionRate) / previousConversionRate) * 100
    : null;

  const funnelStages = [
    {
      label: 'Unique Viewer',
      value: data.uniqueViewers,
      icon: Users,
      color: 'from-blue-500 to-blue-600',
      width: 100,
    },
    {
      label: 'Wiederkehrend',
      value: data.returningViewers,
      icon: Heart,
      color: 'from-purple-500 to-purple-600',
      width: data.uniqueViewers > 0 ? (data.returningViewers / data.uniqueViewers) * 100 : 0,
    },
    {
      label: 'Neue Follower',
      value: data.newFollowers,
      icon: UserPlus,
      color: 'from-green-500 to-green-600',
      width: data.uniqueViewers > 0 ? (data.newFollowers / data.uniqueViewers) * 100 : 0,
    },
  ];

  const sourceData = [
    { label: 'Organisch', value: data.followersBySource.organic, icon: Zap, color: 'text-green-500' },
    { label: 'Raids', value: data.followersBySource.raids, icon: Radio, color: 'text-purple-500' },
    { label: 'Hosts', value: data.followersBySource.hosts, icon: Share2, color: 'text-blue-500' },
    { label: 'Sonstige', value: data.followersBySource.other, icon: Users, color: 'text-gray-500' },
  ];

  const totalSourceFollowers = sourceData.reduce((sum, s) => sum + s.value, 0);

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-success/20 flex items-center justify-center">
            <UserPlus className="w-5 h-5 text-success" />
          </div>
          <div>
            <h3 className="text-lg font-bold text-white">Follower Conversion Funnel</h3>
            <p className="text-sm text-text-secondary">Von Viewer zu Follower</p>
          </div>
        </div>
      </div>

      {/* Main Conversion Rate */}
      <div className="bg-gradient-to-r from-primary/10 to-success/10 rounded-xl p-5 mb-6 border border-primary/20">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm text-text-secondary mb-1">Conversion Rate</div>
            <div className="flex items-baseline gap-3">
              <span className="text-4xl font-bold text-white">{data.conversionRate.toFixed(2)}%</span>
              {conversionTrend !== null && (
                <span className={`flex items-center gap-1 text-sm ${conversionTrend >= 0 ? 'text-success' : 'text-error'}`}>
                  {conversionTrend >= 0 ? <TrendingUp className="w-4 h-4" /> : <TrendingDown className="w-4 h-4" />}
                  {Math.abs(conversionTrend).toFixed(1)}% vs. Vorperiode
                </span>
              )}
            </div>
          </div>
          <div className="text-right">
            <div className="text-sm text-text-secondary mb-1">Ø Zeit bis Follow</div>
            <div className="text-2xl font-bold text-white">{data.avgTimeToFollow.toFixed(0)} Min</div>
          </div>
        </div>
      </div>

      {/* Funnel Visualization */}
      <div className="space-y-3 mb-6">
        {funnelStages.map((stage, i) => {
          const Icon = stage.icon;
          return (
            <motion.div
              key={stage.label}
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: i * 0.1 }}
              className="relative"
            >
              <div className="flex items-center gap-4">
                <div className={`w-10 h-10 rounded-lg bg-gradient-to-r ${stage.color} flex items-center justify-center flex-shrink-0`}>
                  <Icon className="w-5 h-5 text-white" />
                </div>
                <div className="flex-1">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-sm font-medium text-white">{stage.label}</span>
                    <span className="text-sm text-text-secondary">
                      {stage.value.toLocaleString('de-DE')}
                    </span>
                  </div>
                  <div className="h-3 bg-background rounded-full overflow-hidden">
                    <motion.div
                      initial={{ width: 0 }}
                      animate={{ width: `${Math.max(stage.width, 2)}%` }}
                      transition={{ delay: 0.3 + i * 0.1, duration: 0.5 }}
                      className={`h-full bg-gradient-to-r ${stage.color} rounded-full`}
                    />
                  </div>
                </div>
              </div>
              {i < funnelStages.length - 1 && (
                <div className="absolute left-5 top-12 h-3 w-px bg-border" />
              )}
            </motion.div>
          );
        })}
      </div>

      {/* Follower Sources */}
      <div className="border-t border-border pt-4">
        <h4 className="text-sm font-medium text-text-secondary mb-3">Follower-Quellen</h4>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {sourceData.map((source, i) => {
            const Icon = source.icon;
            const percentage = totalSourceFollowers > 0 ? (source.value / totalSourceFollowers) * 100 : 0;
            return (
              <motion.div
                key={source.label}
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                transition={{ delay: 0.5 + i * 0.05 }}
                className="bg-background rounded-lg p-3 text-center"
              >
                <Icon className={`w-5 h-5 mx-auto mb-2 ${source.color}`} />
                <div className="text-xs text-text-secondary mb-1">{source.label}</div>
                <div className="text-lg font-bold text-white">{source.value}</div>
                <div className="text-xs text-text-secondary">{percentage.toFixed(1)}%</div>
              </motion.div>
            );
          })}
        </div>
      </div>

      {/* Insights */}
      <div className="mt-4 pt-4 border-t border-border">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {data.conversionRate > 2 && (
            <InsightBadge
              type="success"
              text={`Überdurchschnittliche Conversion von ${data.conversionRate.toFixed(2)}%!`}
            />
          )}
          {data.conversionRate < 0.5 && (
            <InsightBadge
              type="warning"
              text="Niedrige Conversion - teste Call-to-Actions während des Streams"
            />
          )}
          {data.followersBySource.raids > data.followersBySource.organic * 0.5 && (
            <InsightBadge
              type="info"
              text={`${((data.followersBySource.raids / totalSourceFollowers) * 100).toFixed(0)}% der Follower kommen über Raids - pflege dein Netzwerk!`}
            />
          )}
          {data.avgTimeToFollow < 30 && (
            <InsightBadge
              type="success"
              text={`Schnelle Conversion! Viewer folgen im Schnitt nach ${data.avgTimeToFollow.toFixed(0)} Min`}
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

export default FollowerFunnel;
