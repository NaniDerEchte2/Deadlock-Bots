// Deadlock Performance Bot - Node.js Backend
// npm install express cors axios node-cron ws

const express = require('express');
const cors = require('cors');
const axios = require('axios');
const cron = require('node-cron');
const WebSocket = require('ws');
const path = require('path');

class DeadlockBot {
    constructor() {
        this.app = express();
        this.server = null;
        this.wss = null;
        
        // API Configuration
        this.apiKey = 'HEXE-996eaf69-8130-45d7-bf04-a2fdc0dc4962';
        this.apiBaseUrl = 'https://api.deadlock-api.com/v1';
        this.apiFailureCount = 0;
        this.maxApiFailures = 5; // Nach 5 Fehlern stoppen wir API-Calls für eine Weile
        this.apiLastCheck = Date.now();
        
        // Tracked players and their stats
        this.trackedPlayers = new Map();
        this.activeMatches = new Map();
        this.isTracking = false;
        this.trackingInterval = null;
        
        this.setupExpress();
        this.setupWebSocket();
        
        console.log('🎮 Deadlock Performance Bot initialized!');
        console.log('⏸️ Tracking is STOPPED - use Start/Stop buttons to control');
    }

    setupExpress() {
        // Middleware
        this.app.use(cors());
        this.app.use(express.json());
        this.app.use(express.static('public'));

        // Serve the overlay HTML
        this.app.get('/', (req, res) => {
            res.sendFile(path.join(__dirname, 'overlay.html'));
        });

        // API Routes
        this.app.post('/api/track-player', async (req, res) => {
            const { steamId, username } = req.body;
            await this.addPlayerToTracking(steamId, username);
            res.json({ success: true, message: `Tracking ${username || steamId}` });
        });

        this.app.get('/api/player/:steamId/stats', async (req, res) => {
            const { steamId } = req.params;
            const stats = await this.getPlayerStats(steamId);
            res.json(stats);
        });

        this.app.get('/api/player/:steamId/current-match', async (req, res) => {
            const { steamId } = req.params;
            const match = await this.getCurrentMatch(steamId);
            res.json(match);
        });

        this.app.post('/api/start-tracking', async (req, res) => {
            this.startTracking();
            res.json({ success: true, message: 'Tracking gestartet' });
        });

        this.app.post('/api/stop-tracking', async (req, res) => {
            this.stopTracking();
            res.json({ success: true, message: 'Tracking gestoppt' });
        });

        this.app.get('/api/tracking-status', (req, res) => {
            res.json({ 
                isTracking: this.isTracking,
                trackedPlayers: this.trackedPlayers.size,
                uptime: process.uptime()
            });
        });

        this.app.get('/api/leaderboard', (req, res) => {
            const leaderboard = this.generateLeaderboard();
            res.json(leaderboard);
        });

        // Start server
        this.server = this.app.listen(4000, () => {
            console.log('🚀 Bot läuft auf http://localhost:4000');
            console.log('📊 Overlay verfügbar unter http://localhost:4000');
        });
    }

    setupWebSocket() {
        this.wss = new WebSocket.Server({ server: this.server });
        
        this.wss.on('connection', (ws) => {
            console.log('👤 Overlay connected');
            
            ws.on('message', async (message) => {
                const data = JSON.parse(message);
                
                switch (data.type) {
                    case 'track_player':
                        await this.addPlayerToTracking(data.steamId, data.username);
                        break;
                    case 'get_stats':
                        const stats = await this.getPlayerStats(data.steamId);
                        ws.send(JSON.stringify({ type: 'stats_update', data: stats }));
                        break;
                    case 'start_tracking':
                        this.startTracking();
                        break;
                    case 'stop_tracking':
                        this.stopTracking();
                        break;
                    case 'start_live_tracking':
                        this.startLiveTracking(data.steamId, ws);
                        break;
                    case 'get_leaderboard':
                        const leaderboard = this.generateLeaderboard();
                        ws.send(JSON.stringify({ 
                            type: 'leaderboard_data', 
                            data: leaderboard 
                        }));
                        break;
                }
            });

            ws.on('close', () => {
                console.log('👤 Overlay disconnected');
            });
        });
    }

