"""
Service Hooks Package
Automatische Prüfungen beim Bot-Start
"""
import logging

logger = logging.getLogger("service.hooks")

# Startup DB-Check: Prüft beim Bot-Start auf direkte sqlite3.connect() Aufrufe
try:
    from .startup_check import check_database_usage
    
    # Führe Check automatisch beim Import aus
    logger.info("🔍 Starte Datenbank-Architektur Check...")
    check_database_usage()
except Exception as e:
    logger.error(f"DB-Check fehlgeschlagen: {e}", exc_info=True)

__all__ = ['check_database_usage']
