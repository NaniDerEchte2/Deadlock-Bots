/**
 * Main Analytics Dashboard Application
 * Modular, modern Twitch streamer analytics
 */

const { useState, useEffect, useMemo, useRef } = React;

/**
 * Icons - Lucide-style replacements
 */
const Icons = {
    Eye: () => <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" /><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" /></svg>,
    UserPlus: () => <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M18 9v3m0 0v3m0-3h3m-3 0h-3m-2-5a4 4 0 11-8 0 4 4 0 018 0zM3 20a6 6 0 0112 0v1H3v-1z" /></svg>,
    Clock: () => <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>,
    MessageCircle: () => <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" /></svg>,
    Target: () => <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>,
    TrendingUp: () => <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" /></svg>,
    TrendingDown: () => <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 17h8m0 0V9m0 8l-8-8-4 4-6-6" /></svg>,
    Activity: () => <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" /></svg>,
    Zap: () => <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>,
    Users: () => <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z" /></svg>
};

/**
 * Helper Functions
 */
const formatNumber = (num, decimals = 0) => {
    if (num === undefined || num === null) return '-';
    return num.toLocaleString('de-DE', { 
        minimumFractionDigits: decimals, 
        maximumFractionDigits: decimals 
    });
};

const formatPercent = (num, decimals = 1) => {
    if (num === undefined || num === null) return '-';
    return `${num.toFixed(decimals)}%`;
};

/**
 * Loading Spinner Component
 */
const LoadingSpinner = () => (
    <div className="flex items-center justify-center min-h-screen">
        <div className="flex flex-col items-center gap-4">
            <div className="w-12 h-12 border-4 border-accent border-t-transparent rounded-full animate-spin"></div>
            <p className="text-gray-400 animate-pulse">Lade Analytics-Daten...</p>
        </div>
    </div>
);

/**
 * Error Display Component
 */
const ErrorDisplay = ({ error, onRetry }) => (
    <div className="flex items-center justify-center min-h-screen p-8">
        <div className="bg-red-500/10 border border-red-500/20 rounded-xl p-8 max-w-md text-center">
            <div className="text-5xl mb-4">❌</div>
            <h2 className="text-xl font-bold text-red-200 mb-2">Fehler beim Laden</h2>
            <p className="text-gray-300 mb-4">{error}</p>
            <button 
                onClick={onRetry}
                className="px-6 py-2 bg-red-500/20 hover:bg-red-500/30 text-red-200 rounded-lg transition-colors font-medium"
            >
                Erneut versuchen
            </button>
        </div>
    </div>
);

/**
 * Main Dashboard Application
 */
