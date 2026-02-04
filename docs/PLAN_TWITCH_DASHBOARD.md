# Twitch Analytics Dashboard - Implementierungsplan

## Projektziel
Ein modernes, professionelles React TypeScript Dashboard für Twitch Streamer mit hochpräzisen Analytics, ähnlich StreamsCharts/TwitchTracker, aber besser aufbereitet und mit Deadlock-Kategorie-Vergleichen.

---

## Teil 1: Technische Architektur

### Frontend Stack
```
twitch-dashboard/
├── src/
│   ├── components/
│   │   ├── charts/           # Chart-Komponenten
│   │   ├── cards/            # KPI Cards, Score Gauges
│   │   ├── tables/           # Session Tables, Rankings
│   │   ├── heatmaps/         # Calendar Heatmap, Hour Analysis
│   │   └── layout/           # Header, Sidebar, Navigation
│   ├── pages/
│   │   ├── Overview.tsx      # Hauptübersicht
│   │   ├── StreamAnalysis.tsx
│   │   ├── ChatHealth.tsx
│   │   ├── Comparison.tsx
│   │   └── SessionDetail.tsx
│   ├── hooks/                # Custom React Hooks
│   ├── api/                  # API Client
│   ├── types/                # TypeScript Interfaces
│   └── utils/                # Helper Functions
├── package.json
├── vite.config.ts
├── tsconfig.json
└── tailwind.config.js
```

### Tech Stack
- **React 18** + **TypeScript**
- **Vite** als Build Tool
- **TailwindCSS** für Styling
- **Recharts** oder **Apache ECharts** für Charts
- **TanStack Query** für API State Management
- **Framer Motion** für Animationen

---

## Teil 2: Neue Backend-Endpunkte

### API Struktur
```
/twitch/api/v2/
├── /overview                    # Dashboard-Übersicht
├── /monthly-stats               # Monthly Breakdown
├── /weekly-stats                # Weekly Analysis
├── /hourly-heatmap              # Stunden-Heatmap
├── /calendar-heatmap            # Kalender-Heatmap
├── /chat-analytics              # Chat-Tiefenanalyse
├── /viewer-overlap              # Channel Overlap
├── /tag-analysis                # Tag Performance
├── /growth-metrics              # Wachstumsmetriken
├── /category-comparison         # Deadlock Kategorie Vergleich
├── /session/{id}                # Session Details
├── /streamer/{login}/summary    # Streamer Zusammenfassung
└── /rankings                    # Top Streamer Rankings
```

---

## Teil 3: Feature-Module

### 3.1 Monthly Stats Breakdown
**Datenquelle:** `twitch_stream_sessions` aggregiert nach Monat

| Metrik | Beschreibung | Berechnung |
|--------|--------------|------------|
| Total Hours Watched | Gesamte Watch-Time | `SUM(avg_viewers * duration_seconds / 3600)` |
| Total Airtime | Gesamte Stream-Zeit | `SUM(duration_seconds) / 3600` |
| Average Viewers | Durchschnittliche Zuschauer | `AVG(avg_viewers)` |
| Peak Viewers | Höchste Zuschauerzahl | `MAX(peak_viewers)` |
| Follower Growth | Follower-Zuwachs | `SUM(follower_delta)` |
| Unique Chatters | Einzigartige Chatter | Aggregiert aus `twitch_session_chatters` |

**Neue DB-Tabelle:**
```sql
CREATE TABLE twitch_monthly_stats (
    id INTEGER PRIMARY KEY,
    streamer_login TEXT NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    total_hours_watched REAL,
    total_airtime_hours REAL,
    avg_viewers REAL,
    peak_viewers INTEGER,
    follower_delta INTEGER,
    unique_chatters INTEGER,
    stream_count INTEGER,
    calculated_at TEXT,
    UNIQUE(streamer_login, year, month)
);
```

---

### 3.2 Days of Week Analysis
**Datenquelle:** `twitch_stream_sessions.started_at` (Wochentag extrahieren)

| Metrik | Pro Wochentag |
|--------|---------------|
| Active Days | Anzahl Streams an diesem Tag |
| Hours Streamed | Durchschnittliche Stunden |
| Avg Viewers | Durchschnittliche Viewer |
| Follower Gain | Durchschnittliche Follower |
| Best Time Slot | Beste Startzeit für diesen Tag |

