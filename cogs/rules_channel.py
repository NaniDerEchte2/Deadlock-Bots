# cogs/rules_panel.py
import discord
from discord.ext import commands
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, Union

logger = logging.getLogger(__name__)

# ========= Server/Kanal/Rolle IDs =========
MAIN_GUILD_ID                   = 1289721245281292288
RULES_CHANNEL_ID                = 1315684135175716975

FUNNY_CUSTOM_ROLE_ID            = 1407085699374649364
GRIND_CUSTOM_ROLE_ID            = 1407086020331311144
PATCHNOTES_ROLE_ID              = 1330994309524357140
UBK_ROLE_ID                     = 1397687886580547745
ONBOARD_COMPLETE_ROLE_ID        = 1304216250649415771

THANK_YOU_DELETE_AFTER_SECONDS  = 300   # 5 Minuten
MIN_NEXT_SECONDS                = 5     # wie im DM-Flow

# ========= Emoji-Konfiguration =========
RANK_EMOJI_OVERRIDES: Dict[str, Union[str, int]] = {
    # "phantom": "dl_phantom",
    # "ascendant": 123456789012345678,
    # "ubk": "ubk_emoji_name_or_id",  # optional
}
UNKNOWN_FALLBACK_EMOJI = "❓"

# ========= Regelwerk-Text (ausgeklappt im Kanal) =========
RULES_TEXT = (
    "## Allgemeine Verhaltensregeln\n"
    "**Respektvoller Umgang:** Behandle alle Mitglieder mit Respekt. Keine Beleidigungen, Diskriminierung oder persönlichen Angriffe.\n"
    "**Keine Hassrede:** Rassismus, Sexismus oder Diskriminierung (Alter, Herkunft, Religion, Geschlecht, sexuelle Orientierung usw.) ist verboten.\n"
    "**Keine NSFW-Inhalte:** Keine unangemessenen/expiziten Inhalte – auch nicht in Profilbildern/Status.\n"
    "**Privatsphäre respektieren:** Keine fremden personenbezogenen Daten posten.\n"
    "**Kein Spam:** Keine übermäßigen Nachrichten, unnötige Pings oder irrelevante Inhalte.\n\n"
    "## Erlaubte Kommunikationsformen (im Spielkontext)\n"
    "- **Kompetitive Äußerungen** (situatives Trash Talking)\n"
    "- **Ironischer Sarkasmus**\n"
    "- **Humorvolle Übertreibungen**\n"
    "- **Provokative Wortspiele** (ohne böse Absicht)\n"
    "- **Taktisches Trolling** (im Scherz)\n"
    "- **Metakommunikation** zur Spielweise anderer (ohne persönliche Angriffe)\n"
    "- **Hyperbolische Kritik** (ohne Realitätsbezug)\n"
    "- **Kameradschaftliches Necken**\n\n"
    "Diese Äußerungen dienen der Unterhaltung und sind **nicht** als persönliche Angriffe zu verstehen – können aber ohne "
    "nonverbale Signale missverstanden werden. Also: **erst abchecken**, ob alle damit fein sind.\n\n"
    "## Zusätzliche Richtlinien\n"
    "- **Discord-Richtlinien** sind einzuhalten.\n"
    "- **Keine Werbung** ohne Nachfrage.\n"
    "- **Keine schädlichen Inhalte** (Viren, IP-Grabber etc.) → sofortiger, permanenter Bann.\n\n"
    "Denk daran: **Kritik geht ohne Beleidigungen.** Gerade wenn man sich nicht kennt, kann Ton/Lieschen schiefgehen.\n\n"
    "**Universalregel: Sei kein Arschloch 😄**\n\n"
    "## Moderation & Konfliktlösung\n"
    "- Probleme? Pingt **@Moderator** oder **@Owner**.\n"
    "- Konsequenzen je nach Schwere: Verwarnung, Timeout, Ban (ggf. ohne Vorwarnung).\n\n"
    "## So funktioniert unser Server\n"
    "• Mach dir eine Lane in **➕Casual Lane** – auch wenn du erst allein bist. Wer VC sieht, joint.\n"
    "• Nutzt die Voice-Kanäle aktiv – **das ist das Geheimnis** :)\n\n"
    "### 🔧 Patchnotes\n"
    "Wir posten regelmäßige **Deadlock Patchnotes (DE)** in **#patchnotes**.\n\n"
    "### 📚 Lern-Ressourcen\n"
    "Profi-Strategien, Tricks & Tipps: **#game-guides-und-tipps**.\n\n"
    "### 🔼 Elo pushen?\n"
    "Rang wählen und ab in **➕Rank Grind Lane** – Mates fürs Ranken finden.\n\n"
    "### 🎥 Mehr Content?\n"
    "Schau bei **#live-on-twitch** vorbei. Manchmal erlauben wir uns einen Spaß und ändern Nicknames – "
    "mit Humor nehmen; bei Bedarf einfach melden.\n\n"
    "### Beta-Zugang?\n"
    "Frag in **#beta-zugang** nach.\n\n"
    "Mit der Nutzung des Servers stimmst du dem **Regelwerk** zu.\n\n"
    "_Nani / EarlySalty • [DL] • 22.04.2025 & 01.09.2025_\n"
)

