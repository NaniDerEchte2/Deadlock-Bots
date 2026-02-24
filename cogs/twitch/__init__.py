"""Bridge: lädt twitch_cog aus externem Repo via pip install -e.

Hält Kompatibilität zu älteren Imports wie ``from cogs.twitch import storage``.
Wichtig: enthält eine eigene ``setup``-Funktion, damit die Auto-Discovery den
Cog erkennt.
"""
from __future__ import annotations

import importlib
from typing import Any

from twitch_cog import setup as _real_setup, teardown as _real_teardown

_LAZY_EXPORTS = {"storage", "storage_pg", "social_media"}
__all__ = ["setup", "teardown", *sorted(_LAZY_EXPORTS)]


async def setup(bot):
    return await _real_setup(bot)


async def teardown(bot):
    return await _real_teardown(bot)


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        return importlib.import_module(f"twitch_cog.{name}")
    raise AttributeError(name)
