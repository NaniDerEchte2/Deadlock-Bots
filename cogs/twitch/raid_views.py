"""Shared Discord views and embeds for Twitch raid authorization."""

from __future__ import annotations

import logging
from typing import Optional

import discord

log = logging.getLogger("TwitchStreams.RaidViews")

AUTH_BUTTON_LABEL = "OAuth-Link erzeugen"
AUTH_LINK_LABEL = "Twitch autorisieren"


def build_raid_requirements_embed(twitch_login: str) -> discord.Embed:
    """Build the shared requirements embed for raid authorization."""
    login = (twitch_login or "").strip() or "dein Kanal"
    description = (
        f"Hey **{login}**!\n\n"
        "Wir haben eine **neue Anforderungen** für unser Streamer-Partner-Programm. \n"
        "Bitte stell sicher, dass du sie erfüllst, damit alle Features für dich aktiv sind :).\n\n"
        "Eine Pflicht-Anforderung erfüllst du momentan noch nicht: \n"
        "Twitch-Bot-Autorisierung - bitte stelle sicher das du die neue Anforderung erfüllst :).\n\n"

        "**Twitch Bot-Update: Das ist im Hintergrund passiert**\n"
        "1) **Auto-Raid Manager**\n"
        "- Sobald dein Stream offline geht, raidet der Bot einen live-Partner.\n"
        "2) **Chat Guard - Schutz vor Müll im Chat**\n"
        "- Filtert Viewer-Bot/Spam-Muster (Phrasen/Fragmente wie \"Best viewers\", \"streamboo.com\").\n"
        "3) **Analytics Dashboard (Geplant für 03-05/26)**\n"
        "- Retention (5/10/20 Min), Unique Chatters, Kategorie-Vergleich (DE).\n\n"

    )
    return discord.Embed(
        title="🔐 Twitch-Bot Autorisierung",
        description=description,
        color=0x9146FF,
    )


async def _send_interaction_message(
    interaction: discord.Interaction,
    content: str,
    *,
    view: Optional[discord.ui.View] = None,
) -> None:
    """Send a response or follow-up, using ephemeral only in guilds."""
    ephemeral = interaction.guild_id is not None
    kwargs = {"view": view} if view else {}
    if ephemeral:
        kwargs["ephemeral"] = True

    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, **kwargs)
        else:
            await interaction.response.send_message(content, **kwargs)
    except Exception:
        log.exception("Failed to respond with raid auth link")


class _RaidAuthGenerateButton(discord.ui.Button):
    def __init__(self, parent: "RaidAuthGenerateView", *, label: str) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self._view_ref = parent

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        view = self._view_ref
        login = view.twitch_login
        auth_manager = view.auth_manager

        if not auth_manager or not login:
            await _send_interaction_message(
                interaction,
                "Auth-Link konnte nicht erzeugt werden (fehlende Daten).",
            )
            return

        auth_url = auth_manager.generate_auth_url(login)
        link_view = discord.ui.View(timeout=300)
        link_view.add_item(
            discord.ui.Button(
                label=AUTH_LINK_LABEL,
                url=auth_url,
                style=discord.ButtonStyle.link,
            )
        )
        content = (
            f"Hier ist dein Twitch OAuth-Link für **{login}**.\n"
            "Bitte innerhalb von 10 Minuten öffnen, danach läuft der Link ab."
        )
        await _send_interaction_message(interaction, content, view=link_view)


class RaidAuthGenerateView(discord.ui.View):
    """View that generates a fresh OAuth link on click."""

    def __init__(
        self,
        *,
        auth_manager,
        twitch_login: str,
        button_label: str = AUTH_BUTTON_LABEL,
    ) -> None:
        super().__init__(timeout=12 * 60 * 60)
        self.auth_manager = auth_manager
        self.twitch_login = (twitch_login or "").strip().lower()
        self.add_item(_RaidAuthGenerateButton(self, label=button_label))
