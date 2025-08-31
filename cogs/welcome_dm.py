import discord
from discord.ext import commands
import asyncio
import logging
from datetime import datetime

# ------------- IDs bitte anpassen -------------
FUNNY_CUSTOM_ROLE_ID = 1407085699374649364
GRIND_CUSTOM_ROLE_ID = 1407086020331311144
PATCHNOTES_ROLE_ID = 1330994309524357140
PHANTOM_NOTIFICATION_CHANNEL_ID = 1374364800817303632
# ---------------------------------------------

logger = logging.getLogger(__name__)

# -------------------------------------------------------
# Hilfsfunktion aus deinem Rang-Bot
async def remove_all_rank_roles(member, guild):
    ranks = [
        "initiate", "seeker", "alchemist", "arcanist", "ritualist",
        "emissary", "archon", "oracle", "phantom", "ascendant", "eternus"
    ]
    for role in member.roles:
        if role.name.lower() in ranks:
            await member.remove_roles(role)
# -------------------------------------------------------


# ----------- Views -----------

class CustomGamesView(discord.ui.View):
    """Frage 1: Custom Games (mit Toggle-Buttons)"""

    def __init__(self, member: discord.Member):
        super().__init__(timeout=180)
        self.member = member
        self.state = {
            "funny": False,
            "grind": False
        }

    async def toggle_role(self, interaction, role_id, key, button: discord.ui.Button):
        role = self.member.guild.get_role(role_id)
        if not role:
            await interaction.response.send_message("❌ Rolle nicht gefunden", ephemeral=True)
            return

        if role in self.member.roles:
            await self.member.remove_roles(role, reason="Welcome DM Auswahl")
            self.state[key] = False
            button.style = discord.ButtonStyle.secondary
            button.label = button.label.replace("✔ ", "")
        else:
            await self.member.add_roles(role, reason="Welcome DM Auswahl")
            self.state[key] = True
            button.style = discord.ButtonStyle.success
            if not button.label.startswith("✔ "):
                button.label = f"✔ {button.label}"

        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Funny Custom", style=discord.ButtonStyle.secondary)
    async def funny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.toggle_role(interaction, FUNNY_CUSTOM_ROLE_ID, "funny", button)

    @discord.ui.button(label="Grind Custom", style=discord.ButtonStyle.secondary)
    async def grind(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.toggle_role(interaction, GRIND_CUSTOM_ROLE_ID, "grind", button)

    @discord.ui.button(label="Ne danke", style=discord.ButtonStyle.danger)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🚫 Kein Interesse an Custom Games", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        self.stop()


class PatchnotesView(discord.ui.View):
    """Frage 2: Patchnotes"""

    def __init__(self, member: discord.Member):
        super().__init__(timeout=120)
        self.member = member
        self.active = False

    @discord.ui.button(label="Patchnotes", style=discord.ButtonStyle.secondary)
    async def toggle_patch(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = self.member.guild.get_role(PATCHNOTES_ROLE_ID)
        if not role:
            await interaction.response.send_message("❌ Rolle nicht gefunden", ephemeral=True)
            return

        if role in self.member.roles:
            await self.member.remove_roles(role, reason="Welcome DM Auswahl")
            button.style = discord.ButtonStyle.secondary
            button.label = "Patchnotes"
        else:
            await self.member.add_roles(role, reason="Welcome DM Auswahl")
            button.style = discord.ButtonStyle.success
            button.label = "✔ Patchnotes"

        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Ne danke", style=discord.ButtonStyle.danger)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        self.stop()

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        self.stop()


class RankSelectDropdown(discord.ui.Select):
    """Frage 3: Rang-Auswahl"""

    def __init__(self, member, guild):
        self.member = member
        self.guild = guild

        ranks = [
            "unknown", "initiate", "seeker", "alchemist", "arcanist", "ritualist",
            "emissary", "archon", "oracle", "phantom", "ascendant", "eternus"
        ]

        options = []
        for rank in ranks:
            option = discord.SelectOption(
                label=rank.capitalize(),
                value=rank,
                description=f"Wähle {rank.capitalize()} als Rang"
            )
            options.append(option)

        super().__init__(
            placeholder="🎮 Wähle deinen Deadlock-Rang...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction):
        selected_rank = self.values[0]

        await remove_all_rank_roles(self.member, self.guild)

        if selected_rank == "unknown":
            await interaction.response.send_message(
                "ℹ️ Du hast **Unknown/Neu** gewählt. Keine Sorge – wir helfen dir beim Einstieg ins Game. "
                "Schau in den Tutorial-Kanal oder frag Mods nach Tipps! 💡",
                ephemeral=True
            )
            return

        role = discord.utils.get(self.guild.roles, name=selected_rank.capitalize())
        if not role:
            role = await self.guild.create_role(name=selected_rank.capitalize())
        await self.member.add_roles(role)

        # Phantom+ Notification
        if selected_rank in ["phantom", "ascendant", "eternus"]:
            channel = self.guild.get_channel(PHANTOM_NOTIFICATION_CHANNEL_ID)
            if channel:
                embed = discord.Embed(
                    title="🔥 Phantom+ Rang Update",
                    description=f"**{self.member.display_name}** hat sich den Rang **{selected_rank.capitalize()}** gesetzt!",
                    color=0xff6b35,
                    timestamp=datetime.now()
                )
                await channel.send(embed=embed)

        await interaction.response.send_message(
            f"✅ Rang **{selected_rank.capitalize()}** gesetzt!", ephemeral=True
        )


class RankView(discord.ui.View):
    def __init__(self, member, guild):
        super().__init__(timeout=180)
        self.add_item(RankSelectDropdown(member, guild))


class RulesView(discord.ui.View):
    """Frage 4: Regelwerk"""

    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.button(label="Habe verstanden :)", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("✅ Danke! Willkommen an Bord!", ephemeral=True)
        self.stop()


# ----------- Cog -----------

class WelcomeDM(commands.Cog):
    """Cog für Willkommens-DM"""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Welcome DM System geladen")

    async def send_welcome_messages(self, member: discord.Member):
        try:
            # Begrüßung
            await member.send(
                "👋 Willkommen bei **Deadlock DACH**!\n\n"
                "Damit du dich schnell zurechtfindest, stellen wir dir ein paar Fragen. "
                "So bekommst du direkt die passenden Rollen und Infos."
            )

            # Frage 1: Custom Games
            await member.send(
                "**Frage 1/4:** Möchtest du bei Custom Games mitmachen?\n\n"
                "➡️ Funny Customs = entspannte Fun-Runden\n"
                "➡️ Grind Customs = Tryhard & Ranglisten-Feeling\n\n"
                "Du kannst beide wählen, nur eine, oder 'Ne danke'."
            )
            custom_view = CustomGamesView(member)
            msg = await member.send(view=custom_view)
            await custom_view.wait()
            await msg.edit(view=None)

            # Frage 2: Patchnotes
            await member.send(
                "**Frage 2/4:** Möchtest du über neue Patchnotes informiert werden?\n"
                "So verpasst du keine Balance-Änderungen oder neue Inhalte."
            )
            patch_view = PatchnotesView(member)
            msg = await member.send(view=patch_view)
            await patch_view.wait()
            await msg.edit(view=None)

            # Frage 3: Rangwahl
            await member.send(
                "**Frage 3/4:** Wähle hier deinen Deadlock-Rang.\n"
                "Falls du neu bist oder unsicher → **Unknown**."
            )
            rank_view = RankView(member, member.guild)
            msg = await member.send(view=rank_view)
            await rank_view.wait()
            await msg.edit(view=None)

            # Frage 4: Regelwerk
            await member.send(
                "**Frage 4/4:** Bitte lies kurz das Regelwerk im Server.\n"
                "Bestätige hier, dass du es verstanden hast 👇"
            )
            rules_view = RulesView()
            msg = await member.send(view=rules_view)
            await rules_view.wait()
            await msg.edit(view=None)

            logger.info(f"Willkommens-DM erfolgreich an {member.display_name} gesendet")
            return True

        except discord.Forbidden:
            logger.warning(f"Konnte keine DM an {member.display_name} senden - DMs deaktiviert")
            return False
        except Exception as e:
            logger.error(f"Fehler beim Senden an {member.display_name}: {e}")
            return False

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await asyncio.sleep(2)
        await self.send_welcome_messages(member)

    @commands.command(name="testwelcome")
    @commands.has_permissions(administrator=True)
    async def test_welcome(self, ctx, user: discord.Member = None):
        if not user:
            await ctx.send("❌ Bitte gib einen User an: `!testwelcome @user`")
            return
        await ctx.send(f"📤 Sende Welcome-DM an {user.mention} ...")
        success = await self.send_welcome_messages(user)
        if success:
            await ctx.send(f"✅ Erfolgreich an {user.mention} gesendet!")
        else:
            await ctx.send(f"⚠️ Fehler beim Senden an {user.mention}")


async def setup(bot):
    await bot.add_cog(WelcomeDM(bot))
