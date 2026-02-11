import { useMemo } from 'react';
import { motion } from 'framer-motion';
import {
  GraduationCap, AlertTriangle, TrendingDown, Clock, Calendar, Tag, Users,
  Search, Type, UserMinus, Zap, AlertCircle, Loader2, ChevronRight,
  BarChart3, Target, Timer, MessageCircle, ArrowLeftRight, Trophy, Activity,
} from 'lucide-react';
import {
  BarChart, Bar, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend, Cell,
} from 'recharts';
import { useCoaching } from '@/hooks/useAnalytics';
import type { TimeRange, CoachingData, CoachingRecommendation } from '@/types/analytics';

interface CoachingProps {
  streamer: string;
  days: TimeRange;
}

const WEEKDAY_SHORT = ['So', 'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa'];

const PRIORITY_STYLES: Record<string, { bg: string; border: string; text: string; badge: string }> = {
  critical: { bg: 'bg-error/10', border: 'border-error/30', text: 'text-error', badge: 'bg-error text-white' },
  high: { bg: 'bg-warning/10', border: 'border-warning/30', text: 'text-warning', badge: 'bg-warning text-black' },
  medium: { bg: 'bg-primary/10', border: 'border-primary/30', text: 'text-primary', badge: 'bg-primary text-white' },
  low: { bg: 'bg-text-secondary/10', border: 'border-border', text: 'text-text-secondary', badge: 'bg-text-secondary/30 text-text-secondary' },
};

const ICON_MAP: Record<string, React.ComponentType<{ className?: string }>> = {
  AlertTriangle, TrendingDown, Clock, Calendar, Tag, Users, Search, Type, UserMinus,
};

function getRecommendationIcon(iconName: string) {
  return ICON_MAP[iconName] || AlertCircle;
}

export function Coaching({ streamer, days }: CoachingProps) {
  const { data, isLoading } = useCoaching(streamer, days);

  const { topRecs, otherRecs } = useMemo(() => {
    if (!data?.recommendations) return { topRecs: [], otherRecs: [] };
    const top = data.recommendations.filter(r => r.priority === 'critical' || r.priority === 'high');
    const other = data.recommendations.filter(r => r.priority === 'medium' || r.priority === 'low');
    return { topRecs: top, otherRecs: other };
  }, [data]);

  if (!streamer) {
    return (
      <div className="flex flex-col items-center justify-center h-64">
        <AlertCircle className="w-12 h-12 text-text-secondary mb-4" />
        <p className="text-text-secondary text-lg">Waehle einen Streamer aus</p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  if (!data || data.empty) {
    return (
      <div className="flex flex-col items-center justify-center h-64">
        <GraduationCap className="w-12 h-12 text-text-secondary mb-4" />
        <p className="text-text-secondary text-lg">Keine Daten fuer Coaching-Analyse</p>
        <p className="text-text-secondary text-sm mt-2">Streame mehr, um personalisierte Empfehlungen zu erhalten!</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* 1. Top Recommendations (Critical + High) */}
      {topRecs.length > 0 && (
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }}>
          <div className="flex items-center gap-3 mb-4">
            <Zap className="w-6 h-6 text-warning" />
            <h2 className="text-xl font-bold text-white">Top-Empfehlungen</h2>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {topRecs.map((rec, i) => (
              <RecommendationCard key={i} rec={rec} index={i} />
            ))}
          </div>
        </motion.div>
      )}

      {/* 2. Efficiency Comparison */}
      <EfficiencySection data={data} />

      {/* 3. Duration Sweetspot */}
      <DurationSection data={data} />

      {/* 4. Schedule Optimizer */}
      <ScheduleSection data={data} />

      {/* 5. Title Coach */}
      <TitleSection data={data} />

      {/* 6. Tag Optimization */}
      <TagSection data={data} />

      {/* 7. Retention Curves */}
      <RetentionSection data={data} />

      {/* 8. Cross-Community */}
      <CommunitySection data={data} />

      {/* 9. Double-Stream Warning (conditional) */}
      {data.doubleStreamDetection?.detected && (
        <DoubleStreamSection data={data} />
      )}

      {/* 10. Chat-Konzentration */}
      <ChatConcentrationSection data={data} />

      {/* 11. Raid-Netzwerk */}
      <RaidNetworkSection data={data} />

      {/* 12. Peer-Vergleich */}
      <PeerComparisonSection data={data} />

      {/* 13. Konkurrenz-Dichte */}
      <CompetitionDensitySection data={data} />

      {/* 14. More Recommendations (Medium + Low) */}
      {otherRecs.length > 0 && (
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.5 }}
        >
          <div className="flex items-center gap-3 mb-4">
            <Target className="w-6 h-6 text-primary" />
            <h2 className="text-xl font-bold text-white">Weitere Empfehlungen</h2>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {otherRecs.map((rec, i) => (
              <RecommendationCard key={i} rec={rec} index={i} />
            ))}
          </div>
        </motion.div>
      )}
    </div>
  );
}


// ---------------------------------------------------------------------------
// Recommendation Card
// ---------------------------------------------------------------------------

