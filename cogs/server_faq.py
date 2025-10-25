"""Cog für den ChatGPT-gestützten Deadlock Server FAQ."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from service import faq_logs

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = os.getenv("DEADLOCK_FAQ_MODEL", "gpt-5.0-turbo")


def _ensure_central_env_loaded() -> None:
    """Lädt den zentralen .env-Pfad, falls der API-Key noch fehlt."""

    if os.getenv("OPENAI_API_KEY") or os.getenv("DEADLOCK_OPENAI_KEY"):
        return

    central_env = Path(os.path.expandvars(r"C:\Users\Nani-Admin\Documents\.env"))
    if not central_env.is_file():
        log.debug("Zentrale .env nicht gefunden: %s", central_env)
        return

    try:
        from dotenv import load_dotenv
    except Exception as exc:  # pragma: no cover - optionale Abhängigkeit
        log.warning("python-dotenv nicht verfügbar: %s", exc)
        return

    try:
        load_dotenv(dotenv_path=str(central_env), override=False)
        log.info("Zentrale .env geladen: %s", central_env)
    except Exception:  # pragma: no cover - Dateifehler
        log.exception("Konnte zentrale .env nicht laden: %s", central_env)


FAQ_SYSTEM_PROMPT = """
Du bist der "Deadlock Server FAQ"-Assistent und agierst ausschließlich auf Deutsch.
Deine Aufgabe:
- Beantworte ausschließlich Fragen zum Discord-Server "Deutsche Deadlock Community", seinen Kanälen, Rollen, Bots und Angeboten rund um das Spiel Deadlock.
- Allgemeine Fragen (z. B. zu Wetter, Weltwissen, Smalltalk) musst du konsequent ablehnen. Sage in diesen Fällen klar, dass du nur Server-bezogene Fragen beantworten darfst.
- Falls eine Frage Informationen erfordert, die nicht im Kontext enthalten sind oder die nicht serverbezogen beantwortet werden können, antworte wörtlich: "Ich bin mir nicht sicher. Wende dich bitte mit dieser Frage an @earlysalty, den Server Owner."
- Mache keine Annahmen außerhalb der bereitgestellten Fakten. Erfinde keine neuen Features oder Regeln.
- Nenne Bots, Kanäle oder Rollen so, wie sie in der Dokumentation genannt werden. Sei präzise und hilfreich.
- Erwähne in deiner Antwort, wenn relevante Aktionen über DMs mit dem Deadlock Master Bot stattfinden.
- Wenn jemand Feedback geben möchte, verweise auf das anonyme Feedback-Formular im Feedback Hub.
""".strip()

FAQ_CONTEXT = """
Servername: "Deutsche Deadlock Community" – eine Community für das Spiel Deadlock.
Hilfreiches:
Für Statstiken +ber deadlock und dem eigenen Playstyle gibts https://statlocker.gg/ https://www.lockblaze.com/ für Statstiken für Items und co https://deadlock-api.com/

Bots & Kontakte:
- Deadlock Master Bot: offizieller Bot der Community. Für Streamer-Partnerschaften bitte dem Bot eine DM schicken und den Slash-Befehl /streamer ausführen, um das Setup zu starten.
- Server Owner: @earlysalty. Bei fehlenden Informationen oder Spezialfällen an ihn wenden.

Rollen & Zugänge:
- Verified: wird nach erfolgreicher Steam-Verknüpfung automatisch vergeben.
- VIP: Erhalte die Rolle nach langer, aktiver Teilnahme am Server und wenn du den Twitch-Kanal https://www.twitch.tv/earlysalty abonnierst.

Kategorie "Streamer Only":
- stream-updates, streamer-austausch, Streamer VC.

Kategorie "Streamer Partner":
- vip-lounge, vip VC (Voice Channel).

Kategorie "Spawn":
- hier-starten-regelwerk: enthält das Regelwerk.
- ankündigungen: offizielle Server-Ankündigungen.
- patchnotes: übersetzte Deadlock-Patchnotes auf Deutsch.
- live-on-twitch: listet Deadlock-Streamer-Partner, die gerade live sind.
- server-faq: Platz für das FAQ-Angebot.

