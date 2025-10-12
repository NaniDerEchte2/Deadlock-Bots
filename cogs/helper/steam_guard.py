# cogs/helper/steam_guard.py
import logging

import discord
from discord import app_commands
from discord.ext import commands

LOG = logging.getLogger(__name__)


class SteamGuardCog(commands.Cog):
    """Stub cog that keeps the legacy /sg command available."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="sg")
    @commands.has_permissions(administrator=True)
    async def sg_prefix(self, ctx: commands.Context, code: str):
        await self._submit(code)
        message = (
            "ℹ️ Der Steam-Präsenz-Service ist deaktiviert – Guard-Codes müssen nicht mehr übermittelt werden."
        )
        await ctx.reply(message, ephemeral=True if hasattr(ctx, "response") else False)

    @app_commands.command(name="sg", description="Steam Guard Code an Presence-Service senden")
    @app_commands.describe(code="Der 2FA/Guard Code (z.B. 5 stellig)")
    @app_commands.checks.has_permissions(administrator=True)
    async def sg_slash(self, interaction: discord.Interaction, code: str):
        await self._submit(code)
        await interaction.response.send_message(
            "ℹ️ Der Steam-Präsenz-Service ist deaktiviert – Guard-Codes müssen nicht mehr übermittelt werden.",
            ephemeral=True,
        )

    async def _submit(self, code: str) -> bool:
        LOG.info("Steam presence service is disabled; ignoring submitted guard code '%s'.", code)
        return False


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamGuardCog(bot))
