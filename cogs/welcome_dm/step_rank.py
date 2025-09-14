# cogs/welcome_dm/step_rank.py
import discord
from typing import Optional
from .base import (
    StepView, MAIN_GUILD_ID, UBK_ROLE_ID,
    get_rank_emoji, remove_all_rank_roles, logger
)

class RankSelectDropdown(discord.ui.Select):
    def __init__(self, guild_for_emojis: Optional[discord.Guild] = None, parent_view: Optional["RankView"] = None):
        self.parent_view = parent_view
        ranks = [
            ("ubk", "Neu im Game"),
            ("initiate", "Initiate"),
            ("seeker", "Seeker"),
            ("alchemist", "Alchemist"),
            ("arcanist", "Arcanist"),
            ("ritualist", "Ritualist"),
            ("emissary", "Emissary"),
            ("archon", "Archon"),
            ("oracle", "Oracle"),
            ("phantom", "Phantom"),
            ("ascendant", "Ascendant"),
            ("eternus", "Eternus"),
        ]
        options: list[discord.SelectOption] = []
        for key, label in ranks:
            desc  = f"{label} auswählen"
            emoji = get_rank_emoji(guild_for_emojis, key)
            if emoji is not None:
                options.append(discord.SelectOption(label=label, value=key, description=desc, emoji=emoji))
            else:
                options.append(discord.SelectOption(label=label, value=key, description=desc))

        super().__init__(
            placeholder="🎮 Wähle deinen *aktuellen* Deadlock-Rang …",
            min_values=1, max_values=1, options=options,
            custom_id="wdm:q3:rank"
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.client.get_guild(MAIN_GUILD_ID)  # type: ignore
        if guild is None:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Konnte Guild nicht bestimmen.", ephemeral=True)
            return
        member = guild.get_member(interaction.user.id)
        if member is None:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except Exception:
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Konnte Member nicht finden.", ephemeral=True)
                return

        selected = self.values[0]
        if isinstance(self.parent_view, RankView):
            self.parent_view.selected_key = selected

        role_name = "UBK" if selected == "ubk" else selected.capitalize()
        try:
            await remove_all_rank_roles(member, guild)

            if selected == "ubk":
                role = guild.get_role(UBK_ROLE_ID) or discord.utils.get(guild.roles, name="UBK")
                if role is None:
                    role = await guild.create_role(name="UBK", reason="Welcome DM Rangauswahl (Fallback)")
            else:
                role = discord.utils.get(guild.roles, name=role_name)
                if not role:
                    role = await guild.create_role(name=role_name, reason="Welcome DM Rangauswahl")

            await member.add_roles(role, reason="Welcome DM Rangauswahl")
        except discord.Forbidden:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Rechte fehlen, um Rangrollen zu setzen.", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"[Rank Select] {member.id}: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Fehler beim Rangsetzen.", ephemeral=True)
            return

        if isinstance(self.parent_view, RankView):
            self.parent_view._set_next_enabled(True)

        self.placeholder = f"✅ Ausgewählt: {'Neu im Game' if selected=='ubk' else role_name}"
        self.disabled = True

        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self.parent_view)
            else:
                await interaction.message.edit(view=self.parent_view)
        except Exception:
            pass

class ConfirmRankView(StepView):
    def __init__(self, parent_rank_view: "RankView"):
        super().__init__()
        self.parent_rank_view = parent_rank_view

    @discord.ui.button(label="Sicher 👍", style=discord.ButtonStyle.success, custom_id="wdm:q3:confirm_yes")
    async def confirm_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.parent_rank_view.force_finish()
        await self._finish(interaction)

    @discord.ui.button(label="Nochmal ändern", style=discord.ButtonStyle.secondary, custom_id="wdm:q3:confirm_change")
    async def confirm_change(self, interaction: discord.Interaction, button: discord.ui.Button):
        pv = self.parent_rank_view
        try:
            pv.dropdown.disabled = False
            pv.dropdown.placeholder = "🎮 Wähle deinen *aktuellen* Deadlock-Rang …"
            pv._set_next_enabled(False)
            pv.selected_key = None
            if pv.bound_message:
                await pv.bound_message.edit(view=pv)
        except Exception:
            pass
        await self._finish(interaction)

class RankView(StepView):
    def __init__(self, guild_for_emojis: Optional[discord.Guild] = None):
        super().__init__()
        self.dropdown = RankSelectDropdown(guild_for_emojis, parent_view=self)
        self.add_item(self.dropdown)
        self._set_next_enabled(False)
        self.selected_key: Optional[str] = None

    def _set_next_enabled(self, enabled: bool):
        for c in self.children:
            if isinstance(c, discord.ui.Button) and getattr(c, "custom_id", "") == "wdm:q3:next":
                c.disabled = not enabled
                c.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.primary
                c.label = "Weiter ✅" if enabled else "Weiter"

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="wdm:q3:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return

        if self.selected_key == "ubk":
            await self._finish(interaction)
            return

        bait = (
            "👀 **Na? Sicher, dass das dein *AKTUELLER* Rang ist – nicht dein Peak oder Max Rang?**\n"
            "Wenn ja → **Sicher 👍**, ansonsten bitte nochmal ändern. 💙"
        )
        try:
            emb = discord.Embed(title="Kurz checken", description=bait, color=0xB794F4)
            await interaction.channel.send(embed=emb, view=ConfirmRankView(self))
        except Exception:
            pass

        button.disabled = True
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except Exception:
            pass
