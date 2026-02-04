import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Tag, TrendingUp, TrendingDown, Minus, Users, Clock, UserPlus, ChevronDown, ChevronUp, Trophy, Target } from 'lucide-react';
import type { TagPerformanceExtended, TitlePerformance } from '@/types/analytics';

interface TagPerformanceProps {
  tagData: TagPerformanceExtended[];
  titleData?: TitlePerformance[];
}

export function TagPerformanceChart({ tagData, titleData }: TagPerformanceProps) {
  const [activeTab, setActiveTab] = useState<'tags' | 'titles'>('tags');
  const [expandedTag, setExpandedTag] = useState<string | null>(null);

  // Sort by avgViewers
  const sortedTags = [...tagData].sort((a, b) => b.avgViewers - a.avgViewers);
  const sortedTitles = titleData ? [...titleData].sort((a, b) => b.avgViewers - a.avgViewers) : [];

  const maxViewers = Math.max(...sortedTags.map(t => t.avgViewers), 1);
  const maxTitleViewers = sortedTitles.length > 0 ? Math.max(...sortedTitles.map(t => t.avgViewers), 1) : 1;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-card rounded-xl border border-border p-6"
    >
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-accent/20 flex items-center justify-center">
            <Tag className="w-5 h-5 text-accent" />
          </div>
          <div>
            <h3 className="text-lg font-bold text-white">Tag & Titel Performance</h3>
            <p className="text-sm text-text-secondary">Welche Tags/Titel bringen die besten Ergebnisse?</p>
          </div>
        </div>
      </div>

      {/* Tab Switcher */}
      <div className="flex gap-2 mb-6">
        <button
          onClick={() => setActiveTab('tags')}
          className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
            activeTab === 'tags'
              ? 'bg-accent text-white'
              : 'bg-background text-text-secondary hover:text-white'
          }`}
        >
          <Tag className="w-4 h-4 inline mr-2" />
          Tags ({sortedTags.length})
        </button>
        {sortedTitles.length > 0 && (
          <button
            onClick={() => setActiveTab('titles')}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              activeTab === 'titles'
                ? 'bg-accent text-white'
                : 'bg-background text-text-secondary hover:text-white'
            }`}
          >
            <Target className="w-4 h-4 inline mr-2" />
            Titel ({sortedTitles.length})
          </button>
        )}
      </div>

      {/* Tags View */}
      <AnimatePresence mode="wait">
        {activeTab === 'tags' && (
          <motion.div
            key="tags"
            initial={{ opacity: 0, x: -20 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: 20 }}
            className="space-y-3"
          >
            {/* Top 3 Highlight */}
            {sortedTags.length >= 3 && (
              <div className="grid grid-cols-3 gap-3 mb-4">
                {sortedTags.slice(0, 3).map((tag, i) => (
                  <TopTagCard key={tag.tagName} tag={tag} rank={i + 1} />
                ))}
              </div>
            )}

            {/* Full List */}
            <div className="space-y-2 max-h-[400px] overflow-y-auto pr-2">
              {sortedTags.map((tag, i) => (
                <TagRow
                  key={tag.tagName}
                  tag={tag}
                  index={i}
                  maxViewers={maxViewers}
                  isExpanded={expandedTag === tag.tagName}
                  onToggle={() => setExpandedTag(expandedTag === tag.tagName ? null : tag.tagName)}
                />
              ))}
            </div>
          </motion.div>
        )}

        {activeTab === 'titles' && sortedTitles.length > 0 && (
          <motion.div
            key="titles"
            initial={{ opacity: 0, x: 20 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -20 }}
            className="space-y-2 max-h-[500px] overflow-y-auto pr-2"
          >
            {sortedTitles.map((title, i) => (
              <TitleRow
                key={title.title}
                title={title}
                index={i}
                maxViewers={maxTitleViewers}
              />
            ))}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Summary Insights */}
      {sortedTags.length > 0 && (
        <div className="mt-6 pt-4 border-t border-border">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <InsightBadge
              type="success"
              icon={<Trophy className="w-4 h-4" />}
              text={`"${sortedTags[0].tagName}" ist dein Top-Tag mit Ø ${sortedTags[0].avgViewers.toFixed(0)} Viewern`}
            />
            {sortedTags.some(t => t.trend === 'up' && t.trendValue > 10) && (
              <InsightBadge
                type="info"
                icon={<TrendingUp className="w-4 h-4" />}
                text={`"${sortedTags.find(t => t.trend === 'up' && t.trendValue > 10)?.tagName}" wächst stark (+${sortedTags.find(t => t.trend === 'up')?.trendValue.toFixed(0)}%)`}
              />
            )}
          </div>
        </div>
      )}
    </motion.div>
  );
}

