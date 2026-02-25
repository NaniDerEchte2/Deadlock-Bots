"""
Bridge package so `cogs.steam.*` continues to work after moving Steam code
into the separate Deadlock-Steam-Bot repository.

The master bot still imports `cogs.steam.*`; we extend sys.path and the cogs
namespace to include the external repo before any submodule is loaded.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from pkgutil import extend_path

# Honour an explicit override first.
_CUSTOM_DIR = os.getenv("STEAM_COGS_DIR")
_DEFAULT_DIR = (
    Path(os.path.expandvars(r"%USERPROFILE%")) / "Documents" / "Deadlock-Steam-Bot" / "cogs"
)

_EXTERNAL_COGS = Path(_CUSTOM_DIR).expanduser() if _CUSTOM_DIR else _DEFAULT_DIR

if _EXTERNAL_COGS.exists():
    # Make sure Python can find the external `cogs` package.
    parent = _EXTERNAL_COGS.parent
    if str(parent) not in sys.path:
        sys.path.insert(0, str(parent))

    try:
        import cogs as _cogs_pkg  # type: ignore

        # Add the external cogs path to the namespace package search path.
        if str(_EXTERNAL_COGS.resolve()) not in map(str, _cogs_pkg.__path__):
            _cogs_pkg.__path__.append(str(_EXTERNAL_COGS.resolve()))
    except Exception:  # pragma: no cover - defensive
        # Fallback: nothing else to do, imports will raise naturally later.
        pass

# Make this a namespace-like package to include the external steam folder.
__path__ = extend_path(__path__, __name__)  # type: ignore[name-defined]
_external_steam_pkg = _EXTERNAL_COGS / "steam"
if _external_steam_pkg.is_dir():
    _resolved = str(_external_steam_pkg.resolve())
    if _resolved not in map(str, __path__):
        __path__.append(_resolved)

# Expose nothing from here – real modules live in the external repo.
__all__: list[str] = []
