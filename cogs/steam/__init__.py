"""Steam cog package exports."""

from __future__ import annotations

import logging
from typing import Optional

import discord

log = logging.getLogger(__name__)

try:
    from .schnelllink import (  # type: ignore
        SCHNELL_LINK_CUSTOM_ID,
        SchnellLink,
        SchnellLinkButton,
        respond_with_schnelllink,
    )
    SCHNELL_LINK_AVAILABLE = True
except Exception as exc:  # pragma: no cover - fallback wenn aiohttp fehlt
    log.warning(
        "Schnell-Link-Utilities nicht verfügbar: %s",
        exc,
        exc_info=True,
    )

    SCHNELL_LINK_AVAILABLE = False
    SCHNELL_LINK_CUSTOM_ID = "steam:schnelllink-unavailable"

    class SchnellLink:  # type: ignore[override]
        """Placeholder der klar signalisiert, dass Schnell-Link fehlt."""

        def __init__(self, *_: object, **__: object) -> None:
            raise RuntimeError(
                "Schnell-Link-Integration ist nicht verfügbar (aiohttp fehlt?)."
            )

    class SchnellLinkButton(discord.ui.Button):  # type: ignore[override]
        def __init__(
            self,
            *,
            label: str = "Schnell-Link derzeit nicht verfügbar",
            style: discord.ButtonStyle = discord.ButtonStyle.secondary,
            emoji: Optional[str] = "⚠️",
            custom_id: Optional[str] = None,
            row: Optional[int] = None,
            source: Optional[str] = None,
        ) -> None:
            super().__init__(
                label=label,
                style=style,
                emoji=emoji,
                custom_id=custom_id or SCHNELL_LINK_CUSTOM_ID,
                row=row,
                disabled=True,
            )
            self._source = source or "schnelllink-unavailable"

    async def respond_with_schnelllink(
        interaction: discord.Interaction,
        *,
        source: Optional[str] = None,
    ) -> None:
        message = (
            "⚠️ Der Schnell-Link-Service ist derzeit nicht verfügbar. "
            "Bitte verwende die regulären Verknüpfungsoptionen oder wende dich an das Team."
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

__all__ = [
    "SCHNELL_LINK_AVAILABLE",
    "SCHNELL_LINK_CUSTOM_ID",
    "SchnellLink",
    "SchnellLinkButton",
    "respond_with_schnelllink",
]
