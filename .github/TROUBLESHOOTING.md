# 🔧 Troubleshooting Guide - Security & Quality Suite

## 🎯 Häufige Fehler & Lösungen

### ❌ "Workflow failed" - Allgemein

**Problem**: Ein oder mehrere Workflows schlagen fehl

**Ursachen**:
1. Fehlende Dateien (z.B. requirements.txt, package.json, Dockerfile)
2. Tool-Installation schlägt fehl
3. Syntax-Fehler in Code
4. Berechtigungsprobleme

**Lösung**:
```bash
# 1. Prüfe die Workflow-Logs in GitHub Actions
#    → Klicke auf den fehlgeschlagenen Workflow
#    → Schaue dir die Fehler-Details an

# 2. Die meisten Workflows haben continue-on-error: true
#    → Einzelne Fehler sollten den Workflow nicht komplett stoppen
#    → Prüfe ob wichtige Jobs erfolgreich waren

# 3. Wenn ein Job übersprungen wurde (skipped):
#    → Das ist NORMAL und bedeutet, dass die entsprechenden Dateien nicht gefunden wurden
#    → z.B. Python-Scans werden übersprungen wenn keine .py Files existieren
```

---

### ⏭️ "Job skipped" - Jobs werden übersprungen

**Problem**: Jobs werden als "skipped" angezeigt

**Erklärung**: Das ist **NORMAL** und kein Fehler! 

Die optimierten Workflows prüfen zuerst, ob die entsprechenden Dateien vorhanden sind:

- **Python Security** → Läuft nur wenn `.py` Dateien existieren
- **JavaScript Security** → Läuft nur wenn `.js/.ts` Dateien existieren  
- **Container Security** → Läuft nur wenn `Dockerfile` existiert
- **IaC Security** → Läuft nur wenn Terraform/K8s Dateien existieren

**Das ist gewollt**, um unnötige Scans zu vermeiden!

---

### 🐍 Python Workflow Fehler

#### "pip install failed"

**Problem**: Python-Tools können nicht installiert werden

**Lösung**:
```bash
# Erstelle requirements.txt wenn nicht vorhanden:
echo "# Project dependencies" > requirements.txt

# Oder füge grundlegende Tools hinzu:
cat > requirements-dev.txt << EOF
bandit
safety
ruff
black
radon
EOF
```

#### "No module named 'X'"

**Problem**: Python-Modul fehlt

**Lösung**:
```bash
# In deinem Projekt:
pip install -r requirements.txt

# Oder installiere fehlendes Modul:
pip install <module-name>
```

---

### 📦 JavaScript/Node Workflow Fehler

#### "npm audit failed"

**Problem**: NPM Audit findet Vulnerabilities

**Lösung**:
```bash
# Lokal fixen:
npm audit fix

# Oder nur production dependencies prüfen:
npm audit --production

# Vulnerabilities akzeptieren (temporär):
npm audit --audit-level=high  # Nur high/critical
```

#### "Package-lock.json not found"

**Problem**: Workflow erwartet package-lock.json

**Lösung**:
```bash
# Generiere package-lock.json:
npm install

# Committe die Datei:
git add package-lock.json
git commit -m "Add package-lock.json"
```

---

### 🐳 Container Workflow Fehler

#### "No Dockerfile found"

**Problem**: Container-Scan läuft, aber Dockerfile fehlt

**Erklärung**: Der Workflow prüft das jetzt automatisch und überspringt den Scan

**Lösung**: Kein Action nötig - der Job wird einfach übersprungen

Wenn du Container-Scans aktivieren möchtest:
```bash
# Erstelle ein Dockerfile:
cat > Dockerfile << EOF
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
CMD ["python", "app.py"]
EOF
```

---

### 🏗️ IaC Security Fehler

#### "No IaC files detected"

**Problem**: IaC-Scan findet keine Terraform/K8s Dateien

**Erklärung**: Auch hier - wenn keine IaC-Dateien vorhanden sind, werden die Scans übersprungen

**Das ist OK!** Nicht jedes Projekt braucht IaC.

---

### ⚡ Performance Analysis Fehler

#### "Tool not found" oder "Command failed"

**Problem**: Performance-Tools können nicht ausgeführt werden

**Lösung**: Die meisten Tools haben `continue-on-error: true`, also:
- Workflow läuft weiter
- Andere Tools werden trotzdem ausgeführt
- Prüfe Artifacts für Ergebnisse

---

### ✅ Compliance Check Fehler

#### "Black/Ruff failed"

**Problem**: Code ist nicht formatiert

**Lösung**:
```bash
# Lokal installieren:
pip install black ruff

# Code formatieren:
black .
ruff check --fix .

# Committen:
git add .
git commit -m "style: format code with black and ruff"
```

---