Kategorie "Start":
- allgemein: allgemeiner Austausch zum Spiel und zur Community.
- build-discussion: Diskussionen über Builds für Deadlock-Helden.
- off-topic: Themen außerhalb von Deadlock.
- clip-submission: Anleitung und Einreichung von Clips (Details stehen in der README bzw. Channelbeschreibung).
- deadlock-coaching: wird zukünftig zu "request-a-coaching" umgebaut, aktuell kostenlos.
- leaks: Gerüchte oder frühe Informationen rund um Deadlock.
- game-guides-und-tipps: praktische Videos, Tipps & Tricks rund um Deadlock.
- yt-videos: unterhaltsame Deadlock-Videos ohne Lernfokus.
- beta-zugang: für Anfragen zum Deadlock-Beta-Zugang (Spiel ist Invite-Only).
- coaching-lane: Voice-Bereich, um in Ruhe zu zweit an Verbesserungen zu arbeiten.

Kategorie "Custom Game":
- custom-game-umsetzung: Organisation eigener Deadlock-Matches, z. B. 6v6-Competitive, Melee-only oder Hide & Seek.
- custom-games-ideen: Ideensammlung für zukünftige Custom Games, eigene Wünsche ausdrücklich willkommen.
- Sammelpunkt: Treffpunkt für Custom-Games-Teilnehmer.

Kategorie "Entspannte Lanes":
- temp-voice: Verwaltung eigener Voice-Lanes über TempVoice (Panel erklärt Einstellungen und Verwaltung).
- rank-auswahl: eigene Rang-Rolle wählen.
- spieler-suche: Sucht Mitspieler (LFG) für Deadlock.
- Spaß Lane: Voice-Kanal für lockeres Spielen.

Kategorie "Grind Lanes":
- Grind Lane: Voice-Kanal für konzentriertes Spielen.

Kategorie "Ranked Lanes":
- low-elo-ranked, mid-elo-ranked, high-elo-ranked: Text-Kanäle für koordinierte Spiele innerhalb derselben Elo.
- High Elo Podium: Rückzugsort für High-Elo-Spieler, inkl. Streaming-Möglichkeit ohne Störung.
- Rank Lane: Voice-Kanal für Rang-Spiele.

Kategorie "AFK":
- AFK: automatischer Voice-Kanal für abwesende Nutzer.
- No Deadlock Voice: Voice-Kanal für Offtopic-Unterhaltungen.

