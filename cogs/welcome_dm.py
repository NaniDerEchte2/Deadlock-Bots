import discord
from discord.ext import commands
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, Union

# ---------- IDs (bitte prüfen/anpassen) ----------
FUNNY_CUSTOM_ROLE_ID = 1407085699374649364
GRIND_CUSTOM_ROLE_ID  = 1407086020331311144
PATCHNOTES_ROLE_ID    = 1330994309524357140
PHANTOM_NOTIFICATION_CHANNEL_ID = 1374364800817303632
THANK_YOU_DELETE_AFTER_SECONDS = 300  # 5 Minuten
# -------------------------------------------------

logger = logging.getLogger(__name__)

# ========= Emoji-Konfiguration =========
# Wenn eure Emoji-Namen nicht exakt gleich sind, hier ggf. Overrides setzen
# (Emoji-Name ODER Emoji-ID). Sonst lässt es der Bot automatisch per Namen finden.
RANK_EMOJI_OVERRIDES: Dict[str, Union[str, int]] = {
    # "phantom": "dl_phantom",
    # "ascendant": 123456789012345678,
}

# Nur für "unknown" ein Unicode-Fallback; alle anderen Ränge haben Server-Emojis
UNKNOWN_FALLBACK_EMOJI = "❓"
# ======================================

# =========================
#   Hilfsfunktionen
# =========================

def _find_custom_emoji(guild: discord.Guild, key: Union[str, int]) -> Optional[Union[discord.Emoji, discord.PartialEmoji]]:
    """Findet ein Custom-Emoji per Name (contains) oder per ID."""
    try:
        if isinstance(key, int) or (isinstance(key, str) and key.isdigit()):
            emoji_id = int(key)
            for e in guild.emojis:
                if e.id == emoji_id:
                    return e
            return discord.PartialEmoji(name=None, id=emoji_id, animated=False)
        else:
            name = str(key).lower()
            for e in guild.emojis:
                if e.name.lower() == name:
                    return e
            for e in guild.emojis:
                if name in e.name.lower():
                    return e
    except Exception:
        return None
    return None


def get_rank_emoji(guild: discord.Guild, rank_key: str) -> Optional[Union[discord.Emoji, discord.PartialEmoji, str]]:
    """Liefert Emoji für Rang: Override → Suche per Rangname → None (außer 'unknown' -> ❓)."""
    if rank_key in RANK_EMOJI_OVERRIDES:
        e = _find_custom_emoji(guild, RANK_EMOJI_OVERRIDES[rank_key])
        if e:
            return e
    e2 = _find_custom_emoji(guild, rank_key)
    if e2:
        return e2
    if rank_key == "unknown":
        return UNKNOWN_FALLBACK_EMOJI
    return None


async def remove_all_rank_roles(member: discord.Member, guild: discord.Guild):
    """Entfernt ggf. vorhandene Deadlock-Rangrollen."""
    ranks = {
        "initiate", "seeker", "alchemist", "arcanist", "ritualist",
        "emissary", "archon", "oracle", "phantom", "ascendant", "eternus"
    }
    to_remove = [r for r in member.roles if r.name.lower() in ranks]
    if to_remove:
        await member.remove_roles(*to_remove, reason="Welcome DM Rangauswahl")


def build_step_embed(title: str, desc: str, step: int, total: int, color: int = 0x5865F2) -> discord.Embed:
    """Einheitlicher Embed pro Schritt (Discord-blau als Default)."""
    emb = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.now())
    emb.set_footer(text=f"Frage {step} von {total} • Deadlock DACH")
    return emb


# =========================
#        BASE VIEW
# =========================

