"""Cog für den ChatGPT-gestützten Deadlock Server FAQ."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from service import changelogs, faq_logs

log = logging.getLogger(__name__)

# --- Konfiguration ------------------------------------------------------------

# Fixiertes Primärmodell (keine ENV-Überschreibung, keine Fallbacks).
PRIMARY_MODEL = "gpt-5"

DEFAULT_MAX_OUTPUT_TOKENS = 2000
_max_tokens_env = os.getenv("DEADLOCK_FAQ_MAX_OUTPUT_TOKENS")
if _max_tokens_env:
    try:
        MAX_OUTPUT_TOKENS = max(1, int(_max_tokens_env))
    except ValueError:
        log.warning(
            "Ungültiger Wert für DEADLOCK_FAQ_MAX_OUTPUT_TOKENS: %r – verwende %d.",
            _max_tokens_env,
            DEFAULT_MAX_OUTPUT_TOKENS,
        )
        MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS
else:
    MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS

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
Du bist der „Deadlock Server FAQ“-Assistent und antwortest ausschließlich auf Deutsch.

Dein Auftrag (Priorität in dieser Reihenfolge):
1) Beantworte Fragen zum Discord-Server (Kanäle, Rollen, Bots, Prozesse).
2) Beantworte Deadlock-Gameplay-/Verbesserungsfragen, aber IMMER mit Serverbezug: Verweise konkret auf passende Server-Ressourcen (Kanäle, Rollen, Bots, Tools).
3) Wenn etwas NICHT in deinen Kontext passt (kein Serverbezug möglich UND keine Gameplay-Hilfen aus dem Kontext ableitbar), lehne freundlich ab und verweise auf @earlysalty.

Richtlinien:
- Keine Dinge erfinden. Nutze die Begriffe/Kanäle/Rollen wie im Kontext genannt.
- Erwähne DM-Flows mit dem Deadlock Master Bot, wenn relevant.
- Für Feedback: verweise auf das anonyme Feedback-Formular im Feedback Hub.
- Antworte präzise und hilfreich.
""".strip()


FAQ_CONTEXT = """
Servername: "Deutsche Deadlock Community" – eine Community für das Spiel Deadlock.
Hilfreiches:
Für Statstiken +ber deadlock und dem eigenen Playstyle gibts https://statlocker.gg/ https://www.lockblaze.com/ für Statstiken für Items und co https://deadlock-api.com/ das sind so advanced Deadlock tracker with performance rank estimates and detailed stats analysis

Bots & Kontakte:
- Deadlock Master Bot: offizieller Bot der Community. Für Streamer-Partnerschaften bitte dem Bot eine DM schicken und den Slash-Befehl /streamer ausführen, um das Setup zu starten.
- Server Owner: @earlysalty. Bei fehlenden Informationen oder Spezialfällen an ihn wenden.

Funktionsweise des Temp Voice Bots:
Es gibt die Sprachkanäle Lane erstellen mit dem + da joint man rein dann wird ein Voice Kanal erstellt und die Person durch den Bot rein gemoved.
Über das Interface lassen sich folgende Dinge ändern. DE / EU ist entweder nur Deutsch Sprachige können in den Channel, und eu ist das auch (die Paar wenige Englische) joinen können.
Owner Claim falls der Owner nicht mehr in der Lane ist, kann man damit die Eigentumsrechte übernehmen. Limit setzen ist das Limit an Personen 0-99.
Kick (kann nur mit Owner Rechten gemacht werden) kickt eine Person aus dem Kanal, Ban bannt eine Person permanent aus deinem Voice Channel. Wichtig die Banns sind preresistent bedeutet auch nach dem Verlassen der Lane bleibt diese Einstellung gespeichert, und wird bei einem Späteren erstellen einer Lane re Applyed.
Und Unban entbannt die Person. Mindest Rang setzen geht nur in den Grind Lanes und erklärt sich von selbst. Jedoch sind bei den Grind lanes kleine Rang Caps gesetzt damit es nicht einen riesigen Skill Gap gibt.

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
- request-a-coaching wird aktuell umgebaut das Sowohl Matches als auch 1:1 Live Coachings angeboten werden, aktuell kostenlos.
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
- Street Brawl Lane: Voice-Kanal ohne Rang-Limit (max. 4 Slots).

Kategorie "AFK":
- AFK: automatischer Voice-Kanal für abwesende Nutzer.
- No Deadlock Voice: Voice-Kanal für Offtopic-Unterhaltungen.

Weiteres:
- Unterschied Spaß / Grind / Ranked: Spaß = Casual ohne Ranglimit; Grind = fokussiertes Spielen mit Gewinnabsicht; Ranked = explizites Ranglisten-Spiel mit Spielern ähnlicher Wertung.
- Street Brawl: 4er Lane ohne Rang-Cap, erstellt über den Street-Brawl-(+) Channel.
- TempVoice-Lanes können über das Panel konfiguriert werden (Owner Claim, Kick/Ban, User-Limit, Regionenfilter, Mindest-Rang usw.).
- Clip Submission hat Cooldown und sammelt Einreichungen pro Woche.
- Deadlock Coaching: private Threads führen durch Rang-, Subrang- und Heldenauswahl; informiert Coaching-Team.
- Feedback Hub: erlaubt anonymes Feedback an das Community-Team.
- Voice Leaderboard & Stats: Befehle !vstats und !vleaderboard zeigen Voice-Aktivität.
- Deadlock Team Balancer: !balance Befehle helfen faire Teams zu erstellen.
- Twitch Statistik-Proxy: !twl bietet Leaderboards im Statistik-Channel.
- Steam-Verknüpfung via /account_verknüpfen; erinnert bei Voice ohne Link. Verified-Rolle wird automatisch nach Prüfung vergeben.
- Für ein Lane 1:1 ist @cuzyoul immer zu Haben dadurch kansnt du dich einfach duelieren und verbessern.
- Für ein Coaching oder gernerelle Hilfe zum Server zu Deadlock ist @earlysalty immer zu Haben
- Warum habe ich die Meldung 2FA Requirement for Moderation?`--> Dann hast du einer der Rollen wie Streamer,VIP (haben Rechte um Mitgleieder in Voice Calls zu Verschieben), Community Moderator Server Dev Moderator haben alle teilweise bis viele Moderations rechte.
Zufällige Fragen:
Gehört der Deadlock Master Bot zu diesem Server? Ja er gehört zu dem Server und übernimmt die Wichtigsten aufgaben des Servers, bitte schalte ihn nicht Stumm sonst sind einige Funktionen nicht verfügbar.
Warum bekomme ich eine DM von dem Bot nach Server join? Das ist gewollt und dient dazu das Servererlebniss zu verbessern und dir den einstieg in den Server zu ermöglichen. 
Was ist dieses Kleiner Tipp für besseres Voice-Erlebnis vom Bot da warum bekomme ich das? Info nur für dich, die User sollen ihren Steam Account verknüpfen dafür gibt es mehrere Optionen, dies Dient dazu das wir 1. für Organistaorische Zwecke das Steam Profil mit dem Discord Profil haben, 2. für Statusanzeigen auf den Voice Kanälen, ob die Lane sich in einem Match in der Lobby befindet und ggf Minute. 
""".strip()


