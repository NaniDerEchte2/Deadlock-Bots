# cogs/rules_panel.py
from __future__ import annotations

import discord
from discord.ext import commands
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional

# ⚠️ wir nutzen den Steam-Schritt aus dem DM-Modul
from .welcome_dm.step_steam_link import SteamLinkNudgeView  # persistent view mit custom_ids

logger = logging.getLogger(__name__)

# ========= Server/Kanal/Rolle IDs =========
MAIN_GUILD_ID                   = 1289721245281292288
RULES_CHANNEL_ID                = 1315684135175716975
ONBOARD_COMPLETE_ROLE_ID        = 1304216250649415771

THANK_YOU_DELETE_AFTER_SECONDS  = 300   # 5 Minuten
MIN_NEXT_SECONDS                = 4     # wie im (alten) DM-Flow

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
    "LFG-Rollen & Sichtbarkeit stellst du im **Discord-Onboarding** ein.\n\n"
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

# ========= Step-Views (Thread-kompatibel) =========

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
            txt = custom_txt or "👀 Sicher, dass du in so kurzer Zeit schon alles gelesen hast?"
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(txt, ephemeral=True)
                else:
                    await interaction.followup.send(txt, ephemeral=True)
            except (discord.HTTPException, discord.NotFound):
                logger.debug("min-wait notify failed", exc_info=True)
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
        except (discord.HTTPException, discord.NotFound):
            logger.debug("finish edit failed", exc_info=True)
        try:
            await interaction.message.delete()
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            logger.debug("finish delete failed", exc_info=True)
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
        except (discord.HTTPException, discord.NotFound):
            logger.debug("status_select edit failed", exc_info=True)

    async def next(self, interaction: discord.Interaction):
        if not await self._enforce_min_wait(interaction):
            return
        await interaction.response.defer() if not interaction.response.is_done() else None
        if not self.choice:
            await interaction.followup.send("Bitte wähle zuerst eine Option.", ephemeral=True)
            return
        await self._finish(interaction)

# ---- Abschluss: Regeln bestätigen ----

class RulesConfirmView(StepView):
    @staticmethod
    async def _delete_later(msg: discord.Message, seconds: int):
        await asyncio.sleep(seconds)
        try:
            await msg.delete()
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            logger.debug("delete_later failed", exc_info=True)

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
            except Exception:
                logger.warning("Could not add ONBOARD role", exc_info=True)

        try:
            thanks = await interaction.channel.send("✅ Danke! Willkommen an Bord!")
            asyncio.create_task(self._delete_later(thanks, THANK_YOU_DELETE_AFTER_SECONDS))
        except Exception:
            logger.debug("send thanks failed", exc_info=True)

        await self._finish(interaction)

# ========= Flow-Helfer (Thread) =========

async def send_step_embed_thread(
    thread: discord.Thread,
    *,
    title: str,
    desc: str,
    step: int,
    total: int,
    view: discord.ui.View,   # bewusst generisch (kompatibel mit DM-Views)
    color: int = 0x5865F2
) -> bool:
    emb = build_embed(title, desc, footer=f"Schritt {step} von {total} • Deadlock DACH", color=color)
    msg = await thread.send(embed=emb, view=view)
    # falls View 'bound_message' kennt (unsere StepViews), setzen wir sie
    try:
        setattr(view, "bound_message", msg)  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        await view.wait()
    finally:
        try:
            await msg.delete()
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            logger.debug("cleanup step message failed", exc_info=True)
    # wenn die View ein 'proceed'-Flag hat, nutze das – sonst geht's weiter
    return bool(getattr(view, "proceed", True))

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
    - Button „Weiter ➜“: erstellt privaten Thread für den Nutzer und startet 1/3–3/3 Onboarding.
    """
    def __init__(self, cog: "RulesPanel"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Weiter ➜", style=discord.ButtonStyle.primary, custom_id="rp:panel:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.start_user_thread_flow(interaction)

# ========= Cog =========

class RulesPanel(commands.Cog):
    """Interaktives Regelwerk-Panel: Kanal offen, Onboarding nutzerspezifisch in privatem Thread (1/3…3/3)."""

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
        # persistente Panel-View + die im Flow verwendeten Views (für Reboots)
        self.bot.add_view(RulesPanelView(self))
        self.bot.add_view(PlayerStatusView())
        self.bot.add_view(SteamLinkNudgeView())
        self.bot.add_view(RulesConfirmView())

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

    # ======== Flow-Start (pro User privater Thread) ========

    async def start_user_thread_flow(self, interaction: discord.Interaction):
        """Erstellt/öffnet einen privaten Thread und führt 1/3…3/3 dort aus."""
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
                logger.debug("start_user_thread_flow notify failed", exc_info=True)

            await self._run_flow_in_thread(thread, user)

    async def _run_flow_in_thread(self, thread: discord.Thread, user: discord.User):
        # Schritt 1/3 – Status
        status_view = PlayerStatusView()
        ok = await send_step_embed_thread(
            thread,
            title="Frage 1/3 · Spielst du schon Deadlock – oder wieder?",
            desc="Sag mir kurz, wo du stehst – dann passe ich alles besser für dich an.",
            step=1, total=3,
            view=status_view,
            color=0x95A5A6
        )
        if not ok:
            return
        status_choice = status_view.choice or STATUS_PLAYING

        # Schritt 2/3 – Steam verknüpfen (über bestehendes DM-Modul)
        steam_view = SteamLinkNudgeView()
        ok = await send_step_embed_thread(
            thread,
            title="Frage 2/3 · Steam verknüpfen (empfohlen)",
            desc="Damit Voice-Status & Features funktionieren, verknüpfe bitte deinen Steam-Account.",
            step=2, total=3,
            view=steam_view,
            color=0x2ECC71
        )
        if not ok:
            return

        # Schritt 3/3 – Regeln bestätigen
        await send_rules_confirm_in_thread(thread)

        # Abschluss-Hinweise je nach Status (optional)
        closing_lines = []
        if status_choice == STATUS_NEW_PLAYER:
            closing_lines.append(
                "✨ **Willkommen!** Bei Fragen: @earlysalty oder im Hilfebereich posten."
            )
        if status_choice == STATUS_NEED_BETA:
            closing_lines.append(
                "🎟️ **Beta-Invite?** Schau in **#beta-zugang** vorbei und poste deine **Steam-Freundschafts-ID** "
                "(Steam → Freunde → Freund hinzufügen)."
            )
        if status_choice == STATUS_RETURNING:
            closing_lines.append("🔁 **Willkommen zurück!** Schau in #game-guides-und-tipps für frische Infos.")
        if status_choice == STATUS_PLAYING:
            closing_lines.append("✅ **Viel Spaß!** Nutze die Voice-Kanäle aktiv – so findet man am schnellsten Mates.")

        if closing_lines:
            try:
                await thread.send("\n\n".join(closing_lines))
            except Exception:
                logger.debug("closing lines send failed", exc_info=True)

# ========= Setup =========

async def setup(bot: commands.Bot):
    await bot.add_cog(RulesPanel(bot))
