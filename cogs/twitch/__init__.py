"""Bridge: lädt twitch_cog aus externem Repo via pip install -e."""
from twitch_cog import setup, teardown  # noqa: F401

__all__ = ["setup", "teardown"]