**SQL Query:**
```sql
SELECT
    CASE strftime('%w', started_at)
        WHEN '0' THEN 'Sonntag'
        WHEN '1' THEN 'Montag'
        -- ...
    END as weekday,
    COUNT(*) as stream_count,
    AVG(duration_seconds / 3600.0) as avg_hours,
    AVG(avg_viewers) as avg_viewers,
    AVG(peak_viewers) as avg_peak,
    SUM(follower_delta) as total_followers
FROM twitch_stream_sessions
WHERE streamer_login = ? AND started_at >= ?
GROUP BY strftime('%w', started_at)
```

---

### 3.3 Calendar Heatmap (GitHub-Style)
**Visualisierung:** 365-Tage-Kalender mit Farbintensität basierend auf:
- Stream-Aktivität (gestreamt ja/nein)
- Hours Watched an diesem Tag
- Viewer-Performance

**Komponente:** `<CalendarHeatmap data={dailyData} metric="hoursWatched" />`

---

### 3.4 Hourly Analysis Heatmap
**Visualisierung:** 7×24 Grid (Wochentage × Stunden)
- Zeigt beste Streaming-Zeiten
- Farbcodiert nach durchschnittlichen Viewern

**Neue Aggregation:**
```sql
SELECT
    strftime('%w', started_at) as weekday,
    strftime('%H', started_at) as hour_utc,
    COUNT(*) as stream_count,
    AVG(avg_viewers) as avg_viewers,
    AVG(peak_viewers) as avg_peak
FROM twitch_stream_sessions
GROUP BY weekday, hour_utc
```

---

### 3.5 Chat Analytics Deep Dive
**Metriken:**
- Unique Chatters pro Stream
- First-Time vs Returning Chatters Ratio
- Chat Velocity (Messages per Minute)
- Top Chatter Leaderboard
- Chatter Loyalty Score (wie oft kommen sie zurück)

**Neue Berechnung - Chat Velocity:**
```sql
SELECT
    session_id,
    COUNT(*) as total_messages,
    (julianday(MAX(message_ts)) - julianday(MIN(message_ts))) * 24 * 60 as duration_minutes,
    COUNT(*) / NULLIF((julianday(MAX(message_ts)) - julianday(MIN(message_ts))) * 24 * 60, 0) as messages_per_minute
FROM twitch_chat_messages
GROUP BY session_id
```

---

### 3.6 Viewer Overlap Analysis ⭐ NEU
**Konzept:** Identifiziere Zuschauer, die bei mehreren Streamern chatten

**Neue DB-Tabelle:**
```sql
CREATE TABLE twitch_viewer_overlap (
    id INTEGER PRIMARY KEY,
    streamer_a TEXT NOT NULL,
    streamer_b TEXT NOT NULL,
    shared_chatters INTEGER,
    total_chatters_a INTEGER,
    total_chatters_b INTEGER,
    overlap_percentage REAL,
    calculated_at TEXT,
    UNIQUE(streamer_a, streamer_b)
);
```

**Berechnung:**
```sql
-- Finde gemeinsame Chatter zwischen zwei Streamern
SELECT
    s1.streamer_login as streamer_a,
    s2.streamer_login as streamer_b,
    COUNT(DISTINCT c1.chatter_login) as shared_chatters
FROM twitch_chatter_rollup c1
JOIN twitch_chatter_rollup c2 ON c1.chatter_login = c2.chatter_login
WHERE c1.streamer_login = ?
  AND c2.streamer_login != c1.streamer_login
GROUP BY c1.streamer_login, c2.streamer_login
ORDER BY shared_chatters DESC
```

**Dashboard-Anzeige:**
- Chord-Diagramm der Viewer-Überlappung
- "Ähnliche Kanäle" basierend auf Audience Overlap
- Raid-Empfehlungen basierend auf Overlap

---

### 3.7 Category Comparison (Deadlock) ⭐ WICHTIG
**Konzept:** Alle Metriken im Verhältnis zur Deadlock-Kategorie

**Datenbasis:** `twitch_stats_category` (bereits vorhanden!)

