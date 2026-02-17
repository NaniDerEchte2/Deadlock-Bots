# Social Media Clip Publisher - Setup Guide

## 🎯 Feature Overview

Automatischer Upload von Twitch-Clips auf:
- 🎵 **TikTok**
- 📺 **YouTube Shorts**
- 📷 **Instagram Reels**

---

## 📊 Dashboard-Zugriff

**URL:** `http://localhost:4343/social-media`

**Authentication:** Melde dich mit deinem Twitch-Partner-Account an

- Klicke auf "Login" im Dashboard
- Wirst zu Twitch OAuth weitergeleitet
- Nach erfolgreicher Anmeldung: Automatischer Zugriff (nur für verifizierte Partner)

### Features:
- ✅ Clip-Übersicht (letzte 50 Clips)
- ✅ Upload-Queue Management
- ✅ Analytics (Views, Likes, Shares per Platform)
- ✅ Batch-Upload (alle Plattformen auf einmal)

### Lokaler Zugriff (Development):
- Localhost (`127.0.0.1`) hat automatisch vollen Zugriff ohne Login
- Für Production: Nur verifizierte Partner können zugreifen

---

## 🔧 Setup: Plattform-APIs

### 1. TikTok API

**Status:** ⚠️ Restricted Access (Business-Account nötig)

**Schritte:**
1. Gehe zu: https://developers.tiktok.com/
2. Erstelle eine **TikTok for Developers** App
3. Beantrage **Content Posting API** Zugriff
4. Hole API Credentials:
   - `client_key`
   - `client_secret`
5. Setze in `.env`:
   ```
   TIKTOK_CLIENT_KEY=your_key
   TIKTOK_CLIENT_SECRET=your_secret
   ```

**Alternative:** TikTok erlaubt aktuell nur Business-Accounts. Für Creator-Accounts nutze:
- Manuelle Upload via TikTok Desktop App
- Oder Services wie Hootsuite/Buffer (kostenpflichtig)

---

### 2. YouTube Shorts API

**Status:** ✅ Verfügbar (Google Account nötig)

**Schritte:**
1. Gehe zu: https://console.cloud.google.com/
2. Erstelle neues Projekt: `twitch-clips-uploader`
3. Aktiviere **YouTube Data API v3**
4. Erstelle OAuth 2.0 Credentials:
   - Application Type: **Web Application**
   - Authorized Redirect URI: `http://localhost:4343/youtube/callback`
5. Download `client_secret.json`
6. Setze in `.env`:
   ```
   YOUTUBE_CLIENT_ID=your_id.apps.googleusercontent.com
   YOUTUBE_CLIENT_SECRET=your_secret
   ```

**First-Time Auth:**
```bash
python -m cogs.twitch.social_media.youtube_auth
```
→ Browser öffnet sich, authorize mit deinem YouTube-Account
→ Token wird gespeichert in `data/youtube_token.json`

---

### 3. Instagram Reels API

**Status:** ⚠️ Business/Creator Account nötig

**Schritte:**
1. Gehe zu: https://developers.facebook.com/
2. Erstelle Facebook App
3. Verknüpfe mit **Instagram Business/Creator Account**
4. Aktiviere **Instagram Graph API**
5. Hole Access Token:
   - Graph API Explorer: https://developers.facebook.com/tools/explorer/
   - Permissions: `instagram_content_publish`, `pages_show_list`
6. Setze in `.env`:
   ```
   INSTAGRAM_ACCESS_TOKEN=your_token
   INSTAGRAM_BUSINESS_ACCOUNT_ID=your_account_id
   ```

**Token verlängern** (standardmäßig 60 Tage gültig):
```bash
python -m cogs.twitch.social_media.instagram_refresh_token
```

---

## 🚀 Usage

### 1. Clip erstellen (via Bot Command):

```
!clip [title]
```
→ Clip wird automatisch in DB registriert

### 2. Clips anzeigen (Dashboard):

```
http://localhost:4343/social-media
```

### 3. Upload queuen:

**Via Dashboard:**
- Klicke "📤 Upload All" auf einem Clip

**Via Python:**
```python
from cogs.twitch.social_media import ClipManager

manager = ClipManager(twitch_api)
manager.queue_upload(
    clip_db_id=123,
    platform="tiktok",  # oder "youtube", "instagram", "all"
    title="Mein krasser Clip",
    description="Check das aus!",
    hashtags=["deadlock", "gaming", "twitch"],
    priority=10,  # Higher = Upload zuerst
)
```

### 4. Upload-Worker starten:

```bash
python -m cogs.twitch.social_media.upload_worker
```

→ Verarbeitet Upload-Queue automatisch
→ Läuft im Hintergrund

---

## 📈 Analytics

**Auto-Sync alle 6 Stunden:**
```bash
python -m cogs.twitch.social_media.analytics_sync
```

**Tracked Metrics:**
- 👁 Views
- ❤️ Likes
- 💬 Comments
- 🔄 Shares
- 📊 Completion Rate
- 🔗 External Clicks (zu Twitch)

**Dashboard:**
```
http://localhost:4343/social-media/analytics
```

---

## 🎬 Video-Konvertierung

**Automatisch:** 16:9 (Twitch) → 9:16 (TikTok/Reels)

**Requirements:**
```bash
# FFmpeg installieren (für Video-Konvertierung)
# Windows:
choco install ffmpeg

# Linux:
sudo apt install ffmpeg

# macOS:
brew install ffmpeg
```

**Optional: Auto-Captions** (sehr empfohlen für Engagement!)
```bash
pip install whisper
```
→ Generiert automatisch Untertitel via OpenAI Whisper

---

## 🔐 Best Practices

### Hashtags:
- **Max 30 Hashtags** (TikTok/Instagram)
- Mix aus **Broad** (#gaming) und **Niche** (#deadlock)
- **1-2 Trending** Hashtags pro Tag

### Timing:
- **TikTok:** 14-16 Uhr & 20-22 Uhr (CEST)
- **YouTube:** 12-15 Uhr (Lunch Break)
- **Instagram:** 18-21 Uhr (After Work)

### Content:
- **Unter 60 Sekunden** (optimal: 15-30 Sek)
- **Hook in ersten 3 Sekunden**
- **Captions sind Pflicht** (90% schauen ohne Ton!)

---

## 🐛 Troubleshooting

### "Upload failed: 401 Unauthorized"
→ Token abgelaufen, re-authentifiziere

### "Video zu lang (>60 Sek)"
→ TikTok/Reels erlauben max 60 Sek (ab 10k Follower: 3 Min)

### "FFmpeg not found"
→ FFmpeg installieren (siehe oben)

---

## 🎯 Roadmap

- [ ] **TikTok Upload** (warte auf API-Zugriff)
- [ ] **YouTube Upload** (implementiert)
- [ ] **Instagram Upload** (implementiert)
- [ ] **Auto-Captions** via Whisper
- [ ] **Scheduled Posts** (beste Posting-Times)
- [ ] **A/B-Testing** (mehrere Titel/Thumbnails)
- [ ] **Trending-Hashtag-Finder**
- [ ] **Cross-Platform-Analytics** (ROI per Platform)

---

## 📞 Support

Bei Fragen/Problemen:
1. Check Logs: `logs/twitch_bot.log`
2. Check Dashboard: Upload-Queue Status
3. Discord: #bot-support