    async makeApiCall(endpoint, params = {}) {
        // Check if API is down/nicht verfügbar
        if (this.apiFailureCount >= this.maxApiFailures) {
            const timeSinceLastCheck = Date.now() - this.apiLastCheck;
            if (timeSinceLastCheck < 300000) { // 5 Minuten warten
                return null; // Skip API call
            } else {
                // Reset und versuche wieder
                this.apiFailureCount = 0;
                console.log('🔄 Retrying API after cooldown period');
            }
        }

        try {
            const url = `${this.apiBaseUrl}${endpoint}`;
            const config = {
                headers: {
                    'Authorization': `Bearer ${this.apiKey}`,
                    'X-API-Key': this.apiKey,
                    'Accept': 'application/json'
                },
                params: {
                    api_key: this.apiKey,
                    ...params
                }
            };

            const response = await axios.get(url, config);
            
            // Reset failure count on success
            this.apiFailureCount = 0;
            return response.data;
            
        } catch (error) {
            if (error.response && error.response.status === 404) {
                this.apiFailureCount++;
                this.apiLastCheck = Date.now();
                
                // Nur einmal loggen wenn API down ist
                if (this.apiFailureCount === this.maxApiFailures) {
                    console.log(`⚠️ API seems down (${this.maxApiFailures} 404s) - switching to demo mode for 5 minutes`);
                }
                
                return null;
            } else {
                console.error(`❌ API Error ${endpoint}:`, error.message);
                return null;
            }
        }
    }

    async addPlayerToTracking(steamId, username = null) {
        console.log(`➕ Adding player to tracking: ${username || steamId}`);
        
        // Hole Spieler-Infos
        const playerInfo = await this.getPlayerInfo(steamId);
        
        const playerData = {
            steamId,
            username: username || playerInfo?.personaname || `Player_${steamId.slice(-4)}`,
            lastUpdate: Date.now(),
            currentStats: {
                souls: 0,
                kills: 0,
                deaths: 0,
                assists: 0,
                heroDamage: 0,
                netWorth: 0,
                soulsPerMinute: 0
            },
            matchHistory: [],
            isInMatch: false,
            currentMatchId: null
        };
        
        this.trackedPlayers.set(steamId, playerData);
        this.broadcastUpdate('player_added', playerData);
        
        return playerData;
    }

    async getPlayerInfo(steamId) {
        // Versuche verschiedene Endpoints
        const endpoints = [
            `/players/${steamId}`,
            `/players/${steamId}/profile`,
            `/player/${steamId}`
        ];

        for (const endpoint of endpoints) {
            const data = await this.makeApiCall(endpoint);
            if (data) return data;
        }

        return null;
    }

    async getPlayerStats(steamId) {
        const endpoints = [
            `/players/${steamId}/stats`,
            `/players/${steamId}/matches/recent?limit=1`,
            `/players/${steamId}`
        ];

        for (const endpoint of endpoints) {
            const data = await this.makeApiCall(endpoint);
            if (data) {
                return this.processPlayerStats(data, steamId);
            }
        }

        return this.getDefaultStats();
    }

    processPlayerStats(apiData, steamId) {
        // Verarbeite API-Daten zu einheitlichem Format
        const processed = {
            steamId,
            lastUpdate: Date.now(),
            stats: {
                souls: apiData.souls || apiData.net_worth || 0,
                kills: apiData.kills || 0,
                deaths: apiData.deaths || 0,
                assists: apiData.assists || 0,
                heroDamage: apiData.hero_damage || apiData.damage_dealt || 0,
                netWorth: apiData.net_worth || apiData.souls || 0,
                matchTime: apiData.match_time || apiData.duration || 0
            },
            performance: this.calculatePerformanceMetrics(apiData),
            isInMatch: apiData.is_in_match || false,
            currentMatch: apiData.current_match || null
        };

        // Berechne Souls per Minute
        if (processed.stats.matchTime > 0) {
            processed.stats.soulsPerMinute = (processed.stats.souls / (processed.stats.matchTime / 60)).toFixed(1);
        }

        return processed;
    }

    calculatePerformanceMetrics(data) {
        const kda = data.deaths > 0 ? 
            ((data.kills + data.assists) / data.deaths).toFixed(2) : 
            (data.kills + data.assists).toFixed(2);

        const soulsPerMin = data.matchTime > 0 ? 
            (data.souls / (data.matchTime / 60)).toFixed(1) : 
            data.souls > 0 ? (data.souls / 1).toFixed(1) : 0; // Fallback wenn keine matchTime

        // Performance-Scores (0-100)
        const farmEfficiency = Math.min(100, (parseFloat(soulsPerMin) / 400) * 100); // 400 SPM als Benchmark
        const combatScore = Math.min(100, parseFloat(kda) * 20);
        const damageScore = Math.min(100, ((data.heroDamage || 0) / 20000) * 100); // 20k als Benchmark

        const result = {
            kda: parseFloat(kda),
            soulsPerMinute: parseFloat(soulsPerMin),
            farmEfficiency: Math.round(farmEfficiency),
            combatScore: Math.round(combatScore),
            damageScore: Math.round(damageScore),
            overallScore: Math.round((farmEfficiency + combatScore + damageScore) / 3)
        };

        console.log(`📊 Performance calculated:`, result);
        return result;
    }

