# Twitch Analytics Dashboard - Setup & Installation

## Voraussetzungen

- Python 3.11+
- SQLite3
- Discord.py 2.0+
- aiohttp
- Bestehende Twitch-Cog-Installation

## Installation

### 1. Dateien kopieren

Die folgenden Dateien sollten bereits vorhanden sein:

```
cogs/twitch/
├── analytics_backend.py        # Backend-Queries
├── dashboard/
│   ├── analytics.py            # Frontend-Dashboard
│   ├── analyse.py              # Legacy-Redirect
│   └── ...                     # Andere Dashboard-Module
├── dashboard_mixin.py           # Integration in Hauptcog
└── ANALYTICS_README.md         # Diese Dokumentation
```

### 2. Datenbank-Schema prüfen

Das Analytics-Dashboard nutzt vorhandene Tabellen. Stelle sicher, dass folgende Tabellen existieren:

```sql
-- Haupt-Sessions-Tabelle
CREATE TABLE IF NOT EXISTS twitch_stream_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    streamer_login TEXT NOT NULL,
    stream_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds INTEGER DEFAULT 0,
    start_viewers INTEGER DEFAULT 0,
    peak_viewers INTEGER DEFAULT 0,
    end_viewers INTEGER DEFAULT 0,
    avg_viewers REAL DEFAULT 0,
    samples INTEGER DEFAULT 0,
    retention_5m REAL,
    retention_10m REAL,
    retention_20m REAL,
    dropoff_pct REAL,
    dropoff_label TEXT,
    unique_chatters INTEGER DEFAULT 0,
    first_time_chatters INTEGER DEFAULT 0,
    returning_chatters INTEGER DEFAULT 0,
    followers_start INTEGER,
    followers_end INTEGER,
    follower_delta INTEGER,
    stream_title TEXT,
    notification_text TEXT,
    language TEXT,
    is_mature INTEGER DEFAULT 0,
    tags TEXT,
    notes TEXT
);

-- Viewer-Timeline
CREATE TABLE IF NOT EXISTS twitch_session_viewers (
    session_id INTEGER NOT NULL,
    ts_utc TEXT NOT NULL,
    minutes_from_start INTEGER,
    viewer_count INTEGER NOT NULL,
    PRIMARY KEY (session_id, ts_utc)
);

-- Chat-Tracking
CREATE TABLE IF NOT EXISTS twitch_session_chatters (
    session_id INTEGER NOT NULL,
    streamer_login TEXT NOT NULL,
    chatter_login TEXT NOT NULL,
    chatter_id TEXT,
    first_message_at TEXT NOT NULL,
    messages INTEGER DEFAULT 0,
    is_first_time_global INTEGER DEFAULT 0,
    PRIMARY KEY (session_id, chatter_login)
);

-- Chatter-Rollup
CREATE TABLE IF NOT EXISTS twitch_chatter_rollup (
    streamer_login TEXT NOT NULL,
    chatter_login TEXT NOT NULL,
    chatter_id TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    total_messages INTEGER DEFAULT 0,
    total_sessions INTEGER DEFAULT 0,
    PRIMARY KEY (streamer_login, chatter_login)
);

-- Stats-Samples
CREATE TABLE IF NOT EXISTS twitch_stats_tracked (
    ts_utc TEXT,
    streamer TEXT,
    viewer_count INTEGER,
    is_partner INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS twitch_stats_category (
    ts_utc TEXT,
    streamer TEXT,
    viewer_count INTEGER,
    is_partner INTEGER DEFAULT 0
);

-- Subscriptions (optional)
CREATE TABLE IF NOT EXISTS twitch_subscriptions_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    twitch_user_id TEXT NOT NULL,
    twitch_login TEXT,
    total INTEGER DEFAULT 0,
    tier1 INTEGER DEFAULT 0,
    tier2 INTEGER DEFAULT 0,
    tier3 INTEGER DEFAULT 0,
    points INTEGER DEFAULT 0,
    snapshot_at TEXT DEFAULT CURRENT_TIMESTAMP
);
```

