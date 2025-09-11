# cogs/tempvoice/__init__.py
from __future__ import annotations
from discord.ext import commands

async def setup(bot: commands.Bot):
    # Beide Cogs in EINER Extension registrieren, mit sauberer Verkabelung.
    from .core import TempVoiceCore
    from .util import TempVoiceUtil
    from .interface import TempVoiceInterface

    core = TempVoiceCore(bot)
    util = TempVoiceUtil(core)

    await bot.add_cog(core)
    await bot.add_cog(TempVoiceInterface(bot, core, util))
