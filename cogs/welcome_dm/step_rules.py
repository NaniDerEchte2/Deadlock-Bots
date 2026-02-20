# cogs/welcome_dm/step_rules.py
import asyncio
from datetime import datetime
from typing import Optional

import discord
from contextlib import suppress  # ⬅️ neu

from .base import (
    StepView,
    ONBOARD_COMPLETE_ROLE_ID,
    THANK_YOU_DELETE_AFTER_SECONDS,
    logger,
)


class RulesView(StepView):
    """Frage 6: Regeln bestätigen + Rolle setzen"""

    def __init__(
        self,
        *,
        allowed_user_id: Optional[int] = None,
        created_at: Optional[datetime] = None,
    ):
        super().__init__(allowed_user_id=allowed_user_id, created_at=created_at)

    @staticmethod
    async def _delete_later(msg: discord.Message, seconds: int):
        await asyncio.sleep(seconds)
        # Löschen darf still scheitern (Nachricht weg/Berechtigungen), aber nicht „alles“ schlucken
        with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            await msg.delete()

    @discord.ui.button(
        label="Habe verstanden :)",
        style=discord.ButtonStyle.success,
        custom_id="wdm:q4:confirm",
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if not await self._enforce_min_wait(interaction):
            return

        guild, member = self._get_guild_and_member(interaction)
        if guild and member:
            try:
                role = guild.get_role(ONBOARD_COMPLETE_ROLE_ID)
                if role:
                    await member.add_roles(role, reason="Welcome DM: Regeln bestätigt")
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning(
                    f"Could not add ONBOARD role to {member.id if member else 'unknown'}: {e}"
                )

        # Danke-Nachricht posten und später löschen – Fehler gezielt behandeln
        channel = interaction.channel
        if channel is not None:
            try:
                thank_embed = discord.Embed(
                    title="✅ Danke!",
                    description="Willkommen an Bord!",
                    color=discord.Color.green(),
                )
                thank_msg = await channel.send(embed=thank_embed)
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.debug("Could not send thank-you message: %s", e)
            else:
                asyncio.create_task(
                    self._delete_later(thank_msg, THANK_YOU_DELETE_AFTER_SECONDS)
                )

        await self._finish(interaction)
