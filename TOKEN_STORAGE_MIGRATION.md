# Token Storage Migration Strategy

## Overview
Alle Bot-Tokens werden von Plaintext auf AES-256-GCM verschluesselte Speicherung migriert.

**Betroffene Tabellen:**
- `twitch_raid_auth` (Twitch OAuth Tokens)
- `social_media_platform_auth` (TikTok, YouTube, Instagram OAuth Tokens)

---

## Current Status (2026-02-18)

### Phase 1: COMPLETED ✅
- Master Key generiert und im Windows Credential Manager gespeichert
- Encryption Module `service/field_crypto.py` implementiert
- Schema erweitert mit `_enc` Spalten
- Migration aller 17 existierenden Twitch Tokens nach `twitch_raid_auth.*_enc`

### Phase 2: IN PROGRESS 🚧
- Social Media OAuth Implementation
- Token Auto-Refresh Worker

---

## Token Storage Tables

### 1. twitch_raid_auth (Twitch OAuth)

**Spalten:**
```sql
-- ALTE Spalten (Plaintext, deprecated):
access_token TEXT          -- ⚠️ WIRD NICHT MEHR VERWENDET (nach Migration)
refresh_token TEXT         -- ⚠️ WIRD NICHT MEHR VERWENDET (nach Migration)

-- NEUE Spalten (Encrypted):
access_token_enc BLOB      -- ✅ AES-256-GCM verschluesselt
refresh_token_enc BLOB     -- ✅ AES-256-GCM verschluesselt
enc_version INTEGER        -- Encryption schema version (1)
enc_kid TEXT               -- Key ID ('v1')
enc_migrated_at TEXT       -- Timestamp der Migration
```

**Status:** 17 Tokens migriert (2026-02-18)

**Dual-Write Strategy:**
```
┌─────────────────┐
│ Phase 1         │  Schema-Erweiterung
│ Schema Deploy   │  + _enc Spalten hinzugefuegt
└─────────────────┘
         ↓
┌─────────────────┐
│ Phase 2         │  Voruebergehend BEIDE Spalten schreiben:
│ Dual Write      │  - access_token (plaintext, DEPRECATED)
│ (AKTUELL)       │  - access_token_enc (encrypted, PRIMARY)
└─────────────────┘
         ↓
┌─────────────────┐
│ Phase 3         │  Migration existierender Tokens
│ Backfill        │  ✅ COMPLETED (17 tokens)
└─────────────────┘
         ↓
┌─────────────────┐
│ Phase 4         │  Nur noch aus *_enc lesen
│ Prefer Enc      │  - Fallback zu plaintext wenn enc NULL
└─────────────────┘
         ↓
┌─────────────────┐
│ Phase 5         │  NUR noch encrypted
│ Require Enc     │  - Kein Fallback mehr
│ (ZIEL)          │  - access_token/refresh_token = NULL setzen
└─────────────────┘
         ↓
┌─────────────────┐
│ Phase 6         │  Optional: Plaintext Spalten droppen
│ Cleanup         │  ALTER TABLE ... DROP COLUMN access_token
└─────────────────┘
```

---

### 2. social_media_platform_auth (Social Media OAuth)

**Spalten:**
```sql
-- ALLE Tokens werden NUR encrypted gespeichert (kein Dual-Write)
platform TEXT                -- 'tiktok', 'youtube', 'instagram'
streamer_login TEXT          -- NULL = bot-global

access_token_enc BLOB        -- ✅ AES-256-GCM
refresh_token_enc BLOB       -- ✅ AES-256-GCM (optional)
client_secret_enc BLOB       -- ✅ AES-256-GCM (optional)

client_id TEXT               -- Public (nicht verschluesselt)
token_expires_at TEXT
scopes TEXT
platform_user_id TEXT
platform_username TEXT

enc_version INTEGER          -- 1
enc_kid TEXT                 -- 'v1'
```

**Status:** Neue Tabelle, keine Plaintext-Spalten

---

## Code-Anpassungen

### AKTUELL: Dual-Write für twitch_raid_auth

