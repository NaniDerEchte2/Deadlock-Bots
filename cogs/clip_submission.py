# -*- coding: utf-8 -*-
# filename: cogs/clip_submission.py
from __future__ import annotations

import io
import re
import time
import random
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List

import discord
from discord import app_commands
from discord.ext import commands, tasks

# =========================
# >>> KONFIG-KOPF (deine IDs) <<<
SUBMIT_CHANNEL_ID = 1425215762460835931   # Kanal mit Interface (Embed + Button)
REVIEW_CHANNEL_ID = 1374364800817303632   # Kanal für Review-Embeds (falls aktiviert)
GUILD_ID: int | None = None               # Optional: auf eine Guild begrenzen (sonst None)
AUTO_POST_ON_READY = True                 # Beim Start/Reload Interface automatisch prüfen/erzeugen

# --- Ziele / Defaults ---
REVIEW_CHANNEL_ENABLED = False            # Clip im Review-Channel posten?
SEND_TO_USER = True                       # Standard: Dump/Benachrichtigungen per DM senden
SEND_TO_USER_ID = 662995601738170389      # Ziel-User-ID für DM (Admin/Redaktion)

# --- Wöchentliches Fenster ---
# Ende-Wochentag (0=Montag ... 6=Sonntag) und Uhrzeit (24h) in **lokaler Zeit**.
WINDOW_END_WEEKDAY = 5                    # 5 = Samstag
WINDOW_END_HOUR = 23                      # 23:00 Uhr
LOCAL_TZ = timezone.utc                   # falls du lokal willst: timezone(timedelta(hours=+2)) o.ä.

# Anzeige: so zeigt’s Discord im Embed; Logik bleibt in UTC stabil.
DISPLAY_TZ = timezone.utc

# =========================

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

# ===== ZENTRALE DB =====
try:
    from service import db as central_db  # type: ignore
except Exception:
    central_db = None

def _conn():
    if not central_db:
        raise RuntimeError("Zentrale DB 'service.db' nicht importierbar.")
    return central_db.connect()

def _exec(sql: str, params: tuple = ()) -> None:
    with _conn() as c:
        c.execute(sql, params)

def _fetchone(sql: str, params: tuple = ()):
    with _conn() as c:
        cur = c.execute(sql, params)
        return cur.fetchone()

def _fetchall(sql: str, params: tuple = ()):
    with _conn() as c:
        cur = c.execute(sql, params)
        return cur.fetchall()

# ---- Wir nutzen deine bestehende Tabelle persistent_views:
# persistent_views(message_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, guild_id TEXT NOT NULL,
#                  view_type TEXT NOT NULL, user_id TEXT, created_at TIMESTAMP DEFAULT ...)
VIEW_TYPE = "clip_submission_v1"