interface TopTagCardProps {
  tag: TagPerformanceExtended;
  rank: number;
}

function TopTagCard({ tag, rank }: TopTagCardProps) {
  const rankStyles = {
    1: 'from-yellow-500/20 to-yellow-600/10 border-yellow-500/30',
    2: 'from-gray-400/20 to-gray-500/10 border-gray-400/30',
    3: 'from-amber-700/20 to-amber-800/10 border-amber-700/30',
  };

  const TrendIcon = tag.trend === 'up' ? TrendingUp : tag.trend === 'down' ? TrendingDown : Minus;
  const trendColor = tag.trend === 'up' ? 'text-success' : tag.trend === 'down' ? 'text-error' : 'text-text-secondary';

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: rank * 0.1 }}
      className={`bg-gradient-to-br ${rankStyles[rank as 1 | 2 | 3]} rounded-lg p-4 border`}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs text-text-secondary">#{rank}</span>
        <TrendIcon className={`w-4 h-4 ${trendColor}`} />
      </div>
      <div className="font-medium text-white truncate mb-2" title={tag.tagName}>
        {tag.tagName}
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div>
          <div className="text-text-secondary">Ø Viewer</div>
          <div className="font-medium text-white">{tag.avgViewers.toFixed(0)}</div>
        </div>
        <div>
          <div className="text-text-secondary">Retention</div>
          <div className="font-medium text-white">{tag.avgRetention10m.toFixed(0)}%</div>
        </div>
      </div>
    </motion.div>
  );
}

interface TagRowProps {
  tag: TagPerformanceExtended;
  index: number;
  maxViewers: number;
  isExpanded: boolean;
  onToggle: () => void;
}

function TagRow({ tag, index, maxViewers, isExpanded, onToggle }: TagRowProps) {
  const barWidth = (tag.avgViewers / maxViewers) * 100;
  const TrendIcon = tag.trend === 'up' ? TrendingUp : tag.trend === 'down' ? TrendingDown : Minus;
  const trendColor = tag.trend === 'up' ? 'text-success' : tag.trend === 'down' ? 'text-error' : 'text-text-secondary';

  return (
    <motion.div
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay: index * 0.03 }}
      className="bg-background rounded-lg overflow-hidden"
    >
      <div
        className="p-3 cursor-pointer hover:bg-white/5 transition-colors"
        onClick={onToggle}
      >
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-3">
            <span className="text-xs text-text-secondary w-6">#{index + 1}</span>
            <span className="font-medium text-white truncate max-w-[200px]" title={tag.tagName}>
              {tag.tagName}
            </span>
            <span className="text-xs text-text-secondary">({tag.usageCount}x)</span>
          </div>
          <div className="flex items-center gap-3">
            <span className={`flex items-center gap-1 text-xs ${trendColor}`}>
              <TrendIcon className="w-3 h-3" />
              {tag.trendValue > 0 ? '+' : ''}{tag.trendValue.toFixed(0)}%
            </span>
            {isExpanded ? (
              <ChevronUp className="w-4 h-4 text-text-secondary" />
            ) : (
              <ChevronDown className="w-4 h-4 text-text-secondary" />
            )}
          </div>
        </div>

        {/* Progress Bar */}
        <div className="h-2 bg-border rounded-full overflow-hidden">
          <motion.div
            initial={{ width: 0 }}
            animate={{ width: `${barWidth}%` }}
            transition={{ delay: 0.2, duration: 0.4 }}
            className="h-full bg-gradient-to-r from-accent to-primary rounded-full"
          />
        </div>

        {/* Quick Stats */}
        <div className="flex items-center gap-4 mt-2 text-xs text-text-secondary">
          <span className="flex items-center gap-1">
            <Users className="w-3 h-3" />
            Ø {tag.avgViewers.toFixed(0)}
          </span>
          <span className="flex items-center gap-1">
            <Target className="w-3 h-3" />
            {tag.avgRetention10m.toFixed(0)}% Ret.
          </span>
          <span className="flex items-center gap-1">
            <UserPlus className="w-3 h-3" />
            +{tag.avgFollowerGain.toFixed(1)} Fol.
          </span>
        </div>
      </div>

      {/* Expanded Details */}
      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            className="border-t border-border"
          >
            <div className="p-3 grid grid-cols-2 md:grid-cols-4 gap-3">
              <DetailBox label="Beste Zeit" value={tag.bestTimeSlot} icon={<Clock className="w-4 h-4" />} />
              <DetailBox label="Ø Stream-Dauer" value={`${(tag.avgStreamDuration / 60).toFixed(1)}h`} icon={<Clock className="w-4 h-4" />} />
              <DetailBox label="Kategorie-Rang" value={`#${tag.categoryRank}`} icon={<Trophy className="w-4 h-4" />} />
              <DetailBox label="Nutzungen" value={tag.usageCount.toString()} icon={<Tag className="w-4 h-4" />} />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

