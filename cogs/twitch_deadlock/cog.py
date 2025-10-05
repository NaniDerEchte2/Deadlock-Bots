# ============================
# 🛠️ CONFIG — EDIT HERE
# ============================
# ⚠️ Secrets (Client-ID/Secret) KOMMEN NICHT HIER REIN, sondern aus ENV (siehe unten)!
TWITCH_DASHBOARD_NOAUTH = True                     # ohne Token (nur lokal empfohlen)
TWITCH_DASHBOARD_HOST = "127.0.0.1"
TWITCH_DASHBOARD_PORT = 8765

TWITCH_LANGUAGE = "de"
TWITCH_DEADLOCK_NAME = "Deadlock"
TWITCH_REQUIRED_DISCORD_MARKER = ""                # optionaler Marker im Profiltext (zusätzlich zur Discord-URL)

# Benachrichtigungskanäle
TWITCH_NOTIFY_CHANNEL_ID = 1304169815505637458     # Live-Postings (optional global)
TWITCH_ALERT_CHANNEL_ID  = 1374364800817303632     # Warnungen (30d Re-Check)
TWITCH_ALERT_MENTION     = "<@USER_OR_ROLE_ID>"    # z. B. <@123> oder <@&ROLEID>

# Stats/Sampling: alle N Ticks (Tick=60s) in DB loggen
TWITCH_LOG_EVERY_N_TICKS = 5

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
from typing import Dict, List, Optional, Set, Tuple

import discord
from discord.ext import commands, tasks
from aiohttp import web

from .twitch_api import TwitchAPI
from . import storage
from .dashboard import Dashboard

log = logging.getLogger("TwitchDeadlock")

# Regex: Discord-Invite-URL -> Code
INVITE_URL_RE = re.compile(
    r"(?:https?://)?(?:discord(?:app)?\.com/invite|discord\.gg)/([A-Za-z0-9-]+)",
    re.I,
)

# Deadlock-Kategorie-Name (vom Config-Header)
DEADLOCK_GAME_NAME = TWITCH_DEADLOCK_NAME


