"""Cog für den ChatGPT-gestützten Deadlock Server FAQ."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, Tuple, Optional, List

import discord
from discord import app_commands
from discord.ext import commands

from service import faq_logs

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

# --- Konfiguration ------------------------------------------------------------

# Primäres Modell via ENV überschreibbar. Wir bieten eine Fallback-Liste an.
PRIMARY_MODEL = os.getenv("DEADLOCK_FAQ_MODEL", "gpt-5.0-mini")
MODEL_CANDIDATES: List[str] = [
    PRIMARY_MODEL,
    "gpt-5.0-mini",
    "gpt-4.1-mini",
    "gpt-4o-mini",
]

DEBUG_FAQ = os.getenv("DEADLOCK_FAQ_DEBUG", "0").strip() in {"1", "true", "TRUE"}

# --- ENV-Loader ---------------------------------------------------------------

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
    except Exception as exc:  # pragma: no cover
        log.warning("python-dotenv nicht verfügbar: %s", exc)
        return

    try:
        load_dotenv(dotenv_path=str(central_env), override=False)
        log.info("Zentrale .env geladen: %s", central_env)
    except Exception:  # pragma: no cover
        log.exception("Konnte zentrale .env nicht laden: %s", central_env)


# --- Prompts ------------------------------------------------------------------

FAQ_SYSTEM_PROMPT = """
Du bist der "Deadlock Server FAQ"-Assistent und agierst ausschließlich auf Deutsch.
Deine Aufgabe:
- Beantworte ausschließlich Fragen zum Discord-Server "Deutsche Deadlock Community", seinen Kanälen, Rollen, Bots und Angeboten rund um das Spiel Deadlock.
- Allgemeine oder spiel-unabhängige Fragen musst du konsequent ablehnen: Antworte dann genau mit „Ich bin mir nicht sicher. Wende dich bitte mit dieser Frage an @earlysalty, den Server Owner.“
- Wenn dir Informationen im Kontext fehlen, antworte ebenfalls genau so (kein Raten, nichts erfinden).
- Benenne Bots/Kanäle/Rollen exakt wie in der Doku. Erwähne DMs mit dem Deadlock Master Bot, wo relevant.
- Verweise bei Feedback auf das anonyme Formular im Feedback Hub.
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
- hier-starten-regelwerk, ankündigungen, patchnotes, live-on-twitch, server-faq.

Kategorie "Start":
- allgemein, build-discussion, off-topic, clip-submission, deadlock-coaching (bald: request-a-coaching), leaks,
  game-guides-und-tipps, yt-videos, beta-zugang, coaching-lane.

Kategorie "Custom Game":
- custom-game-umsetzung, custom-games-ideen, Sammelpunkt.

Kategorie "Entspannte Lanes":
- temp-voice (Panel zur Verwaltung), rank-auswahl, spieler-suche, Spaß Lane.

Kategorie "Grind Lanes":
- Grind Lane.

Kategorie "Ranked Lanes":
- low-elo-ranked, mid-elo-ranked, high-elo-ranked, High Elo Podium, Rank Lane.

Kategorie "AFK":
- AFK, No Deadlock Voice.

