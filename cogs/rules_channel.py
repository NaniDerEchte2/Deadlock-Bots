# -*- coding: utf-8 -*-
"""
Rules Panel Cog - startet das neue KI-Onboarding im privaten Thread.
- Persistente Panel-View (nur custom_id-Buttons, kein Link-Button)
- Delegiert an AIOnboarding (cogs/ai_onboarding.py)
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

# ========== Konfiguration ==========
MAIN_GUILD_ID    = 1289721245281292288
RULES_CHANNEL_ID = 1315684135175716975

log = logging.getLogger("RulesPanel")


# ------------------------------ Helpers ------------------------------ #
async def _create_user_thread(interaction: discord.Interaction) -> Optional[discord.Thread]:
    """Erstellt einen (bevorzugt) privaten Thread im Regelkanal und fügt den Nutzer hinzu."""
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("❌ Dieser Button funktioniert nur in der Guild.", ephemeral=True)
        return None

    channel = guild.get_channel(RULES_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ Regelkanal nicht gefunden/kein Textkanal.", ephemeral=True)
        return None

    name = f"onboarding-{interaction.user.name}".replace(" ", "-")[:90]

    # Private Thread versuchen
    try:
        thread = await channel.create_thread(
            name=name,
            type=discord.ChannelType.private_thread,
            invitable=True,
            auto_archive_duration=60,
        )
        await thread.add_user(interaction.user)
        return thread
    except discord.Forbidden as exc:
        log.debug("Privater Thread konnte nicht erstellt werden: %s", exc)

    # Fallback: Public Thread
    try:
        thread = await channel.create_thread(
            name=name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=60,
        )
        return thread
    except Exception as e:
        log.error("Thread creation failed: %r", e)
        await interaction.response.send_message("❌ Konnte keinen Thread erstellen.", ephemeral=True)
        return None


# ------------------------------ Panel-View (persistent) ------------------------------ #
class RulesPanelView(discord.ui.View):
    def __init__(self, cog: "RulesPanel"):
        super().__init__(timeout=None)  # PERSISTENT
        self.cog = cog

    @discord.ui.button(label="Weiter ➜", style=discord.ButtonStyle.primary, custom_id="rp:panel:start")
    async def start(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.cog.start_in_thread(interaction)


# ------------------------------ Cog ------------------------------ #
class RulesPanel(commands.Cog):
    """Wrapper-Cog: Startet das KI-Onboarding im privaten Thread."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Nur die Panel-View persistent registrieren!
        self.bot.add_view(RulesPanelView(self))
        log.info("✅ Rules Panel geladen (Panel-View aktiv)")

    @app_commands.command(name="publish_rules_panel", description="(Admin) Regelwerk-Panel posten")
    @app_commands.checks.has_permissions(administrator=True)
    async def publish_rules_panel(self, interaction: discord.Interaction):
        guild = self.bot.get_guild(MAIN_GUILD_ID)
        if not guild:
            await interaction.response.send_message("❌ MAIN_GUILD_ID ungültig oder Bot nicht auf dieser Guild.", ephemeral=True)
            return
        ch = guild.get_channel(RULES_CHANNEL_ID)
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("❌ RULES_CHANNEL_ID zeigt nicht auf einen Text-/Thread-Kanal.", ephemeral=True)
            return

        emb = discord.Embed(
            title="📜 Regelwerk · Deutsche Deadlock Community",
            description="Klick auf **Weiter ➜**, um dein Onboarding im eigenen Thread zu starten.",
            color=0x00AEEF,
        )
        await ch.send(embed=emb, view=RulesPanelView(self))
        await interaction.response.send_message("✅ Panel gesendet.", ephemeral=True)

    # ----- Start-Flow im Thread -----
    async def start_in_thread(self, interaction: discord.Interaction):
        thread = await _create_user_thread(interaction)
        if not thread:
            return

        # Nutzer informieren
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"🚀 Onboarding in {thread.mention} gestartet.", ephemeral=True)
            else:
                await interaction.followup.send(f"🚀 Onboarding in {thread.mention} gestartet.", ephemeral=True)
        except Exception as exc:
            log.debug("Konnte Start-Hinweis nicht senden: %s", exc)

        # KI-Onboarding starten
        ai_cog = self.bot.get_cog("AIOnboarding")
        if ai_cog and hasattr(ai_cog, "start_in_channel"):
            try:
                ok = await ai_cog.start_in_channel(thread, interaction.user)  # type: ignore
                if ok:
                    return
            except Exception as e:
                log.warning("AIOnboarding.start_in_channel fehlgeschlagen: %r", e)

        # Minimaler Fallback, falls die KI nicht läuft
        fallback_embed = discord.Embed(
            title="Willkommen!",
            description=(
                "Das Onboarding ist gerade nicht verfügbar.\n"
                "Schau in #ankündigungen, finde Mitspieler in #spieler-suche "
                "und richte dir im Temp Voice Panel eine eigene Lane ein.\n"
                "Fragen? Nutze /faq oder ping das Team. 😊"
            ),
            color=0x5865F2,
        )
        await thread.send(embed=fallback_embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(RulesPanel(bot))