Weiteres:
- Unterschied Spaß / Grind / Ranked: Spaß = Casual ohne Ranglimit; Grind = fokussiertes Spielen mit Gewinnabsicht; Ranked = explizites Ranglisten-Spiel mit Spielern ähnlicher Wertung.
- TempVoice-Lanes können über das Panel konfiguriert werden (Owner Claim, Kick/Ban, User-Limit, Regionenfilter, Mindest-Rang usw.).
- Clip Submission hat Cooldown und sammelt Einreichungen pro Woche.
- Deadlock Coaching: private Threads führen durch Rang-, Subrang- und Heldenauswahl; informiert Coaching-Team.
- Feedback Hub: erlaubt anonymes Feedback an das Community-Team.
- Voice Leaderboard & Stats: Befehle !vstats und !vleaderboard zeigen Voice-Aktivität.
- Deadlock Team Balancer: !balance Befehle helfen faire Teams zu erstellen.
- Twitch Statistik-Proxy: !twl bietet Leaderboards im Statistik-Channel.
- Steam-Verknüpfung via /link oder /link_steam; erinnert bei Voice ohne Link. Verified-Rolle wird automatisch nach Prüfung vergeben.
""".strip()


class FAQModal(discord.ui.Modal):
    """Modal, um Fragen an das Server FAQ zu stellen."""

    question_input: discord.ui.TextInput

    def __init__(
        self,
        faq_cog: "ServerFAQ",
        *,
        title: str = "Server FAQ",
        default_question: str | None = None,
    ) -> None:
        super().__init__(title=title, timeout=None)
        self.faq_cog = faq_cog
        self.question_input = discord.ui.TextInput(
            label="Welche Frage hast du zum Server?",
            placeholder="Beschreibe dein Anliegen möglichst konkret.",
            style=discord.TextStyle.long,
            required=True,
            max_length=400,
            default=default_question or "",
        )
        self.add_item(self.question_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.faq_cog.handle_interaction_question(
            interaction=interaction,
            question=self.question_input.value,
            defer=True,
        )


class FAQAskView(discord.ui.View):
    """View mit Button, um das FAQ-Modal aufzurufen."""

    def __init__(self, faq_cog: "ServerFAQ") -> None:
        super().__init__(timeout=120)
        self.faq_cog = faq_cog

    @discord.ui.button(
        label="Frage stellen",
        style=discord.ButtonStyle.primary,
        emoji="❓",
    )
    async def ask_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button["FAQAskView"],
    ) -> None:  # pragma: no cover - rein UI-basiert
        if interaction.response.is_done():
            await interaction.followup.send(
                "Bitte nutze /faq, um eine neue Frage zu stellen.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(FAQModal(self.faq_cog))


class ServerFAQ(commands.Cog):
    """Deadlock-spezifischer FAQ-Bot, der auf GPT-Antworten zurückgreift."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._client = None
        if OpenAI is None:
            log.warning("OpenAI-Paket nicht installiert – Server FAQ reagiert mit Fallback.")
        else:
            _ensure_central_env_loaded()
            api_key = os.getenv("OPENAI_API_KEY") or os.getenv("DEADLOCK_OPENAI_KEY")
            if not api_key:
                log.warning("Kein OpenAI API Key für den Server FAQ gesetzt.")
            else:
                try:
                    self._client = OpenAI(api_key=api_key)
                except Exception:
                    log.exception("Konnte OpenAI-Client nicht initialisieren")
                    self._client = None

    async def _generate_answer(
        self,
        *,
        question: str,
        user: discord.abc.User | discord.Member | None,
        channel: discord.abc.GuildChannel | discord.Thread | None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Fragt das Sprachmodell an und gibt Antwort + Metadaten zurück."""

        metadata: Dict[str, Any] = {
            "model": DEFAULT_MODEL_NAME,
            "user_id": getattr(user, "id", None),
            "channel_id": getattr(channel, "id", None),
            "guild_id": getattr(getattr(channel, "guild", None), "id", None)
            if channel is not None
            else getattr(getattr(user, "guild", None), "id", None),
        }

        if self._client is None:
            fallback = (
                "Der FAQ-Bot steht aktuell nicht zur Verfügung. "
                "Ich bin mir nicht sicher. Wende dich bitte mit dieser Frage an @earlysalty, den Server Owner."
            )
            return fallback, metadata

        composed_user_prompt = f"Kontext:\n{FAQ_CONTEXT}\n\nFrage:\n{question.strip()}"

        try:
            response = await asyncio.to_thread(
                self._client.chat.completions.create,
                model=DEFAULT_MODEL_NAME,
                temperature=0.2,
                max_tokens=800,
                messages=[
                    {"role": "system", "content": FAQ_SYSTEM_PROMPT},
                    {"role": "user", "content": composed_user_prompt},
                ],
            )
        except Exception as exc:  # pragma: no cover - Netzwerk/HTTP-Fehler
            log.exception("OpenAI-Antwort fehlgeschlagen: %s", exc)
            fallback = (
                "Ich bin mir nicht sicher. Wende dich bitte mit dieser Frage an @earlysalty, den Server Owner."
            )
            metadata["error"] = str(exc)
            return fallback, metadata

        content = (response.choices[0].message.content or "").strip()
        if not content:
            content = (
                "Ich bin mir nicht sicher. Wende dich bitte mit dieser Frage an @earlysalty, den Server Owner."
            )

        usage = getattr(response, "usage", None)
        if usage is not None:
            metadata["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        metadata["model"] = getattr(response, "model", DEFAULT_MODEL_NAME)

        return content, metadata

    async def handle_interaction_question(
        self,
        *,
        interaction: discord.Interaction,
        question: str,
        defer: bool,
    ) -> None:
        if defer:
            await interaction.response.defer(ephemeral=True, thinking=True)

        answer, metadata = await self._generate_answer(
            question=question,
            user=interaction.user,
            channel=interaction.channel,
        )

        guild_id = interaction.guild_id
        channel_id = interaction.channel_id
        user_id = interaction.user.id if interaction.user else None

        if "feedback" in question.lower() and "feedback hub" not in answer.lower():
            answer = (
                f"{answer}\n\nFür anonymes Feedback nutzt du im Feedback Hub den Button "
                "„Anonymes Feedback senden“."
            )

        faq_logs.store_exchange(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            question=question,
            answer=answer,
            model=metadata.get("model"),
            metadata=metadata,
        )

        if answer:
            cleaned_answer = answer.strip()
        else:
            cleaned_answer = "Ich bin mir nicht sicher. Wende dich bitte mit dieser Frage an @earlysalty, den Server Owner."

        if len(cleaned_answer) <= 4096:
            embed = discord.Embed(
                title="Server FAQ",
                description=cleaned_answer,
                colour=discord.Colour.blurple(),
            )
            embed.set_footer(text="Deadlock Master Bot • FAQ-Antwort")
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(cleaned_answer, ephemeral=True)


    @app_commands.command(
        name="faq",
        description="Öffnet das Server FAQ und beantwortet Server-bezogene Fragen.",
    )
    @app_commands.guild_only()
    async def faq(self, interaction: discord.Interaction) -> None:
        """Slash-Command, der ein Modal zur Fragestellung öffnet."""

        if interaction.response.is_done():
            await interaction.followup.send(
                "Du kannst nur eine Anfrage gleichzeitig stellen.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(FAQModal(self))

    @app_commands.command(
        name="serverfaq",
        description="Stelle dem Deadlock Server FAQ eine Frage zum Discord-Server.",
    )
    @app_commands.describe(
        frage="Formuliere deine Frage zum Server, seinen Rollen, Kanälen oder Bots.",
    )
    @app_commands.guild_only()
    async def serverfaq(self, interaction: discord.Interaction, frage: str) -> None:
        """Slash-Command für serverbezogene Fragen (Legacy-Variante)."""

        await self.handle_interaction_question(
            interaction=interaction,
            question=frage,
            defer=True,
        )

    @commands.command(name="faq")
    async def faq_prefix(self, ctx: commands.Context) -> None:
        """Prefix-Befehl, der auf den Slash-Command verweist und eine UI anbietet."""

        if ctx.guild is None:
            await ctx.reply("Bitte nutze diesen Befehl auf dem Server.")
            return

        view = FAQAskView(self)
        description = (
            "Nutze die Schaltfläche, um das Deadlock Server FAQ zu öffnen. "
            "Alternativ steht dir jederzeit der Slash-Befehl /faq zur Verfügung."
        )

        embed = discord.Embed(
            title="Deadlock Server FAQ",
            description=description,
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text="Deadlock Master Bot • FAQ")

        await ctx.reply(embed=embed, view=view, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    faq_cog = ServerFAQ(bot)
    await bot.add_cog(faq_cog)

    try:
        bot.tree.add_command(faq_cog.serverfaq)
    except app_commands.CommandAlreadyRegistered:
        bot.tree.remove_command(
            faq_cog.serverfaq.name,
            type=discord.AppCommandType.chat_input,
        )
        bot.tree.add_command(faq_cog.serverfaq)

    try:
        bot.tree.add_command(faq_cog.faq)
    except app_commands.CommandAlreadyRegistered:
        bot.tree.remove_command(
            faq_cog.faq.name,
            type=discord.AppCommandType.chat_input,
        )
        bot.tree.add_command(faq_cog.faq)