Das Schema wird automatisch bei Cog-Start erstellt (siehe `storage.py`).

### 3. Cog-Integration prüfen

In `cog.py` sollte das Analytics-Mixin bereits eingebunden sein:

```python
class TwitchStreamCog(
    TwitchAnalyticsMixin,      # <-- Muss vorhanden sein
    TwitchRaidMixin,
    RaidCommandsMixin,
    TwitchDashboardMixin,
    TwitchLeaderboardMixin,
    TwitchAdminMixin,
    TwitchMonitoringMixin,
    TwitchBaseCog,
):
    pass
```

### 4. Dashboard-Mixin prüfen

In `dashboard_mixin.py` sollten die Analytics-Callbacks registriert sein:

```python
async def _start_dashboard(self):
    app = Dashboard.build_app(
        # ... andere Callbacks ...
        streamer_overview_cb=self._dashboard_streamer_overview,
        session_detail_cb=self._dashboard_session_detail,
        comparison_stats_cb=self._dashboard_comparison_stats,
        streamer_analytics_data_cb=self._dashboard_streamer_analytics_data,
        # ...
    )
```

## Konfiguration

### Environment-Variablen

```bash
# Dashboard-Zugriff (optional)
TWITCH_DASHBOARD_TOKEN=your_secret_token_here
TWITCH_PARTNER_TOKEN=your_partner_token_here  # Für Partner-Ansicht

# Dashboard-Server (falls embedded)
TWITCH_DASHBOARD_HOST=127.0.0.1
TWITCH_DASHBOARD_PORT=8765
TWITCH_DASHBOARD_NOAUTH=True  # Nur für lokale Tests!
```

### Partner-Token Setup

Für den Zugriff auf das Analytics-Dashboard gibt es zwei Token-Typen:

1. **Admin-Token** (`TWITCH_DASHBOARD_TOKEN`):
   - Voller Zugriff auf alle Features
   - Verwaltet Streamer, Verifizierungen, etc.

2. **Partner-Token** (`TWITCH_PARTNER_TOKEN`):
   - Nur Zugriff auf Analytics
   - Für Streamer-Partner gedacht
   - Kein Admin-Zugriff

**Setup:**
```bash
# Generiere sichere Tokens
ADMIN_TOKEN=$(openssl rand -hex 32)
PARTNER_TOKEN=$(openssl rand -hex 32)

# Füge zu .env hinzu
echo "TWITCH_DASHBOARD_TOKEN=$ADMIN_TOKEN" >> .env
echo "TWITCH_PARTNER_TOKEN=$PARTNER_TOKEN" >> .env
```

## Nutzung

### Analytics-Dashboard öffnen

**Lokaler Zugriff:**
```
http://localhost:8765/twitch/analytics
```

**Mit Token (Header):**
```bash
curl -H "X-Partner-Token: your_partner_token" \
     http://localhost:8765/twitch/analytics
```

**Mit Token (Query):**
```
http://localhost:8765/twitch/analytics?partner_token=your_partner_token
```

### Navigation

- **Hauptdashboard**: `/twitch/analytics`
- **Streamer-Detail**: `/twitch/streamer/{login}`
- **Session-Detail**: `/twitch/session/{id}`
- **Vergleich**: `/twitch/compare`
- **API**: `/twitch/api/analytics?streamer={login}&days={days}`

### Filter verwenden

**Streamer filtern:**
```
/twitch/analytics?streamer=myusername
```

**Zeitraum anpassen:**
```
/twitch/analytics?days=60
```

**Kombination:**
```
/twitch/analytics?streamer=myusername&days=90
```

## Datensammlung

### Automatische Sammlung

Das System sammelt automatisch Daten während laufender Streams:

1. **Session-Start**: Beim Go-Live wird eine Session erstellt
2. **Viewer-Tracking**: Alle 60s wird Viewer-Count gesampelt
3. **Chat-Tracking**: Messages werden gezählt (Text NICHT gespeichert!)
4. **Session-End**: Beim Offline-Gehen werden Metriken berechnet

### Retention-Berechnung

