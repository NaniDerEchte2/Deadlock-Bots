"""
Thin wrapper to load the Twitch cog from the external repository.

We keep the import here so the master bot can continue to use the familiar
`cogs.twitch` namespace while the actual code lives in the modular
Deadlock-Twitch-Bot project.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

# Use the sibling split repository path directly.
_BASE_PATH = Path(__file__).resolve().parents[3] / "Deadlock-Twitch-Bot"
_EXPECTED_MODULE_PATHS = {
    (_BASE_PATH / "twitch_cog.py").resolve(),
    (_BASE_PATH / "twitch_cog" / "__init__.py").resolve(),
}

# Ensure the external repo is importable before we pull in twitch_cog -> bot.*
if _BASE_PATH.exists():
    if str(_BASE_PATH) not in sys.path:
        sys.path.insert(0, str(_BASE_PATH))

def _import_twitch_cog():
    """Import the split twitch_cog module and guard against namespace collisions."""
    importlib.invalidate_caches()
    cached = sys.modules.get("twitch_cog")
    if cached is not None:
        cached_file = getattr(cached, "__file__", None)
        if not callable(getattr(cached, "setup", None)) or not cached_file:
            sys.modules.pop("twitch_cog", None)
        else:
            try:
                if Path(cached_file).resolve() not in _EXPECTED_MODULE_PATHS:
                    sys.modules.pop("twitch_cog", None)
            except Exception:
                sys.modules.pop("twitch_cog", None)
    module = importlib.import_module("twitch_cog")
    if not callable(getattr(module, "setup", None)):
        module_file = getattr(module, "__file__", "<namespace>")
        raise AttributeError(
            "Imported twitch_cog has no callable setup(). "
            f"Loaded from: {module_file}; expected under: {_BASE_PATH}"
        )
    return module

try:
    if not _BASE_PATH.exists():
        raise ModuleNotFoundError(f"Expected split Twitch repo at {_BASE_PATH}")
    twitch_cog = _import_twitch_cog()
except (ModuleNotFoundError, AttributeError) as exc:  # pragma: no cover - runtime guard
    # Surface a clearer error when the external repo is missing.
    raise ModuleNotFoundError(f"twitch_cog could not be loaded from {_BASE_PATH}") from exc


async def setup(bot):
    """Entrypoint for the master bot's cog loader (delegates to external repo)."""
    await twitch_cog.setup(bot)


async def teardown(bot):
    """Unload hook mirroring the external cog teardown."""
    await twitch_cog.teardown(bot)


# Optional convenience export: allow `from cogs.twitch import storage`
# so existing onboarding/views keep working after the repo split.
try:  # pragma: no cover - best-effort shim
    from bot import storage  # type: ignore  # noqa: F401
except Exception:
    # Keep the module importable even if the external repo is missing storage.
    storage = None  # type: ignore
