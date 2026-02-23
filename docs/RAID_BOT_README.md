# 🎯 Twitch Raid Bot - Dokumentation

## Übersicht

Der Raid Bot ist eine Erweiterung des Twitch Stream Cog, die automatisch Raids zwischen Streamer-Partnern durchführt. Wenn ein Partner offline geht, raidet der Bot automatisch einen anderen Online-Partner - bevorzugt denjenigen mit der kürzesten Stream-Zeit.

## Features

- ✅ **Automatische Raids**: Wenn ein Partner offline geht, wird automatisch ein anderer Online-Partner geraidet
- 🎯 **Intelligente Auswahl**: Der Partner mit der kürzesten Stream-Zeit wird bevorzugt
- 💾 **Metadaten-Speicherung**: Alle Raids werden mit detaillierten Metadaten gespeichert
- 🔐 **OAuth-Autorisierung**: Sichere Autorisierung über Twitch OAuth
- 💬 **Twitch-Chat-Commands**: Steuerung direkt über Twitch-Chat
- 📊 **Dashboard-Integration**: Übersicht und Statistiken im Web-Dashboard
- 📈 **Raid-History**: Vollständige Historie aller durchgeführten Raids

## Einrichtung

### 1. Umgebungsvariablen

Setze folgende Environment-Variablen:

```bash
# Erforderlich (bereits für Stream-Monitoring vorhanden)
TWITCH_CLIENT_ID=your_client_id
TWITCH_CLIENT_SECRET=your_client_secret

# Neu für Raid-Bot
TWITCH_BOT_TOKEN=oauth:your_bot_account_token  # OAuth-Token für Bot-Account
TWITCH_RAID_REDIRECT_URI=http://your-domain:8765/twitch/raid/callback  # Optional, Standard: http://127.0.0.1:8765/twitch/raid/callback
```

#### Twitch Bot Token erstellen (mit Ankündigungs-Scope):

1. Erstelle einen separaten Twitch-Account für den Bot (z.B. "YourBotName")
2. Gehe zu: https://twitchtokengenerator.com/ (oder nutze dein eigenes OAuth-Setup)
3. Wähle **Custom Scope Token** und aktiviere mindestens:
   - `chat:read`, `chat:edit`, `channel:bot`
   - `user:read:chat`, `user:write:chat`
   - `moderator:manage:chat_messages`
   - **`moderator:manage:announcements`** (wichtig für /helix/chat/announcements)
4. Autorisiere den Account (der Bot muss Moderator im Ziel-Channel sein)
5. Kopiere Access- **und Refresh-Token** und setze sie als `TWITCH_BOT_TOKEN` und optional `TWITCH_BOT_REFRESH_TOKEN`

### 2. Dependencies installieren

```bash
pip install twitchio
```

### 3. Twitch Application einrichten

Füge den Redirect-URI zu deiner Twitch-Application hinzu:

1. Gehe zu: https://dev.twitch.tv/console/apps
2. Öffne deine Application
3. Füge unter "OAuth Redirect URLs" hinzu: `http://your-domain:8765/twitch/raid/callback`
4. Speichern

## Verwendung für Streamer

### Über Twitch-Chat-Commands

Streamer können den Raid-Bot direkt über ihren Twitch-Chat steuern:

#### `!raid_enable` oder `!raidbot`
Aktiviert den Auto-Raid-Bot für den Kanal.

**Erste Verwendung:**
- Bot sendet einen Autorisierungs-Link
- Streamer muss auf Twitch autorisieren
- Danach ist Auto-Raid aktiviert

**Bereits autorisiert:**
- Aktiviert Auto-Raid sofort

**Beispiel:**
```
MeinStreamer: !raid_enable
Bot: @MeinStreamer Um den Auto-Raid-Bot zu nutzen, musst du ihn zuerst autorisieren. Klicke hier: https://id.twitch.tv/oauth2/authorize?...
```

#### `!raid_disable` oder `!raidbot_off`
Deaktiviert den Auto-Raid-Bot.

