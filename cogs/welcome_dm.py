import discord
from discord.ext import commands
import asyncio
import logging

# Rollen IDs
FUNNY_CUSTOM_ROLE_ID = 1407085699374649364
GRIND_CUSTOM_ROLE_ID = 1407086020331311144
PATCHNOTES_ROLE_ID = 1330994309524357140

logger = logging.getLogger(__name__)


# ----------- Views -----------

class CustomRoleView(discord.ui.View):
    """Frage 1: Custom Game Rollen"""

    def __init__(self, member: discord.Member):
        super().__init__(timeout=120)
        self.member = member

    async def toggle_role(self, interaction: discord.Interaction, role_id: int):
        role = self.member.guild.get_role(role_id)
        if not role:
            await interaction.response.send_message("❌ Rolle nicht gefunden", ephemeral=True)
            return
        if role in self.member.roles:
            await self.member.remove_roles(role, reason="Welcome DM Auswahl")
            await interaction.response.send_message(f"❌ {role.name} entfernt", ephemeral=True)
        else:
            await self.member.add_roles(role, reason="Welcome DM Auswahl")
            await interaction.response.send_message(f"✅ {role.name} hinzugefügt", ephemeral=True)

    @discord.ui.button(label="Funny Custom", style=discord.ButtonStyle.secondary)
    async def funny(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore
        await self.toggle_role(interaction, FUNNY_CUSTOM_ROLE_ID)

    @discord.ui.button(label="Grind Custom", style=discord.ButtonStyle.secondary)
    async def grind(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore
        await self.toggle_role(interaction, GRIND_CUSTOM_ROLE_ID)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.success)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore
        await interaction.response.send_message("➡️ Weiter zur nächsten Frage", ephemeral=True)
        self.stop()


class PatchnotesView(discord.ui.View):
    """Frage 2: Patchnotes-Rolle"""

    def __init__(self, member: discord.Member):
        super().__init__(timeout=120)
        self.member = member

    async def toggle_patchnotes(self, interaction: discord.Interaction):
        role = self.member.guild.get_role(PATCHNOTES_ROLE_ID)
        if not role:
            await interaction.response.send_message("❌ Rolle nicht gefunden", ephemeral=True)
            return
        if role in self.member.roles:
            await self.member.remove_roles(role, reason="Welcome DM Auswahl")
            await interaction.response.send_message("❌ Patchnotes entfernt", ephemeral=True)
        else:
            await self.member.add_roles(role, reason="Welcome DM Auswahl")
            await interaction.response.send_message("✅ Patchnotes hinzugefügt", ephemeral=True)

    @discord.ui.button(label="Patchnotes", style=discord.ButtonStyle.secondary)
    async def patchnotes(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore
        await self.toggle_patchnotes(interaction)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.success)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore
        await interaction.response.send_message("✅ Auswahl gespeichert", ephemeral=True)
        self.stop()

    @discord.ui.button(label="Nein danke", style=discord.ButtonStyle.danger)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):  # type: ignore
        role = self.member.guild.get_role(PATCHNOTES_ROLE_ID)
        if role and role in self.member.roles:
            await self.member.remove_roles(role, reason="Welcome DM Auswahl")
        await interaction.response.send_message("🚫 Keine Patchnotes-Benachrichtigungen", ephemeral=True)
        self.stop()


# ----------- Cog -----------

class WelcomeDM(commands.Cog):
    """Cog für automatische Willkommensnachrichten per DM"""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Welcome DM System geladen")

    async def send_welcome_messages(self, member: discord.Member):
        """Sendet Willkommensnachrichten + Fragen"""

        # Begrüßungs-Nachrichten
        messages = [
            "👋 **Willkommen bei Deadlock DACH** 🎮\n\n"
            "Hier findest du Mitspieler, Guides, Patchnotes – und eine aktive Community.\n\n"
            "________________________________________\n\n"
            "📜 **Regeln (Kurzfassung):**\n"
            "✔ Respektvoller Umgang\n"
            "✔ Keine Diskriminierung / Hassrede\n"
            "✔ Keine NSFW Inhalte\n"
            "✔ Keine privaten Daten leaken\n",

            "🎮 **Custom Games:**\n"
            "Wir veranstalten Funny Customs 🤪 und Grind Customs 💪.\n"
            "Damit du keine Spiele verpasst, kannst du gleich Rollen auswählen.\n",

            "📢 **Infos & Updates:**\n"
            "Du kannst dich benachrichtigen lassen, wenn neue Patchnotes erscheinen.\n",

            "🎯 **Rangrollen:**\n"
            "Wähle deine Rangrolle jederzeit hier: "
            "https://discord.com/channels/1289721245281292288/1398021105339334666\n\n"
            "Viel Spaß auf dem Server!"
        ]

        # Nachrichten senden
        for i, message in enumerate(messages, 1):
            try:
                await member.send(message)
                await asyncio.sleep(0.5)
            except discord.Forbidden:
                logger.warning(f"Konnte keine DM an {member.display_name} ({member.id}) senden - DMs deaktiviert")
                return False
            except Exception as e:
                logger.error(f"Fehler beim Senden der Nachricht {i} an {member.display_name}: {e}")
                return False

        # Frage 1: Custom Games
        try:
            await member.send(
                "**Frage 1/2:** Für welche Custom Games möchtest du Ping-Rollen erhalten?\n"
                "Wähle eine oder beide Rollen aus und klicke anschließend **Weiter**."
            )
            custom_view = CustomRoleView(member)
            msg = await member.send(view=custom_view)
            await custom_view.wait()
            await msg.edit(view=None)
        except Exception as e:
            logger.warning(f"Fehler bei Frage 1 für {member.display_name}: {e}")

        # Frage 2: Patchnotes
        try:
            await member.send("**Frage 2/2:** Möchtest du über neue Patchnotes informiert werden?")
            patch_view = PatchnotesView(member)
            msg = await member.send(view=patch_view)
            await patch_view.wait()
            await msg.edit(view=None)
        except Exception as e:
            logger.warning(f"Fehler bei Frage 2 für {member.display_name}: {e}")

        logger.info(f"Willkommens-DM an {member.display_name} ({member.id}) gesendet")
        return True

    @commands.Cog.listener()
    async def on_member_join(self, member):
        """Handler bei neuen Mitgliedern"""
        try:
            await asyncio.sleep(2)
            await self.send_welcome_messages(member)
        except Exception as e:
            logger.error(f"Fehler bei on_member_join für {member.display_name}: {e}")

    @commands.command(name='testwelcome')
    @commands.has_permissions(administrator=True)
    async def test_welcome(self, ctx, user: discord.Member = None):
        """Testet die Willkommensnachricht für einen User"""
        if not user:
            await ctx.send("❌ Bitte gib einen User an: `!testwelcome @user`")
            return
        try:
            await ctx.send(f"📤 Sende Willkommensnachrichten an {user.mention}...")
            success = await self.send_welcome_messages(user)
            if success:
                await ctx.send(f"✅ Erfolgreich an {user.mention} gesendet!")
            else:
                await ctx.send(f"⚠️ Fehler beim Senden an {user.mention}")
        except discord.Forbidden:
            await ctx.send(f"❌ {user.mention} blockiert DMs oder hat sie deaktiviert")
        except Exception as e:
            await ctx.send(f"❌ Fehler: {str(e)}")
            logger.error(f"Fehler bei Test-Welcome für {user.display_name}: {e}")


async def setup(bot):
    """Setup-Funktion für den Cog"""
    await bot.add_cog(WelcomeDM(bot))
