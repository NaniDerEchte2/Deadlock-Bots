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


Spiel-Status via Rich Presence: Der Steam-Bot (Node-Service steam_presence) ist mit dem Steam-Account eingeloggt und überwacht alle Freunde. Alle verknüpften Steam-IDs werden in eine Watchlist aufgenommen
. Über friendRichPresence-Events erhält der Bot laufend Status-Informationen der Freunde in Deadlock. Diese Presence-Daten werden in der SQLite-Tabelle steam_rich_presence gespeichert
 – inklusive Status-Text, Anzeige (steam_display), der Match/Lobby-Gruppe (steam_player_group samt steam_player_group_size) und ggf. Verbindungsdetails. Dadurch hat das System eine stets aktuelle Datenbasis, welche Nutzer gerade Deadlock spielen und ob sie sich in einer Lobby oder in einem Match befinden.
Freund hinzufügen: Sobald ein Nutzer seinen Steam-Account verknüpft, wird dessen SteamID in der Datenbank (steam_links Tabelle) gespeichert und eine Freundschaftsanfrage vom Steam-Bot an den Nutzer eingereiht

. Dies geschieht über die Funktion queue_friend_request(steam_id), die einen Eintrag in der steam_friend_requests Queue-Tabelle anlegt. Der separate Steam-Bot-Prozess liest diese Queue und sendet dann die tatsächliche Freundschaftsanfrage an den Steam-User.

Freundschaftsanfragen annehmen: Der Steam-Bot sollte eingehende Freundschaftsanfragen ebenfalls automatisch akzeptieren. In der aktuellen Codebasis fehlt jedoch ein expliziter Handler dafür – es wird nur auf das Entfernen einer Freundschaft reagiert (Relationship None)

. Hier müsste zukünftige Logik ergänzt werden, z.B. im friendRelationship-Event den Fall EFriendRelationship.RequestRecipient abfangen und via client.addFriend(...) die Anfrage annehmen. Momentan erfolgt dies nicht automatisch, weshalb der Bot eingehende Anfragen noch nicht berücksichtigt (dies erklärt, warum der Bot eventuell nicht alle Spieler-Status sieht, falls der Nutzer den Bot nicht selbst geaddet hat).