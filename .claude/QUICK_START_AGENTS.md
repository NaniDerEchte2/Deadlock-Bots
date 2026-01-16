# Deadlock Bot - AI Agent System - Quick Start Guide

> Nutze 6 AI Agents um neue Discord Bot Features zu entwickeln: Requirements → Architecture → Development → QA → Deployment

---

## 🚀 Setup (Einmalig)

### Du hast bereits:
- ✅ `.claude/agents/` - 6 angepasste Agents für Discord Bot Development
- ✅ `PROJECT_CONTEXT.md` - Projekt-Dokumentation
- ✅ `features/` - Ordner für Feature Specs
- ✅ Alle Agents sind für **Python Discord Bot** optimiert!

### Noch zu tun (optional):
Wenn du die Agents auch für **TradingBot** nutzen willst:
```bash
# Copy Agents zu TradingBot
xcopy C:\Users\Nani-Admin\Documents\Deadlock\.claude C:\Users\Nani-Admin\Documents\GitHub\TradingBot\.claude /E /I
```

---

## 📋 Agents Übersicht

| Agent | Datei | Funktion |
|-------|-------|----------|
| **Requirements Engineer** | `requirements-engineer.md` | Erstellt Feature Specs mit User Stories |
| **Solution Architect** | `solution-architect.md` | Designed Database Schema + Cog Architecture |
| **UI Developer** | `frontend-dev.md` | Baut Discord UI (Embeds, Buttons, Modals) |
| **Backend Developer** | `backend-dev.md` | Implementiert Cogs, Commands, Database |
| **QA Engineer** | `qa-engineer.md` | Testet Features gegen Acceptance Criteria |
| **DevOps** | `devops.md` | Deployed Features, Monitoring, Backups |

---

## 🎯 Workflow für neue Features

### Schritt 1: Feature Spec erstellen

**Sag zu mir (Claude):**
```
Lies bitte:
1. C:\Users\Nani-Admin\Documents\Deadlock\.claude\agents\requirements-engineer.md
2. C:\Users\Nani-Admin\Documents\Deadlock\PROJECT_CONTEXT.md

Erstelle eine Feature Spec für: [Deine Feature-Idee, z.B. "Leveling-System für Voice-Activity"]
```

**Was passiert:**
- Ich stelle dir Fragen (Feature-Typ, Zielgruppe, Discord-Integration)
- Du beantwortest sie
- Ich erstelle `/features/DEADLOCK-X-feature-name.md` mit:
  - User Stories
  - Acceptance Criteria
  - Edge Cases
  - Technical Notes

**Beispiel:**
```
Ich will ein Feature, wo User für Voice-Zeit XP sammeln und Level-Up machen können.
```

---

### Schritt 2: Architektur designen

**Sag zu mir:**
```
Lies bitte:
1. C:\Users\Nani-Admin\Documents\Deadlock\.claude\agents\solution-architect.md
2. C:\Users\Nani-Admin\Documents\Deadlock\features\DEADLOCK-X-feature-name.md

Designe die Architektur für dieses Feature.
```

**Was passiert:**
- Ich prüfe bestehende Cogs/Tables (Code-Reuse!)
- Ich designe Database Schema (SQLite Tables + Indexes)
- Ich plane Cog-Architektur (Commands, Events, Background Tasks)
- Ich dokumentiere Tech Design (PM-friendly, kein Code!)

---

### Schritt 3: UI Components bauen (optional)

**Wenn dein Feature Discord UI braucht (Embeds, Buttons, Modals):**
```
Lies bitte:
1. C:\Users\Nani-Admin\Documents\Deadlock\.claude\agents\frontend-dev.md
2. C:\Users\Nani-Admin\Documents\Deadlock\features\DEADLOCK-X-feature-name.md

Erstelle die Discord UI Components für dieses Feature.
```

**Was passiert:**
- Ich erstelle Embeds (Stats, Leaderboards, Errors)
- Ich erstelle Views mit Buttons (Confirm/Cancel, Navigation)
- Ich erstelle Modals (Feedback-Forms, Input-Dialoge)
- Ich dokumentiere UX Best Practices

---

### Schritt 4: Backend implementieren

**Sag zu mir:**
```
Lies bitte:
1. C:\Users\Nani-Admin\Documents\Deadlock\.claude\agents\backend-dev.md
2. C:\Users\Nani-Admin\Documents\Deadlock\features\DEADLOCK-X-feature-name.md

Implementiere die Backend-Logik für dieses Feature.
```

**Was passiert:**
- Ich erstelle `cogs/feature_name.py` mit:
  - Slash Commands (`@app_commands.command`)
  - Event Listeners (`on_member_join`, `on_voice_state_update`)
  - Background Tasks (wenn nötig)
  - Database Queries (SQLite)
  - Error Handling + Logging
- Ich dokumentiere Code mit Comments

**Beispiel-Output:**
```python
# cogs/voice_leveling.py
class VoiceLeveling(commands.Cog):
    @app_commands.command(name="level")
    async def level_command(self, interaction):
        # Implementation...
```

---

### Schritt 5: Testing

**Sag zu mir:**
```
Lies bitte:
1. C:\Users\Nani-Admin\Documents\Deadlock\.claude\agents\qa-engineer.md
2. C:\Users\Nani-Admin\Documents\Deadlock\features\DEADLOCK-X-feature-name.md

Erstelle einen Test Plan für dieses Feature.
```

**Was passiert:**
- Ich erstelle Test-Checklisten (Functional, Permissions, Database, Edge Cases)
- Ich beschreibe Manual Testing Steps (Discord Test-Server)
- Ich dokumentiere erwartete Results