const AnalyticsDashboard = () => {
    // State
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [data, setData] = useState(null);
    const [viewMode, setViewMode] = useState('overview');
    const [days, setDays] = useState(30);
    const [streamer, setStreamer] = useState('');
    
    // Refs for charts
    const chartRefs = useRef({});
    
    /**
     * Load initial config from injected JSON
     */
    useEffect(() => {
        const configEl = document.getElementById('analytics-config');
        if (configEl) {
            try {
                const config = JSON.parse(configEl.textContent);
                if (config.streamer) setStreamer(config.streamer);
                if (config.days) setDays(config.days);
            } catch (e) {
                console.error('Failed to parse config:', e);
            }
        }
    }, []);
    
    /**
     * Fetch analytics data from API
     */
    useEffect(() => {
        const fetchData = async () => {
            setLoading(true);
            setError(null);
            
            try {
                const params = new URLSearchParams({ days: days.toString() });
                if (streamer) params.set('streamer', streamer);
                
                // Preserve partner token from URL
                const urlParams = new URLSearchParams(window.location.search);
                if (urlParams.has('partner_token')) {
                    params.set('partner_token', urlParams.get('partner_token'));
                }
                
                const response = await fetch(`/twitch/api/analytics?${params}`);
                
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                }
                
                const json = await response.json();
                
                if (json.error) {
                    throw new Error(json.error);
                }
                
                if (json.empty) {
                    throw new Error('Keine Daten für den gewählten Zeitraum vorhanden');
                }
                
                setData(json);
            } catch (err) {
                console.error('Data fetch error:', err);
                setError(err.message);
            } finally {
                setLoading(false);
            }
        };
        
        fetchData();
    }, [days, streamer]);
    
    /**
     * Setup charts when data changes
     */
    useEffect(() => {
        if (!data || !data.retention_timeline) return;
        
        // Destroy existing charts
        Object.values(chartRefs.current).forEach(chart => {
            if (chart) chart.destroy();
        });
        chartRefs.current = {};
        
        // Chart defaults
        const chartDefaults = {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    labels: { color: '#9ca3af', font: { family: 'Outfit' } }
                },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    backgroundColor: '#111827',
                    titleColor: '#fff',
                    bodyColor: '#d1d5db',
                    borderColor: '#374151',
                    borderWidth: 1
                }
            },
            scales: {
                x: {
                    grid: { color: '#374151', drawBorder: false },
                    ticks: { color: '#9ca3af' }
                },
                y: {
                    grid: { color: '#374151', drawBorder: false },
                    ticks: { color: '#9ca3af' }
                }
            }
        };
        
        // 1. Retention Timeline Chart
        const retentionCtx = document.getElementById('retentionChart');
        if (retentionCtx) {
            chartRefs.current.retention = new Chart(retentionCtx, {
                type: 'line',
                data: {
                    labels: data.retention_timeline.map(d => d.date),
                    datasets: [
                        {
                            label: '5 Min Retention',
                            data: data.retention_timeline.map(d => d.retention_5m),
                            borderColor: '#10b981',
                            backgroundColor: 'rgba(16, 185, 129, 0.1)',
                            fill: true,
                            tension: 0.4
                        },
                        {
                            label: '10 Min Retention',
                            data: data.retention_timeline.map(d => d.retention_10m),
                            borderColor: '#3b82f6',
                            backgroundColor: 'rgba(59, 130, 246, 0.1)',
                            fill: true,
                            tension: 0.4
                        },
                        {
                            label: '20 Min Retention',
                            data: data.retention_timeline.map(d => d.retention_20m),
                            borderColor: '#8b5cf6',
                            backgroundColor: 'rgba(139, 92, 246, 0.1)',
                            fill: true,
                            tension: 0.4
                        }
                    ]
                },
                options: chartDefaults
            });
        }
        
        // 2. Discovery Timeline Chart
        const discoveryCtx = document.getElementById('discoveryChart');
        if (discoveryCtx) {
            chartRefs.current.discovery = new Chart(discoveryCtx, {
                type: 'bar',
                data: {
                    labels: data.discovery_timeline.map(d => d.date),
                    datasets: [
                        {
                            label: 'Peak Viewers',
                            data: data.discovery_timeline.map(d => d.peak_viewers),
                            backgroundColor: '#3b82f6',
                            borderRadius: 4
                        },
                        {
                            label: 'Neue Follower',
                            data: data.discovery_timeline.map(d => d.followers_delta),
                            backgroundColor: '#10b981',
                            borderRadius: 4
                        }
                    ]
                },
                options: chartDefaults
            });
        }
        
        // 3. Chat Timeline Chart
        const chatCtx = document.getElementById('chatChart');
        if (chatCtx) {
            chartRefs.current.chat = new Chart(chatCtx, {
                type: 'line',
                data: {
                    labels: data.chat_timeline.map(d => d.date),
                    datasets: [
                        {
                            label: 'Unique Chatters',
                            data: data.chat_timeline.map(d => d.unique_chatters),
                            borderColor: '#8b5cf6',
                            backgroundColor: 'rgba(139, 92, 246, 0.1)',
                            fill: true,
                            tension: 0.4,
                            yAxisID: 'y'
                        },
                        {
                            label: 'Chat/100 Viewers',
                            data: data.chat_timeline.map(d => d.chat_per_100),
                            borderColor: '#f59e0b',
                            borderDash: [5, 5],
                            tension: 0.4,
                            yAxisID: 'y1'
                        }
                    ]
                },
                options: {
                    ...chartDefaults,
                    scales: {
                        ...chartDefaults.scales,
                        y: {
                            ...chartDefaults.scales.y,
                            position: 'left'
                        },
                        y1: {
                            ...chartDefaults.scales.y,
                            position: 'right',
                            grid: { drawOnChartArea: false }
                        }
                    }
                }
            });
        }
        
        // Cleanup on unmount
        return () => {
            Object.values(chartRefs.current).forEach(chart => {
                if (chart) chart.destroy();
            });
        };
    }, [data]);
    
    // Loading state
    if (loading) return <LoadingSpinner />;
    
    // Error state
    if (error) return <ErrorDisplay error={error} onRetry={() => window.location.reload()} />;
    
    // No data
    if (!data) return null;
    
    const { metrics, insights } = data;
    
    return (
        <div className="min-h-screen pb-12">
            {/* Header */}
            <header className="mb-8">
                <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                    <div>
                        <h1 className="text-3xl font-bold text-white mb-1 font-display">
                            Streamer Analytics Dashboard
                        </h1>
                        <p className="text-gray-400 text-sm flex items-center gap-2">
                            <span className="w-2 h-2 rounded-full bg-green-400 inline-block animate-pulse"></span>
                            {streamer || 'Dein Kanal'} • Letzte {days} Tage
                        </p>
                    </div>
                    
                    {/* Time Range Selector */}
                    <div className="flex items-center gap-2 bg-card p-1.5 rounded-lg border border-white/5">
                        {[7, 30, 90].map(d => (
                            <button
                                key={d}
                                onClick={() => setDays(d)}
                                className={`px-4 py-2 rounded-md text-sm font-medium transition-all ${
                                    days === d
                                        ? 'bg-accent text-white shadow-lg'
                                        : 'text-gray-400 hover:text-white hover:bg-white/5'
                                }`}
                            >
                                {d} Tage
                            </button>
                        ))}
                    </div>
                </div>
            </header>
            
            {/* View Mode Tabs */}
            <ViewModeTabs activeView={viewMode} onViewChange={setViewMode} />
            
            {/* Overview Mode */}
            {viewMode === 'overview' && (
                <div className="space-y-6">
                    {/* KPI Cards */}
                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
                        <KpiCard
                            title="Ø Peak Viewer"
                            value={formatNumber(metrics.avg_peak_viewers, 0)}
                            subtitle="Letzte 30 Tage"
                            trend={metrics.peak_viewers_trend}
                            icon={Icons.Eye}
                            color="blue"
                        />
                        <KpiCard
                            title="Follower Gewinn"
                            value={`+${formatNumber(metrics.total_followers_delta)}`}
                            subtitle={`${formatNumber(metrics.followers_per_session, 1)}/Session`}
                            trend={metrics.followers_trend}
                            icon={Icons.UserPlus}
                            color="green"
                        />
                        <KpiCard
                            title="10-Min Retention"
                            value={formatPercent(metrics.retention_10m * 100, 1)}
                            subtitle="Viewer bleiben"
                            trend={metrics.retention_5m_trend}
                            icon={Icons.Target}
                            color="purple"
                        />
                        <KpiCard
                            title="Chat Gesundheit"
                            value={formatPercent(metrics.unique_chatters_per_100, 1)}
                            subtitle="Unique/100 Viewer"
                            trend={metrics.chat_engagement_trend}
                            icon={Icons.MessageCircle}
                            color="orange"
                        />
                    </div>
                    
                    {/* Insights */}
                    <InsightsPanel insights={insights} />
                    
                    {/* Charts Grid */}
                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                        <ChartContainer title="Retention Entwicklung">
                            <div className="h-64">
                                <canvas id="retentionChart"></canvas>
                            </div>
                        </ChartContainer>
                        
                        <ChartContainer title="Discovery & Wachstum">
                            <div className="h-64">
                                <canvas id="discoveryChart"></canvas>
                            </div>
                        </ChartContainer>
                    </div>
                </div>
            )}
            
            {/* Retention Mode */}
            {viewMode === 'retention' && (
                <div className="space-y-6">
                    <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
                        <ScoreGauge
                            score={metrics.retention_5m * 100}
                            label="5-Min Retention"
                            color="auto"
                        />
                        <ScoreGauge
                            score={metrics.retention_10m * 100}
                            label="10-Min Retention"
                            color="auto"
                        />
                        <ScoreGauge
                            score={metrics.retention_20m * 100}
                            label="20-Min Retention"
                            color="auto"
                        />
                        <ScoreGauge
                            score={100 - (metrics.avg_dropoff * 100)}
                            label="Retention Score"
                            color="auto"
                        />
                    </div>
                    
                    <ChartContainer title="Retention Timeline">
                        <div className="h-96">
                            <canvas id="retentionChart"></canvas>
                        </div>
                    </ChartContainer>
                </div>
            )}
            
            {/* Growth Mode */}
            {viewMode === 'growth' && (
                <div className="space-y-6">
                    <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
                        <KpiCard
                            title="Total Follower Δ"
                            value={`+${formatNumber(metrics.total_followers_delta)}`}
                            icon={Icons.UserPlus}
                            color="green"
                        />
                        <KpiCard
                            title="Follower/Session"
                            value={formatNumber(metrics.followers_per_session, 1)}
                            icon={Icons.Activity}
                            color="blue"
                        />
                        <KpiCard
                            title="Follower/Stunde"
                            value={formatNumber(metrics.followers_per_hour, 2)}
                            icon={Icons.Zap}
                            color="purple"
                        />
                        <KpiCard
                            title="Avg. Peak"
                            value={formatNumber(metrics.avg_peak_viewers, 0)}
                            icon={Icons.Eye}
                            color="orange"
                        />
                    </div>
                    
                    <ChartContainer title="Discovery Funnel">
                        <div className="h-96">
                            <canvas id="discoveryChart"></canvas>
                        </div>
                    </ChartContainer>
                </div>
            )}
            
            {/* Chat Mode */}
            {viewMode === 'chat' && (
                <div className="space-y-6">
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                        <KpiCard
                            title="Chat/100 Viewer"
                            value={formatPercent(metrics.unique_chatters_per_100, 1)}
                            subtitle="Engagement Rate"
                            icon={Icons.MessageCircle}
                            color="purple"
                        />
                        <KpiCard
                            title="First-Time Chatter"
                            value={formatNumber(metrics.total_first_time_chatters)}
                            subtitle="Neue Stimmen"
                            icon={Icons.UserPlus}
                            color="blue"
                        />
                        <KpiCard
                            title="Returning Chatter"
                            value={formatNumber(metrics.total_returning_chatters)}
                            subtitle="Stammgäste"
                            icon={Icons.Users}
                            color="green"
                        />
                    </div>
                    
                    <ChartContainer title="Chat Aktivität">
                        <div className="h-96">
                            <canvas id="chatChart"></canvas>
                        </div>
                    </ChartContainer>
                </div>
            )}
            
            {/* Comparison Mode */}
            {viewMode === 'comparison' && (
                <ComparisonView 
                    data={data.comparison || {}} 
                    streamerLogin={streamer} 
                />
            )}
            
            {/* Detailed Mode */}
            {viewMode === 'detailed' && (
                <SessionTable sessions={data.sessions || []} limit={30} />
            )}
        </div>
    );
};

// Render
const root = ReactDOM.createRoot(document.getElementById('analytics-root'));
root.render(<AnalyticsDashboard />);