    async getCurrentMatch(steamId) {
        const endpoints = [
            `/players/${steamId}/current`,
            `/players/${steamId}/matches/live`,
            `/matches/live?player=${steamId}`
        ];

        for (const endpoint of endpoints) {
            const data = await this.makeApiCall(endpoint);
            if (data && data.match_id) {
                return data;
            }
        }

        return null;
    }

    startLiveTracking(steamId, websocket) {
        console.log(`🔴 Starting live tracking for ${steamId}`);
        
        // Update alle 10 Sekunden - aber nur wenn bot tracking aktiv ist
        const interval = setInterval(async () => {
            if (!this.isTracking || websocket.readyState !== WebSocket.OPEN) {
                clearInterval(interval);
                console.log(`🔴 Stopped live tracking for ${steamId} (tracking stopped or connection closed)`);
                return;
            }

            const stats = await this.getPlayerStats(steamId);
            
            if (stats && websocket.readyState === WebSocket.OPEN) {
                websocket.send(JSON.stringify({
                    type: 'live_stats_update',
                    steamId,
                    data: stats
                }));
            }
        }, 10000);

        // Speichere interval am websocket für cleanup
        websocket.liveTrackingInterval = interval;

        // Cleanup wenn Verbindung geschlossen wird
        websocket.on('close', () => {
            clearInterval(interval);
            console.log(`🔴 Stopped live tracking for ${steamId} (websocket closed)`);
        });
    }

    startTracking() {
        if (this.isTracking) {
            console.log('⚠️ Tracking bereits aktiv!');
            return;
        }

        this.isTracking = true;
        console.log('▶️ Performance tracking GESTARTET');
        
        this.broadcastUpdate('tracking_started', { 
            status: 'started',
            message: 'Bot tracking gestartet'
        });

        this.startPerformanceTracking();
    }

    stopTracking() {
        if (!this.isTracking) {
            console.log('⚠️ Tracking bereits gestoppt!');
            return;
        }

        this.isTracking = false;
        
        // Stoppe alle Intervals
        if (this.trackingInterval) {
            clearInterval(this.trackingInterval);
            this.trackingInterval = null;
        }

        // Stoppe auch alle Live-Tracking Intervals
        this.wss.clients.forEach(client => {
            if (client.liveTrackingInterval) {
                clearInterval(client.liveTrackingInterval);
                client.liveTrackingInterval = null;
            }
        });

        console.log('⏹️ Performance tracking GESTOPPT - alle API-Calls gestoppt');
        
        this.broadcastUpdate('tracking_stopped', { 
            status: 'stopped',
            message: 'Bot tracking gestoppt - keine API-Calls mehr'
        });
    }

    startPerformanceTracking() {
        if (this.trackingInterval) {
            clearInterval(this.trackingInterval);
        }

        // Update alle 30 Sekunden - aber nur wenn tracking aktiv ist
        this.trackingInterval = setInterval(async () => {
            if (!this.isTracking) {
                return; // Skip wenn tracking gestoppt
            }

            console.log('🔄 Running tracking cycle...');
            
            for (const [steamId, playerData] of this.trackedPlayers) {
                try {
                    const newStats = await this.getPlayerStats(steamId);
                    
                    if (newStats && newStats.stats) {
                        // Update player data
                        playerData.currentStats = newStats.stats;
                        playerData.lastUpdate = Date.now();
                        
                        // Check for match changes
                        const currentMatch = await this.getCurrentMatch(steamId);
                        if (currentMatch && currentMatch.match_id !== playerData.currentMatchId) {
                            playerData.currentMatchId = currentMatch.match_id;
                            playerData.isInMatch = true;
                            
                            this.broadcastUpdate('match_started', {
                                steamId,
                                matchId: currentMatch.match_id,
                                player: playerData.username
                            });
                        }
                        
                        // Broadcast stats update
                        this.broadcastUpdate('stats_update', {
                            steamId,
                            stats: newStats,
                            username: playerData.username
                        });
                        
                        console.log(`✅ Updated stats for ${playerData.username} - SPM: ${newStats.performance?.soulsPerMinute || 0}`);
                    } else {
                        // Fallback: Generate demo data when API fails
                        console.log(`⚠️ No API data for ${playerData.username} - generating demo data`);
                        const demoStats = this.generateDemoStats(playerData);
                        
                        playerData.currentStats = demoStats.stats;
                        playerData.lastUpdate = Date.now();
                        
                        this.broadcastUpdate('stats_update', {
                            steamId,
                            stats: demoStats
                        });
                    }
                } catch (error) {
                    console.error(`❌ Error updating ${playerData.username}:`, error.message);
                }
            }
        }, 30000);

        console.log('⏰ Performance tracking scheduler configured');
    }

