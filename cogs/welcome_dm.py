import discord
from discord.ext import commands
import asyncio
import logging
from datetime import datetime
from typing import Dict

# ---------- IDs (bitte prüfen/anpassen) ----------
FUNNY_CUSTOM_ROLE_ID = 1407085699374649364
GRIND_CUSTOM_ROLE_ID  = 1407086020331311144
PATCHNOTES_ROLE_ID    = 1330994309524357140
PHANTOM_NOTIFICATION_CHANNEL_ID = 1374364800817303632
# -------------------------------------------------

logger = logging.getLogger(__name__)

# ---- Helper: alle Deadlock-Rangrollen entfernen ----
async def remove_all_rank_roles(member: discord.Member, guild: discord.Guild):
    ranks = {
        "initiate", "seeker", "alchemist", "arcanist", "ritualist",
        "emissary", "archon", "oracle", "phantom", "ascendant", "eternus"
    }
    to_remove = [r for r in member.roles if r.name.lower() in ranks]
    if to_remove:
        await member.remove_roles(*to_remove, reason="Welcome DM Rangauswahl")


# =========================
#        BASE VIEW
# =========================

class StepView(discord.ui.View):
    """
    Basisklasse für eine Frage.
    - proceed: True, sobald 'Weiter' / 'Ne danke' gedrückt wurde.
    - Toggles beenden den Step NICHT.
    - Beim Abschluss: Buttons disablen + Nachricht löschen.
    """
    def __init__(self, *, timeout: float = 420):
        super().__init__(timeout=timeout)
        self.proceed: bool = False

    async def _finish(self, interaction: discord.Interaction):
        # Buttons deaktivieren, Nachricht aktualisieren & löschen
        for child in self.children:
            child.disabled = True
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except Exception:
            pass
        try:
            await interaction.message.delete()
        except Exception:
            pass
        self.proceed = True
        self.stop()


# =========================
#          VIEWS
# =========================

class CustomGamesView(StepView):
    """Frage 1: Custom Games (Toggle-Buttons + Weiter/Ne danke)"""

    def __init__(self, member: discord.Member):
        super().__init__(timeout=420)
        self.member = member

    async def _fresh_member(self) -> discord.Member:
        return await self.member.guild.fetch_member(self.member.id)

    async def _toggle_role(
        self,
        interaction: discord.Interaction,
        role_id: int,
        button: discord.ui.Button,
        base_label: str
    ):
        role = self.member.guild.get_role(role_id)
        if not role:
            await interaction.response.send_message("❌ Rolle nicht gefunden (ID/Hierarchie prüfen).", ephemeral=True)
            return

        m = await self._fresh_member()
        try:
            if role in m.roles:
                await m.remove_roles(role, reason="Welcome DM Auswahl")
                button.style = discord.ButtonStyle.secondary
                button.label = base_label
            else:
                await m.add_roles(role, reason="Welcome DM Auswahl")
                button.style = discord.ButtonStyle.success
                button.label = f"✔ {base_label}"
        except discord.Forbidden:
            await interaction.response.send_message("❌ Rechte fehlen (Manage Roles / Rollenhierarchie).", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"[Custom Toggle] {m.id}: {e}")
            await interaction.response.send_message("⚠️ Fehler beim Rollenwechsel.", ephemeral=True)
            return

        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Funny Custom", style=discord.ButtonStyle.secondary)
    async def funny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_role(interaction, FUNNY_CUSTOM_ROLE_ID, button, "Funny Custom")

    @discord.ui.button(label="Grind Custom", style=discord.ButtonStyle.secondary)
    async def grind(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_role(interaction, GRIND_CUSTOM_ROLE_ID, button, "Grind Custom")

    @discord.ui.button(label="Ne danke", style=discord.ButtonStyle.danger)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction)


