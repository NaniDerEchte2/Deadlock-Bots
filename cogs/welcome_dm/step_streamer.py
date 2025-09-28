# -*- coding: utf-8 -*-
"""
Zwei-Schritt-Streamer-Onboarding für Deadlock:

Step 1  (StreamerIntroView):
  "Streamst du Deadlock? – möchtest du Partner werden?"
  Buttons:
    - Ja, Partner werden  -> weiter zu Step 2
    - Nein, kein Partner  -> Abbruch

Step 2  (StreamerRequirementsView):
  Zeigt die Anforderungen. Button:
    - Abgeschlossen       -> Rolle vergeben + Kontroll-Ping + (optional) Twitch-Register
    - Abbrechen           -> ohne Änderung beenden

Hinweise:
- Nutzt die bestehende StepView aus cogs/welcome_dm/base.py (keine timeout-Args!)
- Funktioniert in DM, Textkanal und Threads
- Views werden persistent registriert (cog_load)
- /streamer Slash-Command startet Step 1

Konfiguration (ENV optional):
  STREAMER_ROLE_ID               (Default 1313624729466441769)
  STREAMER_NOTIFY_CHANNEL_ID     (Default 1374364800817303632)
  MAIN_GUILD_ID                  (nur für DM-Fallback)
"""

from __future__ import annotations

import os
import logging
from typing import Optional, Tuple

import discord
from discord.ext import commands
from discord import app_commands

# Bestehende StepView aus dem Projekt nutzen
from .base import StepView  # WICHTIG: Diese StepView hat __init__(self) OHNE timeout-Argument

log = logging.getLogger("StreamerOnboarding")

# --- IDs (optional via ENV überschreibbar) ---
STREAMER_ROLE_ID = int(os.getenv("STREAMER_ROLE_ID", "1313624729466441769"))
STREAMER_NOTIFY_CHANNEL_ID = int(os.getenv("STREAMER_NOTIFY_CHANNEL_ID", "1374364800817303632"))
MAIN_GUILD_ID = int(os.getenv("MAIN_GUILD_ID", "0"))  # DM-Fallback, falls interaction.guild None


# ------------------------------
# Utilities
# ------------------------------
def _resolve_guild_and_member(
    interaction: discord.Interaction
) -> Tuple[Optional[discord.Guild], Optional[discord.Member]]:
    """
    Liefert (Guild, Member) – auch in DMs (via MAIN_GUILD_ID).
    """
    guild = interaction.guild
    member: Optional[discord.Member] = None

    if guild:
        if isinstance(interaction.user, discord.Member):
            member = interaction.user
        else:
            try:
                member = guild.get_member(interaction.user.id)
            except Exception as e:
                log.debug("resolve member in guild failed: %r", e)
    else:
        # DM-Kontext: versuche über MAIN_GUILD_ID
        if MAIN_GUILD_ID:
            bot: commands.Bot = interaction.client  # type: ignore
            guild = bot.get_guild(MAIN_GUILD_ID)
            if guild:
                try:
                    member = guild.get_member(interaction.user.id)
                except Exception as e:
                    log.debug("resolve member via MAIN_GUILD_ID failed: %r", e)

    return guild, member


