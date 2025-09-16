# cogs/welcome_dm/step_status.py
import logging
import discord
from typing import Optional
from .base import StepView, STATUS_NEED_BETA, STATUS_PLAYING, STATUS_RETURNING, STATUS_NEW_PLAYER

logger = logging.getLogger(__name__)

class PlayerStatusView(StepView):
    """Frage 1: Status"""
    def __init__(self):
        super().__init__()
        self.choice: Optional[str] = None
        self._set_next_enabled(False)

    def _set_next_enabled(self, enabled: bool):
        for c in self.children:
            if isinstance(c, discord.ui.Button) and c.custom_id == "wdm:qS:next":
                c.disabled = not enabled
                c.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.primary
                c.label = "Weiter ✅" if enabled else "Weiter"

    @discord.ui.select(
        placeholder="Bitte Status wählen …",
        min_values=1, max_values=1,
        options=[
            discord.SelectOption(label="Ich will spielen – brauche Beta-Invite", value=STATUS_NEED_BETA, emoji="🎟️"),
            discord.SelectOption(label="Ich spiele bereits", value=STATUS_PLAYING, emoji="✅"),
            discord.SelectOption(label="Ich fange gerade wieder an", value=STATUS_RETURNING, emoji="🔁"),
            discord.SelectOption(label="Neu im Game", value=STATUS_NEW_PLAYER, emoji="✨"),
        ],
        custom_id="wdm:qS:status"
    )
    async def status_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.choice = select.values[0]
        label_map = {opt.value: opt.label for opt in select.options}
        select.placeholder = f"✅ Ausgewählt: {label_map.get(self.choice, '—')}"
        select.disabled = True
        self._set_next_enabled(True)
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except discord.HTTPException as e:
            logger.warning("Edit message fehlgeschlagen (user=%s): %s", getattr(interaction.user, "id", "?"), e)
        except Exception:
            logger.exception("Unerwarteter Fehler beim Edit der Status-Nachricht (user=%s)", getattr(interaction.user, "id", "?"))

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="wdm:qS:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return
        if not self.choice:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Bitte wähle zuerst eine Option.", ephemeral=True)
                else:
                    await interaction.followup.send("Bitte wähle zuerst eine Option.", ephemeral=True)
            except discord.HTTPException as e:
                logger.warning("Hinweis senden fehlgeschlagen (user=%s): %s", getattr(interaction.user, "id", "?"), e)
            except Exception:
                logger.exception("Unerwarteter Fehler beim Senden des Hinweises (user=%s)", getattr(interaction.user, "id", "?"))
            return
        await self._finish(interaction)
