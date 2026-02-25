# cogs/welcome_dm/dm_assistant.py
"""
KI-gestützter DM-Assistent für die Deutsche Deadlock Community.

Reagiert auf DMs mit freier KI-Antwort (Gemini/OpenAI) inkl. passendem
Discord-View basierend auf dem erkannten Intent.
"""

from __future__ import annotations

import json
import logging
import time

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

# ---------- Cooldown-Konfiguration ----------
_COOLDOWNS: dict[int, list[float]] = {}
MAX_CALLS_PER_WINDOW = 3
WINDOW_SECONDS = 60
MIN_INTERVAL_SECONDS = 10

# ---------- AI System Prompt ----------
SYSTEM_PROMPT = """\
Du bist der freundliche Bot der Deutschen Deadlock Community (Discord-Server ID 1289721245281292288).
Deine Aufgabe: Beantworte DMs von Nutzern auf Deutsch, kurz und freundlich.

=== Server-Features ===
- Streamer-Partnerschaft: Auto-Raid, Chat Guard, Analytics Dashboard (twitch.earlysalty.com), \
Discord Auto-Post (#🎥twitch), Chat-Promos alle ~30 Min. → Start mit /streamer oder DM-Flow.
- Beta-Invite: Deadlock Beta-Zugang via Ko-fi oder Invite → /betainvite im Server.
- Steam-Verifizierung: Steam-Account verknüpfen → Rolle "Steam Verifiziert" → /steamlink.
- Twitch Analytics: twitch.earlysalty.com – Retention, Unique Chatters, Leaderboard.
- LFG: Mitspieler finden → #spieler-suche Channel.
- FAQ: /faq oder /serverfaq <frage>.
- Voice: Temp-Voice-Channels, eigene Lanes.
- Ranking: Deadlock-Rang eintragen, Rollen nach Rank.

=== Antwort-Regeln ===
- Immer auf Deutsch, freundlich, max. 3–4 Sätze.
- Antworte NUR mit einem JSON-Objekt (kein Markdown, kein Codeblock darum herum).
- JSON-Format: {"intent": "...", "message": "...", "action": true/false}
- intent-Werte:
    "streamer"  → User fragt nach Streamer-Partnerschaft, Auto-Raid, Chat Guard, Analytics
    "beta"      → User braucht Deadlock Beta-Zugang
    "steam"     → User will Steam-Account verknüpfen oder Steam-Rolle erhalten
    "faq"       → FAQ-Frage oder allgemeine Server-Info
    "general"   → Alles andere (Begrüßung, Smalltalk, unklare Anfragen)
- action=true wenn ein spezieller Discord-View/Embed sinnvoll ist (bei streamer, beta, steam).
- action=false bei general/faq oder wenn kein View nötig ist.
"""


def _check_cooldown(user_id: int) -> str | None:
    """Gibt eine Fehlermeldung zurück wenn der User im Cooldown ist, sonst None."""
    now = time.monotonic()
    timestamps = _COOLDOWNS.get(user_id, [])
    timestamps = [t for t in timestamps if now - t < WINDOW_SECONDS]

    if timestamps and (now - timestamps[-1]) < MIN_INTERVAL_SECONDS:
        wait = int(MIN_INTERVAL_SECONDS - (now - timestamps[-1])) + 1
        return f"Bitte warte noch **{wait} Sekunden**, bevor du mir erneut schreibst. 😊"

    if len(timestamps) >= MAX_CALLS_PER_WINDOW:
        return (
            "Du hast mich gerade zu oft angeschrieben. "
            "Bitte warte kurz (ca. 1 Minute) und versuche es dann erneut. 😊"
        )

    timestamps.append(now)
    _COOLDOWNS[user_id] = timestamps
    return None


def _parse_ai_response(text: str) -> dict:
    """Parsed die AI-JSON-Antwort mit Fallback."""
    try:
        text = text.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            data = json.loads(text[start:end])
            return {
                "intent": str(data.get("intent", "general")).lower().strip(),
                "message": str(data.get("message", "")).strip(),
                "action": bool(data.get("action", False)),
            }
    except Exception:
        log.debug("AI-Antwort konnte nicht als JSON geparst werden: %r", text[:200])
    return {"intent": "general", "message": text.strip(), "action": False}


# ---------- Fallback-View ----------


class FallbackView(discord.ui.View):
    """Kompaktes Button-Menü wenn KI nicht verfügbar."""

    def __init__(self):
        # Wird in dm_main.setup via bot.add_view(...) persistent registriert.
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🎮 Streamer werden",
        style=discord.ButtonStyle.primary,
        custom_id="dma:fallback:streamer",
    )
    async def btn_streamer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Nutze **/streamer** im Server, um den Streamer-Partner-Prozess zu starten!\n"
            "Als Partner bekommst du: Auto-Raid, Chat Guard, Analytics und mehr.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="🎟️ Beta-Invite",
        style=discord.ButtonStyle.secondary,
        custom_id="dma:fallback:beta",
    )
    async def btn_beta(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Für einen Deadlock Beta-Invite nutze **/betainvite** im Server.\n"
            "Alternativ schau im <#1428745737323155679> Channel vorbei.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="🔗 Steam verknüpfen",
        style=discord.ButtonStyle.secondary,
        custom_id="dma:fallback:steam",
    )
    async def btn_steam(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Verknüpfe deinen Steam-Account mit **/steamlink** im Server.\n"
            'So erhältst du die Rolle **"Steam Verifiziert"**.',
            ephemeral=True,
        )

    @discord.ui.button(
        label="❓ FAQ",
        style=discord.ButtonStyle.secondary,
        custom_id="dma:fallback:faq",
    )
    async def btn_faq(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Nutze **/faq** oder **/serverfaq <frage>** im Server für häufig gestellte Fragen.\n"
            "Du kannst mir hier auch direkt deine Frage stellen!",
            ephemeral=True,
        )