async def _assign_role_and_notify(interaction: discord.Interaction) -> Tuple[bool, str]:
    """
    Vergibt die Streamer-Rolle und pingt den Kontrollkanal.
    Gibt (ok, msg) zurück.
    """
    guild, member = _resolve_guild_and_member(interaction)
    if not guild or not member:
        return False, "Konnte dich in einer Guild nicht auflösen. Bitte schreibe einem Team-Mitglied."

    # 1) Rolle vergeben
    role = guild.get_role(STREAMER_ROLE_ID)
    if not role:
        return False, f"Die Streamer-Rolle ({STREAMER_ROLE_ID}) existiert in dieser Guild nicht."

    try:
        await member.add_roles(role, reason="Streamer-Partner-Setup abgeschlossen")
    except discord.Forbidden:
        return False, "Mir fehlen Berechtigungen, um dir die Streamer-Rolle zu geben. Bitte Team informieren."
    except Exception as e:
        log.error("add_roles failed for %s: %r", member.id, e)
        return False, "Unerwarteter Fehler beim Zuweisen der Rolle. Bitte Team informieren."

    # 2) Twitch-Registrierung (optional, mehrere Cog-Namen probieren)
    try:
        possible_cogs = ("TwitchDeadlock", "TwitchBot", "Twitch")
        for name in possible_cogs:
            cog = interaction.client.get_cog(name)  # type: ignore
            if cog:
                for meth in ("register_streamer", "add_streamer", "register"):
                    if hasattr(cog, meth):
                        try:
                            res = await getattr(cog, meth)(member.id)  # type: ignore
                            log.info("%s.%s(%s) -> %r", name, meth, member.id, res)
                            raise StopIteration  # registriert – keine weiteren Versuche nötig
                        except Exception as e:
                            log.warning("Twitch registration via %s.%s failed for %s: %r", name, meth, member.id, e)
                # Fallback: wenn Cog da, aber keine Methode passt, loggen
                log.debug("Twitch cog '%s' gefunden, aber keine passende register-Methode.", name)
        # ignore if nothing found
    except StopIteration:
        pass
    except Exception as e:
        log.debug("Twitch registration check failed: %r", e)

    # 3) Kontroll-Ping
    notify_ch = interaction.client.get_channel(STREAMER_NOTIFY_CHANNEL_ID)  # type: ignore
    if isinstance(notify_ch, (discord.TextChannel, discord.Thread)):
        try:
            await notify_ch.send(
                f"🔔 {member.mention} hat den **Streamer-Partner-Setup** abgeschlossen – Kontrolle notwendig."
            )
        except Exception as e:
            log.warning("Notify send failed in %s: %r", STREAMER_NOTIFY_CHANNEL_ID, e)
    else:
        log.warning("Notify channel %s nicht gefunden/kein Textkanal.", STREAMER_NOTIFY_CHANNEL_ID)

    return True, "Top! Du hast jetzt die **Streamer-Rolle**. Wir prüfen kurz alles Weitere."


# ------------------------------
# Schritt 1: Intro / Entscheidung
# ------------------------------
class StreamerIntroView(StepView):
    """
    Step 1: "Streamst du Deadlock? – möchtest du Partner werden?"
    Buttons:
      - Ja, Partner werden  -> weiter zu Step 2 (Anforderungen)
      - Nein, kein Partner  -> Abbruch
    """
    def __init__(self):
        # WICHTIG: base.StepView.__init__ nimmt KEIN timeout-Argument!
        super().__init__()

    @staticmethod
    def build_embed(user: discord.abc.User) -> discord.Embed:
        e = discord.Embed(
            title="Streamst du Deadlock?",
            description=(
                "Wir haben einen **Streamer-Bereich**. Wenn du möchtest, kannst du "
                "**Partner** werden – Vorteile kurz & knackig:\n\n"
                "• **Auto-Promo** in `#live-on-twitch`, sobald du *Deadlock* streamst\n"
                "• **Mehr Sichtbarkeit** in der deutschsprachigen Deadlock-Community\n"
                "• **Kein Konkurrenz-Zwang**: Dein eigener Server ist willkommen\n\n"
                "Möchtest du **Partner** werden?"
            ),
            color=0x8A2BE2
        )
        e.set_footer(text="Schritt 1/2")
        return e

    @discord.ui.button(
        label="Ja, Partner werden",
        style=discord.ButtonStyle.success,
        custom_id="wdm:streamer:intro_yes",
    )
    async def btn_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=False)
        # Aktuelle Nachricht „deaktivieren“
        await self._disable_all_and_edit(interaction)
        # Step 2 senden
        await interaction.followup.send(
            embed=StreamerRequirementsView.build_embed(),
            view=StreamerRequirementsView()
        )

    @discord.ui.button(
        label="Nein, kein Partner",
        style=discord.ButtonStyle.secondary,
        custom_id="wdm:streamer:intro_no",
    )
    async def btn_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self._finish(interaction, message="Alles klar – du kannst es später mit **/streamer** erneut starten.")


