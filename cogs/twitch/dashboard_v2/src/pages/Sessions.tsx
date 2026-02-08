import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Play, Clock, Users, TrendingUp, ChevronDown, ChevronUp, MessageCircle, AlertCircle, Loader2 } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { fetchOverview, fetchSessionDetail } from '@/api/client';
import type { DashboardOverview, StreamSession, TimeRange } from '@/types/analytics';

interface SessionsProps {
  streamer: string;
  days: TimeRange;
}

export function Sessions({ streamer, days }: SessionsProps) {
  const [expandedId, setExpandedId] = useState<number | null>(null);

  const { data, isLoading } = useQuery<DashboardOverview>({
    queryKey: ['overview', streamer, days],
    queryFn: () => fetchOverview(streamer, days),
    enabled: true,
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
      </div>
    );
  }

  if (!data || data.empty || !data.sessions || data.sessions.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-64">
        <AlertCircle className="w-12 h-12 text-text-secondary mb-4" />
        <p className="text-text-secondary text-lg">Keine Sessions gefunden</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Stats Summary */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          icon={<Play className="w-5 h-5" />}
          label="Total Streams"
          value={data.summary.streamCount.toString()}
          color="primary"
        />
        <StatCard
          icon={<Clock className="w-5 h-5" />}
          label="Total Airtime"
          value={`${data.summary.totalAirtime.toFixed(1)}h`}
          color="accent"
        />
        <StatCard
          icon={<Users className="w-5 h-5" />}
          label="Ø Viewer"
          value={Math.round(data.summary.avgViewers).toLocaleString('de-DE')}
          color="success"
        />
        <StatCard
          icon={<TrendingUp className="w-5 h-5" />}
          label="Peak"
          value={data.summary.peakViewers.toLocaleString('de-DE')}
          color="warning"
        />
      </div>

      {/* Session List */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        className="space-y-3"
      >
        {data.sessions.map((session, i) => (
          <SessionCard
            key={session.id}
            session={session}
            index={i}
            isExpanded={expandedId === session.id}
            onToggle={() => setExpandedId(expandedId === session.id ? null : session.id)}
          />
        ))}
      </motion.div>
    </div>
  );
}

interface StatCardProps {
  icon: React.ReactNode;
  label: string;
  value: string;
  color: 'primary' | 'accent' | 'success' | 'warning';
}

function StatCard({ icon, label, value, color }: StatCardProps) {
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
      className="bg-card rounded-xl border border-border p-4"
    >
      <div className={`w-10 h-10 rounded-lg ${colorClasses[color]} flex items-center justify-center mb-2`}>
        {icon}
      </div>
      <div className="text-xs text-text-secondary">{label}</div>
      <div className="text-xl font-bold text-white">{value}</div>
    </motion.div>
  );
}

interface SessionCardProps {
  session: StreamSession;
  index: number;
  isExpanded: boolean;
  onToggle: () => void;
}

function SessionCard({ session, index, isExpanded, onToggle }: SessionCardProps) {
  const durationHours = (session.duration / 3600).toFixed(1);
  const retentionColor = session.retention10m >= 60 ? 'text-success' : session.retention10m >= 40 ? 'text-warning' : 'text-error';

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.03 }}
      className="bg-card rounded-xl border border-border overflow-hidden"
    >
      {/* Session Header */}
      <div
        className="p-4 cursor-pointer hover:bg-background/50 transition-colors"
        onClick={onToggle}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div className="text-center">
              <div className="text-sm text-text-secondary">{session.date}</div>
              <div className="text-xs text-text-secondary">{session.startTime}</div>
            </div>

            <div className="h-10 w-px bg-border" />

            <div>
              <div className="font-medium text-white truncate max-w-md" title={session.title}>
                {session.title || 'Untitled Stream'}
              </div>
              <div className="flex items-center gap-4 text-sm text-text-secondary mt-1">
                <span className="flex items-center gap-1">
                  <Clock className="w-3 h-3" />
                  {durationHours}h
                </span>
                <span className="flex items-center gap-1">
                  <Users className="w-3 h-3" />
                  Ø {Math.round(session.avgViewers)}
                </span>
                <span className="flex items-center gap-1">
                  <TrendingUp className="w-3 h-3" />
                  Peak {session.peakViewers}
                </span>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-4">
            <div className="text-right">
              <div className={`text-lg font-bold ${retentionColor}`}>
                {session.retention10m.toFixed(0)}%
              </div>
              <div className="text-xs text-text-secondary">10m Retention</div>
            </div>
            {isExpanded ? (
              <ChevronUp className="w-5 h-5 text-text-secondary" />
            ) : (
              <ChevronDown className="w-5 h-5 text-text-secondary" />
            )}
          </div>
        </div>
      </div>

      {/* Expanded Details */}
      <AnimatePresence>
        {isExpanded && (
          <SessionDetails sessionId={session.id} session={session} />
        )}
      </AnimatePresence>
    </motion.div>
  );
}

