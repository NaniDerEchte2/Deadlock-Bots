"""
TEST COG für das neue automatische Loading System
Dieser Cog demonstriert, dass neue Scripts automatisch geladen werden.
"""

import discord
from discord.ext import commands
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class TestNewCog(commands.Cog):
    """Test Cog für automatisches Loading System"""
    
    def __init__(self, bot):
        self.bot = bot
        self.created_at = datetime.now()
        logger.info(f"🧪 TestNewCog initialized at {self.created_at}")
    
    def cog_unload(self):
        logger.info("🛑 TestNewCog unloaded")
    
    @commands.command(name='testnew')
    async def test_new_command(self, ctx):
        """Test Command für das neue Cog"""
        embed = discord.Embed(
            title="🧪 Test New Cog",
            description="Dieser Cog wurde automatisch geladen!",
            color=0x00ff00
        )
        
        embed.add_field(
            name="⏰ Created",
            value=self.created_at.strftime("%H:%M:%S"),
            inline=True
        )
        
        embed.add_field(
            name="🤖 Bot",
            value=f"{self.bot.user.name}",
            inline=True
        )
        
        await ctx.send(embed=embed)
    
    @commands.command(name='autotest')
    async def auto_test(self, ctx):
        """Zeigt dass automatisches Loading funktioniert"""
        await ctx.send("✅ **AUTO-LOADING ERFOLGREICH!**\n"
                      "Dieser Cog wurde automatisch ohne Bot-Neustart geladen!")

async def setup(bot):
    """Setup-Funktion für automatisches Loading"""
    await bot.add_cog(TestNewCog(bot))
    logger.info("✅ TestNewCog setup complete")