# Deutsche Deadlock Community Bot - Projekt Kontext

> Ein umfassender Discord-Bot für die Deutsche Deadlock Community mit Community-Management, Steam-Integration, Voice-Features und Content-Management.

---

## Vision

Ein vollständig autonomer Discord-Bot, der die Deutsche Deadlock Community verwaltet, Steam-Integration bietet, Voice-Channels organisiert und Content-Features wie Clip-Einreichung und Build-Publishing unterstützt.

---

## Aktueller Status

✅ **Produktiv in Betrieb** - Bot läuft auf dem Discord-Server der Deutschen Deadlock Community

### Aktive Features
- ✅ Onboarding & Regelbestätigung
- ✅ Steam-Verknüpfung (OAuth + OpenID)
- ✅ TempVoice-Lanes (Automatische Voice-Channel-Verwaltung)
- ✅ Team Balancer (Faire Match-Erstellung)
- ✅ Voice-Activity-Tracking (Leaderboards & Statistiken)
- ✅ Match-Coaching System
- ✅ Build-Publishing (Automatisches Spiegeln von Top-Spieler-Builds)
- ✅ Clip-Einreichung (Wöchentliche Clip-Sammlung)
- ✅ Feedback Hub (Anonymes Community-Feedback)
- ✅ Twitch-Statistiken (Streamer-Leaderboards)

---

## Tech Stack

### Bot-Framework
- **Sprache:** Python 3.11+
- **Discord Library:** discord.py
- **Database:** SQLite (service/db.py)

### Externe Services
- **Steam-Integration:** Node.js Standalone-Prozess (cogs/steam/)
- **Twitch-Integration:** Twitch API (cogs/twitch/)
- **Protobuf:** Deadlock Game Coordinator Kommunikation

### Deployment
- **Hosting:** [Wo läuft der Bot? Server/Cloud/Lokal?]
- **Logging:** Strukturierte Logs in logs/ (master_bot.log, deadlock_gc_messages.log, etc.)
- **Monitoring:** Autonome Prozesse mit Auto-Recovery

### Development Tools
- **Environment Management:** .venv (Python Virtual Environment)
- **Configuration:** .env Dateien (.env.performance, etc.)
- **Version Control:** Git + GitHub

---

## Projekt-Struktur

```
Deadlock/
├── main_bot.py              # Haupt-Bot Entry Point
├── bot_core/                # Core Bot Logic
│   ├── bootstrap.py         # Runtime Initialisierung
│   └── [weitere Core-Module]
├── cogs/                    # Discord Cogs (Feature-Module)
│   ├── ai_connector.py      # AI-Integration
│   ├── ai_onboarding.py     # KI-gestütztes Onboarding
│   ├── build_publisher.py   # Build-Publishing-Worker
│   ├── claim_system.py      # Claim-System
│   ├── clip_submission.py   # Clip-Einreichung
│   ├── dashboard_cog.py     # Dashboard
│   ├── deadlock_team_balancer.py  # Team-Balancing
│   ├── deadlock_voice_status.py   # Voice-Status
│   ├── dl_coaching.py       # Coaching-System
│   ├── feedback_hub.py      # Feedback-Hub
│   ├── lfg.py               # Looking for Group
│   ├── rank_voice_manager.py # Rank-basierte Voice-Channels
│   ├── rules_channel.py     # Regelkanal
│   ├── security_guard.py    # Sicherheit
│   ├── server_faq.py        # FAQ
│   ├── steam/               # Steam-Integration (Node.js Bridge)
│   ├── steam_link_voice_nudge.py  # Steam-Verknüpfungs-Reminder
│   ├── steam_verified_role.py     # Steam-Verifizierungs-Rollen
│   ├── tempvoice/           # TempVoice-System
│   ├── twitch/              # Twitch-Integration
│   ├── user_activity_analyzer.py  # User-Aktivitäts-Analyse
│   ├── user_retention.py    # User-Retention
│   ├── voice_activity_tracker.py  # Voice-Activity-Tracking
│   └── welcome_dm/          # Welcome-DM-System
├── service/                 # Business Logic
│   ├── config.py            # Konfiguration
│   ├── db.py                # Datenbank-Layer
│   └── standalone_manager.py # Standalone-Prozess-Management
├── data/                    # Datenbank & Exports
├── logs/                    # Log-Dateien
│   ├── master_bot.log
│   ├── deadlock_gc_messages.log
│   └── deadlock_voice_status.log
├── docs/                    # Dokumentation
│   ├── build-publishing/    # Build-Publishing-Docs
│   └── COMMUNITY_FEATURES.md
├── features/                # Feature Specs (AI Agent System)
├── .claude/                 # AI Agent Definitionen
├── standalone/              # Standalone-Prozesse (Node.js)
└── .venv/                   # Python Virtual Environment
```

