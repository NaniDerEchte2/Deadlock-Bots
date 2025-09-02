# cogs/welcome_dm.py
import discord
from discord.ext import commands
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, Union

# ---------- IDs (prüfen/anpassen) ----------
MAIN_GUILD_ID                   = 1289721245281292288  # Haupt-Guild (für Member/Rollen in DMs)
FUNNY_CUSTOM_ROLE_ID            = 1407085699374649364
GRIND_CUSTOM_ROLE_ID            = 1407086020331311144
PATCHNOTES_ROLE_ID              = 1330994309524357140
UBK_ROLE_ID                     = 1397687886580547745  # UBK (= Unbekannt) Pflicht-Fallback für Rang
PHANTOM_NOTIFICATION_CHANNEL_ID = 1374364800817303632
ONBOARD_COMPLETE_ROLE_ID        = 1304216250649415771  # Rolle nach Regelbestätigung
THANK_YOU_DELETE_AFTER_SECONDS  = 300  # 5 Minuten
# -------------------------------------------

logger = logging.getLogger(__name__)

# ========= Emoji-Konfiguration =========
RANK_EMOJI_OVERRIDES: Dict[str, Union[str, int]] = {
    # "phantom": "dl_phantom",
    # "ascendant": 123456789012345678,
    # "ubk": "ubk_emoji_name_or_id",  # optional
}
UNKNOWN_FALLBACK_EMOJI = "❓"
# ======================================

# =========================
#   Hilfsfunktionen
# =========================

def _find_custom_emoji(guild: discord.Guild, key: Union[str, int]) -> Optional[Union[discord.Emoji, discord.PartialEmoji]]:
    """Findet ein Custom-Emoji per Name/ID."""
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


def get_rank_emoji(guild: Optional[discord.Guild], rank_key: str) -> Optional[Union[discord.Emoji, discord.PartialEmoji, str]]:
    """Emoji für Rang: Override → Suche → None (außer 'ubk' -> ❓)."""
    if guild is None:
        return UNKNOWN_FALLBACK_EMOJI if rank_key == "ubk" else None
    if rank_key in RANK_EMOJI_OVERRIDES:
        e = _find_custom_emoji(guild, RANK_EMOJI_OVERRIDES[rank_key])
        if e:
            return e
    e2 = _find_custom_emoji(guild, rank_key)
    if e2:
        return e2
    if rank_key == "ubk":
        return UNKNOWN_FALLBACK_EMOJI
    return None


async def remove_all_rank_roles(member: discord.Member, guild: discord.Guild):
    """Entfernt ggf. vorhandene Deadlock-Rangrollen (UBK wird NICHT entfernt)."""
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


def _safe_role_name(guild: Optional[discord.Guild], role_id: int, fallback: str) -> str:
    """Liefert einen stabilen, DM-tauglichen Rollen-Namen (kein Mention)."""
    if guild:
        r = guild.get_role(role_id)
        if r:
            return r.name
    return fallback


# =========================
#        BASE VIEW
# =========================

