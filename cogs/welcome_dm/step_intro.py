# cogs/welcome_dm/step_intro.py
from datetime import datetime

import discord

from .base import StepView


class IntroView(StepView):
    """Erste Nachricht – kein Anti-Skip hier."""

    def __init__(
        self,
        *,
        allowed_user_id: int | None = None,
        created_at: datetime | None = None,
    ):
        super().__init__(allowed_user_id=allowed_user_id, created_at=created_at)
        self.first_click_done = False

    @discord.ui.button(
        label="Weiter ➜",
        style=discord.ButtonStyle.primary,
        custom_id="wdm:q0:intro_next",
    )
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction)
