Alles klar — hier ist die **vollständig überarbeitete README**, logisch sortiert, inkl. **NSSM als Pflicht** für den Caddy-Dienst. Ich habe dir außerdem einen **einfügbaren Block „4.5 Bot-Watchdog (optional)“** mitgegeben, den du bei Bedarf nach 4) einsetzen kannst.

---

# Steam Link (Discord + Steam OpenID) – Setup & Betrieb (Windows + Caddy/NSSM)

Dieser Dienst ermöglicht es Usern, ihren Steam-Account mit dem Discord-Account zu verknüpfen.

Er besteht aus:

* einem **Bot-Prozess** (Discord Cog `SteamLink`) mit eingebautem **AioHTTP-Callback-Server** auf `127.0.0.1:8888`
* einem **Reverse-Proxy (Caddy)**, der die öffentliche Subdomain **`link.earlysalty.com`** auf den Bot weiterleitet und TLS terminiert

> **Wichtig:** Alle **Secrets/Passwörter** kommen **in die ENV**. Konfigurationen (Pfadnamen, Ports, Labels) können im Bot-Code bleiben.

---

## TL;DR (Kurzfassung)

1. **DNS**: `link.earlysalty.com` → öffentliche IP des Windows-Servers.
2. **Installieren**: `choco install caddy nssm -y`.
3. **Caddy vorbereiten**: `C:\caddy\{Caddyfile,logs,data,config}` anlegen, **XDG-Variablen** setzen.
4. **Firewall** öffnen (80/443).
5. **Caddy als Windows-Dienst via NSSM** registrieren (mit `--watch` & XDG-Variablen).
6. **Bot-ENV** setzen und **Bot starten**.
7. **Discord Developer Portal**: Redirect `https://link.earlysalty.com/discord/callback`.
8. **Tests**: `/health` → 200, `/steam/return?state=abc` → 400.
9. (Optional **4.5**) **Bot-Watchdog**, der Ausfälle in deinen Log-Channel meldet.

---

## Voraussetzungen

* Windows Server (PowerShell 7 ok), Adminrechte
* **Caddy** (über Chocolatey), **NSSM** (Windows-Dienstmanager)
* Python 3.10+ (für den Bot)

---

## 1) DNS

* **`link.earlysalty.com`** als A/AAAA-Record auf die Server-IP zeigen lassen.

---

## 2) Software installieren

```powershell
# Als Administrator
choco install caddy nssm -y
```

* Die `caddy.exe` liegt danach i. d. R. unter
  `C:\ProgramData\chocolatey\bin\caddy.exe`

---

## 3) Caddy vorbereiten (Ordner, XDG, Logs)

```powershell
mkdir C:\caddy\logs  -Force
mkdir C:\caddy\data  -Force
mkdir C:\caddy\config -Force

# Caddy State/Certs/Autosave zentral ablegen (nicht im Benutzerprofil):
setx XDG_DATA_HOME   "C:\caddy\data"   /M
setx XDG_CONFIG_HOME "C:\caddy\config" /M

# Schreibrechte für Logs (SYSTEM = Dienst, du = manuell)
icacls C:\caddy\logs /inheritance:e
icacls C:\caddy\logs /grant "NT AUTHORITY\SYSTEM:(OI)(CI)(F)" /T
icacls C:\caddy\logs /grant "$env:USERNAME:(OI)(CI)M" /T
```

> **Hinweis:** Wenn du Caddy vorher schon mal manuell gestartet hast, kannst du (optional) vorhandene Daten nach `C:\caddy\data\Caddy` kopieren:
>
> ```powershell
> robocopy "$env:APPDATA\Caddy" "C:\caddy\data\Caddy" /E
> ```

---

## 4) Caddy konfigurieren (Caddyfile + Firewall + Dienst)

### 4.1 Caddyfile anlegen

Speichere **`C:\caddy\Caddyfile`** mit folgendem Inhalt:

