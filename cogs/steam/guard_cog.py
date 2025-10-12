# cogs/steam/guard_cog.py
import logging
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cogs.steam.steam_bot import SteamBotCog

LOG = logging.getLogger(__name__)


class SteamGuardCog(commands.Cog):
    """Provides the legacy !sg and /sg commands and forwards codes to the Steam bot service."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="sg")
    @commands.has_permissions(administrator=True)
    async def sg_prefix(self, ctx: commands.Context, code: str):
        if self._forward(code):
            await ctx.reply("✅ Steam Guard Code wurde übermittelt.", mention_author=False)
        else:
            await ctx.reply("❌ Der Steam-Bot ist nicht verfügbar oder Code ungültig.", mention_author=False)

    @app_commands.command(name="sg", description="Steam Guard Code an den Steam-Bot senden")
    @app_commands.describe(code="Der 2FA/Guard Code (z.B. 5-stellig)")
    @app_commands.checks.has_permissions(administrator=True)
    async def sg_slash(self, interaction: discord.Interaction, code: str):
        if self._forward(code):
            await interaction.response.send_message(
                "✅ Steam Guard Code wurde an den Bot weitergeleitet.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Der Steam-Bot ist aktuell nicht verfügbar – bitte später erneut versuchen.",
                ephemeral=True,
            )

    def _forward(self, code: str) -> bool:
        cleaned = str(code or "").strip()
        if not cleaned:
            return False
        service = self._steam_cog
        if not service:
            LOG.warning("SteamBotCog not available; guard code cannot be forwarded")
            return False
        accepted = service.submit_guard_code(cleaned)
        if not accepted:
            LOG.warning("Steam guard code was rejected by service (probably no listener waiting)")
        return accepted

    @property
    def _steam_cog(self) -> Optional["SteamBotCog"]:
        cog = self.bot.get_cog("SteamBotCog")
        if cog and hasattr(cog, "submit_guard_code"):
            return cog  # type: ignore[return-value]
        return None


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamGuardCog(bot))