# --- UI -----------------------------------------------------------------------


class FAQModal(discord.ui.Modal):
    """Modal, um Fragen an das Server FAQ zu stellen."""

    question_input: discord.ui.TextInput

    def __init__(
        self,
        faq_cog: ServerFAQ,
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

    def __init__(self, faq_cog: ServerFAQ) -> None:
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
        button: discord.ui.Button[FAQAskView],
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

    # ---- Antwort erzeugen ----------------------------------------------------

    async def _generate_answer(
        self,
        *,
        question: str,
        user: discord.abc.User | discord.Member | None,
        channel: discord.abc.GuildChannel | discord.Thread | None,
    ) -> tuple[str, dict[str, Any]]:
        """Fragt das Sprachmodell an und gibt Antwort + Metadaten zurück."""

        metadata: dict[str, Any] = {
            "user_id": getattr(user, "id", None),
            "channel_id": getattr(channel, "id", None),
            "guild_id": getattr(getattr(channel, "guild", None), "id", None)
            if channel is not None
            else getattr(getattr(user, "guild", None), "id", None),
        }

        patchnote_context = changelogs.get_context_for_question(question)

        context_parts = [FAQ_CONTEXT]
        if patchnote_context:
            context_parts.append(f"Patchnotes:\n{patchnote_context}")

        composed_user_prompt = (
            "Kontext:\n" + "\n\n".join(context_parts) + f"\n\nFrage:\n{question.strip()}"
        )

        ai = getattr(self.bot, "get_cog", lambda name: None)("AIConnector")
        if not ai:
            fallback = (
                "Der FAQ-Bot steht aktuell nicht zur Verfügung. "
                "Ich bin mir nicht sicher. Wende dich bitte mit dieser Frage an @earlysalty, den Server Owner."
            )
            metadata["error"] = "no_ai_connector"
            return fallback, metadata

        answer_text, meta_resp = await ai.generate_text(
            provider="openai",
            prompt=composed_user_prompt,
            system_prompt=FAQ_SYSTEM_PROMPT,
            model=PRIMARY_MODEL,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=0.3,
        )
        metadata.update(meta_resp)

        if not answer_text:
            fallback = "Ich bin mir nicht sicher. Wende dich bitte mit dieser Frage an @earlysalty, den Server Owner."
            metadata.setdefault("error", "no_response")
            return fallback, metadata

        return answer_text, metadata

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
