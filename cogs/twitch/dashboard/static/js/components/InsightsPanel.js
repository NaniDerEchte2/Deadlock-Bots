/**
 * Insights Panel Component
 * Displays AI-generated insights and recommendations
 */

const InsightsPanel = ({ insights }) => {
    if (!insights || insights.length === 0) return null;
    
    const getInsightStyle = (type) => {
        const styles = {
            success: {
                bg: 'bg-green-500/10',
                border: 'border-green-500/20 border-l-green-500',
                text: 'text-green-200',
                icon: '✓'
            },
            warning: {
                bg: 'bg-yellow-500/10',
                border: 'border-yellow-500/20 border-l-yellow-500',
                text: 'text-yellow-200',
                icon: '⚠'
            },
            error: {
                bg: 'bg-red-500/10',
                border: 'border-red-500/20 border-l-red-500',
                text: 'text-red-200',
                icon: '✕'
            },
            info: {
                bg: 'bg-blue-500/10',
                border: 'border-blue-500/20 border-l-blue-500',
                text: 'text-blue-200',
                icon: 'ℹ'
            }
        };
        
        return styles[type] || styles.info;
    };
    
    return (
        <div className="bg-card p-6 rounded-xl border border-white/5 shadow-lg mb-8">
            <div className="flex items-center gap-2 mb-4">
                <span className="text-2xl">💡</span>
                <h3 className="text-xl font-bold text-white">KI-gestützte Insights</h3>
            </div>
            
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {insights.map((insight, i) => {
                    const style = getInsightStyle(insight.type);
                    
                    return (
                        <div 
                            key={i} 
                            className={`p-4 rounded-lg border border-l-4 ${style.bg} ${style.border} shadow-sm hover:shadow-md transition-shadow`}
                        >
                            <div className="flex items-start gap-2 mb-2">
                                <span className="text-lg opacity-70">{style.icon}</span>
                                <h4 className={`text-sm font-bold ${style.text} flex-1`}>
                                    {insight.title}
                                </h4>
                            </div>
                            <p className="text-gray-300 text-sm leading-relaxed">
                                {insight.description}
                            </p>
                        </div>
                    );
                })}
            </div>
        </div>
    );
};

// Export
if (typeof module !== 'undefined' && module.exports) {
    module.exports = InsightsPanel;
}
