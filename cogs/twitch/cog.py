# ============================
# 🛠️ CONFIG — EDIT HERE
# ============================
# ⚠️ Secrets (Client-ID/Secret) KOMMEN NICHT HIER REIN, sondern aus ENV (siehe unten)!
TWITCH_DASHBOARD_NOAUTH = True                     # ohne Token (nur lokal empfohlen)
TWITCH_DASHBOARD_HOST = "127.0.0.1"
TWITCH_DASHBOARD_PORT = 8765

TWITCH_LANGUAGE = "de"
TWITCH_TARGET_GAME_NAME = "Deadlock"
TWITCH_REQUIRED_DISCORD_MARKER = ""                # optionaler Marker im Profiltext (zusätzlich zur Discord-URL)

# Benachrichtigungskanäle
TWITCH_NOTIFY_CHANNEL_ID = 1304169815505637458     # Live-Postings (optional global)
TWITCH_ALERT_CHANNEL_ID  = 1374364800817303632     # Warnungen (30d Re-Check)
TWITCH_ALERT_MENTION     = "<@USER_OR_ROLE_ID>"    # z. B. <@123> oder <@&ROLEID>

# Öffentlicher Statistik-Kanal (nur dort reagiert !twl)
TWITCH_STATS_CHANNEL_ID  = 1428062025145385111

# Stats/Sampling: alle N Ticks (Tick=60s) in DB loggen
TWITCH_LOG_EVERY_N_TICKS = 5

# Zusätzliche Streams aus der Deadlock-Kategorie für Statistiken loggen (Maximalanzahl je Tick)
TWITCH_CATEGORY_SAMPLE_LIMIT = 400

# Invite-Refresh alle X Stunden
INVITES_REFRESH_INTERVAL_HOURS = 12

# ============================
# 🔒 SECRETS — aus ENV
# ============================
#   set TWITCH_CLIENT_ID und TWITCH_CLIENT_SECRET im System/Hosting
#   (nicht im Code hardcoden — CWE/OWASP)
# ============================

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

import discord
from discord.ext import commands, tasks
from aiohttp import ClientError, web

from .twitch_api import TwitchAPI
from . import storage
from .dashboard import Dashboard

log = logging.getLogger("TwitchStreams")

# Spiel-Kategorie, die als „Deadlock-Stream“ gilt (Standard: Deadlock)
TARGET_GAME_NAME = TWITCH_TARGET_GAME_NAME


