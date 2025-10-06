from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

import discord

from .quick_invites import QuickInvite, reserve_quick_invite

log = logging.getLogger(__name__)

QUICK_INVITE_CUSTOM_ID = "steam:quickinvite"


def _format_invite_message(invite: QuickInvite) -> str:
    parts = [
        "⚡ **Hier ist dein persönlicher Schnell-Link zum Steam-Bot:**\n",
        invite.invite_link,
        "\nJeder Link kann genau **einmal** verwendet werden.",
    ]

    if invite.expires_at:
        expires_dt = _dt.datetime.fromtimestamp(invite.expires_at, tz=_dt.timezone.utc)
        parts.append(
            "\nGültig bis {} ({}).".format(
                discord.utils.format_dt(expires_dt, style="R"),
                discord.utils.format_dt(expires_dt, style="f"),
            )
        )
    else:
        parts.append("\nDieser Link verfällt erst, wenn er eingelöst wurde.")

    parts.append("\nTeile den Link nur mit Personen, denen du vertraust.")
    parts.append("\nAlternativ bleibt der Freundescode **820142646** verfügbar.")
    return "".join(parts)


async def respond_with_quick_invite(
    interaction: discord.Interaction,
    *,
    source: Optional[str] = None,
) -> None:
    """Fetch a quick invite from the pool and respond to the interaction."""

    followup = interaction.response.is_done()
    if not followup:
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            followup = True
        except Exception as exc:  # noqa: BLE001
            log.debug("Quick invite defer failed", exc_info=True, extra={"source": source, "error": str(exc)})
            followup = False

    async def _send(message: str) -> None:
        if followup:
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    invite: Optional[QuickInvite] = None
    try:
        invite = reserve_quick_invite(getattr(interaction.user, "id", None))
    except Exception:  # noqa: BLE001
        log.exception("Failed to reserve quick invite", extra={"source": source})

    if not invite:
        await _send("⚠️ Aktuell sind keine Schnell-Links verfügbar. Bitte versuche es gleich noch einmal.")
        return

    await _send(_format_invite_message(invite))


class QuickInviteButton(discord.ui.Button):
    def __init__(
        self,
        *,
        label: str = "Schnelle Anfrage senden",
        style: discord.ButtonStyle = discord.ButtonStyle.success,
        emoji: Optional[str] = "⚡",
        custom_id: str = QUICK_INVITE_CUSTOM_ID,
        row: Optional[int] = None,
        source: Optional[str] = None,
    ) -> None:
        super().__init__(label=label, style=style, emoji=emoji, custom_id=custom_id, row=row)
        self._source = source or "quick-invite-button"

    async def callback(self, interaction: discord.Interaction) -> None:  # noqa: D401
        await respond_with_quick_invite(interaction, source=self._source)


__all__ = ["QUICK_INVITE_CUSTOM_ID", "QuickInviteButton", "respond_with_quick_invite"]