function RecommendationCard({ rec, index }: { rec: CoachingRecommendation; index: number }) {
  const styles = PRIORITY_STYLES[rec.priority] || PRIORITY_STYLES.low;
  const Icon = getRecommendationIcon(rec.icon);

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ delay: index * 0.05 }}
      className={`${styles.bg} rounded-xl border ${styles.border} p-5`}
    >
      <div className="flex items-start gap-3">
        <div className={`p-2 rounded-lg ${styles.bg}`}>
          <Icon className={`w-5 h-5 ${styles.text}`} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${styles.badge}`}>
              {rec.priority.toUpperCase()}
            </span>
            <span className="text-xs text-text-secondary">{rec.category}</span>
          </div>
          <h3 className="font-semibold text-white mb-1">{rec.title}</h3>
          <p className="text-sm text-text-secondary mb-2">{rec.description}</p>
          {rec.estimatedImpact && (
            <div className="flex items-center gap-1 text-xs text-accent">
              <ChevronRight className="w-3 h-3" />
              <span>{rec.estimatedImpact}</span>
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}


// ---------------------------------------------------------------------------
// 2. Efficiency Section
// ---------------------------------------------------------------------------

function EfficiencySection({ data }: { data: CoachingData }) {
  const eff = data.efficiency;
  if (!eff) return null;

  const barData = [
    ...(eff.topPerformers || []).slice(0, 5).map(tp => ({
      name: tp.streamer,
      ratio: tp.ratio,
      isYou: tp.streamer === data.streamer.toLowerCase(),
    })),
  ];

  // Ensure streamer is in the list
  if (!barData.find(b => b.isYou)) {
    barData.push({ name: data.streamer, ratio: eff.viewerHoursPerStreamHour, isYou: true });
    barData.sort((a, b) => b.ratio - a.ratio);
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.1 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center gap-3 mb-6">
        <BarChart3 className="w-6 h-6 text-primary" />
        <h2 className="text-xl font-bold text-white">Effizienz-Vergleich</h2>
        <span className="text-xs text-text-secondary ml-auto">Viewer-Hours pro Stream-Hour</span>
      </div>

      <div className="grid grid-cols-3 gap-4 mb-6">
        <div className="text-center">
          <div className="text-2xl font-bold text-white">{eff.viewerHoursPerStreamHour}</div>
          <div className="text-xs text-text-secondary">Deine Effizienz</div>
        </div>
        <div className="text-center">
          <div className="text-2xl font-bold text-accent">{eff.categoryAvg}</div>
          <div className="text-xs text-text-secondary">Kategorie-Schnitt</div>
        </div>
        <div className="text-center">
          <div className={`text-2xl font-bold ${eff.percentile >= 50 ? 'text-success' : 'text-warning'}`}>
            {eff.percentile}%
          </div>
          <div className="text-xs text-text-secondary">Perzentil</div>
        </div>
      </div>

      {barData.length > 0 && (
        <div className="space-y-2">
          {barData.map((item) => {
            const maxRatio = Math.max(...barData.map(b => b.ratio), 1);
            const width = (item.ratio / maxRatio) * 100;
            return (
              <div key={item.name} className="flex items-center gap-3">
                <span className={`text-sm w-28 truncate ${item.isYou ? 'text-accent font-semibold' : 'text-text-secondary'}`}>
                  {item.name}
                </span>
                <div className="flex-1 bg-background rounded-full h-6 overflow-hidden">
                  <motion.div
                    initial={{ width: 0 }}
                    animate={{ width: `${width}%` }}
                    transition={{ duration: 0.8, delay: 0.1 }}
                    className={`h-full rounded-full ${item.isYou ? 'bg-accent' : 'bg-primary/60'}`}
                  />
                </div>
                <span className={`text-sm w-12 text-right ${item.isYou ? 'text-accent font-semibold' : 'text-text-secondary'}`}>
                  {item.ratio}
                </span>
              </div>
            );
          })}
          {/* Category average line indicator */}
          <div className="flex items-center gap-3 mt-1">
            <span className="text-xs text-text-secondary w-28">Kat.-Schnitt</span>
            <div className="flex-1 relative h-1">
              <div
                className="absolute top-0 h-4 -mt-1.5 w-0.5 bg-warning"
                style={{ left: `${(eff.categoryAvg / Math.max(...barData.map(b => b.ratio), 1)) * 100}%` }}
              />
            </div>
            <span className="text-xs text-warning w-12 text-right">{eff.categoryAvg}</span>
          </div>
        </div>
      )}

      <div className="flex gap-4 mt-4 text-xs text-text-secondary">
        <span>{eff.totalStreamHours}h gestreamt</span>
        <span>{eff.totalViewerHours.toLocaleString()}h Viewer-Hours</span>
      </div>
    </motion.div>
  );
}


// ---------------------------------------------------------------------------
// 3. Duration Section
// ---------------------------------------------------------------------------

function DurationSection({ data }: { data: CoachingData }) {
  const dur = data.durationAnalysis;
  if (!dur || !dur.buckets.length) return null;

  const chartData = dur.buckets.map(b => ({
    ...b,
    isOptimal: b.label === dur.optimalLabel,
  }));

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.15 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center gap-3 mb-6">
        <Timer className="w-6 h-6 text-accent" />
        <h2 className="text-xl font-bold text-white">Dauer-Sweetspot</h2>
        <span className="text-xs text-text-secondary ml-auto">
          Dein Ø: {dur.currentAvgHours}h | Optimal: {dur.optimalLabel || '–'}
        </span>
      </div>

      <ResponsiveContainer width="100%" height={250}>
        <BarChart data={chartData}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
          <XAxis dataKey="label" stroke="#888" fontSize={12} />
          <YAxis stroke="#888" fontSize={12} />
          <Tooltip
            contentStyle={{ background: '#1a1a2e', border: '1px solid #333', borderRadius: '8px' }}
            labelStyle={{ color: '#fff' }}
            itemStyle={{ color: '#ccc' }}
            formatter={((value: number, name: string) => {
              if (name === 'avgViewers') return [`${value.toFixed(1)}`, 'Ø Viewer'];
              if (name === 'streamCount') return [value, 'Streams'];
              return [value, name];
            }) as any}
          />
          <Bar dataKey="avgViewers" name="avgViewers" radius={[4, 4, 0, 0]}>
            {chartData.map((entry, idx) => (
              <Cell
                key={idx}
                fill={entry.isOptimal ? '#10b981' : 'rgba(124, 58, 237, 0.6)'}
                stroke={entry.isOptimal ? '#10b981' : 'transparent'}
                strokeWidth={entry.isOptimal ? 2 : 0}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      {dur.correlation !== 0 && (
        <div className="mt-4 text-xs text-text-secondary">
          Korrelation Dauer vs Viewer: <span className={dur.correlation < -0.2 ? 'text-warning' : 'text-text-secondary'}>
            r = {dur.correlation}
          </span>
          {dur.correlation < -0.2 && ' (Laengere Streams = weniger Viewer)'}
        </div>
      )}
    </motion.div>
  );
}


// ---------------------------------------------------------------------------
// 4. Schedule Section (Opportunity Heatmap)
// ---------------------------------------------------------------------------

function ScheduleSection({ data }: { data: CoachingData }) {
  const sched = data.scheduleOptimizer;
  if (!sched) return null;

  // Build 7x24 grid
  const grid = useMemo(() => {
    const cells: Record<string, { opportunity: number; competitors: number; viewers: number; isYourSlot: boolean }> = {};
    let maxOpp = 1;

    for (const h of sched.competitionHeatmap) {
      const key = `${h.weekday}-${h.hour}`;
      const opp = h.competitors > 0 ? h.categoryViewers / h.competitors : h.categoryViewers;
      cells[key] = { opportunity: opp, competitors: h.competitors, viewers: h.categoryViewers, isYourSlot: false };
      if (opp > maxOpp) maxOpp = opp;
    }

    for (const s of sched.yourCurrentSlots) {
      const key = `${s.weekday}-${s.hour}`;
      if (cells[key]) cells[key].isYourSlot = true;
    }

    return { cells, maxOpp };
  }, [sched]);

  const hours = Array.from({ length: 24 }, (_, i) => i);

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.2 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center gap-3 mb-6">
        <Calendar className="w-6 h-6 text-primary" />
        <h2 className="text-xl font-bold text-white">Schedule-Optimizer</h2>
      </div>

      {/* Sweet Spots */}
      {sched.sweetSpots.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-medium text-text-secondary mb-3">Top Sweet-Spots (hohe Viewer, wenig Konkurrenz)</h3>
          <div className="flex flex-wrap gap-2">
            {sched.sweetSpots.slice(0, 8).map((spot, i) => (
              <div
                key={i}
                className={`px-3 py-1.5 rounded-lg text-sm ${
                  i < 3 ? 'bg-success/20 border border-success/30 text-success' : 'bg-background text-text-secondary'
                }`}
              >
                {WEEKDAY_SHORT[spot.weekday]} {spot.hour}:00
                <span className="ml-1 text-xs opacity-70">
                  ({spot.categoryViewers.toFixed(0)}v / {spot.competitors}k)
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Opportunity Heatmap */}
      <div className="overflow-x-auto">
        <div className="min-w-[700px]">
          <div className="flex mb-1">
            <div className="w-10" />
            {hours.filter(h => h % 2 === 0).map(h => (
              <div key={h} className="flex-1 text-center text-xs text-text-secondary" style={{ minWidth: '28px' }}>
                {h}
              </div>
            ))}
          </div>
          {[1, 2, 3, 4, 5, 6, 0].map(weekday => (
            <div key={weekday} className="flex items-center mb-0.5">
              <div className="w-10 text-xs text-text-secondary">{WEEKDAY_SHORT[weekday]}</div>
              {hours.map(hour => {
                const key = `${weekday}-${hour}`;
                const cell = grid.cells[key];
                const opp = cell?.opportunity || 0;
                const intensity = Math.min(opp / grid.maxOpp, 1);
                const isYourSlot = cell?.isYourSlot || false;
                return (
                  <div
                    key={hour}
                    className="flex-1 aspect-square rounded-sm mx-px relative"
                    style={{
                      backgroundColor: `rgba(16, 185, 129, ${intensity * 0.8 + 0.05})`,
                      minWidth: '12px',
                      minHeight: '12px',
                    }}
                    title={`${WEEKDAY_SHORT[weekday]} ${hour}:00 - Opp: ${opp.toFixed(1)} | ${cell?.competitors || 0} Streamer | ${cell?.viewers?.toFixed(0) || 0} Viewer`}
                  >
                    {isYourSlot && (
                      <div className="absolute inset-0 border-2 border-accent rounded-sm" />
                    )}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      </div>

      <div className="flex items-center gap-4 mt-4 text-xs text-text-secondary">
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 rounded-sm bg-success/20" />
          <span>Wenig Opportunity</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 rounded-sm bg-success/80" />
          <span>Hohe Opportunity</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 rounded-sm border-2 border-accent" />
          <span>Deine Slots</span>
        </div>
      </div>
    </motion.div>
  );
}


// ---------------------------------------------------------------------------
// 5. Title Coach
// ---------------------------------------------------------------------------

function TitleSection({ data }: { data: CoachingData }) {
  const titles = data.titleAnalysis;
  if (!titles) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.25 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center gap-3 mb-6">
        <Type className="w-6 h-6 text-accent" />
        <h2 className="text-xl font-bold text-white">Titel-Coach</h2>
      </div>

      {/* Title Variety Gauge */}
      {titles.varietyPct !== undefined && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
          <div className="text-center p-3 rounded-lg bg-background/50">
            <div className={`text-2xl font-bold ${titles.varietyPct < titles.avgPeerVarietyPct * 0.5 ? 'text-error' : titles.varietyPct < titles.avgPeerVarietyPct ? 'text-warning' : 'text-success'}`}>
              {titles.varietyPct}%
            </div>
            <div className="text-xs text-text-secondary">Deine Titel-Vielfalt</div>
          </div>
          <div className="text-center p-3 rounded-lg bg-background/50">
            <div className="text-2xl font-bold text-accent">{titles.avgPeerVarietyPct}%</div>
            <div className="text-xs text-text-secondary">Peer-Durchschnitt</div>
          </div>
          <div className="text-center p-3 rounded-lg bg-background/50">
            <div className="text-2xl font-bold text-white">{titles.uniqueTitleCount}</div>
            <div className="text-xs text-text-secondary">Einzigartige Titel</div>
          </div>
          <div className="text-center p-3 rounded-lg bg-background/50">
            <div className="text-2xl font-bold text-white">{titles.totalSessionCount}</div>
            <div className="text-xs text-text-secondary">Sessions gesamt</div>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Your Titles */}
        <div>
          <h3 className="text-sm font-medium text-text-secondary mb-3">Deine Titel (nach Ø Viewer)</h3>
          <div className="space-y-2 max-h-64 overflow-y-auto">
            {titles.yourTitles.slice(0, 10).map((t, i) => (
              <div key={i} className="flex items-center gap-3 p-2 rounded-lg bg-background/50">
                <span className={`text-xs w-5 text-center ${i === 0 ? 'text-success font-bold' : 'text-text-secondary'}`}>
                  {i + 1}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-white truncate">{t.title}</div>
                  <div className="text-xs text-text-secondary">
                    {t.usageCount}x benutzt
                  </div>
                </div>
                <div className="text-right">
                  <div className="text-sm font-medium text-white">{t.avgViewers} Ø</div>
                  <div className="text-xs text-text-secondary">{t.peakViewers} Peak</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Category Top Titles + Missing Patterns */}
        <div>
          <h3 className="text-sm font-medium text-text-secondary mb-3">Kategorie-Top-Titel</h3>
          <div className="space-y-2 mb-4 max-h-40 overflow-y-auto">
            {titles.categoryTopTitles.slice(0, 5).map((t, i) => (
              <div key={i} className="flex items-center gap-3 p-2 rounded-lg bg-background/50">
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-white truncate">{t.title}</div>
                  <div className="text-xs text-text-secondary">{t.streamer}</div>
                </div>
                <span className="text-sm text-accent">{t.avgViewers} Ø</span>
              </div>
            ))}
          </div>

          {titles.yourMissingPatterns.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-text-secondary mb-2">Fehlende Keywords</h3>
              <div className="flex flex-wrap gap-1.5">
                {titles.yourMissingPatterns.map((p, i) => (
                  <span key={i} className="px-2 py-1 rounded-md bg-warning/10 border border-warning/20 text-warning text-xs">
                    {p}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}


// ---------------------------------------------------------------------------
// 6. Tag Optimization
// ---------------------------------------------------------------------------

function TagSection({ data }: { data: CoachingData }) {
  const tags = data.tagOptimization;
  if (!tags) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.3 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center gap-3 mb-6">
        <Tag className="w-6 h-6 text-primary" />
        <h2 className="text-xl font-bold text-white">Tag-Optimierung</h2>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Your Tags */}
        <div>
          <h3 className="text-sm font-medium text-text-secondary mb-3">Deine Tag-Kombinationen</h3>
          <div className="space-y-2">
            {tags.yourTags.slice(0, 8).map((t, i) => (
              <div key={i} className="flex items-center gap-3 p-2 rounded-lg bg-background/50">
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-white truncate">{t.tags}</div>
                  <div className="text-xs text-text-secondary">{t.usageCount}x</div>
                </div>
                <span className="text-sm text-accent">{t.avgViewers} Ø</span>
              </div>
            ))}
          </div>
        </div>

        {/* Category Tags + Missing */}
        <div>
          <h3 className="text-sm font-medium text-text-secondary mb-3">Kategorie-Beste Tags</h3>
          <div className="space-y-2 mb-4">
            {tags.categoryBestTags.slice(0, 5).map((t, i) => (
              <div key={i} className="flex items-center gap-3 p-2 rounded-lg bg-background/50">
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-white truncate">{t.tags}</div>
                  <div className="text-xs text-text-secondary">{t.streamerCount} Streamer</div>
                </div>
                <span className="text-sm text-success">{t.avgViewers} Ø</span>
              </div>
            ))}
          </div>

          {tags.missingHighPerformers.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-text-secondary mb-2">Fehlende Top-Tags</h3>
              <div className="flex flex-wrap gap-1.5">
                {tags.missingHighPerformers.map((t, i) => (
                  <span key={i} className="px-2 py-1 rounded-md bg-success/10 border border-success/20 text-success text-xs">
                    {t}
                  </span>
                ))}
              </div>
            </div>
          )}

          {tags.underperformingTags.length > 0 && (
            <div className="mt-3">
              <h3 className="text-sm font-medium text-text-secondary mb-2">Underperformer (unter deinem Schnitt)</h3>
              <div className="flex flex-wrap gap-1.5">
                {tags.underperformingTags.map((t, i) => (
                  <span key={i} className="px-2 py-1 rounded-md bg-error/10 border border-error/20 text-error text-xs">
                    {t}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}


// ---------------------------------------------------------------------------
// 7. Retention Curves
// ---------------------------------------------------------------------------

function RetentionSection({ data }: { data: CoachingData }) {
  const ret = data.retentionCoaching;
  if (!ret) return null;

  const chartData = useMemo(() => {
    const merged: { minute: number; you: number; top: number }[] = [];
    const maxMin = Math.max(
      ret.yourViewerCurve.length ? ret.yourViewerCurve[ret.yourViewerCurve.length - 1].minute : 0,
      ret.topPerformerCurve.length ? ret.topPerformerCurve[ret.topPerformerCurve.length - 1].minute : 0,
    );
    for (let m = 0; m <= maxMin; m += 5) {
      const yourPt = ret.yourViewerCurve.find(p => p.minute === m);
      const topPt = ret.topPerformerCurve.find(p => p.minute === m);
      merged.push({
        minute: m,
        you: yourPt?.avgViewerPct || 0,
        top: topPt?.avgViewerPct || 0,
      });
    }
    return merged;
  }, [ret]);

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.35 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center gap-3 mb-6">
        <TrendingDown className="w-6 h-6 text-warning" />
        <h2 className="text-xl font-bold text-white">Retention-Analyse</h2>
      </div>

      <div className="grid grid-cols-3 gap-4 mb-6">
        <div className="text-center">
          <div className={`text-2xl font-bold ${ret.your5mRetention >= ret.category5mRetention ? 'text-success' : 'text-warning'}`}>
            {ret.your5mRetention}%
          </div>
          <div className="text-xs text-text-secondary">Deine 5-Min Retention</div>
        </div>
        <div className="text-center">
          <div className="text-2xl font-bold text-accent">{ret.category5mRetention}%</div>
          <div className="text-xs text-text-secondary">Kategorie-Schnitt</div>
        </div>
        <div className="text-center">
          <div className="text-2xl font-bold text-primary">
            {ret.criticalDropoffMinute > 0 ? `${ret.criticalDropoffMinute} min` : '–'}
          </div>
          <div className="text-xs text-text-secondary">Kritischer Drop-off</div>
        </div>
      </div>

      {chartData.length > 1 && (
        <ResponsiveContainer width="100%" height={250}>
          <LineChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
            <XAxis dataKey="minute" stroke="#888" fontSize={12} tickFormatter={(m) => `${m}m`} />
            <YAxis stroke="#888" fontSize={12} domain={[0, 100]} tickFormatter={(v) => `${v}%`} />
            <Tooltip
              contentStyle={{ background: '#1a1a2e', border: '1px solid #333', borderRadius: '8px' }}
              labelFormatter={(m) => `Minute ${m}`}
              formatter={((value: number, name: string) => {
                const label = name === 'you' ? 'Du' : 'Top-Performer';
                return [`${value.toFixed(1)}%`, label];
              }) as any}
            />
            <Legend formatter={(v) => v === 'you' ? 'Du' : 'Top-Performer'} />
            <Line type="monotone" dataKey="you" stroke="#7c3aed" strokeWidth={2} dot={false} />
            <Line type="monotone" dataKey="top" stroke="#10b981" strokeWidth={2} dot={false} strokeDasharray="5 5" />
          </LineChart>
        </ResponsiveContainer>
      )}
    </motion.div>
  );
}


// ---------------------------------------------------------------------------
// 8. Cross-Community
// ---------------------------------------------------------------------------

function CommunitySection({ data }: { data: CoachingData }) {
  const comm = data.crossCommunity;
  if (!comm || comm.totalUniqueChatters === 0) return null;

  const maxShared = comm.chatterSources.length > 0
    ? Math.max(...comm.chatterSources.map(s => s.sharedChatters))
    : 1;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.4 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center gap-3 mb-6">
        <Users className="w-6 h-6 text-accent" />
        <h2 className="text-xl font-bold text-white">Cross-Community</h2>
      </div>

      <div className="grid grid-cols-3 gap-4 mb-6">
        <div className="text-center">
          <div className="text-2xl font-bold text-white">{comm.totalUniqueChatters}</div>
          <div className="text-xs text-text-secondary">Unique Chatters</div>
        </div>
        <div className="text-center">
          <div className="text-2xl font-bold text-accent">{comm.isolatedChatters}</div>
          <div className="text-xs text-text-secondary">Nur bei dir</div>
        </div>
        <div className="text-center">
          <div className={`text-2xl font-bold ${comm.isolatedPercentage > 40 ? 'text-success' : 'text-warning'}`}>
            {comm.isolatedPercentage}%
          </div>
          <div className="text-xs text-text-secondary">Exklusiv-Rate</div>
        </div>
      </div>

      <p className="text-sm text-text-secondary mb-4">{comm.ecosystemSummary}</p>

      <h3 className="text-sm font-medium text-text-secondary mb-3">Chatter-Quellen</h3>
      <div className="space-y-2">
        {comm.chatterSources.slice(0, 10).map((src, i) => (
          <div key={i} className="flex items-center gap-3">
            <span className="text-sm text-text-secondary w-28 truncate">{src.sourceStreamer}</span>
            <div className="flex-1 bg-background rounded-full h-4 overflow-hidden">
              <motion.div
                initial={{ width: 0 }}
                animate={{ width: `${(src.sharedChatters / maxShared) * 100}%` }}
                transition={{ duration: 0.6, delay: i * 0.05 }}
                className="h-full rounded-full bg-accent/60"
              />
            </div>
            <span className="text-xs text-text-secondary w-20 text-right">
              {src.sharedChatters} ({src.percentage}%)
            </span>
          </div>
        ))}
      </div>
    </motion.div>
  );
}


// ---------------------------------------------------------------------------
// 9. Double-Stream Warning
// ---------------------------------------------------------------------------

function DoubleStreamSection({ data }: { data: CoachingData }) {
  const ds = data.doubleStreamDetection;
  if (!ds || !ds.detected) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.45 }}
      className="bg-error/5 rounded-xl border border-error/20 p-6"
    >
      <div className="flex items-center gap-3 mb-4">
        <AlertTriangle className="w-6 h-6 text-error" />
        <h2 className="text-xl font-bold text-white">Doppel-Stream-Warnung</h2>
        <span className="text-xs px-2 py-0.5 rounded-full bg-error text-white font-medium">
          {ds.count}x erkannt
        </span>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-4">
        <div className="p-3 rounded-lg bg-background/50">
          <div className="text-lg font-bold text-white">{ds.singleDayAvg} Ø</div>
          <div className="text-xs text-text-secondary">Single-Stream-Tage</div>
        </div>
        <div className="p-3 rounded-lg bg-background/50">
          <div className={`text-lg font-bold ${ds.doubleDayAvg < ds.singleDayAvg ? 'text-error' : 'text-white'}`}>
            {ds.doubleDayAvg} Ø
          </div>
          <div className="text-xs text-text-secondary">Doppel-Stream-Tage</div>
        </div>
      </div>

      {ds.occurrences.length > 0 && (
        <div className="space-y-1">
          <h3 className="text-sm font-medium text-text-secondary mb-2">Betroffene Tage</h3>
          <div className="flex flex-wrap gap-2">
            {ds.occurrences.map((occ, i) => (
              <span key={i} className="px-2 py-1 rounded-md bg-error/10 text-error text-xs">
                {occ.date} ({occ.sessionCount} Sessions, {occ.avgViewers} Ø)
              </span>
            ))}
          </div>
        </div>
      )}
    </motion.div>
  );
}


// ---------------------------------------------------------------------------
// 10. Chat-Konzentration & Loyalty
// ---------------------------------------------------------------------------

const LOYALTY_LABELS: Record<string, { label: string; color: string }> = {
  oneTimer: { label: 'Einmalig', color: 'bg-error/60' },
  casual: { label: 'Gelegentlich', color: 'bg-warning/60' },
  regular: { label: 'Regulaer', color: 'bg-primary/60' },
  loyal: { label: 'Loyal', color: 'bg-success/60' },
};

function ChatConcentrationSection({ data }: { data: CoachingData }) {
  const chat = data.chatConcentration;
  if (!chat || chat.totalChatters === 0) return null;

  const bucketOrder = ['oneTimer', 'casual', 'regular', 'loyal'];
  const buckets = bucketOrder
    .filter(k => chat.loyaltyBuckets[k])
    .map(k => ({ key: k, ...chat.loyaltyBuckets[k], ...LOYALTY_LABELS[k] }));

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.5 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center gap-3 mb-6">
        <MessageCircle className="w-6 h-6 text-primary" />
        <h2 className="text-xl font-bold text-white">Chat-Konzentration</h2>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <div className="text-center p-3 rounded-lg bg-background/50">
          <div className={`text-2xl font-bold ${chat.top1Pct > 50 ? 'text-error' : chat.top1Pct > 30 ? 'text-warning' : 'text-success'}`}>
            {chat.top1Pct}%
          </div>
          <div className="text-xs text-text-secondary">Top-1 Chatter Anteil</div>
        </div>
        <div className="text-center p-3 rounded-lg bg-background/50">
          <div className={`text-2xl font-bold ${chat.top3Pct > 70 ? 'text-warning' : 'text-white'}`}>
            {chat.top3Pct}%
          </div>
          <div className="text-xs text-text-secondary">Top-3 kumulativ</div>
        </div>
        <div className="text-center p-3 rounded-lg bg-background/50">
          <div className="text-2xl font-bold text-white">{chat.msgsPerChatter}</div>
          <div className="text-xs text-text-secondary">Msgs / Chatter</div>
        </div>
        <div className="text-center p-3 rounded-lg bg-background/50">
          <div className={`text-2xl font-bold ${chat.concentrationIndex > 2500 ? 'text-error' : 'text-white'}`}>
            {chat.concentrationIndex.toLocaleString()}
          </div>
          <div className="text-xs text-text-secondary">HHI-Index</div>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Loyalty Buckets Stacked Bar */}
        <div>
          <h3 className="text-sm font-medium text-text-secondary mb-3">Chatter-Loyalitaet</h3>
          <div className="flex rounded-lg overflow-hidden h-8 mb-3">
            {buckets.map(b => (
              <div
                key={b.key}
                className={`${b.color} flex items-center justify-center text-xs text-white font-medium`}
                style={{ width: `${b.pct}%`, minWidth: b.pct > 5 ? undefined : '2px' }}
                title={`${b.label}: ${b.count} (${b.pct}%)`}
              >
                {b.pct >= 10 && `${b.pct}%`}
              </div>
            ))}
          </div>
          <div className="flex flex-wrap gap-3 text-xs">
            {buckets.map(b => (
              <div key={b.key} className="flex items-center gap-1.5">
                <div className={`w-3 h-3 rounded-sm ${b.color}`} />
                <span className="text-text-secondary">{b.label}: {b.count} ({b.pct}%)</span>
              </div>
            ))}
          </div>

          {/* One-Timer Comparison */}
          <div className="mt-4 p-3 rounded-lg bg-background/50">
            <div className="flex justify-between text-sm mb-1">
              <span className="text-text-secondary">Einmal-Chatter</span>
              <span className={chat.ownOneTimerPct > chat.avgPeerOneTimerPct + 10 ? 'text-warning font-medium' : 'text-white'}>
                {chat.ownOneTimerPct}%
              </span>
            </div>
            <div className="flex justify-between text-sm">
              <span className="text-text-secondary">Peer-Durchschnitt</span>
              <span className="text-accent">{chat.avgPeerOneTimerPct}%</span>
            </div>
          </div>
        </div>

        {/* Top Chatters */}
        <div>
          <h3 className="text-sm font-medium text-text-secondary mb-3">Top-Chatter (nach Nachrichten)</h3>
          <div className="space-y-1.5 max-h-64 overflow-y-auto">
            {chat.topChatters.slice(0, 10).map((c, i) => (
              <div key={i} className="flex items-center gap-2">
                <span className={`text-xs w-5 text-center ${i === 0 ? 'text-accent font-bold' : 'text-text-secondary'}`}>
                  {i + 1}
                </span>
                <span className="text-sm text-white flex-1 truncate">{c.login}</span>
                <div className="flex-1 bg-background rounded-full h-3 overflow-hidden max-w-[120px]">
                  <motion.div
                    initial={{ width: 0 }}
                    animate={{ width: `${c.sharePct}%` }}
                    transition={{ duration: 0.6, delay: i * 0.03 }}
                    className={`h-full rounded-full ${i === 0 ? 'bg-accent' : 'bg-primary/60'}`}
                  />
                </div>
                <span className="text-xs text-text-secondary w-16 text-right">
                  {c.messages} ({c.sharePct}%)
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </motion.div>
  );
}


// ---------------------------------------------------------------------------
// 11. Raid-Netzwerk
// ---------------------------------------------------------------------------

function RaidNetworkSection({ data }: { data: CoachingData }) {
  const raids = data.raidNetwork;
  if (!raids || raids.totalPartners === 0) return null;

  const maxActivity = Math.max(
    ...raids.partners.map(p => p.sentCount + p.receivedCount),
    1,
  );

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.55 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center gap-3 mb-6">
        <ArrowLeftRight className="w-6 h-6 text-accent" />
        <h2 className="text-xl font-bold text-white">Raid-Netzwerk</h2>
      </div>

      {/* KPIs */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        <div className="text-center p-3 rounded-lg bg-background/50">
          <div className="text-2xl font-bold text-white">{raids.totalSent}</div>
          <div className="text-xs text-text-secondary">Raids gesendet</div>
        </div>
        <div className="text-center p-3 rounded-lg bg-background/50">
          <div className="text-2xl font-bold text-white">{raids.totalReceived}</div>
          <div className="text-xs text-text-secondary">Raids erhalten</div>
        </div>
        <div className="text-center p-3 rounded-lg bg-background/50">
          <div className={`text-2xl font-bold ${raids.reciprocityRatio < 0.3 ? 'text-warning' : raids.reciprocityRatio > 0.7 ? 'text-success' : 'text-white'}`}>
            {raids.reciprocityRatio}x
          </div>
          <div className="text-xs text-text-secondary">Reziprozitaet</div>
        </div>
        <div className="text-center p-3 rounded-lg bg-background/50">
          <div className="text-2xl font-bold text-accent">{raids.mutualPartners}</div>
          <div className="text-xs text-text-secondary">Gegenseitige Partner</div>
        </div>
      </div>

      {/* Viewer Balance */}
      <div className="grid grid-cols-2 gap-4 mb-6">
        <div className="p-3 rounded-lg bg-background/50">
          <div className="text-sm text-text-secondary mb-1">Ø Viewer gesendet</div>
          <div className="text-lg font-bold text-white">{raids.avgSentViewers}</div>
          <div className="text-xs text-text-secondary">{raids.totalSentViewers.toLocaleString()} total</div>
        </div>
        <div className="p-3 rounded-lg bg-background/50">
          <div className="text-sm text-text-secondary mb-1">Ø Viewer erhalten</div>
          <div className="text-lg font-bold text-white">{raids.avgReceivedViewers}</div>
          <div className="text-xs text-text-secondary">{raids.totalReceivedViewers.toLocaleString()} total</div>
        </div>
      </div>

      {/* Partner List */}
      <h3 className="text-sm font-medium text-text-secondary mb-3">Raid-Partner</h3>
      <div className="space-y-2 max-h-72 overflow-y-auto">
        {raids.partners.slice(0, 15).map((p, i) => {
          const total = p.sentCount + p.receivedCount;
          const width = (total / maxActivity) * 100;
          const sentPct = total > 0 ? (p.sentCount / total) * 100 : 0;
          return (
            <div key={i} className="flex items-center gap-3">
              <span className="text-sm text-white w-28 truncate">{p.login}</span>
              <div className="flex-1 bg-background rounded-full h-5 overflow-hidden flex">
                <div
                  className="h-full bg-primary/60"
                  style={{ width: `${sentPct * width / 100}%` }}
                  title={`Gesendet: ${p.sentCount}`}
                />
                <div
                  className="h-full bg-accent/60"
                  style={{ width: `${(100 - sentPct) * width / 100}%` }}
                  title={`Erhalten: ${p.receivedCount}`}
                />
              </div>
              <span className={`text-xs w-20 text-right ${
                p.reciprocity === 'mutual' ? 'text-success' :
                p.reciprocity === 'sentOnly' ? 'text-warning' : 'text-accent'
              }`}>
                {p.sentCount}↑ {p.receivedCount}↓
              </span>
            </div>
          );
        })}
      </div>

      <div className="flex items-center gap-4 mt-4 text-xs text-text-secondary">
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 rounded-sm bg-primary/60" />
          <span>Gesendet</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 rounded-sm bg-accent/60" />
          <span>Erhalten</span>
        </div>
      </div>
    </motion.div>
  );
}


// ---------------------------------------------------------------------------
// 12. Peer-Vergleich
// ---------------------------------------------------------------------------

const METRIC_LABELS: Record<string, string> = {
  avgViewers: 'Ø Viewer',
  maxPeak: 'Peak',
  avgChatters: 'Ø Chatters',
  retention5m: '5m Retention',
  titleVariety: 'Titel-Vielfalt',
  sessions: 'Sessions',
};

function PeerComparisonSection({ data }: { data: CoachingData }) {
  const pc = data.peerComparison;
  if (!pc || !pc.ownData) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.6 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center gap-3 mb-6">
        <Trophy className="w-6 h-6 text-warning" />
        <h2 className="text-xl font-bold text-white">Peer-Vergleich</h2>
        <span className="text-xs text-text-secondary ml-auto">
          Rang {pc.ownRank} von {pc.totalStreamers}
        </span>
      </div>

      {/* Per-Metric Rankings */}
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mb-6">
        {Object.entries(pc.metricsRanked).map(([metric, ranking]) => (
          <div key={metric} className="p-3 rounded-lg bg-background/50">
            <div className="flex justify-between items-center mb-1">
              <span className="text-xs text-text-secondary">{METRIC_LABELS[metric] || metric}</span>
              <span className={`text-xs font-medium ${
                ranking.rank <= 3 ? 'text-success' :
                ranking.rank <= Math.ceil(ranking.total / 2) ? 'text-white' : 'text-warning'
              }`}>
                #{ranking.rank}/{ranking.total}
              </span>
            </div>
            <div className="flex items-end gap-2">
              <span className="text-lg font-bold text-white">
                {typeof ranking.value === 'number' ? (ranking.value % 1 === 0 ? ranking.value : ranking.value.toFixed(1)) : ranking.value}
              </span>
            </div>
            {/* Mini rank bar */}
            <div className="mt-2 bg-background rounded-full h-1.5 overflow-hidden">
              <div
                className={`h-full rounded-full ${
                  ranking.rank <= 3 ? 'bg-success' :
                  ranking.rank <= Math.ceil(ranking.total / 2) ? 'bg-primary' : 'bg-warning'
                }`}
                style={{ width: `${(1 - (ranking.rank - 1) / Math.max(ranking.total - 1, 1)) * 100}%` }}
              />
            </div>
          </div>
        ))}
      </div>

      {/* Gap to Next */}
      {pc.gapToNext && (
        <div className="p-4 rounded-lg bg-accent/5 border border-accent/20 mb-6">
          <h3 className="text-sm font-medium text-accent mb-2">Abstand zum Naechsten: {pc.gapToNext.login}</h3>
          <div className="grid grid-cols-3 gap-4 text-center">
            <div>
              <div className="text-lg font-bold text-white">+{pc.gapToNext.avgViewersDiff}</div>
              <div className="text-xs text-text-secondary">Ø Viewer</div>
            </div>
            <div>
              <div className="text-lg font-bold text-white">+{pc.gapToNext.chatDiff}</div>
              <div className="text-xs text-text-secondary">Ø Chatters</div>
            </div>
            <div>
              <div className="text-lg font-bold text-white">+{pc.gapToNext.retentionDiff}%</div>
              <div className="text-xs text-text-secondary">Retention</div>
            </div>
          </div>
        </div>
      )}

      {/* Similar + Aspirational Peers */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {pc.similarPeers.length > 0 && (
          <div>
            <h3 className="text-sm font-medium text-text-secondary mb-3">Aehnliche Streamer</h3>
            <div className="space-y-2">
              {pc.similarPeers.map((p, i) => (
                <div key={i} className="flex items-center gap-3 p-2 rounded-lg bg-background/50">
                  <span className="text-sm text-white flex-1 truncate">{p.login}</span>
                  <span className="text-xs text-accent">{p.avgViewers} Ø</span>
                  <span className="text-xs text-text-secondary">{p.sessions}s</span>
                  <span className="text-xs text-text-secondary">{p.retention5m}%</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {pc.aspirationalPeers.length > 0 && (
          <div>
            <h3 className="text-sm font-medium text-text-secondary mb-3">Vorbilder (naechste Stufe)</h3>
            <div className="space-y-2">
              {pc.aspirationalPeers.map((p, i) => (
                <div key={i} className="flex items-center gap-3 p-2 rounded-lg bg-background/50">
                  <span className="text-sm text-white flex-1 truncate">{p.login}</span>
                  <span className="text-xs text-success">{p.avgViewers} Ø</span>
                  <span className="text-xs text-text-secondary">{p.sessions}s</span>
                  <span className="text-xs text-text-secondary">{p.retention5m}%</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </motion.div>
  );
}


// ---------------------------------------------------------------------------
// 13. Konkurrenz-Dichte
// ---------------------------------------------------------------------------

function CompetitionDensitySection({ data }: { data: CoachingData }) {
  const comp = data.competitionDensity;
  if (!comp || !comp.hourly.length) return null;

  const maxStreamers = Math.max(...comp.hourly.map(h => h.activeStreamers), 1);

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.65 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center gap-3 mb-6">
        <Activity className="w-6 h-6 text-warning" />
        <h2 className="text-xl font-bold text-white">Konkurrenz-Dichte</h2>
      </div>

      {/* Sweet Spots */}
      {comp.sweetSpots.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-medium text-text-secondary mb-3">Beste Gelegenheiten (niedrige Konkurrenz, hohe Viewer)</h3>
          <div className="flex flex-wrap gap-2">
            {comp.sweetSpots.map((s, i) => (
              <div
                key={i}
                className={`px-3 py-1.5 rounded-lg text-sm ${
                  i < 2 ? 'bg-success/20 border border-success/30 text-success' : 'bg-background text-text-secondary'
                }`}
              >
                {s.hour.toString().padStart(2, '0')}:00 UTC
                <span className="ml-1 text-xs opacity-70">
                  ({s.activeStreamers} Str. / {s.avgViewers} Ø / Score {s.opportunityScore})
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Hourly Density Chart */}
      <h3 className="text-sm font-medium text-text-secondary mb-3">Streamer pro Stunde (UTC)</h3>
      <ResponsiveContainer width="100%" height={250}>
        <BarChart data={comp.hourly}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.1)" />
          <XAxis dataKey="hour" stroke="#888" fontSize={12} tickFormatter={(h) => `${h}:00`} />
          <YAxis stroke="#888" fontSize={12} />
          <Tooltip
            contentStyle={{ background: '#1a1a2e', border: '1px solid #333', borderRadius: '8px' }}
            labelFormatter={(h) => `${h}:00 UTC`}
            formatter={((value: number, name: string) => {
              if (name === 'activeStreamers') return [value, 'Aktive Streamer'];
              return [value, name];
            }) as any}
          />
          <Bar dataKey="activeStreamers" name="activeStreamers" radius={[4, 4, 0, 0]}>
            {comp.hourly.map((entry, idx) => (
              <Cell
                key={idx}
                fill={entry.yourData
                  ? 'rgba(124, 58, 237, 0.8)'
                  : `rgba(255, 255, 255, ${0.1 + (entry.activeStreamers / maxStreamers) * 0.4})`
                }
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>

      {/* Weekly Density */}
      {comp.weekly.length > 0 && (
        <div className="mt-6">
          <h3 className="text-sm font-medium text-text-secondary mb-3">Wochentag-Verteilung</h3>
          <div className="grid grid-cols-7 gap-2">
            {comp.weekly.map((w) => {
              const intensity = w.activeStreamers / Math.max(...comp.weekly.map(x => x.activeStreamers), 1);
              return (
                <div key={w.weekday} className="text-center">
                  <div className="text-xs text-text-secondary mb-1">{w.weekdayLabel}</div>
                  <div
                    className="rounded-lg p-2 border"
                    style={{
                      backgroundColor: `rgba(124, 58, 237, ${intensity * 0.4 + 0.05})`,
                      borderColor: w.yourData ? 'rgba(124, 58, 237, 0.6)' : 'transparent',
                    }}
                  >
                    <div className="text-sm font-bold text-white">{w.activeStreamers}</div>
                    <div className="text-xs text-text-secondary">{w.avgViewers} Ø</div>
                    {w.yourData && (
                      <div className="text-xs text-accent mt-1">{w.yourData.count}x</div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div className="flex items-center gap-4 mt-4 text-xs text-text-secondary">
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 rounded-sm" style={{ backgroundColor: 'rgba(124, 58, 237, 0.8)' }} />
          <span>Deine Stunden</span>
        </div>
        <div className="flex items-center gap-1">
          <div className="w-3 h-3 rounded-sm" style={{ backgroundColor: 'rgba(255, 255, 255, 0.3)' }} />
          <span>Ohne dich</span>
        </div>
      </div>
    </motion.div>
  );
}