class StepView(discord.ui.View):
    """
    Basisklasse für eine Frage mit "Weiter"/"Ne danke".
    Persistenz:
      • timeout=None
      • Buttons/Selects haben feste custom_id
      • View wird in cog_load() global registriert (bot.add_view)
    """
    def __init__(self):
        # timeout=None = persistent
        super().__init__(timeout=None)
        self.proceed: bool = False  # nur bei flow-instanz relevant

    # Hilfsfunktion: Guild/Member aus der MAIN_GUILD holen (bei DMs hat Interaction keine Guild)
    @staticmethod
    def _get_guild_and_member(inter: discord.Interaction) -> tuple[Optional[discord.Guild], Optional[discord.Member]]:
        guild = inter.client.get_guild(MAIN_GUILD_ID)  # type: ignore
        if guild is None:
            return None, None
        m = guild.get_member(inter.user.id)
        return guild, m

    async def _finish(self, interaction: discord.Interaction):
        # Buttons deaktivieren, Nachricht aktualisieren & löschen (nur wenn das die "laufende" Flow-Instanz ist)
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
    """Frage 1: Custom Games (Toggle-Buttons + Weiter/Ne danke).
       Persistente Buttons: wdm:q1:*
    """
    def __init__(self):
        super().__init__()
        # Auswahl-State (nur für Gating der "Weiter"-Taste relevant)
        self.sel_funny: bool = False
        self.sel_grind: bool = False
        # "Weiter" anfänglich sperren
        self._set_next_enabled(False)

    def _set_next_enabled(self, enabled: bool):
        for c in self.children:
            if isinstance(c, discord.ui.Button) and c.custom_id == "wdm:q1:next":
                c.disabled = not enabled

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

        # Gating aktualisieren: Weiter nur, wenn mind. 1 Auswahl getätigt wurde
        self._set_next_enabled(self.sel_funny or self.sel_grind)

        # Button-UI im DM aktualisieren
        if not interaction.response.is_done():
            await interaction.response.edit_message(view=self)
        else:
            try:
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
        # „Ne danke“ beendet den Schritt unmittelbar (klassischer UX-Pfad)
        await self._finish(interaction)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="wdm:q1:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Ist per Gating nur klickbar, wenn mind. 1 Option gewählt wurde
        await self._finish(interaction)


class PatchnotesView(StepView):
    """Frage 2: Patchnotes (Toggle + Weiter/Ne danke).  Persistente Buttons: wdm:q2:*"""
    def __init__(self):
        super().__init__()
        self.patch_selected: bool = False
        self._set_next_enabled(False)

    def _set_next_enabled(self, enabled: bool):
        for c in self.children:
            if isinstance(c, discord.ui.Button) and c.custom_id == "wdm:q2:next":
                c.disabled = not enabled

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

        # „Weiter“ nur nach einer Entscheidung (hier: aktives Patchnotes-Opt-In)
        self._set_next_enabled(self.patch_selected)

        if not interaction.response.is_done():
            await interaction.response.edit_message(view=self)
        else:
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="Ne danke", style=discord.ButtonStyle.danger, custom_id="wdm:q2:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="wdm:q2:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._finish(interaction)


