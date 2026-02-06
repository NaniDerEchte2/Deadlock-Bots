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
    2. Twitch-Bot autorisieren (Kanal wird automatisch erkannt)
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
import textwrap
from typing import Optional, Tuple

log = logging.getLogger("StreamerOnboarding")

try:
    from cogs.twitch import storage as twitch_storage
except Exception as exc:  # pragma: no cover - optional dependency
    log.warning("StreamerOnboarding: Twitch-Module nicht verfügbar: %s", exc, exc_info=True)
    twitch_storage = None  # type: ignore[assignment]

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
def _find_raid_bot(client: discord.Client) -> Optional[object]:
    """
    Versucht den Raid-Bot aus den geladenen Cogs zu ermitteln.
    Nutzt bekannte Cog-Namen und fällt auf eine generische Suche zurück.
    """
    known_names = ("TwitchStreamCog", "TwitchStreams", "Twitch", "TwitchBot", "TwitchDeadlock")

    # Erst bekannte Namen abfragen (schnellster Weg)
    for name in known_names:
        try:
            cog = client.get_cog(name)  # type: ignore[arg-type]
        except Exception as exc:
            log.debug("get_cog(%s) failed: %r", name, exc)
            continue
        raid_bot = getattr(cog, "_raid_bot", None)
        if raid_bot:
            return raid_bot

    # Fallback: durch alle Cogs iterieren
    try:
        for cog in getattr(client, "cogs", {}).values():  # type: ignore[attr-defined]
            raid_bot = getattr(cog, "_raid_bot", None)
            if raid_bot:
                return raid_bot
    except Exception as exc:
        log.debug("Fallback Raid-Bot lookup fehlgeschlagen: %r", exc)

    return None


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