**Beim Token-Refresh/Neuanlage:**
```python
# In cogs/twitch/raid/manager.py oder ähnlich

from service.field_crypto import get_crypto

crypto = get_crypto()

# AAD Format: table|column|row_id|version
aad_access = f"twitch_raid_auth|access_token|{twitch_user_id}|1"
aad_refresh = f"twitch_raid_auth|refresh_token|{twitch_user_id}|1"

# Encrypt
access_enc = crypto.encrypt_field(access_token, aad_access, kid="v1")
refresh_enc = crypto.encrypt_field(refresh_token, aad_refresh, kid="v1")

# DUAL-WRITE (voruebergehend):
conn.execute(
    """
    UPDATE twitch_raid_auth
    SET access_token = ?,          -- Plaintext (DEPRECATED)
        refresh_token = ?,         -- Plaintext (DEPRECATED)
        access_token_enc = ?,      -- Encrypted (PRIMARY)
        refresh_token_enc = ?,     -- Encrypted (PRIMARY)
        enc_version = 1,
        enc_kid = 'v1'
    WHERE twitch_user_id = ?
    """,
    (access_token, refresh_token, access_enc, refresh_enc, twitch_user_id)
)
```

### Beim Token-Lesen:

**Phase 4 (Prefer Encrypted, mit Fallback):**
```python
row = conn.execute(
    "SELECT access_token, access_token_enc, enc_version FROM twitch_raid_auth WHERE twitch_user_id = ?",
    (user_id,)
).fetchone()

if row["access_token_enc"]:
    # Encrypted token vorhanden → decrypt
    aad = f"twitch_raid_auth|access_token|{user_id}|{row['enc_version']}"
    access_token = crypto.decrypt_field(row["access_token_enc"], aad)
else:
    # Fallback zu Plaintext (nur während Migration)
    access_token = row["access_token"]
    log.warning("Using plaintext token for user %s (enc_version missing)", user_id)
```

**Phase 5 (Nur Encrypted, KEIN Fallback):**
```python
row = conn.execute(
    "SELECT access_token_enc, enc_version FROM twitch_raid_auth WHERE twitch_user_id = ?",
    (user_id,)
).fetchone()

if not row or not row["access_token_enc"]:
    raise ValueError("No encrypted token found - user must re-authenticate")

aad = f"twitch_raid_auth|access_token|{user_id}|{row['enc_version']}"
access_token = crypto.decrypt_field(row["access_token_enc"], aad)
```

---

## Social Media Tokens (KEIN Dual-Write)

**Social Media Tokens werden DIREKT nur encrypted gespeichert:**

```python
# In oauth_manager.py

from service.field_crypto import get_crypto
crypto = get_crypto()

row_id = f"{platform}|{streamer_login or 'global'}"

# Encrypt
aad_access = f"social_media_platform_auth|access_token|{row_id}|1"
access_enc = crypto.encrypt_field(tokens["access_token"], aad_access, kid="v1")

aad_refresh = f"social_media_platform_auth|refresh_token|{row_id}|1"
refresh_enc = crypto.encrypt_field(tokens["refresh_token"], aad_refresh, kid="v1")

# DIRECT ENCRYPTED WRITE (kein Plaintext):
conn.execute(
    """
    INSERT INTO social_media_platform_auth
        (platform, streamer_login, access_token_enc, refresh_token_enc, ...)
    VALUES (?, ?, ?, ?, ...)
    """,
    (platform, streamer_login, access_enc, refresh_enc, ...)
)
```

---

## Migration Timeline

| Phase | Status | Datum | Aktion |
|-------|--------|-------|--------|
| 0. Encryption Foundation | ✅ DONE | 2026-02-18 | Master Key + field_crypto.py |
| 1. Schema Extend | ✅ DONE | 2026-02-18 | _enc Spalten hinzugefuegt |
| 2. Backfill Twitch | ✅ DONE | 2026-02-18 | 17 Tokens migriert |
| 3. Dual-Write | ✅ DONE | 2026-02-21 | Beendet, umgestellt auf Enc-only |
| 4. Prefer Encrypted | ✅ DONE | 2026-02-21 | Lesen nur noch aus _enc |
| 5. Require Encrypted | ✅ DONE | 2026-02-21 | Kein Fallback mehr zu Plaintext |
| 6. Cleanup | ✅ DONE | 2026-02-21 | Klartext-Spalten auf 'ENC' gesetzt |

