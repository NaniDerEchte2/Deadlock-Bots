/**
 * Session Table Component
 * Displays recent stream sessions with key metrics
 */

const SessionTable = ({ sessions, limit = 10 }) => {
    if (!sessions || sessions.length === 0) {
        return (
            <div className="bg-card p-8 rounded-xl border border-white/5 text-center">
                <p className="text-gray-400">Keine Sessions gefunden</p>
            </div>
        );
    }
    
    const displaySessions = sessions.slice(0, limit);
    
    const formatDuration = (seconds) => {
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
    };
    
    const getRetentionColor = (retention) => {
        if (retention >= 70) return 'bg-green-500';
        if (retention >= 50) return 'bg-blue-500';
        if (retention >= 30) return 'bg-yellow-500';
        return 'bg-red-500';
    };
    
    return (
        <div className="bg-card rounded-xl border border-white/5 overflow-hidden shadow-lg">
            {/* Header */}
            <div className="p-5 border-b border-white/5 flex justify-between items-center">
                <h3 className="text-lg font-bold text-white">Letzte Sessions</h3>
                <div className="flex gap-2">
                    <button className="text-xs bg-white/5 hover:bg-white/10 px-3 py-1.5 rounded transition-colors">
                        Alle anzeigen
                    </button>
                </div>
            </div>
            
            {/* Table */}
            <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                    <thead className="bg-black/20 text-xs uppercase font-semibold text-gray-500 border-b border-white/5">
                        <tr>
                            <th className="px-5 py-3">Datum & Zeit</th>
                            <th className="px-5 py-3">Dauer</th>
                            <th className="px-5 py-3 text-right">Ø Viewer</th>
                            <th className="px-5 py-3 text-right">Peak</th>
                            <th className="px-5 py-3 text-right">Retention</th>
                            <th className="px-5 py-3 text-right">Follower</th>
                            <th className="px-5 py-3 text-right">Chat</th>
                            <th className="px-5 py-3"></th>
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-white/5 text-gray-400">
                        {displaySessions.map(session => (
                            <tr key={session.id} className="hover:bg-white/5 transition-colors">
                                <td className="px-5 py-3">
                                    <div className="flex flex-col">
                                        <span className="text-white font-medium">{session.date}</span>
                                        <span className="text-xs text-gray-600">{session.startTime}</span>
                                    </div>
                                </td>
                                <td className="px-5 py-3 font-mono text-xs">
                                    {formatDuration(session.duration)}
                                </td>
                                <td className="px-5 py-3 text-right font-medium">
                                    {session.avgViewers.toFixed(0)}
                                </td>
                                <td className="px-5 py-3 text-right">
                                    <span className="text-blue-400 font-semibold">{session.peakViewers}</span>
                                </td>
                                <td className="px-5 py-3 text-right">
                                    <div className="flex items-center justify-end gap-2">
                                        <div className="w-16 h-1.5 bg-gray-700 rounded-full overflow-hidden">
                                            <div 
                                                className={`h-full ${getRetentionColor(session.retention10m)} transition-all`}
                                                style={{ width: `${session.retention10m}%` }}
                                            />
                                        </div>
                                        <span className="text-xs w-10 text-right">{session.retention10m.toFixed(0)}%</span>
                                    </div>
                                </td>
                                <td className="px-5 py-3 text-right">
                                    <span className="text-green-400">+{session.followersEnd - session.followersStart}</span>
                                </td>
                                <td className="px-5 py-3 text-right">
                                    {session.uniqueChatters}
                                </td>
                                <td className="px-5 py-3 text-right">
                                    <a 
                                        href={`/twitch/session/${session.id}`} 
                                        className="text-accent hover:text-accent-hover font-semibold text-xs border border-accent/30 px-2 py-1 rounded hover:bg-accent/10 transition-colors inline-block"
                                    >
                                        Details
                                    </a>
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
            
            {/* Footer */}
            {sessions.length > limit && (
                <div className="p-4 border-t border-white/5 text-center">
                    <button className="text-sm text-accent hover:text-accent-hover font-medium">
                        Zeige {sessions.length - limit} weitere Sessions →
                    </button>
                </div>
            )}
        </div>
    );
};

// Export
if (typeof module !== 'undefined' && module.exports) {
    module.exports = SessionTable;
}
