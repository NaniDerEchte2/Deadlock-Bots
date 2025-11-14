# -*- coding: utf-8 -*-
# filename: cogs/clip_submission.py
from __future__ import annotations

import io
import logging
import random
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands, tasks

# =========================
# >>> KONFIG-KOPF (deine IDs) <<<
SUBMIT_CHANNEL_ID = 1425215762460835931   # Kanal mit Interface (Embed + Button)
REVIEW_CHANNEL_ID = 1374364800817303632   # Kanal für Review-Embeds (falls aktiviert)
GUILD_ID: int | None = None               # Optional: auf eine Guild begrenzen (sonst None)
AUTO_POST_ON_READY = True                 # Beim Start/Reload Interface automatisch prüfen/erzeugen

# --- Ziele ---
REVIEW_CHANNEL_ENABLED = False            # Clip im Review-Channel posten?
SEND_TO_USER = True                       # Am Ende des Fensters: Gesamt-Dump an User senden?
SEND_TO_USER_ID = 388772056717590539      # Ziel-User-ID für Wochen-Dump (Gesamtpaket)

# --- Wöchentliche Fenster (Europe/Berlin) ---
WEEKLY_WINDOW_ENABLED = True
WINDOW_TZ = "Europe/Berlin"               # Zeitzone für Fensterberechnung
WINDOW_START_WEEKDAY = 6                  # 0=Mo ... 6=So -> Start: Sonntag 00:00
WINDOW_END_HOUR = 23                      # Ende: Samstag 23:00
# =========================

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
TZ = ZoneInfo(WINDOW_TZ)
VIEW_TYPE = "clip_submission_v1"          # Schlüssel in persistent_views

log = logging.getLogger(__name__)

# ===== ZENTRALE DB (nutzt deine bestehende service/db.py) =====
try:
    from service import db as central_db  # type: ignore
except Exception:
    central_db = None

def _conn():
    if not central_db:
        raise RuntimeError("Zentrale DB 'service.db' nicht importierbar.")
    return central_db.get_conn()

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

# ---- wir benutzen vorhandene Tabelle:
# persistent_views(message_id TEXT PK, channel_id TEXT, guild_id TEXT, view_type TEXT, user_id TEXT, created_at TIMESTAMP DEFAULT ...)
# Keine Schema-Änderung an persistent_views nötig.

