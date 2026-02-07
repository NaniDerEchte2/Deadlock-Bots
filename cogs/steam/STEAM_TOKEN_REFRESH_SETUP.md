# Steam Token Auto-Refresh Setup

## Übersicht

Dieses System refresht automatisch den Steam Refresh Token **alle 80 Tage** (bevor er nach 90 Tagen abläuft).

**Wie es funktioniert:**
1. ⏰ Checker läuft täglich und prüft Token-Alter
2. 🔄 Bei 80+ Tagen: Automatischer Re-Login
3. 📧 Steam sendet Guard Code per Email
4. 🤖 Bot liest Email (IMAP) und extrahiert Code
5. ✅ Submittet Code automatisch → neuer Token!

---

## Setup Schritt-für-Schritt

### 1. Python Dependency installieren

```bash
pip install keyring
```

Dies ermöglicht Windows Credential Manager Integration.

### 2. Email-Passwort im Windows Credential Manager speichern

**Option A: Via Python (empfohlen)**

```python
import keyring
keyring.set_password("DeadlockBot", "STEAM_EMAIL_ACCOUNT_PASSWORD", "DEIN_EMAIL_PASSWORT")
```

**Option B: Via PowerShell**

```powershell
$password = Read-Host "Email Passwort" -AsSecureString
$credential = New-Object System.Management.Automation.PSCredential("STEAM_EMAIL_ACCOUNT_PASSWORD", $password)
cmdkey /generic:"DeadlockBot" /user:"STEAM_EMAIL_ACCOUNT_PASSWORD" /pass:$credential.GetNetworkCredential().Password
```

**Option C: Via Umgebungsvariable (weniger sicher)**

```bash
export STEAM_EMAIL_ACCOUNT_PASSWORD="dein_passwort"
```

### 3. Weitere Env-Variablen setzen

```bash
# Pflicht
export STEAM_GUARD_EMAIL="mail@steam.earlysalty.com"
export STEAM_BOT_USERNAME="dein_steam_username"
export STEAM_BOT_PASSWORD="dein_steam_passwort"

# Optional (Defaults sind OK)
export STEAM_TOKEN_REFRESH_DAYS="80"           # Token-Refresh nach N Tagen
export STEAM_GUARD_IMAP_SERVER="imap.ionos.de"  # Für Deutschland: .de statt .com
export STEAM_GUARD_IMAP_PORT="993"
export STEAM_GUARD_POLL_INTERVAL="10"          # Email-Check Intervall (Sekunden)
export STEAM_GUARD_MAX_EMAIL_AGE="5"           # Max Email-Alter (Minuten)
```

### 4. Bot neu starten

```bash
python main.py
```

**Logs prüfen:**
```
Steam Guard automation configured for mail@steam.earlysalty.com
Token refresh scheduled every 80 days
```

---

## Manuell testen

### Token-Alter prüfen:

```python
from pathlib import Path
from datetime import datetime

token_path = Path('cogs/steam/steam_presence/.steam-data/refresh.token')
if token_path.exists():
    mtime = datetime.fromtimestamp(token_path.stat().st_mtime)
    age = (datetime.now() - mtime).days
    print(f"Token age: {age} days")
```

### Manueller Refresh (Discord):

```
!steam_token_refresh
```

Dies triggert den kompletten Refresh-Flow:
1. Logout
2. Delete Token
3. Re-Login (sendet Email)
4. Bot monitort Email
5. Extrahiert Code
6. Submittet automatisch

---

## Troubleshooting

### Email wird nicht abgerufen

**Check 1: IMAP Credentials**
```python
import imaplib
mail = imaplib.IMAP4_SSL("imap.ionos.com", 993)
mail.login("mail@steam.earlysalty.com", "dein_passwort")
print("✅ IMAP Login successful")
```

**Check 2: Keyring**
```python
import keyring
password = keyring.get_password("DeadlockBot", "STEAM_EMAIL_ACCOUNT_PASSWORD")
print("Password from Credential Manager:", password)
```

**Check 3: Logs**
```bash
tail -f logs/bot.log | grep "Steam Guard"
```

### Token wird nicht refresht

**Check Token-Alter:**
- Muss >= 80 Tage sein
- Oder manuell mit `!steam_token_refresh` triggern

**Check DB State:**
```sql
SELECT payload FROM standalone_bot_state WHERE bot = 'steam';
-- Prüfe: guard_required.type == 'email'
```

### Code nicht extrahiert

**Email-Format prüfen:**

Steam sendet Emails mit Text wie:
```
Your Steam Guard code is: ABC12
```

Regex Pattern:
```python
import re
pattern = re.compile(r'(?:Your Steam Guard code is[:\s]+|Guard code[:\s]+)([A-Z0-9]{5})', re.IGNORECASE)
# Test mit Email-Body
```

---

## Sicherheit

⚠️ **Wichtig:**

1. **Email-Passwort:** Speichere im Windows Credential Manager (nicht in `.env`)
2. **Steam-Passwort:** Umgebungsvariable ist OK (nur auf Server)
3. **Nie committen:** `.env` in `.gitignore`
4. **App-Passwort:** Falls 2FA auf Email aktiv, nutze App-Passwort

---

## Weitere Befehle

```
!steam_login                  # Manueller Login
!steam_guard ABC12            # Manueller Guard Code
!steam_token_clear            # Token löschen
!steam_token_refresh          # Refresh triggern
```

---

## Architektur

```
┌─────────────────────────────────────────────┐
│  schedule_token_refresh.loop (daily)        │
│  ├─ Check token age                         │
│  └─ If >= 80 days: trigger refresh          │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│  _trigger_token_refresh()                   │
│  ├─ AUTH_LOGOUT task                        │
│  ├─ Delete refresh.token file               │
│  └─ AUTH_LOGIN task (with credentials)      │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│  Steam Bridge (Node.js)                     │
│  └─ Fires 'steamGuard' event                │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│  check_steam_guard.loop (10s)               │
│  ├─ Detect guard_required.type == 'email'   │
│  ├─ Poll IONOS IMAP                         │
│  ├─ Extract code via regex                  │
│  └─ Submit AUTH_GUARD_CODE task             │
└─────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│  Steam Bridge accepts code                  │
│  └─ Stores new refresh.token (age = 0)      │
└─────────────────────────────────────────────┘
```

---

## Monitoring

**Logs to watch:**
```bash
# Token age check
"Token age: X days (refresh at 80 days)"

# Refresh triggered
"⚠️ Token is X days old - triggering automatic refresh"

# Email monitoring started
"🔍 Steam Guard (email) is pending - starting email monitoring"

# Code found
"✅ Found Steam Guard code: ABC12"

# Success
"✅ Token refresh completed successfully!"
```

---

**Fragen? Bugs?**
Check logs in `logs/bot.log` oder Discord Debug-Channel.
