# Twitch Analytics Dashboard - Dokumentation

## Überblick

Das Twitch Analytics Dashboard ist ein umfassendes Tool zur Analyse und Optimierung von Twitch-Streams. Es bietet detaillierte Einblicke in Retention, Discovery, Chat-Gesundheit und Benchmarking.

## Hauptfunktionen

### 1. **Retention & Drop-Off Analyse**
- **5/10/20-Minuten-Retention**: Wie viele Zuschauer bleiben nach X Minuten?
- **Drop-Off-Tracking**: Wo verlieren Sie die meisten Zuschauer?
- **Actionable Insights**: Konkrete Empfehlungen zur Verbesserung

**Interpretation:**
- Retention < 50%: Einstieg überarbeiten, Hook früher setzen
- Retention > 70%: Content fesselt von Anfang an
- Drop-Off > 30%: Wiederkehrende Zeitpunkte identifizieren

### 2. **Discovery Funnel**
- **Peak Viewer Tracking**: Maximale Reichweite pro Stream
- **Follower-Conversion**: Neue Follower pro Session/Stunde
- **Returning Viewer**: 7-Tage und 30-Tage Wiederkehrrate

**Interpretation:**
- Follower < 5% der Peak-Viewer: Follow-Calls-to-Action verstärken
- Follower > 15% der Peak-Viewer: Exzellente Conversion
- Hohe Returning-Rate: Starke Community-Bindung

### 3. **Chat-Gesundheit**
- **Chat/100 Viewer**: Engagement-Metrik
- **First-Time vs. Returning**: Community-Wachstum vs. Stammchat
- **Chat Health Score**: Zusammengesetzter Score (0-100)

**Interpretation:**
- Chat/100 < 5: Mehr Interaktion nötig, Fragen stellen
- Chat/100 > 15: Sehr engagierte Community
- Hoher First-Time-Anteil: Gutes Discovery, aber Retention beobachten

### 4. **Benchmarking**
- **Kategorie-Durchschnitt**: Vergleich mit Deadlock-Streamern
- **Partner-Durchschnitt**: Vergleich mit tracked Partnern
- **Top-Streamer-Ranking**: Positionierung im Feld

**Interpretation:**
- Unter Kategorie-Durchschnitt: Potenzial für Optimierung
- Über Partner-Durchschnitt: Überdurchschnittliche Performance
- Quantile-Vergleich: Wo stehen Sie im Vergleich?

## Dashboard-Struktur

### **Übersichtsseite** (`/twitch/analytics`)
Zeigt aggregierte KPIs für den gewählten Zeitraum:
- Retention (5-Min als primäre Metrik)
- Discovery (Avg Peak Viewer)
- Follower Growth (Total neue Follower)
- Chat Engagement (Chat/100 Viewer)

Mit Trend-Indikatoren (↑/↓) im Vergleich zur Vorperiode.

### **Streamer-Detail** (`/twitch/streamer/{login}`)
Tiefenanalyse für einzelnen Streamer:
- 30-Tage Zusammenfassung
- Letzte 20 Sessions mit Metriken
- Trend-Charts (Viewer, Retention)
- Link zu Session-Details

### **Session-Detail** (`/twitch/session/{id}`)
Mikro-Analyse einer einzelnen Session:
- Viewer-Timeline (Retention-Kurve)
- Retention-Metriken (5/10/20 Min)
- Chat-Engagement (Top Chatters)
- Drop-Off-Analyse mit Zeitstempel

### **Vergleichsansicht** (`/twitch/compare`)
Marktanalyse und Benchmarking:
- Kategorie-Durchschnitt vs. Tracked-Partner
- Top 10 Performer
- Quantile-Verteilung (Q25/Q50/Q75)

## Metrik-Definitionen

### Retention-Metriken

**5-Minuten-Retention**
```sql
retention_5m = (viewer_count_at_5min / start_viewers) * 100
```
Prozentsatz der Viewer, die nach 5 Minuten noch schauen.

**10/20-Minuten-Retention**
Analog zur 5-Min-Retention, aber bei 10/20 Minuten gemessen.

**Drop-Off %**
```sql
dropoff_pct = ((peak_viewers - end_viewers) / peak_viewers) * 100
```
Prozentsatz der Peak-Viewer, die bis zum Ende verloren gehen.

### Discovery-Metriken

**Avg Peak Viewer**
```sql
avg_peak_viewers = SUM(peak_viewers) / COUNT(sessions)
```
Durchschnittliche maximale Zuschauerzahl über alle Sessions.

