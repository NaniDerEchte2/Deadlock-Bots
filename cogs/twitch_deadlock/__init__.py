from .cog import TwitchDeadlockCog

async def setup(bot) -> None:
    await bot.add_cog(TwitchDeadlockCog(bot))
