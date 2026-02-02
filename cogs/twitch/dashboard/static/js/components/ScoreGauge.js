/**
 * Score Gauge Component
 * Circular progress indicator for performance scores
 */

const ScoreGauge = ({ score, label, max = 100, color = '#7c3aed', size = 'md' }) => {
    const sizes = {
        sm: { container: 'w-24 h-24', text: 'text-2xl', label: 'text-xs' },
        md: { container: 'w-32 h-32', text: 'text-3xl', label: 'text-xs' },
        lg: { container: 'w-40 h-40', text: 'text-4xl', label: 'text-sm' }
    };
    
    const sizeClasses = sizes[size] || sizes.md;
    const circumference = 2 * Math.PI * 40;
    const offset = circumference - ((score / max) * circumference);
    
    // Determine color based on score
    const getScoreColor = () => {
        if (score >= 80) return '#10b981'; // green
        if (score >= 60) return '#3b82f6'; // blue
        if (score >= 40) return '#f59e0b'; // orange
        return '#ef4444'; // red
    };
    
    const finalColor = color === 'auto' ? getScoreColor() : color;
    
    return (
        <div className="flex flex-col items-center justify-center p-4 bg-card rounded-xl border border-white/5 relative overflow-hidden group hover:border-white/10 transition-all">
            <div className={`relative ${sizeClasses.container} flex items-center justify-center`}>
                <svg className="w-full h-full transform -rotate-90" viewBox="0 0 100 100">
                    {/* Background circle */}
                    <circle 
                        cx="50" 
                        cy="50" 
                        r="40" 
                        stroke="#1f2937" 
                        strokeWidth="8" 
                        fill="none" 
                    />
                    {/* Progress circle */}
                    <circle 
                        cx="50" 
                        cy="50" 
                        r="40" 
                        stroke={finalColor} 
                        strokeWidth="8" 
                        fill="none" 
                        strokeDasharray={circumference} 
                        strokeDashoffset={offset} 
                        strokeLinecap="round"
                        className="transition-all duration-1000 ease-out"
                        style={{ filter: 'drop-shadow(0 0 6px currentColor)' }}
                    />
                </svg>
                
                {/* Score text */}
                <div className="absolute inset-0 flex flex-col items-center justify-center">
                    <span className={`${sizeClasses.text} font-bold text-white`}>
                        {Math.round(score)}
                    </span>
                    {max !== 100 && (
                        <span className="text-xs text-gray-500">/{max}</span>
                    )}
                </div>
            </div>
            
            {/* Label */}
            <span className={`mt-3 text-gray-400 font-medium tracking-wide uppercase ${sizeClasses.label}`}>
                {label}
            </span>
            
            {/* Subtle glow effect on hover */}
            <div 
                className="absolute inset-0 opacity-0 group-hover:opacity-20 transition-opacity pointer-events-none"
                style={{ 
                    background: `radial-gradient(circle at center, ${finalColor} 0%, transparent 70%)` 
                }}
            />
        </div>
    );
};

// Export
if (typeof module !== 'undefined' && module.exports) {
    module.exports = ScoreGauge;
}
