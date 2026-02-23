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

## 3. Twitch Bot Token erstellen (inkl. Announcement-Scope)

1. Erstelle einen separaten Twitch-Account für den Bot (z.B. "DeadlockRaidBot")
2. Gehe zu: https://twitchtokengenerator.com/
3. Wähle **Custom Scope Token** und aktiviere:
   - `chat:read`, `chat:edit`, `channel:bot`
   - `user:read:chat`, `user:write:chat`
   - `moderator:manage:chat_messages`
   - **`moderator:manage:announcements`** (erforderlich für hervorgehobene Ankündigungen)
4. Autorisiere den Account (der Bot muss Moderator im Channel sein)
5. Kopiere Access- **und Refresh-Token** (Refresh, falls angeboten)
6. Setze sie als `TWITCH_BOT_TOKEN` und optional `TWITCH_BOT_REFRESH_TOKEN` (Token beginnt meist mit `oauth:`)

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