# ---- zusätzliche Tabellen (Clips & Contests) – falls noch nicht vorhanden:
def init_schema():
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS clip_submissions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                link TEXT NOT NULL,
                credit TEXT NOT NULL,
                permission TEXT NOT NULL,
                info TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS clip_contests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT,
                start_ts INTEGER NOT NULL,
                end_ts   INTEGER NOT NULL,
                announce_channel_id INTEGER,
                status TEXT NOT NULL DEFAULT 'running', -- running|ended|published
                video_url TEXT,
                winner_user_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS clip_contest_submissions(
                contest_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY(contest_id, submission_id),
                FOREIGN KEY(contest_id) REFERENCES clip_contests(id) ON DELETE CASCADE,
                FOREIGN KEY(submission_id) REFERENCES clip_submissions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_clip_contests_guild ON clip_contests(guild_id);
            CREATE INDEX IF NOT EXISTS idx_clip_contests_active ON clip_contests(guild_id, start_ts, end_ts);
            """
        )

# -------------------- Zeit-/Contest-Utils --------------------

def _now_utc_ts() -> int:
    return int(time.time())

def _to_display_ts(ts: int) -> str:
    return f"<t:{int(ts)}:f>"

def _week_window_for_now() -> tuple[int, int, str]:
    """
    Liefert (start_ts_utc, end_ts_utc, name) für die **aktuelle Woche** gemäß WINDOW_END_WEEKDAY/HOUR.
    Logik: Fenster endet am nächsten gewünschten Wochentag um WINDOW_END_HOUR (lokale Zeit), Länge = 7 Tage.
    """
    now_local = datetime.now(tz=LOCAL_TZ)
    # finde nächste End-Grenze
    days_ahead = (WINDOW_END_WEEKDAY - now_local.weekday()) % 7
    end_local = (now_local + timedelta(days=days_ahead)).replace(
        hour=WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    # wenn Endzeit bereits überschritten: auf die nächste Woche schieben
    if end_local <= now_local:
        end_local += timedelta(days=7)
    start_local = end_local - timedelta(days=7)

    # nach UTC konvertieren
    end_utc = end_local.astimezone(timezone.utc)
    start_utc = start_local.astimezone(timezone.utc)
    name = f"Clips {start_local.date().isoformat()} – {end_local.date().isoformat()}"
    return int(start_utc.timestamp()), int(end_utc.timestamp()), name

def _ensure_current_week_contest(guild_id: int) -> int:
    """
    Stellt sicher, dass es **ein** laufendes/scheduled Contest für die aktuelle Woche gibt.
    Gibt die Contest-ID zurück.
    """
    start_ts, end_ts, name = _week_window_for_now()
    # existiert passende Zeile?
    row = _fetchone(
        """
        SELECT id FROM clip_contests
         WHERE guild_id=? AND start_ts=? AND end_ts=?
         LIMIT 1
        """, (guild_id, start_ts, end_ts)
    )
    if row:
        return int(row[0])
    # evtl. veraltete "running" der Vorwoche beenden
    _exec(
        "UPDATE clip_contests SET status='ended' WHERE guild_id=? AND end_ts<=? AND status!='ended'",
        (guild_id, _now_utc_ts())
    )
    # neuen Datensatz anlegen (running)
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO clip_contests(guild_id, name, start_ts, end_ts, status) VALUES (?, ?, ?, ?, 'running')",
            (guild_id, name, start_ts, end_ts)
        )
        return int(cur.lastrowid)

def _get_running_contest(guild_id: int) -> Optional[dict]:
    now = _now_utc_ts()
    row = _fetchone(
        """
        SELECT id, name, start_ts, end_ts, status
          FROM clip_contests
         WHERE guild_id=? AND start_ts<=? AND end_ts>? AND status='running'
         LIMIT 1
        """, (guild_id, now, now)
    )
    return dict(row) if row else None

def _mark_contest_ended_and_dump(guild_id: int, contest_id: int) -> None:
    _exec("UPDATE clip_contests SET status='ended' WHERE id=?", (contest_id,))
    # Dump wird im Task erzeugt & versendet (siehe weekly_closer)

def _list_contest_submissions(contest_id: int) -> List[dict]:
    rows = _fetchall(
        """
        SELECT s.id, s.user_id, s.link, s.credit, s.permission, s.info, s.created_at
          FROM clip_contest_submissions x
          JOIN clip_submissions s ON s.id = x.submission_id
         WHERE x.contest_id = ?
         ORDER BY s.created_at ASC
        """, (contest_id,)
    )
    return [dict(r) for r in rows]

def _build_dump_text(contest: dict, subs: List[dict]) -> str:
    lines = []
    header = f"# Dump – {contest.get('name') or 'Unbenannt'} | Zeitraum: {datetime.fromtimestamp(contest['start_ts'], tz=DISPLAY_TZ)} – {datetime.fromtimestamp(contest['end_ts'], tz=DISPLAY_TZ)}"
    lines.append(header)
    lines.append("")
    for s in subs:
        created = s.get("created_at", "")
        # minimalistisch, einfach parsbar:
        lines.append(f"{s['link']} | {s['credit']} | user_id={s['user_id']} | created_at={created}")
    if not subs:
        lines.append("(keine Einsendungen)")
    lines.append("")
    lines.append(f"Total submissions: {len(subs)}")
    return "\n".join(lines)

# -------- Persistent-View-Helpers (nutzen persistent_views) --------

def pv_get_latest(guild_id: int, view_type: str) -> Optional[dict]:
    row = _fetchone(
        """
        SELECT message_id, channel_id, guild_id, view_type, user_id, created_at
          FROM persistent_views
         WHERE guild_id = ? AND view_type = ?
         ORDER BY datetime(created_at) DESC
         LIMIT 1
        """,
        (str(guild_id), view_type)
    )
    return dict(row) if row else None

def pv_upsert_single(guild_id: int, channel_id: int, message_id: int, view_type: str, user_id: Optional[int] = None) -> None:
    with _conn() as c:
        c.execute("DELETE FROM persistent_views WHERE guild_id = ? AND view_type = ?", (str(guild_id), view_type))
        c.execute(
            """
            INSERT INTO persistent_views (message_id, channel_id, guild_id, view_type, user_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(message_id), str(channel_id), str(guild_id), view_type, str(user_id) if user_id else None)
        )

