# =========================================
# cogs/twitch/__init__.py
# =========================================
"""Package entry point for the Twitch stream monitor cog."""

import logging
from functools import wraps
from typing import Optional

from discord.ext import commands

from .cog import TwitchStreamCog

log = logging.getLogger("TwitchStreams")


async def setup(bot):
    """Add the Twitch stream cog to the master bot."""

    existing = bot.get_command("twl")
    if existing is not None:
        # Entfernt alte/stale Command-Objekte (z. B. nach fehlgeschlagenem Reload),
        # bevor das Cog hinzugefügt wird – sonst schlägt discord.py mit
        # CommandRegistrationError fehl.
        bot.remove_command(existing.name)
        log.info("Removed pre-existing !twl command before adding Twitch cog")

    cog = TwitchStreamCog(bot)
    await bot.add_cog(cog)

    @wraps(cog.twitch_leaderboard)
    async def _twl_proxy(ctx: commands.Context, *, filters: str = ""):
        active_cog: Optional[TwitchStreamCog] = bot.get_cog(cog.__cog_name__)  # type: ignore[assignment]
        if not isinstance(active_cog, TwitchStreamCog):
            await ctx.reply("Twitch-Statistiken sind derzeit nicht verfügbar.")
            return
        await active_cog.twitch_leaderboard(ctx, filters=filters)

    prefix_command = commands.Command(
        _twl_proxy,
        name="twl",
        help=cog.twitch_leaderboard.__doc__,
    )

    # Der Command gehört logisch zum Cog, wird aber bewusst außerhalb von add_cog registriert,
    # um Doppel-Registrierungen des Decorators zu vermeiden.
    prefix_command._set_cog(cog)  # type: ignore[attr-defined]
    bot.add_command(prefix_command)
    cog.set_prefix_command(prefix_command)
    log.info("Registered !twl prefix command via setup hook")
