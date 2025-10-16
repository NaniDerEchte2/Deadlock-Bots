# =========================================
# cogs/twitch/__init__.py
# =========================================
"""Package entry point for the Twitch stream monitor cog."""

import logging

from .cog import TwitchStreamCog

log = logging.getLogger("TwitchStreams")


async def setup(bot):
    """Add the Twitch stream cog to the master bot."""

    cog = TwitchStreamCog(bot)
    await bot.add_cog(cog)

    # discord.py registriert Commands beim Hinzufügen des Cogs automatisch. In seltenen Fällen
    # (z. B. bei Hot-Reloads nach Exceptions) beobachten wir jedoch, dass der Prefix-Command fehlt.
    # Wir prüfen daher nach dem Hinzufügen einmal nach und registrieren ihn bei Bedarf nachträglich.
    if bot.get_command("twl") is None:
        fallback_cmd = next((cmd.copy() for cmd in cog.get_commands() if cmd.name == "twl"), None)
        if fallback_cmd is not None:
            fallback_cmd._set_cog(cog)  # type: ignore[attr-defined]
            bot.add_command(fallback_cmd)
            log.warning("Re-registered missing !twl prefix command after cog setup")