def init_schema():
    """Minimale Schemata für Clips & Wochenfenster (idempotent)."""
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

            -- Wochenfenster-Status (automatisch pro Woche erzeugt)
            CREATE TABLE IF NOT EXISTS clip_windows(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                start_ts INTEGER NOT NULL,
                end_ts   INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'running', -- running | dumped
                dump_sent_ts INTEGER,
                UNIQUE(guild_id, start_ts, end_ts)
            );

            -- Zuordnung Einsendung ↔ Fenster (nur für Query-Komfort)
            CREATE TABLE IF NOT EXISTS clip_window_submissions(
                window_id INTEGER NOT NULL,
                submission_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY(window_id, submission_id),
                FOREIGN KEY(window_id) REFERENCES clip_windows(id) ON DELETE CASCADE,
                FOREIGN KEY(submission_id) REFERENCES clip_submissions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_clip_windows_guild ON clip_windows(guild_id);
            CREATE INDEX IF NOT EXISTS idx_clip_submissions_guild ON clip_submissions(guild_id);
            """
        )

# -------------------- Zeitfenster-Helfer --------------------

def _now_ts() -> int:
    return int(time.time())

def _dt_to_ts(dt: datetime) -> int:
    return int(dt.timestamp())

def _format_ts(ts: int, style: str = "f") -> str:
    return f"<t:{int(ts)}:{style}>"

def _compute_week_window_berlin(now: datetime) -> Tuple[int, int]:
    """
    Liefert (start_ts, end_ts) des aktuellen Wochenfensters gemäß:
    Start: Sonntag 00:00 Europe/Berlin
    Ende:  Samstag 23:00 Europe/Berlin
    """
    local = now.astimezone(TZ).replace(minute=0, second=0, microsecond=0)
    # finde den letzten Sonntag 00:00
    days_since_sun = (local.weekday() - WINDOW_START_WEEKDAY) % 7
    start_local = (local - timedelta(days=days_since_sun)).replace(hour=0)
    # Ende: Samstag 23:00 => Samstag ist (Sonntag + 6 Tage)
    end_local = (start_local + timedelta(days=6)).replace(hour=WINDOW_END_HOUR)
    return _dt_to_ts(start_local), _dt_to_ts(end_local)

def _ensure_window(guild_id: int) -> dict:
    """
    Stellt sicher, dass es ein laufendes Fenster für diese Woche gibt.
    Gibt das aktuelle Fenster (running oder bereits 'dumped', falls überfällig) als dict zurück.
    """
    now = datetime.now(tz=TZ)
    start_ts, end_ts = _compute_week_window_berlin(now)
    row = _fetchone(
        "SELECT id, start_ts, end_ts, status, dump_sent_ts FROM clip_windows WHERE guild_id=? AND start_ts=? AND end_ts=? LIMIT 1",
        (guild_id, start_ts, end_ts)
    )
    if row:
        return dict(row)

    # neu anlegen (running)
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO clip_windows(guild_id, start_ts, end_ts, status) VALUES (?, ?, ?, 'running')",
            (guild_id, start_ts, end_ts)
        )
        row = c.execute(
            "SELECT id, start_ts, end_ts, status, dump_sent_ts FROM clip_windows WHERE guild_id=? AND start_ts=? AND end_ts=? LIMIT 1",
            (guild_id, start_ts, end_ts)
        ).fetchone()
    return dict(row)

def _get_current_window_if_running(guild_id: int) -> Optional[dict]:
    w = _ensure_window(guild_id)
    now_ts = _now_ts()
    if w and int(w["start_ts"]) <= now_ts <= int(w["end_ts"]):
        return w
    return None

# -------------------- persistent_views-Helper --------------------

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
    # genau 1 Datensatz je (guild_id, view_type)
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
        await self.open_modal(interaction)

    async def open_modal(self, interaction: discord.Interaction):
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
            await interaction.response.send_message(
                "❌ Der Link sieht nicht wie eine gültige URL aus. Bitte erneut versuchen.",
                ephemeral=True,
            )
            return

        now = time.time()
        last = self.cog._last_submit_ts.get(user.id, 0)
        if now - last < 60:
            await interaction.response.send_message(
                "⏱️ Bitte warte kurz bevor du erneut einsendest (60 Sek. Cooldown).",
                ephemeral=True,
            )
            return
        self.cog._last_submit_ts[user.id] = now

        # Speichern
        with _conn() as db:
            cur = db.execute(
                """
                INSERT INTO clip_submissions(guild_id, user_id, link, credit, permission, info)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (guild.id if guild else 0, user.id, link, credit, permission, info),
            )
            submission_id = cur.lastrowid

            # aktuelle Woche zuordnen (falls running)
            w = _get_current_window_if_running(guild.id if guild else 0)
            if w:
                db.execute(
                    "INSERT OR IGNORE INTO clip_window_submissions(window_id, submission_id, user_id) VALUES (?, ?, ?)",
                    (int(w["id"]), int(submission_id), int(user.id))
                )

        # Optional: Review-Channel Info (kein DM mehr pro Submission!)
        if REVIEW_CHANNEL_ENABLED and guild:
            try:
                ch = guild.get_channel(REVIEW_CHANNEL_ID) or await guild.fetch_channel(REVIEW_CHANNEL_ID)
                if isinstance(ch, discord.TextChannel):
                    embed = discord.Embed(
                        title="🎬 Neue Clip-Einsendung",
                        description=f"Von **{user}** – Credit: `{credit}`",
                        color=discord.Color.blurple(),
                    )
                    embed.add_field(name="Link", value=link, inline=False)
                    if info:
                        embed.add_field(name="Info", value=info[:1024], inline=False)
                    embed.set_footer(text=f"UserID: {user.id}")
                    await ch.send(embed=embed)
            except Exception as exc:
                log.warning(
                    "Konnte Clip-Einsendung nicht im Review-Kanal posten (Guild %s, User %s): %s",
                    guild.id if guild else "?",
                    user.id,
                    exc,
                )

        await interaction.response.send_message(
            "✅ Danke! Dein Clip ist eingegangen.\n\nMindestqualität **1080p**.",
            ephemeral=True,
        )


class ClipSubmitView(discord.ui.View):
    def __init__(self, cog: "ClipSubmissionCog"):
        super().__init__(timeout=None)  # persistent!
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