# ========= Utils =========

def build_embed(title: str, desc: str, *, footer: Optional[str] = None, color: int = 0x5865F2) -> discord.Embed:
    emb = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.now())
    if footer:
        emb.set_footer(text=footer)
    return emb

def _safe_role_name(guild: Optional[discord.Guild], role_id: int, fallback: str) -> str:
    if guild:
        r = guild.get_role(role_id)
        if r:
            return r.name
    return fallback

def _find_custom_emoji(guild: discord.Guild, key: Union[str, int]) -> Optional[Union[discord.Emoji, discord.PartialEmoji]]:
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
    ranks = {
        "initiate", "seeker", "alchemist", "arcanist", "ritualist",
        "emissary", "archon", "oracle", "phantom", "ascendant", "eternus"
    }
    to_remove = [r for r in member.roles if r.name.lower() in ranks]
    if to_remove:
        await member.remove_roles(*to_remove, reason="Rules Panel Rangauswahl")

# ========= Step-Views (Thread-basiert, wie DM-Flow) =========

class StepView(discord.ui.View):
    """Basisklasse für einen Step im Thread mit Mindestwartezeit."""
    def __init__(self):
        super().__init__(timeout=None)
        self.created_at: datetime = datetime.now()
        self.proceed: bool = False
        self.bound_message: Optional[discord.Message] = None

    @staticmethod
    def _get_guild_and_member(inter: discord.Interaction) -> tuple[Optional[discord.Guild], Optional[discord.Member]]:
        guild = inter.client.get_guild(MAIN_GUILD_ID)  # type: ignore
        if guild is None:
            return None, None
        m = guild.get_member(inter.user.id)
        return guild, m

    async def _enforce_min_wait(self, interaction: discord.Interaction, *, custom_txt: Optional[str] = None) -> bool:
        elapsed = (datetime.now() - self.created_at).total_seconds()
        remain = int(MIN_NEXT_SECONDS - elapsed)
        if remain > 0:
            txt = custom_txt or "⏳ Kurzer Moment… bitte noch kurz lesen. Du schaffst das. 💙"
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(txt, ephemeral=True)
                else:
                    await interaction.followup.send(txt, ephemeral=True)
            except Exception:
                pass
            return False
        return True

    def force_finish(self):
        self.proceed = True
        self.stop()

    async def _finish(self, interaction: discord.Interaction):
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
        self.force_finish()

# ---- Schritt 1: Status ----

STATUS_NEED_BETA   = "need_beta"
STATUS_PLAYING     = "already_playing"
STATUS_RETURNING   = "returning"
STATUS_NEW_PLAYER  = "new_player"

