/**
 * View Mode Tabs Component
 * Navigation tabs for switching between different dashboard views
 */

const ViewModeTabs = ({ activeView, onViewChange, views }) => {
    const defaultViews = views || [
        { id: 'overview', label: 'Übersicht', icon: '📊' },
        { id: 'retention', label: 'Retention & Drop-Off', icon: '🎯' },
        { id: 'growth', label: 'Wachstum & Discovery', icon: '📈' },
        { id: 'chat', label: 'Chat-Gesundheit', icon: '💬' },
        { id: 'comparison', label: 'Vergleich', icon: '⚖️' },
        { id: 'detailed', label: 'Detaillierte Analyse', icon: '🔍' }
    ];
    
    return (
        <div className="bg-card rounded-xl shadow-sm p-2 mb-6 border border-white/5">
            <div className="flex flex-wrap gap-2">
                {defaultViews.map(view => (
                    <button
                        key={view.id}
                        onClick={() => onViewChange(view.id)}
                        className={`px-4 py-2.5 rounded-lg font-medium transition-all flex items-center gap-2 ${
                            activeView === view.id
                                ? 'bg-accent text-white shadow-lg shadow-accent/20'
                                : 'bg-transparent text-gray-400 hover:text-white hover:bg-white/5'
                        }`}
                    >
                        <span className="text-base">{view.icon}</span>
                        <span className="text-sm">{view.label}</span>
                    </button>
                ))}
            </div>
        </div>
    );
};

// Export
if (typeof module !== 'undefined' && module.exports) {
    module.exports = ViewModeTabs;
}