```caddy
{
  email admin@earlysalty.de
}

# --- www -> non-www Redirect (holt eigenes Zertifikat, dann 308 Redirect) ---
www.earlysalty.de {
  tls {
    issuer acme {
      email admin@earlysalty.de
      disable_http_challenge
    }
  }
  redir https://earlysalty.de{uri} 308
}

# --- Hauptseite (optional; entfernen, wenn ungenutzt) ---
earlysalty.de {
  encode zstd gzip
  tls {
    issuer acme {
      email admin@earlysalty.de
      disable_http_challenge
    }
  }
  header {
    Strict-Transport-Security "max-age=31536000"
    Referrer-Policy "no-referrer"
    X-Content-Type-Options "nosniff"
    X-Frame-Options "DENY"
    Content-Security-Policy "
      default-src 'self';
      script-src 'self';
      style-src 'self' 'unsafe-inline';
      img-src 'self' data:;
      font-src 'self' data:;
      connect-src 'self';
      base-uri 'none';
      frame-ancestors 'none'
    "
  }
  @health path /health
  respond @health 200
  reverse_proxy 127.0.0.1:4888

  log {
    output file C:/caddy/logs/earlysalty.access.log {
      roll_size 10MiB
      roll_keep 5
      roll_keep_for 720h
    }
    format json
  }
}

# --- Link-Subdomain: öffentlich -> Bot (127.0.0.1:8888) ---
link.earlysalty.com {
  encode zstd gzip
  tls {
    issuer acme {
      email admin@earlysalty.de
      disable_http_challenge
    }
  }

  # Security für die Link-Seite
  header {
    Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
    Referrer-Policy "no-referrer"
    X-Content-Type-Options "nosniff"
    X-Frame-Options "DENY"
    Content-Security-Policy "default-src 'none'; style-src 'unsafe-inline'; form-action https://steamcommunity.com; base-uri 'none'; frame-ancestors 'none'"
    X-Robots-Tag "noindex, nofollow"
  }

  # Root -> Health (praktisch für schnelle Checks)
  @root path /
  redir @root /health 302

  @health path /health
  respond @health 200

  reverse_proxy 127.0.0.1:8888

  log {
    output file C:/caddy/logs/link.access.log {
      roll_size 10MiB
      roll_keep 5
      roll_keep_for 720h
    }
    format json
  }
}
```

### 4.2 Firewall öffnen (einmalig)

```powershell
netsh advfirewall firewall add rule name="Caddy HTTP 80"  dir=in action=allow protocol=TCP localport=80
netsh advfirewall firewall add rule name="Caddy HTTPS 443" dir=in action=allow protocol=TCP localport=443
```

### 4.3 Caddy als **Windows-Dienst via NSSM** registrieren (**Pflicht**)

> **Wichtig:** Falls Caddy noch manuell läuft → zuerst beenden (`CTRL+C`).

```powershell
# Dienst anlegen
nssm install Caddy "C:\ProgramData\chocolatey\bin\caddy.exe" run --config "C:\caddy\Caddyfile" --watch
nssm set Caddy AppDirectory "C:\caddy"
nssm set Caddy Start SERVICE_AUTO_START
nssm set Caddy AppStdout "C:\caddy\logs\service.out.log"
nssm set Caddy AppStderr "C:\caddy\logs\service.err.log"

# XDG-Variablen im Dienst erzwingen (damit Certs/State immer unter C:\caddy\... landen)
nssm set Caddy AppEnvironmentExtra "XDG_DATA_HOME=C:\caddy\data"
nssm set Caddy AppEnvironmentExtra "XDG_CONFIG_HOME=C:\caddy\config"

# Starten & prüfen
nssm start Caddy
sc query Caddy
```

### 4.4 Funktionstest HTTP(S)

```powershell
# Konfig prüfen (optional)
"C:\ProgramData\chocolatey\bin\caddy.exe" validate --config C:\caddy\Caddyfile

# Health Check über Internet:
Invoke-WebRequest https://link.earlysalty.com/health | Select-Object StatusCode
# Erwartet: 200
```

### 4.5 (OPTIONAL – EINFÜGBARE STELLE) Bot-Watchdog (Discord Log-Channel)

> **Füge diesen Unterpunkt genau hier ein**, wenn du den Watchdog verwenden willst.