Retention wird automatisch berechnet basierend auf `twitch_session_viewers`:

```python
# Beispiel: 5-Min-Retention
start_viewers = viewer_count_at_minute_0
viewers_at_5min = viewer_count_at_minute_5
retention_5m = (viewers_at_5min / start_viewers) * 100
```

### Drop-Off-Detection

Drop-Offs werden erkannt via:
- Peak-Viewer vs. End-Viewer
- Größter einzelner Drop in Timeline
- Zeitpunkt des Drops (als Label)

### Chat-Tracking

Chat-Daten werden **datenschutzkonform** erfasst:
- ✅ Chatter-Login & ID
- ✅ Anzahl Messages
- ✅ First-Time-Flag
- ❌ **KEIN** Nachrichtentext
- ❌ **KEINE** IP-Adressen

## Performance-Optimierung

### Datenbank-Indizes

Wichtige Indizes sind bereits definiert:

```sql
CREATE INDEX idx_sessions_login ON twitch_stream_sessions(streamer_login, started_at);
CREATE INDEX idx_sessions_open ON twitch_stream_sessions(streamer_login) WHERE ended_at IS NULL;
CREATE INDEX idx_session_viewers ON twitch_session_viewers(session_id);
CREATE INDEX idx_chatters ON twitch_session_chatters(streamer_login, session_id);
```

### Query-Optimierung

Große Zeiträume (>90 Tage) können langsam sein. Nutze:
- Kleinere Zeitfenster (7-30 Tage)
- Streamer-Filter
- Limit auf relevante Metriken

### Caching

Für Production-Deployments empfohlen:
- Redis für API-Response-Caching
- Materialized Views für häufige Queries
- Background-Jobs für Pre-Aggregation

## Troubleshooting

### Dashboard lädt nicht

**Problem:** Weiße Seite oder 404

**Lösung:**
```bash
# Prüfe, ob Server läuft
curl http://localhost:8765/twitch/analytics

# Logs prüfen
tail -f logs/bot.log | grep -i "dashboard"

# Dashboard neu starten
# Im Bot: /reload twitch
```

### Keine Daten im Dashboard

**Problem:** "Keine Daten verfügbar"

**Lösung:**
```sql
-- Prüfe, ob Sessions existieren
SELECT COUNT(*) FROM twitch_stream_sessions;

-- Prüfe Datum der letzten Session
SELECT MAX(started_at) FROM twitch_stream_sessions;

-- Prüfe spezifischen Streamer
SELECT * FROM twitch_stream_sessions 
WHERE streamer_login = 'your_username' 
ORDER BY started_at DESC 
LIMIT 5;
```

### Metriken sind 0 oder NULL

**Problem:** Retention/Drop-Off zeigt 0%

**Lösung:**
```sql
-- Prüfe Viewer-Timeline
SELECT COUNT(*) FROM twitch_session_viewers WHERE session_id = 123;

-- Prüfe ob ended_at gesetzt ist
SELECT id, started_at, ended_at, retention_5m 
FROM twitch_stream_sessions 
WHERE ended_at IS NULL;
```

**Fix:** Sessions müssen korrekt geschlossen werden (ended_at != NULL).

### API gibt 500-Fehler

**Problem:** `/api/analytics` returned Internal Server Error

**Lösung:**
```python
# Aktiviere Debug-Logging
import logging
logging.getLogger("TwitchStreams.AnalyticsBackend").setLevel(logging.DEBUG)

# Prüfe Exception-Log
tail -f logs/bot.log | grep -i "analytics"
```

### Authentifizierung schlägt fehl

**Problem:** "Unauthorized" oder "Invalid token"

**Lösung:**
```bash
# Prüfe Token-Konfiguration
echo $TWITCH_PARTNER_TOKEN

# Teste mit Token
curl -H "X-Partner-Token: $TWITCH_PARTNER_TOKEN" \
     http://localhost:8765/twitch/analytics

# NOAUTH für lokale Tests
export TWITCH_DASHBOARD_NOAUTH=True
```

## Migration von Legacy-Stats