    generateLeaderboard() {
        const players = Array.from(this.trackedPlayers.values())
            .map(player => ({
                username: player.username,
                steamId: player.steamId,
                soulsPerMinute: player.currentStats.soulsPerMinute || 0,
                kda: player.currentStats.kills && player.currentStats.deaths > 0 ?
                    ((player.currentStats.kills + player.currentStats.assists) / player.currentStats.deaths).toFixed(2) : 0,
                isInMatch: player.isInMatch,
                lastUpdate: player.lastUpdate
            }))
            .sort((a, b) => parseFloat(b.soulsPerMinute) - parseFloat(a.soulsPerMinute));

        return {
            timestamp: Date.now(),
            players,
            totalTracked: players.length,
            activeMatches: Array.from(this.trackedPlayers.values()).filter(p => p.isInMatch).length
        };
    }

    broadcastUpdate(type, data) {
        const message = JSON.stringify({ type, data, timestamp: Date.now() });
        
        this.wss.clients.forEach(client => {
            if (client.readyState === WebSocket.OPEN) {
                client.send(message);
            }
        });
    }

    generateDemoStats(playerData) {
        // Generate realistic demo data mit progressiver Entwicklung
        const baseStats = playerData.currentStats;
        const now = Date.now();
        const timeSinceLastUpdate = now - (playerData.lastUpdate || now - 30000);
        const minutesSinceStart = Math.max(1, (now - (playerData.demoStartTime || now)) / 60000);
        
        // Realistische Souls-Progression (300-450 SPM je nach Phase)
        const targetSPM = Math.min(450, 200 + (minutesSinceStart * 15)); // Steigt über Zeit
        const soulsGain = (targetSPM / 60) * (timeSinceLastUpdate / 1000); // Pro Sekunde
        const newSouls = Math.round(baseStats.souls + soulsGain + (Math.random() * 10 - 5));
        
        // Gelegentliche Events basierend auf Spielzeit
        let newKills = baseStats.kills;
        let newDeaths = baseStats.deaths;
        let newAssists = baseStats.assists;
        
        // Frühe Game: Weniger Kills
        // Late Game: Mehr Action
        const actionMultiplier = Math.min(2, minutesSinceStart / 15);
        
        if (Math.random() < (0.03 * actionMultiplier)) { // Kill chance steigt über Zeit
            newKills++;
            this.broadcastUpdate('demo_event', {
                type: 'kill',
                player: playerData.username,
                message: `${playerData.username} got a kill! (+${Math.floor(Math.random() * 400 + 200)} souls)`
            });
        }
        
        if (Math.random() < (0.015 * actionMultiplier)) { // Death chance
            newDeaths++;
            this.broadcastUpdate('demo_event', {
                type: 'death',
                player: playerData.username,
                message: `${playerData.username} died`
            });
        }
        
        if (Math.random() < (0.05 * actionMultiplier)) { // Assist chance
            newAssists++;
        }

        // Realistische Damage-Progression
        const damageGain = Math.random() * 800 + 200; // 200-1000 damage pro update
        const newHeroDamage = Math.round(baseStats.heroDamage + damageGain);

        const stats = {
            souls: Math.max(0, newSouls),
            kills: newKills,
            deaths: newDeaths,
            assists: newAssists,
            heroDamage: newHeroDamage,
            netWorth: Math.round(newSouls + (newKills * 350) + (newAssists * 100)),
            matchTime: Math.round(minutesSinceStart * 60)
        };

        // Set demo start time if not set
        if (!playerData.demoStartTime) {
            playerData.demoStartTime = now;
        }
        
        return {
            steamId: playerData.steamId,
            lastUpdate: now,
            stats: stats,
            performance: this.calculatePerformanceMetrics(stats),
            isInMatch: true,
            isDemoMode: true
        };
    }

    getDefaultStats() {
        return {
            stats: {
                souls: 0,
                kills: 0,
                deaths: 0,
                assists: 0,
                heroDamage: 0,
                netWorth: 0,
                soulsPerMinute: 0
            },
            performance: {
                kda: 0,
                soulsPerMinute: 0,
                farmEfficiency: 0,
                combatScore: 0,
                damageScore: 0,
                overallScore: 0
            },
            isInMatch: false
        };
    }
}

// CLI Interface
if (require.main === module) {
    const bot = new DeadlockBot();
    
    // Graceful shutdown
    process.on('SIGINT', () => {
        console.log('\n🛑 Shutting down Deadlock Bot...');
        process.exit(0);
    });
    
    // Add some test players (optional)
    setTimeout(() => {
        // bot.addPlayerToTracking('76561198012345678', 'TestPlayer1');
        console.log('✅ Bot ready! Add players via API or web interface.');
    }, 2000);
}

module.exports = DeadlockBot;