class RankSelectDropdown(discord.ui.Select):
    """Frage 3: Rang-Auswahl (Dropdown mit Server-Emojis, persistent custom_id='wdm:q3:rank')."""

    def __init__(self, guild_for_emojis: Optional[discord.Guild] = None):
        # UBK ist Pflicht-Fallback
        ranks = [
            "ubk", "initiate", "seeker", "alchemist", "arcanist", "ritualist",
            "emissary", "archon", "oracle", "phantom", "ascendant", "eternus"
        ]

        options: list[discord.SelectOption] = []
        for r in ranks:
            label = "UBK" if r == "ubk" else r.capitalize()
            desc  = f"{label} auswählen"
            emoji = get_rank_emoji(guild_for_emojis, r)
            if emoji is not None:
                options.append(discord.SelectOption(label=label, value=r, description=desc, emoji=emoji))
            else:
                options.append(discord.SelectOption(label=label, value=r, description=desc))

        super().__init__(
            placeholder="🎮 Wähle deinen Deadlock-Rang… (UBK falls unsicher/kein Rang)",
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

        # Helper: Select sperren + Placeholder setzen + „Weiter“ aktivieren
        async def _lock_select_and_enable_next(placeholder_text: str):
            try:
                # „Weiter“ am Parent-View aktivieren
                parent_view = self.view
                if isinstance(parent_view, RankView):
                    parent_view._set_next_enabled(True)
                self.placeholder = placeholder_text
                self.disabled = True
                if not interaction.response.is_done():
                    await interaction.response.edit_message(view=self.view)
                else:
                    await interaction.message.edit(view=self.view)
            except Exception:
                pass

        # Rang setzen
        role_name = "UBK" if selected == "ubk" else selected.capitalize()
        try:
            await remove_all_rank_roles(member, guild)

            if selected == "ubk":
                role = guild.get_role(UBK_ROLE_ID)
                if role is None:
                    # Fallback: per Name
                    role = discord.utils.get(guild.roles, name="UBK")
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

        # UI sperren & Weiter freischalten
        placeholder = f"✅ Rang: {role_name}"
        await _lock_select_and_enable_next(placeholder)

        # Optional: Phantom+ Hinweis
        if selected in {"phantom", "ascendant", "eternus"}:
            ch = guild.get_channel(PHANTOM_NOTIFICATION_CHANNEL_ID)
            if ch:
                embed = discord.Embed(
                    title="🔥 Phantom+ Rang Update",
                    description=f"**{member.display_name}** hat sich den Rang **{role_name}** gesetzt!",
                    color=0xFF6B35,
                    timestamp=datetime.now()
                )
                try:
                    await ch.send(embed=embed)
                except Exception:
                    pass


class RankView(StepView):
    def __init__(self, guild_for_emojis: Optional[discord.Guild] = None):
        super().__init__()
        self.add_item(RankSelectDropdown(guild_for_emojis))
        # „Weiter“ anfänglich sperren
        self._set_next_enabled(False)

    def _set_next_enabled(self, enabled: bool):
        for c in self.children:
            if isinstance(c, discord.ui.Button) and c.custom_id == "wdm:q3:next":
                c.disabled = not enabled

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="wdm:q3:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Ist per Gating nur klickbar, wenn ein Rang gewählt wurde (inkl. UBK)
        await self._finish(interaction)


class RulesView(StepView):
    """Frage 4: Regelwerk bestätigen + Abschluss-Rolle setzen (persistenter Button: wdm:q4:confirm)."""

    @staticmethod
    async def _delete_later(msg: discord.Message, seconds: int):
        await asyncio.sleep(seconds)
        try:
            await msg.delete()
        except Exception:
            pass

    @discord.ui.button(label="Habe verstanden :)", style=discord.ButtonStyle.success, custom_id="wdm:q4:confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, member = self._get_guild_and_member(interaction)
        if guild and member:
            try:
                role = guild.get_role(ONBOARD_COMPLETE_ROLE_ID)
                if role:
                    await member.add_roles(role, reason="Welcome DM: Regeln bestätigt")
            except Exception as e:
                logger.warning(f"Could not add ONBOARD role to {member.id if member else 'unknown'}: {e}")

        # Danke-Nachricht separat & nach 5 Min löschen
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
    """Cog für Willkommens-DM (Embeds + **persistente** Components)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session_locks: Dict[int, asyncio.Lock] = {}  # pro-User Flow-Sperre

    def _get_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._session_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[user_id] = lock
        return lock

    async def cog_load(self):
        """
        WICHTIG: Persistente Views global registrieren.
        Diese Instanzen werden nur für die Interaction-Routing benötigt
        (die „Flow“-Instanzen werden beim Senden separat erzeugt).
        """
        self.bot.add_view(CustomGamesView())
        self.bot.add_view(PatchnotesView())
        self.bot.add_view(RankView(guild_for_emojis=None))
        self.bot.add_view(RulesView())

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Welcome DM System geladen (persistente Views aktiv)")

    async def _cleanup_old_bot_dms(self, member: discord.Member, limit: int = 50):
        """Optionales Aufräumen (löscht alte Bot-Nachrichten in der DM, damit keine alten Views stören)."""
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

    async def _send_step_embed(
        self,
        member: discord.Member,
        *,
        title: str,
        desc: str,
        step: int,
        total: int,
        view: StepView,
        color: int = 0x5865F2
    ) -> bool:
        """Sendet einen Embed + View und wartet, bis der Step abgeschlossen wurde."""
        emb = build_step_embed(title, desc, step, total, color=color)
        msg = await member.send(embed=emb, view=view)
        try:
            await view.wait()  # stop() wird nur in _finish() gerufen
        finally:
            # Nach Abschluss den Step wegräumen (die Buttons bleiben persistent, aber die Flow-Nachricht löschen wir)
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
                # Optional: alte Bot-DMs aufräumen
                await self._cleanup_old_bot_dms(member, limit=50)

                guild = self.bot.get_guild(MAIN_GUILD_ID)

                # Rollennamen DM-tauglich vorbereiten (statt Mentions)
                funny_name = _safe_role_name(guild, FUNNY_CUSTOM_ROLE_ID, "Funny Custom")
                grind_name = _safe_role_name(guild, GRIND_CUSTOM_ROLE_ID, "Grind Custom")

                # Begrüßung (wird am Ende entfernt)
                greet_msg = await member.send(
                    "👋 **Willkommen bei der Deutschen Deadlock Community!**\n\n"
                    "Diese DM hilft dir beim Start: Wir vergeben dir passende Rollen und zeigen dir die wichtigsten Infos."
                )

                # ---- Frage 1 ----
                # Beschreibung ohne Mentions & ohne „Weiter“-Hinweis
                q1_desc = (
                    "🎮 **Custom Games**\n\n"
                    "**Was sind Custom Games?**\n"
                    "Customs sind selbsterstellte Lobbys, die nichts mit dem normalen Matchmaking zu tun haben. "
                    "Hier legen wir eigene Regeln fest → Fokus auf Spaß, Lernen oder gemeinsames Training.\n\n"
                    "Dafür gibt es 2 Rollen:\n"
                    f"• **{funny_name}** → Für Fun & kreative Custom-Runden 🤪\n"
                    f"• **{grind_name}** → Für Scrims & ernsthafte Trainings 💪\n\n"
                    "➡ Über die Buttons kannst du dir die Rolle(n) selbst geben, wenn du mitmachen willst."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 1/4 · Lust auf Custom Games?",
                    desc=q1_desc,
                    step=1, total=4,
                    view=CustomGamesView(),
                    color=0x2ECC71  # grünlich
                ):
                    return False

                # ---- Frage 2 ----
                # Beschreibung ohne Hinweis-Satz
                q2_desc = (
                    "Möchtest du über neue **Patchnotes** informiert werden?\n"
                    "So verpasst du keine Balance-Änderungen oder neuen Content."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 2/4 · Patchnotes-Benachrichtigungen",
                    desc=q2_desc,
                    step=2, total=4,
                    view=PatchnotesView(),
                    color=0x3498DB  # blau
                ):
                    return False

                # ---- Frage 3 ----
                q3_desc = (
                    "Wähle deinen **Deadlock-Rang**.\n"
                    "Bist du neu/unsicher → **UBK**."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 3/4 · Rang auswählen (Pflicht)",
                    desc=q3_desc,
                    step=3, total=4,
                    view=RankView(guild_for_emojis=guild),
                    color=0x9B59B6  # lila
                ):
                    return False

                # ---- Frage 4 ----
                q4_desc = (
                    "📜 **Regelwerk – Das Wichtigste in Kürze**\n\n"
                    "✔ Respektvoller Umgang – keine Beleidigungen oder persönlichen Angriffe\n"
                    "✔ Null Toleranz bei Rassismus, Sexismus oder Hassrede\n"
                    "✔ Keine NSFW / expliziten Inhalte\n"
                    "✔ Privatsphäre respektieren – keine fremden Daten leaken\n"
                    "✔ Kein Spam / unnötige Pings\n"
                    "✔ Keine Fremdwerbung oder Schadsoftware\n\n"
                    "👉 Universalregel: **Sei kein Arschloch.**"
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
