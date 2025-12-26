# Installation des Raid-Bots

## 1. Dependency installieren

```bash
pip install twitchio
```

## 2. Environment-Variablen setzen

```bash
# Twitch Bot Account OAuth-Token
export TWITCH_BOT_TOKEN="oauth:your_bot_account_token_here"

# Optional: Redirect URI für OAuth (Standard: http://127.0.0.1:8765/twitch/raid/callback)
export TWITCH_RAID_REDIRECT_URI="http://your-domain:8765/twitch/raid/callback"
```

## 3. Twitch Bot Token erstellen

1. Erstelle einen separaten Twitch-Account für den Bot (z.B. "DeadlockRaidBot")
2. Gehe zu: https://twitchtokengenerator.com/
3. Wähle "Bot Chat Token"
4. Autorisiere den Account
5. Kopiere das Token (sollte mit `oauth:` beginnen)
6. Setze es als `TWITCH_BOT_TOKEN`

## 4. Twitch Application konfigurieren

1. Gehe zu: https://dev.twitch.tv/console/apps
2. Öffne deine Application (oder erstelle eine neue)
3. Füge unter "OAuth Redirect URLs" hinzu:
   ```
   http://127.0.0.1:8765/twitch/raid/callback
   ```
   (oder deine Custom-Domain)
4. Speichern

## 5. Bot starten

Der Bot startet automatisch mit dem Discord-Bot. Keine weiteren Schritte erforderlich.

## Verifikation

Nach dem Start solltest du folgende Log-Meldungen sehen:

```
INFO - Raid-Bot initialisiert (redirect_uri: http://127.0.0.1:8765/twitch/raid/callback)
INFO - Twitch Chat Bot gestartet
INFO - Connected to channels: partner1, partner2, partner3, ...
```

## Troubleshooting

Falls der Chat Bot nicht startet:
- Prüfe, ob `TWITCH_BOT_TOKEN` korrekt gesetzt ist
- Prüfe, ob das Token mit `oauth:` beginnt
- Checke die Logs auf Fehler

Falls twitchio nicht gefunden wird:
```bash
pip install --upgrade twitchio
```

## Weitere Dokumentation

Siehe `RAID_BOT_README.md` für ausführliche Dokumentation.