# -------------------- Views & Modal --------------------

class ConfirmPermissionView(discord.ui.View):
    def __init__(self, cog: "ClipSubmissionCog"):
        super().__init__(timeout=120)
        self.cog = cog

    @discord.ui.button(
        label="Ich bin Ersteller oder Erlaubnis liegt vor",
        style=discord.ButtonStyle.success,
        custom_id="clip_perm_yes_v1",
    )
    async def perm_yes(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.cog._pending_permission[interaction.user.id] = "owner_or_permission"
        await interaction.response.send_modal(ClipSubmitModal(self.cog))


class ClipSubmitModal(discord.ui.Modal, title="Gameplay-Clip einreichen"):
    def __init__(self, cog: "ClipSubmissionCog"):
        super().__init__(timeout=180)
        self.cog = cog

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

        now = time.time()
        last = self.cog._last_submit_ts.get(user.id, 0)
        if now - last < 60:
            return await interaction.response.send_message(
                "⏱️ Bitte warte kurz bevor du erneut einsendest (60 Sek. Cooldown).",
                ephemeral=True,
            )
        self.cog._last_submit_ts[user.id] = now

        with _conn() as db:
            cur = db.execute(
                """
                INSERT INTO clip_submissions(guild_id, user_id, link, credit, permission, info)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (guild.id if guild else 0, user.id, link, credit, permission, info),
            )
            submission_id = cur.lastrowid

            # aktuelle Woche/Contest sicherstellen und Zuordnung
            contest_id = _ensure_current_week_contest(guild.id if guild else 0)
            db.execute(
                "INSERT OR IGNORE INTO clip_contest_submissions(contest_id, submission_id, user_id) VALUES (?, ?, ?)",
                (contest_id, submission_id, user.id)
            )

        # Optional: Review-Channel & Admin-DM
        embed = discord.Embed(
            title="🎬 Neue Clip-Einsendung",
            description="Eine neue Einsendung zur Sichtung.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Link", value=link, inline=False)
        embed.add_field(name="Credit (Overlay)", value=credit, inline=True)
        perm_text = "Ich besitze den Clip / Erlaubnis" if permission == "owner_or_permission" else "Unbekannt"
        embed.add_field(name="Erlaubnis", value=perm_text, inline=True)
        if info:
            embed.add_field(name="Info", value=info[:1024], inline=False)
        embed.set_footer(text=f"User: {user} • UserID: {user.id}")
        embed.timestamp = discord.utils.utcnow()

        errors: List[str] = []
        if REVIEW_CHANNEL_ENABLED and guild:
            try:
                channel = guild.get_channel(REVIEW_CHANNEL_ID) or await guild.fetch_channel(REVIEW_CHANNEL_ID)
                if isinstance(channel, discord.TextChannel):
                    await channel.send(embed=embed)
            except Exception as e:
                errors.append(f"Review-Channel fehlgeschlagen: {e}")

        if SEND_TO_USER and SEND_TO_USER_ID:
            try:
                admin = (interaction.client or self.cog.bot).get_user(SEND_TO_USER_ID) or await (interaction.client or self.cog.bot).fetch_user(SEND_TO_USER_ID)
                await admin.send(embed=embed)
            except Exception as e:
                errors.append(f"DM an Admin fehlgeschlagen: {e}")

        note = "✅ Danke! Dein Clip ist eingegangen."
        if errors:
            note += "\n\n⚠️ Hinweise:\n- " + "\n- ".join(f"`{e}`" for e in errors)

        await interaction.response.send_message(note + "\n\nMindestqualität **1080p**.", ephemeral=True)


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
        await interaction.response.send_message(
            "Bitte bestätige zunächst die Verwendungserlaubnis:",
            view=ConfirmPermissionView(self.cog),
            ephemeral=True,
        )

# -------------------- Cog --------------------

RULES_TEXT = (
    "• Reiche einen Gameplay-Clip in mind. 1080p ein.\n"
    "• Füge **Link**, **Credit/Username** (Overlay) und **Kontext/Info** hinzu.\n"
    "• Durch das Absenden bestätigst du, dass die Einverständnis des Erstellers vorliegt.\n"
    "• Durch das Absenden dürfen wir den Clip frei verwenden; Credits erscheinen im Video.\n"
)

class ClipSubmissionCog(commands.Cog):
    """Clip-Interface (persistent), wöchentliches Teilnahmefenster, automatischer Dump am Wochenende."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending_permission: Dict[int, str] = {}
        self._last_submit_ts: Dict[int, float] = {}
        self._upsert_locks: Dict[int, asyncio.Lock] = {}

        init_schema()

        # persistenten View global registrieren (überlebt Restarts)
        self.bot.add_view(ClipSubmitView(self))

        # periodisch Interface aktualisieren & Weekly-Logik ausführen
        self.interface_refresher.start()
        self.weekly_closer.start()

    # ---------- Interface & Embeds ----------

    def _contest_line(self, guild_id: int) -> str:
        # Stellt sicher, dass aktuelle Woche existiert (liefert ID, aber wir brauchen nur Zeitraum)
        cid = _ensure_current_week_contest(guild_id)
        row = _fetchone("SELECT start_ts, end_ts FROM clip_contests WHERE id=?", (cid,))
        if not row:
            return "📅 Aktuell ist kein Teilnahmefenster geplant."
        start_ts, end_ts = int(row[0]), int(row[1])
        now = _now_utc_ts()
        if start_ts <= now < end_ts:
            return f"🏁 **Teilnahmefenster aktiv**: {_to_display_ts(start_ts)} – {_to_display_ts(end_ts)} (endet <t:{end_ts}:R>)"
        return f"🗓️ Nächstes Fenster: {_to_display_ts(start_ts)} – {_to_display_ts(end_ts)} (startet <t:{start_ts}:R>)"

    async def upsert_interface(self, guild: discord.Guild) -> Optional[int]:
        if GUILD_ID is not None and guild.id != GUILD_ID:
            return None

        # guild-lock: verhindert Doppelevents/Mehrfachposts
        lock = self._upsert_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            # Kanal ermitteln
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

            # existierenden Datensatz aus persistent_views nehmen
            pv = pv_get_latest(guild.id, VIEW_TYPE)
            message = None
            if pv and pv.get("channel_id") and int(pv["channel_id"]) == channel.id:
                try:
                    message = await channel.fetch_message(int(pv["message_id"]))
                except Exception:
                    message = None  # wurde evtl. gelöscht

            embed = discord.Embed(
                title="🎥 Deadlock Gameplay-Clips einsenden",
                description=RULES_TEXT + "\n\n" + self._contest_line(guild.id),
                color=discord.Color.green(),
            )
            embed.set_footer(text="Mit dem Button unten kannst du deinen Clip einreichen.")
            view = ClipSubmitView(self)

            if message:
                await message.edit(embed=embed, view=view, content=None)
                pv_upsert_single(guild.id, channel.id, message.id, VIEW_TYPE)
                return message.id
            else:
                # falls pv existiert aber Channel-ID abweicht -> neu posten und pv ersetzen
                sent = await channel.send(embed=embed, view=view)
                pv_upsert_single(guild.id, channel.id, sent.id, VIEW_TYPE)
                return sent.id

    @tasks.loop(minutes=5)
    async def interface_refresher(self):
        """Alle 5 Minuten: Fenster-Text aktualisieren + Interface gesund halten."""
        try:
            if GUILD_ID is not None:
                g = self.bot.get_guild(GUILD_ID)
                if g:
                    await self.upsert_interface(g)
                return
            for g in self.bot.guilds:
                await self.upsert_interface(g)
        except Exception:
            pass

    @interface_refresher.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ---------- Weekly-Ende & Dump ----------

    @tasks.loop(minutes=1)
    async def weekly_closer(self):
        """
        Prüft minütlich:
        - Wenn aktueller "running"-Contest abgelaufen ist -> auf 'ended' setzen und Dump versenden.
        - Danach sofort nächsten Wochen-Contest **anlegen** (damit Interface weiter korrekt anzeigt).
        """
        try:
            guild_ids = [GUILD_ID] if GUILD_ID is not None else [g.id for g in self.bot.guilds]
            now = _now_utc_ts()
            for gid in guild_ids:
                # Running-Contest?
                cont = _get_running_contest(gid)
                if cont and now >= int(cont["end_ts"]):
                    # beenden
                    _mark_contest_ended_and_dump(gid, int(cont["id"]))
                    # Dump erzeugen & versenden
                    await self._send_dump_for_contest(gid, int(cont["id"]))
                    # nächstes Fenster sofort bereitstellen
                    _ensure_current_week_contest(gid)
                    # Interface aktualisieren
                    g = self.bot.get_guild(gid)
                    if g:
                        await self.upsert_interface(g)
        except Exception:
            pass

    @weekly_closer.before_loop
    async def _wait_ready2(self):
        await self.bot.wait_until_ready()

    async def _send_dump_for_contest(self, guild_id: int, contest_id: int):
        """Erzeugt TXT-Dump & sendet ihn per DM an SEND_TO_USER_ID; fallback: im SUBMIT_CHANNEL_ID posten."""
        contest = _fetchone("SELECT id, name, start_ts, end_ts FROM clip_contests WHERE id=?", (contest_id,))
        if not contest:
            return
        cont = dict(contest)
        subs = _list_contest_submissions(contest_id)
        txt = _build_dump_text(cont, subs)

        # Datei bauen
        fp = io.BytesIO(txt.encode("utf-8"))
        filename = f"clip_dump_{contest_id}.txt"
        file = discord.File(fp, filename=filename)

        # DM an Admin?
        sent = False
        if SEND_TO_USER and SEND_TO_USER_ID:
            try:
                user = self.bot.get_user(SEND_TO_USER_ID) or await self.bot.fetch_user(SEND_TO_USER_ID)
                await user.send(
                    content=f"📦 **Dump für `{cont.get('name','Contest')}`** (Contest #{contest_id})",
                    file=file
                )
                sent = True
            except Exception:
                sent = False

        if not sent:
            # Fallback: in SUBMIT_CHANNEL posten
            g = self.bot.get_guild(guild_id)
            if not g:
                return
            ch = g.get_channel(SUBMIT_CHANNEL_ID) or await g.fetch_channel(SUBMIT_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                await ch.send(content=f"📦 **Dump – Contest #{contest_id}**", file=file)

    # ---------- Lifecycle ----------

    @commands.Cog.listener()
    async def on_ready(self):
        if not AUTO_POST_ON_READY:
            return

        if GUILD_ID is not None:
            g = self.bot.get_guild(GUILD_ID)
            if g:
                try:
                    _ensure_current_week_contest(g.id)
                    await self.upsert_interface(g)
                except Exception:
                    pass
            return

        for g in self.bot.guilds:
            try:
                _ensure_current_week_contest(g.id)
                await self.upsert_interface(g)
            except Exception:
                pass

    # ---------- Admin-Befehle ----------

    @app_commands.command(name="clips_repost", description="Interface-Nachricht erneut erstellen/aktualisieren.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clips_repost(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Nur in einem Server nutzbar.", ephemeral=True)

        _ensure_current_week_contest(guild.id)
        msg_id = await self.upsert_interface(guild)
        if msg_id:
            await interaction.response.send_message(f"✅ Interface aktiv. MessageID: `{msg_id}`", ephemeral=True)
        else:
            await interaction.response.send_message(
                "❌ Konnte Interface nicht posten – prüfe `SUBMIT_CHANNEL_ID` und Bot-Rechte.",
                ephemeral=True,
            )

    @app_commands.command(name="clips_dump_now", description="Sende sofort den Dump für das aktuelle (laufende) Fenster.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clips_dump_now(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            return await interaction.response.send_message("Nur im Server nutzbar.", ephemeral=True)

        cont = _get_running_contest(guild.id)
        if not cont:
            return await interaction.response.send_message("ℹ️ Kein laufendes Fenster.", ephemeral=True)

        await self._send_dump_for_contest(guild.id, int(cont["id"]))
        await interaction.response.send_message("✅ Dump gesendet.", ephemeral=True)

    @app_commands.command(name="clips_window_status", description="Zeigt Zeitraum des aktuellen Wochenfensters.")
    async def clips_window_status(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("Nur im Server nutzbar.", ephemeral=True)
        cid = _ensure_current_week_contest(guild.id)
        row = _fetchone("SELECT name, start_ts, end_ts FROM clip_contests WHERE id=?", (cid,))
        if not row:
            return await interaction.response.send_message("ℹ️ Kein Fenster gefunden.", ephemeral=True)
        name, start_ts, end_ts = row[0], int(row[1]), int(row[2])
        await interaction.response.send_message(
            f"**{name}**\nZeitraum: {_to_display_ts(start_ts)} – {_to_display_ts(end_ts)}",
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ClipSubmissionCog(bot))
