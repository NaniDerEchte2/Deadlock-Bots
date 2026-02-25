"""
Thin wrapper to load the Twitch cog from the external repository.

We keep the import here so the master bot can continue to use the familiar
`cogs.twitch` namespace while the actual code lives in the modular
Deadlock-Twitch-Bot project.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Prefer an explicit override via env, otherwise fall back to the sibling repo.
_DEFAULT_PATH = (
    Path(os.path.expandvars(r"%USERPROFILE%")) / "Documents" / "Deadlock-Twitch-Bot"
)
_CUSTOM_PATH = os.getenv("TWITCH_COG_PATH")

_BASE_PATH = Path(_CUSTOM_PATH).expanduser() if _CUSTOM_PATH else _DEFAULT_PATH

# Ensure the external repo is importable before we pull in twitch_cog -> bot.*
if _BASE_PATH.exists():
    if str(_BASE_PATH) not in sys.path:
        sys.path.insert(0, str(_BASE_PATH))

try:
    import twitch_cog
except ModuleNotFoundError as exc:  # pragma: no cover - runtime guard
    # Surface a clearer error when the external repo is missing.
    raise ModuleNotFoundError(
        f"twitch_cog not found. Expected at {_BASE_PATH}. "
        "Set TWITCH_COG_PATH to the Deadlock-Twitch-Bot checkout."
    ) from exc


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
