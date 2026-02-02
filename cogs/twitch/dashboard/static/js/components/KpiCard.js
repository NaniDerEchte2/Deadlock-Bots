/**
 * KPI Card Component
 * Displays key performance indicators with optional trends
 */

const KpiCard = ({ title, value, subtitle, trend, icon: Icon, color = "blue" }) => {
    const colorMap = {
        blue: "bg-blue-500/10 text-blue-400 border-blue-500/20",
        green: "bg-green-500/10 text-green-400 border-green-500/20",
        purple: "bg-purple-500/10 text-purple-400 border-purple-500/20",
        orange: "bg-orange-500/10 text-orange-400 border-orange-500/20",
        red: "bg-red-500/10 text-red-400 border-red-500/20",
        yellow: "bg-yellow-500/10 text-yellow-400 border-yellow-500/20"
    };
    
    const iconBgClass = colorMap[color] || colorMap.blue;
    const trendColor = trend > 0 ? 'text-green-400' : trend < 0 ? 'text-red-400' : 'text-gray-400';
    const trendIcon = trend > 0 ? '▲' : trend < 0 ? '▼' : '–';
    
    return (
        <div className="bg-card p-5 rounded-xl border border-white/5 shadow-lg hover:shadow-xl hover:border-white/10 transition-all">
            <div className="flex justify-between items-start mb-3">
                <span className="text-gray-400 text-xs font-semibold uppercase tracking-wider">{title}</span>
                {Icon && (
                    <div className={`p-2 rounded-lg ${iconBgClass} border`}>
                        <Icon className="w-5 h-5" />
                    </div>
                )}
            </div>
            
            <div className="flex items-end gap-3 mb-2">
                <span className="text-3xl font-bold text-white leading-none">{value}</span>
                {trend !== undefined && trend !== null && (
                    <span className={`text-sm font-medium mb-1 ${trendColor} flex items-center gap-1`}>
                        <span>{trendIcon}</span>
                        <span>{Math.abs(trend).toFixed(1)}%</span>
                    </span>
                )}
            </div>
            
            {subtitle && (
                <div className="text-xs text-gray-500 font-medium mt-2">
                    {subtitle}
                </div>
            )}
        </div>
    );
};

// Export for module systems
if (typeof module !== 'undefined' && module.exports) {
    module.exports = KpiCard;
}
