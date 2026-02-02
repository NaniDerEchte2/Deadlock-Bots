/**
 * Module Loader for Analytics Dashboard
 * Loads all component scripts in the correct order
 */

(function() {
    'use strict';
    
    const BASE_PATH = '/twitch/static/js/components/';
    
    const components = [
        'KpiCard.js',
        'ScoreGauge.js',
        'ChartContainer.js',
        'InsightsPanel.js',
        'SessionTable.js',
        'ViewModeTabs.js',
        'ComparisonView.js'
    ];
    
    // Load scripts sequentially
    function loadScript(src) {
        return new Promise((resolve, reject) => {
            const script = document.createElement('script');
            script.src = src;
            script.onload = resolve;
            script.onerror = reject;
            document.head.appendChild(script);
        });
    }
    
    // Load all components
    async function loadComponents() {
        try {
            for (const component of components) {
                await loadScript(BASE_PATH + component);
            }
            console.log('✓ All analytics components loaded');
        } catch (error) {
            console.error('Failed to load component:', error);
        }
    }
    
    // Start loading when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', loadComponents);
    } else {
        loadComponents();
    }
})();