class TwitchDeadlockCog(commands.Cog):
    """
    Postet Live-Messages nur, wenn beobachtete Streamer Deadlock streamen.
    Prüft Twitch-Profilbeschreibung auf Invite-Links und matcht sie gegen unsere(n) Discord-Server.
    Mit Dashboard (Tabs), optionalem Sprachfilter und 30d Re-Check.
    """

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
        self._required_marker_default = TWITCH_REQUIRED_DISCORD_MARKER or None

        # Channels/Alerts
        self._notify_channel_id = int(TWITCH_NOTIFY_CHANNEL_ID or 0)
        self._alert_channel_id = int(TWITCH_ALERT_CHANNEL_ID or 0)
        self._alert_mention = TWITCH_ALERT_MENTION or ""

        # Stats logging cadence
        self._tick_count = 0
        self._log_every_n = max(1, int(TWITCH_LOG_EVERY_N_TICKS or 5))

        # Dashboard
        self._web: Optional[web.AppRunner] = None
        self._web_app: Optional[web.Application] = None

        # Invite-Cache: {guild_id: {code, ...}} und {code: guild_id}
        self._invite_codes: Dict[int, Set[str]] = {}
        self._invite_code_cache: Dict[str, int] = {}

        # Background tasks
        self.poll_streams.start()
        self.link_reverify.start()
        self.invites_refresh.start()
        self.bot.loop.create_task(self._ensure_category_id())
        self.bot.loop.create_task(self._start_dashboard())
        self.bot.loop.create_task(self._refresh_all_invites())

    def cog_unload(self):
        """Sauberer Shutdown ohne leere except-Blöcke (CWE-390/703-freundlich)."""
        loops = (self.poll_streams, self.link_reverify, self.invites_refresh)

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
                    "SELECT twitch_login, require_discord_link, last_link_ok FROM twitch_streamers ORDER BY twitch_login"
                ).fetchall()
                return [dict(r) for r in rows]

        async def rescan():
            await self._rescan_all_links()

        async def stats_cb():
            return await self._compute_stats()

        async def export_cb():
            with storage.get_conn() as c:
                rows = c.execute("SELECT * FROM twitch_stream_logs ORDER BY ts DESC LIMIT 10000").fetchall()
                return {"logs": [dict(r) for r in rows]}

        async def export_csv_cb():
            with storage.get_conn() as c:
                rows = c.execute(
                    "SELECT ts, streamer_login, viewers, title, started_at, language, game_name FROM twitch_stream_logs ORDER BY ts"
                ).fetchall()
            out = ["Timestamp,Streamer,Viewers,Title,Started_At,Language,Game\n"]
            for r in rows:
                title = (r["title"] or "").replace('"', '""').replace("\n", " ")
                out.append(
                    f'"{r["ts"]}","{r["streamer_login"]}",{r["viewers"] or 0},"{title}","{r["started_at"]}","{r["language"] or ""}","{r["game_name"] or ""}"\n'
                )
            return "".join(out)

        Dashboard(
            app_token=self._dashboard_token,
            noauth=self._dashboard_noauth,
            add_cb=add,
            remove_cb=remove,
            list_cb=list_items,
            rescan_cb=rescan,
            stats_cb=stats_cb,
            export_cb=export_cb,
            export_csv_cb=export_csv_cb,
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

    def _extract_invite_codes(self, text: str) -> Set[str]:
        return {m.group(1) for m in INVITE_URL_RE.finditer(text or "")}

    async def _codes_match_our_guild(self, codes: Set[str]) -> bool:
        """True, wenn einer der Codes zu einer Guild gehört, in der unser Bot ist."""
        if not codes:
            return False

        our_guild_ids = {g.id for g in self.bot.guilds}

        # 1) Schnell: lokaler Cache
        for gid, known in self._invite_codes.items():
            if codes & known:
                return True

        # 2) Fallback: API-Auflösung (Code -> Guild-ID)
        for code in list(codes)[:5]:  # harte Obergrenze
            if code in self._invite_code_cache:
                if self._invite_code_cache[code] in our_guild_ids:
                    return True
                continue
            try:
                inv = await self.bot.fetch_invite(code)
                gid = getattr(getattr(inv, "guild", None), "id", None)
                if gid:
                    gid = int(gid)
                    self._invite_code_cache[code] = gid
                    if gid in our_guild_ids:
                        return True
            except Exception as e:
                log.debug("invite fetch failed for %s: %s", code, e)
            await asyncio.sleep(0.25)
        return False

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

    # ---------- Link check (Profil-Bio + Invite-Quervergleich) ----------
    async def _check_discord_link(self, login: str) -> bool:
        assert self.api
        users = await self.api.get_users([login])
        u = users.get(login.lower())
        if not u:
            return False
        desc = (u.get("description") or "").strip()

        codes = self._extract_invite_codes(desc)
        link_matches_our_guild = await self._codes_match_our_guild(codes)

        marker_ok = True
        if self._required_marker_default:
            marker_ok = self._required_marker_default.lower() in desc.lower()

        has_link_and_ok = bool(link_matches_our_guild and marker_ok)

        with storage.get_conn() as c:
            c.execute(
                "UPDATE twitch_streamers SET last_description=?, last_link_ok=?, "
                "last_link_checked_at=CURRENT_TIMESTAMP, next_link_check_at=datetime('now','+30 days') "
                "WHERE twitch_login=?",
                (desc[:4000], int(has_link_and_ok), login.lower()),
            )
        return has_link_and_ok

    async def _rescan_all_links(self):
        assert self.api
        with storage.get_conn() as c:
            rows = c.execute("SELECT twitch_login FROM twitch_streamers").fetchall()
        for r in rows:
            try:
                await self._check_discord_link(r["twitch_login"])
                await asyncio.sleep(0.2)
            except Exception as e:
                log.warning("rescan failed for %s: %s", r["twitch_login"], e)

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
        users = await self.api.get_users([login])
        u = users.get(login.lower())
        if not u:
            return "Unbekannter Twitch-Login"
        with storage.get_conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO twitch_streamers (twitch_login, twitch_user_id, require_discord_link, next_link_check_at) VALUES (?, ?, ?, datetime('now','+30 days'))",
                (u["login"].lower(), u["id"], int(require_link)),
            )
        try:
            await self._check_discord_link(login)
        except Exception as e:
            log.debug("initial link check failed for %s: %s", login, e)
        return f"{u['display_name']} hinzugefügt"

    async def _cmd_remove(self, login: str) -> str:
        with storage.get_conn() as c:
            c.execute("DELETE FROM twitch_streamers WHERE twitch_login=?", (login.lower(),))
            c.execute("DELETE FROM twitch_live_state WHERE streamer_login=?", (login.lower(),))
        return f"{login} entfernt"

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
                "SELECT twitch_login, require_discord_link, last_link_ok FROM twitch_streamers ORDER BY twitch_login"
            ).fetchall()
        if not rows:
            await ctx.reply("Keine Streamer gespeichert.")
            return
        lines = [
            f"• {r['twitch_login']}  (require_link={'ja' if r['require_discord_link'] else 'nein'}, has_link={'ja' if r['last_link_ok'] else 'nein'})"
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

    # ---------- Polling & Posting ----------
    async def _ensure_category_id(self):
        if not self.api:
            return
        try:
            self._category_id = await self.api.get_category_id(DEADLOCK_GAME_NAME)
            if self._category_id:
                log.info("Deadlock category_id = %s", self._category_id)
            else:
                log.warning("Deadlock category not found via Search Categories; fallback by game_name.")
        except Exception as e:
            log.error("could not resolve category id: %r", e)

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
                "SELECT twitch_login, twitch_user_id, require_discord_link, last_link_ok FROM twitch_streamers"
            ).fetchall()
        if not rows:
            return
        logins = [r["twitch_login"] for r in rows]
        require_map = {
            r["twitch_login"].lower(): (bool(r["require_discord_link"]), bool(r["last_link_ok"])) for r in rows
        }

        # Streams (live) holen
        streams = await self.api.get_streams(
            user_logins=logins, game_id=self._category_id, language=self._language_filter
        )
        if not self._category_id and streams:
            streams = [s for s in streams if (s.get("game_name") or "").lower() == DEADLOCK_GAME_NAME.lower()]

        # Fallback: wenn keine Streams mit Kategorie gefunden wurden, erneut ohne Kategorie filtern
        if not streams and logins:
            try:
                fallback_streams = await self.api.get_streams(user_logins=logins, language=self._language_filter)
                streams = [s for s in fallback_streams if (s.get("game_name") or "").lower() == DEADLOCK_GAME_NAME.lower()]
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
                req, has = require_map.get(login_l, (False, False))
                if req and not has:
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
        self._tick_count += 1
        if self._tick_count % self._log_every_n == 0 and streams:
            with storage.get_conn() as c:
                for s in streams:
                    c.execute(
                        "INSERT INTO twitch_stream_logs (streamer_login, user_id, title, viewers, started_at, language, game_id, game_name) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            s.get("user_login"),
                            s.get("user_id"),
                            s.get("title"),
                            s.get("viewer_count"),
                            s.get("started_at"),
                            s.get("language"),
                            s.get("game_id"),
                            s.get("game_name"),
                        ),
                    )

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
            if thumb:
                embed.set_image(url=thumb)
            embed.add_field(name="Viewer", value=str(s.get("viewer_count")))
            embed.add_field(name="Kategorie", value=s.get("game_name") or "Deadlock", inline=True)
            url = f"https://twitch.tv/{login}"
            embed.add_field(name="Link", value=url, inline=False)
            try:
                view = discord.ui.View()
                view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="Auf Twitch ansehen", url=url))
                msg = await channel.send(content=f"🔴 **{s.get('user_name')}** ist live: {url}", embed=embed, view=view)
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

    # ---------- 30-Tage-Check (täglich) ----------
    @tasks.loop(hours=24)
    async def link_reverify(self):
        if not self.api:
            return
        with storage.get_conn() as c:
            rows = c.execute(
                "SELECT twitch_login, require_discord_link FROM twitch_streamers WHERE require_discord_link=1 AND (next_link_check_at IS NULL OR next_link_check_at <= CURRENT_TIMESTAMP)"
            ).fetchall()
        for r in rows:
            login = r["twitch_login"]
            log.info("[twitch] 30d re-check: %s", login)
            try:
                ok = await self._check_discord_link(login)
                if not ok:
                    await self._notify_missing_link(login)
                await asyncio.sleep(0.25)
            except Exception as e:
                log.warning("link reverification failed for %s: %s", login, e)

    async def _notify_missing_link(self, login: str):
        if not self._alert_channel_id:
            return
        ch = self.bot.get_channel(self._alert_channel_id)
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return
        mention = self._alert_mention or ""
        try:
            await ch.send(f"⚠️ {mention} Twitch-Profillink fehlt oder ungültig bei **{login}**.")
        except Exception as e:
            log.warning("failed to send alert: %s", e)

    # ---------- simple stats for dashboard ----------
    async def _compute_stats(self) -> dict:
        with storage.get_conn() as c:
            total_sessions = c.execute("SELECT COUNT(*) AS c FROM twitch_stream_logs").fetchone()["c"]
            unique_streamers = c.execute("SELECT COUNT(DISTINCT streamer_login) AS c FROM twitch_stream_logs").fetchone()["c"]
            rows = c.execute(
                "SELECT streamer_login, COUNT(*) AS sessions, AVG(COALESCE(viewers,0)) AS avg_viewers, MAX(COALESCE(viewers,0)) AS max_viewers "
                "FROM twitch_stream_logs GROUP BY streamer_login ORDER BY sessions DESC LIMIT 10"
            ).fetchall()
        top = [
            {
                "streamer": r["streamer_login"],
                "sessions": r["sessions"],
                "avg_viewers": r["avg_viewers"] or 0,
                "max_viewers": r["max_viewers"] or 0,
            }
            for r in rows
        ]
        return {"total_sessions": total_sessions, "unique_streamers": unique_streamers, "top": top}
