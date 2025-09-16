# cogs/welcome_dm/step_patchnotes.py
import discord
from .base import StepView, PATCHNOTES_ROLE_ID, logger


class PatchnotesView(StepView):
    """Frage 3: Patchnotes (Rolle)"""

    def __init__(self):
        super().__init__()
        self.patch_selected = False
        self._set_next_enabled(False)

    def _set_next_enabled(self, enabled: bool):
        for c in self.children:
            if isinstance(c, discord.ui.Button) and c.custom_id == "wdm:q2:next":
                c.disabled = not enabled
                c.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.primary
                c.label = "Weiter ✅" if enabled else "Weiter"

    @discord.ui.button(label="Patchnotes", style=discord.ButtonStyle.secondary, custom_id="wdm:q2:patch")
    async def toggle_patch(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, member = self._get_guild_and_member(interaction)
        if not guild or not member:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Konnte Guild/Member nicht bestimmen.", ephemeral=True)
            return

        role = guild.get_role(PATCHNOTES_ROLE_ID)
        if not role:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Rolle nicht gefunden (ID/Hierarchie prüfen).", ephemeral=True)
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Welcome DM Auswahl")
                button.style = discord.ButtonStyle.secondary
                button.label = "Patchnotes"
                self.patch_selected = False
            else:
                await member.add_roles(role, reason="Welcome DM Auswahl")
                button.style = discord.ButtonStyle.success
                button.label = "✔ Patchnotes"
                self.patch_selected = True
        except discord.Forbidden:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Rechte fehlen (Manage Roles / Rollenhierarchie).", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"[Patchnotes Toggle] {member.id}: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Fehler beim Rollenwechsel.", ephemeral=True)
            return

        self._set_next_enabled(self.patch_selected)

        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except discord.NotFound:
            logger.debug("PatchnotesView: Original-Message für Edit nicht gefunden (evtl. gelöscht).")
        except discord.HTTPException as e:
            logger.debug("PatchnotesView: HTTPException beim Edit: %r", e)
        except Exception as e:
            logger.warning("PatchnotesView: Unerwarteter Fehler beim Edit: %r", e)

    @discord.ui.button(label="Ne danke", style=discord.ButtonStyle.danger, custom_id="wdm:q2:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction, custom_txt="👀 Sicher, dass du in so kurzer Zeit schon alles gelesen hast?"):
            return
        await self._finish(interaction)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="wdm:q2:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return
        await self._finish(interaction)
