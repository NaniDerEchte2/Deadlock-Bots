# 🔒 GitHub Security & Quality Analysis Suite

Diese umfassende Security-Suite bietet die **maximalste und tiefste Analyse** für dein Repository mit über **50+ verschiedenen Security-Tools** und Analysemethoden.

## 📋 Übersicht

Diese Suite führt automatisch folgende Analysen durch:

### 🔒 **Security Workflows**

1. **CodeQL Advanced** (`codeql.yml`)
   - Automatische Spracherkennung
   - Security-Extended & Quality Queries
   - Unterstützt: Python, JavaScript/TypeScript, Go, Java, C#, Ruby, Rust, C/C++, Swift

2. **Deep Security Scan** (`security-deep-scan.yml`)
   - **Python**: Bandit, Safety, Pip-Audit, Semgrep, Vulture, Dodgy, Prospector
   - **JavaScript/Node**: NPM Audit, Snyk, ESLint Security, RetireJS
   - **Multi-Target**: Trivy (Filesystem, Config, SBOM)
   - **SAST**: Semgrep Professional (OWASP Top 10, Security Audit)
   - **OSSF Security Scorecard**

3. **Dashboard Auth Guardrails** (`dashboard-auth-guard.yml`)
   - Startet den Twitch-Dashboard-Server im CI-Job
   - Prüft harte Access-Control-Regeln für `/twitch/admin`
   - Testet Header-Spoofing (`X-Dashboard-Context`, `X-Forwarded-*`)
   - Verhindert Auth-Bypass-Regressionen

4. **Container Security** (`container-security.yml`)
   - Dockerfile Security: Hadolint, Checkov
   - Image Scanning: Trivy
   - Docker Compose Security
   - Best Practices Check
   - Image Size Optimization (Dive)

5. **Infrastructure as Code** (`iac-security.yml`)
   - **Terraform**: TFLint, TFSec, Checkov
   - **Kubernetes**: KubeLinter, KICS
   - **CloudFormation**: CFN-Lint, Checkov
   - **Ansible**: ansible-lint
   - Configuration File Security

6. **Secret Scanning** (`secret-scanning.yml`)
   - Gitleaks: Git history scanning
   - Trivy Secrets: Filesystem scanning

7. **Dependency Review** (`dependency-review.yml`)
   - GitHub Dependency Review
   - NPM Audit
   - Python Safety Check

### ⚡ **Performance Workflows**

8. **Performance Analysis** (`performance-analysis.yml`)
   - **Python**: Memory profiling, Leak detection, Complexity analysis
   - **JavaScript**: Bundle size, Memory patterns
   - **Database**: Query optimization, N+1 detection
   - **API**: Performance checks, Rate limiting, Caching
   - Resource usage estimation

### ✅ **Compliance Workflows**

9. **Compliance Check** (`compliance-check.yml`)
   - **License Compliance**: Python (pip-licenses), Node (license-checker), FOSSA
   - **Code Style**: Black, Ruff, isort, Flake8, Prettier, ESLint
   - **Documentation**: README, CONTRIBUTING, LICENSE, CODE_OF_CONDUCT checks
   - **Git Hygiene**: .gitignore, large files, sensitive files
   - **Accessibility**: Alt text, semantic HTML, ARIA

### 🎯 **Master Workflow**

10. **Master Dashboard** (`master-dashboard.yml`)
   - Orchestriert alle Workflows
   - Aggregiert Ergebnisse
   - Erstellt umfassendes Dashboard
   - Sammelt Metriken
   - Wöchentliche Ausführung

## 🚀 Setup & Verwendung

### Automatische Ausführung

Die Workflows laufen automatisch bei:

- **Push** auf `main`/`master` Branch
- **Pull Requests**
- **Zeitplan** (täglich/wöchentlich je nach Workflow)
- **Manuell** über GitHub Actions UI

### Manuelle Ausführung

1. Gehe zu **Actions** Tab in deinem Repository
2. Wähle den gewünschten Workflow
3. Klicke auf "Run workflow"
4. (Optional) Wähle Scan-Type für Master Dashboard

### Benötigte Secrets (Optional)

Für erweiterte Funktionen kannst du folgende Secrets in deinem Repository einrichten:

```bash
# Repository Settings → Secrets → Actions

SNYK_TOKEN          # Snyk Security Scanning
FOSSA_API_KEY       # FOSSA License Compliance
SONAR_TOKEN         # SonarCloud Integration (wenn gewünscht)
```

**Hinweis**: Die meisten Workflows funktionieren auch ohne diese Secrets!

## 📊 Ergebnisse & Reports

### Wo finde ich die Ergebnisse?

1. **Security Tab**
   - Alle SARIF-Ergebnisse erscheinen automatisch hier
   - CodeQL, Semgrep, Trivy, etc.

2. **Actions Artifacts**
   - Detaillierte Reports für jede Analyse
   - JSON, Markdown, und Text-Formate
   - Bleiben 90 Tage verfügbar