---

## Cog-System (Feature-Module)

### Community-Management
- **ai_onboarding.py** - KI-gestütztes Onboarding neuer Mitglieder
- **rules_channel.py** - Regelbestätigung
- **welcome_dm/** - Welcome-DM-System
- **security_guard.py** - Sicherheit & Moderation
- **user_retention.py** - User-Retention-Strategien

### Steam-Integration
- **steam/** - Node.js Bridge für Steam Game Coordinator
- **steam_link_voice_nudge.py** - Reminder zur Steam-Verknüpfung
- **steam_verified_role.py** - Automatische Rollen-Vergabe nach Verifikation

### Voice-Features
- **tempvoice/** - Automatische Voice-Channel-Verwaltung
- **rank_voice_manager.py** - Rank-basierte Voice-Channels
- **voice_activity_tracker.py** - Voice-Statistiken & Leaderboards
- **deadlock_voice_status.py** - Voice-Status-Tracking

### Gaming-Features
- **deadlock_team_balancer.py** - Faire Team-Zusammenstellung
- **dl_coaching.py** - Match-Coaching-System
- **lfg.py** - Looking for Group
- **build_publisher.py** - Automatisches Build-Spiegeln

### Content-Management
- **clip_submission.py** - Wöchentliche Clip-Sammlung
- **feedback_hub.py** - Anonymes Community-Feedback
- **twitch/** - Twitch-Statistiken & Streamer-Leaderboards

### Utility
- **dashboard_cog.py** - Dashboard
- **db_helper.py** - Datenbank-Helfer
- **server_faq.py** - FAQ-System
- **claim_system.py** - Claim-System
- **ai_connector.py** - AI-Integration

---

## Features Roadmap

### ✅ Produktiv (Done)
- [DEADLOCK-1] Onboarding & Regelbestätigung
- [DEADLOCK-2] Steam-Verknüpfung (OAuth)
- [DEADLOCK-3] TempVoice-Lanes
- [DEADLOCK-4] Team Balancer
- [DEADLOCK-5] Voice-Activity-Tracking
- [DEADLOCK-6] Match-Coaching
- [DEADLOCK-7] Build-Publishing
- [DEADLOCK-8] Clip-Einreichung
- [DEADLOCK-9] Feedback Hub
- [DEADLOCK-10] Twitch-Statistiken

### 🔵 Geplant (Planned)
- [DEADLOCK-X] [Neue Features kommen hierhin]

### ⚪ Backlog
- [DEADLOCK-X] [Future Ideas]

---

## Status-Legende
- ⚪ Backlog (noch nicht gestartet)
- 🔵 Planned (Requirements geschrieben)
- 🟡 In Review (User reviewt)
- 🟢 In Development (Wird gebaut)
- ✅ Done (Live + getestet)

---

## Environment Variables

```bash
# Discord
DISCORD_TOKEN=your_discord_bot_token

# Steam (Optional für erweiterte Features)
STEAM_API_KEY=your_steam_api_key

# Database
DATABASE_PATH=data/bot.db  # SQLite Database

# Performance
KILL_AFTER_SECONDS=2  # Shutdown watchdog timer

# Logging
LOG_LEVEL=INFO

# [Weitere ENV-Variablen hier dokumentieren]
```

Siehe `.env.example` für vollständige Liste.

---

## Autonome Komponenten

### Standalone Manager (`service/standalone_manager.py`)
Verwaltet autonome Hintergrund-Prozesse mit Auto-Recovery:

- **Steam-Bridge** (Node.js)
  - Auto-Login mit Refresh-Token
  - Auto-Reconnect bei Disconnect
  - Auto-Recovery bei Crash
  
- **Build-Publishing-Worker**
  - Automatisches Spiegeln von Top-Spieler-Builds
  - Zero-Maintenance nach Aktivierung
  - Queue-basiertes Processing

### Monitoring
```bash
# Master Bot Logs
tail -f logs/master_bot.log

# Steam GC Messages
tail -f logs/deadlock_gc_messages.log

# Voice Status
tail -f logs/deadlock_voice_status.log

# Build Publisher
tail -f logs/master_bot.log | grep build_publisher
```

---

## Development Workflow mit AI Agents

### 1. Requirements Phase
```
"Lies .claude/agents/requirements-engineer.md und erstelle eine Feature Spec für [neue Idee]"
```

### 2. Architecture Phase
```
"Lies .claude/agents/solution-architect.md und designe die Architektur für /features/DEADLOCK-X.md"
```

### 3. Implementation Phase
```
"Lies .claude/agents/backend-dev.md und implementiere /features/DEADLOCK-X.md"
```

### 4. Testing Phase
```
"Lies .claude/agents/qa-engineer.md und teste /features/DEADLOCK-X.md"
```

### 5. Deployment Phase
```
"Lies .claude/agents/devops.md und deploye DEADLOCK-X"
```

---

## Design Decisions

### Warum Python + discord.py?
- Python 3.11+ für moderne Async-Unterstützung
- discord.py ist die etablierteste Discord-Library
- Einfache Integration mit Steam/Twitch APIs

### Warum SQLite statt PostgreSQL/MongoDB?
- Einfache Deployment (keine separaten Services)
- Ausreichend für Community-Bot-Scale
- Gut für Backups (einfach data/bot.db kopieren)

### Warum Node.js Bridge für Steam?
- Steam Game Coordinator nutzt Protobuf
- Bestehende Node.js-Libraries (steam-user, etc.)
- Standalone-Prozess für Isolation

### Warum Cog-System?
- Modulare Architektur (Features isoliert)
- Einfaches Hot-Reloading (`!load`, `!unload`)
- Bessere Code-Organisation

---

## Bekannte Limitierungen

### Discord API Rate Limits
- Bulk-Operationen müssen rate-limited werden
- Voice-Updates haben separate Limits

### Steam GC Verbindung
- Kann bei Steam-Wartung disconnecten
- Auto-Reconnect implementiert

### SQLite Concurrency
- Keine parallelen Writes (ABER: ausreichend für Bot-Scale)
- Bei Bedarf später auf PostgreSQL migrieren

---

## Community & Credits

- **Deutsche Deadlock Community** - Discord-Server
- **EarlySalty** - Streamer & Community-Lead
- **Build-Quellen:** Sanya Sniper, Cosmetical, Piggy, Average Jonas, u.a.

---

## Next Steps für neue Features

1. **Feature-Idee definieren**
   - Welches Problem soll gelöst werden?
   - Für welche User-Gruppe?

2. **Requirements Engineer starten**
   ```
   "Lies C:\Users\Nani-Admin\.claude-agents\agents\requirements-engineer.md
   und C:\Users\Nani-Admin\Documents\Deadlock\PROJECT_CONTEXT.md
   und erstelle eine Feature Spec für [Idee]"
   ```

3. **AI Agent Workflow folgen**
   - Requirements → Architecture → Development → QA → Deployment

4. **Testing im Dev-Server**
   - Erst lokal testen
   - Dann auf Discord Test-Server
   - Dann Production

5. **Monitoring nach Deployment**
   - Logs checken
   - User-Feedback sammeln
   - Bugs fixen

---

**Built with Python + discord.py + AI Agent Team System**

Stand: Januar 2025