class PlayerStatusView(StepView):
    def __init__(self):
        super().__init__()
        self.choice: Optional[str] = None
        self._next_btn = discord.ui.Button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="rp:q1:next")
        self._next_btn.callback = self.next  # type: ignore
        self._next_btn.disabled = True
        self.add_item(self._next_btn)

    def _update_next(self, enabled: bool):
        self._next_btn.disabled = not enabled
        self._next_btn.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.primary
        self._next_btn.label = "Weiter ✅" if enabled else "Weiter"

    @discord.ui.select(
        placeholder="Bitte Status wählen …",
        min_values=1, max_values=1,
        options=[
            discord.SelectOption(label="Ich will spielen – brauche Beta-Invite", value=STATUS_NEED_BETA, emoji="🎟️"),
            discord.SelectOption(label="Ich spiele bereits", value=STATUS_PLAYING, emoji="✅"),
            discord.SelectOption(label="Ich fange gerade wieder an", value=STATUS_RETURNING, emoji="🔁"),
            discord.SelectOption(label="Neu im Game", value=STATUS_NEW_PLAYER, emoji="✨"),
        ],
        custom_id="rp:q1:status"
    )
    async def status_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.choice = select.values[0]
        label_map = {opt.value: opt.label for opt in select.options}
        select.placeholder = f"✅ Ausgewählt: {label_map.get(self.choice, '—')}"
        select.disabled = True
        self._update_next(True)
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except Exception:
            pass

    async def next(self, interaction: discord.Interaction):
        if not await self._enforce_min_wait(interaction):
            return
        await interaction.response.defer() if not interaction.response.is_done() else None
        if not self.choice:
            await interaction.followup.send("Bitte wähle zuerst eine Option.", ephemeral=True)
            return
        await self._finish(interaction)

# ---- Schritt 2: Customs ----

