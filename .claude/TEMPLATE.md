# [DEADLOCK-X] Feature Name Template

**Status:** 🔵 Planned  
**Created:** [Datum]  
**Type:** [Command | Event | Background Task | Combo]  
**Feature-ID:** DEADLOCK-X

---

## User Story
Als [User-Rolle] möchte ich [Aktion], damit [Nutzen].

**Beispiel:**
> Als Discord-Mitglied möchte ich einen `/level` Command nutzen, 
> damit ich meinen aktuellen Voice-Activity-Level sehen kann.

---

## Problem Statement
Welches Problem löst dieses Feature?

**Beispiel:**
> Aktuell gibt es keine Möglichkeit, aktive Voice-User zu erkennen. 
> Ein Leveling-System motiviert User, mehr in Voice-Channels aktiv zu sein.

---

## Acceptance Criteria

### Must-Have (MVP)
- [ ] [Kriterium 1]
- [ ] [Kriterium 2]
- [ ] [Kriterium 3]

### Should-Have
- [ ] [Kriterium 4]
- [ ] [Kriterium 5]

### Nice-to-Have
- [ ] [Kriterium 6]
- [ ] [Kriterium 7]

---

## Edge Cases

### Discord-spezifisch
- [ ] [Edge Case 1 - z.B. User muted]
- [ ] [Edge Case 2 - z.B. User alleine in Channel]

### Database
- [ ] [Edge Case 3 - z.B. User verlässt Server]
- [ ] [Edge Case 4 - z.B. Guild gelöscht]

### Permissions
- [ ] [Edge Case 5 - z.B. Command in DMs nutzbar?]
- [ ] [Edge Case 6 - z.B. Wer darf Admin-Commands nutzen?]

### Performance
- [ ] [Edge Case 7 - z.B. 1000 User gleichzeitig]
- [ ] [Edge Case 8 - z.B. Leaderboard mit 10.000 Users]

---

## Tech Design (vom Solution Architect)

### Database Schema

**Table: `feature_name`**

**Zweck:** [Beschreibung]

**Felder:**
- `id` (INTEGER, PRIMARY KEY, AUTOINCREMENT) - Unique ID
- `user_id` (INTEGER, NOT NULL) - Discord User ID
- `guild_id` (INTEGER, NOT NULL) - Discord Guild ID
- `data` (TEXT/INTEGER/JSON) - Feature-spezifische Daten
- `created_at` (TIMESTAMP, DEFAULT CURRENT_TIMESTAMP)
- `updated_at` (TIMESTAMP, DEFAULT CURRENT_TIMESTAMP)

**Constraints:**
- UNIQUE(user_id, guild_id)

**Indexes:**
- `idx_user_guild` auf (user_id, guild_id)
- `idx_guild_data` auf (guild_id, data)

---

### Discord Integration

**Commands:**
1. `/command_name`
   - Beschreibung: [Was macht der Command]
   - Parameter: [param1 (Type, required/optional)]
   - Permissions: [Administrator / Moderator / Public]
   - Response: [Ephemeral / Public]

**Events:**
1. `on_event_name`
   - Trigger: [Wann wird Event gefeuert]
   - Aktion: [Was passiert]

**Background Tasks:**
1. Feature Update Loop
   - Frequenz: [z.B. alle 5 Minuten]
   - Aktion: [Was wird ausgeführt]

---

### UI Components

**Embeds:**
- [Embed 1 Name]: [Beschreibung]
- [Embed 2 Name]: [Beschreibung]

**Views/Buttons:**
- [View 1 Name]: [Buttons + Funktionen]

**Modals:**
- [Modal 1 Name]: [Input-Felder]

---

### Cog Architecture

**File:** `cogs/feature_name.py`

**Class:** `FeatureName(commands.Cog)`

**Methods:**
- `__init__(bot)` - Initialize
- `command_name()` - Slash Command
- `on_event()` - Event Listener
- `background_task()` - Loop Task (optional)