| Metrik | Streamer | Kategorie Ø | Verhältnis |
|--------|----------|-------------|------------|
| Avg Viewers | 150 | 89 | +68% 🟢 |
| Retention 10m | 55% | 48% | +15% 🟢 |
| Chat Health | 12/100 | 8/100 | +50% 🟢 |

**Visualisierung:**
- Radar-Chart: Streamer vs. Kategorie-Durchschnitt
- Percentile-Ranking: "Du bist besser als X% der Deadlock-Streamer"

---

### 3.8 Tag Performance Analysis ⭐ NEU
**Datenquelle:** `twitch_stream_sessions.tags` (JSON Array)

**Analyse:**
- Welche Tags korrelieren mit höheren Viewern?
- Tag-Kombinationen und ihre Performance
- Empfehlungen für optimale Tags

**Neue Tabelle:**
```sql
CREATE TABLE twitch_tag_performance (
    id INTEGER PRIMARY KEY,
    tag_name TEXT NOT NULL,
    usage_count INTEGER,
    avg_viewers REAL,
    avg_retention_10m REAL,
    avg_follower_gain REAL,
    calculated_at TEXT,
    UNIQUE(tag_name)
);
```

---

### 3.9 Estimated Audience Insights (Geschätzt) ⚠️
**WICHTIG:** Diese Daten sind **geschätzt**, nicht von Twitch API

#### 3.9.1 Sprach-/Regions-Schätzung
- Basierend auf: Stream-Sprache, Chat-Sprache-Detection, Aktive Stunden
- Anzeige: "Geschätzt basierend auf Chat-Aktivität"

#### 3.9.2 Interaktive vs. Passive Zuschauer
**Definition:**
- **Interaktive Zuschauer:** Haben mindestens 1x gechattet
- **Passive Zuschauer:** Avg Viewers - Unique Chatters

```typescript
interface AudienceBreakdown {
    interactive: number;      // = uniqueChatters
    passive: number;          // = avgViewers - uniqueChatters
    interactionRate: number;  // = interactive / avgViewers * 100
}
```

---

### 3.10 Growth Metrics
**Wachstums-KPIs:**
- Follower Growth Rate (% pro Woche/Monat)
- Viewer Growth Trend (Liniendiagramm)
- New Viewer Acquisition Rate
- Returning Viewer Rate

**Neue Berechnung:**
```sql
-- Wöchentliches Wachstum
WITH weekly AS (
    SELECT
        strftime('%Y-W%W', started_at) as week,
        AVG(avg_viewers) as avg_viewers,
        SUM(follower_delta) as followers
    FROM twitch_stream_sessions
    WHERE streamer_login = ?
    GROUP BY week
)
SELECT
    week,
    avg_viewers,
    followers,
    (avg_viewers - LAG(avg_viewers) OVER (ORDER BY week)) /
        NULLIF(LAG(avg_viewers) OVER (ORDER BY week), 0) * 100 as viewer_growth_pct
FROM weekly
```

---

## Teil 4: UI/UX Design

