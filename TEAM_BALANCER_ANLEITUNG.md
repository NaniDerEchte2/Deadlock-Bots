# 🎯 Deadlock Team Balancer - Anleitung

## Was ist das?
Der Deadlock Team Balancer erstellt automatisch ausbalancierte Teams für Custom Matches basierend auf den Deadlock-Rängen der Spieler.

## Installation
Das Team Balancer Cog ist bereits in den `main_bot.py` integriert und wird automatisch geladen.

## Verwendung

### Grundlegende Befehle
- `!balance` oder `!bal` - Zeigt alle verfügbaren Befehle
- `!balance auto` - Automatisches Balancing (nur Anzeige)
- `!balance start` - **NEU**: Balancing mit automatischen Voice Channels
- `!balance voice` - Alias für `auto`
- `!balance manual @user1 @user2 @user3...` - Manuelles Balancing für spezifische User
- `!balance status [@user]` - Zeigt Rank-Status eines Users (oder eigenen)
- `!balance matches` - **NEU**: Zeigt alle aktiven Matches
- `!balance end <match_id>` - **NEU**: Beendet Match mit TempVoice Nachbesprechung
- `!balance cleanup [hours]` - **NEU**: Admin-Befehl zum Bereinigen alter Matches

### Typischer Workflow

#### Option A: Nur Anzeige (ohne automatische Channels)
1. **Alle Spieler in einen Voice Channel sammeln**
2. **Balance berechnen**: `!balance auto`
3. **Manuell aufteilen**: Spieler bewegen sich selbst in separate Channels

#### Option B: Vollautomatisch (mit automatischen Channels)
1. **Alle Spieler in einen Voice Channel sammeln** (4-12 Personen)
2. **Match starten**: `!balance start`
3. **Bei >12 Spielern**: Interaktive Spielerauswahl mit Buttons
4. **Automatische Aufteilung**:
   - Bot erstellt "🔵 Team Amber - Match XXX" Channel
   - Bot erstellt "🟠 Team Sapphire - Match XXX" Channel  
   - Bot bewegt Spieler automatisch in ihre Team-Channels
5. **Nach dem Match**: `!balance end XXX` 
   - Bot geht in Casual Lane → triggert TempVoice System
   - Bewegt alle Spieler in den neuen TempVoice Channel
   - Löscht die Team-Channels automatisch
6. **Nachbesprechung**: TempVoice Channel wird automatisch verwaltet

### Beispiel: Vollautomatisches Match

```
User: !balance start

Bot: 
🎯 Match-Start für General Voice - Match 001

🔵 Team Amber (Ø 6.2)
**MaxMustermann** - Archon (7)
**GamerGirl123** - Emissary (6)
**ProPlayer** - Oracle (8)
**Noob42** - Ritualist (5)
**DeadlockFan** - Archon (7)
**CasualPlayer** - Emissary (6)

🟠 Team Sapphire (Ø 6.0)
**EliteGamer** - Oracle (8)
**MidTier** - Ritualist (5)
**TopPlayer** - Archon (7)
**NewPlayer** - Alchemist (3)
**VetPlayer** - Oracle (8)
**SkillIssue** - Emissary (6)

📊 Balance-Analyse
✅ Rank-Unterschied: 0.17
Team 1 Varianz: 0.97
Team 2 Varianz: 3.33

🎮 Match Channels
**🔵 Team Amber - Match 001**: 6/6 Spieler bewegt
**🟠 Team Sapphire - Match 001**: 6/6 Spieler bewegt
Match ID: `001` - Verwende `!balance end 001` zum Beenden
```

### Match beenden mit TempVoice Nachbesprechung
```
User: !balance end 001

Bot:
🏁 Match 001 beendet

💬 TempVoice Nachbesprechung
**Channel**: #Nani-Admin's Channel
**Spieler bewegt**: 12/12
*TempVoice Channel wurde über Casual Lane erstellt und wird automatisch verwaltet*

🗑️ Gelöschte Match-Channels
🔵 Team Amber - Match 001
🟠 Team Sapphire - Match 001

📊 Match-Statistiken
**Dauer**: 23min 45s
**Spieler**: 12
```

> **Info**: Der TempVoice Channel wird automatisch von eurem TempVoice System verwaltet und löscht sich selbst wenn leer.

## Rang-System

### Deadlock Ränge (niedrig zu hoch)
1. **Obscurus** (0) - Unranked/Default
2. **Initiate** (1)
3. **Seeker** (2)
4. **Alchemist** (3)
5. **Arcanist** (4)
6. **Ritualist** (5)
7. **Emissary** (6)
8. **Archon** (7)
9. **Oracle** (8)
10. **Phantom** (9)
11. **Ascendant** (10)
12. **Eternus** (11)