## 🔍 Debug-Strategien

### 1. Workflow-Logs lesen

```
GitHub → Actions Tab → Fehlgeschlagener Workflow → Klick auf Job → Schaue Log
```

Achte auf:
- ❌ Rote "Error" Meldungen
- ⚠️ Gelbe "Warning" Meldungen  
- ℹ️ Blaue Info-Meldungen

### 2. Artifacts prüfen

Auch wenn ein Workflow fehlschlägt, werden oft Artifacts hochgeladen:

```
GitHub → Actions → Workflow Run → Scroll nach unten → Artifacts
```

**Download** die Reports um Details zu sehen!

### 3. Lokal testen

Viele Tools kannst du lokal ausführen:

```bash
# Python
bandit -r . -ll
safety check
ruff check .

# JavaScript
npm audit
retire --path .

# Docker
docker run --rm -i hadolint/hadolint < Dockerfile
```

---

## 🚨 Kritische Probleme

### "Permission denied" Fehler

**Problem**: Workflow hat keine Berechtigungen

**Lösung**:
```yaml
# In der Workflow-Datei sollte stehen:
permissions:
  contents: read
  security-events: write
  actions: read
```

Prüfe: `Settings → Actions → General → Workflow permissions`
- ✅ "Read and write permissions" sollte aktiviert sein

### "Rate limit exceeded"

**Problem**: Zu viele API-Requests

**Lösung**:
- Warte 1 Stunde
- Reduziere Workflow-Frequenz
- Nutze `schedule` statt `push` für häufige Commits

---

## 📊 Erwartete Ergebnisse

### Nach dem ersten erfolgreichen Run:

✅ **Normal**:
- Einige Jobs sind "skipped" (wenn Dateien fehlen)
- Warnings in Code-Quality Scans
- Einige Low/Medium Severity Findings

⚠️ **Aufmerksamkeit erforderlich**:
- High Severity Dependencies
- Hardcoded Secrets
- Critical Security Issues

🚨 **Sofort beheben**:
- Secrets im Code
- Critical CVEs
- Sensitive Files committed

---

## 💡 Performance-Tipps

### Workflows beschleunigen

1. **Nutze `paths` Filter**:
```yaml
on:
  push:
    paths:
      - '**.py'
      - 'requirements.txt'
```

2. **Reduziere Scan-Frequenz**:
```yaml
schedule:
  - cron: '0 2 * * 1'  # Nur Montags statt täglich
```

3. **Nutze Caching**:
```yaml
- uses: actions/cache@v4
  with:
    path: ~/.cache/pip
    key: ${{ hashFiles('requirements.txt') }}
```

---

## 🆘 Immer noch Probleme?

### 1. Prüfe die Dokumentation

- [QUICK_START.md](./QUICK_START.md)
- [SECURITY_SUITE_README.md](./SECURITY_SUITE_README.md)
- [CONFIGURATION_GUIDE.md](./CONFIGURATION_GUIDE.md)

### 2. Prüfe GitHub Docs

- [GitHub Actions](https://docs.github.com/en/actions)
- [Security Features](https://docs.github.com/en/code-security)

### 3. Erstelle ein Issue

Wenn du einen Bug findest, erstelle ein Issue mit:
- Workflow-Name
- Fehler-Message (aus Logs)
- Deine Projekt-Struktur (Python? Node? Docker?)

---

## ✅ Checkliste für erfolgreiche Workflows

Nach dem Push, prüfe:

- [ ] Workflows erscheinen im Actions Tab
- [ ] Mindestens 1 Workflow läuft erfolgreich
- [ ] Security Tab zeigt keine kritischen Issues
- [ ] Artifacts werden erstellt (falls Jobs laufen)
- [ ] "Skipped" Jobs sind OK (wenn Dateien fehlen)

**Wichtig**: Nicht alle Workflows müssen "grün" sein beim ersten Run!
- Skipped Jobs = OK
- Failed Jobs mit `continue-on-error` = OK
- Kritische Errors = Beheben

---

## 📞 Spezifische Tool-Fehler

### Bandit
```bash
# Lokal testen:
bandit -r . -ll

# Bestimmte Tests überspringen:
bandit -r . -ll -s B101,B601
```

### Safety
```bash
# Lokal testen:
safety check

# Mit Details:
safety check --full-report
```

### Trivy
```bash
# Filesystem scannen:
trivy fs .

# Nur HIGH/CRITICAL:
trivy fs --severity HIGH,CRITICAL .
```

### Semgrep
```bash
# Lokal installieren:
pip install semgrep

# Scan starten:
semgrep --config=auto .
```

---

**Brauchst du weitere Hilfe? Öffne ein Issue mit Details zu deinem Problem!**

*Letzte Aktualisierung: 2025-02-08*
