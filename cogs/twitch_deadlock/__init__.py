# =========================================
# cogs/twitch_deadlock/__init__.py
# =========================================
from .cog import TwitchDeadlockCog

async def setup(bot):
    await bot.add_cog(TwitchDeadlockCog(bot))
