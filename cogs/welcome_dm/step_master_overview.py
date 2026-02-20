"""Step-Views für den Master-Bot-Intro und Server-Rundgang."""

from __future__ import annotations

import discord

from .base import StepView


class MasterBotIntroView(StepView):
    """Vorstellung des Master-Bots als erster Schritt."""

    @discord.ui.button(
        label="Alles klar ➜",
        style=discord.ButtonStyle.primary,
        custom_id="wdm:q1:masterbot",
    )
    async def confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        if not await self._enforce_min_wait(interaction):
            return
        await self._finish(interaction)


class ServerTourView(StepView):
    """Führt neue Nutzer durch die wichtigsten Server-Bereiche."""

    @discord.ui.button(
        label="Weiter zum Setup",
        style=discord.ButtonStyle.success,
        custom_id="wdm:q2:servertour",
    )
    async def next_step(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):
        if not await self._enforce_min_wait(interaction):
            return
        await self._finish(interaction)
