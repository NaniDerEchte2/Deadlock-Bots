import { motion } from 'framer-motion';
import { MessageCircle, Users, Heart, TrendingUp, AlertCircle, Loader2, Award } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { fetchChatAnalytics } from '@/api/client';
import type { ChatAnalytics as ChatAnalyticsType } from '@/types/analytics';

import type { TimeRange } from '@/types/analytics';

interface ChatAnalyticsProps {
  streamer: string;
  days: TimeRange;
}

export function ChatAnalytics({ streamer, days }: ChatAnalyticsProps) {
  const { data, isLoading } = useQuery<ChatAnalyticsType>({
    queryKey: ['chatAnalytics', streamer, days],
    queryFn: () => fetchChatAnalytics(streamer, days),
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

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex flex-col items-center justify-center h-64">
        <MessageCircle className="w-12 h-12 text-text-secondary mb-4" />
        <p className="text-text-secondary text-lg">Keine Chat-Daten verfügbar</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Overview Stats */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          icon={<Users className="w-5 h-5" />}
          label="Unique Chatter"
          value={data.uniqueChatters.toLocaleString('de-DE')}
          color="primary"
        />
        <StatCard
          icon={<TrendingUp className="w-5 h-5" />}
          label="Erstmalige Chatter"
          value={data.firstTimeChatters.toLocaleString('de-DE')}
          subtext={data.uniqueChatters > 0 ? `${((data.firstTimeChatters / data.uniqueChatters) * 100).toFixed(1)}%` : '-'}
          color="accent"
        />
        <StatCard
          icon={<Heart className="w-5 h-5" />}
          label="Wiederkehrende Chatter"
          value={data.returningChatters.toLocaleString('de-DE')}
          subtext={`${data.chatterReturnRate.toFixed(1)}% Return Rate`}
          color="success"
        />
        <StatCard
          icon={<MessageCircle className="w-5 h-5" />}
          label="Chat-Aktivität"
          value={data.messagesPerMinute > 0 ? `${data.messagesPerMinute.toFixed(1)}/min` : '-'}
          color="warning"
        />
      </div>

      {/* Chatter Loyalty Distribution */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="bg-card rounded-xl border border-border p-6"
      >
        <div className="flex items-center gap-3 mb-6">
          <Heart className="w-6 h-6 text-primary" />
          <h2 className="text-xl font-bold text-white">Community-Treue</h2>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <LoyaltyGauge
            label="Neue Zuschauer"
            percentage={(data.firstTimeChatters / Math.max(1, data.uniqueChatters)) * 100}
            description="Chatten zum ersten Mal"
            color="from-accent to-primary"
          />
          <LoyaltyGauge
            label="Stammzuschauer"
            percentage={data.chatterReturnRate}
            description="Kommen regelmäßig zurück"
            color="from-success to-accent"
          />
          <LoyaltyGauge
            label="Interaktionsrate"
            percentage={Math.min(100, data.uniqueChatters / 10)}
            description="Chatter pro 100 Viewer (geschätzt)"
            color="from-primary to-success"
          />
        </div>
      </motion.div>

      {/* Message Analysis */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Message Types */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1 }}
          className="bg-card rounded-xl border border-border p-6"
        >
          <div className="flex items-center gap-3 mb-6">
            <MessageCircle className="w-6 h-6 text-accent" />
            <h2 className="text-xl font-bold text-white">Nachrichtentypen</h2>
          </div>
          <div className="space-y-4">
            {data.messageTypes?.map((type) => (
              <div key={type.type}>
                <div className="flex justify-between text-sm mb-1">
                  <span className="text-text-secondary">{type.type}</span>
                  <span className="text-white font-medium">{type.percentage}% ({type.count})</span>
                </div>
                <div className="h-2 bg-background rounded-full overflow-hidden">
                  <motion.div
                    initial={{ width: 0 }}
                    animate={{ width: `${type.percentage}%` }}
                    transition={{ duration: 1, ease: "easeOut" }}
                    className="h-full bg-accent"
                  />
                </div>
              </div>
            ))}
            {(!data.messageTypes || data.messageTypes.length === 0) && (
               <p className="text-text-secondary text-center py-4">Keine Daten verfügbar</p>
            )}
          </div>
        </motion.div>

        {/* Hourly Activity */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.15 }}
          className="bg-card rounded-xl border border-border p-6"
        >
          <div className="flex items-center gap-3 mb-6">
            <TrendingUp className="w-6 h-6 text-success" />
            <h2 className="text-xl font-bold text-white">Aktivität nach Tageszeit</h2>
          </div>
          <div className="h-64 flex items-end gap-1">
            {Array.from({ length: 24 }).map((_, hour) => {
              const stat = data.hourlyActivity?.find(h => h.hour === hour);
              const count = stat?.count || 0;
              const maxCount = Math.max(...(data.hourlyActivity?.map(h => h.count) || [1]));
              const height = maxCount > 0 ? (count / maxCount) * 100 : 0;
              
              return (
                <div key={hour} className="flex-1 flex flex-col items-center group relative">
                  <motion.div
                    initial={{ height: 0 }}
                    animate={{ height: `${height}%` }}
                    transition={{ duration: 0.5, delay: hour * 0.02 }}
                    className={`w-full bg-success/50 rounded-t-sm group-hover:bg-success transition-colors ${height === 0 ? 'min-h-[2px] bg-border' : ''}`}
                  />
                  {/* Tooltip */}
                  <div className="absolute bottom-full mb-2 hidden group-hover:block bg-popover text-white text-xs p-2 rounded z-10 whitespace-nowrap border border-border">
                    {hour}:00 Uhr: {count} Nachrichten
                  </div>
                  {hour % 4 === 0 && (
                    <div className="text-[10px] text-text-secondary mt-1">{hour}h</div>
                  )}
                </div>
              );
            })}
          </div>
        </motion.div>
      </div>

      {/* Top Chatters Leaderboard */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2 }}
        className="bg-card rounded-xl border border-border p-6"
      >
        <div className="flex items-center gap-3 mb-6">
          <Award className="w-6 h-6 text-warning" />
          <h2 className="text-xl font-bold text-white">Top Chatter</h2>
        </div>

        {data.topChatters && data.topChatters.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {data.topChatters.slice(0, 12).map((chatter, i) => (
              <ChatterCard
                key={chatter.login}
                rank={i + 1}
                login={chatter.login}
                messages={chatter.totalMessages}
                sessions={chatter.totalSessions}
                loyaltyScore={chatter.loyaltyScore}
              />
            ))}
          </div>
        ) : (
          <div className="text-center py-8 text-text-secondary">
            <Users className="w-12 h-12 mx-auto mb-3 opacity-50" />
            <p>Keine Chatter-Daten vorhanden</p>
          </div>
        )}
      </motion.div>

      {/* Insights */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.3 }}
        className="bg-gradient-to-r from-primary/10 to-accent/10 rounded-xl border border-primary/20 p-6"
      >
        <h3 className="font-bold text-white mb-4">Chat-Insights</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <InsightItem
            type={data.chatterReturnRate > 30 ? 'positive' : 'warning'}
            text={data.chatterReturnRate > 30
              ? `Starke Community! ${data.chatterReturnRate.toFixed(0)}% deiner Chatter kommen wieder.`
              : `${data.chatterReturnRate.toFixed(0)}% Return Rate - versuche mehr Interaktion!`}
          />
          <InsightItem
            type={data.firstTimeChatters > data.returningChatters ? 'info' : 'positive'}
            text={data.firstTimeChatters > data.returningChatters
              ? 'Viele neue Gesichter! Binde sie durch direkte Ansprache.'
              : 'Deine Stammzuschauer sind loyal - belohne sie!'
            }
          />
        </div>
      </motion.div>
    </div>
  );
}

