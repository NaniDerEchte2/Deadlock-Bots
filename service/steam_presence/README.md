# Deadlock Steam Rich Presence Service

Dieser Node.js-Service loggt sich mit `steam-user` ein und schreibt die Rich-Presence-Daten
für überwachte Accounts in die gemeinsame SQLite-Datenbank (`steam_rich_presence`).
Dadurch kann der Python-basierte Discord-Bot präzisere Match- und Lobby-Zustände erkennen.

## Voraussetzungen

- Node.js 18+
- Ein eigener Steam-Account für den Bot, der mit den zu überwachenden Accounts befreundet ist
- Zugriff auf dieselbe SQLite-DB, die auch der Discord-Bot nutzt (`DEADLOCK_DB_PATH` oder `DEADLOCK_DB_DIR`)

## Konfiguration (Environment)

| Variable | Beschreibung |
| --- | --- |
| `STEAM_BOT_USERNAME` / `STEAM_LOGIN` | Steam-Loginname des Bot-Accounts |
| `STEAM_BOT_PASSWORD` | Passwort, falls kein Login-Key verwendet wird |
| `STEAM_LOGIN_KEY` | Optionaler gespeicherter Login-Key (alternativ zu Passwort) |
| `STEAM_LOGIN_KEY_PATH` | Dateipfad zum Speichern/Laden des Login-Keys |
| `STEAM_TOTP_SECRET` | Shared-Secret für 2FA (Steam Guard) |
| `STEAM_GUARD_CODE` | Einmaliger Steam-Guard-Code (Fallback, falls kein TOTP) |
| `DEADLOCK_DB_PATH` / `DEADLOCK_DB_DIR` | Pfad bzw. Verzeichnis zur SQLite-Datenbank |
| `DEADLOCK_APP_ID` | Steam-AppID von Deadlock (Default: `1422450`) |
| `RP_WATCH_REFRESH_SEC` | Intervall zum Nachladen der Watchlist (Sekunden) |
| `RP_POLL_INTERVAL_MS` | Intervall zum Abfragen der Rich Presence (Millisekunden) |
| `LOG_LEVEL` | `error`, `warn`, `info` (Default) oder `debug` |

## Starten

```bash
npm install
npm run start
```

Die Watchlist ergibt sich automatisch aus allen verknüpften Steam-IDs (`steam_links`) plus
optional manuellen Einträgen in `steam_presence_watchlist`.
