# 🎮 Deadlock Performance Bot

Ein **echtzeitfähiger Performance-Tracker** für Deadlock mit professionellem Node.js Backend und Live-Overlay.

## ✨ Features

- **🔴 Live Performance Tracking** - Souls/Min, KDA, Damage in Echtzeit
- **📊 Erweiterte Metriken** - Farm Efficiency, Combat Score, Overall Performance  
- **🏆 Leaderboard System** - Vergleiche mit anderen getrackten Spielern
- **⚡ WebSocket Updates** - Keine CORS-Probleme, echte Live-Daten
- **🎯 Multi-Player Support** - Tracke mehrere Spieler gleichzeitig
- **📈 Performance Charts** - Visualisierung deiner Entwicklung
- **🔄 Auto-Reconnect** - Stabile Verbindung auch bei Netzwerkproblemen

## 🚀 Installation

### Voraussetzungen
- **Node.js 16+** installiert
- **Steam ID** (64-bit Format)
- **API Key** von deadlock-api.com

### Setup
```bash
# 1. Projekt erstellen
mkdir deadlock-bot
cd deadlock-bot

# 2. Files erstellen (bot.js, package.json, overlay.html)
# Kopiere die Artifacts in entsprechende Dateien

# 3. Dependencies installieren
npm install

# 4. Bot starten
npm start
```

### Dateien erstellen:
1. **`bot.js`** - Der Node.js Backend Code
2. **`package.json`** - Dependencies Configuration  
3. **`public/overlay.html`** - Das Frontend Overlay
4. **`.env`** (optional) - Environment Variablen

## ⚙️ Konfiguration

### API Key Setup
Ersetze in `bot.js` zeile 15:
```javascript
this.apiKey = 'DEIN-API-KEY-HIER';
```

### Steam ID finden
1. Gehe zu https://steamdb.info/calculator/
2. Füge deinen Steam Profil-Link ein
3. Kopiere die **64-bit Steam ID**

## 🎯 Nutzung

### 1. Bot starten
```bash
npm start
```
Console zeigt: `🚀 Bot läuft auf http://localhost:3000`

### 2. Overlay öffnen
- Öffne Browser: `http://localhost:3000`
- **Steam ID eingeben** und "Player Tracken" klicken
- **"API Test"** für Verbindungstest
- **"Start Live"** für Echtzeit-Tracking

### 3. API Endpoints

#### Player Stats abrufen
```
GET /api/player/{steamId}/stats
```

#### Aktuelles Match
```
GET /api/player/{steamId}/current-match  
```

#### Leaderboard
```
GET /api/leaderboard
```

#### Player hinzufügen
```
POST /api/track-player
{
  "steamId": "76561198012345678",
  "username": "PlayerName"
}
```

## 🔧 Features im Detail

### Performance Metriken
- **Souls per Minute** - Hauptmetrik für Farm-Effizienz
- **KDA Ratio** - Kills + Assists / Deaths
- **Hero Damage** - Schaden an feindlichen Helden
- **Farm Efficiency** - % basierend auf 400 SPM Benchmark
- **Combat Score** - KDA-basierte Kampfleistung  
- **Overall Score** - Kombinierte Performance

### Makro Events (geplant)
- Minotaur Spawn Alerts
- Urn Verfügbarkeit
- Objective Timings
- Team Fight Detection

### Live Updates
- **WebSocket Connection** für Echtzeit-Updates
- **Automatische Reconnection** bei Verbindungsabbruch
- **Event-basierte Architektur** für saubere Datenverteilung

## 🛠️ Erweiterte Konfiguration

### Environment Variables (.env)
```env
API_KEY=dein-deadlock-api-key
PORT=3000
UPDATE_INTERVAL=10000
LOG_LEVEL=info
```

### Docker Support (optional)
```dockerfile
FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
EXPOSE 3000
CMD ["npm", "start"]
```

## 🐛 Troubleshooting

### API Connection Failed
- ✅ API Key korrekt?
- ✅ Steam ID richtig (17 Ziffern)?
- ✅ Internet-Verbindung stabil?
- ✅ deadlock-api.com erreichbar?

### WebSocket Verbindung verloren
- Bot automatisch neu starten
- Browser-Cache leeren
- Firewall-Einstellungen prüfen

### Keine Live-Daten
- Player muss **aktiv im Match** sein
- API-Rate-Limits beachten
- Match-Detection kann 1-2 Minuten dauern

## 📈 Performance Benchmarks

| Metrik | Schlecht | Okay | Gut | Exzellent |
|--------|----------|------|-----|-----------|
| Souls/Min | <200 | 200-300 | 300-400 | >400 |
| KDA | <1.0 | 1.0-1.5 | 1.5-2.5 | >2.5 |
| Farm Efficiency | <50% | 50-70% | 70-85% | >85% |

## 🔮 Roadmap

### Version 1.1
- [ ] Discord Bot Integration
- [ ] Match History Analysis
- [ ] Team Performance Comparison
- [ ] Custom Alerts/Notifications

### Version 1.2  
- [ ] Overwolf App Version
- [ ] Advanced Statistics Dashboard
- [ ] Tournament Bracket Management
- [ ] AI-powered Performance Insights

### Version 2.0
- [ ] Machine Learning Predictions
- [ ] Professional Coaching Features
- [ ] Replay Analysis Integration
- [ ] Multi-Game Support

## 🤝 Contributing

Contributions welcome! 

1. Fork das Repository
2. Feature Branch erstellen: `git checkout -b feature/amazing-feature`
3. Commit Changes: `git commit -m 'Add amazing feature'`
4. Push to Branch: `git push origin feature/amazing-feature`
5. Pull Request öffnen

## 📄 License

MIT License - siehe LICENSE file für Details.

## 🙏 Credits

- **deadlock-api.com** - Für die fantastische API
- **Deadlock Community** - Für Feedback und Support
- **Valve** - Für das großartige Spiel

## 📞 Support

- GitHub Issues für Bugs
- Discord Community für Fragen
- Email: deadlock-bot@example.com

---

**Happy Tracking! 🎮🚀**

*Möge dein SPM hoch und deine Deaths niedrig sein!*