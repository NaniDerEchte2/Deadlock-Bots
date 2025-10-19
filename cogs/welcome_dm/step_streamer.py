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
import re
from typing import Optional, Tuple
from urllib.parse import urlparse

try:
    from cogs.twitch import storage as twitch_storage
    from cogs.twitch.base import TwitchBaseCog
except Exception:
    twitch_storage = None  # type: ignore[assignment]
    TwitchBaseCog = None  # type: ignore[assignment]

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
async def _resolve_guild_and_member(
    interaction: discord.Interaction
) -> Tuple[Optional[discord.Guild], Optional[discord.Member]]:
    """
    Liefert (Guild, Member) – robust auch in DMs (via MAIN_GUILD_ID) und bei leerem Cache.
    Nutzt fetch_member() als Fallback (braucht Members-Intent).
    """
    async def _try(g: Optional[discord.Guild]) -> Tuple[Optional[discord.Guild], Optional[discord.Member]]:
        if not g:
            return None, None

        # 1) Wenn Interaction bereits einen Member aus genau dieser Guild liefert
        if isinstance(interaction.user, discord.Member) and getattr(interaction.user.guild, "id", None) == g.id:
            return g, interaction.user  # type: ignore

        # 2) Cache
        m = g.get_member(interaction.user.id)
        if m:
            return g, m

        # 3) Netzwerk-Fetch
        try:
            m = await g.fetch_member(interaction.user.id)
            return g, m
        except Exception as e:
            log.debug("fetch_member failed in guild %s for user %s: %r", getattr(g, "id", "?"), interaction.user.id, e)
            return g, None

    guild = interaction.guild
    g1, m1 = await _try(guild)

    # DM-Fallback über MAIN_GUILD_ID
    if (not g1 or not m1) and MAIN_GUILD_ID:
        bot: commands.Bot = interaction.client  # type: ignore
        mg = bot.get_guild(MAIN_GUILD_ID)
        g2, m2 = await _try(mg)
        if g2 and m2:
            return g2, m2

    return g1, m1


async def _assign_role_and_notify(interaction: discord.Interaction) -> Tuple[bool, str]:
    """
    Vergibt die Streamer-Rolle und pingt den Kontrollkanal.
    Gibt (ok, msg) zurück.
    """
    guild, member = await _resolve_guild_and_member(interaction)
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
                log.debug("Twitch cog '%s' gefunden, aber keine passende register-Methode.", name)
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


async def _safe_send(
    interaction: discord.Interaction,
    *,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    ephemeral: bool = False,
) -> None:
    """
    Sendet sicher eine Nachricht: nutzt followup.send, falls bereits geantwortet wurde.
    """
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
    except Exception as e:
        log.exception("Failed to send response: %r", e)


async def _disable_all_and_edit(
    view: discord.ui.View,
    interaction: discord.Interaction,
    *,
    new_embed: Optional[discord.Embed] = None,
    new_content: Optional[str] = None,
) -> None:
    """
    Deaktiviert alle Buttons und editiert die ursprüngliche Nachricht (falls möglich).
    Funktioniert für Komponenten-Interaktionen auch nach defer().
    """
    for child in view.children:
        try:
            child.disabled = True  # type: ignore[attr-defined]
        except Exception:
            pass

    try:
        if interaction.message:
            await interaction.message.edit(embed=new_embed, content=new_content, view=view)
            return
    except Exception as e:
        log.debug("message.edit failed: %r", e)

    try:
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=new_embed, content=new_content, view=view)
        else:
            await interaction.response.edit_message(embed=new_embed, content=new_content, view=view)
    except Exception as e:
        log.debug("response edit failed: %r", e)


# ------------------------------
# Twitch-Helper
# ------------------------------
def _normalize_twitch_login(raw: str) -> str:
    if TwitchBaseCog is not None:
        try:
            normalized = TwitchBaseCog._normalize_login(raw)
            if normalized:
                return normalized
        except Exception as exc:  # pragma: no cover - defensive fallback
            log.debug("TwitchBaseCog._normalize_login failed: %r", exc)

    value = (raw or "").strip()
    if not value:
        return ""

    value = value.split("?")[0].split("#")[0].strip()
    if "twitch.tv" in value.lower():
        if "//" not in value:
            value = f"https://{value}"
        try:
            parsed = urlparse(value)
            path = (parsed.path or "").strip("/")
            if path:
                value = path.split("/")[0]
        except Exception:
            return ""

    value = value.strip().lstrip("@")
    return re.sub(r"[^a-z0-9_]", "", value.lower())


def _store_twitch_signup(discord_user_id: int, raw_input: str) -> Tuple[bool, Optional[str], str]:
    login = _normalize_twitch_login(raw_input)
    if not login:
        return False, None, "⚠️ Der eingegebene Twitch-Link oder Login wirkt ungültig. Bitte probiere es erneut."

    if twitch_storage is None:
        log.error("Twitch storage module unavailable – cannot persist signup for %s", discord_user_id)
        return False, None, "⚠️ Interner Fehler beim Speichern deines Twitch-Profils. Bitte informiere das Team."

    raw_trimmed = (raw_input or "").strip()

    try:
        with twitch_storage.get_conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO twitch_streamers (twitch_login) VALUES (?)",
                (login,),
            )
            inserted = bool(cur.rowcount)

            conn.execute(
                "UPDATE twitch_streamers "
                "SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL "
                "WHERE twitch_login=?",
                (login,),
            )

            conn.execute(
                "INSERT INTO twitch_signup_requests (discord_user_id, twitch_login, raw_input) VALUES (?, ?, ?)",
                (int(discord_user_id), login, raw_trimmed),
            )
    except Exception as exc:  # pragma: no cover - robust gegen DB-Fehler
        log.exception("Failed to persist Twitch signup for %s: %r", discord_user_id, exc)
        return (
            False,
            None,
            "⚠️ Dein Twitch-Profil konnte nicht gespeichert werden. Bitte versuche es später erneut oder melde dich beim Team.",
        )

    if inserted:
        log.info("Twitch signup stored for user %s with login %s", discord_user_id, login)
    else:
        log.info("Twitch signup updated for user %s with existing login %s", discord_user_id, login)

    return True, login, ""


