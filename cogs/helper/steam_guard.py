# cogs/helper/steam_guard.py
import logging
from typing import Optional
import discord
from discord.ext import commands
from discord import app_commands

LOG = logging.getLogger(__name__)

class SteamGuardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Prefix command: !sg ABCDE
    @commands.command(name="sg")
    @commands.has_permissions(administrator=True)
    async def sg_prefix(self, ctx: commands.Context, code: str):
        ok = await self._submit(code)
        await ctx.reply("✅ Code gesendet." if ok else "❌ Konnte Code nicht senden.", ephemeral=True if hasattr(ctx, "response") else False)

    # Slash command: /sg ABCDE
    @app_commands.command(name="sg", description="Steam Guard Code an Presence-Service senden")
    @app_commands.describe(code="Der 2FA/Guard Code (z.B. 5 stellig)")
    @app_commands.checks.has_permissions(administrator=True)
    async def sg_slash(self, interaction: discord.Interaction, code: str):
        ok = await self._submit(code)
        await interaction.response.send_message("✅ Code gesendet." if ok else "❌ Konnte Code nicht senden.", ephemeral=True)

    async def _submit(self, code: str) -> bool:
        # Manager vom Bot holen
        manager = getattr(self.bot, "steam_service_manager", None)
        if manager is None:
            LOG.warning("No steam_service_manager on bot.")
            return False
        return await manager.submit_guard_code(code)

async def setup(bot: commands.Bot):
    await bot.add_cog(SteamGuardCog(bot))
