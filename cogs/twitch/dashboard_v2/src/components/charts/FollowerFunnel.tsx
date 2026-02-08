import { motion } from 'framer-motion';
import { UserPlus, Users, Heart, TrendingUp, TrendingDown, Zap, Radio, Share2, Info, ArrowRight } from 'lucide-react';
import { useState } from 'react';
import type { FollowerFunnel as FollowerFunnelType } from '@/types/analytics';

interface FollowerFunnelProps {
  data: FollowerFunnelType;
  previousConversionRate?: number;
}

function rateConversion(rate: number): { label: string; color: string; bgColor: string; description: string; position: number } {
  if (rate < 0.5) {
    const position = Math.max(2, (rate / 0.5) * 25);
    return {
      label: 'Niedrig', color: 'text-error', bgColor: 'bg-error/20',
      description: 'Teste Follow-Reminders und Call-to-Actions während des Streams.',
      position,
    };
  }
  if (rate < 2) {
    const position = 25 + ((rate - 0.5) / 1.5) * 25;
    return {
      label: 'Durchschnitt', color: 'text-warning', bgColor: 'bg-warning/20',
      description: 'Solide Basis - interaktive Segmente und Raids können die Rate steigern.',
      position,
    };
  }
  if (rate < 5) {
    const position = 50 + ((rate - 2) / 3) * 25;
    return {
      label: 'Gut', color: 'text-success', bgColor: 'bg-success/20',
      description: 'Überdurchschnittlich! Dein Content überzeugt neue Viewer zum Folgen.',
      position,
    };
  }
  const position = Math.min(98, 75 + ((rate - 5) / 5) * 25);
  return {
    label: 'Exzellent', color: 'text-emerald-400', bgColor: 'bg-emerald-400/20',
    description: 'Herausragend - deine Nische und Community-Bindung sind stark.',
    position,
  };
}

export function FollowerFunnel({ data, previousConversionRate }: FollowerFunnelProps) {
  const [showExplainer, setShowExplainer] = useState(false);

  const conversionTrend = previousConversionRate
    ? ((data.conversionRate - previousConversionRate) / previousConversionRate) * 100
    : null;

  const rating = rateConversion(data.conversionRate);

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

      {/* Conversion Rate Hero Section */}
      <div className="bg-gradient-to-r from-primary/10 to-success/10 rounded-xl p-5 mb-6 border border-primary/20">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <span className="text-sm text-text-secondary">Conversion Rate</span>
            <button
              onClick={() => setShowExplainer(!showExplainer)}
              className="text-text-secondary hover:text-white transition-colors"
              title="Was ist die Conversion Rate?"
            >
              <Info className="w-4 h-4" />
            </button>
          </div>
          {rating.label && (
            <span className={`text-xs font-medium px-2 py-0.5 rounded ${rating.color} ${rating.bgColor}`}>
              {rating.label}
            </span>
          )}
        </div>

        {/* Explainer dropdown */}
        {showExplainer && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="mb-4 p-3 bg-background/60 rounded-lg text-xs text-text-secondary space-y-2"
          >
            <p className="font-medium text-white text-sm">Was bedeutet Conversion Rate?</p>
            <p>
              Die Conversion Rate zeigt, wie viele deiner Zuschauer zu Followern werden.
              Sie berechnet sich aus: <span className="text-white font-mono">Neue Follower / Unique Viewer × 100</span>
            </p>
            <p>
              Die Unique Viewer werden aus der Chatter-Zahl hochgerechnet (ca. 12.5% aller Viewer chatten).
              Das ist eine Schätzung - Twitch gibt keine exakten Unique-Viewer-Zahlen.
            </p>
            <div className="grid grid-cols-4 gap-2 pt-2 border-t border-border">
              <div className="text-center">
                <div className="text-error font-medium">&lt; 0.5%</div>
                <div className="text-[10px]">Niedrig</div>
              </div>
              <div className="text-center">
                <div className="text-warning font-medium">0.5-2%</div>
                <div className="text-[10px]">Durchschnitt</div>
              </div>
              <div className="text-center">
                <div className="text-success font-medium">2-5%</div>
                <div className="text-[10px]">Gut</div>
              </div>
              <div className="text-center">
                <div className="text-emerald-400 font-medium">&gt; 5%</div>
                <div className="text-[10px]">Exzellent</div>
              </div>
            </div>
          </motion.div>
        )}

        {/* Rate + Formula */}
        <div className="flex items-center justify-between">
          <div>
            <div className="flex items-baseline gap-3">
              <span className="text-4xl font-bold text-white">{data.conversionRate.toFixed(2)}%</span>
              {conversionTrend !== null && (
                <span className={`flex items-center gap-1 text-sm ${conversionTrend >= 0 ? 'text-success' : 'text-error'}`}>
                  {conversionTrend >= 0 ? <TrendingUp className="w-4 h-4" /> : <TrendingDown className="w-4 h-4" />}
                  {Math.abs(conversionTrend).toFixed(1)}% vs. Vorperiode
                </span>
              )}
            </div>
            {/* Formula breakdown */}
            <div className="flex items-center gap-2 mt-2 text-xs text-text-secondary">
              <span className="text-white font-medium">+{data.newFollowers}</span>
              <span>gewonnen</span>
              <span>/</span>
              <span className="text-white font-medium">{data.uniqueViewers.toLocaleString('de-DE')}</span>
              <span>Viewer</span>
              <ArrowRight className="w-3 h-3" />
              <span className={rating.color}>{data.conversionRate.toFixed(2)}%</span>
            </div>
            {data.netFollowerDelta !== undefined && data.netFollowerDelta !== data.newFollowers && (
              <div className="flex items-center gap-2 mt-1 text-xs">
                <span className={data.netFollowerDelta >= 0 ? 'text-success' : 'text-error'}>
                  Netto: {data.netFollowerDelta >= 0 ? '+' : ''}{data.netFollowerDelta}
                </span>
                <span className="text-text-secondary">
                  ({Math.abs(data.netFollowerDelta - data.newFollowers)} verloren)
                </span>
              </div>
            )}
          </div>
          <div className="text-right">
            <div className="text-sm text-text-secondary mb-1">Ø Zeit bis Follow</div>
            <div className="text-2xl font-bold text-white">{data.avgTimeToFollow.toFixed(0)} Min</div>
          </div>
        </div>

        {/* Benchmark Gauge */}
        <div className="mt-4">
          <div className="relative h-2 rounded-full overflow-hidden flex">
            <div className="flex-1 bg-red-500/40" />
            <div className="flex-1 bg-yellow-500/40" />
            <div className="flex-1 bg-green-500/40" />
            <div className="flex-1 bg-emerald-500/40" />
          </div>
          {rating.position > 0 && (
            <motion.div
              initial={{ left: '0%' }}
              animate={{ left: `${Math.min(rating.position, 98)}%` }}
              transition={{ delay: 0.3, duration: 0.6 }}
              className="relative -top-2.5 w-3 h-3 rounded-full bg-white border-2 border-primary shadow-md"
              style={{ marginLeft: '-6px' }}
            />
          )}
          <div className="flex justify-between text-[10px] text-text-secondary mt-0.5">
            <span>0%</span>
            <span>0.5%</span>
            <span>2%</span>
            <span>5%</span>
            <span>10%+</span>
          </div>
        </div>

        {/* Rating tip */}
        {rating.description && (
          <div className={`mt-3 text-xs ${rating.color}`}>
            {rating.description}
          </div>
        )}
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
          {data.followersBySource.raids > data.followersBySource.organic * 0.5 && totalSourceFollowers > 0 && (
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