**Beispiel:**
```
MeinStreamer: !raid_disable
Bot: @MeinStreamer 🛑 Auto-Raid deaktiviert. Du kannst es jederzeit mit !raid_enable wieder aktivieren.
```

#### `!raid_status` oder `!raidbot_status`
Zeigt den aktuellen Status des Raid-Bots an.

**Beispiel:**
```
MeinStreamer: !raid_status
Bot: @MeinStreamer Raid-Bot Status: ✅ Aktiv. Auto-Raids sind aktiviert. | Statistik: 15 Raids (14 erfolgreich) | Letzter Raid ✅: PartnerXYZ (42 Viewer) am 2025-01-15 20:30
```

#### `!raid_history` oder `!raidbot_history`
Zeigt die letzten 3 Raids an.

**Beispiel:**
```
MeinStreamer: !raid_history
Bot: @MeinStreamer Letzte Raids: ✅ PartnerA (42V, 2025-01-15) | ✅ PartnerB (38V, 2025-01-14) | ✅ PartnerC (55V, 2025-01-13)
```

### Berechtigungen

- Nur der **Broadcaster** oder **Moderatoren** können den Raid-Bot aktivieren/deaktivieren
- Status und History kann jeder im Chat sehen

## Admin-Funktionen

### Dashboard

Unter `http://your-domain:8765/twitch/raid/` stehen folgende Funktionen zur Verfügung:

#### Raid-History ansehen
```
GET /twitch/raid/history?token=YOUR_TOKEN&limit=50
```

Zeigt die letzten Raids mit allen Metadaten an.

#### Streamer autorisieren (Admin)
```
GET /twitch/raid/auth?token=YOUR_TOKEN&login=streamer_name
```

Generiert einen Autorisierungs-Link für einen Streamer.

## Datenbank-Schema

### `twitch_raid_auth`
Speichert OAuth-Tokens für autorisierte Streamer.

```sql
- twitch_user_id (PK)
- twitch_login
- access_token
- refresh_token
- token_expires_at
- scopes
- authorized_at
- last_refreshed_at
- raid_enabled (1 = aktiv, 0 = deaktiviert)
```

### `twitch_raid_history`
Speichert Metadaten aller durchgeführten Raids.

```sql
- id (AI)
- from_broadcaster_id
- from_broadcaster_login
- to_broadcaster_id
- to_broadcaster_login
- viewer_count
- stream_duration_sec
- reason (z.B. "auto_raid_on_offline")
- executed_at
- success (1 = erfolgreich, 0 = fehlgeschlagen)
- error_message
- target_stream_started_at
- candidates_count (Anzahl verfügbarer Online-Partner)
```

### `twitch_streamers` (erweitert)
Neues Feld:
- `raid_bot_enabled` (1 = aktiviert, 0 = deaktiviert)

## Raid-Logik

### Wann wird geraidet?

1. Ein Partner geht offline (war live in Deadlock-Kategorie)
2. Partner hat Auto-Raid aktiviert (`raid_bot_enabled = 1`)
3. Partner hat den Bot autorisiert (Token in `twitch_raid_auth`)
4. Mindestens ein anderer Partner ist gerade online

### Partner-Auswahl (Fairness-System)

Der Bot wählt den Online-Partner nach **Fairness** aus:

**Kriterien (in dieser Reihenfolge):**
1. **Wer hat weniger Raids bekommen?** (Hauptkriterium)
2. **Wer ist kürzer live?** (Tiebreaker bei Gleichstand)

```python
# Sortierung nach Fairness
candidate_stats.sort(key=lambda x: (x["received_raids"], x["started_at"]))
target = candidate_stats[0]  # Fairster Kandidat
```

**Warum Fairness statt nur Stream-Zeit?**
- Gleichmäßige Verteilung der Raid-Unterstützung
- Jeder Partner bekommt gleich viel Hilfe
- Verhindert, dass einzelne Partner bevorzugt werden
- Fördert echte gegenseitige Unterstützung

**Logging:**
- Gesendete Raids werden geloggt
- Empfangene Raids werden gezählt
- Statistiken fließen in die Auswahl ein

### Beispiel