class StreamerTwitchProfileModal(discord.ui.Modal):
    """Fragt nach dem Twitch-Profil und speichert es direkt unverifiziert."""

    def __init__(self, parent_view: "StreamerIntroView"):
        super().__init__(title="Twitch-Profil angeben")
        self.parent_view = parent_view
        self.twitch_input = discord.ui.TextInput(
            label="Dein Twitch-Profil oder Login",
            placeholder="z. B. twitch.tv/DeinName",
            required=True,
            max_length=100,
        )
        self.add_item(self.twitch_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ok, login, error_msg = _store_twitch_signup(interaction.user.id, str(self.twitch_input.value))
        if not ok or not login:
            if interaction.response.is_done():
                await interaction.followup.send(error_msg, ephemeral=True)
            else:
                await interaction.response.send_message(error_msg, ephemeral=True)
            return

        if self.parent_view is not None and self.parent_view.bound_message is not None:
            for child in self.parent_view.children:
                try:
                    child.disabled = True
                except Exception:
                    pass
            try:
                await self.parent_view.bound_message.edit(view=self.parent_view)
            except Exception as exc:  # pragma: no cover - rein informativ
                log.debug("Failed to disable intro view after modal submit: %r", exc)

        if self.parent_view is not None:
            self.parent_view.force_finish()

        embed = StreamerRequirementsView.build_embed(twitch_login=login)
        view = StreamerRequirementsView()
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=view, ephemeral=False)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # pragma: no cover - defensive
        log.exception("StreamerTwitchProfileModal failed: %r", error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "⚠️ Unerwarteter Fehler beim Speichern deines Twitch-Profils. Bitte probiere es später erneut.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "⚠️ Unerwarteter Fehler beim Speichern deines Twitch-Profils. Bitte probiere es später erneut.",
                    ephemeral=True,
                )
        except Exception:
            log.debug("Modal error response failed", exc_info=True)


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
        super().__init__()

    @staticmethod
    def build_embed(user: discord.abc.User) -> discord.Embed:
        e = discord.Embed(
            title="Streamst du Deadlock?",
            description=(
                "Wir haben einen **Streamer-Bereich**. Wenn du möchtest, kannst du "
                "**Partner** werden – Das sind deine Benefits:\n\n"
                "• **Auto-Promo** in `#live-on-twitch`, sobald du *Deadlock* streamst\n"
                "• **Mehr Sichtbarkeit** in der deutschsprachigen Deadlock-Community\n"
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
        self.bound_message = interaction.message
        await interaction.response.send_modal(StreamerTwitchProfileModal(self))

    @discord.ui.button(
        label="Nein, kein Partner",
        style=discord.ButtonStyle.secondary,
        custom_id="wdm:streamer:intro_no",
    )
    async def btn_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await _safe_send(
            interaction,
            content="Alles klar – du kannst es später mit **/streamer** erneut starten.",
            ephemeral=True,
        )
        await self._finish(interaction)


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
    def build_embed(*, twitch_login: Optional[str] = None) -> discord.Embed:
        intro = ""
        if twitch_login:
            intro = (
                f"Wir haben dein Twitch-Profil **{twitch_login}** gespeichert. "
                "Ein Team-Mitglied prüft es manuell und schaltet dich nach erfolgreicher Kontrolle frei.\n\n"
            )

        description = (
            f"{intro}"
            "Bitte erfülle kurz die Voraussetzungen:\n\n"
            "1) Nutze einen **nicht ablaufenden Invite-Link** zu unserem Server (persönlich für dich).\n"
            "2) Packe den **Server-Link** in deine **Twitch-Bio** – z. B. mit dem Text:\n"
            "   *„Deutscher Deadlock Community Server“*\n"
            "3) Wünchenswert wäre es wenn du Zuschauer auf den Server verweist.\n"
            "4) Genauso wünschenswert ist es, wenn du Deadlock-Content postest, verlinke da gerne den Server.\n\n"
            "Du **darfst** selbstverständlich deinen **eigenen Server** weiterführen – \n"
            "wir verstehen uns nicht als Konkurrenz, sondern als Hub für deutschsprachige Deadlock-Spieler."
        )

        e = discord.Embed(
            title="Partner-Voraussetzungen",
            description=description,
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
            await _safe_send(interaction, content=msg, ephemeral=True)
            await self._finish(interaction)
        else:
            await _safe_send(interaction, content=f"⚠️ {msg}", ephemeral=True)

    @discord.ui.button(
        label="Abbrechen",
        style=discord.ButtonStyle.danger,
        custom_id="wdm:streamer:req_cancel",
    )
    async def btn_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await _safe_send(
            interaction,
            content="Abgebrochen. Du kannst es später mit **/streamer** erneut starten.",
            ephemeral=True,
        )
        await self._finish(interaction)


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