**Du testest dann:**
- Commands im Discord Test-Server
- Edge Cases (leere Inputs, falsche Permissions)
- Database (prüfe mit DB Browser for SQLite)

---

### Schritt 6: Deployment

**Sag zu mir:**
```
Lies bitte:
1. C:\Users\Nani-Admin\Documents\Deadlock\.claude\agents\devops.md
2. C:\Users\Nani-Admin\Documents\Deadlock\features\DEADLOCK-X-feature-name.md

Erstelle einen Deployment Plan für dieses Feature.
```

**Was passiert:**
- Ich erstelle Pre-Deployment Checklist
- Ich beschreibe Deployment Steps (Hot-Reload vs. Restart)
- Ich dokumentiere Post-Deployment Testing
- Ich erstelle Rollback Plan

**Du führst dann aus:**
```bash
# Hot-Reload (wenn nur Cog geändert):
!reload cogs.feature_name

# Oder Full Restart (wenn Database Migration):
python main_bot.py
```

---

## 📝 Beispiel: Voice-Leveling Feature

### 1. Requirements
```
User: "Ich will ein Leveling-System für Voice-Activity"

Claude (Requirements Engineer):
- Welche Voice-Channels zählen? (Alle oder nur spezielle?)
- Wie viele XP pro Minute?
- Ab wann Level-Up?
- Sollen User ihre Stats sehen können?
- Leaderboard gewünscht?

User antwortet → Feature Spec wird erstellt
```

### 2. Architecture
```
Claude (Solution Architect):

Database Schema:
- Table: voice_levels (user_id, guild_id, xp, level, timestamps)
- Indexes: (user_id, guild_id), (guild_id, xp DESC)

Cog Architecture:
- Commands: /level, /leaderboard, /set-xp (Admin)
- Events: on_voice_state_update
- Background Task: Calculate XP alle 5 Min
```

### 3. UI Components
```
Claude (UI Developer):

Embeds:
- Stats Embed: Level, XP, Progress Bar
- Leaderboard Embed: Top 10 User

Views:
- Pagination für Leaderboard (Next/Previous)
```

### 4. Backend
```
Claude (Backend Developer):

Erstellt:
- cogs/voice_leveling.py
- Commands implementiert
- Database Queries geschrieben
- Error Handling hinzugefügt
```

### 5. QA
```
Claude (QA Engineer):

Test Plan:
- Functional: /level zeigt korrekte Stats
- Permissions: /set-xp nur für Admins
- Database: XP wird gespeichert
- Edge Cases: User alleine in Voice → zählt nicht
```

### 6. DevOps
```
Claude (DevOps):

Deployment:
1. Backup Database
2. Deploy Code
3. Restart Bot
4. Health Check
5. Monitor Logs
```

---

## 🎨 Tipps für beste Ergebnisse

### ✅ DO
- **Klar beschreiben** was du willst (User Stories helfen!)
- **Fragen beantworten** wenn Agent fragt
- **Bestehende Features erwähnen** falls ähnlich
- **Edge Cases nennen** die dir einfallen

### ❌ DON'T
- **Zu vage sein** ("Mach irgendwas mit Voice")
- **Alle Agents auf einmal nutzen** (folge dem Workflow!)
- **Agent-Namen als Commands** (nicht `/requirements-engineer`, sondern "Lies .claude/agents/...")

---

## 🔧 Troubleshooting

### "Agent findet bestehende Cogs nicht"
```
→ Agent soll Git-Log prüfen:
"Prüfe bitte mit 'git log' welche Cogs bereits existieren"
```

### "Feature Spec zu generisch"
```
→ Agent soll mehr Fragen stellen:
"Bitte stelle mir 5-10 detaillierte Fragen um die Requirements zu verstehen"
```

### "Code funktioniert nicht"
```
→ Zeig mir Logs:
"Hier ist der Error aus logs/master_bot.log: [Error einfügen]"
```

---

## 📚 Nützliche Befehle

### Agent starten
```
Lies C:\Users\Nani-Admin\Documents\Deadlock\.claude\agents\[agent-name].md
und C:\Users\Nani-Admin\Documents\Deadlock\PROJECT_CONTEXT.md
und [deine Aufgabe]
```

### Bestehende Features prüfen
```bash
# Welche Features gibt es?
ls features/

# Welche Cogs existieren?
ls cogs/*.py

# Git-Log für Features
git log --oneline --grep="DEADLOCK-" -10
```

### Database prüfen
```bash
# SQLite öffnen
sqlite3 data/bot.db

# Tables anzeigen
.tables

# Table-Schema
.schema table_name

# Query
SELECT * FROM table_name LIMIT 10;
```

---

## 🎯 Next Steps

1. **Teste das System** mit einem kleinen Feature:
   ```
   Lies C:\Users\Nani-Admin\Documents\Deadlock\.claude\agents\requirements-engineer.md
   und erstelle eine Feature Spec für: "Admin-Command /stats der Bot-Statistiken zeigt"
   ```

2. **Folge dem kompletten Workflow** (Requirements → Architecture → Development → QA → Deployment)

3. **Dokumentiere Learnings** in `PROJECT_CONTEXT.md` → Design Decisions

4. **Iteriere** - Agents werden besser je mehr Context sie haben!

---

## ❓ Fragen?

Sag einfach:
```
"Erkläre mir wie [Agent-Name] funktioniert"
"Zeig mir ein Beispiel für [Feature-Typ]"
"Welcher Agent ist der richtige für [Aufgabe]"
```

---

**Viel Erfolg mit deinem Deadlock Bot! 🚀**

*Stand: Januar 2025*