class CustomGamesView(StepView):
    def __init__(self):
        super().__init__()
        self.sel_funny = False
        self.sel_grind = False
        self._next_btn = discord.ui.Button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="rp:q2:next")
        self._next_btn.callback = self.next  # type: ignore
        self._next_btn.disabled = False  # Weiter auch ohne Rollen erlaubt
        self.add_item(self._next_btn)

    async def _toggle_role(self, interaction: discord.Interaction, role_id: int, button: discord.ui.Button, base_label: str):
        guild, member = self._get_guild_and_member(interaction)
        if not guild or not member:
            await interaction.response.send_message("❌ Konnte Guild/Member nicht bestimmen.", ephemeral=True)
            return

        role = guild.get_role(role_id)
        if not role:
            await interaction.response.send_message("❌ Rolle nicht gefunden (ID/Hierarchie prüfen).", ephemeral=True)
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Rules Panel Auswahl")
                button.style = discord.ButtonStyle.secondary
                button.label = base_label
                if role_id == FUNNY_CUSTOM_ROLE_ID:
                    self.sel_funny = False
                elif role_id == GRIND_CUSTOM_ROLE_ID:
                    self.sel_grind = False
            else:
                await member.add_roles(role, reason="Rules Panel Auswahl")
                button.style = discord.ButtonStyle.success
                button.label = f"✔ {base_label}"
                if role_id == FUNNY_CUSTOM_ROLE_ID:
                    self.sel_funny = True
                elif role_id == GRIND_CUSTOM_ROLE_ID:
                    self.sel_grind = True
        except discord.Forbidden:
            await interaction.response.send_message("❌ Rechte fehlen (Manage Roles / Rollenhierarchie).", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"[Custom Toggle] {member.id}: {e}")
            await interaction.response.send_message("⚠️ Fehler beim Rollenwechsel.", ephemeral=True)
            return

        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="Funny Custom", style=discord.ButtonStyle.secondary, custom_id="rp:q2:funny")
    async def funny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_role(interaction, FUNNY_CUSTOM_ROLE_ID, button, "Funny Custom")

    @discord.ui.button(label="Grind Custom", style=discord.ButtonStyle.secondary, custom_id="rp:q2:grind")
    async def grind(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._toggle_role(interaction, GRIND_CUSTOM_ROLE_ID, button, "Grind Custom")

    async def next(self, interaction: discord.Interaction):
        if not await self._enforce_min_wait(interaction):
            return
        await interaction.response.defer() if not interaction.response.is_done() else None
        await self._finish(interaction)

# ---- Schritt 3: Patchnotes ----

class PatchnotesView(StepView):
    def __init__(self):
        super().__init__()
        self._next_btn = discord.ui.Button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="rp:q3:next")
        self._next_btn.callback = self.next  # type: ignore
        self.add_item(self._next_btn)

    @discord.ui.button(label="Patchnotes", style=discord.ButtonStyle.secondary, custom_id="rp:q3:patch")
    async def toggle_patch(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, member = self._get_guild_and_member(interaction)
        if not guild or not member:
            await interaction.response.send_message("❌ Konnte Guild/Member nicht bestimmen.", ephemeral=True)
            return

        role = guild.get_role(PATCHNOTES_ROLE_ID)
        if not role:
            await interaction.response.send_message("❌ Rolle nicht gefunden (ID/Hierarchie prüfen).", ephemeral=True)
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Rules Panel Auswahl")
                button.style = discord.ButtonStyle.secondary
                button.label = "Patchnotes"
            else:
                await member.add_roles(role, reason="Rules Panel Auswahl")
                button.style = discord.ButtonStyle.success
                button.label = "✔ Patchnotes"
        except discord.Forbidden:
            await interaction.response.send_message("❌ Rechte fehlen (Manage Roles / Rollenhierarchie).", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"[Patchnotes Toggle] {member.id}: {e}")
            await interaction.response.send_message("⚠️ Fehler beim Rollenwechsel.", ephemeral=True)
            return

        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except Exception:
            pass

    async def next(self, interaction: discord.Interaction):
        if not await self._enforce_min_wait(interaction):
            return
        await interaction.response.defer() if not interaction.response.is_done() else None
        await self._finish(interaction)

# ---- Schritt 4: Rang ----

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
            custom_id="rp:q4:rank"
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.client.get_guild(MAIN_GUILD_ID)  # type: ignore
        if guild is None:
            await interaction.response.send_message("❌ Konnte Guild nicht bestimmen.", ephemeral=True)
            return
        member = guild.get_member(interaction.user.id)
        if member is None:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except Exception:
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
                    role = await guild.create_role(name="UBK", reason="Rules Panel Rangauswahl (Fallback)")
            else:
                role = discord.utils.get(guild.roles, name=role_name)
                if not role:
                    role = await guild.create_role(name=role_name, reason="Rules Panel Rangauswahl")
            await member.add_roles(role, reason="Rules Panel Rangauswahl")
        except discord.Forbidden:
            await interaction.response.send_message("❌ Rechte fehlen, um Rangrollen zu setzen.", ephemeral=True)
            return
        except Exception as e:
            logger.error(f"[Rank Select] {member.id}: {e}")
            await interaction.response.send_message("⚠️ Fehler beim Rangsetzen.", ephemeral=True)
            return

        if isinstance(self.parent_view, RankView):
            self.parent_view._enable_next(True)

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
    def __init__(self, on_confirm_coro, parent_view: "RankView"):
        super().__init__()
        self.on_confirm_coro = on_confirm_coro  # coroutine to call on confirm
        self.parent_view = parent_view          # Referenz auf das Rang-View

    @discord.ui.button(label="Sicher 👍", style=discord.ButtonStyle.success, custom_id="rp:q4:confirm_yes")
    async def confirm_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1) sofort defern, damit die Interaktion nicht ausläuft
        try:
            await interaction.response.defer() if not interaction.response.is_done() else None
        except Exception:
            pass

        # 2) Eltern-View (Frage 4) sauber schließen: Message löschen + waiter freigeben
        pv = self.parent_view
        try:
            if pv.bound_message:
                await pv.bound_message.delete()
        except Exception:
            pass
        pv.force_finish()  # send_step_embed_thread kann weiterlaufen

        # 3) Danach den Folge-Schritt ausführen (Regelwerk o.ä.)
        try:
            await self.on_confirm_coro()
        except Exception as e:
            logger.error(f"on_confirm_coro error: {e}")

        # 4) Dieses Bestätigungs-Panel aufräumen (eigene Nachricht)
        await self._finish(interaction)

    @discord.ui.button(label="Nochmal ändern", style=discord.ButtonStyle.secondary, custom_id="rp:q4:confirm_change")
    async def confirm_change(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Interaktion bestätigen
        try:
            await interaction.response.defer() if not interaction.response.is_done() else None
        except Exception:
            pass

        # Eltern-View (Frage 4) wieder aktivieren
        pv = self.parent_view
        try:
            pv.selected_key = None
            pv._enable_next(False)                # "Weiter" wieder deaktivieren
            pv.dropdown.disabled = False          # Dropdown wieder freischalten
            pv.dropdown.placeholder = "🎮 Wähle deinen *aktuellen* Deadlock-Rang …"
            if pv.bound_message:
                await pv.bound_message.edit(view=pv)
        except Exception as e:
            logger.warning(f"Could not re-enable rank view: {e}")

        # Bestätigungs-Nachricht entfernen
        try:
            await interaction.message.delete()
        except Exception:
            pass
        # Parent-View bleibt offen (kein finish), Nutzer kann neu wählen

class RankView(StepView):
    def __init__(self, guild_for_emojis: Optional[discord.Guild] = None, *, proceed_callback=None):
        super().__init__()
        self.dropdown = RankSelectDropdown(guild_for_emojis, parent_view=self)
        self.add_item(self.dropdown)
        self.selected_key: Optional[str] = None
        self._next_btn = discord.ui.Button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="rp:q4:next")
        self._next_btn.callback = self.next  # type: ignore
        self._next_btn.disabled = True
        self.add_item(self._next_btn)
        self._proceed_callback = proceed_callback  # called after rank confirmed

    def _enable_next(self, enabled: bool):
        self._next_btn.disabled = not enabled
        self._next_btn.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.primary
        self._next_btn.label = "Weiter ✅" if enabled else "Weiter"

    async def next(self, interaction: discord.Interaction):
        if not await self._enforce_min_wait(interaction):
            return
        try:
            await interaction.response.defer() if not interaction.response.is_done() else None
        except Exception:
            pass

        # UBK -> direkt Abschluss (Regeln bestätigen) – erst Frage 4 schließen, dann Regelwerk senden
        if self.selected_key == "ubk":
            await self._finish(interaction)  # Frage-4-Message schließen & waiter freigeben
            if self._proceed_callback:
                await self._proceed_callback()
            return

        # Peak-Check (separates Prompt im selben Thread)
        bait = (
            "👀 **Na? Sicher, dass das dein *AKTUELLER* Rang ist – nicht Peak/Max?**\n"
            "Wenn ja → **Sicher 👍**. Ansonsten bitte nochmal ändern. 💙"
        )
        emb = build_embed("Kurz checken", bait, color=0xB794F4)

        async def on_confirm():
            if self._proceed_callback:
                await self._proceed_callback()

        view = ConfirmRankView(on_confirm, parent_view=self)
        msg = await interaction.channel.send(embed=emb, view=view)  # type: ignore
        view.bound_message = msg