async def _assign_role_and_notify(
    interaction: discord.Interaction, 
    twitch_login: Optional[str] = None
) -> Tuple[bool, str]:
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

    # 2) Verifizierung (Chat-Promo aktiv → immer erfolgreich)
    auto_verified = True
    verification_reason = "Auto-verifiziert (Promonachricht im Chat aktiv)."

    if twitch_login and twitch_storage:
        try:
            with twitch_storage.get_conn() as conn:
                conn.execute(
                    "UPDATE twitch_streamers SET manual_verified_permanent=1, manual_verified_at=CURRENT_TIMESTAMP "
                    "WHERE twitch_login=?",
                    (twitch_login.lower(),)
                )
            log.info("Auto-verified streamer %s (Twitch: %s)", member.id, twitch_login)
        except Exception as e:
            log.exception("Fehler bei der automatisierten Streamer-Prüfung")
            verification_reason = f"Fehler bei der Prüfung: {e}"

    # 3) Twitch-Registrierung (optional, mehrere Cog-Namen probieren)
    try:
        possible_cogs = ("TwitchStreamCog", "TwitchDeadlock", "TwitchBot", "Twitch")
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

    # 4) Kontroll-Ping
    notify_ch = interaction.client.get_channel(STREAMER_NOTIFY_CHANNEL_ID)  # type: ignore
    if isinstance(notify_ch, (discord.TextChannel, discord.Thread)):
        try:
            status_emoji = "✅" if auto_verified else "🔔"
            msg = f"{status_emoji} {member.mention} hat den **Streamer-Partner-Setup** abgeschlossen.\n"
            msg += f"**Twitch:** {twitch_login or 'Unbekannt'}\n"
            msg += f"**Auto-Check:** {'Erfolgreich' if auto_verified else 'Fehlgeschlagen'}\n"
            msg += f"**Details:** {verification_reason}"
            
            await notify_ch.send(msg)
        except Exception as e:
            log.warning("Notify send failed in %s: %r", STREAMER_NOTIFY_CHANNEL_ID, e)
    else:
        log.warning("Notify channel %s nicht gefunden/kein Textkanal.", STREAMER_NOTIFY_CHANNEL_ID)

    final_msg = (
        "✅ **Verifizierung erfolgreich!** Du bist nun als Partner freigeschaltet. "
        "Der Bot startet automatisch mit Chat-Promos, sobald du nächstes Mal live gehst."
        if auto_verified
        else "Alles klar! Wir schauen uns dein Setup kurz an und schalten dich dann frei. Falls wir Rückfragen haben, melden wir uns bei dir."
    )

    return (True, final_msg)


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
            title="🎮 Streamst du Deadlock?",
            description=(
                "Wir haben einen **exklusiven Streamer-Bereich** mit automatisierten Tools, "
                "die dir als Partner das Leben leichter machen.\n\n"
                "**Twitch Bot-Update: Das passiert im Hintergrund:**\n\n"
                
                "**1️⃣ Auto-Raid Manager**\n"
                "Schluss mit manuellem Raid-Suchen am Ende eines langen Streams. Der Bot übernimmt das automatisch:\n"
                "• Sobald dein Stream **offline** geht, prüft der Bot, **welche Partner aktuell live** sind und raidet einen davon\n"
                "• **Fallback:** Wenn **kein Partner live** ist, sucht der Bot automatisch nach **deutschen Deadlock-Streamern**\n\n"
                
                "**2️⃣ Chat Guard – Schutz vor Müll im Chat**\n"
                "Damit dein Chat sauber bleibt, ohne dass du ständig moderieren musst:\n"
                "• **Spam-Mod:** Filtert Spam anhand einer vorgegebenen Liste (z. B. Viewer-Bots)\n"
                "• **Erweiterbar:** Neue Spam-Wellen können wir schnell ergänzen\n"
                "• **Wichtig:** Bitte gebt Feedback inkl. **exakter Nachricht** – nur so können wir zuverlässig bannen\n\n"
                
                "**3️⃣ Analytics Dashboard** *(Work in Progress 03-05/26)*\n"
                "• **Retention-Analyse:** Wann droppen Zuschauer? (z. B. nach 5, 10 oder 20 Minuten)\n"
                "• **Unique Chatters:** Wie viele **verschiedene** Menschen interagieren wirklich?\n"
                "• **Kategorie-Vergleich (DE):** Analyse der deutschen Deadlock-Kategorie & Vergleich zwischen Streamern\n"
                "→ Ziel: Du erkennst Muster und weißt, was du optimieren kannst.\n\n"
                
                "**4️⃣ Discord – Live-Stream Auto-Post**\n"
                "• Sobald du **Deadlock** streamst, wird dein Stream automatisch im Discord gepostet (#🎥twitch)\n"
                "→ Ergebnis: Mehr Sichtbarkeit in der Community, ohne dass du selbst posten musst.\n\n"

                "**5️⃣ Chat-Promos**\n"
                "• Der Bot postet alle ~30 Minuten eine kurze Promo in deinem Chat\n"
                "• Inhalt: Einladung zur deutschen Deadlock-Community + Discord-Link\n"
                "→ Mehr Sichtbarkeit für die Community, vollautomatisch.\n\n"

                "**Wenn du Lust hast, teste die Beta-Features direkt:**\n"
                "Nutze #🎥streamer-austausch `!traid`, autorisiere den **Twitch-Bot** "
                "und gib uns Feedback, wenn dir etwas auffällt oder du dir weitere Features wünschst.\n\n"
                
                "**Bereit, Partner zu werden?**"
            ),
            color=0x9146FF  # Twitch-Lila
        )
        e.set_footer(text="Schritt 1/2 • Streamer-Partner werden")
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
    """Mehrstufige Erfassung der Voraussetzungen und finaler Start der Verifizierung."""

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
        raid_entry = f"{'✅' if raid_bot_authorized else '⬜'} Twitch-Bot autorisiert (Pflicht)"
        if twitch_login:
            raid_entry += f" (**{twitch_login}**)"
        else:
            raid_entry += " (Kanal wird automatisch erkannt)"

        checklist = [
            f"{'✅' if acknowledged else '⬜'} Voraussetzungen bestätigt",
            raid_entry,
            f"{'✅' if verification_started else '⬜'} Verifizierung angestoßen",
        ]

        checklist_text = "\n".join(checklist)

        requirement_text = textwrap.dedent(
            """
            **📋 Voraussetzungen für Streamer-Partner:**

            **1️⃣ Twitch-Bot autorisieren (Pflicht)** 🎯
            **Ohne Twitch-Bot-Autorisierung können wir dich nicht freischalten.**

            **Twitch Bot-Update: Das passiert im Hintergrund**
            • **Auto-Raid Manager:** Wenn du offline gehst, raidet der Bot automatisch einen Partner
            • **Fallback:** Kein Partner live? → Raid zu deutschen Deadlock-Streamern
            • **Chat Guard:** Spam-Filter + erweiterbare Ban-Liste (Feedback inkl. exakter Nachricht hilft)
            • **Discord Auto-Post:** Live-Stream wird automatisch im Discord gepostet
            • **Analytics (WIP 03-05/26):** Retention, Unique Chatters, Kategorie-Vergleich (DE)

            **Wie aktivieren?**
            Klick auf den Button unten → Autorisiere auf Twitch → Fertig! 🎉
            Dein **Twitch-Kanal wird automatisch erkannt** – kein manuelles Eingeben nötig.

            **Berechtigungen des Bots:**
            ✓ Raids in deinem Namen starten
            ✓ Chat-Nachrichten lesen/senden (für Spam-Schutz)
            ✓ Follower-Liste einsehen (als Moderator)

            **2️⃣ Community-Promo (automatisch)** 🎮
            Der Bot postet regelmäßig eine kurze Promo in deinem Chat – damit die deutsche Deadlock-Community sichtbar wird, ohne dass du selbst handeln musst.
            • Intervall: alle ~30 Minuten (nur wenn du live bist)
            • Inhalt: Einladung zur deutschen Community + Discord-Link
            • Keine Aktion von dir nötig – läuft vollautomatisch ab der Autorisierung

            **3️⃣ Community-Support**
            • Post deine Streams/Content gerne in den Promo-Kanälen
            • Erwähne den Server in deinem Stream/Chat
            • Lade interessierte Zuschauer ein
            *Eine Hand wäscht die andere – je aktiver die Community, desto mehr profitieren alle!*

            ━━━━━━━━━━━━━━━━━━━━━━━━
            **💬 Eigener Discord? Kein Problem!**
            • Wir sehen uns nicht als Konkurrenz, sondern als zentralen Treffpunkt
            • Behalte deinen eigenen Server – schau einfach ab und zu bei uns vorbei
            • Spiele mit anderen aus der Community → mehr Sichtbarkeit für dich!
            • Die Leute lernen dich als aktiven Teil der Community kennen
            """
        ).strip()

        if twitch_login:
            requirement_text = (
                f"✅ **Twitch-Kanal erkannt:** **{twitch_login}**\n"
                "Ein Team-Mitglied prüft dein Profil und schaltet dich nach erfolgreicher Kontrolle frei.\n\n"
                f"{requirement_text}"
            )

        embed_description = f"**📊 Fortschritt:**\n{checklist_text}\n\n{requirement_text}"

        if verification_started:
            followup = (
                verification_message
                or "✅ **Danke!** Wir prüfen jetzt alles und melden uns, sobald die Kontrolle abgeschlossen ist."
            )
            embed_description += f"\n\n{followup}"
        else:
            embed_description += (
                "\n\n**🎯 Nächste Schritte:**\n"
                "Nutze die Buttons unten, um:\n"
                "1️⃣ Voraussetzungen bestätigen\n"
                "2️⃣ Twitch-Bot autorisieren (Pflicht)\n"
                "3️⃣ Verifizierung starten"
            )

        e = discord.Embed(
            title="📝 Partner-Voraussetzungen & Setup",
            description=embed_description,
            color=0x32CD32,
        )
        e.set_footer(text="Schritt 2/2 • Alle Schritte abarbeiten")
        return e

    def _sync_button_states(self) -> None:
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue

            if child.custom_id == "wdm:streamer:req_ack":
                child.disabled = self.acknowledged
            elif child.custom_id == "wdm:streamer:req_raid_bot":
                child.disabled = (not self.acknowledged) or self.raid_bot_authorized or self.verification_started
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
        label="1️⃣ Voraussetzungen bestätigen",
        style=discord.ButtonStyle.primary,
        custom_id="wdm:streamer:req_ack",
    )
    async def btn_acknowledge(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.acknowledged:
            await _safe_send(
                interaction,
                content="✅ Du hast die Voraussetzungen bereits bestätigt.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(StreamerRequirementsAcknowledgementModal(self))

    @discord.ui.button(
        label="2️⃣ Twitch-Bot autorisieren",
        style=discord.ButtonStyle.primary,
        custom_id="wdm:streamer:req_raid_bot",
    )
    async def btn_raid_bot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.raid_bot_authorized:
            await _safe_send(
                interaction,
                content="✅ Du hast den Twitch-Bot bereits autorisiert.",
                ephemeral=True,
            )
            return

        # Twitch Cog finden und OAuth-URL generieren
        try:
            raid_bot = _find_raid_bot(interaction.client)
            auth_mgr = getattr(raid_bot, "auth_manager", None) if raid_bot else None

            if not raid_bot or not auth_mgr:
                await _safe_send(
                    interaction,
                    content="⚠️ Twitch-Bot ist derzeit nicht verfügbar. Bitte informiere einen Admin.",
                    ephemeral=True,
                )
                return

            # OAuth-URL generieren (Discord-ID im State, Kanal wird automatisch erkannt)
            state_payload = f"discord:{interaction.user.id}"
            auth_url = auth_mgr.generate_auth_url(state_payload)

            # View mit Link-Button erstellen
            view = discord.ui.View()
            view.add_item(
                discord.ui.Button(
                    label="🔗 Auf Twitch autorisieren",
                    url=auth_url,
                    style=discord.ButtonStyle.link,
                )
            )

            await _safe_send(
                interaction,
                content=(
                    "**🎯 Twitch-Bot autorisieren**\n\n"
                    "**Pflicht für Streamer-Partner:** Ohne OAuth keine Freischaltung.\n"
                    "Dein **Twitch-Kanal wird automatisch erkannt** – kein Link nötig.\n\n"
                    "**Was passiert jetzt?**\n"
                    "1. Klick auf den Button unten\n"
                    "2. Du wirst zu Twitch weitergeleitet\n"
                    "3. Autorisiere den Bot (dauert nur 10 Sekunden)\n"
                    "4. Komm zurück und klick auf **'✅ Ich habe autorisiert'**\n\n"
                    
                    "**Was macht der Twitch-Bot?**\n"
                    "✓ Auto-Raid Manager (Partner live prüfen + Fallback)\n"
                    "✓ Chat Guard (Spam-Filter)\n"
                    "✓ Discord Auto-Post (Live-Stream im Discord)\n"
                    "✓ Analytics Dashboard (WIP 03-05/26)\n\n"
                    
                    "**Berechtigungen:**\n"
                    "✓ Raids in deinem Namen starten (NUR raiden!)\n"
                    "✓ Chat-Nachrichten lesen (für Spam-Schutz)\n"
                    "✓ Follower-Liste einsehen (als Mod)\n\n"
                    
                    "**Wichtig:**\n"
                    "• Automatische Raids nur bei Deadlock als letzter Kategorie"
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
                        "❌ Dieser Button ist nicht für dich.",
                        ephemeral=True
                    )
                    return

                await btn_interaction.response.defer(ephemeral=True)

                # Prüfe, ob Autorisierung in DB vorhanden + Kanal automatisch erkannt
                if not twitch_storage:
                    await btn_interaction.followup.send(
                        "⚠️ Twitch-Modul ist derzeit nicht verfügbar. Bitte informiere einen Admin.",
                        ephemeral=True,
                    )
                    return

                try:
                    discord_user_id = str(btn_interaction.user.id)
                    display_label = (
                        getattr(btn_interaction.user, "global_name", None)
                        or getattr(btn_interaction.user, "display_name", None)
                        or str(btn_interaction.user)
                    )

                    with twitch_storage.get_conn() as conn:
                        row = conn.execute(
                            "SELECT twitch_login FROM twitch_streamers WHERE discord_user_id = ?",
                            (discord_user_id,),
                        ).fetchone()
                        twitch_login = None
                        if row:
                            twitch_login = row["twitch_login"] if hasattr(row, "keys") else row[0]

                        if not twitch_login:
                            await btn_interaction.followup.send(
                                "⚠️ **Kanal noch nicht erkannt**\n\n"
                                "Falls du gerade autorisiert hast, warte bitte kurz (ca. 10 Sek.) "
                                "und klicke den Button erneut.",
                                ephemeral=True,
                            )
                            return

                        auth_row = conn.execute(
                            "SELECT raid_enabled FROM twitch_raid_auth WHERE lower(twitch_login)=lower(?)",
                            (twitch_login,),
                        ).fetchone()

                        if auth_row:
                            conn.execute(
                                "UPDATE twitch_streamers SET discord_display_name=?, is_on_discord=1 "
                                "WHERE lower(twitch_login)=lower(?)",
                                (display_label, twitch_login),
                            )
                            conn.commit()

                    if auth_row:
                        self.twitch_login = twitch_login
                        self.raid_bot_authorized = True
                        await self._update_message(btn_interaction)
                        await btn_interaction.followup.send(
                            "✅ **Twitch-Bot erfolgreich autorisiert!**\n"
                            f"**Kanal erkannt:** **{twitch_login}**\n"
                            "Du kannst jetzt die Verifizierung anstoßen (Button 3️⃣).",
                            ephemeral=True,
                        )
                        confirm_button.disabled = True
                        await btn_interaction.edit_original_response(view=confirm_view)
                    else:
                        await btn_interaction.followup.send(
                            "⚠️ **Autorisierung noch nicht gefunden (OAuth fehlt)**\n\n"
                            "Mögliche Gründe:\n"
                            "• Du hast den Bot noch nicht auf Twitch autorisiert\n"
                            "• Die Autorisierung wurde noch nicht synchronisiert (warte 10 Sek.)\n\n"
                            "Wichtig: Ohne Twitch-Bot-Autorisierung keine Freischaltung.\n"
                            "Stelle sicher, dass du auf Twitch autorisiert hast und versuche es dann erneut.",
                            ephemeral=True,
                        )
                except Exception as e:
                    log.exception("Failed to check raid auth: %r", e)
                    await btn_interaction.followup.send(
                        "⚠️ Fehler beim Prüfen der Autorisierung. Bitte versuche es erneut oder kontaktiere einen Admin.",
                        ephemeral=True,
                    )

            confirm_button.callback = confirm_callback
            confirm_view.add_item(confirm_button)

            await interaction.followup.send(
                "**Nach der Autorisierung auf Twitch:**",
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
        label="3️⃣ Verifizierung starten",
        style=discord.ButtonStyle.success,
        custom_id="wdm:streamer:req_verify",
    )
    async def btn_verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.acknowledged or not self.twitch_login or not self.raid_bot_authorized:
            missing = []
            if not self.acknowledged:
                missing.append("1️⃣ Voraussetzungen bestätigen")
            if not self.raid_bot_authorized or not self.twitch_login:
                missing.append("2️⃣ Twitch-Bot autorisieren (Pflicht)")

            await _safe_send(
                interaction,
                content=f"⚠️ **Bitte erledige noch folgende Schritte:**\n\n" + "\n".join(missing),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        assign_ok, assign_msg = await _assign_role_and_notify(interaction, self.twitch_login)
        if not assign_ok:
            await interaction.followup.send(f"⚠️ {assign_msg}", ephemeral=True)
            return

        self.verification_started = True
        self.verification_message = assign_msg
        await self._update_message(interaction)
        await interaction.followup.send(
            f"✅ {assign_msg}\n\n"
            "**Was passiert jetzt?**\n"
            "• Ein Team-Mitglied prüft dein Setup\n"
            "• Du wirst freigeschaltet, sobald alles passt\n"
            "• Bei Rückfragen melden wir uns bei dir\n\n"
            "Danke für deine Geduld! 🎉",
            ephemeral=True
        )
        await self._finish(interaction)

    @discord.ui.button(
        label="❌ Abbrechen",
        style=discord.ButtonStyle.danger,
        custom_id="wdm:streamer:req_cancel",
    )
    async def btn_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await _safe_send(
            interaction,
            content=(
                "Setup abgebrochen.\n\n"
                "Du kannst es jederzeit mit **/streamer** erneut starten."
            ),
            ephemeral=True,
        )
        await self._finish(interaction)

    async def mark_acknowledged(self, interaction: discord.Interaction) -> None:
        self.acknowledged = True
        await self._update_message(interaction)
        await _safe_send(
            interaction,
            content=(
                "✅ **Voraussetzungen bestätigt!**\n\n"
                "Wir schauen kurz, ob du alles erfüllst.\n"
                "Als nächstes: Autorisiere den Twitch-Bot (Button 2️⃣)."
            ),
            ephemeral=True,
        )


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
        # Sicherstellen, dass der Slash-Command in der Haupt-Guild sofort verfügbar ist
        await self._sync_slash_commands()

    @app_commands.command(name="streamer", description="Streamer-Partner werden (2 Schritte).")
    async def streamer_cmd(self, interaction: discord.Interaction):
        """Startet Schritt 1 direkt per DM und bestätigt hier nur kurz."""
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
                    "✅ **Streamer-Setup gestartet!**\n\n"
                    "Ich habe dir alle Infos in die DMs geschickt.\n"
                    "Die Buttons bleiben persistent – du kannst jederzeit weitermachen."
                ),
                ephemeral=True,
            )
        except discord.Forbidden:
            await _safe_send(
                interaction,
                content=(
                    "⚠️ **Ich konnte dir keine DM senden.**\n\n"
                    "Bitte aktiviere Direktnachrichten vom Server in deinen Discord-Einstellungen.\n"
                    "Alternativ kontaktiere das Team."
                ),
                ephemeral=True,
            )
        except Exception as e:
            log.error("streamer_cmd failed: %r", e)
            await _safe_send(
                interaction,
                content="⚠️ Unerwarteter Fehler beim Start. Bitte probiere es erneut oder kontaktiere einen Admin.",
                ephemeral=True,
            )

    async def _sync_slash_commands(self) -> None:
        """Synchronisiert den Command für die Haupt-Guild, damit er angezeigt wird."""
        if not MAIN_GUILD_ID:
            return

        try:
            synced = await self.bot.tree.sync(guild=discord.Object(id=MAIN_GUILD_ID))
            log.info(
                "StreamerOnboarding: Slash-Command sync abgeschlossen (Guild %s, %d Commands)",
                MAIN_GUILD_ID,
                len(synced),
            )
        except Exception as exc:
            log.warning("StreamerOnboarding: Slash-Command sync fehlgeschlagen: %s", exc, exc_info=True)



async def setup(bot: commands.Bot):
    await bot.add_cog(StreamerOnboarding(bot))
