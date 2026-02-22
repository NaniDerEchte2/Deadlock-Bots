# Twitch Analytics – Migration auf Postgres/Timescale (ohne Datenverlust)

## Ziele
- Twitch-Analytics-Daten in eigenständige Hochleistungs-DB auslagern.
- Keine Löschung, nur **verlustfreie Compression** per TimescaleDB.
- Eigenes Hosting (Linux/Windows/WSL/Docker).

## Vorbereitung
1. Postgres 16 + TimescaleDB installieren (oder Docker `timescale/timescaledb:pg16`).
2. Leere DB anlegen, z. B. `twitch_analytics`.
3. DSN setzen: `export TWITCH_ANALYTICS_DSN=postgresql://user:pass@localhost:5432/twitch_analytics`
4. (Optional) SQLite-Pfad überschreiben: `export SQLITE_PATH=service/deadlock.sqlite3`

## Schema anlegen
```bash
psql "$TWITCH_ANALYTICS_DSN" -f cogs/twitch/migrations/twitch_analytics_schema.sql
```

## Migration (SQLite -> Postgres)
```bash
python cogs/twitch/migrations/twitch_analytics_migrate.py \
  --sqlite "${SQLITE_PATH:-service/deadlock.sqlite3}" \
  --dsn "$TWITCH_ANALYTICS_DSN"
```
- Kopiert nur Analyse-Tabellen, keine Tokens/OAuth.
- Standard: TRUNCATE vor Import. Mit `--no-truncate` behalten.
- Batchgröße anpassbar: `--batch 20000` für große Tabellen.

## Nach der Migration
- Stichproben prüfen (Counts, Min/Max Timestamp):
  ```bash
  psql "$TWITCH_ANALYTICS_DSN" -c "SELECT count(*) FROM twitch_chat_messages;"
  psql "$TWITCH_ANALYTICS_DSN" -c "SELECT min(message_ts), max(message_ts) FROM twitch_chat_messages;"
  ```
- Compression aktiviert (keine Retention). Daten bleiben vollständig erhalten.
- Alte SQLite-Datei als Backup aufbewahren.

## Geplante nächste Schritte (Code)
- neuen DB-Adapter (psycopg/asyncpg) anbinden.
- Twitch-Write-Pfade dual-write (SQLite + Postgres), dann Read-Switch für Analytics.
- Optionale Continuous Aggregates für tägliche Rollups.