interface StatCardProps {
  icon: React.ReactNode;
  label: string;
  value: string;
  subtext?: string;
  color: 'primary' | 'accent' | 'success' | 'warning';
}

function StatCard({ icon, label, value, subtext, color }: StatCardProps) {
  const colorClasses = {
    primary: 'bg-primary/10 text-primary',
    accent: 'bg-accent/10 text-accent',
    success: 'bg-success/10 text-success',
    warning: 'bg-warning/10 text-warning',
  };

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      className="bg-card rounded-xl border border-border p-5"
    >
      <div className={`w-10 h-10 rounded-lg ${colorClasses[color]} flex items-center justify-center mb-3`}>
        {icon}
      </div>
      <div className="text-sm text-text-secondary mb-1">{label}</div>
      <div className="text-2xl font-bold text-white">{value}</div>
      {subtext && <div className="text-sm text-text-secondary mt-1">{subtext}</div>}
    </motion.div>
  );
}

interface LoyaltyGaugeProps {
  label: string;
  percentage: number;
  description: string;
  color: string;
}

function LoyaltyGauge({ label, percentage, description, color }: LoyaltyGaugeProps) {
  const clampedPercentage = Math.min(100, Math.max(0, percentage));

  return (
    <div className="text-center">
      <div className="relative w-32 h-32 mx-auto mb-3">
        <svg className="w-full h-full -rotate-90">
          <circle
            cx="64"
            cy="64"
            r="56"
            fill="none"
            stroke="currentColor"
            strokeWidth="12"
            className="text-border"
          />
          <motion.circle
            cx="64"
            cy="64"
            r="56"
            fill="none"
            stroke="url(#gaugeGradient)"
            strokeWidth="12"
            strokeLinecap="round"
            strokeDasharray={`${(clampedPercentage / 100) * 352} 352`}
            initial={{ strokeDasharray: '0 352' }}
            animate={{ strokeDasharray: `${(clampedPercentage / 100) * 352} 352` }}
            transition={{ duration: 1, delay: 0.3 }}
          />
          <defs>
            <linearGradient id="gaugeGradient" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%" className={`text-${color.split(' ')[0].replace('from-', '')}`} stopColor="currentColor" />
              <stop offset="100%" className={`text-${color.split(' ')[1]?.replace('to-', '') || 'primary'}`} stopColor="currentColor" />
            </linearGradient>
          </defs>
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          <span className="text-2xl font-bold text-white">{clampedPercentage.toFixed(0)}%</span>
        </div>
      </div>
      <div className="font-medium text-white">{label}</div>
      <div className="text-sm text-text-secondary">{description}</div>
    </div>
  );
}