* Zweck: `/health` wird periodisch gecheckt; **DOWN/UP** wird in deinen Bot-Log-Channel gepostet.
* Datei: `cogs/caddy_watchdog.py` in deinem Bot-Repo.
* ENV:

  * `LOG_CHANNEL_ID=<dein_discord_channel_id>` (Pflicht)
  * `CADDY_HEALTH_URL=https://link.earlysalty.com/health` (optional; Default passt)
  * `CADDY_CHECK_INTERVAL_SEC=60`, `CADDY_ALERT_EVERY_MIN=30` (optional)
* Slash-Cmd: `/caddy_status`
* Bot neu starten, **Auto-Discovery** lädt die Cog.

---

## 5) Bot – ENV & Start

**.env Beispiel** (Werte anpassen; **Secrets nie committen**):

```bash
# Öffentliche Basis-URL (muss 1:1 passen; Änderung → Bot neu starten)
PUBLIC_BASE_URL=https://link.earlysalty.com

# Optional: Pfad für den Steam-Return (Default passt)
STEAM_RETURN_PATH=/steam/return

# AioHTTP Callback-Server
HTTP_HOST=127.0.0.1
STEAM_OAUTH_PORT=8888
# (Alternativ: HTTP_PORT=8888 – Code liest erst STEAM_OAUTH_PORT, dann HTTP_PORT.)

# Discord OAuth App
DISCORD_OAUTH_CLIENT_ID=xxxxxxxxxxxxxxxxxx
DISCORD_OAUTH_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Steam Web API Key (für Vanity/Persona; OpenID selbst braucht ihn nicht)
STEAM_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# UI (optional)
OAUTH_BUTTON_MODE=one_click
LINK_COVER_IMAGE=
LINK_COVER_LABEL=link.earlysalty.com
LINK_BUTTON_LABEL=Mit Discord verknüpfen
STEAM_BUTTON_LABEL=Bei Steam anmelden
```

**Starten** (Beispiel):

```powershell
$env:PUBLIC_BASE_URL="https://link.earlysalty.com"
$env:STEAM_RETURN_PATH="/steam/return"
$env:HTTP_HOST="127.0.0.1"
$env:STEAM_OAUTH_PORT="8888"
$env:DISCORD_OAUTH_CLIENT_ID="..."
$env:DISCORD_OAUTH_CLIENT_SECRET="..."
$env:STEAM_API_KEY="..."

python main.py
```

> **Merke:** `PUBLIC_BASE_URL`/Ports geändert? → **Bot neu starten**.

---

## 6) Discord Developer Portal

* **OAuth2 → Redirects**:
  `https://link.earlysalty.com/discord/callback` hinzufügen
* Scopes: **`identify connections`**
* Client-ID & Secret in ENV (siehe oben) setzen

---

## 7) Steam OpenID

* Kein App-Eintrag nötig.
* **`openid.return_to`** & **`openid.realm`** müssen exakt zu **`PUBLIC_BASE_URL`** passen
  (der Code baut das aus `PUBLIC_BASE_URL` + `STEAM_RETURN_PATH`).
* Outbound zu `steamcommunity.com` muss erlaubt sein.

---

## 8) Tests (PowerShell)

Lokaler Port:

```powershell
Test-NetConnection 127.0.0.1 -Port 8888
```

Über **Caddy/Internet**:

```powershell
# Health → 200
Invoke-WebRequest https://link.earlysalty.com/health -SkipCertificateCheck |
  Select-Object StatusCode,Content

# Steam Return mit Fake-State → 400 ("invalid/expired state" vom Bot)
Invoke-WebRequest 'https://link.earlysalty.com/steam/return?state=abc' `
  -MaximumRedirection 0 -SkipCertificateCheck | Select-Object StatusCode

# Steam Login HTML → 200, enthält OpenID-URL
$r = Invoke-WebRequest 'https://link.earlysalty.com/steam/login?uid=1' -SkipCertificateCheck
$r.Content -match 'steamcommunity\.com/openid/login'
```

**End-to-End (echter Flow):**

1. In Discord `/account_verknüpfen` ausführen → passenden Button klicken.
2. Discord OAuth zeigt Connections.
3. Falls keine Steam-Connection vorhanden → automatische Weiterleitung zu Steam.
4. Nach Steam-Login landest du auf `https://link.earlysalty.com/steam/return?...` (Erfolg) und der Bot schickt eine **DM**.