Falls du bereits Daten in alten Tabellen hast:

### Schema-Update

```sql
-- Füge fehlende Spalten hinzu (falls nötig)
ALTER TABLE twitch_stream_sessions ADD COLUMN follower_delta INTEGER;
ALTER TABLE twitch_stream_sessions ADD COLUMN retention_5m REAL;
ALTER TABLE twitch_stream_sessions ADD COLUMN retention_10m REAL;
```

### Daten-Backfill

Für historische Sessions Retention nachträglich berechnen:

```python
# In Python-Shell oder separatem Script
from cogs.twitch import storage

with storage.get_conn() as conn:
    sessions = conn.execute("""
        SELECT id, started_at 
        FROM twitch_stream_sessions 
        WHERE retention_5m IS NULL 
          AND ended_at IS NOT NULL
    """).fetchall()
    
    for session in sessions:
        session_id = session[0]
        
        # Berechne Retention aus Viewer-Timeline
        viewers = conn.execute("""
            SELECT minutes_from_start, viewer_count
            FROM twitch_session_viewers
            WHERE session_id = ?
            ORDER BY minutes_from_start
        """, [session_id]).fetchall()
        
        if len(viewers) < 2:
            continue
        
        start_count = viewers[0][1]
        count_5m = next((v[1] for v in viewers if v[0] >= 5), None)
        
        if count_5m and start_count > 0:
            retention_5m = (count_5m / start_count) * 100
            conn.execute("""
                UPDATE twitch_stream_sessions 
                SET retention_5m = ? 
                WHERE id = ?
            """, [retention_5m, session_id])
    
    conn.commit()
```

## Security Best Practices

### Token-Management

❌ **Nicht:**
```python
PARTNER_TOKEN = "my_secret_123"  # Hardcoded
```

✅ **Stattdessen:**
```python
PARTNER_TOKEN = os.getenv("TWITCH_PARTNER_TOKEN")
```

### Datenschutz

- ❌ Nachrichtentext speichern
- ❌ IP-Adressen loggen
- ❌ User-Daten weitergeben
- ✅ Nur aggregierte Statistiken
- ✅ Opt-Out für Tracking ermöglichen
- ✅ DSGVO-konforme Speicherung

### Rate-Limiting

Für Public-Deployments:

```python
from aiohttp import web
from aiohttp_ratelimit import RateLimiter, MemoryStorage

# In dashboard setup
app = web.Application()
ratelimiter = RateLimiter(storage=MemoryStorage(), rate="100/hour")
app.middlewares.append(ratelimiter)
```

## Production-Deployment

### Reverse-Proxy (nginx)

```nginx
server {
    listen 80;
    server_name analytics.yourdomain.com;
    
    location /twitch {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        
        # Partner-Token via Header
        proxy_set_header X-Partner-Token $http_x_partner_token;
    }
}
```

### HTTPS (Let's Encrypt)

```bash
# Certbot installieren
sudo apt install certbot python3-certbot-nginx

# Zertifikat anfordern
sudo certbot --nginx -d analytics.yourdomain.com
```

### Monitoring

```bash
# Systemd Service
[Unit]
Description=Twitch Analytics Bot
After=network.target

[Service]
Type=simple
User=bot
WorkingDirectory=/opt/bot
ExecStart=/opt/bot/venv/bin/python main.py
Restart=always

[Install]
WantedBy=multi-user.target
```

### Backup

```bash
# Automatisches SQLite-Backup
0 2 * * * sqlite3 /opt/bot/data/service.db ".backup '/backup/service_$(date +\%Y\%m\%d).db'"
```

## Support & Weiterführende Links

- **Haupt-Dokumentation**: `ANALYTICS_README.md`
- **API-Referenz**: `dashboard/analytics.py` Docstrings
- **Backend-Queries**: `analytics_backend.py`
- **GitHub Issues**: [Link zu Repository]
- **Discord**: [Link zu Support-Channel]

---

**Version:** 1.0.0  
**Maintainer:** Twitch Analytics Team  
**Letzte Aktualisierung:** Januar 2026