3. **GitHub Summary**
   - Jeder Workflow erstellt ein Summary
   - Sichtbar direkt im Actions Run

### Report-Kategorien

Jeder Workflow erstellt spezifische Artifacts:

- `python-security-reports`: Bandit, Safety, etc.
- `javascript-security-reports`: NPM Audit, ESLint
- `semgrep-reports`: SAST Ergebnisse
- `dependency-analysis`: Dependency Trees, Lizenzen
- `code-quality-metrics`: Komplexität, Maintainability
- `container-security`: Docker & Image Scans
- `performance`: Memory, Bottlenecks, API
- `compliance`: Lizenzen, Style, Docs

## 🔧 Anpassung

### Workflow-Trigger ändern

In jedem Workflow-File kannst du die Trigger anpassen:

```yaml
on:
  push:
    branches: [ "main", "develop" ]  # Deine Branches
  schedule:
    - cron: '0 2 * * *'  # Deine gewünschte Zeit
```

### Tools ein/ausschalten

Du kannst einzelne Jobs auskommentieren oder löschen:

```yaml
# Diesen Job deaktivieren:
# python-security:
#   name: 🐍 Python Security Suite
#   ...
```

### Severity Levels anpassen

Für strengere oder lockerere Checks:

```yaml
# Strenger:
fail-on-severity: low

# Lockerer:
fail-on-severity: critical
```

## 📈 Tool-Übersicht

### Security Tools (30+)

| Kategorie | Tools |
|-----------|-------|
| **SAST** | CodeQL, Semgrep, Bandit, ESLint-Security |
| **SCA** | Trivy, Safety, Pip-Audit, Snyk, NPM Audit, RetireJS |
| **Secrets** | Gitleaks, Trivy-Secrets |
| **Container** | Trivy, Hadolint, Checkov, Dive |
| **IaC** | TFSec, TFLint, Checkov, KICS, KubeLinter, CFN-Lint |
| **Compliance** | OSSF Scorecard, Dependency Review |

### Performance Tools (15+)

| Kategorie | Tools |
|-----------|-------|
| **Python** | Radon, py-spy, Scalene, memory-profiler, Vulture |
| **JavaScript** | webpack-bundle-analyzer, size-limit |
| **Analysis** | MyPy, Complexity Analysis |

### Quality Tools (20+)

| Kategorie | Tools |
|-----------|-------|
| **Python** | Black, Ruff, isort, Flake8, pydocstyle, Prospector |
| **JavaScript** | Prettier, ESLint |
| **License** | FOSSA, pip-licenses, license-checker |
| **Documentation** | Custom checks |

## 🎯 Best Practices

### Was sollte ich zuerst beheben?

1. **Critical/High Severity Issues** aus Security Tab
2. **Secrets** die gefunden wurden (sofort!)
3. **Known Vulnerabilities** in Dependencies
4. **Medium Severity** Security Issues
5. **Performance Bottlenecks** (bei Bedarf)
6. **Code Quality** und Style

### Wie oft laufen die Scans?

- **Security**: Täglich (automatisch)
- **Performance**: Wöchentlich (Sonntag)
- **Compliance**: Wöchentlich (Montag)
- **Master Dashboard**: Wöchentlich (Montag)
- **Bei jedem Push/PR**: Alle relevanten Workflows

### Kann ich Scans überspringen?

Ja, mit Skip CI:

```bash
git commit -m "docs: update README [skip ci]"
```

## 🔍 Troubleshooting

### Workflow schlägt fehl

- Prüfe die Logs im Actions Tab
- Die meisten Jobs haben `continue-on-error: true`
- Einzelne Fehler sollten den gesamten Workflow nicht stoppen

### Zu viele Findings

- Nutze `severity` Filter in den Tools
- Fokussiere zuerst auf High/Critical
- Arbeite iterativ

### Performance Issues

- Reduziere `max_results` in Tools
- Nutze `paths-ignore` in Workflows
- Führe Performance-Scans nur wöchentlich aus

## 📚 Dokumentation

Jedes Tool hat ausführliche Dokumentation:

- [CodeQL](https://codeql.github.com/docs/)
- [Semgrep](https://semgrep.dev/docs/)
- [Trivy](https://aquasecurity.github.io/trivy/)
- [Bandit](https://bandit.readthedocs.io/)
- [Checkov](https://www.checkov.io/1.Welcome/What%20is%20Checkov.html)

## 🤝 Contributing

Hast du Verbesserungsvorschläge für diese Security Suite?

1. Öffne ein Issue
2. Beschreibe deine Idee
3. (Optional) Erstelle einen PR

## 📝 License

Diese Workflow-Konfigurationen sind frei verwendbar für deine Projekte.

---

**Erstellt mit ❤️ für maximale Security & Quality**

*Letzte Aktualisierung: 2025-02*
