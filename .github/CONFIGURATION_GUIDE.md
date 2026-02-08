# Security & Quality Suite - Konfigurationsguide

## 🔐 Benötigte Repository Secrets

Gehe zu: `Settings → Secrets and variables → Actions → New repository secret`

### Optional aber empfohlen:

```bash
# Snyk (für erweiterte Dependency Scanning)
SNYK_TOKEN=<dein-snyk-token>
# Kostenlos registrieren: https://snyk.io/

# FOSSA (für License Compliance)
FOSSA_API_KEY=<dein-fossa-key>
# Kostenlos registrieren: https://fossa.com/

# SonarCloud (wenn gewünscht)
SONAR_TOKEN=<dein-sonar-token>
# Kostenlos registrieren: https://sonarcloud.io/
```

### Standard GitHub Secrets (automatisch verfügbar):

```bash
GITHUB_TOKEN  # Automatisch von GitHub bereitgestellt
```

## ⚙️ Empfohlene GitHub Repository Settings

### 1. Branch Protection Rules

Gehe zu: `Settings → Branches → Add branch protection rule`

Für `main` Branch:

- ✅ Require a pull request before merging
- ✅ Require approvals (mindestens 1)
- ✅ Require status checks to pass before merging
  - Wähle wichtigste Workflows aus:
    - `CodeQL`
    - `Deep Security Scan`
    - `Dependency Review`
- ✅ Require conversation resolution before merging
- ✅ Do not allow bypassing the above settings

### 2. Security & Analysis

Gehe zu: `Settings → Security & analysis`

Aktiviere:

- ✅ **Dependency graph** (sollte bereits an sein)
- ✅ **Dependabot alerts**
- ✅ **Dependabot security updates**
- ✅ **Code scanning** (CodeQL wird automatisch konfiguriert)
- ✅ **Secret scanning** (wenn verfügbar)
- ✅ **Secret scanning push protection** (wenn verfügbar)

### 3. Actions Permissions

Gehe zu: `Settings → Actions → General`

```yaml
# Workflow permissions
Permissions: Read and write permissions
✅ Allow GitHub Actions to create and approve pull requests
```

### 4. Notifications

Gehe zu: `Settings → Notifications`

Empfohlen:
- ✅ Security alerts
- ✅ Dependabot alerts
- ✅ Failed workflow runs

## 📋 .gitignore Erweiterungen

Füge zu deiner `.gitignore` hinzu:

```gitignore
# Security Reports (lokal)
*-report.json
*-report.md
*-report.txt
*.sarif

# Security Tools Cache
.semgrep/
.trivy/

# Python Security
.bandit
.safety

# Node Security
npm-audit.json
.snyk

# Performance Reports
*.prof
*.pstats
```

## 🔧 Tool-spezifische Konfigurationen

### CodeQL Config (bereits vorhanden)

`.github/codeql/codeql-config.yml` ✅

### Semgrep Config (optional)

Erstelle `.semgrep.yml`:

```yaml
rules:
  - id: custom-security-rule
    patterns:
      - pattern: eval(...)
    message: Avoid using eval()
    severity: ERROR
    languages: [python, javascript]
```

### Bandit Config (optional)

Erstelle `.bandit`:

```ini
[bandit]
exclude_dirs = /tests,/venv,/node_modules
skips = B101,B601
```

### Ruff Config (optional)

Erstelle `ruff.toml`:

```toml
line-length = 100
target-version = "py312"

[lint]
select = ["E", "F", "W", "C90", "I", "N", "UP", "S", "B", "A", "C4"]
ignore = ["E501"]

[format]
quote-style = "double"
```

### ESLint Config (optional)

Erstelle `.eslintrc.json`:

```json
{
  "extends": [
    "eslint:recommended",
    "plugin:security/recommended"
  ],
  "plugins": ["security", "no-secrets"],
  "rules": {
    "no-eval": "error",
    "no-implied-eval": "error",
    "security/detect-object-injection": "warn"
  }
}
```

## 📊 Dependabot Konfiguration

Erstelle `.github/dependabot.yml`:

```yaml
version: 2
updates:
  # Python Dependencies
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
    open-pull-requests-limit: 5
    labels:
      - "dependencies"
      - "python"
    
  # npm Dependencies
  - package-ecosystem: "npm"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "monday"
    open-pull-requests-limit: 5
    labels:
      - "dependencies"
      - "javascript"
    
  # GitHub Actions
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "monthly"
    labels:
      - "dependencies"
      - "github-actions"
```

