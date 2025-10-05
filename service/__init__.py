# service/__init__.py
# Re-export der Submodule, damit "from service import db" etc. funktioniert.
from . import db
try:
    from . import steam  # Rich-Presence/Steam-Hilfen
except Exception:
    steam = None
try:
    from . import socket_bus  # falls vorhanden
except Exception:
    socket_bus = None
try:
    from . import worker_client  # falls vorhanden
except Exception:
    worker_client = None

__all__ = ["db", "socket_bus", "worker_client", "steam"]
