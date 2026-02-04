import { motion } from 'framer-motion';
import { ExternalLink } from 'lucide-react';
import type { StreamSession } from '@/types/analytics';
import { formatNumber, formatDurationShort, formatDate, formatTime, getRetentionColor } from '@/utils/formatters';

interface SessionTableProps {
  sessions: StreamSession[];
  limit?: number;
  showViewAll?: boolean;
  onViewAll?: () => void;
  onSessionClick?: (sessionId: number) => void;
}

export function SessionTable({
  sessions,
  limit = 10,
  showViewAll = true,
  onViewAll,
  onSessionClick,
}: SessionTableProps) {
  const displaySessions = limit ? sessions.slice(0, limit) : sessions;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className="bg-card rounded-xl border border-border overflow-hidden"
    >
      {/* Header */}
      <div className="p-5 border-b border-border flex justify-between items-center">
        <h3 className="text-lg font-bold text-white">Recent Sessions</h3>
        {showViewAll && onViewAll && (
          <button
            onClick={onViewAll}
            className="text-xs bg-white/5 hover:bg-white/10 px-3 py-1 rounded transition text-text-secondary hover:text-white"
          >
            View All
          </button>
        )}
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm text-text-secondary">
          <thead className="bg-black/20 text-xs uppercase font-semibold text-text-secondary">
            <tr>
              <th className="px-5 py-3">Datum</th>
              <th className="px-5 py-3">Dauer</th>
              <th className="px-5 py-3">Avg</th>
              <th className="px-5 py-3">Peak</th>
              <th className="px-5 py-3">Retention</th>
              <th className="px-5 py-3">Chat</th>
              <th className="px-5 py-3"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {displaySessions.map((session, index) => (
              <motion.tr
                key={session.id}
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: index * 0.05 }}
                className="hover:bg-white/5 transition cursor-pointer"
                onClick={() => onSessionClick?.(session.id)}
              >
                <td className="px-5 py-3">
                  <span className="text-white font-medium">{formatDate(session.date)}</span>
                  <span className="text-text-secondary text-xs ml-2">{formatTime(session.startTime)}</span>
                </td>
                <td className="px-5 py-3">{formatDurationShort(session.duration)}</td>
                <td className="px-5 py-3 text-white">{formatNumber(session.avgViewers)}</td>
                <td className="px-5 py-3">{formatNumber(session.peakViewers)}</td>
                <td className="px-5 py-3">
                  <div className="flex items-center gap-2">
                    <div className="w-16 h-1.5 bg-gray-700 rounded-full overflow-hidden">
                      <div
                        className="h-full rounded-full transition-all"
                        style={{
                          width: `${Math.min(session.retention10m, 100)}%`,
                          backgroundColor: getRetentionColor(session.retention10m),
                        }}
                      />
                    </div>
                    <span className="text-xs">{session.retention10m.toFixed(0)}%</span>
                  </div>
                </td>
                <td className="px-5 py-3">
                  <span className="text-white">{session.uniqueChatters}</span>
                  <span className="text-text-secondary text-xs ml-1">
                    ({session.firstTimeChatters} neu)
                  </span>
                </td>
                <td className="px-5 py-3">
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onSessionClick?.(session.id);
                    }}
                    className="text-accent hover:text-accent-hover font-semibold text-xs border border-accent/30 px-2 py-1 rounded flex items-center gap-1"
                  >
                    <ExternalLink className="w-3 h-3" />
                    Details
                  </button>
                </td>
              </motion.tr>
            ))}
          </tbody>
        </table>

        {sessions.length === 0 && (
          <div className="p-8 text-center text-text-secondary">
            Keine Sessions im gewählten Zeitraum
          </div>
        )}
      </div>
    </motion.div>
  );
}
