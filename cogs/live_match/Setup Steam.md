
# Steam Link (Discord + Steam OpenID) – Setup & Betrieb (Windows + Caddy)

Dieser Dienst ermöglicht es Usern, ihren Steam-Account mit dem Discord-Account zu verknüpfen.
Er besteht aus:

* einem **Bot-Prozess** (Discord Cog `SteamLink`) mit eingebautem **AioHTTP-Callback-Server** auf `127.0.0.1:8888`
* einem **Reverse-Proxy (Caddy)**, der die öffentliche Subdomain **`link.earlysalty.com`** auf den Bot weiterleitet und TLS terminiert

> **Wichtig:** Alle **Secrets/Passwörter** kommen **in die ENV**. Konfigurationen (Pfadnamen, Ports, Labels) können gern im Bot-Code bleiben – so ist dieser Guide geschrieben.

---

## TL;DR (Kurzfassung)

1. **DNS**: `link.earlysalty.com` → öffentliche IP des Windows-Servers.
2. **Caddy** installieren und mit untenstehendem **Caddyfile** starten.
3. **ENV** für den Bot setzen (siehe `.env.example`), **Bot starten/neu starten**.
4. **Discord Developer Portal**: Redirect‐URI `https://link.earlysalty.com/discord/callback` hinterlegen.
5. **Tests**: `https://link.earlysalty.com/health` → 200,
   `https://link.earlysalty.com/steam/return?state=abc` → 400 (*invalid/expired state*).
6. In Discord `/link` ausführen → happy path.

---

## Voraussetzungen

* Windows Server (PowerShell 7 ok)
* Administratorrechte
* `caddy.exe` (in `C:\caddy`)
* Python 3.10+ (für den Bot)
* Optional: **NSSM** zum Installieren als Windows-Dienst

---

## 1) DNS

* **`link.earlysalty.com`** als A/AAAA-Record auf den Server zeigen lassen

---

## 2) Caddy konfigurieren

Lege `C:\caddy\Caddyfile` an (du hast schon eine funktionsfähige; diese passt 1:1):

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

# --- Hauptseite (nur der Vollständigkeit halber; kann entfernt werden, falls nicht genutzt) ---
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

Firewall öffnen (einmalig):

```powershell
netsh advfirewall firewall add rule name="Caddy HTTP 80"  dir=in action=allow protocol=TCP localport=80
netsh advfirewall firewall add rule name="Caddy HTTPS 443" dir=in action=allow protocol=TCP localport=443
```

Starten:

```powershell
cd C:\caddy
caddy validate --config Caddyfile
caddy start --config Caddyfile --watch
```

> Tipp: Du kannst Caddy später als Dienst via **NSSM** installieren (`nssm install Caddy "C:\caddy\caddy.exe" "run --config Caddyfile --watch"`).

---

## 3) Bot – ENV & Start

**.env Beispiel** (Werte anpassen; **Secrets NIE committen**):

```bash
# Öffentliche Basis-URL des Link-Servers (GENAU diese Domain; ändert sich → Bot neu starten)
PUBLIC_BASE_URL=https://link.earlysalty.com

# Optional: Pfad für den Steam-Return (Default passt)
STEAM_RETURN_PATH=/steam/return

# AioHTTP Callback-Server
HTTP_HOST=127.0.0.1
STEAM_OAUTH_PORT=8888
# (Alternativ: HTTP_PORT=8888 – der Code liest erst STEAM_OAUTH_PORT, dann HTTP_PORT.)

# Discord OAuth App
DISCORD_OAUTH_CLIENT_ID=xxxxxxxxxxxxxxxxxx
DISCORD_OAUTH_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Steam Web API Key (für Vanity-Resolve + Persona; OpenID selbst braucht ihn nicht, aber sehr sinnvoll)
STEAM_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Button/UI (optional)
OAUTH_BUTTON_MODE=one_click          # oder two_step
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

# dann euren Bot starten, z.B.:
python main.py
```

> **Merke:** Wenn du `PUBLIC_BASE_URL` oder Ports änderst → **Bot-Prozess neu starten**. Diese Werte werden beim Laden benutzt.

---

## 4) Discord Developer Portal

In deiner Anwendung:

* **OAuth2 → Redirects**:
  `https://link.earlysalty.com/discord/callback` hinzufügen
* Scopes, die der Code nutzt: **`identify connections`**
* Client-ID & Secret aus der App in ENV setzen (s.o.)

---

## 5) Steam OpenID

* Kein eigenes App-Eintragen nötig.
* Wichtig ist, dass **`openid.return_to`** und **`openid.realm`** exakt zu **`PUBLIC_BASE_URL`** passen (der Code baut das automatisch aus `PUBLIC_BASE_URL` + `STEAM_RETURN_PATH`).
* Der Bot prüft via OpenID Verify; `steamcommunity.com` muss erreichbar sein (Server outbound).

---

## 6) Tests (PowerShell)

Lokaler Port:

