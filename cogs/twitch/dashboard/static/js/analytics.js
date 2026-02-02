const { useState, useEffect, useMemo } = React;

/* --- Icons --- */
const Icons = {
    TrendUp: () => <span style={{ color: '#4ade80' }}>▲</span>,
    TrendDown: () => <span style={{ color: '#f87171' }}>▼</span>,
    Users: () => <span>👥</span>,
    Clock: () => <span>⏰</span>,
    Message: () => <span>💬</span>,
    Star: () => <span>⭐</span>,
    Target: () => <span>🎯</span>,
    Zap: () => <span>⚡</span>
};

/* --- Helpers --- */
const fmtNumber = (n, d=0) => n?.toLocaleString(undefined, {minimumFractionDigits: d, maximumFractionDigits: d}) ?? '-';
const fmtPercent = (n, d=1) => (n !== undefined && n !== null) ? `${n.toFixed(d)}%` : '-';

/* --- Components --- */

const ScoreGauge = ({ score, label, color = '#7c3aed' }) => {
    const circumference = 2 * Math.PI * 40;
    const offset = circumference - ((score / 100) * circumference);
    
    return (
        <div className="flex flex-col items-center justify-center p-4 bg-card rounded-xl border border-white/5 relative overflow-hidden group hover:border-white/10 transition-all">
            <div className="relative w-32 h-32 flex items-center justify-center">
                <svg className="w-full h-full transform -rotate-90" viewBox="0 0 100 100">
                    <circle cx="50" cy="50" r="40" stroke="#1f2937" strokeWidth="8" fill="none" />
                    <circle 
                        cx="50" cy="50" r="40" 
                        stroke={color} strokeWidth="8" fill="none" 
                        strokeDasharray={circumference} 
                        strokeDashoffset={offset} 
                        strokeLinecap="round"
                        className="transition-all duration-1000 ease-out"
                    />
                </svg>
                <div className="absolute inset-0 flex flex-col items-center justify-center">
                    <span className="text-3xl font-bold text-white">{Math.round(score)}</span>
                </div>
            </div>
            <span className="mt-2 text-gray-400 font-medium tracking-wide uppercase text-xs">{label}</span>
        </div>
    );
};

const KpiCard = ({ title, value, subValue, trend, icon: Icon, color = "blue" }) => {
    const trendColor = trend > 0 ? 'text-green-400' : trend < 0 ? 'text-red-400' : 'text-gray-400';
    const trendIcon = trend > 0 ? '▲' : trend < 0 ? '▼' : '–';
    
    return (
        <div className="bg-card p-5 rounded-xl border border-white/5 shadow-lg hover:shadow-xl hover:border-white/10 transition-all">
            <div className="flex justify-between items-start mb-2">
                <span className="text-gray-400 text-sm font-semibold uppercase tracking-wider">{title}</span>
                {Icon && <div className={`p-2 rounded-lg bg-${color}-500/10 text-${color}-400`}><Icon /></div>}
            </div>
            <div className="flex items-end gap-3 mt-1">
                <span className="text-3xl font-bold text-white">{value}</span>
                {trend !== undefined && (
                    <span className={`text-sm font-medium mb-1 ${trendColor}`}>
                        {trendIcon} {Math.abs(trend)}%
                    </span>
                )}
            </div>
            {subValue && <div className="mt-3 text-xs text-gray-500 font-medium">{subValue}</div>}
        </div>
    );
};

const ActionPlan = ({ actions }) => {

    if (!actions || actions.length === 0) return null;

    return (

        <div className="bg-card p-6 rounded-xl border border-accent/20 shadow-lg mb-8">

            <h3 className="text-xl font-bold text-white mb-4 flex items-center gap-2">

                <span className="bg-accent/20 p-1.5 rounded text-accent"><Icons.Target /></span> 

                Action Plan

            </h3>

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">

                {actions.map((act, i) => (

                    <div key={i} className="bg-black/20 p-4 rounded-lg border-l-4 border-accent">

                        <span className="text-xs font-bold text-accent uppercase tracking-wider mb-1 block">{act.tag}</span>

                        <p className="text-gray-300 text-sm font-medium">{act.text}</p>

                    </div>

                ))}

            </div>

        </div>

    );

};



