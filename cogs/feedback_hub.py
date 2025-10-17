from __future__ import annotations

import logging
from typing import Iterable

import discord
from discord.ext import commands

from service import db

log = logging.getLogger(__name__)

FEEDBACK_CHANNEL_ID = 1289721245281292291
FEEDBACK_RECIPIENT_ID = 662995601738170389


def _trim(value: str | None, max_length: int = 1024) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > max_length:
        return value[: max_length - 1] + "…"
    return value


class FeedbackHubModal(discord.ui.Modal):
    def __init__(self, cog: "FeedbackHub", *, source_message_id: int | None) -> None:
        super().__init__(title="Deadlock Feedback")
        self.cog = cog
        self.source_message_id = source_message_id

        self.experience = discord.ui.TextInput(
            label="Wie war dein bisheriges Spielerlebnis?",
            placeholder="Beschreibe dein Erlebnis so präzise wie möglich.",
            style=discord.TextStyle.paragraph,
            max_length=1024,
            required=True,
        )
        self.server_usage = discord.ui.TextInput(
            label="Wie gut kommst du mit dem Server zurecht?",
            placeholder="Gibt es Bots, Kanäle oder Möglichkeiten die dir helfen oder fehlen?",
            style=discord.TextStyle.paragraph,
            max_length=1024,
            required=False,
        )
        self.improvements = discord.ui.TextInput(
            label="Wie können wir den Server verbessern?",
            placeholder="Jeder Wunsch ist willkommen – egal ob umsetzbar oder nicht.",
            style=discord.TextStyle.paragraph,
            max_length=1024,
            required=True,
        )
        self.wish = discord.ui.TextInput(
            label="Beschreibe deinen Wunsch möglichst genau.",
            style=discord.TextStyle.paragraph,
            max_length=1024,
            required=False,
        )
        self.additional = discord.ui.TextInput(
            label="Möchtest du noch etwas mitteilen?",
            style=discord.TextStyle.paragraph,
            max_length=1024,
            required=False,
        )

        for component in (
            self.experience,
            self.server_usage,
            self.improvements,
            self.wish,
            self.additional,
        ):
            self.add_item(component)

    async def on_submit(self, interaction: discord.Interaction) -> None:  # noqa: D401
        answers = (
            _trim(self.experience.value),
            _trim(self.server_usage.value),
            _trim(self.improvements.value),
            _trim(self.wish.value),
            _trim(self.additional.value),
        )

        reference_id = int(interaction.id)
        channel_id = interaction.channel_id or FEEDBACK_CHANNEL_ID

        await self.cog.notify_feedback(
            reference_id,
            interaction.guild_id,
            channel_id,
            self.source_message_id,
            answers,
        )

        try:
            await interaction.response.send_message(
                "Vielen Dank für dein Feedback! Es wurde anonym weitergeleitet.",
                ephemeral=True,
            )
        except discord.HTTPException:
            log.warning("Antwort auf Feedback-Modal konnte nicht gesendet werden", exc_info=True)


