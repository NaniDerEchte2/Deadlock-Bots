# cogs/welcome_dm/step_customs.py
import discord
from .base import StepView, FUNNY_CUSTOM_ROLE_ID, GRIND_CUSTOM_ROLE_ID, logger

class CustomGamesView(StepView):
    """Frage 2: Custom Games (Rollen-Toggles)"""
    def __init__(self):
        super().__init__()
        self.sel_funny = False
        self.sel_grind = False
        self._set_next_enabled(False)

    def _set_next_enabled(self, enabled: bool):
        for c in self.children:
            if isinstance(c, discord.ui.Button) and c.custom_id == "wdm:q1:next":
                c.disabled = not enabled
                c.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.primary
                c.label = "Weiter ✅" if enabled else "Weiter"

    async def _toggle_role(self, interaction: discord.Interaction, role_id: int, button: discord.ui.Button, base_label: str):
        guild, member = self._get_guild_and_member(interaction)
        if not guild or not member:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Konnte Guild/Member nicht bestimmen.", ephemeral=True)
            return

        role = guild.get_role(role_id)
        if not role:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Rolle nicht gefunden (ID/Hierarchie prüfen).", ephemeral=True)
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Welcome DM Auswahl")
                button.style = discord.ButtonStyle.secondary
                button.label = base_label
                if role_id == FUNNY_CUSTOM_ROLE_ID:
                    self.sel_funny = False
                elif role_id == GRIND_CUSTOM_ROLE_ID:
                    self.sel_grind = False
            else:
                await member.add_roles(role, reason="Welcome DM Auswahl")
                button.style = discord.ButtonStyle.success
                button.label = f"✔ {base_label}"
                if role_id == FUNNY_CUSTOM_ROLE_ID:
                    self.sel_funny = True
                elif role_id == GRIND_CUSTOM_ROLE_ID:
                    self.sel_grind = True
        except discord.Forbidden:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Rechte fehlen (Manage Roles / Rollenhierarchie).", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"[Custom Toggle] {member.id}: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Fehler beim Rollenwechsel.", ephemeral=True)
            return

        self._set_next_enabled(self.sel_funny or self.sel_grind)

        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="Funny Custom", style=discord.ButtonStyle.secondary, custom_id="wdm:q1:funny")
    async def funny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_role(interaction, FUNNY_CUSTOM_ROLE_ID, button, "Funny Custom")

    @discord.ui.button(label="Grind Custom", style=discord.ButtonStyle.secondary, custom_id="wdm:q1:grind")
    async def grind(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_role(interaction, GRIND_CUSTOM_ROLE_ID, button, "Grind Custom")

    @discord.ui.button(label="Ne danke", style=discord.ButtonStyle.danger, custom_id="wdm:q1:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction, custom_txt="👀 Sicher, dass du in so kurzer Zeit schon alles gelesen hast?"):
            return
        await self._finish(interaction)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="wdm:q1:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return
        await self._finish(interaction)