class StepView(discord.ui.View):
    """
    Basisklasse für eine Frage mit "Weiter"/"Ne danke".
    - proceed: True, sobald 'Weiter' oder 'Ne danke' gedrückt wurde.
    - Toggles/Dropdowns beenden den Step NICHT.
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
    """Frage 3: Rang-Auswahl (Dropdown mit Server-Emojis)"""

    def __init__(self, member: discord.Member, guild: discord.Guild):
        self.member = member
        self.guild = guild

        ranks = [
            "unknown", "initiate", "seeker", "alchemist", "arcanist", "ritualist",
            "emissary", "archon", "oracle", "phantom", "ascendant", "eternus"
        ]

        options: list[discord.SelectOption] = []
        for r in ranks:
            label = r.capitalize()
            value = r
            desc  = f"{label} auswählen"
            emoji = get_rank_emoji(guild, r)  # Server-Emoji (oder ❓ bei unknown)
            if emoji is not None:
                opt = discord.SelectOption(label=label, value=value, description=desc, emoji=emoji)
            else:
                opt = discord.SelectOption(label=label, value=value, description=desc)
            options.append(opt)

        super().__init__(
            placeholder="🎮 Wähle deinen Deadlock-Rang…",
            min_values=1, max_values=1, options=options
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        m: discord.Member = await self.member.guild.fetch_member(self.member.id)

        if selected == "unknown":
            # Freundliche Nachricht (eigene Message, nicht ephemeral)
            try:
                await interaction.channel.send(
                    "ℹ️ **Unknown/Neu** gewählt.\n"
                    "**Willkommen an Bord! 😊**\n"
                    "Wenn du neu bist: Basics & erste Schritte findest du hier: "
                    "https://discord.com/channels/1289721245281292288/1326975033838665803\n"
                    "Wenn du magst, helfen wir dir beim Reinkommen – sag einfach kurz Bescheid, "
                    "dann gehen wir in Ruhe alles Wichtige zu **Deadlock** durch. "
                    "Schreib dazu einfach **@earlysalty** aka Nani 🙂"
                )
            except Exception:
                pass
            await interaction.response.send_message("👍 Alles klar – Unknown gesetzt (keine Rangrolle vergeben).", ephemeral=True)
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

    @staticmethod
    async def _delete_later(msg: discord.Message, seconds: int):
        await asyncio.sleep(seconds)
        try:
            await msg.delete()
        except Exception:
            pass

    @discord.ui.button(label="Habe verstanden :)", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Danke-Nachricht als eigene DM und nach 5 Min entfernen
        try:
            thank_msg = await interaction.channel.send("✅ Danke! Willkommen an Bord!")
            asyncio.create_task(self._delete_later(thank_msg, THANK_YOU_DELETE_AFTER_SECONDS))
        except Exception:
            pass

        await self._finish(interaction)


# =========================
#           COG
# =========================

class WelcomeDM(commands.Cog):
    """Cog für Willkommens-DM (Embeds + Components)"""

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

    async def _send_step_embed(self, member: discord.Member, *, title: str, desc: str, step: int, total: int, view: StepView, color: int = 0x5865F2) -> bool:
        """Sendet einen hübschen Embed + View und wartet, bis der Step abgeschlossen wurde."""
        emb = build_step_embed(title, desc, step, total, color=color)
        msg = await member.send(embed=emb, view=view)
        try:
            await view.wait()  # stop() wird nur in _finish() gerufen
        finally:
            try:
                await msg.delete()
            except Exception:
                pass
        return view.proceed  # False bei Timeout

    async def send_welcome_messages(self, member: discord.Member):
        lock = self._get_lock(member.id)
        async with lock:  # verhindert parallele Sessions je User
            greet_msg: Optional[discord.Message] = None
            try:
                # Vorherige Bot-DMs aufräumen
                await self._cleanup_old_bot_dms(member, limit=50)

                # Begrüßung (einfach, wird am Ende entfernt)
                greet_msg = await member.send(
                    "👋 **Willkommen bei Deadlock DACH!**\n\n"
                    "Diese DM hilft dir beim Start: Wir vergeben dir passende Rollen und zeigen dir die wichtigsten Infos."
                )

                # ---- Frage 1 ----
                q1_desc = (
                    "➡️ **Funny Customs** – entspannte Fun-Runden\n"
                    "➡️ **Grind Customs** – Tryhard & Ranglisten-Feeling\n\n"
                    "Du kannst beide wählen, nur eine – oder **Ne danke**."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 1/4 · Lust auf Custom Games?",
                    desc=q1_desc,
                    step=1, total=4,
                    view=CustomGamesView(member),
                    color=0x2ECC71  # grünlich
                ):
                    return False

                # ---- Frage 2 ----
                q2_desc = (
                    "Möchtest du über neue **Patchnotes** informiert werden?\n"
                    "So verpasst du keine Balance-Änderungen oder neuen Content."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 2/4 · Patchnotes-Benachrichtigungen",
                    desc=q2_desc,
                    step=2, total=4,
                    view=PatchnotesView(member),
                    color=0x3498DB  # blau
                ):
                    return False

                # ---- Frage 3 ----
                q3_desc = (
                    "Wähle deinen **Deadlock-Rang**.\n"
                    "Bist du neu/unsicher → **Unknown**. Klicke danach **Weiter**."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 3/4 · Rang auswählen",
                    desc=q3_desc,
                    step=3, total=4,
                    view=RankView(member, member.guild),
                    color=0x9B59B6  # lila
                ):
                    return False

                # ---- Frage 4 ----
                q4_desc = (
                    "Bitte lies das **Regelwerk** im Server.\n"
                    "Bestätige hier, dass du es verstanden hast."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 4/4 · Regelwerk bestätigen",
                    desc=q4_desc,
                    step=4, total=4,
                    view=RulesView(),
                    color=0xE67E22  # orange
                ):
                    return False

                # Begrüßungsnachricht am Ende entfernen
                try:
                    if greet_msg:
                        await greet_msg.delete()
                except Exception:
                    pass

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
