"""Re-export service submodules without triggering self-import warnings."""

from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Literal, Optional, overload

log = logging.getLogger(__name__)


@overload
def _load_submodule(name: str, *, required: Literal[True]) -> ModuleType:
    pass


@overload
def _load_submodule(name: str, *, required: bool = False) -> ModuleType | None:
    pass


def _load_submodule(name: str, *, required: bool = False) -> ModuleType | None:
    """Import a submodule defensively, treating some imports as optional."""
    try:
        if name == "db":
            return importlib.import_module(".db", __name__)
        if name == "socket_bus":
            return importlib.import_module(".socket_bus", __name__)
        if name == "worker_client":
            return importlib.import_module(".worker_client", __name__)
        if name == "dashboard":
            return importlib.import_module(".dashboard", __name__)
        if name == "changelogs":
            return importlib.import_module(".changelogs", __name__)
        if name == "faq_logs":
            return importlib.import_module(".faq_logs", __name__)
        if name == "standalone_manager":
            return importlib.import_module(".standalone_manager", __name__)
        if name == "hooks":
            return importlib.import_module(".hooks", __name__)
        raise ImportError(f"Unknown service submodule: {name}")
    except Exception as exc:
        if required:
            raise
        log.debug("Optional service submodule %s not available: %s", name, exc)
        return None


db = _load_submodule("db", required=True)
socket_bus = _load_submodule("socket_bus")
worker_client = _load_submodule("worker_client")
dashboard = _load_submodule("dashboard")
changelogs = _load_submodule("changelogs")
faq_logs = _load_submodule("faq_logs")
standalone_manager = _load_submodule("standalone_manager")

# Auto-Setup for Pre-Commit Hook (verhindert direkte DB-Zugriffe)
# Aktiviert sich automatisch bei jedem Import von service
hooks = _load_submodule("hooks")

__all__ = [
    "db",
    "socket_bus",
    "worker_client",
    "dashboard",
    "changelogs",
    "faq_logs",
    "standalone_manager",
    "hooks",
]