const Correlations = ({ data }) => {

    if (!data) return null;

    const { durationVsViewers, chatVsRetention } = data;

    

    const getLabel = (val) => {

        if (val > 0.3) return { text: "Strong Positive", color: "text-green-400" };

        if (val < -0.3) return { text: "Negative Impact", color: "text-red-400" };

        return { text: "No Correlation", color: "text-gray-500" };

    };



    const dur = getLabel(durationVsViewers);

    const chat = getLabel(chatVsRetention);



    return (

        <div className="bg-card p-5 rounded-xl border border-white/5 mb-8">

            <h4 className="text-sm font-bold text-gray-400 uppercase tracking-wider mb-4">Metric Correlations</h4>

            <div className="flex flex-col gap-3">

                <div className="flex justify-between items-center text-sm border-b border-white/5 pb-2">

                    <span className="text-gray-300">Longer Streams → More Viewers?</span>

                    <span className={`font-bold ${dur.color}`}>{dur.text} ({durationVsViewers})</span>

                </div>

                <div className="flex justify-between items-center text-sm">

                    <span className="text-gray-300">More Chat → Higher Retention?</span>

                    <span className={`font-bold ${chat.color}`}>{chat.text} ({chatVsRetention})</span>

                </div>

            </div>

        </div>

    );

};



/* --- Main Application --- */