### Rang-Erkennung
Der Bot erkennt Ränge auf zwei Arten:
1. **Discord-Rollen** (Priorität) - Automatisch basierend auf Server-Rollen
2. **Datenbank** (Fallback) - Aus dem separaten Rank-Bot System

## Balance-Algorithmus

### Wie funktioniert das Balancing?
1. **Rank-Werte sammeln** - Jeder Spieler bekommt einen numerischen Rank-Wert
2. **Team-Kombinationen generieren** - Alle möglichen 6v6 Aufteilungen werden berechnet
3. **Balance-Score berechnen** - Kombiniert Durchschnitts-Rank und Varianz innerhalb der Teams
4. **Beste Kombination wählen** - Team-Paar mit geringster Rank-Diferenz wird gewählt

### Balance-Qualität
- ✅ **Sehr gut** - Rank-Unterschied < 1.0
- ⚠️ **Akzeptabel** - Rank-Unterschied 1.0-2.0
- ❌ **Unbalanciert** - Rank-Unterschied > 2.0

## Erweiterte Features

### Match-Verwaltung
```
!balance matches         # Zeigt alle aktiven Matches
!balance end <match_id>  # Beendet Match mit TempVoice Nachbesprechung
!balance cleanup 3       # Admin: Löscht Matches älter als 3 Stunden
```

### Manuelles Balancing
Für spezifische User-Auswahl:
```
!balance manual @User1 @User2 @User3 @User4 @User5 @User6 @User7 @User8
```

### Status-Check
Prüfe den Rank eines Users:
```
!balance status @Username
```
Zeigt:
- Aktueller Rank (aus Rollen oder DB)
- Discord-Rollen-Rank
- Datenbank-Rank

### Automatische Features
- **Voice Channel Erstellung**: Erstellt automatisch Team-Channels
- **User Movement**: Bewegt Spieler automatisch in ihre Team-Channels
- **Match-Tracking**: Verfolgt aktive Matches mit IDs
- **TempVoice Integration**: Nutzt euer TempVoice System für Nachbesprechung
- **Auto-Cleanup**: Admin-Befehle für Channel-Bereinigung

### Interaktive Spielerauswahl (>12 Spieler)

Wenn mehr als 12 Spieler im Voice Channel sind:

```
User: !balance start

Bot: 
👥 Zu viele Spieler im Channel!
15 Spieler gefunden, aber Maximum ist 12.
Wähle die Spieler für das Match aus:

📋 Verfügbare Spieler
1. MaxMustermann - Archon (7)
2. GamerGirl123 - Emissary (6) 
3. ProPlayer - Oracle (8)
[... weitere Spieler als klickbare Buttons ...]

🎯 Anleitung
• Klicke auf Spieler um sie auszuwählen
• 🎲 für zufällige Auswahl  
• 🎮 um Match zu starten
```

- **Buttons**: Klicke Spieler an/ab (grün = ausgewählt)
- **🎲 Zufällig**: Wählt automatisch 12 zufällige Spieler
- **🎮 Match starten**: Startet mit ausgewählten Spielern (4-12)

## Häufige Probleme

### "Mindestens 4 Spieler benötigt"
- Das System funktioniert ab 4 Spielern (2v2)
- Optimale Balance ab 6+ Spielern

### "Keine Rang-Rollen gefunden"
- User haben keine Discord-Rang-Rollen
- User sind nicht in der Rank-Bot Datenbank
- Standard-Rank "Obscurus" wird verwendet

### Balance scheint schlecht
- Bei sehr unterschiedlichen Skill-Levels ist perfekte Balance schwierig
- Der Algorithmus wählt die beste verfügbare Kombination
- Manchmal sind manuelle Anpassungen nötig

## Integration mit anderen Systemen

### Mit Rank Voice Manager
- Beide Systeme nutzen dieselben Discord-Rollen
- Konsistente Rang-Erkennung
- Automatische Synchronisation

### Mit Standalone Rank Bot
- Fallback für User ohne Discord-Rollen
- Separate Datenbank als zusätzliche Quelle
- Kombinierte Rank-Ermittlung

## Admin-Funktionen

Der Team Balancer hat keine speziellen Admin-Befehle - alle Funktionen sind für normale User verfügbar.

## Technische Details

### Dateien
- **Hauptdatei**: `cogs/deadlock_team_balancer.py`
- **Integration**: Automatisch in `main_bot.py` geladen
- **Abhängigkeiten**: Nutzt existierende Discord-Rollen und DB

### Logging
Alle Balance-Operationen werden geloggt in:
- `logs/master_bot.log`
- Console-Output bei Fehlern