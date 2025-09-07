# Discord Bot System Dokumentation
## Vollständige Anleitung für Stellvertretungen und Notfall-Management

---

## 🤖 **ÜBERBLICK - Bot-System Architektur**

Das Discord Bot System besteht aus **2 Hauptkomponenten**:

Alle Module greifen auf **eine gemeinsame SQLite-Datenbank** zu:
`%USERPROFILE%/Documents/Deadlock/service/deadlock.sqlite3` (überschreibbar über `DEADLOCK_DB_DIR`).
Diese Datei wird beim Herunterfahren automatisch aufgeräumt (VACUUM).

### **1. Master Bot (main_bot.py)**
- **Pfad:** `C:\Users\Nani-Admin\Documents\Deadlock\main_bot.py`
- **Funktion:** Verwaltet alle anderen Bot-Module (Cogs)
- **Status:** Läuft kontinuierlich und überwacht alle Subsysteme

### **2. Standalone Rank Bot**
- **Pfad:** `C:\Users\Nani-Admin\Documents\Deadlock\rank_bot\standalone_rank_bot.py` 
- **Funktion:** Separater Bot nur für Rank-Management
- **Status:** Läuft unabhängig vom Master Bot

---

## 📋 **MASTER BOT - Alle Funktionen im Detail**

Der Master Bot lädt folgende **Cogs (Module)**:

### **1. 🎓 DL Coaching System (`dl_coaching.py`)**
- **Zweck:** Deadlock Coaching und Training
- **Funktionalität:** Startet externes Coaching-Skript
- **Pfad:** `Deadlock/original_scripts/dl_coaching.py`
- **Notfall:** Bei Fehlern Cog neu laden oder Original-Skript direkt starten

### **2. 🏷️ Claim System (`claim_system.py`)**
- **Zweck:** Für die Zuweisung des Tickets aus DL Coaching
- **Funktionalität:** Startet externes Claim-System-Skript
- **Pfad:** `Deadlock/original_scripts/Claim-System.py`
- **Notfall:** Bei Fehlern Original-Skript manuell starten

### **3. 📰 Changelog Bot (`changelog_discord_bot.py`)**
- **Zweck:** Automatische Deadlock Patchnote Update-Benachrichtigungen
- **Funktionalität:** Überwacht Deadlock-Updates und postet in Changelogs
- **Pfad:** `Deadlock/original_scripts/changelog-discord-bot.py`
- **Notfall:** Wichtig für Community - bei Ausfall schnell neu starten

### **4. 🤖 Forum KI Bot (`forum_ki_bot.py`)**
- **Zweck:** KI-basierte übersetzung für die Patches
- **Funktionalität:** s.o.
- **Pfad:** `Deadlock/original_scripts/forum-ki-bot.py`
- **Notfall:** Bei KI-Fehlern kann temporär deaktiviert werden

### **5. 📊 Voice Activity Tracker (`voice_activity_tracker.py`)**
- **Zweck:** Überwacht und belohnt Voice-Channel Aktivität # Absolut unwichtig für den Normalbetrieb
- **Funktionalität:**
  - Trackt Zeit in Voice-Channels
  - Vergibt Punkte für Aktivität
  - Spezielle Belohnungen für bestimmte Rollen
  - Speicherung aller Statistiken in gemeinsamer SQLite-Datenbank
- **Wichtig:** Nutzt zentrale DB unter `%USERPROFILE%/Documents/Deadlock/service/deadlock.sqlite3`
- **Notfall:** Backup der Datenbank täglich automatisch

### **6. 🎖️ Rank Voice Manager (`rank_voice_manager.py`)**
- **Zweck:** Automatische Voice-Channel Berechtigung basierend auf Rängen # Für die Rank Kategorie wichtig
- **Funktionalität:**
  - Überwacht Ranked Voice-Channels
  - Blockiert niedrigere Ränge automatisch
  - Basiert auf Discord-Rollen-System
- **Kategorien:** Überwacht Kategorie `1357422957017698478`
- **Ausnahmen:** Bestimmte Channels sind ausgeschlossen

### **7. 🎙️ TempVoice System (`tempvoice.py`)**
- **Zweck:** Temporäre Voice-Channel Erstellung und Verwaltung
- **Funktionalität:**
  - Automatische Channel-Erstellung
  - EU/DE Region-Filter
  - Voice Channel Status Berechtigungen
  - Umfassende Kontrollen (Rename, Limit, Privacy)
- **Besonderheit:** Casual Lane Auto-Numbering
- **Interface:** Buttons für alle Funktionen

### **8. ⚖️ Team Balancer (`deadlock_team_balancer.py`)**
- **Zweck:** Automatische Team-Balance für Deadlock Custom Matches
- **Funktionalität:**
  - Rank-basierte Team-Aufteilung
  - Interaktive Spielerauswahl
  - Balance-Algorithmus für faire Teams
- **Verwendung:** Für Tournament/Scrim Organisation

---

## 🏆 **STANDALONE RANK BOT**

### **Hauptfunktionen:**
- **Dropdown-Interface** für Rank-Auswahl
- **Automatische Rollen-Verwaltung**
- **Persistent Views** (überleben Bot-Neustarts)
- **Gemeinsame SQLite Datenbank** für Rank-Tracking (`%USERPROFILE%/Documents/Deadlock/service/deadlock.sqlite3`)

