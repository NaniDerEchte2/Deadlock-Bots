/**
 * Chart Container Component
 * Wrapper for chart canvases with consistent styling
 */

const ChartContainer = ({ title, subtitle, children, actions, className = '' }) => {
    return (
        <div className={`bg-card rounded-xl border border-white/5 overflow-hidden shadow-lg ${className}`}>
            {/* Header */}
            {(title || actions) && (
                <div className="p-5 border-b border-white/5 flex justify-between items-center">
                    <div>
                        <h3 className="text-lg font-bold text-white">{title}</h3>
                        {subtitle && (
                            <p className="text-xs text-gray-500 mt-1">{subtitle}</p>
                        )}
                    </div>
                    {actions && (
                        <div className="flex gap-2">
                            {actions}
                        </div>
                    )}
                </div>
            )}
            
            {/* Chart content */}
            <div className="p-5">
                {children}
            </div>
        </div>
    );
};

// Export
if (typeof module !== 'undefined' && module.exports) {
    module.exports = ChartContainer;
}