# ---- Abschluss: Regeln bestätigen ----

class RulesConfirmView(StepView):
    @staticmethod
    async def _delete_later(msg: discord.Message, seconds: int):
        await asyncio.sleep(seconds)
        try:
            await msg.delete()
        except Exception:
            pass

    @discord.ui.button(label="Habe verstanden :)", style=discord.ButtonStyle.success, custom_id="rp:qX:confirm_rules")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return
        await interaction.response.defer() if not interaction.response.is_done() else None

        guild, member = self._get_guild_and_member(interaction)
        if guild and member:
            try:
                role = guild.get_role(ONBOARD_COMPLETE_ROLE_ID)
                if role:
                    await member.add_roles(role, reason="Rules Panel: Regeln bestätigt")
            except Exception as e:
                logger.warning(f"Could not add ONBOARD role to {member.id if member else 'unknown'}: {e}")

        try:
            thanks = await interaction.channel.send("✅ Danke! Willkommen an Bord!")
            asyncio.create_task(self._delete_later(thanks, THANK_YOU_DELETE_AFTER_SECONDS))
        except Exception:
            pass

        await self._finish(interaction)

# ========= Flow-Helfer (Thread) =========

async def send_step_embed_thread(
    thread: discord.Thread,
    *,
    title: str,
    desc: str,
    step: int,
    total: int,
    view: StepView,
    color: int = 0x5865F2
) -> bool:
    emb = build_embed(title, desc, footer=f"Schritt {step} von {total} • Deadlock DACH", color=color)
    msg = await thread.send(embed=emb, view=view)
    view.bound_message = msg
    try:
        await view.wait()
    finally:
        try:
            await msg.delete()
        except Exception:
            pass
    return view.proceed

