# -*- coding: utf-8 -*-
"""
Zwei-Schritt-Streamer-Onboarding für Deadlock:

Step 1  (StreamerIntroView):
  "Streamst du Deadlock? – möchtest du Partner werden?"
  Buttons:
    - Ja, Partner werden  -> weiter zu Step 2
    - Nein, kein Partner  -> Abbruch

Step 2  (StreamerRequirementsView):
  Zeigt die Anforderungen und führt durch 3 Schritte:
    1. Voraussetzungen via Modal bestätigen ("bestätigen")
    2. Twitch-Link eintragen (wird gespeichert, aber noch nicht freigeschaltet)
    3. Button "Verifizierung anstoßen" vergibt Rolle + Kontroll-Ping
  Zusätzlich: "Abbrechen" beendet ohne Änderungen.

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

log = logging.getLogger("StreamerOnboarding")

try:
    from cogs.twitch import storage as twitch_storage
    from cogs.twitch.base import TwitchBaseCog
except Exception as exc:  # pragma: no cover - optional dependency
    log.warning("StreamerOnboarding: Twitch-Module nicht verfügbar: %s", exc, exc_info=True)
    twitch_storage = None  # type: ignore[assignment]
    TwitchBaseCog = None  # type: ignore[assignment]

import discord
from discord.ext import commands
from discord import app_commands

# Bestehende StepView aus dem Projekt nutzen
from .base import StepView  # WICHTIG: Diese StepView hat __init__(self) OHNE timeout-Argument

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
    bot: commands.Bot = interaction.client  # type: ignore
    if (not g1 or not m1) and MAIN_GUILD_ID:
        mg = bot.get_guild(MAIN_GUILD_ID)
        g2, m2 = await _try(mg)
        if g2 and m2:
            return g2, m2

    # Letzter Fallback: durchsuche alle Guilds nach einer, die die Streamer-Rolle enthält
    if not g1 or not m1:
        seen_ids = {g1.id} if g1 else set()
        for guild_candidate in bot.guilds:
            if guild_candidate.id in seen_ids:
                continue
            seen_ids.add(guild_candidate.id)

            # Wenn die gesuchte Rolle nicht existiert, lohnt sich kein weiterer Versuch
            if not guild_candidate.get_role(STREAMER_ROLE_ID):
                continue

            g3, m3 = await _try(guild_candidate)
            if g3 and m3:
                return g3, m3

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
        registered = False
        for name in possible_cogs:
            cog = interaction.client.get_cog(name)  # type: ignore
            if not cog:
                continue

            method_found = False
            for meth in ("register_streamer", "add_streamer", "register"):
                if not hasattr(cog, meth):
                    continue

                method_found = True
                try:
                    res = await getattr(cog, meth)(member.id)  # type: ignore[attr-defined]
                    log.info("%s.%s(%s) -> %r", name, meth, member.id, res)
                    registered = True
                    break
                except Exception as e:
                    log.warning("Twitch registration via %s.%s failed for %s: %r", name, meth, member.id, e)

            if not method_found:
                log.debug("Twitch cog '%s' gefunden, aber keine passende register-Methode.", name)

            if registered:
                break
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

    return (
        True,
        "Alles klar! Wir kümmern uns jetzt um deine Verifizierung. Falls wir Rückfragen haben, melden wir uns bei dir.",
    )


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
        except Exception as exc:
            log.debug(
                "Konnte Button %s nicht deaktivieren: %s",
                getattr(child, "custom_id", getattr(child, "label", "?")),
                exc,
            )

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
        except Exception as exc:
            log.debug("Twitch-URL konnte nicht geparst werden (%r): %s", value, exc)
            return ""

    value = value.strip().lstrip("@")
    return re.sub(r"[^a-z0-9_]", "", value.lower())


def _store_twitch_signup(
    discord_user_id: int,
    raw_input: str,
    *,
    discord_display_name: Optional[str] = None,
) -> Tuple[bool, Optional[str], str]:
    login = _normalize_twitch_login(raw_input)
    if not login:
        return False, None, "⚠️ Der eingegebene Twitch-Link oder Login wirkt ungültig. Bitte probiere es erneut."

    if twitch_storage is None:
        log.error("Twitch storage module unavailable – cannot persist signup for %s", discord_user_id)
        return False, None, "⚠️ Interner Fehler beim Speichern deines Twitch-Profils. Bitte informiere das Team."

    try:
        with twitch_storage.get_conn() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO twitch_streamers (twitch_login) VALUES (?)",
                (login,),
            )
            inserted = bool(cur.rowcount)

            conn.execute(
                "UPDATE twitch_streamers "
                "SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL, "
                "    discord_user_id=?, discord_display_name=?, is_on_discord=1 "
                "WHERE twitch_login=?",
                (
                    str(discord_user_id),
                    str(discord_display_name or ""),
                    login,
                ),
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


class StreamerRequirementsAcknowledgementModal(discord.ui.Modal):
    """Fragt aktiv ab, dass die Voraussetzungen verstanden wurden."""

    def __init__(self, parent_view: "StreamerRequirementsView"):
        super().__init__(title="Partner-Voraussetzungen bestätigt")
        self.parent_view = parent_view
        self.confirm_input = discord.ui.TextInput(
            label="Hiermit bestätige ich die Voraussetzungen",
            placeholder="Bitte tippe hier 'bestätigen' ein",
            required=True,
            max_length=20,
        )
        self.add_item(self.confirm_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if str(self.confirm_input.value).strip().lower() != "bestätigen":
            await _safe_send(
                interaction,
                content="⚠️ Bitte gib genau \"bestätigen\" ein, um zu bestätigen, dass du die Voraussetzungen erfüllt hast.",
                ephemeral=True,
            )
            return

        if self.parent_view is not None:
            await self.parent_view.mark_acknowledged(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # pragma: no cover - defensive
        log.exception("StreamerRequirementsAcknowledgementModal failed: %r", error)
        try:
            await _safe_send(
                interaction,
                content="⚠️ Unerwarteter Fehler beim Bestätigen der Voraussetzungen. Bitte probiere es erneut.",
                ephemeral=True,
            )
        except Exception:
            log.debug("Ack modal error response failed", exc_info=True)


class StreamerTwitchProfileModal(discord.ui.Modal):
    """Fragt nach dem Twitch-Profil und speichert es direkt unverifiziert."""

    def __init__(self, parent_view: "StreamerRequirementsView"):
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
        await interaction.response.defer(ephemeral=True)

        display_label = (
            getattr(interaction.user, "global_name", None)
            or getattr(interaction.user, "display_name", None)
            or str(interaction.user)
        )
        ok, login, error_msg = _store_twitch_signup(
            interaction.user.id,
            str(self.twitch_input.value),
            discord_display_name=display_label,
        )
        if not ok or not login:
            await interaction.followup.send(error_msg, ephemeral=True)
            return

        if self.parent_view is not None:
            await self.parent_view.mark_twitch_saved(interaction, twitch_login=login)

        await _safe_send(
            interaction,
            content="✅ Dein Twitch-Profil wurde gespeichert. Starte jetzt die Verifizierung, sobald alles erfüllt ist.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:  # pragma: no cover - defensive
        log.exception("StreamerTwitchProfileModal failed: %r", error)
        try:
            await _safe_send(
                interaction,
                content="⚠️ Unerwarteter Fehler beim Speichern deines Twitch-Profils. Bitte probiere es später erneut.",
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
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(thinking=False)
            except Exception:
                log.debug("Intro defer failed", exc_info=True)

        requirements_view = StreamerRequirementsView()
        requirements_embed = StreamerRequirementsView.build_embed()

        sent_message: Optional[discord.Message] = None

        # Entferne die ursprüngliche Intro-Nachricht, damit nur noch die Anforderungen sichtbar sind.
        try:
            if interaction.message:
                await interaction.message.delete()
        except Exception:
            log.debug("Konnte Intro-Nachricht nicht löschen.", exc_info=True)

        try:
            channel = interaction.channel
            if channel is None:
                if isinstance(interaction.user, (discord.User, discord.Member)):
                    channel = await interaction.user.create_dm()

            if channel is not None:
                sent_message = await channel.send(embed=requirements_embed, view=requirements_view)
            else:
                sent_message = await interaction.followup.send(
                    embed=requirements_embed,
                    view=requirements_view,
                    wait=True,
                )
        except Exception:
            log.exception("Senden der Anforderungen fehlgeschlagen")
            await _safe_send(
                interaction,
                content="⚠️ Die Anforderungen konnten nicht angezeigt werden. Bitte versuche es später erneut.",
                ephemeral=True,
            )
            self.stop()
            return

        if hasattr(requirements_view, "bound_message") and sent_message is not None:
            requirements_view.bound_message = sent_message

        try:
            await requirements_view.wait()
        finally:
            # Weiter mit dem Welcome-Flow, nachdem die Anforderungen abgeschlossen oder abgebrochen wurden.
            self.proceed = getattr(requirements_view, "proceed", False)
            self.stop()

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
    """Mehrstufige Erfassung der Voraussetzungen, Twitch-Daten und finaler Start der Verifizierung."""

    def __init__(self):
        super().__init__()
        self.acknowledged = False
        self.twitch_login: Optional[str] = None
        self.raid_bot_authorized = False
        self.verification_started = False
        self.verification_message: Optional[str] = None
        self._sync_button_states()

    @staticmethod
    def build_embed(
        *,
        acknowledged: bool = False,
        twitch_login: Optional[str] = None,
        raid_bot_authorized: bool = False,
        verification_started: bool = False,
        verification_message: Optional[str] = None,
    ) -> discord.Embed:
        twitch_entry = f"{'✅' if twitch_login else '⬜'} Twitch-Profil gespeichert"
        if twitch_login:
            twitch_entry += f" (**{twitch_login}**)"

        raid_entry = f"{'✅' if raid_bot_authorized else '⬜'} Raid-Bot autorisiert"

        checklist = [
            f"{'✅' if acknowledged else '⬜'} Voraussetzungen bestätigt",
            twitch_entry,
            raid_entry,
            f"{'✅' if verification_started else '⬜'} Verifizierung angestoßen",
        ]

        checklist_text = "\n".join(checklist)

        requirement_text = (
            "📋 **Voraussetzungen:**\n\n"
            "**1️⃣ Invite-Link erstellen**\n"
            " Rechtsklick auf den Server → *Leute einladen* → **„Einladungslink bearbeiten"**\n"
            " Stelle ein: `Läuft ab: Nie` · `Kein Limit`\n\n"

            "**2️⃣ Twitch-Bio anpassen**\n"
            " Füge den Server-Link in deine Bio ein, z. B.:\n"
            "> *„Deutscher Deadlock Community Server"*\n\n"

            "**3️⃣ Raid-Bot aktivieren (PFLICHT)**\n"
            " Was macht der Bot?\n"
            "• Der Bot übernimmt für dich automatische Raids, wenn du offline gehst\n"
            "• Er schickt deine Viewer zu einem anderen Partner, der gerade live ist\n"
            "• Die Auswahl läuft nach Fairness: Wer weniger Raids bekommen hat, wird bevorzugt\n"
            "• So hilfst du anderen beim Wachsen und bekommst selbst Unterstützung zurück\n"
            "• Du kannst es jederzeit in deinem Chat mit `!raid_disable` pausieren\n\n"
            " **Wichtig:** Der Bot braucht deine Erlaubnis auf Twitch.\n"
            " Klick einfach auf den Button unten, autorisier ihn auf Twitch, fertig.\n"
            " Er kann nur raiden, sonst nix.\n\n"

            "**4️⃣ Unterstützung & Promo**\n"
            "• Wenn du Deadlock streamst oder Content erstellst, kannst du gern in den Promo-Kanälen posten.\n"
            "• Erwähne den Server in Stream oder Chat und lade interessierte Zuschauer oder Mitspieler ein.\n"
            "• Je mehr aktive Spieler zusammenkommen, desto stärker wächst die Community – "
            "*eine Hand wäscht die andere.* ❤️\n\n"

            "──────────────────────────────\n\n"
            "**Eigener Discord? Kein Problem!**\n"
            "• Du kannst natürlich weiterhin deinen eigenen Server führen – wir sehen uns nicht als Konkurrenz,\n"
            "  sondern als zentralen Treffpunkt für deutschsprachige Deadlock-Spieler.\n"
            "• Schau gerne hin und wieder bei uns vorbei – je mehr du mit anderen spielst, desto sichtbarer wirst du,\n"
            "  und die Community lernt dich als Teil von uns kennen – nicht nur als jemand, der streamt.\n\n"

            "Wir prüfen selbstverständlich, ob du alle Voraussetzungen erfüllst."
        )


        if twitch_login:
            requirement_text = (
                f"Wir haben dein Twitch-Profil **{twitch_login}** gespeichert. "
                "Ein Team-Mitglied prüft es manuell und schaltet dich nach erfolgreicher Kontrolle frei.\n\n"
                f"{requirement_text}"
            )

        embed_description = f"{checklist_text}\n\n{requirement_text}" if checklist_text else requirement_text

        if verification_started:
            followup = (
                verification_message
                or "Danke! Wir prüfen jetzt alles und melden uns, sobald die manuelle Kontrolle abgeschlossen ist."
            )
            embed_description += f"\n\n{followup}"
        else:
            embed_description += (
                "\n\nNutze die Buttons unten, um zuerst die Voraussetzungen zu bestätigen, danach deinen Twitch-Link "
                "anzugeben und im letzten Schritt die Verifizierung anzustoßen."
            )


        e = discord.Embed(
            title="Partner-Voraussetzungen",
            description=embed_description,
            color=0x32CD32,
        )
        e.set_footer(text="Schritt 2/2")
        return e

    def _sync_button_states(self) -> None:
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue

            if child.custom_id == "wdm:streamer:req_ack":
                child.disabled = self.acknowledged
            elif child.custom_id == "wdm:streamer:req_twitch":
                child.disabled = (not self.acknowledged) or self.verification_started
            elif child.custom_id == "wdm:streamer:req_raid_bot":
                child.disabled = (not self.twitch_login) or self.raid_bot_authorized or self.verification_started
            elif child.custom_id == "wdm:streamer:req_verify":
                child.disabled = not (self.acknowledged and self.twitch_login and self.raid_bot_authorized and not self.verification_started)
            elif child.custom_id == "wdm:streamer:req_cancel":
                child.disabled = self.verification_started

    async def _update_message(self, interaction: discord.Interaction) -> None:
        self._sync_button_states()
        embed = self.build_embed(
            acknowledged=self.acknowledged,
            twitch_login=self.twitch_login,
            raid_bot_authorized=self.raid_bot_authorized,
            verification_started=self.verification_started,
            verification_message=self.verification_message,
        )

        target_message = getattr(self, "bound_message", None)
        if target_message is not None:
            try:
                await target_message.edit(embed=embed, view=self)
                return
            except Exception as exc:  # pragma: no cover - fallback auf Interaction
                log.debug("Failed to edit bound message: %r", exc)

        try:
            if interaction.message:
                await interaction.message.edit(embed=embed, view=self)
            elif interaction.response.is_done():
                await interaction.edit_original_response(embed=embed, view=self)
        except Exception as exc:  # pragma: no cover - defensive logging
            log.debug("Failed to update requirements message: %r", exc)

    @discord.ui.button(
        label="Voraussetzungen bestätigen",
        style=discord.ButtonStyle.primary,
        custom_id="wdm:streamer:req_ack",
    )
    async def btn_acknowledge(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.acknowledged:
            await _safe_send(
                interaction,
                content="Du hast die Voraussetzungen bereits bestätigt.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(StreamerRequirementsAcknowledgementModal(self))

    @discord.ui.button(
        label="Twitch-Link angeben",
        style=discord.ButtonStyle.secondary,
        custom_id="wdm:streamer:req_twitch",
    )
    async def btn_twitch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.acknowledged:
            await _safe_send(
                interaction,
                content="Bitte bestätige zuerst, dass du die Voraussetzungen gelesen hast.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(StreamerTwitchProfileModal(self))

    @discord.ui.button(
        label="🎯 Raid-Bot autorisieren",
        style=discord.ButtonStyle.primary,
        custom_id="wdm:streamer:req_raid_bot",
    )
    async def btn_raid_bot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.twitch_login:
            await _safe_send(
                interaction,
                content="Bitte gib zuerst deinen Twitch-Link an, bevor du den Raid-Bot autorisierst.",
                ephemeral=True,
            )
            return

        if self.raid_bot_authorized:
            await _safe_send(
                interaction,
                content="Du hast den Raid-Bot bereits autorisiert.",
                ephemeral=True,
            )
            return

        # Twitch Cog finden und OAuth-URL generieren
        try:
            possible_cogs = ("TwitchDeadlock", "TwitchBot", "Twitch")
            raid_bot = None
            for name in possible_cogs:
                cog = interaction.client.get_cog(name)  # type: ignore
                if cog and hasattr(cog, "_raid_bot"):
                    raid_bot = cog._raid_bot  # type: ignore
                    break

            if not raid_bot:
                await _safe_send(
                    interaction,
                    content="⚠️ Raid-Bot ist derzeit nicht verfügbar. Bitte informiere einen Admin.",
                    ephemeral=True,
                )
                return

            # OAuth-URL generieren
            auth_url = raid_bot.auth_manager.generate_auth_url(self.twitch_login)

            # View mit Link-Button erstellen
            view = discord.ui.View()
            view.add_item(
                discord.ui.Button(
                    label="Auf Twitch autorisieren",
                    url=auth_url,
                    style=discord.ButtonStyle.link,
                )
            )

            await _safe_send(
                interaction,
                content=(
                    f"**Raid-Bot autorisieren für {self.twitch_login}**\n\n"
                    "Klick auf den Button unten, um den Bot auf Twitch zu autorisieren.\n\n"
                    "**Was passiert danach?**\n"
                    "• Der Bot kann in deinem Namen raiden (NUR raiden, nichts anderes)\n"
                    "• Wenn du offline gehst, raidet er automatisch einen Partner\n"
                    "• Du kannst es jederzeit mit `!raid_disable` in deinem Chat ausschalten\n\n"
                    "**Nachdem du autorisiert hast:**\n"
                    "Komm zurück und klick unten auf **'Ich habe autorisiert'**, damit wir das abhaken können."
                ),
                embed=None,
                ephemeral=True,
            )

            # Followup mit Link
            await interaction.followup.send(
                view=view,
                ephemeral=True
            )

            # Confirmations-Button zum Abhaken
            confirm_view = discord.ui.View(timeout=None)
            confirm_button = discord.ui.Button(
                label="✅ Ich habe autorisiert",
                style=discord.ButtonStyle.success,
                custom_id=f"wdm:streamer:raid_confirmed:{interaction.user.id}",
            )

            async def confirm_callback(btn_interaction: discord.Interaction):
                if btn_interaction.user.id != interaction.user.id:
                    await btn_interaction.response.send_message(
                        "Dieser Button ist nicht für dich.",
                        ephemeral=True
                    )
                    return

                await btn_interaction.response.defer(ephemeral=True)

                # Prüfe, ob Autorisierung in DB vorhanden
                if twitch_storage:
                    try:
                        with twitch_storage.get_conn() as conn:
                            row = conn.execute(
                                "SELECT raid_enabled FROM twitch_raid_auth WHERE twitch_login = ?",
                                (self.twitch_login,),
                            ).fetchone()

                        if row:
                            self.raid_bot_authorized = True
                            await self._update_message(btn_interaction)
                            await btn_interaction.followup.send(
                                "✅ Raid-Bot erfolgreich autorisiert! Du kannst jetzt die Verifizierung anstoßen.",
                                ephemeral=True
                            )
                            confirm_button.disabled = True
                            await btn_interaction.message.edit(view=confirm_view)  # type: ignore
                        else:
                            await btn_interaction.followup.send(
                                "⚠️ Ich konnte deine Autorisierung noch nicht in der Datenbank finden. "
                                "Stelle sicher, dass du den Bot auf Twitch autorisiert hast, und versuche es dann erneut.",
                                ephemeral=True
                            )
                    except Exception as e:
                        log.exception("Failed to check raid auth: %r", e)
                        await btn_interaction.followup.send(
                            "⚠️ Fehler beim Prüfen der Autorisierung. Bitte versuche es erneut.",
                            ephemeral=True
                        )

            confirm_button.callback = confirm_callback
            confirm_view.add_item(confirm_button)

            await interaction.followup.send(
                "Sobald du auf Twitch autorisiert hast, klick hier:",
                view=confirm_view,
                ephemeral=True
            )

        except Exception as e:
            log.exception("Raid bot authorization failed: %r", e)
            await _safe_send(
                interaction,
                content="⚠️ Fehler beim Generieren des Autorisierungs-Links. Bitte informiere einen Admin.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Verifizierung anstoßen",
        style=discord.ButtonStyle.success,
        custom_id="wdm:streamer:req_verify",
    )
    async def btn_verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.acknowledged or not self.twitch_login or not self.raid_bot_authorized:
            missing = []
            if not self.acknowledged:
                missing.append("Voraussetzungen bestätigen")
            if not self.twitch_login:
                missing.append("Twitch-Profil angeben")
            if not self.raid_bot_authorized:
                missing.append("Raid-Bot autorisieren")

            await _safe_send(
                interaction,
                content=f"⚠️ Bitte erledige noch folgende Schritte:\n• " + "\n• ".join(missing),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        assign_ok, assign_msg = await _assign_role_and_notify(interaction)
        if not assign_ok:
            await interaction.followup.send(f"⚠️ {assign_msg}", ephemeral=True)
            return

        self.verification_started = True
        self.verification_message = assign_msg
        await self._update_message(interaction)
        await interaction.followup.send(assign_msg, ephemeral=True)
        await self._finish(interaction)

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

    async def mark_acknowledged(self, interaction: discord.Interaction) -> None:
        self.acknowledged = True
        await self._update_message(interaction)
        await _safe_send(
            interaction,
            content=(
                "Danke! Wir schauen uns kurz an, ob du alle Voraussetzungen erfüllst. "
                "Als nächstes gib bitte deinen Twitch-Link an."
            ),
            ephemeral=True,
        )

    async def mark_twitch_saved(self, interaction: discord.Interaction, *, twitch_login: str) -> None:
        self.twitch_login = twitch_login
        await self._update_message(interaction)



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
        """Startet Schritt 1 direkt per DM und bestaetigt hier nur kurz."""
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            log.debug("streamer_cmd defer failed", exc_info=True)

        try:
            dm = await interaction.user.create_dm()
            await dm.send(
                embed=StreamerIntroView.build_embed(interaction.user),
                view=StreamerIntroView(),
            )
            await _safe_send(
                interaction,
                content=(
                    "Ich habe dir den Streamer-Setup in die DMs geschickt. "
                    "Schau dort vorbei; die Buttons bleiben persistent."
                ),
                ephemeral=True,
            )
        except discord.Forbidden:
            await _safe_send(
                interaction,
                content=(
                    "Ich konnte dir keine DM senden. "
                    "Bitte erlaube Direktnachrichten vom Server oder kontaktiere das Team."
                ),
                ephemeral=True,
            )
        except Exception as e:
            log.error("streamer_cmd failed: %r", e)
            await _safe_send(
                interaction,
                content="Unerwarteter Fehler beim Start. Bitte probiere es erneut.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(StreamerOnboarding(bot))