**Follower / Session**
```sql
followers_per_session = total_follower_delta / session_count
```
Durchschnittlich gewonnene Follower pro Stream.

**Follower / Stunde**
```sql
followers_per_hour = total_follower_delta / total_stream_hours
```
Follower-Effizienz bezogen auf Streamzeit.

**Returning Viewer (7d)**
```sql
SELECT COUNT(DISTINCT chatter)
FROM chatter_rollup
WHERE last_seen >= NOW() - 7 days
  AND first_seen < NOW() - 7 days
```
Zuschauer, die vor >7 Tagen das erste Mal da waren und in den letzten 7 Tagen zurückkamen.

### Chat-Metriken

**Unique Chat / 100 Viewer**
```sql
chat_per_100 = (unique_chatters / avg_viewers) * 100
```
Normalisiertes Chat-Engagement unabhängig von Viewer-Zahl.

**First-Time Share**
```sql
first_time_share = first_time_chatters / unique_chatters
```
Anteil neuer Chatter (Discovery-Indikator).

**Returning Share**
```sql
returning_share = returning_chatters / unique_chatters
```
Anteil wiederkehrender Chatter (Community-Indikator).

**Chat Health Score**
```
score = 0.4 * unique_norm
      + 0.2 * first_norm
      + 0.2 * returning_norm
      + 0.1 * peaks_norm
      + 0.1 * trend_norm
```
Zusammengesetzter Score (0-100) aus verschiedenen Chat-Dimensionen.

## Actionable Insights

Das Dashboard generiert automatisch Empfehlungen basierend auf erkannten Mustern:

### **Retention-Insights**

**Niedrige 5-Min-Retention (<50%)**
> "Deine 5-Minuten-Retention liegt bei 42%. Empfehlung: Verbessere deinen Stream-Einstieg. Setze einen stärkeren Hook in den ersten 2-3 Minuten. Vermeide lange Intros oder Setup-Phasen."

**Starke Retention (>70%)**
> "Sehr gut! Deine 5-Minuten-Retention liegt bei 78%. Dein Content fesselt die Zuschauer von Anfang an."

**Hoher Drop-Off (>30%)**
> "Durchschnittlich verlierst du 35% der Peak-Viewer während des Streams. Prüfe, ob es wiederkehrende Zeitpunkte gibt (z.B. nach 30-45 Min) und strukturiere deinen Content neu."

### **Discovery-Insights**

**Niedrige Follower-Conversion (<5%)**
> "Bei durchschnittlich 120 Peak-Viewern hast du nur 4 neue Follower gewonnen. Empfehlung: Erinnere Zuschauer regelmäßig daran zu folgen. Setze Follow-Goals und belohne neue Follower."

**Exzellente Conversion (>15%)**
> "Stark! Du gewinnst 25 Follower bei 150 durchschnittlichen Peak-Viewern. Dein Content motiviert neue Zuschauer, dir zu folgen."

### **Chat-Insights**

**Niedrige Chat-Aktivität (<5)**
> "Nur 3.2 Unique Chatters pro 100 Viewer. Empfehlung: Stelle mehr Fragen an den Chat, starte Umfragen, reagiere aktiv auf Nachrichten. Baue Interaktions-Momente in deinen Stream ein."

**Sehr engagierte Community (>15)**
> "Wow! 18.5 Unique Chatters pro 100 Viewer zeigen eine sehr aktive Community. Deine Zuschauer fühlen sich eingebunden."

### **Trend-Insights**

**Retention steigt**
> "Deine Retention verbessert sich in den letzten 7 Tagen. Was auch immer du änderst - mach weiter so!"

**Retention sinkt**
> "Deine Retention nimmt in den letzten 7 Tagen ab. Prüfe, ob du Content-Änderungen vorgenommen hast."

## Filter & Zeiträume

Das Dashboard unterstützt flexible Filterung:

**Zeiträume**
- 7 Tage: Kurz-Trends erkennen
- 30 Tage: Standard-Periode (empfohlen)
- 60 Tage: Mittel-Trends
- 90 Tage: Lang-Trends und Saison-Effekte

**Streamer-Filter**
- Alle Tracked: Aggregierte Ansicht aller Partner
- Einzelner Streamer: Fokussierte Analyse

## API-Endpunkte

### **GET /twitch/analytics**
Haupt-Dashboard mit Filteroptionen.

**Query Parameter:**
- `streamer` (optional): Twitch-Login
- `days` (optional): 7, 30, 60, oder 90 (default: 30)