interface ChatterCardProps {
  rank: number;
  login: string;
  messages: number;
  sessions: number;
  loyaltyScore: number;
}

function ChatterCard({ rank, login, messages, sessions, loyaltyScore }: ChatterCardProps) {
  const getRankStyle = () => {
    if (rank === 1) return 'bg-gradient-to-br from-yellow-400 to-yellow-600 text-black';
    if (rank === 2) return 'bg-gradient-to-br from-gray-300 to-gray-500 text-black';
    if (rank === 3) return 'bg-gradient-to-br from-amber-600 to-amber-800 text-white';
    return 'bg-border text-text-secondary';
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: rank * 0.03 }}
      className="flex items-center gap-3 p-3 bg-background rounded-lg"
    >
      <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold ${getRankStyle()}`}>
        {rank}
      </div>
      <div className="flex-1 min-w-0">
        <div className="font-medium text-white truncate">{login}</div>
        <div className="text-xs text-text-secondary">
          {messages.toLocaleString('de-DE')} Nachrichten • {sessions} Sessions
        </div>
      </div>
      <div className="text-right">
        <div className="text-sm font-medium text-primary">{loyaltyScore}</div>
        <div className="text-xs text-text-secondary">Loyalität</div>
      </div>
    </motion.div>
  );
}

interface InsightItemProps {
  type: 'positive' | 'warning' | 'info';
  text: string;
}

function InsightItem({ type, text }: InsightItemProps) {
  const styles = {
    positive: 'bg-success/10 border-success/20 text-success',
    warning: 'bg-warning/10 border-warning/20 text-warning',
    info: 'bg-primary/10 border-primary/20 text-primary',
  };

  return (
    <div className={`p-3 rounded-lg border ${styles[type]}`}>
      <p className="text-sm text-white">{text}</p>
    </div>
  );
}

export default ChatAnalytics;
