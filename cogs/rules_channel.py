# -*- coding: utf-8 -*-
"""
Rules Panel Cog – startet den Welcome-Flow 1:1 im privaten Thread aus dem Regel-Channel.
- Persistente Panel-View (nur custom_id-Buttons, kein Link-Button)
- Nutzt die bestehenden Views aus cogs.welcome_dm (keine Duplikate)
"""

from __future__ import annotations

import logging
import contextlib
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

# ========== Konfiguration ==========
MAIN_GUILD_ID    = 1289721245281292288
RULES_CHANNEL_ID = 1315684135175716975

log = logging.getLogger("RulesPanel")

# ========== Imports aus welcome_dm ==========
from cogs.welcome_dm.base import build_step_embed
from cogs.welcome_dm.step_intro import IntroView
from cogs.welcome_dm.step_status import PlayerStatusView
from cogs.welcome_dm.step_steam_link import SteamLinkStepView, steam_link_detailed_description
from cogs.welcome_dm.step_rules import RulesView


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
    except discord.Forbidden:
        pass

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


async def _send_step(thread: discord.Thread, embed: discord.Embed, view: discord.ui.View) -> bool:
    """Sendet Embed+View in den Thread, wartet auf Abschluss und räumt auf."""
    msg = await thread.send(embed=embed, view=view)
    try:
        setattr(view, "bound_message", msg)  # kompatibel mit DM-Views
    except Exception:
        pass
    try:
        await view.wait()
    finally:
        with contextlib.suppress(Exception):
            await msg.delete()
    return bool(getattr(view, "proceed", True))


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
    """Wrapper-Cog: Startet den WelcomeDM-Flow im privaten Thread."""

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
            title="📜 Regelwerk • Deadlock DACH",
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
                await interaction.response.send_message(f"🧵 Onboarding in {thread.mention} gestartet.", ephemeral=True)
            else:
                await interaction.followup.send(f"🧵 Onboarding in {thread.mention} gestartet.", ephemeral=True)
        except Exception:
            pass

        # Bevorzugt: WelcomeDM um Hilfe bitten
        wdm = self.bot.get_cog("WelcomeDM")
        if wdm and hasattr(wdm, "run_flow_in_channel"):
            try:
                await wdm.run_flow_in_channel(thread, interaction.user)  # type: ignore
                return
            except Exception as e:
                log.warning("WelcomeDM.run_flow_in_channel failed, fallback local: %r", e)

        # Fallback: denselben Flow lokal starten (Intro ungezählt; danach 1/3–3/3)
        total = 3

        # Intro (ohne Zählung)
        emb = build_step_embed(
            title="👋 Willkommen!",
            desc="Ich helfe dir, dein Erlebnis hier optimal einzustellen. 2–3 Minuten genügen.",
            step=None, total=total, color=0x5865F2,
        )
        ok = await _send_step(thread, emb, IntroView())
        if not ok:
            return

        # 1/3 Status
        emb = build_step_embed(
            title="Frage 1/3 · Wie ist dein Status?",
            desc="Sag kurz, wo du stehst – dann passen wir alles besser an.",
            step=1, total=total, color=0x95A5A6,
        )
        status = PlayerStatusView()
        ok = await _send_step(thread, emb, status)
        if not ok:
            return

        # 2/3 Steam
        emb = build_step_embed(
            title="Frage 2/3 · Steam verknüpfen (empfohlen)",
            desc=steam_link_detailed_description(),
            step=2,
            total=total,
            color=0x2ECC71,
        )
        ok = await _send_step(thread, emb, SteamLinkStepView())
        if not ok:
            return

        # 3/3 Regeln
        emb = build_step_embed(
            title="Frage 3/3 · Regelwerk bestätigen",
            desc="Kurz bestätigen, dass du die Regeln gelesen hast.",
            step=3, total=total, color=0xE67E22,
        )
        await _send_step(thread, emb, RulesView())


async def setup(bot: commands.Bot):
    await bot.add_cog(RulesPanel(bot))