class PatchnotesView(StepView):
    """Frage 2: Patchnotes (gleiches Toggle-Verhalten wie Customs)"""

    def __init__(self, member: discord.Member):
        super().__init__(timeout=420)
        self.member = member

    async def _fresh_member(self) -> discord.Member:
        return await self.member.guild.fetch_member(self.member.id)

    @discord.ui.button(label="Patchnotes", style=discord.ButtonStyle.secondary)
    async def toggle_patch(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = self.member.guild.get_role(PATCHNOTES_ROLE_ID)
        if not role:
            await interaction.response.send_message("❌ Rolle nicht gefunden (ID/Hierarchie prüfen).", ephemeral=True)
            return

        m = await self._fresh_member()
        try:
            if role in m.roles:
                await m.remove_roles(role, reason="Welcome DM Auswahl")
                button.style = discord.ButtonStyle.secondary
                button.label = "Patchnotes"
            else:
                await m.add_roles(role, reason="Welcome DM Auswahl")
                button.style = discord.ButtonStyle.success
                button.label = "✔ Patchnotes"
        except discord.Forbidden:
            await interaction.response.send_message("❌ Rechte fehlen (Manage Roles / Rollenhierarchie).", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"[Patchnotes Toggle] {m.id}: {e}")
            await interaction.response.send_message("⚠️ Fehler beim Rollenwechsel.", ephemeral=True)
            return

        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Ne danke", style=discord.ButtonStyle.danger)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction)


class RankSelectDropdown(discord.ui.Select):
    """Frage 3: Rang-Auswahl (Dropdown)"""

    def __init__(self, member: discord.Member, guild: discord.Guild):
        self.member = member
        self.guild = guild

        ranks = [
            "unknown", "initiate", "seeker", "alchemist", "arcanist", "ritualist",
            "emissary", "archon", "oracle", "phantom", "ascendant", "eternus"
        ]
        options = [discord.SelectOption(label=r.capitalize(), value=r, description=f"{r.capitalize()} auswählen") for r in ranks]

        super().__init__(placeholder="🎮 Wähle deinen Deadlock-Rang...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        m: discord.Member = await self.member.guild.fetch_member(self.member.id)

        if selected == "unknown":
            await interaction.response.send_message(
                "ℹ️ **Unknown/Neu** gewählt. Schau gern im Tutorial-Kanal vorbei – Mods helfen beim Einstieg. 💡",
                ephemeral=True
            )
            return

        try:
            await remove_all_rank_roles(m, self.guild)
            role_name = selected.capitalize()
            role = discord.utils.get(self.guild.roles, name=role_name)
            if not role:
                role = await self.guild.create_role(name=role_name, reason="Welcome DM Rangauswahl")
            await m.add_roles(role, reason="Welcome DM Rangauswahl")
        except discord.Forbidden:
            await interaction.response.send_message("❌ Rechte fehlen, um Rangrollen zu setzen.", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"[Rank Select] {m.id}: {e}")
            await interaction.response.send_message("⚠️ Fehler beim Rangsetzen.", ephemeral=True)
            return

        if selected in {"phantom", "ascendant", "eternus"}:
            ch = self.guild.get_channel(PHANTOM_NOTIFICATION_CHANNEL_ID)
            if ch:
                embed = discord.Embed(
                    title="🔥 Phantom+ Rang Update",
                    description=f"**{m.display_name}** hat sich den Rang **{role_name}** gesetzt!",
                    color=0xFF6B35,
                    timestamp=datetime.now()
                )
                try:
                    await ch.send(embed=embed)
                except Exception:
                    pass

        await interaction.response.send_message(f"✅ Rang **{role_name}** gesetzt!", ephemeral=True)


class RankView(StepView):
    def __init__(self, member: discord.Member, guild: discord.Guild):
        super().__init__(timeout=420)
        self.add_item(RankSelectDropdown(member, guild))

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction)


class RulesView(StepView):
    """Frage 4: Regelwerk bestätigen"""

    def __init__(self):
        super().__init__(timeout=420)

    @discord.ui.button(label="Habe verstanden :)", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("✅ Danke! Willkommen an Bord!", ephemeral=True)
        await self._finish(interaction)


# =========================
#           COG
# =========================

class WelcomeDM(commands.Cog):
    """Cog für Willkommens-DM"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session_locks: Dict[int, asyncio.Lock] = {}  # pro-User Flow-Sperre

    def _get_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._session_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[user_id] = lock
        return lock

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Welcome DM System geladen")

    async def _cleanup_old_bot_dms(self, member: discord.Member, limit: int = 50):
        """Löscht alte Bot-Nachrichten in der DM, damit keine alten Views stören."""
        try:
            dm = member.dm_channel or await member.create_dm()
            async for msg in dm.history(limit=limit):
                if msg.author.id == self.bot.user.id:
                    try:
                        await msg.delete()
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"DM-Cleanup für {member.id} übersprungen: {e}")

    async def _send_step(self, member: discord.Member, text: str, view: StepView) -> bool:
        """Sendet eine Frage (Text + View) und wartet, bis 'Weiter/Ne danke' gedrückt wurde oder Timeout."""
        msg = await member.send(text, view=view)
        try:
            await view.wait()  # stop() wird nur in _finish() gerufen
        finally:
            # Falls die Nachricht noch existiert -> löschen
            try:
                await msg.delete()
            except Exception:
                pass
        return view.proceed  # False bei Timeout

    async def send_welcome_messages(self, member: discord.Member):
        lock = self._get_lock(member.id)
        async with lock:  # verhindert parallele Sessions je User
            try:
                # Vorherige Bot-DMs aufräumen, damit keine alten Views übrig sind
                await self._cleanup_old_bot_dms(member, limit=50)

                # Begrüßung
                await member.send(
                    "👋 **Willkommen bei Deadlock DACH!**\n\n"
                    "Diese DM hilft dir beim Start: Wir vergeben dir passende Rollen und zeigen dir die wichtigsten Infos."
                )

                # ---- Frage 1 ----
                custom_text = (
                    "**Frage 1/4:** Lust auf Custom Games?\n\n"
                    "➡️ **Funny Customs** – entspannte Fun-Runden\n"
                    "➡️ **Grind Customs** – Tryhard & Ranglisten-Feeling\n\n"
                    "Du kannst beide wählen, nur eine – oder **Ne danke**."
                )
                if not await self._send_step(member, custom_text, CustomGamesView(member)):
                    return False  # Timeout -> Abbruch

                # ---- Frage 2 ----
                patch_text = (
                    "**Frage 2/4:** Patchnotes-Benachrichtigungen aktivieren?\n"
                    "So verpasst du keine Balance-Änderungen oder neuen Content."
                )
                if not await self._send_step(member, patch_text, PatchnotesView(member)):
                    return False

                # ---- Frage 3 ----
                rank_text = (
                    "**Frage 3/4:** Wähle deinen Deadlock-Rang.\n"
                    "Bist du neu/unsicher → **Unknown**. Klicke danach **Weiter**."
                )
                if not await self._send_step(member, rank_text, RankView(member, member.guild)):
                    return False

                # ---- Frage 4 ----
                rules_text = (
                    "**Frage 4/4:** Bitte lies das Regelwerk im Server.\n"
                    "Bestätige hier, dass du es verstanden hast 👇"
                )
                if not await self._send_step(member, rules_text, RulesView()):
                    return False

                logger.info(f"Welcome-DM abgeschlossen für {member} ({member.id})")
                return True

            except discord.Forbidden:
                logger.warning(f"DM an {member} ({member.id}) nicht möglich (DMs aus / blockiert)")
                return False
            except Exception as e:
                logger.error(f"Fehler beim Welcome-DM an {member} ({member.id}): {e}")
                return False

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await asyncio.sleep(2)
        await self.send_welcome_messages(member)

    @commands.command(name="testwelcome")
    @commands.has_permissions(administrator=True)
    async def test_welcome(self, ctx: commands.Context, user: discord.Member = None):
        if not user:
            await ctx.send("❌ Bitte gib einen User an: `!testwelcome @user`")
            return
        await ctx.send(f"📤 Sende Welcome-DM an {user.mention} …")
        ok = await self.send_welcome_messages(user)
        await ctx.send("✅ Erfolgreich gesendet!" if ok else "⚠️ Senden fehlgeschlagen.")


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeDM(bot))
