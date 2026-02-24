# Twitch Tables Migration: SQLite → PostgreSQL

## Überblick

Alle Twitch-Tabellen (außer Auth) werden von SQLite nach PostgreSQL migriert.

### In SQLite BLEIBEN (verschlüsselt)
| Tabelle | Grund |
|---|---|
| `twitch_raid_auth` | Verschlüsselte OAuth-Tokens |
| `social_media_platform_auth` | Verschlüsselte Platform-Credentials |
| `oauth_state_tokens` | Ephemere CSRF-Tokens |

### Nach PostgreSQL migriert (35 Tabellen)
Streamer, Live-State, Stream-Sessions, Chat, Raid-History, Events, Social-Media-Clips, Templates, Partner-Outreach, Discord-Invites, Snapshots, etc.

---

## Migration ausführen

### 1. Dry-Run (nichts wird geschrieben)
```bash
python migrations/twitch_tables_migrate.py --dry-run
```
Zeigt Zeilenzahlen aus SQLite und was migriert würde.

### 2. Daten migrieren, SQLite-Tabellen NICHT löschen
```bash
python migrations/twitch_tables_migrate.py --no-drop
```
Schreibt Daten nach PG, lässt SQLite unverändert. Gut zum Testen.

### 3. Vollständige Migration (Daten migrieren + SQLite aufräumen)
```bash
python migrations/twitch_tables_migrate.py
```
- Migriert alle Daten nach PG
- Vergleicht Zeilenzahlen (PG ≥ SQLite erforderlich)
- Löscht migrierte Tabellen aus SQLite

### Voraussetzungen
- `TWITCH_ANALYTICS_DSN` muss gesetzt sein (Env-Variable oder Windows Credential Manager)
- Bot-Root als Working Directory: `cd C:\Users\Nani-Admin\Documents\Deadlock`

---

## Architektur nach Migration

### DB-Zugriffsmuster

| Datei | Import | Tabellen |
|---|---|---|
| `raid/manager.py` (RaidAuthManager) | `_sqlite_get_conn` | `twitch_raid_auth` |
| `raid/manager.py` (RaidBot/Executor) | `get_conn` (PG) | alle anderen |
| `raid/mixin.py` | beide | PG für Streamers, SQLite für Auth |
| `raid/commands.py` | beide | PG für Streamers/History, SQLite für Auth |
| `social_media/oauth_manager.py` | SQLite | Auth-only |
| `social_media/credential_manager.py` | SQLite | Auth-only |
| `social_media/token_refresh_worker.py` | SQLite | Auth-only |
| alle anderen `cogs/twitch/**` | PG | non-auth |

### Cross-DB JOINs
`twitch_streamers` (PG) und `twitch_raid_auth` (SQLite) können nicht direkt gejoint werden.
→ Zwei separate Queries + Python-Merge (siehe `mixin.py:186`, `commands.py:185`, `manager.py:2618`).

---

## Schema-Verwaltung

`storage_pg.py::ensure_schema()` wird beim Bot-Start aufgerufen und erstellt/aktualisiert alle PG-Tabellen idempotent.

PG-Besonderheiten vs. SQLite:
- `INTEGER PRIMARY KEY AUTOINCREMENT` → `SERIAL PRIMARY KEY`
- `BLOB` → `BYTEA`
- View nutzt `::timestamptz >= NOW()` statt `julianday()`
- `ADD COLUMN IF NOT EXISTS` statt Migrations-Helper