### 4.1 Dashboard Layout
```
┌─────────────────────────────────────────────────────────────────┐
│  [Logo] Twitch Analytics    [Streamer Dropdown]   [7d|30d|90d] │
├─────────────────────────────────────────────────────────────────┤
│ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐   │
│ │ Health  │ │ Viewers │ │ Growth  │ │ Chat    │ │ Rank    │   │
│ │  78/100 │ │   156   │ │  +12%   │ │  15/100 │ │ Top 8%  │   │
│ └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘   │
├─────────────────────────────────────────────────────────────────┤
│ [Overview] [Streams] [Chat] [Growth] [Compare] [Sessions]      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌────────────────────────────┐  ┌────────────────────────────┐│
│  │   Viewer Trend Chart       │  │   Retention Radar          ││
│  │   ~~~~~~~~~~~~~~~~~~~~~~~~ │  │      ●───●                 ││
│  │   ~~~~~~~~~~~~             │  │    / You \ Category        ││
│  └────────────────────────────┘  └────────────────────────────┘│
│                                                                 │
│  ┌────────────────────────────┐  ┌────────────────────────────┐│
│  │   Weekly Heatmap           │  │   Calendar Heatmap         ││
│  │   Mo [█][█][░][░][░]...    │  │   ▓▓░░▓▓▓░▓▓░░▓▓▓░        ││
│  │   Di [░][█][█][░][░]...    │  │   ░▓▓▓░░▓▓▓▓░░▓░░░        ││
│  └────────────────────────────┘  └────────────────────────────┘│
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Recent Sessions Table                                    │  │
│  │  Date     | Duration | Viewers | Peak | Retention | Chat │  │
│  │  ─────────┼──────────┼─────────┼──────┼───────────┼───── │  │
│  │  02.02.25 | 3h 45m   | 156     | 234  | 67%       | 18   │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 Farbschema
```css
:root {
    --bg-primary: #0b0e14;      /* Dunkel */
    --bg-card: #151a25;          /* Karten */
    --accent: #7c3aed;           /* Lila Akzent */
    --success: #4ade80;          /* Grün */
    --warning: #fbbf24;          /* Gelb */
    --danger: #f87171;           /* Rot */
    --text-primary: #e2e8f0;
    --text-secondary: #94a3b8;
}
```

---

## Teil 5: Implementierungs-Reihenfolge

### Phase 1: Foundation (2-3 Tage)
1. ✅ Vite + React + TypeScript Setup
2. ✅ TailwindCSS Konfiguration
3. ✅ API Client Setup
4. ✅ Basis-Layout (Header, Navigation, Cards)
5. ✅ Typ-Definitionen

### Phase 2: Core Features (3-4 Tage)
6. ✅ Overview Dashboard mit KPIs
7. ✅ Viewer Trend Chart
8. ✅ Session Table
9. ✅ Retention Metrics
10. ✅ Backend: `/api/v2/overview` Endpoint

### Phase 3: Advanced Analytics (4-5 Tage)
11. ✅ Monthly Stats Breakdown
12. ✅ Weekly Heatmap (7×24)
13. ✅ Calendar Heatmap (365 Tage)
14. ✅ Chat Analytics Deep Dive
15. ✅ Backend: Neue Aggregations-Queries

### Phase 4: Comparison & Insights (3-4 Tage)
16. ✅ Category Comparison (Deadlock)
17. ✅ Viewer Overlap Analysis
18. ✅ Tag Performance
19. ✅ Percentile Rankings
20. ✅ Backend: Overlap-Berechnung (Cronjob)

### Phase 5: Polish & Integration (2-3 Tage)
21. ✅ Animationen & Transitions
22. ✅ Loading States
23. ✅ Error Handling
24. ✅ Build & Deployment ins bestehende System
25. ✅ Dokumentation

---

## Teil 6: Backend-Erweiterungen

### Neue Dateien
```
cogs/twitch/
├── analytics_v2.py           # Neue API Endpunkte
├── aggregations.py           # Aggregations-Berechnungen
├── overlap_calculator.py     # Viewer Overlap Cronjob
└── dashboard_v2/
    └── dist/                 # Compiled React App
```

### Cronjobs (neue Tasks)
1. **Hourly:** Viewer-Overlap zwischen Streamern berechnen
2. **Daily:** Monthly Stats aggregieren
3. **Daily:** Tag Performance aktualisieren

---

## Teil 7: Bekannte Limitationen

### Nicht verfügbar über Twitch API:
- ❌ Audience Demographics (Alter, Geschlecht)
- ❌ Geographic Distribution (Land, Region)
- ❌ Traffic Sources (Suche, Browse, Raids)
- ❌ Revenue/Income Data
- ❌ Watch Time per Viewer

### Alternativen:
- ✅ **Viewer Overlap:** Über Chat-Daten berechenbar
- ✅ **Sprach-Schätzung:** Über Stream-Sprache + Chat-Detection
- ✅ **Interaktive vs. Passive:** Über Chatter-Ratio
- ✅ **Hours Watched:** `avg_viewers × stream_duration`

---

## Nächste Schritte

Nach Genehmigung dieses Plans:
1. React TypeScript Projekt initialisieren
2. Basis-Komponenten erstellen
3. Backend-Endpunkte implementieren
4. Schrittweise Features hinzufügen

**Geschätzter Gesamtaufwand:** 2-3 Wochen für vollständige Implementierung
