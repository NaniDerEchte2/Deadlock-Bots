# Deadlock Steam Rich Presence Service

Dieser Node.js-Service loggt sich mit `steam-user` ein und schreibt die Rich-Presence-Daten
für überwachte Accounts in die gemeinsame SQLite-Datenbank (`steam_rich_presence`).
Dadurch kann der Python-basierte Discord-Bot präzisere Match- und Lobby-Zustände erkennen.

## Voraussetzungen

- Node.js 18+
- Ein eigener Steam-Account für den Bot, der mit den zu überwachenden Accounts befreundet ist
- Zugriff auf dieselbe SQLite-DB, die auch der Discord-Bot nutzt (`DEADLOCK_DB_PATH` oder `DEADLOCK_DB_DIR`)

## Konfiguration (Environment)

Du kannst die Variablen entweder klassisch in deiner Shell/als Dienst setzen oder über
eine `.env`-Datei im Verzeichnis `service/steam_presence` hinterlegen – sie wird beim
Start automatisch eingelesen.

| Variable | Beschreibung |
| --- | --- |
| `STEAM_BOT_USERNAME` / `STEAM_LOGIN` | Steam-Loginname des Bot-Accounts |
| `STEAM_BOT_PASSWORD` | Passwort, falls kein Login-Key verwendet wird |
| `STEAM_LOGIN_KEY` | Optionaler gespeicherter Login-Key (alternativ zu Passwort) |
| `STEAM_LOGIN_KEY_PATH` | Dateipfad zum Speichern/Laden des Login-Keys |
| `STEAM_TOTP_SECRET` | Shared-Secret für 2FA (Steam Guard), erzeugt beim Einrichten der Mobile App |
| `STEAM_GUARD_CODE` | Einmaliger Steam-Guard-Code (Fallback, falls kein TOTP). Wird beim Start verbraucht |
| `DEADLOCK_DB_PATH` / `DEADLOCK_DB_DIR` | Pfad bzw. Verzeichnis zur SQLite-Datenbank |
| `DEADLOCK_APP_ID` | Steam-AppID von Deadlock (Default: `1422450`) |
| `RP_WATCH_REFRESH_SEC` | Intervall zum Nachladen der Watchlist (Sekunden) |
| `RP_POLL_INTERVAL_MS` | Intervall zum Abfragen der Rich Presence (Millisekunden) |
| `LOG_LEVEL` | `error`, `warn`, `info` (Default) oder `debug` |
| `AUTO_START_STEAM_SERVICE` | Vom Master-Bot gesteuert: `1` (Default) startet den Service automatisch |
| `STEAM_SERVICE_AUTO_INSTALL` | `1` (Default) führt vor dem Start einmalig `npm install` aus |
| `STEAM_SERVICE_CMD` | Optionaler alternativer Startbefehl (Default: `npm run start`) |
| `STEAM_SERVICE_INSTALL_CMD` | Optionaler Installationsbefehl (Default: `npm install`) |

### Login-Key automatisch verwalten

Setze `STEAM_LOGIN_KEY_PATH` auf eine beschreibbare Datei (z. B. `C:\\Bots\\steam_login.key`).
Beim ersten erfolgreichen Login legt der Dienst dort den von Steam ausgestellten `loginKey`
ab und lädt ihn bei zukünftigen Starts automatisch wieder. So genügt es, einmalig Passwort
und Guard-Code einzutragen; danach kannst du beide Variablen entfernen und startest ohne
erneute 2FA-Eingabe.


### Steam Guard / 2FA

Es gibt keine interaktive Abfrage – der Dienst benötigt den Steam-Guard-Code bereits beim
Start. Am bequemsten ist es, das `STEAM_TOTP_SECRET` aus der Steam-Mobile-App zu
übernehmen; daraus generiert der Bot bei jedem Login automatisch einen gültigen Code.
Alternativ kannst du einmalig `STEAM_GUARD_CODE` setzen (z. B. in der `.env`). Nach der
Verwendung wird er verworfen und du musst beim nächsten Start einen neuen Code hinterlegen.

## Starten

```bash
npm install
npm run start
```

Die Watchlist ergibt sich automatisch aus allen verknüpften Steam-IDs (`steam_links`) plus
optional manuellen Einträgen in `steam_presence_watchlist`.

> **Hinweis:** Wenn du den Python-Master-Bot verwendest, kannst du das manuelle Starten
> überspringen. Der Bot bringt einen Supervisor mit (`SteamPresenceServiceManager`), der den
> Node-Prozess beim Hochfahren automatisch startet (sofern `AUTO_START_STEAM_SERVICE` nicht
> deaktiviert ist) und bei Abstürzen neu startet. Über Discord stehen dir zusätzliche
> Befehle zur Verfügung:
>
> - `!master steam` – zeigt Status, Pfad und letzte Start-/Stoppzeiten an
> - `!master steam start|stop|restart` – manuelles Steuern des Dienstes
> - `!master steam tail [limit] [stdout|stderr]` – letze Logzeilen ausgeben

## Betrieb mit den bestehenden Discord-Bots

- **Gleiche Datenbank:** Stelle sicher, dass sowohl der Python-Discord-Bot als auch dieser
  Service dieselbe SQLite-Datei verwenden (`DEADLOCK_DB_PATH`).
- **Feature ist aktiv:** Im Python-Bot ist keine zusätzliche Konfiguration notwendig –
  solange `ENABLE_RICH_PRESENCE` nicht auf `0` gesetzt ist, fließen die Daten automatisch
  in die Match-Heuristik ein.
- **Gemeinsamer Start:** Lass den Node-Service parallel zum Discord-Bot laufen (z. B. über
  `pm2`, `systemd`, den Windows-Taskplaner oder einen zweiten `start`-Befehl neben dem
  bestehenden `start_master_bot.bat`). Wichtig ist nur, dass beide Prozesse auf dieselbe
  Datenbank zugreifen.

Eingehende Freundschaftsanfragen an den Bot-Account werden automatisch angenommen, damit
neue Spieler sofort überwacht werden können.