### **Ranks System:**
```
Obscurus (0) → Initiate (1) → Seeker (2) → Alchemist (3) 
→ Arcanist (4) → Ritualist (5) → Emissary (6) → Archon (7) 
→ Oracle (8) → Phantom (9) → Ascendant (10) → Eternus (11)
```

### **Kritische Features:**
- **Automatische Rollen-Zuweisung**
- **Rank-Upgrade/Downgrade System**
- **Persistent UI** (funktioniert auch nach Neustart)

---

## 🚨 **NOTFALL-PROCEDURES**

### **Bot ist offline/crashed:**

#### **1. Master Bot Neustart:**
```bash
cd C:\Users\Nani-Admin\Documents\Deadlock
python main_bot.py
```

#### **2. Rank Bot Neustart:**
```bash
cd C:\Users\Nani-Admin\Documents\Deadlock\rank_bot
python standalone_rank_bot.py
```

#### **3. Einzelne Cogs neu laden:**
Im Discord (#bot-logs): `!master reload <cog_name>`
Beispiel: `!master reload tempvoice`

#### **4. Cog Status prüfen:**
Im Discord (#bot-logs): `!master cog_status`

### **Häufige Probleme & Lösungen:**

#### **❌ "Cog lädt nicht"**
1. Log-Datei prüfen: `Deadlock/logs/master_bot.log`
2. Original-Skript einzeln testen

#### **❌ "Voice Tracking funktioniert nicht"**
1. Voice Activity Tracker neu laden

#### **❌ "TempVoice Buttons reagieren nicht"**
1. TempVoice Cog neu laden
2. Interface neu deployen: `!tempvoice reload`

#### **❌ "Rank Bot Dropdown weg"**
1. Standalone Rank Bot neu starten
2. Persistent Views werden automatisch restauriert
3. Falls nicht: Interface manuell neu erstellen

---

## 🔧 **WARTUNG & MONITORING**

### **Checks:**
- [ ] Alle Bots online und erreichbar
(- [ ] Log-Dateien auf Fehler prüfen)
(- [ ] Voice Activity Punkte werden korrekt vergeben)
- [ ] TempVoice Channels funktionieren
- [ ] Rank System reagiert auf Änderungen

### **Wartung:**
- [ ] Log-Dateien archivieren/löschen
- [ ] Datenbank-Backup prüfen
- [ ] Performance-Metriken überprüfen
- [ ] Update-Check für Dependencies

### **Log-Dateien Standorte:**
- Master Bot: `Deadlock/logs/master_bot.log`
- Voice Tracker: `Deadlock/voice_data/tracker.log`
- Rank Bot: Konsolen-Output

---

## 📞 **ESKALATION & SUPPORT**

### **Level 1 - Einfache Probleme:**
- Bot-Neustart
- Cog neu laden
- Interface neu deployen

### **Level 2 - Erweiterte Probleme:**
- Datenbank-Issues
- Berechtigungs-Probleme
- Performance-Issues

### **Level 3 - Kritische Probleme:**
- Vollständiger System-Ausfall
- Daten-Verlust
- Security-Issues

### **Wichtige Befehle für Troubleshooting:**
```
!master reload <cog_name>     # Einzelnes Modul neu laden
!master cog_status           # Status aller Module
!master tempvoice status     # TempVoice spezifisch
!master cleanup             # Aufräumen von verwaisten Channels
```

---

## 🔐 **SICHERHEIT & BERECHTIGUNGEN**

### **Bot-Token Standorte:**
- Master Bot: `.env` Datei im Deadlock Ordner
- Rank Bot: `.env` Datei im rank_bot Ordner

### **Kritische Berechtigungen:**
- **Administrator** (für Channel-Management)
- **Manage Roles** (für Rank-System)
- **Manage Channels** (für TempVoice)
- **View Audit Log** (für Monitoring)

### **Backup-Strategien:**
- **Code:** Git Repository (automatisch)
- **Datenbanken:** Tägliche SQLite Backups
- **Konfiguration:** .env Dateien sichern

---

## 📋 **KONTAKT & RESSOURCEN**

### **Bei kritischen Problemen:**
1. **Sofort:** Bot-Neustart versuchen
2. **Binnen 5 Min:** System-Administrator kontaktieren
3. **Dokumentation:** Diese Datei als Referenz nutzen

### **Useful Commands Cheat Sheet:**
```bash
# Bot Status prüfen
!ping
!cog_status

# Module Management  
!master reload tempvoice
!master reload voice_activity_tracker

# TempVoice spezifisch
!master tempvoice status
!master tempvoice cleanup
!master tempvoice reload

# Emergency
!shutdown  # Nur für Admins
```
Server: 
RDP: 94.16.119.96
Visual Studio Code öffnen und dort alles ausführen.

---

## ⚠️ **WICHTIGE HINWEISE**

1. **NIE beide Bots gleichzeitig neustarten** - Service-Unterbrechung
2. **Vor Wartung:** Community über geplante Downtime informieren  
3. **Datenbank-Backups:** Automatisch, aber manuell prüfen
4. **Performance:** Bei >1000 Usern online besonders auf Voice Tracker achten
5. **Updates:** Nur nach Test in Development-Umgebung

---

**Dokumentation erstellt:** 2025-08-17  
**Version:** 1.0  
**Nächste Review:** Bei größeren Änderungen oder monatlich