---

## Rollback-Plan

**Falls Probleme auftreten:**

1. **Dual-Write Phase**:
   - Plaintext-Spalten sind noch vorhanden
   - Einfach Encrypted-Read auskommentieren
   - Bot verwendet wieder Plaintext

2. **Prefer Encrypted Phase**:
   - Fallback-Logik ist aktiv
   - Bei Decrypt-Fehler → automatisch Plaintext verwendet

3. **Require Encrypted Phase**:
   - Database-Backup vor Migration
   - Falls Rollback: Restore Backup
   - Oder: access_token Spalten aus Backup kopieren

---

## Security Notes

### Verschluesselung:
- **Algorithmus**: AES-256-GCM
- **Key Size**: 256 bits (32 bytes)
- **Nonce Size**: 96 bits (12 bytes, unique per encryption)
- **AAD (Associated Authenticated Data)**: Bindet Ciphertext an spezifische DB-Zeile

### AAD Format:
```
table_name|column_name|row_identifier|enc_version
```

**Beispiele:**
- `twitch_raid_auth|access_token|123456789|1`
- `social_media_platform_auth|refresh_token|tiktok|earlysalty|1`

**Warum AAD wichtig ist:**
- Verhindert Token-Kopieren zwischen Zeilen
- Manipulationen am Ciphertext werden erkannt
- GCM-Tag versagt bei falscher AAD

### Key Management:
- **Speicherort**: Windows Credential Manager
- **Key ID**: `DeadlockBot` / `DB_MASTER_KEY_V1`
- **Backup**: Passwortmanager + offline verschluesselte Datei
- **Rotation**: `enc_kid` und `enc_version` erlauben Key-Updates

---

## Testing

### Encryption Roundtrip Test:
```bash
python scripts/test_encryption.py
```

**Expected Output:**
```
[OK] Roundtrip encryption test
[OK] Unique nonces
[OK] AAD binding
[OK] Tamper detection
[OK] Invalid input handling

All tests passed!
```

### Token Migration Test:
```bash
python scripts/migrate_twitch_tokens_enc.py
```

**Expected Output:**
```
Migrating tokens from plaintext to encrypted...
[OK] Migrated: 17
[FAIL] Failed: 0
Migration complete!
```

---

## Code References

**Verschluesselung:**
- `service/field_crypto.py` - AES-256-GCM Encryption
- `scripts/generate_master_key.py` - Key Generation
- `scripts/test_encryption.py` - Test Suite

**Twitch Tokens:**
- `cogs/twitch/storage.py` - Schema mit _enc Spalten
- `cogs/twitch/raid/manager.py` - Token Refresh (TODO: Update auf Dual-Write)
- `scripts/migrate_twitch_tokens_enc.py` - Migration Script

**Social Media Tokens:**
- `cogs/twitch/social_media/oauth_manager.py` - OAuth Flow + Token Save
- `cogs/twitch/social_media/credential_manager.py` - Token Decrypt + Load
- `cogs/twitch/social_media/token_refresh_worker.py` - Auto-Refresh

---

## Next Steps (COMPLETED)

1. ✅ **Test Bot Restart** - Pruefen ob Encrypted Tokens korrekt geladen werden
2. ✅ **Test Token Refresh** - Neuer Token wird nur noch verschluesselt gespeichert
3. ✅ **Update Raid Manager** - Token-Refresh auf Encrypted-only umgestellt
4. ✅ **Test Social Media OAuth** - TikTok/YouTube/Instagram Verbindung (Enc-only)
5. ✅ **Monitor Auto-Refresh** - Token-Refresh-Worker aktiv
6. ✅ **Phase 5 Rollout** - Require Encrypted ist aktiv (Klartext gelöscht)
7. ✅ **Cleanup** - Alle Plaintext-Tokens in DB wurden mit 'ENC' überschrieben

---

**Stand:** 2026-02-21, 14:00 Uhr
**Version:** 1.1 (Full Encryption Complete)
**Letzte Aenderung:** Full migration to encrypted storage; removed dual-write and cleared plaintext tokens.