---

## 9) Troubleshooting (häufige Stolpersteine)

* **`invalid/expired state`** direkt nach manuellem Aufruf von `/steam/return?state=abc` → **erwartet**.
  Im echten Flow ist `state` frisch (Ablauf ~10 min). Tritt’s dort auf: Bot während des Flows neu gestartet **oder** `PUBLIC_BASE_URL` stimmt nicht.

* **IIS antwortet 404/500** → Falsches Routing.
  Prüfen: Caddy läuft, 80/443 offen, DNS korrekt, **keine** IIS-Bindings für `link.earlysalty.com`.

* **ACME/LE schlägt fehl** → Logs checken (`C:\caddy\logs\service.err.log`).
  `tls-alpn-01` braucht Port 443 inbound. Rate-Limits beachten.

* **Log-Fehler „Zugriff verweigert“** → Rechte auf `C:\caddy\logs` setzen:

  ```powershell
  icacls C:\caddy\logs /inheritance:e
  icacls C:\caddy\logs /grant "NT AUTHORITY\SYSTEM:(OI)(CI)(F)" /T
  icacls C:\caddy\logs /grant "$env:USERNAME:(OI)(CI)M" /T
  ```

* **Autosave/Certs landen im AppData** → XDG-Variablen **im Dienst** setzen:

  ```powershell
  nssm set Caddy AppEnvironmentExtra "XDG_DATA_HOME=C:\caddy\data"
  nssm set Caddy AppEnvironmentExtra "XDG_CONFIG_HOME=C:\caddy\config"
  nssm restart Caddy
  ```

* **Nach ENV-Änderung keine Wirkung** → **Bot neu starten**.
  Caddy neu laden nur bei Caddyfile-Änderungen nötig (Service läuft mit `--watch`).

---

## 10) Betrieb & Wartung

* **Status prüfen**

  ```powershell
  sc query Caddy
  ```
* **Logs live ansehen**

  ```powershell
  Get-Content C:\caddy\logs\service.out.log -Wait
  Get-Content C:\caddy\logs\service.err.log -Wait
  ```
* **Dienst Neustart**

  ```powershell
  nssm restart Caddy
  ```
* **Beim Boot starten**: Ist per `SERVICE_AUTO_START` aktiv.
* **Port-Konflikte prüfen**

  ```powershell
  netstat -ano | findstr :80
  netstat -ano | findstr :443
  ```

---

## 11) Ordnerstruktur (Beispiel)

```
C:\caddy\
  Caddyfile
  logs\
  data\
  config\

C:\apps\deadlock\
  main.py
  cogs\steam\steam_link_oauth.py
  # optional (Watchdog):
  cogs\caddy_watchdog.py
  shared\db.py
  .env
```

---

## 12) Checkliste „Bereit für Prod“

* [ ] DNS: `link.earlysalty.com` → Server-IP
* [ ] Caddy als **Dienst** via NSSM (RUNNING), Zertifikate aktiv
* [ ] `XDG_DATA_HOME`/`XDG_CONFIG_HOME` im **Dienst** gesetzt
* [ ] `PUBLIC_BASE_URL=https://link.earlysalty.com` gesetzt
* [ ] Discord OAuth Redirect konfiguriert
* [ ] `DISCORD_OAUTH_CLIENT_ID` / `DISCORD_OAUTH_CLIENT_SECRET` in ENV
* [ ] `STEAM_API_KEY` in ENV (empfohlen)
* [ ] `HTTP_HOST=127.0.0.1`, `STEAM_OAUTH_PORT=8888` gesetzt, Port frei
* [ ] Health/Return/Login-Tests grün
* [ ] DM kommt nach erfolgreichem Link-Flow
* [ ] (Optional) Watchdog aktiv & meldet DOWN/UP in Log-Channel