Weiteres:
- Unterschied Spaß/Grind/Ranked: Spaß=Casual; Grind=fokussiert; Ranked=Rating.
- TempVoice: Owner Claim, Kick/Ban, User-Limit, Regionenfilter, Mindest-Rang usw.
- Clip Submission: Cooldown, sammelt pro Woche.
- Coaching: private Threads führen durch Rang/Subrang/Heldenauswahl; informiert Coaching-Team.
- Feedback Hub: anonymes Feedback ans Team.
- Voice Leaderboard & Stats: !vstats / !vleaderboard.
- Team Balancer: !balance.
- Twitch Statistik-Proxy: !twl.
- Steam-Verknüpfung via /link oder /link_steam; Verified-Rolle nach Prüfung automatisch.
""".strip()


# --- UI -----------------------------------------------------------------------

class FAQModal(discord.ui.Modal):
    """Modal, um Fragen an das Server FAQ zu stellen."""

    question_input: discord.ui.TextInput

    def __init__(
        self,
        faq_cog: "ServerFAQ",
        *,
        title: str = "Server FAQ",
        default_question: Optional[str] = None,
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
    ) -> None:  # pragma: no cover
        if interaction.response.is_done():
            await interaction.followup.send(
                "Bitte nutze /faq, um eine neue Frage zu stellen.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(FAQModal(self.faq_cog))


# --- Cog ----------------------------------------------------------------------

class ServerFAQ(commands.Cog):
    """Deadlock-spezifischer FAQ-Bot, der auf GPT-Antworten zurückgreift."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._client: Optional[OpenAI] = None

        if OpenAI is None:
            log.warning("OpenAI-Paket nicht installiert – Server FAQ nutzt Fallback.")
            return

        _ensure_central_env_loaded()
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("DEADLOCK_OPENAI_KEY")
        if not api_key:
            log.warning("Kein OpenAI API Key (OPENAI_API_KEY/DEADLOCK_OPENAI_KEY) gesetzt – Fallback aktiv.")
            return

        try:
            self._client = OpenAI(api_key=api_key)
        except Exception:
            log.exception("Konnte OpenAI-Client nicht initialisieren")
            self._client = None

    # ---- Low-level Call mit Token-Param-Kompatibilität ----------------------

    def _responses_create(self, **kwargs):
        """
        Ruft responses.create auf und ist kompatibel zu SDKs mit
        max_output_tokens ODER max_tokens.
        """
        if self._client is None:
            raise RuntimeError("OpenAI client not initialized")

        # Erster Versuch: moderne Param-Namen
        try:
            return self._client.responses.create(
                **kwargs,
                max_output_tokens=800,
            )
        except TypeError:
            # Fallback für ältere SDKs
            return self._client.responses.create(
                **kwargs,
                max_tokens=800,
            )

    # ---- Antwort erzeugen ----------------------------------------------------

    async def _generate_answer(
        self,
        *,
        question: str,
        user: discord.abc.User | discord.Member | None,
        channel: discord.abc.GuildChannel | discord.Thread | None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Fragt das Sprachmodell an und gibt Antwort + Metadaten zurück."""

        metadata: Dict[str, Any] = {
            "model": PRIMARY_MODEL,
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
            metadata["error"] = "no_client"
            return fallback, metadata

        composed_user_prompt = f"Kontext:\n{FAQ_CONTEXT}\n\nFrage:\n{question.strip()}"

        # Wir versuchen Modelle der Reihe nach, bis eins funktioniert.
        last_exc: Optional[Exception] = None
        response = None
        used_model = None

        for model_name in MODEL_CANDIDATES:
            try:
                used_model = model_name
                response = await asyncio.to_thread(
                    self._responses_create,
                    model=model_name,
                    temperature=0.2,
                    input=composed_user_prompt,
                    instructions=FAQ_SYSTEM_PROMPT,
                )
                break
            except Exception as exc:  # pragma: no cover
                last_exc = exc
                log.warning("Model '%s' fehlgeschlagen (%s). Versuche nächstes Modell.", model_name, exc)

        if response is None:
            fallback = (
                "Ich bin mir nicht sicher. Wende dich bitte mit dieser Frage an @earlysalty, den Server Owner."
            )
            metadata["error"] = repr(last_exc) if last_exc else "unknown_error"
            return fallback, metadata

        # ---- Content-Extraction robust ----
        content = ""
        try:
            if getattr(response, "output_text", None):
                content = response.output_text.strip()
            else:
                out = getattr(response, "output", None) or getattr(response, "outputs", None)
                if out:
                    fragments: List[str] = []
                    for item in out:
                        if getattr(item, "type", None) != "message":
                            continue
                        for part in getattr(item, "content", []) or []:
                            txt = getattr(part, "text", None)
                            if txt:
                                fragments.append(txt)
                    content = "".join(fragments).strip()
        except Exception:
            log.exception("Antwort-Parsing fehlgeschlagen")
            content = ""

        if not content:
            content = (
                "Ich bin mir nicht sicher. Wende dich bitte mit dieser Frage an @earlysalty, den Server Owner."
            )

        usage = getattr(response, "usage", None)
        if usage is not None:
            metadata["usage"] = {
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }

        metadata["model"] = used_model or getattr(response, "model", PRIMARY_MODEL)
        return content, metadata

    # ---- Handling der Interaktion -------------------------------------------

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

        # Persist Logs
        faq_logs.store_exchange(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            question=question,
            answer=answer,
            model=metadata.get("model"),
            metadata=metadata,
        )

        # Antwort bauen
        cleaned_answer = (answer or "").strip()
        if not cleaned_answer:
            cleaned_answer = "Ich bin mir nicht sicher. Wende dich bitte mit dieser Frage an @earlysalty, den Server Owner."

        if len(cleaned_answer) <= 4096:
            embed = discord.Embed(
                title="Server FAQ",
                description=cleaned_answer,
                colour=discord.Colour.blurple(),
            )
            footer = "Deadlock Master Bot • FAQ-Antwort"
            if DEBUG_FAQ and metadata:
                err = metadata.get("error")
                used_model = metadata.get("model")
                if err:
                    footer += f" • DEBUG: error={str(err)[:60]} • model={used_model}"
                else:
                    footer += f" • model={used_model}"
            embed.set_footer(text=footer)
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(cleaned_answer, ephemeral=True)

    # ---- Commands ------------------------------------------------------------

    @app_commands.command(
        name="faq",
        description="Öffnet das Server FAQ und beantwortet Server-bezogene Fragen.",
    )
    @app_commands.guild_only()
    async def faq(self, interaction: discord.Interaction) -> None:
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
        await self.handle_interaction_question(
            interaction=interaction,
            question=frage,
            defer=True,
        )

    @commands.command(name="faq")
    async def faq_prefix(self, ctx: commands.Context) -> None:
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