```
Partner-Status:
- PartnerA: Offline (geht gerade offline, 150 Viewer)
- PartnerB: Online (seit 1 Stunde, 5 Raids bekommen)
- PartnerC: Online (seit 3 Stunden, 2 Raids bekommen) ← AUSGEWÄHLT
- PartnerD: Online (seit 30 Minuten, 2 Raids bekommen)

PartnerA raidet PartnerC mit 150 Viewern
(PartnerC hat am wenigsten Raids bekommen)
```

**Bei Gleichstand:**
```
Partner-Status:
- PartnerA: Offline (geht gerade offline, 150 Viewer)
- PartnerB: Online (seit 2 Stunden, 3 Raids bekommen)
- PartnerC: Online (seit 30 Minuten, 3 Raids bekommen) ← AUSGEWÄHLT
- PartnerD: Online (seit 1 Stunde, 5 Raids bekommen)

PartnerA raidet PartnerC mit 150 Viewern
(Beide haben 3 Raids, aber PartnerC ist kürzer live)
```

## Troubleshooting

### Bot joined nicht die Channels

**Problem:** Twitch Chat Bot verbindet sich nicht mit den Partner-Channels.

**Lösung:**
- Prüfe, ob `TWITCH_BOT_TOKEN` korrekt gesetzt ist
- Checke die Logs: `log.info("Twitch Chat Bot gestartet")`
- Der Bot joint automatisch alle Partner-Channels alle 60 Minuten

### OAuth-Autorisierung schlägt fehl

**Problem:** Redirect nach OAuth-Flow funktioniert nicht.

**Lösung:**
- Prüfe, ob `TWITCH_RAID_REDIRECT_URI` korrekt ist
- Stelle sicher, dass die URI in der Twitch-App eingetragen ist
- Checke, ob der Dashboard-Server läuft

### Raids werden nicht ausgeführt

**Problem:** Auto-Raids funktionieren nicht.

**Lösung:**
1. Prüfe Status mit `!raid_status` im Chat
2. Checke, ob `raid_enabled = 1` in `twitch_raid_auth`
3. Schaue in die Logs: `log.info("Auto-Raid triggered...")`
4. Prüfe, ob andere Partner online sind

### Token ist abgelaufen

**Problem:** "No valid token" Fehler.

**Lösung:**
- Der Bot erneuert Tokens automatisch
- Falls Refresh fehlschlägt: Streamer muss neu autorisieren mit `!raid_enable`

## Logs

Wichtige Log-Meldungen:

```
INFO - Raid-Bot initialisiert (redirect_uri: ...)
INFO - Twitch Chat Bot gestartet
INFO - Auto-Raid triggered für streamer (offline): 5 Online-Partner gefunden
INFO - ✅ Auto-Raid erfolgreich: streamerA -> streamerB
ERROR - Fehler beim Auto-Raid für streamerX
```

## Sicherheit

- **OAuth-Tokens werden verschlüsselt gespeichert** in der Datenbank
- **Nur autorisierte Streamer** können geraidet werden
- **Scopes sind auf Minimum beschränkt**: `channel:manage:raids`
- **Token-Refresh** erfolgt automatisch
- **Rate-Limiting** beim Channel-Join (0.5s Delay)

## Performance

- **Polling-Intervall**: 60 Sekunden (Standard)
- **Token-Cache**: Tokens werden gecacht und nur bei Bedarf refreshed
- **DB-Queries**: Optimiert mit Indizes auf `twitch_user_id` und `executed_at`
- **Async-Operations**: Alle API-Calls sind asynchron

## Statistiken

Raid-Statistiken können abgerufen werden:

```python
with get_conn() as conn:
    stats = conn.execute("""
        SELECT
            COUNT(*) as total_raids,
            SUM(success) as successful_raids,
            AVG(viewer_count) as avg_viewers,
            AVG(stream_duration_sec) as avg_stream_duration
        FROM twitch_raid_history
        WHERE from_broadcaster_id = ?
    """, (user_id,)).fetchone()
```

## Support

Bei Fragen oder Problemen:
1. Checke die Logs
2. Prüfe die Datenbank
3. Kontaktiere einen Admin