```powershell
Test-NetConnection 127.0.0.1 -Port 8888
```

Über **Caddy/Internet**:

```powershell
# Health → 200 und Content {"ok":true,...}
Invoke-WebRequest https://link.earlysalty.com/health -SkipCertificateCheck |
  Select-Object StatusCode,Content

# Steam Return mit Fake-State → 400 "invalid/expired state" (vom Bot)
Invoke-WebRequest 'https://link.earlysalty.com/steam/return?state=abc' `
  -MaximumRedirection 0 -SkipCertificateCheck | Select-Object StatusCode

# Steam Login HTML → 200, enthält steamcommunity OpenID URL
$r = Invoke-WebRequest 'https://link.earlysalty.com/steam/login?uid=1' -SkipCertificateCheck
$r.Content -match 'steamcommunity\.com/openid/login'
```

**End-to-End (echter Flow):**

1. In Discord `/link` ausführen → Button klicken.
2. Discord OAuth zeigt Connections.
3. Falls keine Steam-Connection vorhanden → automatische Weiterleitung zu Steam.
4. Nach Steam-Login landest du auf
   `https://link.earlysalty.com/steam/return?...` (Erfolgstext)
   und der Bot schickt eine **DM**.

---

## 7) Troubleshooting (häufige Stolpersteine)

* **`invalid/expired state`** direkt nach Return:
  Das ist korrekt, **wenn du manuell** auf `/steam/return?state=abc` gehst.
  Im echten Flow stellt der Bot einen frischen `state`; Ablaufzeit: 10 min.
  Wenn es **im echten Flow** passiert → Bot wurde während des Flows neu gestartet **oder** du nutzt eine andere Domain als `PUBLIC_BASE_URL`.

* **404/500 von IIS statt 400 vom Bot**:
  Bedeutet, die Anfrage ist **falsch geroutet** (landet bei IIS).
  → Prüfe Caddy läuft, Firewall 80/443 offen, DNS zeigt korrekt, **keine** IIS-Bindings für `link.earlysalty.com`.

* **Let's Encrypt schlägt fehl**:
  Caddy-Logs anschauen. Port 443 muss offen sein (hier ACME `tls-alpn-01`).

* **Discord „Invalid Redirect“**:
  Redirect‐URI **exakt** so im Portal: `https://link.earlysalty.com/discord/callback`.

* **Steam Vanity/Persona fehlen**:
  `STEAM_API_KEY` fehlt oder falsch → OpenID klappt trotzdem, aber Vanity→ID-Resolve und Persona-Abruf gehen dann nicht.
  Der Code fällt auf einen Discord-\@Namen zurück.

* **Nach ENV-Änderung keine Wirkung**:
  Bot **neu starten**. Caddy brauchst du nur bei Caddyfile-Änderungen neu zu laden.

---

## 8) Betrieb als Windows-Dienst (optional)

Mit **NSSM** (empfohlen):

```powershell
# Caddy als Dienst
nssm install Caddy "C:\caddy\caddy.exe" run --config Caddyfile --watch
nssm set Caddy AppDirectory "C:\caddy"
nssm start Caddy

# Bot als Dienst (Beispiel)
nssm install DeadlockBot "C:\Python311\python.exe" "C:\apps\deadlock\main.py"
nssm set DeadlockBot AppDirectory "C:\apps\deadlock"
# ENVs setzen (mehrfach 'set' aufrufen)
nssm set DeadlockBot AppEnvironmentExtra "PUBLIC_BASE_URL=https://link.earlysalty.com"
nssm set DeadlockBot AppEnvironmentExtra "STEAM_OAUTH_PORT=8888"
...
nssm start DeadlockBot
```

---

## 9) Ordnerstruktur (Beispiel)

```
C:\caddy\
  Caddyfile
  logs\

C:\apps\deadlock\
  main.py
  cogs\live_match\steam_link_oauth.py
  shared\db.py            # <– eure DB-Abstraktion (SQLite o.ä.), Schreibrechte beachten
  .env                    # (nur lokal, nicht commiten)
```

---

## 10) Checkliste „Bereit für Prod“

* [ ] DNS: `link.earlysalty.com` → Server-IP
* [ ] Caddy läuft, Zertifikat aktiv (Logs ok)
* [ ] `PUBLIC_BASE_URL=https://link.earlysalty.com` gesetzt
* [ ] Discord OAuth Redirect konfiguriert
* [ ] `DISCORD_OAUTH_CLIENT_ID` / `DISCORD_OAUTH_CLIENT_SECRET` in ENV
* [ ] `STEAM_API_KEY` in ENV (empfohlen)
* [ ] `HTTP_HOST=127.0.0.1`, `STEAM_OAUTH_PORT=8888` gesetzt, Port frei
* [ ] Bot zuletzt **nach** ENV-Anpassung neu gestartet
* [ ] Health/Return/Login-Tests grün
* [ ] DM kommt nach erfolgreichem Link-Flow