INTERFACE_TITLE = "🎥 Deadlock Gameplay-Clips einsenden"

RULES_TEXT = (
    "• Reiche einen Gameplay-Clip in mind. 1080p ein.\n"
    "• Füge **Link**, **Credit/Username** (Overlay) und **Kontext/Info** hinzu.\n"
    "• Durch das Absenden bestätigst du, dass die Einverständnis des Erstellers vorliegt.\n"
    "• Durch das Absenden dürfen wir den Clip frei verwenden; Credits erscheinen im Video.\n"
)

class ClipSubmissionCog(commands.Cog):
    """Einsende-Interface + wöchentliches Fenster + Wochen-Dump. Persistenz via persistent_views."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending_permission: Dict[int, str] = {}
        self._last_submit_ts: Dict[int, float] = {}

        init_schema()

        # Persistente View global registrieren (überlebt Restarts)
        self.bot.add_view(ClipSubmitView(self))

        # Hintergrund-Tasks
        self.interface_refresher.start()
        if WEEKLY_WINDOW_ENABLED:
            self.weekly_window_manager.start()

    # ---------- Interface ----------

    def _window_line(self, guild_id: int) -> str:
        w = _ensure_window(guild_id)
        s, e = int(w["start_ts"]), int(w["end_ts"])
        now = _now_ts()
        if s <= now <= e:
            return f"🏁 **Teilnahmefenster aktiv**: { _format_ts(s,'f') } – { _format_ts(e,'f') } (endet { _format_ts(e,'R') })"
        return f"🗓️ Nächstes Fenster: { _format_ts(s,'f') } – { _format_ts(e,'f') } (startet { _format_ts(s,'R') })"

    async def _find_existing_interface_message(self, channel: discord.TextChannel) -> Optional[discord.Message]:
        """Fallback: Suche vorhandene Interface-Nachricht, falls persistent_views leer ist."""
        bot_user = self.bot.user
        if bot_user is None:
            return None
        try:
            async for msg in channel.history(limit=50):
                if msg.author.id != bot_user.id:
                    continue
                if not msg.embeds:
                    continue
                title = (msg.embeds[0].title or '').strip()
                if title == INTERFACE_TITLE:
                    return msg
        except discord.Forbidden:
            log.warning('Clip Interface: Keine Berechtigung, Verlauf von %s zu lesen.', channel.id)
        except discord.HTTPException as exc:
            log.warning('Clip Interface: HTTP-Fehler beim Durchsuchen von %s: %s', channel.id, exc)
        return None

    async def upsert_interface(self, guild: discord.Guild) -> Optional[int]:
        if GUILD_ID is not None and guild.id != GUILD_ID:
            return None

        # Kanal
        channel: Optional[discord.TextChannel] = None
        ch = guild.get_channel(SUBMIT_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            channel = ch
        else:
            try:
                ch = await guild.fetch_channel(SUBMIT_CHANNEL_ID)
                if isinstance(ch, discord.TextChannel):
                    channel = ch
            except Exception as exc:
                channel = None
                log.warning(
                    "Konnte Submit-Channel %s in Guild %s nicht abrufen: %s",
                    SUBMIT_CHANNEL_ID,
                    guild.id,
                    exc,
                )
        if channel is None:
            return None

        # vorhandene Interface-Message über persistent_views
        pv = pv_get_latest(guild.id, VIEW_TYPE)
        message = None
        if pv and str(pv["channel_id"]).isdigit():
            try:
                if int(pv["channel_id"]) == channel.id:
                    message = await channel.fetch_message(int(pv["message_id"]))
            except Exception as exc:
                message = None
                log.debug(
                    "Persistente Clip-Nachricht %s konnte nicht geladen werden (Guild %s): %s",
                    pv["message_id"],
                    guild.id,
                    exc,
                )
        if message is None:
            fallback = await self._find_existing_interface_message(channel)
            if fallback:
                message = fallback
                pv_upsert_single(guild.id, channel.id, message.id, VIEW_TYPE)
                log.info(
                    "Clip Interface: vorhandene Nachricht %s in Kanal %s wiederverwendet (Guild %s).",
                    message.id,
                    channel.id,
                    guild.id,
                )

        embed = discord.Embed(
            title=INTERFACE_TITLE,
            description=RULES_TEXT + "\n\n" + self._window_line(guild.id),
            color=discord.Color.green(),
        )
        embed.set_footer(text="Mit dem Button unten kannst du deinen Clip einreichen.")
        view = ClipSubmitView(self)

        if message:
            await message.edit(embed=embed, view=view, content=None)
            pv_upsert_single(guild.id, channel.id, message.id, VIEW_TYPE)
            return message.id

        # nicht gefunden → neu posten und persistent speichern
        sent = await channel.send(embed=embed, view=view)
        pv_upsert_single(guild.id, channel.id, sent.id, VIEW_TYPE)
        log.info(
            "Clip Interface: neue Nachricht %s in Kanal %s erstellt (Guild %s).",
            sent.id,
            channel.id,
            guild.id,
        )
        return sent.id

    @tasks.loop(minutes=5)
    async def interface_refresher(self):
        """Alle 5 Minuten Embed updaten (Countdown/Zeitraum)."""
        try:
            if GUILD_ID is not None:
                g = self.bot.get_guild(GUILD_ID)
                if g:
                    await self.upsert_interface(g)
                return
            for g in self.bot.guilds:
                await self.upsert_interface(g)
        except Exception:
            log.exception("Clip-Interface konnte nicht aktualisiert werden")

    @interface_refresher.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ---------- Weekly Window Manager ----------

    @tasks.loop(minutes=2)
    async def weekly_window_manager(self):
        """Erzeugt / hält das aktuelle Wochenfenster und verschickt genau einmal den Wochen-Dump nach Ablauf."""
        if not WEEKLY_WINDOW_ENABLED:
            return
        try:
            targets = []
            if GUILD_ID is not None:
                g = self.bot.get_guild(GUILD_ID)
                if g:
                    targets = [g]
            else:
                targets = list(self.bot.guilds)

            now_ts = _now_ts()
            for g in targets:
                w = _ensure_window(g.id)
                start_ts, end_ts = int(w["start_ts"]), int(w["end_ts"])
                status = w["status"]
                dumped = w["dump_sent_ts"]

                # nach Ende: Dump senden, falls noch nicht gesendet
                if now_ts > end_ts and (dumped is None or int(dumped or 0) == 0) and status in ("running",):
                    # Dump generieren und senden
                    await self._send_window_dump(g, start_ts, end_ts)
                    with _conn() as c:
                        c.execute("UPDATE clip_windows SET status='dumped', dump_sent_ts=? WHERE id=?", (now_ts, int(w["id"])))
                # automatisch: nächstes Fenster wird durch _ensure_window() beim nächsten Tick abgedeckt
        except Exception:
            log.exception("Wochenfenster-Manager-Task fehlgeschlagen")

    @weekly_window_manager.before_loop
    async def _wait_ready2(self):
        await self.bot.wait_until_ready()

    async def _send_window_dump(self, guild: discord.Guild, start_ts: int, end_ts: int, target_channel: Optional[discord.TextChannel] = None):
        """Baut TXT-Dump und sendet ihn an SEND_TO_USER_ID (oder target_channel, wenn angegeben)."""
        rows = _fetchall(
            """
            SELECT id, user_id, link, credit, permission, info, created_at
              FROM clip_submissions
             WHERE guild_id=? AND strftime('%s', created_at) BETWEEN ? AND ?
             ORDER BY datetime(created_at) ASC
            """,
            (guild.id, str(start_ts), str(end_ts))
        )
        text_lines = [
            f"# Deadlock Clips – Fenster {datetime.fromtimestamp(start_ts, TZ):%Y-%m-%d %H:%M} → {datetime.fromtimestamp(end_ts, TZ):%Y-%m-%d %H:%M} ({WINDOW_TZ})",
            f"# Guild: {guild.name} ({guild.id})",
            "",
            "id | user_id | created_at | credit | link | permission | info",
            "-"*120
        ]
        for r in rows:
            created = r["created_at"]
            line = f"{r['id']} | {r['user_id']} | {created} | {r['credit']} | {r['link']} | {r['permission']} | {(r['info'] or '').replace(chr(10),' ')}"
            text_lines.append(line)

        content = "\n".join(text_lines) if rows else "# (keine Einsendungen in diesem Fenster)"
        data = io.BytesIO(content.encode("utf-8"))
        filename = f"deadlock_clips_{guild.id}_{start_ts}_{end_ts}.txt"
        file = discord.File(data, filename=filename)

        if target_channel and isinstance(target_channel, discord.TextChannel):
            await target_channel.send(content="📦 **Wochen-Dump (Clips)**", file=file)
            return

        if SEND_TO_USER and SEND_TO_USER_ID:
            try:
                user = self.bot.get_user(SEND_TO_USER_ID) or await self.bot.fetch_user(SEND_TO_USER_ID)
                await user.send(content=f"📦 **Wochen-Dump (Clips)** – {guild.name}", file=file)
            except Exception as exc:
                log.warning("Konnte Wochen-Dump nicht als DM senden: %s", exc)
                # Fallback: in den Submit-Channel posten
                ch = guild.get_channel(SUBMIT_CHANNEL_ID) or await guild.fetch_channel(SUBMIT_CHANNEL_ID)
                if isinstance(ch, discord.TextChannel):
                    await ch.send(content="📦 **Wochen-Dump (Clips)**", file=file)

    # ---------- Lifecycle ----------

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
                    log.exception("Konnte Clip-Interface für Guild %s beim Start nicht aktualisieren", g.id)
            return

        for g in self.bot.guilds:
            try:
                await self.upsert_interface(g)
            except Exception:
                log.exception("Konnte Clip-Interface für Guild %s nicht aktualisieren", g.id)

    # ---------- Commands ----------

    @app_commands.command(name="clips_repost", description="Interface-Nachricht erneut erstellen/aktualisieren.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clips_repost(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Nur in einem Server nutzbar.", ephemeral=True)
            return

        msg_id = await self.upsert_interface(guild)
        if msg_id:
            await interaction.response.send_message(f"✅ Interface aktiv. MessageID: `{msg_id}`", ephemeral=True)
        else:
            await interaction.response.send_message(
                "❌ Konnte Interface nicht posten – prüfe `SUBMIT_CHANNEL_ID` und Bot-Rechte.",
                ephemeral=True,
            )

    # Gruppe
    clips_group = app_commands.Group(name="clips", description="Clips & Zeitfenster")

    @clips_group.command(name="dump", description="Erzeuge einen TXT-Dump für einen Zeitraum (beeinflusst das Wochenfenster NICHT).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clips_dump(self,
                         interaction: discord.Interaction,
                         start_ts: Optional[int] = None,
                         end_ts: Optional[int] = None,
                         to_channel: Optional[discord.TextChannel] = None):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Nur im Server nutzbar.", ephemeral=True)
            return

        # Default: aktuelles Wochenfenster
        if start_ts is None or end_ts is None:
            s, e = _compute_week_window_berlin(datetime.now(tz=TZ))
            start_ts = start_ts or s
            end_ts = end_ts or e

        await self._send_window_dump(guild, int(start_ts), int(end_ts), target_channel=to_channel)
        await interaction.response.send_message(
            f"📦 Dump angefordert: {_format_ts(int(start_ts),'f')} → {_format_ts(int(end_ts),'f')}",
            ephemeral=True
        )

    @clips_group.command(name="winner_draw", description="Zufallsgewinner aus einem Zeitraum ziehen (reiner Test, ohne Fensterlogik).")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def clips_winner_draw(self,
                                interaction: discord.Interaction,
                                start_ts: Optional[int] = None,
                                end_ts: Optional[int] = None):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("Nur im Server nutzbar.", ephemeral=True)
            return

        # Zeitraum default = aktuelles Wochenfenster
        if start_ts is None or end_ts is None:
            s, e = _compute_week_window_berlin(datetime.now(tz=TZ))
            start_ts = start_ts or s
            end_ts = end_ts or e

        rows = _fetchall(
            """
            SELECT DISTINCT user_id
              FROM clip_submissions
             WHERE guild_id=? AND strftime('%s', created_at) BETWEEN ? AND ?
            """,
            (guild.id, str(int(start_ts)), str(int(end_ts)))
        )
        users = [int(r[0]) for r in rows]
        if not users:
            await interaction.response.send_message("Keine Teilnehmenden im Zeitraum.", ephemeral=True)
            return

        winner_id = random.choice(users)
        await interaction.response.send_message(f"🏆 Gewinner (Test): <@{winner_id}>", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ClipSubmissionCog(bot))