interface TitleRowProps {
  title: TitlePerformance;
  index: number;
  maxViewers: number;
}

function TitleRow({ title, index, maxViewers }: TitleRowProps) {
  const barWidth = (title.avgViewers / maxViewers) * 100;

  return (
    <motion.div
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay: index * 0.03 }}
      className="bg-background rounded-lg p-3"
    >
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-3 flex-1 min-w-0">
          <span className="text-xs text-text-secondary w-6 flex-shrink-0">#{index + 1}</span>
          <span className="font-medium text-white truncate" title={title.title}>
            {title.title}
          </span>
        </div>
        <span className="text-xs text-text-secondary flex-shrink-0 ml-2">({title.usageCount}x)</span>
      </div>

      {/* Progress Bar */}
      <div className="h-2 bg-border rounded-full overflow-hidden mb-2">
        <motion.div
          initial={{ width: 0 }}
          animate={{ width: `${barWidth}%` }}
          transition={{ delay: 0.2, duration: 0.4 }}
          className="h-full bg-gradient-to-r from-primary to-accent rounded-full"
        />
      </div>

      {/* Stats */}
      <div className="flex items-center gap-4 text-xs text-text-secondary">
        <span className="flex items-center gap-1">
          <Users className="w-3 h-3" />
          Ø {title.avgViewers.toFixed(0)}
        </span>
        <span className="flex items-center gap-1">
          <TrendingUp className="w-3 h-3" />
          Peak {title.peakViewers}
        </span>
        <span className="flex items-center gap-1">
          <Target className="w-3 h-3" />
          {title.avgRetention10m.toFixed(0)}%
        </span>
        <span className="flex items-center gap-1">
          <UserPlus className="w-3 h-3" />
          +{title.avgFollowerGain.toFixed(1)}
        </span>
      </div>

      {/* Keywords */}
      {title.keywords && title.keywords.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {title.keywords.slice(0, 5).map(keyword => (
            <span key={keyword} className="px-2 py-0.5 bg-card rounded text-xs text-text-secondary">
              {keyword}
            </span>
          ))}
        </div>
      )}
    </motion.div>
  );
}

interface DetailBoxProps {
  label: string;
  value: string;
  icon: React.ReactNode;
}

function DetailBox({ label, value, icon }: DetailBoxProps) {
  return (
    <div className="bg-card rounded-lg p-2 text-center">
      <div className="flex items-center justify-center gap-1 text-text-secondary text-xs mb-1">
        {icon}
        {label}
      </div>
      <div className="font-medium text-white text-sm">{value}</div>
    </div>
  );
}

interface InsightBadgeProps {
  type: 'success' | 'warning' | 'info';
  icon: React.ReactNode;
  text: string;
}

function InsightBadge({ type, icon, text }: InsightBadgeProps) {
  const styles = {
    success: 'bg-success/10 border-success/20 text-success',
    warning: 'bg-warning/10 border-warning/20 text-warning',
    info: 'bg-primary/10 border-primary/20 text-primary',
  };

  return (
    <div className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-xs ${styles[type]}`}>
      {icon}
      <span className="text-white">{text}</span>
    </div>
  );
}

export default TagPerformanceChart;
