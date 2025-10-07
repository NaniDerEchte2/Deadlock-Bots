# -*- coding: utf-8 -*-
# filename: cogs/clip_submission.py
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Optional, Dict

import discord
from discord import app_commands
from discord.ext import commands

# =========================
# >>> KONFIG-KOPF (deine IDs) <<<
SUBMIT_CHANNEL_ID = 1374364800817303632   # Kanal mit Interface (Embed + Button)
REVIEW_CHANNEL_ID = 1374364800817303632   # Kanal für Review-Embeds (falls aktiviert)
GUILD_ID: int | None = None               # Optional: auf eine Guild begrenzen (sonst None)
AUTO_POST_ON_READY = True                 # Beim Start/Reload Interface automatisch prüfen/erzeugen

# --- NEU: Zielsteuerung ---
REVIEW_CHANNEL_ENABLED = False             # Clip im Review-Channel posten? (True/False)
SEND_TO_USER = True                      # Clip zusätzlich/alternativ per DM an User senden? (True/False)
SEND_TO_USER_ID = 00000    # Ziel-User-ID für DM (z.B. 123456789012345678) – erforderlich wenn SEND_TO_USER=True
# =========================

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def get_db_path() -> Path:
    env_path = os.getenv("DEADLOCK_DB_PATH")
    if env_path:
        p = Path(env_path)
    else:
        p = Path.home() / "Documents" / "Deadlock" / "service" / "deadlock.sqlite3"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def with_db():
    path = get_db_path()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    with with_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS clip_submissions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                link TEXT NOT NULL,
                credit TEXT NOT NULL,
                permission TEXT NOT NULL,
                info TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS clip_fixed_message(
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                message_id INTEGER
            )
            """
        )
        db.commit()


# -------------------- Views & Modal --------------------

class ConfirmPermissionView(discord.ui.View):
    def __init__(self, cog: "ClipSubmissionCog"):
        super().__init__(timeout=120)
        self.cog = cog

    @discord.ui.button(
        label="Ich bin ersteller des Clips oder die Erlaubnis liegt vor",
        style=discord.ButtonStyle.success,
        custom_id="clip_perm_yes_v1",
    )
    async def perm_yes(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.cog._pending_permission[interaction.user.id] = "owner_or_permission"
        await self.open_modal(interaction)

    '''
    @discord.ui.button(
        label="Nur Owner-Erlaubnis (nicht mein Clip)",
        style=discord.ButtonStyle.primary,
        custom_id="clip_perm_owner_v1",
    )
    async def perm_owner(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.cog._pending_permission[interaction.user.id] = "owner_granted"
        await self.open_modal(interaction)
    '''
    async def open_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ClipSubmitModal(self.cog))


class ClipSubmitModal(discord.ui.Modal, title="Gameplay-Clip einreichen"):
    def __init__(self, cog: "ClipSubmissionCog"):
        super().__init__(timeout=180)
        self.cog = cog

        # Wichtig: Labels <= 45 Zeichen!
        self.clip_link = discord.ui.TextInput(
            label="Clip-Link (YouTube/Twitch/etc.)",
            placeholder="https://…",
            required=True,
            max_length=400,
        )
        self.credit = discord.ui.TextInput(
            label="Credit/Username (Overlay)",
            placeholder="@DeadlockPlayer123",
            required=True,
            max_length=100,
        )
        self.info = discord.ui.TextInput(
            label="Info (Kontext/Zeitstempel)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            placeholder="z. B. Held, Map, Timestamp 00:36, Besonderheiten",
        )

        self.add_item(self.clip_link)
        self.add_item(self.credit)
        self.add_item(self.info)

    async def on_submit(self, interaction: discord.Interaction):
        user = interaction.user
        guild = interaction.guild
        link = self.clip_link.value.strip()
        credit = self.credit.value.strip()
        info = (self.info.value or "").strip()
        permission = self.cog._pending_permission.pop(user.id, "unspecified")

        if not URL_RE.fullmatch(link):
            return await interaction.response.send_message(
                "❌ Der Link sieht nicht wie eine gültige URL aus. Bitte erneut versuchen.",
                ephemeral=True,
            )

        now = discord.utils.utcnow().timestamp()
        last = self.cog._last_submit_ts.get(user.id, 0)
        if now - last < 60:
            return await interaction.response.send_message(
                "⏱️ Bitte warte kurz bevor du erneut einsendest (60 Sek. Cooldown).",
                ephemeral=True,
            )
        self.cog._last_submit_ts[user.id] = now

        # Persistenz
        with with_db() as db:
            db.execute(
                """
                INSERT INTO clip_submissions(guild_id, user_id, link, credit, permission, info)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (guild.id if guild else 0, user.id, link, credit, permission, info),
            )
            db.commit()

        # Embed bauen (identisch für Kanal & DM)
        embed = discord.Embed(
            title="🎬 Neue Clip-Einsendung",
            description="Eine neue Einsendung zur Sichtung.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Link", value=link, inline=False)
        embed.add_field(name="Credit (Overlay)", value=credit, inline=True)
        perm_text = (
            "Ich besitze den Clip / Erlaubnis"
            if permission == "owner_or_permission"
            else "Owner hat Erlaubnis gegeben"
            if permission == "owner_granted"
            else "Unbekannt"
        )
        embed.add_field(name="Erlaubnis", value=perm_text, inline=True)
        if info:
            embed.add_field(name="Info", value=info[:1024], inline=False)
        embed.set_footer(text=f"User: {user} • UserID: {user.id}")
        embed.timestamp = discord.utils.utcnow()

        # Flags zum Reporting sammeln
        posted_to_channel = False
        sent_dm = False
        errors: list[str] = []

        # Optional: Review-Channel Posting
        if REVIEW_CHANNEL_ENABLED:
            review_channel = None
            if guild:
                ch = guild.get_channel(REVIEW_CHANNEL_ID)
                if isinstance(ch, discord.TextChannel):
                    review_channel = ch
                else:
                    try:
                        ch = await guild.fetch_channel(REVIEW_CHANNEL_ID)
                        if isinstance(ch, discord.TextChannel):
                            review_channel = ch
                    except Exception:
                        review_channel = None

            if review_channel is None:
                errors.append("Review-Channel nicht gefunden/zugreifbar.")
            else:
                try:
                    await review_channel.send(embed=embed)
                    posted_to_channel = True
                except Exception as e:
                    errors.append(f"Posting im Review-Channel fehlgeschlagen: {e}")

        # Optional: DM an definierten User
        if SEND_TO_USER:
            if SEND_TO_USER_ID is None:
                errors.append("SEND_TO_USER=True aber SEND_TO_USER_ID ist nicht gesetzt.")
            else:
                try:
                    target_user = (interaction.client or self.cog.bot).get_user(SEND_TO_USER_ID)
                    if target_user is None:
                        # fetch_user holt auch, wenn nicht gecached
                        target_user = await (interaction.client or self.cog.bot).fetch_user(SEND_TO_USER_ID)
                    await target_user.send(embed=embed)
                    sent_dm = True
                except Exception as e:
                    errors.append(f"DM an User {SEND_TO_USER_ID} fehlgeschlagen: {e}")

        # Nutzer-Feedback (ephemeral)
        # Fallunterscheidung, um klares Feedback zu geben:
        if posted_to_channel and sent_dm:
            note = "✅ Danke! Dein Clip ist eingegangen. An Review-Channel **und** den konfigurierten User gesendet."
        elif posted_to_channel:
            note = "✅ Danke! Dein Clip ist eingegangen. Im Review-Channel gepostet."
        elif sent_dm:
            note = "✅ Danke! Dein Clip ist eingegangen. An den konfigurierten User per DM gesendet."
        else:
            # Nichts gesendet – aber gespeichert
            note = "✅ Gespeichert! (Es wurde weder an einen Kanal noch per DM gesendet – prüfe die Konfiguration.)"

        if errors:
            note += "\n\n⚠️ Hinweise:\n- " + "\n- ".join(f"`{e}`" for e in errors)

        await interaction.response.send_message(
            note + "\n\nMindestqualität **1080p**.",
            ephemeral=True,
        )


