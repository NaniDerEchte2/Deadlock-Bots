"""
Changelog Discord Bot Cog - DEACTIVATED
Diese Cog wurde durch den Unified Patch Bot ersetzt.
Alle Funktionen sind jetzt im unified_patchnotes_bot.py integriert.
"""

import discord
from discord.ext import commands
import logging

logger = logging.getLogger(__name__)

class ChangelogDiscordBotCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("✅ Changelog Discord Bot Cog geladen (DEAKTIVIERT - ersetzt durch Unified Patch Bot)")
        
    @commands.command(name='changelogstatus')
    @commands.has_permissions(administrator=True)
    async def changelog_status(self, ctx):
        """Zeigt Informationen über den deaktivierten Changelog Bot"""
        embed = discord.Embed(
            title="📋 Changelog Discord Bot Status",
            description="**DEAKTIVIERT** - Diese Funktionalität ist jetzt im **Unified Patch Bot** integriert.",
            color=0xff9900
        )
        embed.add_field(
            name="🔄 Migration",
            value="Alle Changelog-Funktionen wurden in den `unified_patchnotes_bot.py` verschoben.",
            inline=False
        )
        embed.add_field(
            name="🛠️ Neues System",
            value="Verwende `!patchbot start/stop/restart/status` für die Patch Bot Kontrolle.",
            inline=False
        )
        embed.add_field(
            name="ℹ️ Hilfe",
            value="Verwende `!patchhelp` für alle verfügbaren Commands.",
            inline=False
        )
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(ChangelogDiscordBotCog(bot))