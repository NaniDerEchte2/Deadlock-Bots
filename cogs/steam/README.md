# Steam Deadlock Presence Cog

Dieses Verzeichnis enthält Hilfsmodule rund um Steam-Integrationen des Bots. Der zuvor
verwendete Node.js-Rich-Presence-Dienst wurde entfernt; stattdessen übernimmt der
Cog `deadlock_presence.py` direkt das Einloggen in Steam und die Übermittlung der
Deadlock-Spielerliste an Discord.

## Deadlock Presence

Der Cog `deadlock_presence.py` stellt dieselbe Funktionalität wie das ehemalige
Standalone-Skript `deadlock_presence_bot.py` bereit. Er verbindet sich mit Steam,
beobachtet die Freundesliste und aktualisiert einen Embed in einem konfigurierbaren
Discord-Channel.

Konfiguration über Environment-Variablen:

| Variable | Beschreibung |
| --- | --- |
| `STEAM_USERNAME` / `STEAM_PASSWORD` | Zugangsdaten des Steam-Bot-Accounts |
| `STEAM_TOTP_SECRET` | Optionales Shared-Secret für 2FA/TOTP |
| `DEADLOCK_PRESENCE_CHANNEL_ID` | Channel-ID für den Status-Embed (Default `1374364800817303632`) |
| `DEADLOCK_PRESENCE_POLL_SECONDS` | Optionales Fallback-Polling-Intervall (Default `20`) |

Falls keine gültigen Steam-Zugangsdaten gesetzt sind, deaktiviert sich der Cog beim
Laden selbstständig.

## Schnelllink-Helfer

`schnelllink.py` stellt weiterhin Komponenten bereit, um personalisierte
Freundschafts-Links zu erzeugen (z. B. für Buttons in anderen Cogs).