### **GET /twitch/api/analytics**
JSON-API für dynamisches Laden.

**Response:**
```json
{
  "metrics": {
    "retention_5m": 0.65,
    "avg_peak_viewers": 120.5,
    "total_followers_delta": 45,
    "unique_chatters_per_100": 8.2,
    "retention_5m_trend": 5.3,
    ...
  },
  "retention_timeline": [...],
  "discovery_timeline": [...],
  "chat_timeline": [...],
  "insights": [
    {
      "type": "success",
      "title": "Starke Retention",
      "description": "..."
    }
  ]
}
```

### **GET /twitch/streamer/{login}**
Detailansicht für einzelnen Streamer.

### **GET /twitch/session/{id}**
Session-Detailanalyse.

### **GET /twitch/compare**
Vergleichsstatistiken.

## Datenbasis

Das Dashboard nutzt folgende Tabellen:

### **twitch_stream_sessions**
Haupttabelle für Session-Metriken:
- Retention-Werte (5/10/20 Min)
- Drop-Off-Daten
- Viewer-Zahlen (Start/Peak/End/Avg)
- Chat-Metriken
- Follower-Deltas

### **twitch_session_viewers**
Zeitreihe für Viewer-Count:
- Minutengenaue Aufzeichnung
- Basis für Retention-Kurven

### **twitch_session_chatters**
Chat-Engagement pro Session:
- Unique Chatters
- First-Time vs. Returning
- Message-Count

### **twitch_chatter_rollup**
Globale Chatter-Historie:
- First/Last Seen
- Total Messages/Sessions
- Basis für Returning-Viewer-Berechnung

### **twitch_stats_tracked / twitch_stats_category**
Benchmarking-Daten:
- Viewer-Samples für Partner/Kategorie
- Basis für Vergleiche

### **twitch_subscriptions_snapshot**
Sub-Zahlen (alle 6h):
- Total Subs
- Tier-Breakdown
- Sub-Points

## Best Practices

### **Datenqualität**
- Mindestens 5 Sessions für aussagekräftige Metriken
- 30-Tage-Fenster als Standard (Balance zwischen Trends und Sample-Size)
- Regelmäßige Datenerfassung ohne Lücken

### **Interpretation**
- Trends wichtiger als Absolut-Werte
- Vergleich mit eigener Historie, nicht nur mit Durchschnitt
- Kontext beachten (z.B. neue Kategorie, Tageszeit)

### **Optimierung**
- EIN Metric zur Zeit fokussieren
- A/B-Tests mit mindestens 2 Wochen Laufzeit
- Änderungen dokumentieren für spätere Analyse

## Technische Implementation

### **Backend**
- `analytics_backend.py`: Query-Engine für alle Metriken
- Asynchrone Verarbeitung via `async/await`
- SQLite-basiert mit optimierten Queries
- Caching für Performance

### **Frontend**
- `dashboard/analytics.py`: HTML-Rendering
- Chart.js für Visualisierungen
- Vanilla JavaScript (kein Build-Step)
- Responsive Design

### **Integration**
- Callbacks über `dashboard_mixin.py`
- Einbindung in Main-Cog via Mixins
- Authentifizierung via Token oder Partner-Token

## Troubleshooting

### **"Keine Daten verfügbar"**
- Prüfen: Sind Sessions in der DB?
- Zeitraum anpassen (mehr Tage)
- Streamer-Filter überprüfen

### **"Trends zeigen 0%"**
- Vorperiode hat keine Daten
- Zu kurzer Zeitraum gewählt
- Neue Streams ohne Historie

### **"Charts laden nicht"**
- Browser-Console prüfen
- JavaScript-Fehler?
- CDN (Chart.js) erreichbar?

## Roadmap

Geplante Features:

- [ ] Content-Performance-Tracking (Title/Tags-Analyse)
- [ ] Raid-Impact-Analyse
- [ ] Shared Audience Detection
- [ ] Predictive Analytics (ML-basiert)
- [ ] Export-Funktionen (CSV/PDF)
- [ ] Custom Alerts (Schwellwerte)
- [ ] Mobile App

## Support

Bei Fragen oder Problemen:
- Discord: [Link zu Support-Channel]
- GitHub Issues: [Repository-Link]
- Dokumentation: Dieses Dokument

---

**Version:** 1.0.0  
**Letzte Aktualisierung:** Januar 2026  
**Maintainer:** Twitch Analytics Team