const App = () => {

    const [loading, setLoading] = useState(true);

    const [data, setData] = useState(null);

    const [days, setDays] = useState(30);

    const [streamer, setStreamer] = useState('');

    const [error, setError] = useState(null);



    // Initial Config Load

    useEffect(() => {

        const cfgEl = document.getElementById('analytics-config');

        if (cfgEl) {

            try {

                const cfg = JSON.parse(cfgEl.textContent);

                if (cfg.streamer) setStreamer(cfg.streamer);

                if (cfg.days) setDays(cfg.days);

            } catch(e) { console.error("Config parse error", e); }

        }

    }, []);



    // Data Fetch

    useEffect(() => {

        const fetchData = async () => {

            setLoading(true);

            try {

                const params = new URLSearchParams({ days });

                if (streamer) params.set('streamer', streamer);

                

                // Keep partner token if present

                const urlParams = new URLSearchParams(window.location.search);

                if (urlParams.has('partner_token')) {

                    params.set('partner_token', urlParams.get('partner_token'));

                }



                const res = await fetch(`/twitch/api/analytics?${params}`);

                const json = await res.json();

                

                if (json.error) throw new Error(json.error);

                setData(json);

                setError(null);

            } catch (err) {

                console.error(err);

                setError(err.message);

            } finally {

                setLoading(false);

            }

        };



        fetchData();

    }, [days, streamer]);



    // Chart Setup

    const chartRefs = React.useRef({});

    

    useEffect(() => {

        if (!data || !data.sessions) return;

        

        // Helper to create chart

        const createChart = (id, config) => {

            const ctx = document.getElementById(id);

            if (!ctx) return;

            

            if (chartRefs.current[id]) chartRefs.current[id].destroy();

            

            // Common Options

            const defaults = {

                responsive: true,

                maintainAspectRatio: false,

                plugins: {

                    legend: { labels: { color: '#9ca3af' } },

                    tooltip: {

                        mode: 'index', intersect: false,

                        backgroundColor: '#111827', titleColor: '#fff', bodyColor: '#d1d5db', borderColor: '#374151', borderWidth: 1

                    }

                },

                scales: {

                    x: { grid: { color: '#374151', drawBorder: false }, ticks: { color: '#9ca3af' } },

                    y: { grid: { color: '#374151', drawBorder: false }, ticks: { color: '#9ca3af' } }

                }

            };



            chartRefs.current[id] = new Chart(ctx, {

                ...config,

                options: { ...defaults, ...config.options }

            });

        };



        // 1. Trend Chart (Viewers)

        const recentSessions = [...data.sessions].reverse(); // Chronological

        createChart('viewerTrendChart', {

            type: 'line',

            data: {

                labels: recentSessions.map(s => s.date),

                datasets: [

                    {

                        label: 'Avg Viewers',

                        data: recentSessions.map(s => s.avgViewers),

                        borderColor: '#8b5cf6',

                        backgroundColor: 'rgba(139, 92, 246, 0.1)',

                        fill: true,

                        tension: 0.4

                    },

                    {

                        label: 'Peak Viewers',

                        data: recentSessions.map(s => s.peakViewers),

                        borderColor: '#38bdf8',

                        borderDash: [5, 5],

                        tension: 0.4

                    }

                ]

            }

        });



        // 2. Retention vs Engagement Radar (if scores exist)

        if (data.scores) {

             createChart('scoreRadarChart', {

                type: 'radar',

                data: {

                    labels: ['Reach', 'Retention', 'Engagement', 'Growth', 'Monetization', 'Network'],

                    datasets: [{

                        label: 'Channel Health',

                        data: [

                            data.scores.reach, data.scores.retention, data.scores.engagement,

                            data.scores.growth, data.scores.monetization, data.scores.network

                        ],

                        backgroundColor: 'rgba(124, 58, 237, 0.2)',

                        borderColor: '#7c3aed',

                        pointBackgroundColor: '#fff',

                        pointBorderColor: '#7c3aed',

                    }]

                },

                options: {

                    scales: {

                        r: {

                            angleLines: { color: '#374151' },

                            grid: { color: '#374151' },

                            pointLabels: { color: '#d1d5db', font: { size: 12 } },

                            suggestedMin: 0,

                            suggestedMax: 100

                        }

                    }

                }

            });

        }



    }, [data]);





    if (loading) return (

        <div className="flex items-center justify-center min-h-screen text-accent animate-pulse">

            <svg className="w-10 h-10 mr-3" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none"></circle><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path></svg>

            Loading Analytics...

        </div>

    );



    if (error) return (

        <div className="p-8 text-center">

            <h2 className="text-red-400 text-xl font-bold mb-2">Error Loading Data</h2>

            <p className="text-gray-400">{error}</p>

            <button onClick={() => window.location.reload()} className="mt-4 px-4 py-2 bg-white/10 hover:bg-white/20 rounded">Retry</button>

        </div>

    );



    if (!data) return null;



    const { scores, summary, findings, actions, correlations, sessions } = data;



    return (

        <div className="min-h-screen pb-12">

            {/* Header */}

            <header className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-8">

                <div>

                    <h1 className="text-3xl font-display font-bold text-white mb-1">Channel Analytics</h1>

                    <p className="text-gray-400 text-sm flex items-center gap-2">

                        <span className="w-2 h-2 rounded-full bg-green-400 inline-block animate-pulse"></span>

                        {streamer || 'Your Channel'} • Last {days} Days

                    </p>

                </div>

                

                <div className="flex items-center gap-3 bg-card p-1 rounded-lg border border-white/5">

                    {[7, 30, 90].map(d => (

                        <button 

                            key={d}

                            onClick={() => setDays(d)}

                            className={`px-4 py-1.5 rounded-md text-sm font-medium transition-all ${days === d ? 'bg-accent text-white shadow' : 'text-gray-400 hover:text-white'}`}

                        >

                            {d}d

                        </button>

                    ))}

                </div>

            </header>



            {/* Top Scores Grid */}

            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">

                {/* Main Score - Featured */}

                <div className="md:col-span-2 lg:col-span-1 bg-gradient-to-br from-card to-accent/10 p-6 rounded-2xl border border-accent/20 flex flex-col items-center justify-center text-center relative overflow-hidden">

                    <div className="absolute top-0 right-0 p-4 opacity-10"><Icons.Zap /></div>

                    <h2 className="text-gray-300 font-semibold uppercase tracking-wider text-xs mb-4">Channel Health</h2>

                    <div className="relative mb-2">

                        <span className="text-6xl font-display font-bold text-white">{scores?.total ?? 0}</span>

                        <span className="text-xl text-accent absolute -top-1 -right-4">/100</span>

                    </div>

                    <div className="mt-4 w-full grid grid-cols-3 gap-2 text-xs text-gray-400">

                        <div className="bg-black/20 rounded p-1">Reach <strong>{scores?.reach}</strong></div>

                        <div className="bg-black/20 rounded p-1">Ret. <strong>{scores?.retention}</strong></div>

                        <div className="bg-black/20 rounded p-1">Eng. <strong>{scores?.engagement}</strong></div>

                    </div>

                </div>



                {/* Key KPIs */}

                <KpiCard 

                    title="Average Viewers" 

                    value={fmtNumber(summary.avgViewers, 1)} 

                    subValue={`Peak: ${fmtNumber(summary.peakViewers)}`}

                    icon={Icons.Users}

                    color="blue"

                />

                 <KpiCard 

                    title="Follower Growth" 

                    value={`+${fmtNumber(summary.followersDelta)}`} 

                    subValue={`${fmtNumber(summary.followersPerHour, 2)} / hr`}

                    trend={0} 

                    icon={Icons.TrendUp}

                    color="green"

                />

                <KpiCard 

                    title="Retention (10m)" 

                    value={fmtPercent(summary.retention10m, 1)} 

                    subValue="Target: >40%"

                    icon={Icons.Target}

                    color="purple"

                />

            </div>



            {/* AI Findings */}

            {findings && findings.length > 0 && (

                <div className="mb-8">

                    <h3 className="text-lg font-bold text-white mb-4">Key Findings</h3>

                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">

                        {findings.map((ins, i) => (

                            <div key={i} className={`p-4 rounded-lg border border-l-4 shadow-sm ${

                                ins.type === 'neg' ? 'bg-red-500/10 border-red-500 border-l-red-500' : 

                                ins.type === 'warn' ? 'bg-yellow-500/10 border-yellow-500 border-l-yellow-500' :

                                'bg-green-500/10 border-green-500 border-l-green-500'

                            }`}>

                                <h4 className={`text-sm font-bold mb-1 ${

                                    ins.type === 'neg' ? 'text-red-200' : 

                                    ins.type === 'warn' ? 'text-yellow-200' :

                                    'text-green-200'

                                }`}>{ins.title}</h4>

                                <p className="text-gray-300 text-sm">{ins.text}</p>

                            </div>

                        ))}

                    </div>

                </div>

            )}



            {/* Action Plan */}

            <ActionPlan actions={actions} />



            {/* Main Content Grid */}

            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mb-8">

                {/* Left Column: Charts */}

                <div className="lg:col-span-2 flex flex-col gap-6">

                    <ChartContainer title="Viewer Growth Trend">

                        <canvas id="viewerTrendChart"></canvas>

                    </ChartContainer>

                    

                    {/* Session Table */}

                    <div className="bg-card rounded-xl border border-white/5 overflow-hidden shadow-lg">

                        <div className="p-5 border-b border-white/5 flex justify-between items-center">

                            <h3 className="text-lg font-bold text-white">Recent Sessions</h3>

                            <button className="text-xs bg-white/5 hover:bg-white/10 px-3 py-1 rounded transition">View All</button>

                        </div>

                        <div className="overflow-x-auto">

                            <table className="w-full text-left text-sm text-gray-400">

                                <thead className="bg-black/20 text-xs uppercase font-semibold text-gray-500">

                                    <tr>

                                        <th className="px-5 py-3">Date</th>

                                        <th className="px-5 py-3">Duration</th>

                                        <th className="px-5 py-3">Avg Viewers</th>

                                        <th className="px-5 py-3">Peak</th>

                                        <th className="px-5 py-3">Retention</th>

                                        <th className="px-5 py-3">Score</th>

                                    </tr>

                                </thead>

                                <tbody className="divide-y divide-white/5">

                                    {sessions.slice(0, 8).map(s => (

                                        <tr key={s.id} className="hover:bg-white/5 transition">

                                            <td className="px-5 py-3 text-white font-medium">{s.date} <span className="text-gray-600 text-xs ml-1">{s.startTime}</span></td>

                                            <td className="px-5 py-3">{Math.floor(s.duration / 60)}m</td>

                                            <td className="px-5 py-3">{fmtNumber(s.avgViewers, 0)}</td>

                                            <td className="px-5 py-3">{s.peakViewers}</td>

                                            <td className="px-5 py-3">

                                                <div className="flex items-center gap-2">

                                                    <div className="w-16 h-1.5 bg-gray-700 rounded-full overflow-hidden">

                                                        <div className="h-full bg-blue-500" style={{ width: `${s.retention10m}%` }}></div>

                                                    </div>

                                                    <span className="text-xs">{s.retention10m}%</span>

                                                </div>

                                            </td>

                                            <td className="px-5 py-3">

                                                 <a href={`/twitch/session/${s.id}`} className="text-accent hover:text-accent-hover font-semibold text-xs border border-accent/30 px-2 py-1 rounded">Analyze</a>

                                            </td>

                                        </tr>

                                    ))}

                                </tbody>

                            </table>

                        </div>

                    </div>

                </div>



                {/* Right Column: Score Radar & Details */}

                <div className="flex flex-col gap-6">

                    <Correlations data={correlations} />



                    <div className="bg-card p-6 rounded-xl border border-white/5 shadow-lg">

                        <h3 className="text-lg font-bold text-white mb-4">Performance Mix</h3>

                        <div className="h-64 relative">

                            <canvas id="scoreRadarChart"></canvas>

                        </div>

                        <div className="mt-4 text-xs text-center text-gray-500">

                            Based on benchmarks vs. Deadlock category

                        </div>

                    </div>



                    {/* Score Breakdown Cards */}

                    <div className="grid grid-cols-2 gap-4">

                        <ScoreGauge score={scores?.growth ?? 0} label="Growth" color="#4ade80" />

                        <ScoreGauge score={scores?.monetization ?? 0} label="Revenue" color="#fbbf24" />

                        <ScoreGauge score={scores?.network ?? 0} label="Network" color="#ec4899" />

                        <ScoreGauge score={scores?.retention ?? 0} label="Retention" color="#60a5fa" />

                    </div>



                    {/* Network Stats */}

                    <div className="bg-card p-5 rounded-xl border border-white/5">

                        <h4 className="text-sm font-bold text-gray-300 mb-3 uppercase tracking-wide">Network Activity</h4>

                        <div className="space-y-3">

                            <div className="flex justify-between items-center p-3 bg-black/20 rounded-lg">

                                <span className="text-gray-400 text-sm">Raids Sent</span>

                                <span className="text-white font-bold">{data.network?.sent ?? 0}</span>

                            </div>

                            <div className="flex justify-between items-center p-3 bg-black/20 rounded-lg">

                                <span className="text-gray-400 text-sm">Raids Received</span>

                                <span className="text-white font-bold">{data.network?.received ?? 0}</span>

                            </div>

                            <div className="flex justify-between items-center p-3 bg-black/20 rounded-lg">

                                <span className="text-gray-400 text-sm">Raid Viewers Sent</span>

                                <span className="text-white font-bold">{fmtNumber(data.network?.sentViewers)}</span>

                            </div>

                        </div>

                    </div>

                </div>

            </div>

        </div>

    );

};



// Render

const container = document.getElementById('analytics-root');

const root = ReactDOM.createRoot(container);

root.render(<App />);
