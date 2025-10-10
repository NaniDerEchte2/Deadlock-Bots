# =========================================
# cogs/twitch/__init__.py
# =========================================
"""Package entry point for the Twitch stream monitor cog."""

from .cog import TwitchStreamCog


async def setup(bot):
    """Add the Twitch stream cog to the master bot."""

    await bot.add_cog(TwitchStreamCog(bot))
