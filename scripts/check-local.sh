#!/usr/bin/env bash
# Lokale Code-Qualität und Security-Prüfung
# Voraussetzung: pip install ruff mypy bandit pytest pytest-cov

set -euo pipefail

cd "$(dirname "$0")/.."

fail=0

run_check() {
  local name="$1"
  shift
  echo
  echo "=== $name ==="
  if "$@"; then
    echo "✅ $name: OK"
  else
    echo "❌ $name: FEHLGESCHLAGEN"
    fail=1
  fi
}

run_check "Ruff Lint" \
  ruff check . --target-version=py311

run_check "Ruff Format" \
  ruff format . --check --target-version=py311

run_check "mypy Type Check" \
  mypy service cogs bot_core --ignore-missing-imports --no-error-summary --show-column-numbers

run_check "Bandit Security SAST" \
  bandit -r bot_core cogs service tests -ll -ii

run_check "pytest" \
  pytest -q --cov=bot_core --cov=cogs --cov=service --cov-branch --cov-fail-under=40

echo
if [ "$fail" -eq 0 ]; then
  echo "✅ Alle lokalen Checks bestanden."
else
  echo "❌ Einige Checks sind fehlgeschlagen."
  exit 1
fi