class TwitchStreamCog(commands.Cog):
    """Monitor Twitch streamers and post go-live messages for the target game."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # 🔒 Secrets nur aus ENV (nicht hardcoden!)
        self.client_id = os.getenv("TWITCH_CLIENT_ID") or ""
        self.client_secret = os.getenv("TWITCH_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            log.error("TWITCH_CLIENT_ID/SECRET not configured; cog disabled")
            self.api = None
            return

        self.api = TwitchAPI(self.client_id, self.client_secret)
        self._category_id: Optional[str] = None
        self._language_filter = (TWITCH_LANGUAGE or "").strip() or None

        # Dashboard/Auth (aus Config-Header)
        self._dashboard_token = os.getenv("TWITCH_DASHBOARD_TOKEN") or None
        self._dashboard_noauth = bool(TWITCH_DASHBOARD_NOAUTH)
        self._dashboard_host = TWITCH_DASHBOARD_HOST or ("127.0.0.1" if self._dashboard_noauth else "0.0.0.0")
        self._dashboard_port = int(TWITCH_DASHBOARD_PORT)
        self._partner_dashboard_token = os.getenv("TWITCH_PARTNER_TOKEN") or None
        self._required_marker_default = TWITCH_REQUIRED_DISCORD_MARKER or None

        # Channels/Alerts
        self._notify_channel_id = int(TWITCH_NOTIFY_CHANNEL_ID or 0)
        self._alert_channel_id = int(TWITCH_ALERT_CHANNEL_ID or 0)
        self._alert_mention = TWITCH_ALERT_MENTION or ""

        # Stats logging cadence
        self._tick_count = 0
        self._log_every_n = max(1, int(TWITCH_LOG_EVERY_N_TICKS or 5))
        self._category_sample_limit = max(50, int(TWITCH_CATEGORY_SAMPLE_LIMIT or 400))

        # Dashboard
        self._web: Optional[web.AppRunner] = None
        self._web_app: Optional[web.Application] = None

        # Invite-Cache: {guild_id: {code, ...}}
        self._invite_codes: Dict[int, Set[str]] = {}

        # Background tasks
        self.poll_streams.start()
        self.invites_refresh.start()
        self.bot.loop.create_task(self._ensure_category_id())
        self.bot.loop.create_task(self._start_dashboard())
        self.bot.loop.create_task(self._refresh_all_invites())

    def cog_unload(self):
        """Sauberer Shutdown ohne leere except-Blöcke (CWE-390/703-freundlich)."""
        loops = (self.poll_streams, self.invites_refresh)

        async def _graceful_shutdown():
            # 1) laufende Tasks abbrechen
            for lp in loops:
                try:
                    if lp.is_running():
                        lp.cancel()
                except Exception:
                    log.exception("Konnte Loop nicht canceln: %r", lp)
            await asyncio.sleep(0)

            # 2) Dashboard herunterfahren
            if self._web:
                try:
                    await self._stop_dashboard()
                except Exception:
                    log.exception("Dashboard shutdown fehlgeschlagen")

            # 3) HTTP-Session schließen (aiohttp)
            if getattr(self, "api", None):
                try:
                    await self.api.aclose()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.exception("TwitchAPI-Session konnte nicht geschlossen werden")

        try:
            self.bot.loop.create_task(_graceful_shutdown())
        except Exception:
            log.exception("Fehler beim Start des Shutdown-Tasks")

        try:
            existing = self.bot.get_command("twl")
            if existing and getattr(existing, "cog", None) is self:
                self.bot.remove_command(existing.name)
        except Exception:
            log.exception("Konnte !twl-Command nicht deregistrieren")

    # ---------- Dashboard (aiohttp) ----------
    async def _start_dashboard(self):
        if self._web is not None:
            return
        self._web_app = web.Application()

        async def add(login: str, require_link: bool):
            return await self._cmd_add(login, require_link)

        async def remove(login: str):
            await self._cmd_remove(login)

        async def list_items():
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT twitch_login, manual_verified_permanent, manual_verified_until, manual_verified_at "
                    "FROM twitch_streamers ORDER BY twitch_login"
                ).fetchall()
                return [dict(r) for r in rows]

        async def stats_cb():
            return await self._compute_stats()

        async def export_cb():
            with storage.get_conn() as c:
                rows = c.execute("SELECT * FROM twitch_stream_logs ORDER BY ts DESC LIMIT 10000").fetchall()
                return {"logs": [dict(r) for r in rows]}

        async def export_csv_cb():
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT ts, streamer_login, viewers, title, started_at, language, game_name, is_tracked FROM twitch_stream_logs ORDER BY ts"
                ).fetchall()
            out = ["Timestamp,Streamer,Viewers,Title,Started_At,Language,Game,Is_Tracked\n"]
            for r in rows:
                title = (r["title"] or "").replace('"', '""').replace("\n", " ")
                out.append(
                    f'"{r["ts"]}","{r["streamer_login"]}",{r["viewers"] or 0},"{title}","{r["started_at"]}","{r["language"] or ""}","{r["game_name"] or ""}",{int(r["is_tracked"] or 0)}\n'
                )
            return "".join(out)

        async def verify_cb(login: str, mode: str) -> str:
            return await self._set_manual_verification(login, mode)

        Dashboard(
            app_token=self._dashboard_token,
            noauth=self._dashboard_noauth,
            partner_token=self._partner_dashboard_token,
            add_cb=add,
            remove_cb=remove,
            list_cb=list_items,
            stats_cb=stats_cb,
            export_cb=export_cb,
            export_csv_cb=export_csv_cb,
            verify_cb=verify_cb,
        ).attach(self._web_app)

        runner = web.AppRunner(self._web_app)
        await runner.setup()
        site = web.TCPSite(runner, self._dashboard_host, self._dashboard_port)
        await site.start()
        self._web = runner
        log.info("Twitch dashboard running on http://%s:%d/twitch", self._dashboard_host, self._dashboard_port)
        if self._dashboard_noauth and self._dashboard_host != "127.0.0.1":
            log.warning("Dashboard without auth and not bound to localhost — restrict access!")

    async def _stop_dashboard(self):
        try:
            if self._web:
                await self._web.cleanup()
        finally:
            self._web = None
            self._web_app = None

    # ---------- Invite-Cache ----------
    @tasks.loop(hours=INVITES_REFRESH_INTERVAL_HOURS)
    async def invites_refresh(self):
        await self._refresh_all_invites()

    async def _refresh_all_invites(self):
        for g in self.bot.guilds:
            await self._refresh_guild_invites(g)

    async def _refresh_guild_invites(self, guild: discord.Guild):
        try:
            invites = await guild.invites()  # Manage Guild / Admin notwendig
            codes = {i.code for i in invites if i.code}
        except Exception as e:
            log.warning("cannot fetch invites for guild %s: %s", guild.id, e)
            codes = set()
        # Vanity-Invite ergänzen (falls vorhanden)
        try:
            v = await guild.vanity_invite()
            if v and v.code:
                codes.add(v.code)
        except Exception:
            pass
        self._invite_codes[guild.id] = codes
        log.debug("invite cache for %s: %d codes", guild.id, len(codes))

    # ---------- DB helpers ----------
    def _get_settings(self, guild_id: int) -> Optional[dict]:
        with storage.get_conn() as c:
            r = c.execute("SELECT * FROM twitch_settings WHERE guild_id=?", (guild_id,)).fetchone()
            return dict(r) if r else None

    def _set_channel(self, guild_id: int, channel_id: int):
        with storage.get_conn() as c:
            c.execute(
                "INSERT INTO twitch_settings (guild_id, channel_id, language_filter, required_marker) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
                (guild_id, channel_id, self._language_filter, self._required_marker_default),
            )

    async def _set_manual_verification(self, login: str, mode: str) -> str:
        normalized = self._normalize_login(login)
        if not normalized:
            return "Ungültiger Twitch-Login"

        with storage.get_conn() as c:
            existing = c.execute(
                "SELECT twitch_login FROM twitch_streamers WHERE twitch_login=?",
                (normalized,),
            ).fetchone()

        if not existing:
            return f"{normalized} ist nicht in der Liste"

        now = datetime.now(timezone.utc)

        if mode == "permanent":
            with storage.get_conn() as c:
                c.execute(
                    "UPDATE twitch_streamers SET manual_verified_permanent=1, manual_verified_until=NULL, manual_verified_at=? WHERE twitch_login=?",
                    (now.isoformat(), normalized),
                )
            return f"{normalized} dauerhaft verifiziert"

        if mode == "temp":
            until = now + timedelta(days=30)
            with storage.get_conn() as c:
                c.execute(
                    "UPDATE twitch_streamers SET manual_verified_permanent=0, manual_verified_until=?, manual_verified_at=? WHERE twitch_login=?",
                    (until.isoformat(), now.isoformat(), normalized),
                )
            return f"{normalized} für 30 Tage verifiziert"

        if mode == "clear":
            with storage.get_conn() as c:
                c.execute(
                    "UPDATE twitch_streamers SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL WHERE twitch_login=?",
                    (normalized,),
                )
            return f"Verifizierung für {normalized} zurückgesetzt"

        return "Unbekannter Verifizierungsmodus"

    def _format_list_line(self, row: dict) -> str:
        login = row.get("twitch_login", "")
        if row.get("manual_verified_permanent"):
            status = "permanent verifiziert"
        else:
            until = row.get("manual_verified_until")
            if until:
                try:
                    dt = datetime.fromisoformat(str(until))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    status = f"verifiziert bis {dt.date().isoformat()}"
                except ValueError:
                    status = f"verifiziert bis {until}"
            else:
                status = "nicht verifiziert"
        return f"• {login} — {status}"

    def _is_verified_now(self, row: dict) -> bool:
        if row.get("manual_verified_permanent"):
            return True
        until = row.get("manual_verified_until")
        if not until:
            return False
        try:
            dt = datetime.fromisoformat(str(until))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt >= datetime.now(timezone.utc)

    # ---------- Commands (hybrid) ----------
    @commands.hybrid_group(name="twitch", with_app_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("Subcommands: add, remove, list, channel, forcecheck, invites")

    @twitch_group.command(name="channel")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_channel(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        channel = channel or ctx.channel
        self._set_channel(ctx.guild.id, channel.id)
        await ctx.reply(f"Live-Posts gehen jetzt in {channel.mention}")

    async def _cmd_add(self, login: str, require_link: bool) -> str:
        assert self.api
        normalized = self._normalize_login(login)
        if not normalized:
            return "Ungültiger Twitch-Login"

        users = await self.api.get_users([normalized])
        u = users.get(normalized)
        if not u:
            return "Unbekannter Twitch-Login"
        with storage.get_conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO twitch_streamers (twitch_login, twitch_user_id, require_discord_link, next_link_check_at) VALUES (?, ?, ?, datetime('now','+30 days'))",
                (u["login"].lower(), u["id"], int(require_link)),
            )
        with storage.get_conn() as c:
            c.execute(
                "UPDATE twitch_streamers SET manual_verified_permanent=0, manual_verified_until=NULL, manual_verified_at=NULL WHERE twitch_login=?",
                (normalized,),
            )
        return f"{u['display_name']} hinzugefügt"

    async def _cmd_remove(self, login: str) -> str:
        normalized = self._normalize_login(login)
        if not normalized:
            return "Ungültiger Twitch-Login"

        with storage.get_conn() as c:
            cur = c.execute("DELETE FROM twitch_streamers WHERE twitch_login=?", (normalized,))
            deleted = cur.rowcount or 0
            c.execute("DELETE FROM twitch_live_state WHERE streamer_login=?", (normalized,))

        if deleted:
            return f"{normalized} entfernt"
        return f"{normalized} war nicht gespeichert"

    @twitch_group.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_add(self, ctx: commands.Context, login: str, require_discord_link: Optional[bool] = False):
        msg = await self._cmd_add(login, bool(require_discord_link))
        await ctx.reply(msg)

    @twitch_group.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_remove(self, ctx: commands.Context, login: str):
        msg = await self._cmd_remove(login)
        await ctx.reply(msg)

    @twitch_group.command(name="list")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_list(self, ctx: commands.Context):
        with storage.get_conn() as c:
            rows = c.execute(
                "SELECT twitch_login, manual_verified_permanent, manual_verified_until FROM twitch_streamers ORDER BY twitch_login"
            ).fetchall()
        if not rows:
            await ctx.reply("Keine Streamer gespeichert.")
            return
        lines = [
            self._format_list_line(dict(r))
            for r in rows
        ]
        await ctx.reply("\n".join(lines)[:1900])

    @twitch_group.command(name="forcecheck")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_forcecheck(self, ctx: commands.Context):
        await ctx.reply("Prüfe jetzt…")
        await self._tick()

    @twitch_group.command(name="invites")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_invites(self, ctx: commands.Context):
        await self._refresh_guild_invites(ctx.guild)
        codes = sorted(self._invite_codes.get(ctx.guild.id, set()))
        if not codes:
            await ctx.reply("Keine aktiven Einladungen gefunden.")
        else:
            urls = [f"https://discord.gg/{c}" for c in codes]
            await ctx.reply("Aktive Einladungen:\n" + "\n".join(urls)[:1900])

    @commands.command(name="twl")
    async def twitch_leaderboard(self, ctx: commands.Context, *, filters: str = ""):
        """Zeigt Twitch-Statistiken im Partner-Kanal an."""

        if ctx.channel.id != TWITCH_STATS_CHANNEL_ID:
            channel_hint = f"<#{TWITCH_STATS_CHANNEL_ID}>"
            await ctx.reply(f"Dieser Befehl kann nur in {channel_hint} verwendet werden.")
            return

        if filters.strip().lower() in {"help", "?", "hilfe"}:
            help_text = (
                "Verwendung: !twl [samples=Zahl] [avg=Zahl] [partner=only|exclude|any] [limit=Zahl]\n"
                "Beispiel: !twl samples=15 avg=25 partner=only"
            )
            await ctx.reply(help_text)
            return

        min_samples: Optional[int] = None
        min_avg: Optional[float] = None
        partner_filter = "any"
        limit = 5

        for token in filters.split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            key = key.lower().strip()
            value = value.strip()
            if key in {"samples", "min_samples"}:
                try:
                    parsed = max(0, int(value))
                except ValueError:
                    continue
                min_samples = parsed
            elif key in {"avg", "min_avg", "avg_viewers"}:
                try:
                    parsed_avg = max(0.0, float(value))
                except ValueError:
                    continue
                min_avg = parsed_avg
            elif key == "partner":
                lowered = value.lower()
                if lowered in {"only", "exclude", "any", "all"}:
                    partner_filter = "any" if lowered in {"any", "all"} else lowered
            elif key == "limit":
                try:
                    limit_val = max(1, min(20, int(value)))
                except ValueError:
                    continue
                limit = limit_val

        try:
            stats = await self._compute_stats()
        except Exception as exc:
            log.exception("!twl stats fetch failed: %s", exc)
            await ctx.reply("Konnte Statistiken nicht laden.")
            return

        tracked_items = stats.get("tracked", {}).get("top", [])
        category_items = stats.get("category", {}).get("top", [])

        tracked_filtered = self._filter_stats_items(
            tracked_items,
            min_samples=min_samples,
            min_avg_viewers=min_avg,
            partner_filter=partner_filter,
        )[:limit]
        category_filtered = self._filter_stats_items(
            category_items,
            min_samples=min_samples,
            min_avg_viewers=min_avg,
            partner_filter=partner_filter,
        )[:limit]

        filter_parts = []
        if min_samples is not None:
            filter_parts.append(f"Samples ≥ {min_samples}")
        if min_avg is not None:
            filter_parts.append(f"Ø Viewer ≥ {min_avg:.1f}")
        if partner_filter == "only":
            filter_parts.append("nur Partner")
        elif partner_filter == "exclude":
            filter_parts.append("ohne Partner")

        if not filter_parts:
            filter_parts.append("keine Filter")

        def _format_lines(title: str, items: List[dict]) -> List[str]:
            if not items:
                return [f"**{title}:** keine Daten für die aktuellen Filter."]
            lines = [f"**{title}:**"]
            for idx, item in enumerate(items, start=1):
                streamer = item.get("streamer") or "?"
                avg_viewers = float(item.get("avg_viewers") or 0.0)
                samples = int(item.get("samples") or 0)
                peak = int(item.get("max_viewers") or 0)
                partner_flag = " (Partner)" if item.get("is_partner") else ""
                lines.append(
                    f"{idx}. {streamer} — Ø {avg_viewers:.1f} Viewer (Samples: {samples}, Peak: {peak}){partner_flag}"
                )
            return lines

        response_lines = ["Filter: " + ", ".join(filter_parts)]
        response_lines.extend(_format_lines("Top Tracked", tracked_filtered))
        response_lines.extend(_format_lines("Top Kategorie", category_filtered))

        await ctx.reply("\n".join(response_lines)[:1900])

    # ---------- Polling & Posting ----------
    @staticmethod
    def _normalize_login(raw: str) -> str:
        login = (raw or "").strip()
        if not login:
            return ""

        login = login.split("?")[0].split("#")[0].strip()

        lowered = login.lower()
        if "twitch.tv" in lowered:
            if "//" not in login:
                login = f"https://{login}"
            try:
                parsed = urlparse(login)
                path = (parsed.path or "").strip("/")
                if path:
                    login = path.split("/")[0]
                else:
                    login = ""
            except Exception:
                login = ""

        login = login.strip().lstrip("@")
        login = login.lower()

        if not re.fullmatch(r"[a-z0-9_]+", login):
            return ""
        return login

    async def _ensure_category_id(self):
        if not self.api:
            return
        try:
            self._category_id = await self.api.get_category_id(TARGET_GAME_NAME)
            if self._category_id:
                log.info("Deadlock category_id = %s", self._category_id)
            else:
                log.warning("Deadlock category not found via Search Categories; fallback by game_name.")
        except Exception as e:
            log.error("could not resolve category id: %r", e)

    async def _fetch_category_streams(self) -> List[dict]:
        """Hole eine Liste aller Live-Streams in der Deadlock-Kategorie."""
        assert self.api
        try:
            streams = await self.api.get_streams_for_game(
                game_id=self._category_id,
                game_name=TARGET_GAME_NAME,
                language=self._language_filter,
                limit=self._category_sample_limit,
            )
        except Exception as e:
            log.debug("category stream fetch failed: %s", e)
            streams = []
        return streams

    def _log_stream_samples(self, streams: List[dict], tracked_logins: Set[str]) -> None:
        if not streams:
            return
        tracked = {login.lower() for login in tracked_logins}
        with storage.get_conn() as c:
            for s in streams:
                login = (s.get("user_login") or "").lower()
                c.execute(
                    "INSERT INTO twitch_stream_logs (streamer_login, user_id, title, viewers, started_at, language, game_id, game_name, is_tracked) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        login or s.get("user_login"),
                        s.get("user_id"),
                        s.get("title"),
                        s.get("viewer_count"),
                        s.get("started_at"),
                        s.get("language"),
                        s.get("game_id"),
                        s.get("game_name"),
                        1 if login in tracked else 0,
                    ),
                )

    @tasks.loop(seconds=60.0)
    async def poll_streams(self):
        try:
            await self._tick()
        except Exception as e:
            log.warning("tick failed: %s", e)

    async def _tick(self):
        if not self.api:
            return
        # Liste der beobachteten Logins
        with storage.get_conn() as c:
            rows = c.execute(
                "SELECT twitch_login, twitch_user_id, require_discord_link, manual_verified_permanent, manual_verified_until FROM twitch_streamers"
            ).fetchall()
        if not rows:
            return
        logins = [r["twitch_login"] for r in rows]
        require_flags = {r["twitch_login"].lower(): bool(r["require_discord_link"]) for r in rows}
        verification_state = {
            r["twitch_login"].lower(): self._is_verified_now(dict(r)) for r in rows
        }
        tracked_logins = {login.lower() for login in logins}

        self._tick_count += 1
        should_log = self._tick_count % self._log_every_n == 0

        # Streams (live) holen
        streams = await self.api.get_streams(
            user_logins=logins, game_id=self._category_id, language=self._language_filter
        )
        if not self._category_id and streams:
            streams = [s for s in streams if (s.get("game_name") or "").lower() == TARGET_GAME_NAME.lower()]

        # Fallback: wenn keine Streams mit Kategorie gefunden wurden, erneut ohne Kategorie filtern
        if not streams and logins:
            try:
                fallback_streams = await self.api.get_streams(user_logins=logins, language=self._language_filter)
                streams = [s for s in fallback_streams if (s.get("game_name") or "").lower() == TARGET_GAME_NAME.lower()]
            except Exception as e:
                log.debug("fallback get_streams (ohne Kategorie) failed: %s", e)

        live_by_login = {s.get("user_login", "").lower(): s for s in streams}

        # aktueller State
        with storage.get_conn() as c:
            states = {r["streamer_login"].lower(): dict(r) for r in c.execute("SELECT * FROM twitch_live_state").fetchall()}

        now_live: List[str] = []
        now_offline: List[str] = []

        for login in logins:
            login_l = login.lower()
            is_live = login_l in live_by_login
            st = states.get(login_l)

            if is_live:
                if require_flags.get(login_l, False) and not verification_state.get(login_l, False):
                    continue  # solange Linkpflicht nicht erfüllt, nix posten
                s = live_by_login[login_l]
                stream_id = s.get("id")
                started_at = s.get("started_at")
                title = s.get("title")

                if not st or not st.get("is_live") or st.get("last_stream_id") != stream_id:
                    now_live.append(login_l)
                with storage.get_conn() as c:
                    c.execute(
                        "INSERT INTO twitch_live_state (twitch_user_id, streamer_login, last_stream_id, last_started_at, last_title, last_game_id, is_live)"
                        " VALUES (?, ?, ?, ?, ?, ?, 1)"
                        " ON CONFLICT(twitch_user_id) DO UPDATE SET last_stream_id=excluded.last_stream_id, last_started_at=excluded.last_started_at, last_title=excluded.last_title, last_game_id=excluded.last_game_id, is_live=1",
                        (s.get("user_id"), login_l, stream_id, started_at, title, s.get("game_id")),
                    )
            else:
                if st and st.get("is_live"):
                    now_offline.append(login_l)
                with storage.get_conn() as c:
                    c.execute("UPDATE twitch_live_state SET is_live=0 WHERE streamer_login=?", (login_l,))

        if now_live:
            await self._post_go_live(now_live, live_by_login)
        if now_offline:
            await self._mark_offline(now_offline)

        # periodisches Logging (Stats)
        category_streams: List[dict] = []
        if should_log:
            category_streams = await self._fetch_category_streams()
            if category_streams:
                self._log_stream_samples(category_streams, tracked_logins)
            elif streams:
                # Fallback: wenigstens die beobachteten Streams loggen
                self._log_stream_samples(streams, tracked_logins)

    async def _post_go_live(self, logins: List[str], live_by_login: Dict[str, dict]):
        """
        Postet Go-Live in:
          - globalem Channel (TWITCH_NOTIFY_CHANNEL_ID), falls gesetzt — EINMAL
          - sonst pro Guild in dem dort konfigurierten Channel
        """
        # 1) Globaler Kanal?
        if self._notify_channel_id:
            ch = self.bot.get_channel(self._notify_channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                await self._post_to_channel(ch, logins, live_by_login)
            return  # wichtig: nicht pro Guild erneut posten

        # 2) Pro Guild
        for g in self.bot.guilds:
            settings = self._get_settings(g.id)
            channel = g.get_channel(int(settings["channel_id"])) if settings else None
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                await self._post_to_channel(channel, logins, live_by_login)

    async def _post_to_channel(self, channel: discord.abc.Messageable, logins: List[str], live_by_login: Dict[str, dict]):
        for login in logins:
            s = live_by_login[login]
            embed = discord.Embed(
                title=f"{s.get('user_name')} ist LIVE in Deadlock!",
                description=s.get("title") or "",
                colour=discord.Colour.purple(),
            )
            thumb = (s.get("thumbnail_url") or "").replace("{width}", "640").replace("{height}", "360")
            thumb_cache_buster = f"?t={int(datetime.now(timezone.utc).timestamp())}"
            file: Optional[discord.File] = None
            if thumb:
                thumb_with_cache_buster = f"{thumb}{thumb_cache_buster}"
                if self.api:
                    try:
                        session = self.api.get_http_session()
                        async with session.get(thumb_with_cache_buster) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                filename = f"{login}_preview.jpg"
                                file = discord.File(BytesIO(data), filename=filename)
                                embed.set_image(url=f"attachment://{filename}")
                            else:
                                embed.set_image(url=thumb_with_cache_buster)
                    except ClientError:
                        embed.set_image(url=thumb_with_cache_buster)
                    except Exception:
                        log.exception("preview fetch failed for %s", login)
                        embed.set_image(url=thumb_with_cache_buster)
                else:
                    embed.set_image(url=thumb_with_cache_buster)
            embed.add_field(name="Viewer", value=str(s.get("viewer_count")))
            embed.add_field(name="Kategorie", value=s.get("game_name") or "Deadlock", inline=True)
            url = f"https://twitch.tv/{login}"
            embed.add_field(name="Link", value=url, inline=False)
            try:
                view = discord.ui.View()
                view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Auf Twitch ansehen", url=url))
                kwargs = {
                    "content": f"🔴 **{s.get('user_name')}** ist live: {url}",
                    "embed": embed,
                    "view": view,
                }
                if file:
                    kwargs["file"] = file
                msg = await channel.send(**kwargs)
                with storage.get_conn() as c:
                    c.execute(
                        "UPDATE twitch_live_state SET last_discord_message_id=?, last_notified_at=CURRENT_TIMESTAMP WHERE streamer_login=?",
                        (str(msg.id), login),
                    )
            except Exception as e:
                log.warning("failed to post go-live for %s: %s", login, e)

    async def _mark_offline(self, logins: List[str]):
        """
        Markiert letzte Live-Nachricht als „beendet“ – analog zu _post_go_live:
        - globaler Channel einmal, sonst pro Guild.
        """
        # 1) Global?
        if self._notify_channel_id:
            ch = self.bot.get_channel(self._notify_channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                await self._mark_offline_in_channel(ch, logins)
            return

        # 2) Pro Guild
        for g in self.bot.guilds:
            settings = self._get_settings(g.id)
            if not settings:
                continue
            ch = g.get_channel(int(settings["channel_id"]))
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                await self._mark_offline_in_channel(ch, logins)

    async def _mark_offline_in_channel(self, ch: discord.abc.Messageable, logins: List[str]):
        with storage.get_conn() as c:
            qmarks = ",".join(["?" for _ in logins])
            rows = c.execute(
                f"SELECT streamer_login, last_discord_message_id FROM twitch_live_state WHERE streamer_login IN ({qmarks})",
                tuple(logins),
            ).fetchall()
        for r in rows:
            mid = r["last_discord_message_id"]
            if not mid:
                continue
            try:
                msg = await ch.fetch_message(int(mid))  # type: ignore[attr-defined]
                await msg.edit(content=(msg.content + " (beendet)"))  # type: ignore[attr-defined]
            except Exception as e:
                log.debug("cannot edit message %s: %s", mid, e)

    # ---------- simple stats for dashboard ----------
    async def _compute_stats(self) -> dict:
        with storage.get_conn() as c:
            total_samples = c.execute("SELECT COUNT(*) AS c FROM twitch_stream_logs").fetchone()["c"]
            unique_streamers = c.execute("SELECT COUNT(DISTINCT streamer_login) AS c FROM twitch_stream_logs").fetchone()["c"]
            tracked_samples = (
                c.execute("SELECT COUNT(*) AS c FROM twitch_stream_logs WHERE is_tracked=1").fetchone()["c"]
            )
            tracked_unique = (
                c.execute(
                    "SELECT COUNT(DISTINCT streamer_login) AS c FROM twitch_stream_logs WHERE is_tracked=1"
                ).fetchone()["c"]
            )
            avg_all = c.execute(
                "SELECT AVG(COALESCE(viewers,0)) AS avg_v FROM twitch_stream_logs"
            ).fetchone()["avg_v"]
            avg_tracked = c.execute(
                "SELECT AVG(COALESCE(viewers,0)) AS avg_v FROM twitch_stream_logs WHERE is_tracked=1"
            ).fetchone()["avg_v"]
            top_tracked_rows = c.execute(
                "SELECT l.streamer_login, COUNT(*) AS samples, AVG(COALESCE(l.viewers,0)) AS avg_viewers, "
                "MAX(COALESCE(l.viewers,0)) AS max_viewers, "
                "MAX(COALESCE(s.manual_verified_permanent,0)) AS manual_verified_permanent, "
                "MAX(CASE WHEN s.manual_verified_until IS NULL THEN '' ELSE s.manual_verified_until END) AS manual_verified_until "
                "FROM twitch_stream_logs l "
                "LEFT JOIN twitch_streamers s ON s.twitch_login = l.streamer_login "
                "WHERE l.is_tracked=1 "
                "GROUP BY l.streamer_login "
                "ORDER BY avg_viewers DESC, max_viewers DESC"
            ).fetchall()
            top_category_rows = c.execute(
                "SELECT l.streamer_login, COUNT(*) AS samples, AVG(COALESCE(l.viewers,0)) AS avg_viewers, "
                "MAX(COALESCE(l.viewers,0)) AS max_viewers, "
                "MAX(COALESCE(s.manual_verified_permanent,0)) AS manual_verified_permanent, "
                "MAX(CASE WHEN s.manual_verified_until IS NULL THEN '' ELSE s.manual_verified_until END) AS manual_verified_until "
                "FROM twitch_stream_logs l "
                "LEFT JOIN twitch_streamers s ON s.twitch_login = l.streamer_login "
                "GROUP BY l.streamer_login "
                "ORDER BY avg_viewers DESC, max_viewers DESC"
            ).fetchall()

        def _rows_to_list(rows):
            items = []
            for r in rows:
                row_dict = dict(r)
                is_partner = False
                try:
                    is_partner = self._is_verified_now(row_dict)
                except Exception:
                    is_partner = False
                items.append(
                    {
                        "streamer": row_dict.get("streamer_login"),
                        "samples": row_dict.get("samples", 0),
                        "avg_viewers": round((row_dict.get("avg_viewers") or 0), 2),
                        "max_viewers": row_dict.get("max_viewers") or 0,
                        "is_partner": bool(is_partner),
                    }
                )
            return items

        return {
            "total_sessions": total_samples,
            "unique_streamers": unique_streamers,
            "avg_viewers_all": round(avg_all or 0, 2),
            "avg_viewers_tracked": round(avg_tracked or 0, 2),
            "tracked": {
                "samples": tracked_samples,
                "unique_streamers": tracked_unique,
                "top": _rows_to_list(top_tracked_rows),
            },
            "category": {
                "samples": total_samples,
                "unique_streamers": unique_streamers,
                "top": _rows_to_list(top_category_rows),
            },
        }

    @staticmethod
    def _filter_stats_items(
        items: List[dict],
        *,
        min_samples: Optional[int] = None,
        min_avg_viewers: Optional[float] = None,
        partner_filter: str = "any",
    ) -> List[dict]:
        partner_filter = (partner_filter or "any").lower()
        out: List[dict] = []
        for item in items:
            samples = int(item.get("samples") or 0)
            avg_viewers = float(item.get("avg_viewers") or 0.0)
            is_partner = bool(item.get("is_partner"))

            if min_samples is not None and samples < min_samples:
                continue
            if min_avg_viewers is not None and avg_viewers < min_avg_viewers:
                continue
            if partner_filter == "only" and not is_partner:
                continue
            if partner_filter == "exclude" and is_partner:
                continue

            out.append(item)
        return out
