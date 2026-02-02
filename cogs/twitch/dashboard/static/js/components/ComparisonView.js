/**
 * Comparison View Component
 * Shows streamer performance against category benchmarks
 */

const ComparisonView = ({ data, streamerLogin }) => {
    if (!data) return null;
    
    const { topStreamers = [], categoryAvg = {}, yourStats = {} } = data;
    
    // Calculate percentile ranking
    const calculatePercentile = (yourValue, allValues) => {
        const sorted = allValues.sort((a, b) => a - b);
        const index = sorted.findIndex(v => v >= yourValue);
        return ((index / sorted.length) * 100).toFixed(0);
    };
    
    return (
        <div className="space-y-6">
            {/* Header */}
            <div className="bg-gradient-to-r from-accent/20 to-blue-500/20 p-6 rounded-xl border border-accent/30">
                <h2 className="text-2xl font-bold text-white mb-2">Kategorie-Vergleich</h2>
                <p className="text-gray-300 text-sm">
                    Deine Performance im Vergleich zu anderen Deadlock-Streamern
                </p>
            </div>
            
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                {/* Top Streamer Rankings */}
                <div className="bg-card p-6 rounded-xl border border-white/5 shadow-lg">
                    <h3 className="text-lg font-bold text-white mb-4 flex items-center gap-2">
                        <span>🏆</span> Top Partner Streamer (Tracked)
                    </h3>
                    
                    <div className="space-y-2">
                        {topStreamers.slice(0, 10).map((streamer, index) => {
                            const isYou = streamer.login.toLowerCase() === streamerLogin?.toLowerCase();
                            
                            return (
                                <div 
                                    key={streamer.login}
                                    className={`flex items-center gap-3 p-3 rounded-lg transition-all ${
                                        isYou 
                                            ? 'bg-accent/20 border border-accent/40' 
                                            : 'bg-white/5 hover:bg-white/10'
                                    }`}
                                >
                                    {/* Rank */}
                                    <div className={`flex-shrink-0 w-8 h-8 rounded-full flex items-center justify-center font-bold text-sm ${
                                        index === 0 ? 'bg-yellow-500/20 text-yellow-400' :
                                        index === 1 ? 'bg-gray-400/20 text-gray-300' :
                                        index === 2 ? 'bg-orange-500/20 text-orange-400' :
                                        'bg-white/5 text-gray-400'
                                    }`}>
                                        #{index + 1}
                                    </div>
                                    
                                    {/* Name */}
                                    <div className="flex-1">
                                        <span className={`font-medium ${isYou ? 'text-white' : 'text-gray-300'}`}>
                                            {streamer.login}
                                            {isYou && <span className="ml-2 text-xs text-accent">(Du)</span>}
                                        </span>
                                    </div>
                                    
                                    {/* Stats */}
                                    <div className="flex gap-4 text-sm">
                                        <div className="text-right">
                                            <div className="text-gray-500 text-xs">Ø Viewer</div>
                                            <div className="text-white font-semibold">{streamer.avgViewers}</div>
                                        </div>
                                        <div className="text-right">
                                            <div className="text-gray-500 text-xs">Peak</div>
                                            <div className="text-blue-400 font-semibold">{streamer.peakViewers}</div>
                                        </div>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                </div>
                
                {/* Performance Comparison Bars */}
                <div className="bg-card p-6 rounded-xl border border-white/5 shadow-lg">
                    <h3 className="text-lg font-bold text-white mb-4 flex items-center gap-2">
                        <span>📊</span> Performance-Vergleich
                    </h3>
                    
                    <div className="space-y-4">
                        {[
                            { label: 'Ø Viewer', yours: yourStats.avgViewers || 0, category: categoryAvg.avgViewers || 0 },
                            { label: 'Peak Viewer', yours: yourStats.peakViewers || 0, category: categoryAvg.peakViewers || 0 },
                            { label: 'Retention 10m', yours: yourStats.retention10m || 0, category: categoryAvg.retention10m || 0, suffix: '%' },
                            { label: 'Chat Health', yours: yourStats.chatHealth || 0, category: categoryAvg.chatHealth || 0, suffix: '%' }
                        ].map(metric => {
                            const maxValue = Math.max(metric.yours, metric.category);
                            const yourPercent = (metric.yours / maxValue) * 100;
                            const categoryPercent = (metric.category / maxValue) * 100;
                            const isBetter = metric.yours > metric.category;
                            
                            return (
                                <div key={metric.label} className="space-y-2">
                                    <div className="flex justify-between text-sm">
                                        <span className="text-gray-400 font-medium">{metric.label}</span>
                                        <span className={`font-semibold ${isBetter ? 'text-green-400' : 'text-gray-400'}`}>
                                            {isBetter ? '▲' : '▼'} {Math.abs(((metric.yours - metric.category) / metric.category * 100)).toFixed(0)}%
                                        </span>
                                    </div>
                                    
                                    <div className="space-y-1">
                                        {/* Your bar */}
                                        <div className="flex items-center gap-2">
                                            <span className="text-xs text-white w-12">Du</span>
                                            <div className="flex-1 h-2 bg-gray-700 rounded-full overflow-hidden">
                                                <div 
                                                    className="h-full bg-accent transition-all duration-500"
                                                    style={{ width: `${yourPercent}%` }}
                                                />
                                            </div>
                                            <span className="text-xs text-white w-16 text-right font-mono">
                                                {metric.yours.toFixed(0)}{metric.suffix || ''}
                                            </span>
                                        </div>
                                        
                                        {/* Category bar */}
                                        <div className="flex items-center gap-2">
                                            <span className="text-xs text-gray-500 w-12">Kat.</span>
                                            <div className="flex-1 h-2 bg-gray-700 rounded-full overflow-hidden">
                                                <div 
                                                    className="h-full bg-gray-400 transition-all duration-500"
                                                    style={{ width: `${categoryPercent}%` }}
                                                />
                                            </div>
                                            <span className="text-xs text-gray-400 w-16 text-right font-mono">
                                                {metric.category.toFixed(0)}{metric.suffix || ''}
                                            </span>
                                        </div>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                </div>
            </div>
            
            {/* Strengths & Weaknesses */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div className="bg-green-500/10 border border-green-500/20 p-6 rounded-xl">
                    <h3 className="text-lg font-bold text-green-200 mb-3 flex items-center gap-2">
                        <span>💪</span> Deine Stärken
                    </h3>
                    <ul className="space-y-2 text-sm text-gray-300">
                        {yourStats.retention10m > categoryAvg.retention10m && (
                            <li className="flex items-start gap-2">
                                <span className="text-green-400">✓</span>
                                <span>Überdurchschnittliche Retention - Dein Content bindet Zuschauer</span>
                            </li>
                        )}
                        {yourStats.chatHealth > categoryAvg.chatHealth && (
                            <li className="flex items-start gap-2">
                                <span className="text-green-400">✓</span>
                                <span>Starke Chat-Interaktion - Aktive Community</span>
                            </li>
                        )}
                        {yourStats.avgViewers > categoryAvg.avgViewers && (
                            <li className="flex items-start gap-2">
                                <span className="text-green-400">✓</span>
                                <span>Überdurchschnittliche Viewerzahlen</span>
                            </li>
                        )}
                    </ul>
                </div>
                
                <div className="bg-orange-500/10 border border-orange-500/20 p-6 rounded-xl">
                    <h3 className="text-lg font-bold text-orange-200 mb-3 flex items-center gap-2">
                        <span>🎯</span> Verbesserungspotenzial
                    </h3>
                    <ul className="space-y-2 text-sm text-gray-300">
                        {yourStats.retention10m <= categoryAvg.retention10m && (
                            <li className="flex items-start gap-2">
                                <span className="text-orange-400">→</span>
                                <span>Arbeite an deiner Retention - Verbessere Stream-Hooks</span>
                            </li>
                        )}
                        {yourStats.chatHealth <= categoryAvg.chatHealth && (
                            <li className="flex items-start gap-2">
                                <span className="text-orange-400">→</span>
                                <span>Steigere Chat-Interaktion - Stelle mehr Fragen</span>
                            </li>
                        )}
                        {yourStats.avgViewers <= categoryAvg.avgViewers && (
                            <li className="flex items-start gap-2">
                                <span className="text-orange-400">→</span>
                                <span>Wachstumspotenzial - Optimiere Stream-Zeiten und Titel</span>
                            </li>
                        )}
                    </ul>
                </div>
            </div>
        </div>
    );
};

// Export
if (typeof module !== 'undefined' && module.exports) {
    module.exports = ComparisonView;
}