# ------------------------------
# Schritt 2: Anforderungen + Abschluss
# ------------------------------
class StreamerRequirementsView(StepView):
    """
    Step 2: Zeigt die Anforderungen und vergibt Rolle + pingt Kontrollkanal nach "Abgeschlossen".
    Buttons:
      - Abgeschlossen  -> Rolle vergeben + Notify (+ optional Twitch-Register)
      - Abbrechen      -> ohne Änderung beenden
    """
    def __init__(self):
        super().__init__()

    @staticmethod
    def build_embed() -> discord.Embed:
        e = discord.Embed(
            title="Partner-Voraussetzungen",
            description=(
                "Bitte erfülle kurz die Voraussetzungen:\n\n"
                "1) Nutze einen **nicht ablaufenden Invite-Link** zu unserem Server (persönlich für dich).\n"
                "2) Packe den **Server-Link** in deine **Twitch-Bio** – z. B. mit dem Text:\n"
                "   *„Deutscher Deadlock Community Server“*\n"
                "3) Verweise Zuschauer **aktiv** auf den Server (z. B. Panel/Chat-Command).\n"
                "4) Wenn du Deadlock-Content postest, **verlinke den Server**.\n\n"
                "Du **darfst** selbstverständlich deinen **eigenen Server** weiterführen – "
                "wir verstehen uns nicht als Konkurrenz, sondern als Hub für deutschsprachige Deadlock-Spieler."
            ),
            color=0x32CD32
        )
        e.set_footer(text="Schritt 2/2")
        return e

    @discord.ui.button(
        label="Abgeschlossen",
        style=discord.ButtonStyle.success,
        custom_id="wdm:streamer:req_done",
    )
    async def btn_done(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await _assign_role_and_notify(interaction)
        if ok:
            await self._finish(interaction, message=msg)
        else:
            try:
                await interaction.followup.send(f"⚠️ {msg}", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(
        label="Abbrechen",
        style=discord.ButtonStyle.danger,
        custom_id="wdm:streamer:req_cancel",
    )
    async def btn_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self._finish(interaction, message="Abgebrochen. Du kannst es später mit **/streamer** erneut starten.")


# ---------------------------------------------------------
# Backward-Compat: Export "StreamerView" für bestehende Importe
# ---------------------------------------------------------
class StreamerView(StreamerIntroView):
    """Alias für alte Imports: `from cogs.welcome_dm.step_streamer import StreamerView`."""
    pass


# ------------------------------
# Cog: Registrierung & Slash-Command
# ------------------------------
class StreamerOnboarding(commands.Cog):
    """Registriert die Views und bietet /streamer zum Starten des Flows."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Persistente Views für Reboots registrieren
        self.bot.add_view(StreamerIntroView())
        self.bot.add_view(StreamerRequirementsView())
        log.info("StreamerOnboarding Views registriert (persistent).")

    @app_commands.command(name="streamer", description="Streamer-Partner werden (2 Schritte).")
    async def streamer_cmd(self, interaction: discord.Interaction):
        """Startet Schritt 1 im aktuellen Kanal/Thread oder in DMs."""
        try:
            await interaction.response.send_message(
                embed=StreamerIntroView.build_embed(interaction.user),
                view=StreamerIntroView(),
                ephemeral=False  # bewusst öffentlich, damit Mods ggf. helfen können
            )
        except discord.Forbidden:
            # Fallback auf DM
            try:
                dm = await interaction.user.create_dm()
                await dm.send(embed=StreamerIntroView.build_embed(interaction.user), view=StreamerIntroView())
                await interaction.followup.send("Ich habe dir den Streamer-Setup per DM geschickt.", ephemeral=True)
            except Exception as e:
                log.error("streamer_cmd DM fallback failed: %r", e)
                await interaction.followup.send("Konnte dir keine Nachricht senden. Bitte kontaktiere das Team.", ephemeral=True)
        except Exception as e:
            log.error("streamer_cmd failed: %r", e)
            try:
                await interaction.followup.send("Unerwarteter Fehler beim Start. Bitte probiere es erneut.", ephemeral=True)
            except Exception:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(StreamerOnboarding(bot))
