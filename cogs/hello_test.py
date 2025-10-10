# cogs/hello_test.py
import logging
from discord.ext import commands

logger = logging.getLogger(__name__)

class HelloTest(commands.Cog):
    """Ein einfacher Test-Cog, der beim Start 'Hallo' ausgibt."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("👋 HelloTest Cog wurde initialisiert.")

    @commands.Cog.listener()
    async def on_ready(self):
        # Wird aufgerufen, wenn der Bot bereit ist
        logger.info("👋 HelloTest sagt: Hallo! (Bot ist bereit)")
        print("👋 HelloTest sagt: Hallo!")  # Optional auch direkt auf der Konsole

async def setup(bot: commands.Bot):
    await bot.add_cog(HelloTest(bot))
