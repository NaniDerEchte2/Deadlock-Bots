# Maintenance Scripts

Diese Scripts sind für Wartung und Setup des Build-Systems.

## Scripts

### Python Scripts

#### `reset_and_rebuild_builds.py`
**Zweck:** Löscht ALLE Builds aus der Datenbank und triggert einen kompletten Rebuild.

**Wann nutzen:**
- Wenn das Build-System komplett neu aufgebaut werden soll
- Nach größeren Änderungen an der Build-Logik
- Wenn zu viele alte/falsche Builds vorhanden sind

**Warnung:** Löscht alle hero_build_sources und hero_build_clones!

```bash
python scripts/maintenance/reset_and_rebuild_builds.py
```

---

### `trigger_rebuild.py`
**Zweck:** Führt einen manuellen Build-Sync durch (ohne vorher zu löschen).

**Wann nutzen:**
- Um sofort neue Builds zu fetchen (statt 4h zu warten)
- Nach dem Hinzufügen neuer Autoren
- Zum Testen der Build-Selection-Logik

**Was es macht:**
1. Fetcht Builds von allen watched_build_authors
2. Speichert sie in hero_build_sources
3. Wählt top 3 Builds pro Hero (nach Priorität)
4. Queued sie für Publishing

```bash
python scripts/maintenance/trigger_rebuild.py
```

---

#### `setup_discovery.py`
**Zweck:** Setup-Script für das Build Discovery System.

**Wann nutzen:** Einmalig beim initialen Setup.

---

#### `run_watched_discovery.py`
**Zweck:** Manuelle Ausführung des Build Discovery Tasks.

**Wann nutzen:** Zum Testen oder manuellen Triggern der Discovery.

---

### JavaScript/Node.js Scripts

#### `check_build_status.js`
**Zweck:** Überprüft den Status aller Build-Tasks im System.

**Wann nutzen:** Zum Debuggen von Build-Publishing-Problemen.

```bash
node scripts/maintenance/check_build_status.js
```

---

#### `check_latest_tasks.js`
**Zweck:** Zeigt die neuesten Steam-Tasks aus der Datenbank.

**Wann nutzen:** Zum Debuggen der Task-Queue.

```bash
node scripts/maintenance/check_latest_tasks.js
```

---

#### `reset_failed_builds.js`
**Zweck:** Setzt fehlgeschlagene Builds zurück auf 'pending' Status.

**Wann nutzen:** Wenn Builds stuck sind oder manuell retry werden sollen.

```bash
node scripts/maintenance/reset_failed_builds.js
```

---

## Build System Übersicht

**Prioritätenliste (höchste → niedrigste):**
1. Deathy (Prio 9)
2. Amerikanec (Prio 8)
3. Heresy (Prio 7)
4. JonJon69 (Prio 6)
5. AverageJonas (Prio 5)
6. ABL (Prio 4)
7. Piggy (Prio 3)
8. Cosmetical (Prio 2)
9. Sanya Sniper (Prio 1)

**Logik:**
- Max 3 Builds pro Hero
- Sortierung: Autor-Priorität (DESC) → Publish-Timestamp (DESC)
- Wenn ein hochpriorisierter Autor keinen Build für einen Hero hat, wird der nächste verfügbare genommen

**Datenbank:** `service/deadlock.sqlite3`
- `hero_build_sources`: Alle gefetchten Builds
- `hero_build_clones`: Publishing-Queue
- `watched_build_authors`: Autor-Prioritäten
