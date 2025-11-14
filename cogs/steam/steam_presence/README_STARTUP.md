# Steam Presence Bridge - Startup Guide

## Problem Diagnosis

Das häufigste Problem beim Steam Invite System ist, dass die **Steam Presence Bridge nicht läuft**.

### Symptome:
- Fehler: "Game Coordinator hat die Einladung abgelehnt"
- Fehler: "Timeout waiting for Deadlock GC" 
- Tasks bleiben im `RUNNING` Status hängen

## Lösung: Steam Bridge starten

### 1. Terminal öffnen und ins Verzeichnis wechseln:
```bash
cd "Documents/Deadlock/cogs/steam/steam_presence"
```

### 2. Dependencies prüfen:
```bash
npm list
```

Sollte zeigen:
- `steamid@2.2.0`
- `sqlite3@5.1.7`
- `steam-user@4.29.3`

### 3. Steam Bridge starten:
```bash
npm start
```

### 4. Erfolgsmeldung prüfen:
```
{"time":"2025-11-05T18:51:41.105Z","level":"info","msg":"Steam login successful","steam_id64":"76561198780408374"}
{"time":"2025-11-05T18:51:42.892Z","level":"info","msg":"Deadlock app launched – GC session starting"}
```

### 5. Bridge im Hintergrund laufen lassen:
Die Bridge muss **dauerhaft laufen**, damit Invites funktionieren.

## Checking if Bridge is Running

### PowerShell:
```powershell
Get-Process node | Where-Object {$_.Path -like "*steam_presence*"}
```

### Check Database Tasks:
```bash
sqlite3 "../../service/deadlock.sqlite3" "SELECT id, type, status, created_at FROM steam_tasks ORDER BY id DESC LIMIT 5"
```

## Troubleshooting

### Bridge startet nicht:
1. Node.js Version prüfen: `node --version` (sollte ≥16)
2. Dependencies installieren: `npm install`
3. Steam credentials prüfen in `.env`

### "Deadlock GC Timeout":
1. Bridge neu starten
2. Steam Client neu starten
3. 10-15 Minuten warten (Steam GC überlastet)

### Bot hat keine Invite-Berechtigung:
- Steam Bot-Account muss selbst Deadlock-Zugang haben
- Bot-Account darf nicht Limited User sein

## Monitoring

Die Bridge loggt alle wichtigen Events. Bei Problemen die Logs prüfen:
- Startup-Messages
- Steam login status
- Deadlock GC connection
- Task processing errors

**WICHTIG**: Die Bridge kommuniziert über SQLite Database, NICHT über HTTP!