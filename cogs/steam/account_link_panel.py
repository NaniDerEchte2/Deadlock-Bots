"""
Steam-Account-Verknüpfen Panel Cog.

Postet eine persistente Panel-Message mit einem Button.
Beim Klick bekommt der User seine persönlichen Link-Buttons (ephemeral).

Admin-Command: /publish_steam_panel  →  postet/editiert das Panel
"""

from __future__ import annotations

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

GUILD_ID = int(os.getenv("GUILD_ID", "1289721245281292288"))

# Wird nach dem ersten /publish_steam_panel gesetzt und beim Bot-Restart
# aus dem kv_store geladen (optional – Panel funktioniert auch ohne Persist).
_PANEL_CUSTOM_ID = "steam_link_panel:open"


# ---------------------------------------------------------------------------
# Persistente Panel-View
# ---------------------------------------------------------------------------


class SteamLinkPanelView(discord.ui.View):
    """Persistente View die in der Panel-Message sitzt."""

    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @discord.ui.button(
        label="Steam Account verknüpfen 🔗",
        style=discord.ButtonStyle.success,
        custom_id=_PANEL_CUSTOM_ID,
    )
    async def open_link(self, interaction: discord.Interaction, _button: discord.ui.Button):
        from cogs.steam.steam_link_oauth import (
            LINK_BUTTON_LABEL,
            LINK_COVER_IMAGE,
            LINK_COVER_LABEL,
            PUBLIC_BASE_URL,
            STEAM_BUTTON_LABEL,
        )

        if not PUBLIC_BASE_URL:
            await interaction.response.send_message(
                "⚠️ PUBLIC_BASE_URL fehlt – Verknüpfung nicht möglich.", ephemeral=True
            )
            return

        uid = interaction.user.id
        desc = (
            "Waehle, wie du deinen Account verknüpfen willst:\n"
            "- **Discord**: liest deine verbundenen Accounts und erkennt Steam automatisch.\n"
            "- **Steam**: direkter OpenID-Login bei Steam.\n\n"
            "Nach erfolgreicher Verknüpfung bekommst du automatisch eine Steam-Freundschaftsanfrage vom Bot."
        )
        embed = discord.Embed(
            title="Account verknüpfen", description=desc, color=discord.Color.green()
        )
        if LINK_COVER_IMAGE:
            embed.set_image(url=LINK_COVER_IMAGE)
        embed.set_author(name=LINK_COVER_LABEL)

        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label=LINK_BUTTON_LABEL,
                url=f"{PUBLIC_BASE_URL}/discord/login?uid={uid}",
            )
        )
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link,
                label=STEAM_BUTTON_LABEL,
                url=f"{PUBLIC_BASE_URL}/steam/login?uid={uid}",
            )
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class SteamLinkPanel(commands.Cog):
    """Verwaltet das persistente Steam-Account-Verknüpfen-Panel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(SteamLinkPanelView())
        log.info("SteamLinkPanel geladen (persistent view aktiv).")

    # ------------------------------------------------------------------
    # Admin-Command: Panel posten / editieren
    # ------------------------------------------------------------------

    @app_commands.command(
        name="publish_steam_panel",
        description="(Admin) Steam-Verknüpfen-Panel in diesem Channel posten / aktualisieren",
    )
    @app_commands.guilds(discord.Object(id=GUILD_ID))
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        message_id="ID einer bestehenden Message die editiert werden soll (optional)"
    )
    async def publish_steam_panel(
        self,
        interaction: discord.Interaction,
        message_id: str | None = None,
    ):
        embed = discord.Embed(
            title="🔗 Steam Account verknüpfen",
            description=(
                "Verknüpfe deinen Steam-Account mit deinem Discord-Profil.\n\n"
                "**Was bringt das?**\n"
                "- Dein Rang wird automatisch auf dem Server angezeigt\n"
                "- Der Live-Status in den Voice Lanes funktioniert\n"
                "- Du wirst in der Spieler-Suche korrekt eingestuft\n\n"
                "Klick einfach auf den Button – der Rest geht automatisch."
            ),
            color=0x00AEEF,
        )
        view = SteamLinkPanelView()

        # Bestehende Message editieren?
        if message_id:
            try:
                mid = int(message_id)
                msg = await interaction.channel.fetch_message(mid)
                await msg.edit(embed=embed, view=view)
                await interaction.response.send_message("✅ Panel aktualisiert.", ephemeral=True)
                return
            except (ValueError, discord.NotFound):
                await interaction.response.send_message(
                    "❌ Message nicht gefunden. Neues Panel wird gepostet.", ephemeral=True
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    "❌ Keine Berechtigung diese Message zu editieren.", ephemeral=True
                )
                return

        # Neue Message posten
        await interaction.channel.send(embed=embed, view=view)
        if not interaction.response.is_done():
            await interaction.response.send_message("✅ Panel gepostet.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SteamLinkPanel(bot))