## 🏷️ GitHub Labels

Erstelle folgende Labels für bessere Organisation:

```bash
# Security
security         # FF0000 (rot)
vulnerability    # D73A4A (dunkelrot)
dependency       # 0366D6 (blau)

# Quality
code-quality     # FEF2C0 (gelb)
performance      # FBCA04 (orange)
documentation    # 0075CA (blau)

# Compliance
license          # C5DEF5 (hellblau)
compliance       # BFD4F2 (hellblau)
```

## 📈 Monitoring & Metriken

### GitHub Insights nutzen

1. **Security Tab**
   - Zeigt alle Vulnerabilities
   - CodeQL Ergebnisse
   - Dependabot Alerts

2. **Insights → Dependency graph**
   - Dependency Tree
   - Dependabot Alerts
   - Security Advisories

3. **Actions → Workflows**
   - Workflow Runs
   - Success/Failure Rates
   - Execution Times

### Status Badges für README

Füge zu deiner README.md hinzu:

```markdown
![Security Scan](https://github.com/<user>/<repo>/workflows/Deep%20Security%20Scan/badge.svg)
![CodeQL](https://github.com/<user>/<repo>/workflows/CodeQL%20Advanced/badge.svg)
![Container Security](https://github.com/<user>/<repo>/workflows/Container%20Security/badge.svg)
```

## 🔄 Workflow-Ausführungsreihenfolge

Die Workflows werden in folgender Priorität ausgeführt:

1. **Bei jedem Push/PR:**
   - CodeQL (schnellste Feedback-Loop)
   - Secret Scanning
   - Dependency Review

2. **Täglich (automatisch):**
   - Deep Security Scan (2:00 Uhr)
   - Container Security (4:00 Uhr)
   - IaC Security (3:00 Uhr)

3. **Wöchentlich (automatisch):**
   - Performance Analysis (Sonntag 5:00 Uhr)
   - Compliance Check (Montag 6:00 Uhr)
   - Master Dashboard (Montag 1:00 Uhr)

## 🚨 Alert Management

### Severity Levels

- **CRITICAL**: Sofort beheben!
- **HIGH**: Innerhalb 7 Tage beheben
- **MEDIUM**: Innerhalb 30 Tage beheben
- **LOW**: Bei Gelegenheit beheben

### Alert-Workflow

1. Security Alert erhalten
2. Issue im Repository erstellen
3. Priorität basierend auf Severity
4. Fix entwickeln
5. PR erstellen
6. Workflows prüfen lassen
7. Merge nach erfolgreichen Checks

## 💡 Performance Optimierung

### Workflows beschleunigen

```yaml
# Nutze caching für Dependencies
- name: Cache Python Dependencies
  uses: actions/cache@v4
  with:
    path: ~/.cache/pip
    key: ${{ runner.os }}-pip-${{ hashFiles('requirements.txt') }}

# Parallel ausführen wo möglich
strategy:
  matrix:
    python-version: [3.11, 3.12]
  max-parallel: 4
```

### Kosten sparen

- Nutze `continue-on-error: true` für nicht-kritische Jobs
- Setze `timeout-minutes` um hängende Jobs zu stoppen
- Nutze `paths` Filter um unnötige Runs zu vermeiden:

```yaml
on:
  push:
    paths:
      - '**.py'
      - '**.js'
      - 'requirements.txt'
      - 'package.json'
```

## 📞 Support & Hilfe

Bei Problemen:

1. Prüfe die [GitHub Actions Dokumentation](https://docs.github.com/en/actions)
2. Schau in die Tool-spezifischen Docs (siehe SECURITY_SUITE_README.md)
3. Öffne ein Issue in diesem Repository
4. Prüfe die Workflow-Logs für Details

## ✅ Checkliste für Setup

- [ ] Repository Secrets konfiguriert
- [ ] Branch Protection Rules aktiviert
- [ ] Security Features aktiviert
- [ ] Dependabot konfiguriert
- [ ] Labels erstellt
- [ ] .gitignore erweitert
- [ ] Erste Workflow-Runs erfolgreich
- [ ] Security Tab geprüft
- [ ] Badges zur README hinzugefügt
- [ ] Team informiert über neue Workflows

---

**Du bist jetzt bereit für maximale Security! 🚀**