class ClipSubmitView(discord.ui.View):
    def __init__(self, cog: "ClipSubmissionCog"):
        super().__init__(timeout=None)  # persistent
        self.cog = cog

    @discord.ui.button(
        label="Clip einsenden",
        style=discord.ButtonStyle.primary,
        custom_id="clip_submit_btn_v1",
    )
    async def submit(self, interaction: discord.Interaction, _: discord.ui.Button):
        view = ConfirmPermissionView(self.cog)
        await interaction.response.send_message(
            "Bitte bestätige zunächst die Verwendungserlaubnis:",
            view=view,
            ephemeral=True,
        )


# -------------------- Cog --------------------

RULES_TEXT = (
    "• Reiche einen Gameplay-Clip in mind. 1080p ein.\n"
    "• Füge **Link**, **Credit/Username** (Overlay) und **Kontext/Info** hinzu.\n"
    "• Durch das Absenden bestätigst du das du die Einverständnis des Erstellers hast.\n"
    "• Durch das Absenden dürfen wir den Clip frei Verwenden; Credits erscheinen im Video.\n"
)

class ClipSubmissionCog(commands.Cog):
    """Persistent Interface: prüft beim Start, nimmt bestehende Message wieder auf oder erstellt neu und speichert ID."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending_permission: Dict[int, str] = {}
        self._last_submit_ts: Dict[int, float] = {}
        init_db()
        self.bot.add_view(ClipSubmitView(self))

    async def upsert_interface(self, guild: discord.Guild) -> Optional[int]:
        if GUILD_ID is not None and guild.id != GUILD_ID:
            return None

        channel: Optional[discord.TextChannel] = None
        ch = guild.get_channel(SUBMIT_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            channel = ch
        else:
            try:
                ch = await guild.fetch_channel(SUBMIT_CHANNEL_ID)
                if isinstance(ch, discord.TextChannel):
                    channel = ch
            except Exception:
                channel = None
        if channel is None:
            return None

        with with_db() as db:
            row = db.execute(
                "SELECT message_id FROM clip_fixed_message WHERE guild_id=?",
                (guild.id,),
            ).fetchone()

        message = None
        if row and row[0]:
            try:
                message = await channel.fetch_message(row[0])
            except Exception:
                message = None

        embed = discord.Embed(
            title="🎥 Deadlock Gameplay-Clips einsenden",
            description=RULES_TEXT,
            color=discord.Color.green(),
        )
        embed.set_footer(text="Mit dem Button unten kannst du deinen Clip einreichen.")
        view = ClipSubmitView(self)

        if message:
            await message.edit(embed=embed, view=view, content=None)
            message_id = message.id
        else:
            sent = await channel.send(embed=embed, view=view)
            message_id = sent.id
            with with_db() as db:
                db.execute(
                    """
                    INSERT INTO clip_fixed_message(guild_id, channel_id, message_id)
                    VALUES(?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        channel_id=excluded.channel_id,
                        message_id=excluded.message_id
                    """,
                    (guild.id, channel.id, message_id),
                )
                db.commit()

        return message_id

    @commands.Cog.listener()
    async def on_ready(self):
        if not AUTO_POST_ON_READY:
            return

        if GUILD_ID is not None:
            g = self.bot.get_guild(GUILD_ID)
            if g:
                try:
                    await self.upsert_interface(g)
                except Exception:
                    pass
            return

        for g in self.bot.guilds:
            try:
                await self.upsert_interface(g)
            except Exception:
                pass

    @app_commands.command(name="clips_repost", description="Interface-Nachricht erneut erstellen/aktualisieren.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clips_repost(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Nur in einem Server nutzbar.", ephemeral=True)

        msg_id = await self.upsert_interface(guild)
        if msg_id:
            await interaction.response.send_message(f"✅ Interface aktiv. MessageID: `{msg_id}`", ephemeral=True)
        else:
            await interaction.response.send_message(
                "❌ Konnte Interface nicht posten – prüfe `SUBMIT_CHANNEL_ID` und Bot-Rechte.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(ClipSubmissionCog(bot))