interface SessionDetailsProps {
  sessionId: number;
  session: StreamSession;
}

function SessionDetails({ sessionId, session }: SessionDetailsProps) {
  const { data: detail, isLoading } = useQuery({
    queryKey: ['sessionDetail', sessionId],
    queryFn: () => fetchSessionDetail(sessionId),
  });

  return (
    <motion.div
      initial={{ height: 0, opacity: 0 }}
      animate={{ height: 'auto', opacity: 1 }}
      exit={{ height: 0, opacity: 0 }}
      transition={{ duration: 0.2 }}
      className="border-t border-border"
    >
      <div className="p-4 space-y-4">
        {/* Retention Breakdown */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <MetricBox label="5 Min" value={`${session.retention5m.toFixed(0)}%`} />
          <MetricBox label="10 Min" value={`${session.retention10m.toFixed(0)}%`} />
          <MetricBox label="20 Min" value={`${session.retention20m.toFixed(0)}%`} />
          <MetricBox label="Dropoff" value={`${session.dropoffPct.toFixed(0)}%`} isNegative />
        </div>

        {/* Viewer Stats */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <MetricBox label="Start Viewer" value={session.startViewers.toString()} />
          <MetricBox label="End Viewer" value={session.endViewers.toString()} />
          <MetricBox label="Unique Chatter" value={session.uniqueChatters.toString()} icon={<MessageCircle className="w-4 h-4" />} />
          <MetricBox
            label="Follower"
            value={`${session.followersEnd - session.followersStart >= 0 ? '+' : ''}${session.followersEnd - session.followersStart}`}
            isPositive={session.followersEnd >= session.followersStart}
          />
        </div>

        {/* Timeline Chart */}
        {isLoading ? (
          <div className="h-32 flex items-center justify-center">
            <Loader2 className="w-5 h-5 animate-spin text-primary" />
          </div>
        ) : detail?.timeline && detail.timeline.length > 0 ? (
          <div className="h-48">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={detail.timeline}>
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                <XAxis
                  dataKey="minute"
                  stroke="#9ca3af"
                  fontSize={11}
                  tickFormatter={(val) => `${val}m`}
                />
                <YAxis stroke="#9ca3af" fontSize={11} />
                <Tooltip
                  contentStyle={{
                    backgroundColor: '#1f2937',
                    border: '1px solid #374151',
                    borderRadius: '8px',
                  }}
                  labelFormatter={(val) => `Minute ${val}`}
                />
                <Line
                  type="monotone"
                  dataKey="viewers"
                  name="Viewer"
                  stroke="#7c3aed"
                  strokeWidth={2}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : null}

        {/* Top Chatters */}
        {detail?.chatters && detail.chatters.length > 0 && (
          <div>
            <div className="text-sm font-medium text-text-secondary mb-2">Top Chatter dieser Session</div>
            <div className="flex flex-wrap gap-2">
              {detail.chatters.slice(0, 10).map((c: { login: string; messages: number }) => (
                <span
                  key={c.login}
                  className="px-2 py-1 text-xs bg-background rounded-full text-text-secondary"
                >
                  {c.login} ({c.messages})
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </motion.div>
  );
}

interface MetricBoxProps {
  label: string;
  value: string;
  icon?: React.ReactNode;
  isPositive?: boolean;
  isNegative?: boolean;
}

function MetricBox({ label, value, icon, isPositive, isNegative }: MetricBoxProps) {
  let colorClass = 'text-white';
  if (isPositive !== undefined) {
    colorClass = isPositive ? 'text-success' : 'text-error';
  }
  if (isNegative) {
    colorClass = 'text-warning';
  }

  return (
    <div className="p-3 bg-background rounded-lg">
      <div className="text-xs text-text-secondary mb-1 flex items-center gap-1">
        {icon}
        {label}
      </div>
      <div className={`font-bold ${colorClass}`}>{value}</div>
    </div>
  );
}

export default Sessions;