**Dependencies:**
- service/db.py
- discord.py
- [Andere Cogs wenn nötig]

---

### Data Flow

**Example Flow:**

```
1. User → /command_name param="value"
2. Bot → Validate Permissions
3. Bot → Query Database
4. Bot → Process Data
5. Bot → Update Database
6. Bot → Send Response (Embed)
7. Bot → Log Action
```

---

## User Flow

### Schritt 1: [Action]
```
User → [Aktion]
Bot → [Reaktion]
Bot → [Weiteres]
```

### Schritt 2: [Action]
```
User → [Aktion]
Bot → [Reaktion]
```

---

## Implementation (vom Backend Developer)

### Cog-Datei: `cogs/feature_name.py`

```python
# Wird vom Backend Developer erstellt
```

### Database Migration

```sql
-- CREATE TABLE statements
-- INDEX statements
```

---

## Test Results (vom QA Engineer)

**Date:** [Datum]  
**Tester:** QA Engineer  
**Status:** [🟢 Passed | 🟡 Passed with Issues | 🔴 Failed]

### Summary
- Total Tests: X
- Passed: X
- Failed: X
- Known Issues: X

### Functional Tests
- [✅ / ❌] [Test 1]
- [✅ / ❌] [Test 2]

### Permission Tests
- [✅ / ❌] [Test 3]
- [✅ / ❌] [Test 4]

### Database Tests
- [✅ / ❌] [Test 5]

### Edge Case Tests
- [✅ / ❌] [Test 6]

### Bugs
- [Bug 1 - Link/Beschreibung]
- [Bug 2 - Link/Beschreibung]

### Recommendations
- [Empfehlung 1]
- [Empfehlung 2]

---

## Deployment (vom DevOps Engineer)

**Date:** [Datum]  
**Engineer:** DevOps  
**Status:** [✅ Deployed | 🟡 Staged | ⚪ Not Deployed]

### Deployment Steps
1. Code pulled from Git
2. Database Migrations: [List]
3. Bot restarted via: [Method]
4. Health Check: [✅ Passed / ❌ Failed]

### Production URLs
- Bot: Online auf Discord Server
- Logs: logs/master_bot.log

### Monitoring
- Health Check: `/health` (Admin-only)
- Logs: `tail -f logs/master_bot.log`

### Known Issues
- [Issue 1]
- [Issue 2]

### Rollback Plan
- Database Backup: [Path]
- Git Commit: [Hash]

---

## Success Metrics

Wie messen wir Erfolg?

- **Metric 1:** [z.B. +20% mehr Voice-Activity]
- **Metric 2:** [z.B. 50% der User nutzen Command]
- **Metric 3:** [z.B. User bleiben +10 Min länger]

---

## Dependencies

- Benötigt: [service/db.py, discord.py v2.x]
- Optional: [Andere Cogs]

---

## Rollout Plan

1. **Dev-Server Testing** (1-2 Tage)
   - Test mit 5-10 User
   - Edge Cases prüfen

2. **Beta auf Production** (1 Woche)
   - Aktivieren für alle User
   - Monitoring

3. **Full Launch** (nach 1 Woche Beta)
   - Announcement
   - Optional: Features aktivieren

---

## Open Questions

- [Frage 1]
- [Frage 2]

---

## Change Log

| Date | Change | By |
|------|--------|-----|
| [Datum] | Initial Spec | Requirements Engineer |
| [Datum] | Tech Design added | Solution Architect |
| [Datum] | Implementation completed | Backend Developer |
| [Datum] | Testing completed | QA Engineer |
| [Datum] | Deployed to Production | DevOps |

---

**Next Steps:**
1. User reviewt Spec
2. Weiter zu Solution Architect (wenn approved)
3. Weiter zu Backend Developer (nach Tech Design)
4. Weiter zu QA Engineer (nach Implementation)
5. Weiter zu DevOps (nach Testing)

---

**Feature Status History:**
- ⚪ Backlog → 🔵 Planned → 🟡 In Review → 🟢 In Development → ✅ Done
