# service/__init__.py
# Re-export der Submodule, damit "from service import db" etc. funktioniert.
from . import db
try:
    from . import socket_bus  # falls vorhanden
except Exception:
    socket_bus = None
# worker_client entfernt - nicht mehr benötigt
try:
    from . import dashboard  # neues Dashboard-Modul
except Exception:
    dashboard = None
try:
    from . import faq_logs
except Exception:
    faq_logs = None
try:
    from . import changelogs
except Exception:
    changelogs = None
try:
    from . import standalone_manager
except Exception:
    standalone_manager = None

__all__ = [
    "db",
    "socket_bus",
    "dashboard",
    "faq_logs",
    "changelogs",
    "standalone_manager",
]
