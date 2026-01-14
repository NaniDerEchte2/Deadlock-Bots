"""Configuration constants for the Twitch cogs."""

# ============================
# 🛠️ CONFIG — EDIT HERE
# ============================
# ⚠️ Secrets (Client-ID/Secret) KOMMEN NICHT HIER REIN, sondern aus ENV (siehe unten)!
TWITCH_DASHBOARD_NOAUTH = True                     # ohne Token (nur lokal empfohlen)
TWITCH_DASHBOARD_HOST = "127.0.0.1"
TWITCH_DASHBOARD_PORT = 8765
TWITCH_RAID_REDIRECT_URI = "https://raid.earlysalty.com/twitch/raid/callback"

TWITCH_LANGUAGE = "de de-de de-at de-ch"          # Mehrere Varianten via Komma/Leerzeichen (z. B. "de de-at de-ch")
TWITCH_TARGET_GAME_NAME = "Deadlock"
TWITCH_BRAND_COLOR_HEX = 0x9146FF                     # offizielles Twitch-Lila für Embeds
TWITCH_REQUIRED_DISCORD_MARKER = ""                # optionaler Marker im Profiltext (zusätzl. zur Discord-URL)
TWITCH_DISCORD_REF_CODE = "DE-Deadlock-Discord"    # Referral-Parameter für Buttons/Links
TWITCH_BUTTON_LABEL = "Auf Twitch ansehen"         # Standardtext für den Go-Live-Button
TWITCH_VOD_BUTTON_LABEL = "VOD anschauen"          # Buttontext für die Offline/VOD-Ansicht

# Benachrichtigungskanäle
TWITCH_NOTIFY_CHANNEL_ID = 1304169815505637458     # Live-Postings (optional global)
TWITCH_ALERT_CHANNEL_ID  = 1374364800817303632     # Warnungen (30d Re-Check)
TWITCH_ALERT_MENTION     = ""                      # z. B. "<@123>" oder "<@&456>"

# Öffentlicher Statistik-Kanal (nur dort reagiert !twl)
TWITCH_STATS_CHANNEL_IDS  = [1428062025145385111, 1374364800817303632]

# Stats/Sampling: alle N Ticks (Tick=60s) in DB loggen
TWITCH_LOG_EVERY_N_TICKS = 5

# Zusätzliche Streams aus der Deadlock-Kategorie für Statistiken loggen (Maximalanzahl je Tick)
TWITCH_CATEGORY_SAMPLE_LIMIT = 400

# Invite-Refresh alle X Stunden
INVITES_REFRESH_INTERVAL_HOURS = 12

# Poll-Intervall (Sekunden)
POLL_INTERVAL_SECONDS = 60
