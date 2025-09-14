# cogs/welcome_dm/step_intro.py
import discord
from .base import StepView

class IntroView(StepView):
    """Erste Nachricht – kein Anti-Skip hier."""
    def __init__(self):
        super().__init__()
        self.first_click_done = False

    @discord.ui.button(label="Weiter ➜", style=discord.ButtonStyle.primary, custom_id="wdm:q0:intro_next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction)
