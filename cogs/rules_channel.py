# -*- coding: utf-8 -*-
"""
Rules Panel Cog – spiegelt das Welcome-DM Onboarding 1:1 im Thread.

Idee:
- NICHT die Inhalte hier duplizieren.
- Stattdessen die bestehenden DM-Views aus `cogs.welcome_dm` verwenden.
- Wenn der WelcomeDM-Cog eine Methode `run_flow_in_channel(channel, member)` anbietet,
  nutzen wir genau diese (volle Wiederverwendung).
- Falls nicht, verwenden wir eine *Fallback*-Sequenz, die ebenfalls ausschließlich
  die vorhandenen Step-Views nutzt (Intro/Status/Steam/Rules).

Benutzung:
- /publish_rules_panel (oder dein bestehender Admin-Command) postet das Panel.
- Klick auf „Weiter ➜“: erstellt privaten Thread und startet den Flow im Thread.

Hinweis:
- Die View ist persistent (timeout=None) und wird in `cog_load` registriert.
- Thread-Erstellung: bevorzugt privat; fällt ggf. auf public thread zurück.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple
import contextlib

import discord
from discord.ext import commands
from discord import app_commands

# ========== Konfiguration (IDs bitte prüfen) ==========
MAIN_GUILD_ID            = 1289721245281292288
RULES_CHANNEL_ID         = 1315684135175716975

log = logging.getLogger("RulesPanel")

# ========== Bestehende DM-Bausteine importieren ==========
# Wir wollen NICHT neu schreiben – wir verwenden die existierenden Views/Helper aus welcome_dm.
from cogs.welcome_dm.base import build_step_embed
from cogs.welcome_dm.step_intro import IntroView
from cogs.welcome_dm.step_status import PlayerStatusView
from cogs.welcome_dm.step_steam_link import SteamLinkNudgeView
from cogs.welcome_dm.step_rules import RulesView


# ------------------------------
# Thread-Helfer
# ------------------------------
async def _create_user_thread(interaction: discord.Interaction) -> Optional[discord.Thread]:
    """Erstellt einen (bevorzugt) privaten Thread unter RULES_CHANNEL_ID und fügt den Nutzer hinzu."""
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("❌ Dieser Button funktioniert nur in der Guild.", ephemeral=True)
        return None

    channel = guild.get_channel(RULES_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ Regelkanal nicht gefunden/kein Textkanal.", ephemeral=True)
        return None

    name = f"onboarding-{interaction.user.name}".replace(' ', '-')[:90]

    # Versuche Private Thread
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
    # Falls die View ein bound_message-Feld hat, setze es (für kompatible StepViews)
    try:
        setattr(view, "bound_message", msg)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        await view.wait()
    finally:
        # Die DM-Views löschen oft selbst; Cleanup hier defensiv.
        with contextlib.suppress(Exception):
            await msg.delete()
    # proceed=true -> weiter
    return bool(getattr(view, "proceed", True))


# ------------------------------
# Panel-View (Regelkanal)
# ------------------------------
class RulesPanelView(discord.ui.View):
    def __init__(self, cog: "RulesPanel"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Weiter ➜", style=discord.ButtonStyle.primary, custom_id="rp:panel:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.start_in_thread(interaction)


# ------------------------------
# Cog
# ------------------------------
class RulesPanel(commands.Cog):
    """Kleiner Wrapper-Cog: startet das bestehende Welcome-DM Onboarding im Thread."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Panel-View persistieren
        self.bot.add_view(RulesPanelView(self))
        log.info("✅ Rules Panel geladen (Panel-View aktiv)")

        # Safety: Falls der WelcomeDM-Cog Views NICHT registriert hat (z. B. Reihenfolge),
        # versichern wir uns, dass die DM-Step-Views registriert sind.
        self.bot.add_view(IntroView())
        self.bot.add_view(PlayerStatusView())
        self.bot.add_view(SteamLinkNudgeView())
        self.bot.add_view(RulesView())

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

    # ----- Start-Flow -----
    async def start_in_thread(self, interaction: discord.Interaction):
        # Thread anlegen
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

        # 1) Bevorzugt den WelcomeDM-Cog fragen, ob er den Flow liefern kann
        wdm = self.bot.get_cog("WelcomeDM")
        if wdm and hasattr(wdm, "run_flow_in_channel"):
            try:
                await getattr(wdm, "run_flow_in_channel")(thread, interaction.user)  # type: ignore
                return
            except Exception as e:
                log.warning("WelcomeDM.run_flow_in_channel failed, fallback local: %r", e)

        # 2) Fallback: lokaler Mini-Runner (nutzt dieselben Step-Views, damit Texte/Logik aus welcome_dm kommen)
        await self._fallback_flow(thread, interaction.user)

    async def _fallback_flow(self, thread: discord.Thread, user: discord.User):
        """Notfall: Wenn WelcomeDM keine Channel-API hat, nutzen wir die Views direkt."""
        total = 4

        # Intro
        intro = IntroView()
        emb = build_step_embed(
            title="👋 Willkommen in der Deutschen Deadlock Community!",
            desc="Ich helfe dir jetzt, dein Erlebnis hier optimal einzustellen.",
            step=None, total=total, color=0x5865F2,
        )
        ok = await _send_step(thread, emb, intro)
        if not ok:
            return

        # Status
        status = PlayerStatusView()
        emb = build_step_embed(
            title="Frage 1/4 · Wie ist dein Status?",
            desc="Sag kurz, wo du stehst – dann passen wir alles besser an.",
            step=1, total=total, color=0x95A5A6,
        )
        ok = await _send_step(thread, emb, status)
        if not ok:
            return

        # Steam
        steam = SteamLinkNudgeView()
        emb = build_step_embed(
            title="Frage 2/4 · Steam verknüpfen (empfohlen)",
            desc="Für Voice-Status & Features bitte deinen Steam-Account verknüpfen.",
            step=2, total=total, color=0x2ECC71,
        )
        ok = await _send_step(thread, emb, steam)
        if not ok:
            return

        # Regeln
        rules = RulesView()
        emb = build_step_embed(
            title="Frage 3/4 · Regelwerk bestätigen",
            desc="Kurz bestätigen, dass du die Regeln gelesen hast.",
            step=3, total=total, color=0xE67E22,
        )
        await _send_step(thread, emb, rules)


async def setup(bot: commands.Bot):
    await bot.add_cog(RulesPanel(bot))