# ---------- Hauptcog ----------


class BotDMAssistant(commands.Cog):
    """KI-gestützter DM-Assistent: reagiert auf freie DM-Nachrichten mit AI + passender View."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _ai(self):
        return self.bot.get_cog("AIConnector")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Nur DMs, keine Bots, nicht vom Bot selbst
        if message.author.bot:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        if message.author.id == self.bot.user.id:
            return

        # Leere Nachrichten / nur Attachments überspringen
        if not message.content.strip():
            return

        # Rate-Limit prüfen
        cooldown_msg = _check_cooldown(message.author.id)
        if cooldown_msg:
            try:
                await message.channel.send(cooldown_msg)
            except Exception:
                log.debug(
                    "BotDMAssistant: cooldown message could not be sent to user=%s",
                    message.author.id,
                    exc_info=True,
                )
            return

        ai_cog = self._ai()
        if not ai_cog:
            log.warning("BotDMAssistant: AIConnector nicht geladen – sende Fallback.")
            await self._send_fallback(message.channel)
            return

        async with message.channel.typing():
            try:
                text, meta = await ai_cog.generate_text(
                    provider="gemini",
                    prompt=message.content,
                    system_prompt=SYSTEM_PROMPT,
                    max_output_tokens=400,
                    temperature=0.7,
                )

                # Fallback zu OpenAI wenn Gemini fehlschlägt
                if text is None:
                    log.debug("BotDMAssistant: Gemini fehlgeschlagen, versuche OpenAI.")
                    text, meta = await ai_cog.generate_text(
                        provider="openai",
                        prompt=message.content,
                        system_prompt=SYSTEM_PROMPT,
                        max_output_tokens=400,
                        temperature=0.7,
                    )

                if not text:
                    log.warning(
                        "BotDMAssistant: Beide AI-Provider fehlgeschlagen (user=%s).",
                        message.author.id,
                    )
                    await self._send_fallback(message.channel)
                    return

                parsed = _parse_ai_response(text)
                intent = parsed["intent"]
                ai_message = parsed["message"]
                action = parsed["action"]

                if not ai_message:
                    await self._send_fallback(message.channel)
                    return

                await self._handle_intent(message, intent, ai_message, action)

            except Exception:
                log.exception(
                    "BotDMAssistant: Unerwarteter Fehler für user=%s.",
                    message.author.id,
                )
                await self._send_fallback(message.channel)

    async def _handle_intent(
        self,
        message: discord.Message,
        intent: str,
        ai_message: str,
        action: bool,
    ) -> None:
        channel = message.channel

        if intent == "streamer" and action:
            from .step_streamer import StreamerIntroView

            embed = StreamerIntroView.build_embed(message.author)
            view = StreamerIntroView()
            await channel.send(ai_message)
            await channel.send(embed=embed, view=view)

        elif intent == "beta" and action:
            embed = discord.Embed(
                title="🎟️ Deadlock Beta-Invite",
                description=(
                    "So bekommst du einen Beta-Invite:\n\n"
                    "**1.** Betritt unseren Discord-Server\n"
                    "**2.** Nutze `/betainvite` im richtigen Channel\n\n"
                    "Du kannst auch direkt im <#1428745737323155679> Channel nachschauen."
                ),
                color=discord.Color.blue(),
            )
            await channel.send(ai_message, embed=embed)

        elif intent == "steam" and action:
            embed = discord.Embed(
                title="🔗 Steam-Account verknüpfen",
                description=(
                    "So verknüpfst du deinen Steam-Account:\n\n"
                    "**1.** Betritt unseren Discord-Server\n"
                    "**2.** Nutze den Befehl `/steamlink`\n"
                    "**3.** Folge den Anweisungen\n"
                    "**4.** Sende dem Steam-Bot eine Freundschaftsanfrage "
                    "(Freundescode **820142646**) und nimm sie an\n\n"
                    'Nach der Verknüpfung erhältst du die Rolle **"Steam Verifiziert"**.'
                ),
                color=discord.Color.green(),
            )
            await channel.send(ai_message, embed=embed)

        else:
            # general, faq, oder action=false
            await channel.send(ai_message)

    async def _send_fallback(self, channel: discord.DMChannel) -> None:
        """Sendet das Fallback-Menü wenn KI nicht verfügbar ist."""
        embed = discord.Embed(
            title="Hallo! Ich bin der Deadlock Community Bot 👋",
            description="Womit kann ich dir helfen? Wähle eine Option:",
            color=0x5865F2,
        )
        try:
            await channel.send(embed=embed, view=FallbackView())
        except Exception:
            log.debug("Konnte Fallback-Nachricht nicht senden.", exc_info=True)