async def send_rules_confirm_in_thread(thread: discord.Thread):
    desc = (
        "📜 **Regelwerk – Das Wichtigste in Kürze**\n\n"
        "✔ Respektvoller Umgang – keine Beleidigungen/persönlichen Angriffe\n"
        "✔ Null Toleranz bei Rassismus, Sexismus oder Hassrede\n"
        "✔ Keine NSFW / expliziten Inhalte\n"
        "✔ Privatsphäre respektieren – keine fremden Daten leaken\n"
        "✔ Kein Spam / unnötige Pings\n"
        "✔ Keine Fremdwerbung oder Schadsoftware\n\n"
        "👉 Universalregel: **Sei kein Arschloch.**"
    )
    emb = build_embed("Abschluss · Regelwerk bestätigen", desc, footer="Deadlock DACH", color=0xE67E22)
    view = RulesConfirmView()
    msg = await thread.send(embed=emb, view=view)
    view.bound_message = msg

# ========= Panel-View im Regelkanal =========

class RulesPanelView(discord.ui.View):
    """
    Öffentliche, persistente View im Regelkanal.
    - Zeigt das Regelwerk „ausgeklappt“.
    - Button „Weiter ➜“: erstellt privaten Thread für den Nutzer und startet 1/4–4/4 Onboarding.
    """
    def __init__(self, cog: "RulesPanel"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Weiter ➜", style=discord.ButtonStyle.primary, custom_id="rp:panel:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.start_user_thread_flow(interaction)

# ========= Start-Here View für Join-Threads =========

class StartHereView(discord.ui.View):
    """Button im Join-Thread, um das Onboarding hier im Thread zu starten."""
    def __init__(self, cog: "RulesPanel"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Onboarding hier starten", style=discord.ButtonStyle.primary, custom_id="rp:join:start_here")
    async def start_here(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Sicherstellen, dass wir im Thread sind
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Bitte im Thread klicken.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.cog._run_flow_in_thread(interaction.channel, interaction.user)  # type: ignore

# ========= Cog =========

class RulesPanel(commands.Cog):
    """Interaktives Regelwerk-Panel: Kanal offen, Onboarding nutzerspezifisch in privatem Thread (1/4…4/4)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._locks: Dict[int, asyncio.Lock] = {}          # pro User
        self._user_threads: Dict[int, int] = {}            # user_id -> thread_id

    def _get_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def cog_load(self):
        # persistente Panel-View registrieren
        self.bot.add_view(RulesPanelView(self))
        self.bot.add_view(StartHereView(self))

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Rules Panel geladen (Panel-View aktiv)")

    @commands.command(name="publish_rules_panel")
    @commands.has_permissions(administrator=True)
    async def publish_rules_panel(self, ctx: commands.Context):
        """Postet das ausgeklappte Regelwerk + Weiter-Button in den Regelkanal."""
        guild = self.bot.get_guild(MAIN_GUILD_ID)
        if guild is None:
            await ctx.reply("❌ MAIN_GUILD_ID ungültig oder Bot nicht auf der Guild.")
            return

        channel = self.bot.get_channel(RULES_CHANNEL_ID)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await ctx.reply("❌ RULES_CHANNEL_ID zeigt nicht auf einen Textkanal.")
            return

        try:
            title = "📜 Regelwerk • Deadlock DACH"
            emb = build_embed(title, RULES_TEXT, footer="Regelwerk • Deadlock DACH", color=0x00AEEF)
            await channel.send(embed=emb, view=RulesPanelView(self))
            await ctx.reply("✅ Regelwerk-Panel veröffentlicht.")
        except discord.Forbidden:
            await ctx.reply("❌ Keine Berechtigung, im Regelkanal zu schreiben.")
        except Exception as e:
            logger.error(f"publish_rules_panel failed: {e}")
            await ctx.reply("⚠️ Unerwarteter Fehler beim Veröffentlichen.")

    # ======== Auto-Ping beim Server-Join ========

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        # Privaten Thread im Regelkanal anlegen und Nutzer dort pingen
        guild = self.bot.get_guild(MAIN_GUILD_ID)
        if not guild or member.guild.id != guild.id:
            return

        rules_channel = guild.get_channel(RULES_CHANNEL_ID)
        if not isinstance(rules_channel, discord.TextChannel):
            return

        try:
            name = f"welcome-{member.name}".replace(" ", "-")[:90]
            thread = await rules_channel.create_thread(
                name=name,
                type=discord.ChannelType.private_thread,
                invitable=True,
                auto_archive_duration=60
            )
            await thread.add_user(member)
        except discord.Forbidden:
            # Fallback auf öffentlichen Thread (immer noch besser als nichts)
            thread = await rules_channel.create_thread(
                name=name,
                type=discord.ChannelType.public_thread,
                auto_archive_duration=60
            )
        except Exception as e:
            logger.error(f"Join thread creation failed for {member.id}: {e}")
            return

        self._user_threads[member.id] = thread.id

        try:
            msg = (
                f"{member.mention} Willkommen! 👋\n\n"
                "➡ **Option A:** Antworte auf die **DM** vom **Deadlock Master Bot**.\n"
                "➡ **Option B:** **Starte das Onboarding direkt hier** im Thread:"
            )
            await thread.send(msg, view=StartHereView(self))
        except Exception as e:
            logger.error(f"Failed to send join ping for {member.id}: {e}")

    # ======== Flow-Start (pro User privater Thread) ========

    async def start_user_thread_flow(self, interaction: discord.Interaction):
        """Erstellt/öffnet einen privaten Thread und führt 1/4…4/4 dort aus."""
        user = interaction.user
        lock = self._get_lock(user.id)

        async with lock:
            # bestehenden Thread nutzen?
            thread = None
            thread_id = self._user_threads.get(user.id)
            if thread_id and interaction.guild:
                thread = interaction.guild.get_thread(thread_id)

            # neuen privaten Thread erstellen, wenn nötig
            if thread is None:
                rules_channel = interaction.guild.get_channel(RULES_CHANNEL_ID) if interaction.guild else None  # type: ignore
                if not isinstance(rules_channel, discord.TextChannel):
                    await interaction.response.send_message("❌ Regelkanal nicht gefunden/kein Textkanal.", ephemeral=True)
                    return

                name = f"onboarding-{user.name}".replace(" ", "-")[:90]
                try:
                    thread = await rules_channel.create_thread(
                        name=name,
                        type=discord.ChannelType.private_thread,
                        invitable=True,
                        auto_archive_duration=60
                    )
                    await thread.add_user(user)
                except discord.Forbidden:
                    thread = await rules_channel.create_thread(
                        name=name,
                        type=discord.ChannelType.public_thread,
                        auto_archive_duration=60
                    )
                    await interaction.response.send_message(
                        "⚠️ Konnte keinen **privaten** Thread erstellen. Starte im **öffentlichen** Thread. "
                        "Mods: bitte **Create Private Threads** erlauben.",
                        ephemeral=True
                    )
                except Exception as e:
                    await interaction.response.send_message("❌ Konnte keinen Thread erstellen.", ephemeral=True)
                    logger.error(f"Thread creation failed for {user.id}: {e}")
                    return

                self._user_threads[user.id] = thread.id

            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(f"🧵 Onboarding in {thread.mention} gestartet.", ephemeral=True)
                else:
                    await interaction.followup.send(f"🧵 Onboarding in {thread.mention} gestartet.", ephemeral=True)
            except Exception:
                pass

            await self._run_flow_in_thread(thread, user)

    async def _run_flow_in_thread(self, thread: discord.Thread, user: discord.User):
        guild = self.bot.get_guild(MAIN_GUILD_ID)

        # Schritt 1/4 – Status
        status_view = PlayerStatusView()
        ok = await send_step_embed_thread(
            thread,
            title="Frage 1/4 · Spielst du schon Deadlock – oder wieder?",
            desc="Sag mir kurz, wo du stehst – dann passe ich alles besser für dich an.",
            step=1, total=4,
            view=status_view,
            color=0x95A5A6
        )
        if not ok:
            return
        status_choice = status_view.choice or STATUS_PLAYING

        # Schritt 2/4 – Customs
        funny_name = _safe_role_name(guild, FUNNY_CUSTOM_ROLE_ID, "Funny Custom")
        grind_name = _safe_role_name(guild, GRIND_CUSTOM_ROLE_ID, "Grind Custom")
        q2_desc = (
            "🎮 **Custom Games**\n\n"
            "**Was sind Custom Games?** Selbsterstellte Lobbys außerhalb des Matchmakings. "
            "Eigene Regeln → Spaß / Lernen / Training.\n\n"
            "Rollen:\n"
            f"• **{funny_name}** → Fun & kreative Runden 🤪\n"
            f"• **{grind_name}** → Scrims & ernsthafte Trainings 💪\n\n"
            "➡ Über die Buttons kannst du dir die Rolle(n) selbst geben."
        )
        ok = await send_step_embed_thread(
            thread,
            title="Frage 2/4 · Lust auf Custom Games?",
            desc=q2_desc,
            step=2, total=4,
            view=CustomGamesView(),
            color=0x2ECC71
        )
        if not ok:
            return

        # Schritt 3/4 – Patchnotes
        ok = await send_step_embed_thread(
            thread,
            title="Frage 3/4 · Patchnotes-Benachrichtigungen",
            desc="Möchtest du über neue **Patchnotes** informiert werden?\nSo verpasst du keine Balance-Änderungen oder neuen Content.",
            step=3, total=4,
            view=PatchnotesView(),
            color=0x3498DB
        )
        if not ok:
            return

        # Schritt 4/4 – Rang
        async def after_rank_confirm():
            await send_rules_confirm_in_thread(thread)

        rank_view = RankView(guild_for_emojis=guild, proceed_callback=after_rank_confirm)
        q4_desc = (
            "Bitte wähle hier deinen **AKTUELLEN RANG**\n"
            "**Kein MAX/PEAK**, kein „Weihnachten in Afrika“ – **dein jetziger Rang**. 😄\n"
            "____________________________\n"
            "**Rang unklar?** In Deadlock: **Esc → Profil** → neben **Sortieren nach: Spielzeit**.\n"
            "____________________________\n"
            "Wenn du **neu im Game** bist, wähle **„Neu im Game“**."
        )
        ok = await send_step_embed_thread(
            thread,
            title="Frage 4/4 · Rang auswählen (Pflicht)",
            desc=q4_desc,
            step=4, total=4,
            view=rank_view,
            color=0x9B59B6
        )
        if not ok:
            return

        # Abschluss-Hinweise je nach Status
        closing_lines = []
        if status_choice == STATUS_NEW_PLAYER:
            closing_lines.append(
                "✨ **Schön, dass du neu bist!** Frag die Community – die hilft gern. "
                "Für eine kleine Einführung schreib **@earlysalty** oder poste in **#allgemein**."
            )
        if status_choice == STATUS_NEED_BETA:
            closing_lines.append(
                "🎟️ **Beta-Invite benötigt?** Schau im Kanal **#beta-zugang** vorbei und poste deine **Steam-Freundschafts-ID** "
                "(Steam → Freunde → Freund hinzufügen). Annehmen: https://store.steampowered.com/account/playtestinvites"
            )
        if status_choice == STATUS_RETURNING:
            closing_lines.append("🔁 **Willkommen zurück!** Fürs Reinkommen: **Scrims/Grind-Runden** oder **Customs** nutzen.")
        if status_choice == STATUS_PLAYING:
            closing_lines.append("✅ **Viel Spaß!** Nutz **Customs**, **Patchnotes** & **Guides** – und ping uns, wenn du was brauchst.")

        if closing_lines:
            try:
                await thread.send("\n\n".join(closing_lines))
            except Exception:
                pass

# ========= Setup =========

async def setup(bot: commands.Bot):
    await bot.add_cog(RulesPanel(bot))
