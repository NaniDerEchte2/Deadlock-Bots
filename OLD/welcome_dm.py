# cogs/welcome_dm.py
import discord
from discord.ext import commands
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, Union
from cogs.steam_link_dm import send_steam_link_step


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

# Mindest-Lesezeit für alle "Weiter"- und "Ne danke"-Aktionen
MIN_NEXT_SECONDS = 5

# Status-Optionen (Frage 1)
STATUS_NEED_BETA   = "need_beta"
STATUS_PLAYING     = "already_playing"
STATUS_RETURNING   = "returning"
STATUS_NEW_PLAYER  = "new_player"

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


def build_step_embed(title: str, desc: str, step: Optional[int], total: int, color: int = 0x5865F2) -> discord.Embed:
    """Einheitlicher Embed pro Schritt; step=None zeigt 'Einführung'."""
    emb = discord.Embed(title=title, description=desc, color=color, timestamp=datetime.now())
    footer = "Einführung • Deadlock DACH" if step is None else f"Frage {step} von {total} • Deadlock DACH"
    emb.set_footer(text=footer)
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
    Basisklasse für einen Step mit "Weiter"/"Ne danke".
    Persistenz:
      • timeout=None
      • Buttons/Selects haben feste custom_id
      • View wird in cog_load() global registriert (bot.add_view)
    Plus:
      • Mindestwartezeit (MIN_NEXT_SECONDS) für *alle* Weiter-/Skip-Aktionen.
      • bound_message: Referenz auf die gesendete Nachricht (für spätere UI-Edits).
    """
    def __init__(self):
        super().__init__(timeout=None)
        self.proceed: bool = False
        self.created_at: datetime = datetime.now()
        self.bound_message: Optional[discord.Message] = None  # <— NEU

    @staticmethod
    def _get_guild_and_member(inter: discord.Interaction) -> tuple[Optional[discord.Guild], Optional[discord.Member]]:
        guild = inter.client.get_guild(MAIN_GUILD_ID)  # type: ignore
        if guild is None:
            return None, None
        m = guild.get_member(inter.user.id)
        return guild, m

    async def _enforce_min_wait(self, interaction: discord.Interaction, *, custom_txt: Optional[str] = None) -> bool:
        """Stellt sicher, dass MIN_NEXT_SECONDS vergangen sind. Keine Countdown-Texte."""
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
        """Externen Abschluss erlauben (z. B. von einer separaten Confirm-View aus)."""
        self.proceed = True
        self.stop()

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
        self.force_finish()


# =========================
#     INTRO (NEUE 1. MSG)
# =========================

class IntroView(StepView):
    """
    Informelle Begrüßung — der erste „Weiter“-Klick geht jetzt OHNE Cooldown direkt weiter.
    (Alle anderen Schritte behalten die Mindestwartezeit bei.)
    """
    def __init__(self):
        super().__init__()
        self.first_click_done: bool = False
        self.first_click_time: Optional[datetime] = None

    @discord.ui.button(label="Weiter ➜", style=discord.ButtonStyle.primary, custom_id="wdm:q0:intro_next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Direkt weiter – kein Anti-Skip, keine 5s Wartezeit beim ersten Klick.
        await self._finish(interaction)


# =========================
#    FRAGE 1: STATUS
# =========================

class PlayerStatusView(StepView):
    """„Spielst du schon Deadlock? Oder wieder?“ — Dropdown + Weiter."""
    def __init__(self):
        super().__init__()
        self.choice: Optional[str] = None
        self._set_next_enabled(False)

    def _set_next_enabled(self, enabled: bool):
        for c in self.children:
            if isinstance(c, discord.ui.Button) and c.custom_id == "wdm:qS:next":
                c.disabled = not enabled
                c.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.primary
                c.label = "Weiter ✅" if enabled else "Weiter"

    @discord.ui.select(
        placeholder="Bitte Status wählen …",
        min_values=1, max_values=1,
        options=[
            discord.SelectOption(label="Ich will spielen – brauche Beta-Invite", value=STATUS_NEED_BETA, emoji="🎟️"),
            discord.SelectOption(label="Ich spiele bereits", value=STATUS_PLAYING, emoji="✅"),
            discord.SelectOption(label="Ich fange gerade wieder an", value=STATUS_RETURNING, emoji="🔁"),
            discord.SelectOption(label="Neu im Game", value=STATUS_NEW_PLAYER, emoji="✨"),
        ],
        custom_id="wdm:qS:status"
    )
    async def status_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.choice = select.values[0]
        # „Abgeschlossen“-Gefühl: Select sperren + Placeholder setzen
        label_map = {opt.value: opt.label for opt in select.options}
        select.placeholder = f"✅ Ausgewählt: {label_map.get(self.choice, '—')}"
        select.disabled = True
        self._set_next_enabled(True)
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="wdm:qS:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return
        if not self.choice:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Bitte wähle zuerst eine Option.", ephemeral=True)
                else:
                    await interaction.followup.send("Bitte wähle zuerst eine Option.", ephemeral=True)
            except Exception:
                pass
            return
        await self._finish(interaction)


# =========================
#   FRAGE 2: CUSTOMS
# =========================

class CustomGamesView(StepView):
    """Custom Games (Toggle-Buttons + Weiter/Ne danke).  Persistente Buttons: wdm:q1:*"""
    def __init__(self):
        super().__init__()
        self.sel_funny: bool = False
        self.sel_grind: bool = False
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
        # Wartepflicht ohne Countdown/Anweisung
        if not await self._enforce_min_wait(interaction, custom_txt="👀 Sicher, dass du in so kurzer Zeit schon alles gelesen hast?"):
            return
        await self._finish(interaction)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="wdm:q1:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return
        await self._finish(interaction)


# =========================
#   FRAGE 3: PATCHNOTES
# =========================

class PatchnotesView(StepView):
    """Patchnotes (Toggle + Weiter/Ne danke).  Persistente Buttons: wdm:q2:*"""
    def __init__(self):
        super().__init__()
        self.patch_selected: bool = False
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
        except Exception:
            pass

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


# =========================
#   FRAGE 4: RANG
# =========================

class RankSelectDropdown(discord.ui.Select):
    """Rang-Auswahl (Dropdown mit Server-Emojis, persistent custom_id='wdm:q3:rank')."""

    def __init__(self, guild_for_emojis: Optional[discord.Guild] = None, parent_view: Optional["RankView"] = None):
        self.parent_view = parent_view
        # UBK ist Pflicht-Fallback — Anzeige: "Neu im Game"
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
        # ---> Ausgewählte Option auch im Parent speichern (für UBK-Bypass)
        if isinstance(self.parent_view, RankView):
            self.parent_view.selected_key = selected  # <— NEU

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

        # Rank-Weiter aktivieren + Grün/Haken
        if isinstance(self.parent_view, RankView):
            self.parent_view._set_next_enabled(True)

        # „Abgeschlossen“-Gefühl im Dropdown selbst
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
    """Separater Bestätigungs-Prompt für den 'ertappt'-Moment (eigene Nachricht)."""
    def __init__(self, parent_rank_view: "RankView"):
        super().__init__()
        self.parent_rank_view = parent_rank_view

    @discord.ui.button(label="Sicher 👍", style=discord.ButtonStyle.success, custom_id="wdm:q3:confirm_yes")
    async def confirm_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Schließe den eigentlichen Step (RankView) „extern“
        self.parent_rank_view.force_finish()
        # Entferne diese Confirm-Nachricht
        await self._finish(interaction)

    @discord.ui.button(label="Nochmal ändern", style=discord.ButtonStyle.secondary, custom_id="wdm:q3:confirm_change")
    async def confirm_change(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Dropdown im Parent wieder freigeben + Placeholder zurücksetzen + Weiter deaktivieren
        pv = self.parent_rank_view
        try:
            pv.dropdown.disabled = False
            pv.dropdown.placeholder = "🎮 Wähle deinen *aktuellen* Deadlock-Rang …"
            pv._set_next_enabled(False)
            pv.selected_key = None  # <— Reset
            # Ursprüngliche Nachricht (mit Dropdown) aktualisieren
            if pv.bound_message:
                await pv.bound_message.edit(view=pv)
        except Exception:
            pass
        # Diese Bestätigungsnachricht schließen
        await self._finish(interaction)


class RankView(StepView):
    def __init__(self, guild_for_emojis: Optional[discord.Guild] = None):
        super().__init__()
        self.dropdown = RankSelectDropdown(guild_for_emojis, parent_view=self)
        self.add_item(self.dropdown)
        self._set_next_enabled(False)
        self.selected_key: Optional[str] = None  # <— NEU

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

        # ---- NEU: UBK (Neu im Game) überspringt die "ertappt"-Bestätigung
        if self.selected_key == "ubk":
            await self._finish(interaction)
            return

        # Beim Weiter-Klick: separaten „ertappt“-Prompt senden, Auswahl bleibt stehen
        bait = (
            "👀 **Na? Sicher, dass das dein *AKTUELLER* Rang ist – nicht dein Peak oder Max Rang?**\n"
            "Wenn ja → **Sicher 👍**. ansonsten bitte nochmal ändern**. 💙"
        )
        try:
            emb = discord.Embed(title="Kurz checken", description=bait, color=0xB794F4)
            await interaction.channel.send(embed=emb, view=ConfirmRankView(self))
        except Exception:
            pass

        # Den Weiter-Button hier sperren, um mehrfaches Spammen zu verhindern
        button.disabled = True
        try:
            if not interaction.response.is_done():
                await interaction.response.edit_message(view=self)
            else:
                await interaction.message.edit(view=self)
        except Exception:
            pass


# =========================
#   FRAGE 5: STEAM-LINK (NUDGE)
# =========================

class SteamLinkNudgeView(StepView):
    """
    Leichte Empfehlung (skippbar) + Button, der die eigentliche Steam-Link-View sendet.
    Persistente Buttons: wdm:q5:*
    """
    @discord.ui.button(label="Jetzt verknüpfen (empfohlen)", style=discord.ButtonStyle.success, custom_id="wdm:q5:linknow", emoji="🔗")
    async def link_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            # Öffnet die „3 Optionen“-View aus dem Steam-Link-DM-Modul (Discord/Steam/Manuell)
            await send_steam_link_step(interaction.client, interaction.user)  # type: ignore
            # Kein Finish – User kann danach „Weiter“ klicken, wenn gelesen.
            if not interaction.response.is_done():
                await interaction.response.send_message("📨 Link-Fenster geöffnet. Du kannst hier gleich **Weiter** klicken.", ephemeral=True)
            else:
                await interaction.followup.send("📨 Link-Fenster geöffnet. Du kannst hier gleich **Weiter** klicken.", ephemeral=True)
        except Exception:
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ Konnte die Verknüpfung gerade nicht öffnen. Probier später **/link**.", ephemeral=True)
            else:
                await interaction.followup.send("⚠️ Konnte die Verknüpfung gerade nicht öffnen. Probier später **/link**.", ephemeral=True)

    @discord.ui.button(label="Später", style=discord.ButtonStyle.secondary, custom_id="wdm:q5:skip", emoji="⏭️")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return
        await self._finish(interaction)

    @discord.ui.button(label="Weiter", style=discord.ButtonStyle.primary, custom_id="wdm:q5:next")
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return
        await self._finish(interaction)


# =========================
#   FRAGE 6: REGELN
# =========================

class RulesView(StepView):
    """Regelwerk bestätigen + Abschluss-Rolle setzen (persistenter Button: wdm:q4:confirm)."""

    @staticmethod
    async def _delete_later(msg: discord.Message, seconds: int):
        await asyncio.sleep(seconds)
        try:
            await msg.delete()
        except Exception:
            pass

    @discord.ui.button(label="Habe verstanden :)", style=discord.ButtonStyle.success, custom_id="wdm:q4:confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._enforce_min_wait(interaction):
            return

        guild, member = self._get_guild_and_member(interaction)
        if guild and member:
            try:
                role = guild.get_role(ONBOARD_COMPLETE_ROLE_ID)
                if role:
                    await member.add_roles(role, reason="Welcome DM: Regeln bestätigt")
            except Exception as e:
                logger.warning(f"Could not add ONBOARD role to {member.id if member else 'unknown'}: {e}")

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
        """Persistente Views global registrieren (für Interaction-Routing)."""
        self.bot.add_view(IntroView())
        self.bot.add_view(PlayerStatusView())
        self.bot.add_view(CustomGamesView())
        self.bot.add_view(PatchnotesView())
        self.bot.add_view(RankView(guild_for_emojis=None))
        self.bot.add_view(SteamLinkNudgeView())
        self.bot.add_view(RulesView())

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Welcome DM System geladen (persistente Views aktiv)")

    async def _cleanup_old_bot_dms(self, member: discord.Member, limit: int = 50):
        """Optionale Aufräumhilfe in DMs."""
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
        step: Optional[int],
        total: int,
        view: StepView,
        color: int = 0x5865F2
    ) -> bool:
        """Sendet einen Embed + View und wartet, bis der Step abgeschlossen wurde."""
        emb = build_step_embed(title, desc, step, total, color=color)
        msg = await member.send(embed=emb, view=view)
        # --- Bind message so nested views (e.g., Confirm) can edit original UI
        view.bound_message = msg  # <— NEU
        try:
            await view.wait()
        finally:
            try:
                await msg.delete()
            except Exception:
                pass
        return view.proceed

    async def send_welcome_messages(self, member: discord.Member):
        lock = self._get_lock(member.id)
        async with lock:
            greet_msg: Optional[discord.Message] = None
            try:
                await self._cleanup_old_bot_dms(member, limit=50)

                guild = self.bot.get_guild(MAIN_GUILD_ID)
                funny_name = _safe_role_name(guild, FUNNY_CUSTOM_ROLE_ID, "Funny Custom")
                grind_name = _safe_role_name(guild, GRIND_CUSTOM_ROLE_ID, "Grind Custom")

                # (0) Kurzer Begrüßungs-Trailer
                greet_msg = await member.send(
                    "👋 **Herzlich willkommen in der Deutschen Deadlock Community!**\n\n"
                    "Ich helfe dir jetzt, dein Spielerlebnis hier **bestmöglich** einzustellen. "
                    "Dazu brauche ich **kurz** deine Aufmerksamkeit. 💙\n\n"
                    "**:bangbang: __Ohne diese Schritte hast du keinen Zugriff auf den Server.__:bangbang: **"
                )

                # (0.5) Intro-Nachricht (jetzt ohne Anti-Skip beim ersten Klick)
                intro_desc = (
                    "Hey, schön dass du da bist! 🫶\n\n"
                    "Bitte nimm dir **2–3 Minuten** Zeit, die nächsten Fragen **in Ruhe** zu lesen "
                    "und zu verstehen, was ich von dir brauche. Ich bin dafür da, "
                    "dein Spielerlebnis auf dem Server **maximal angenehm** zu machen – "
                    "mit möglichst wenig Chaos und maximal viel **Liebe**. 💙\n\n"
                    "_Kleiner Tipp:_ Wer liest, bekommt die besseren Rollen. 😉"
                )
                if not await self._send_step_embed(
                    member,
                    title="Willkommen 💙",
                    desc=intro_desc,
                    step=None, total=6,
                    view=IntroView(),
                    color=0x00AEEF
                ):
                    return False

                # ---- Frage 1/6: Status
                status_view = PlayerStatusView()
                if not await self._send_step_embed(
                    member,
                    title="Frage 1/6 · Spielst du schon Deadlock – oder wieder?",
                    desc="Sag mir kurz, wo du stehst – dann passe ich alles besser für dich an.",
                    step=1, total=6,
                    view=status_view,
                    color=0x95A5A6
                ):
                    return False
                status_choice = status_view.choice or STATUS_PLAYING

                # ---- Frage 2/6: Custom Games
                q2_desc = (
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
                    title="Frage 2/6 · Lust auf Custom Games?",
                    desc=q2_desc,
                    step=2, total=6,
                    view=CustomGamesView(),
                    color=0x2ECC71
                ):
                    return False

                # ---- Frage 3/6: Patchnotes
                q3_desc = (
                    "Möchtest du über neue **Patchnotes** informiert werden?\n"
                    "So verpasst du keine Balance-Änderungen oder neuen Content."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 3/6 · Patchnotes-Benachrichtigungen",
                    desc=q3_desc,
                    step=3, total=6,
                    view=PatchnotesView(),
                    color=0x3498DB
                ):
                    return False

                # ---- Frage 4/6: Rang
                q4_desc = (
                    "Bitte wähle hier deinen **AKTUELLEN RANG**\n"
                    "**Kein MAX RANG, NICHT PEAK, auch NICHT WEIHNACHTEN IN AFRIKA**\n"
                    "SONDERN DEIN JETZIGER RANG**😄\n"
                    "____________________________\n"
                    "**Du weißt deinen Rang nicht oder findest ihn nicht?**\n"
                    "• Starte **Deadlock**\n"
                    "• Drücke **Esc** → **Profil**\n"
                    "• Unter dem **letzten Match**, neben **Sortieren nach: Spielzeit**, findest du deinen **Rang**\n"
                    f"• Oder schau hier aufs Bild: [Hier Klicken](https://media.discordapp.net/attachments/1330665839078146059/1412581096436269096/image.png?ex=68c20aa9&is=68c0b929&hm=d5faa19b0a50cd844950fde1222c74ccd5b071b1e9bd4a05a96f099f534e6d1f&=&format=webp&quality=lossless&width=1522&height=856)\n"
                    "**Deutsch/Englisch verwirrt?**\n"
                    "Vergleiche einfach **das Aussehen** der Abzeichen mit dem, was du im Dropdown siehst.\n\n"
                    "Wenn du **neu im Game** bist, wähle bitte **„Neu im Game“**."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 4/6 · Rang auswählen (Pflicht)",
                    desc=q4_desc,
                    step=4, total=6,
                    view=RankView(guild_for_emojis=guild),
                    color=0x9B59B6
                ):
                    return False

                # ---- Frage 5/6: Steam-Verknüpfung (Empfehlung)
                q5_desc = (
                    "**Empfehlung für besseres Erlebnis:**\n"
                    "• **Wozu ist das gut?** Wir können dadurch einen **exakten Voice-Status** "
                    "(z. B. *Lobby/In-Game*, **Anzahl im Match**) als Kanalbeschreibung bereitstellen.\n"
                    "• Zudem ermöglicht es **sauberere Orga & Balancing** bei Events.\n\n"
                    "**Wichtig:** In Steam → Profil → **Datenschutzeinstellungen** → "
                    "**Spieldetails = Öffentlich** (und **Gesamtspielzeit** nicht auf „immer privat“)."
                )
                if not await self._send_step_embed(
                    member,
                    title="Frage 5/6 · Steam verknüpfen (empfohlen, skippbar)",
                    desc=q5_desc,
                    step=5, total=6,
                    view=SteamLinkNudgeView(),
                    color=0x5865F2
                ):
                    return False

                # Zusätzlich: Öffne direkt die Link-View, falls der User „Jetzt verknüpfen“ gedrückt hat
                # (Die Nudge-View ruft send_steam_link_step bereits – hier kein weiterer Call nötig.)

                # ---- Frage 6/6: Regeln
                q6_desc = (
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
                    title="Frage 6/6 · Regelwerk bestätigen",
                    desc=q6_desc,
                    step=6, total=6,
                    view=RulesView(),
                    color=0xE67E22
                ):
                    return False

                # -------- Abschluss-Hinweise je nach Status --------
                closing_lines = []
                if status_choice == STATUS_NEW_PLAYER:
                    closing_lines.append(
                        "✨ **Schön, dass du neu bist!** Für alle Fragen rund um Deadlock frag liebend gern die Community – "
                        "die wartet nur darauf zu helfen. Wenn du eine **Einführung** ins Game (kleines Tutorial) möchtest, "
                        "schreib einfach **@earlysalty**. Oder poste in **#allgemein**: "
                        "_„Hey, ich bin neu und möchte das Spiel Schritt für Schritt entdecken.“_ 💙"
                    )
                if status_choice == STATUS_NEED_BETA:
                    closing_lines.append(
                        "🎟️ **Beta-Invite benötigt?** Super, dass du spielen willst! Deine Einladung bekommst du hier:\n"
                        "https://discord.com/channels/1289721245281292288/1410754840706945034\n\n"
                        "Bitte poste dort eine kurze Nachricht, z. B.:\n"
                        "```\n"
                        "Hey :)\n"
                        "wäre jemand so lieb und könnte mich für den Deadlock-Playtest einladen?\n"
                        "Meine Steam-Freundschafts-ID: 444500904\n"
                        "```\n"
                        "👉 Deine **Steam-Freundschafts-ID** findest du in Steam unter **Freunde → Freund hinzufügen**.\n"
                        "Nachdem dich jemand eingeladen hat, prüfe zum **Akzeptieren** hier:\n"
                        "<https://store.steampowered.com/account/playtestinvites>\n"
                        "_Das kann ein paar Stunden dauern – nicht wundern._"
                    )
                if status_choice == STATUS_RETURNING:
                    closing_lines.append("🔁 **Willkommen zurück!** Fürs Reinkommen frag gern nach **Scrims/Grind-Runden** oder schau bei **Customs** rein.")
                if status_choice == STATUS_PLAYING:
                    closing_lines.append("✅ **Viel Spaß!** Nutz **Customs**, **Patchnotes** & **Guides** – und ping uns, wenn du was brauchst.")

                if closing_lines:
                    try:
                        await member.send("\n\n".join(closing_lines))
                    except Exception:
                        pass

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
        ...
        await ctx.send(f"📤 Sende Welcome-DM an {user.mention} …")
        ok = await self.send_welcome_messages(user)
        await ctx.send("✅ Erfolgreich gesendet!" if ok else "⚠️ Senden fehlgeschlagen.")


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeDM(bot))