class FeedbackHubView(discord.ui.View):
    def __init__(self, cog: "FeedbackHub") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Anonymes Feedback senden",
        style=discord.ButtonStyle.primary,
        custom_id="feedback_hub:open_modal",
    )
    async def open_modal(  # type: ignore[override]
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        modal = FeedbackHubModal(self.cog, source_message_id=getattr(interaction.message, "id", None))
        await interaction.response.send_modal(modal)


class FeedbackHub(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.view = FeedbackHubView(self)
        bot.add_view(self.view)

    async def notify_feedback(
        self,
        reference_id: int,
        guild_id: int | None,
        channel_id: int,
        message_id: int | None,
        answers: Iterable[str | None],
    ) -> None:
        recipient = self.bot.get_user(FEEDBACK_RECIPIENT_ID)
        if recipient is None:
            try:
                recipient = await self.bot.fetch_user(FEEDBACK_RECIPIENT_ID)
            except discord.HTTPException as exc:
                log.warning("Feedback Empfänger konnte nicht geladen werden: %s", exc)
                return

        embed = discord.Embed(
            title="Neues anonymes Feedback",
            colour=discord.Colour.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Feedback #{reference_id}")

        channel_reference = f"<#{channel_id}>"
        source_lines = [f"Kanal: {channel_reference}"]
        if guild_id and message_id:
            interface_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
            source_lines.append(f"[Interface öffnen]({interface_url})")

        embed.add_field(
            name="Quelle",
            value="\n".join(source_lines),
            inline=False,
        )

        labels = (
            "Spielerlebnis",
            "Server & Möglichkeiten",
            "Verbesserungsvorschläge",
            "Detaillierter Wunsch",
            "Weitere Mitteilungen",
        )

        for label, answer in zip(labels, answers):
            embed.add_field(name=label, value=answer or "—", inline=False)

        try:
            await recipient.send(embed=embed)
        except discord.Forbidden:
            log.warning("Feedback konnte nicht per DM zugestellt werden (Forbidden)")
        except discord.HTTPException as exc:
            log.error("Versand des Feedbacks fehlgeschlagen: %s", exc)

    @commands.command(name="fhub")
    @commands.has_permissions(manage_guild=True)
    async def create_feedback_interface(self, ctx: commands.Context) -> None:
        channel: discord.abc.MessageableChannel | None = None
        if ctx.guild:
            channel = ctx.guild.get_channel(FEEDBACK_CHANNEL_ID)  # type: ignore[assignment]
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(FEEDBACK_CHANNEL_ID)  # type: ignore[assignment]
            except discord.HTTPException as exc:
                await ctx.reply(
                    "Der Feedback-Kanal konnte nicht gefunden werden. Bitte prüfe die Konfiguration.",
                    mention_author=False,
                )
                log.error("Feedback-Kanal %s nicht erreichbar: %s", FEEDBACK_CHANNEL_ID, exc)
                return

        embed = discord.Embed(
            title="Feedback Hub",
            description=(
                "Teile dein anonymes Feedback zu unserem Server, den Spielern oder deinem Spielerlebnis. "
                "Deine Antworten werden nur intern weitergegeben."
            ),
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="So funktioniert's",
            value=(
                "Klicke auf den Button, beantworte die Fragen im Formular und bestätige. "
                "Dein Feedback bleibt anonym und wird direkt an das Team weitergeleitet."
            ),
            inline=False,
        )

        stored_message_id = db.get_kv("feedback_hub", "interface_message_id")
        existing_message: discord.Message | None = None
        if stored_message_id:
            try:
                existing_message = await channel.fetch_message(int(stored_message_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                existing_message = None

        view = self.view

        try:
            if existing_message:
                await existing_message.edit(embed=embed, view=view)
                message = existing_message
            else:
                message = await channel.send(embed=embed, view=view)
        except discord.HTTPException as exc:
            await ctx.reply(
                "Das Interface konnte nicht gesendet werden. Bitte versuche es später erneut.",
                mention_author=False,
            )
            log.error("Feedback Interface konnte nicht erstellt werden: %s", exc)
            return

        db.set_kv("feedback_hub", "interface_message_id", str(message.id))

        if ctx.channel.id != channel.id:
            await ctx.reply(
                f"Interface erstellt: {message.jump_url}",
                mention_author=False,
            )
        else:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

    @create_feedback_interface.error
    async def on_create_feedback_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("Du benötigst die Berechtigung 'Server verwalten', um diesen Befehl zu nutzen.")
            return
        log.error("Fehler beim Erstellen des Feedback-Interfaces: %s", error, exc_info=True)
        await ctx.reply("Beim Erstellen des Feedback-Interfaces ist ein Fehler aufgetreten.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FeedbackHub(bot))
