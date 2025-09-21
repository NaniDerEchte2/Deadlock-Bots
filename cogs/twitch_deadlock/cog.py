# =========================================
# cogs/twitch_deadlock/cog.py
# =========================================
import asyncio
import logging
import os
import re
from typing import Dict, List, Optional

import discord
from discord.ext import commands, tasks
from aiohttp import web

from .twitch_api import TwitchAPI
from . import storage
from .dashboard import Dashboard, DISCORD_URL_RE

log = logging.getLogger("TwitchDeadlock")

DEADLOCK_GAME_NAME = os.getenv("TWITCH_DEADLOCK_NAME", "Deadlock")

def _bool(v: Optional[str]) -> bool:
    return str(v).lower() in {"1", "true", "yes", "on"}

class TwitchDeadlockCog(commands.Cog):
    """Discord Cog: posts live messages for tracked Twitch streamers *only* when
    they are playing Deadlock. Includes a minimal admin dashboard.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client_id = os.getenv("TWITCH_CLIENT_ID")
        self.client_secret = os.getenv("TWITCH_CLIENT_SECRET")
        if not self.client_id or not self.client_secret:
            log.error("TWITCH_CLIENT_ID/SECRET not configured; cog disabled")
            self.api = None
            return
        self.api = TwitchAPI(self.client_id, self.client_secret)
        self._category_id: Optional[str] = None
        self._language_filter = os.getenv("TWITCH_LANGUAGE", "").strip() or None

        # Dashboard/auth
        self._dashboard_token = os.getenv("TWITCH_DASHBOARD_TOKEN") or None
        self._dashboard_noauth = _bool(os.getenv("TWITCH_DASHBOARD_NOAUTH", "0"))
        self._dashboard_host = os.getenv("TWITCH_DASHBOARD_HOST") or ("127.0.0.1" if self._dashboard_noauth else "0.0.0.0")
        self._dashboard_port = int(os.getenv("TWITCH_DASHBOARD_PORT", "8765"))
        self._required_marker_default = os.getenv("TWITCH_REQUIRED_DISCORD_MARKER", "") or None

        # Channel overrides (optional)
        self._notify_channel_id = int(os.getenv("TWITCH_NOTIFY_CHANNEL_ID", "0") or 0)
        self._alert_channel_id = int(os.getenv("TWITCH_ALERT_CHANNEL_ID", "0") or 0)
        self._alert_mention = os.getenv("TWITCH_ALERT_MENTION", "")  # e.g. <@123> or <@&role>

        # logging/stats
        self._tick_count = 0
        self._log_every_n = max(1, int(os.getenv("TWITCH_LOG_EVERY_N_TICKS", "5")))

        self._web: Optional[web.AppRunner] = None
        self._web_app: Optional[web.Application] = None

        self.poll_streams.start()
        self.link_reverify.start()
        self.bot.loop.create_task(self._ensure_category_id())
        self.bot.loop.create_task(self._start_dashboard())

    def cog_unload(self):
        try:
            self.poll_streams.cancel()
            self.link_reverify.cancel()
        except Exception:
            pass
        if self._web:
            self.bot.loop.create_task(self._stop_dashboard())

    # -----------------------------
    # Dashboard (aiohttp)
    # -----------------------------
    async def _start_dashboard(self):
        if self._web is not None:
            return
        self._web_app = web.Application()

        async def add(login: str, require_link: bool):
            await self._cmd_add(login, require_link)
        async def remove(login: str):
            await self._cmd_remove(login)
        async def list_items():
            with storage.get_conn() as c:
                rows = c.execute("SELECT twitch_login, require_discord_link, last_link_ok FROM twitch_streamers ORDER BY twitch_login").fetchall()
                return [dict(r) for r in rows]
        async def rescan():
            await self._rescan_all_links()
        async def stats_cb():
            return await self._compute_stats()
        async def export_cb():
            with storage.get_conn() as c:
                # minimal export: stream logs
                rows = c.execute("SELECT * FROM twitch_stream_logs ORDER BY ts DESC LIMIT 10000").fetchall()
                return {"logs": [dict(r) for r in rows]}
        async def export_csv_cb():
            with storage.get_conn() as c:
                rows = c.execute("SELECT ts, streamer_login, viewers, title, started_at, language, game_name FROM twitch_stream_logs ORDER BY ts").fetchall()
            out = ["Timestamp,Streamer,Viewers,Title,Started_At,Language,Game
"]
            for r in rows:
                title = (r["title"] or "").replace('"', '""').replace("
", " ")
                out.append(f'"{r["ts"]}","{r["streamer_login"]}",{r["viewers"] or 0},"{title}","{r["started_at"]}","{r["language"] or ''}","{r["game_name"] or ''}"
')
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
        # Do NOT log token (CWE-522)
        log.info("Twitch dashboard running on http://%s:%d/twitch", self._dashboard_host, self._dashboard_port)
        if self._dashboard_noauth and self._dashboard_host != "127.0.0.1":
            log.warning("Dashboard is running without auth and not bound to localhost — consider restricting access.")

    async def _stop_dashboard(self):
        try:
            if self._web:
                await self._web.cleanup()
        finally:
            self._web = None
            self._web_app = None

    # -----------------------------
    # DB helpers
    # -----------------------------
    def _get_settings(self, guild_id: int) -> Optional[dict]:
        with storage.get_conn() as c:
            r = c.execute("SELECT * FROM twitch_settings WHERE guild_id=?", (guild_id,)).fetchone()
            return dict(r) if r else None

    def _set_channel(self, guild_id: int, channel_id: int):
        with storage.get_conn() as c:
            c.execute(
                "INSERT INTO twitch_settings (guild_id, channel_id, language_filter, required_marker) VALUES (?, ?, ?, ?)
"
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
                (guild_id, channel_id, self._language_filter, self._required_marker_default),
            )

    # -----------------------------
    # Link check
    # -----------------------------
    async def _check_discord_link(self, login: str) -> bool:
        assert self.api
        users = await self.api.get_users([login])
        u = users.get(login.lower())
        if not u:
            return False
        desc = (u.get("description") or "").strip()
        has_link = bool(DISCORD_URL_RE.search(desc))
        marker_ok = True
        if self._required_marker_default:
            marker_ok = self._required_marker_default.lower() in desc.lower()
        with storage.get_conn() as c:
            c.execute(
                "UPDATE twitch_streamers SET last_description=?, last_link_ok=?, last_link_checked_at=CURRENT_TIMESTAMP, next_link_check_at=datetime('now','+30 days') WHERE twitch_login=?",
                (desc[:4000], int(has_link and marker_ok), login.lower()),
            )
        return has_link and marker_ok

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

    # -----------------------------
    # Commands (hybrid)
    # -----------------------------
    @commands.hybrid_group(name="twitch", with_app_command=True)
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_group(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("Subcommands: add, remove, list, channel, forcecheck")

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

    @twitch_group.command(name="add")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_add(self, ctx: commands.Context, login: str, require_discord_link: Optional[bool] = False):
        msg = await self._cmd_add(login, bool(require_discord_link))
        await ctx.reply(msg)

    async def _cmd_remove(self, login: str) -> str:
        with storage.get_conn() as c:
            c.execute("DELETE FROM twitch_streamers WHERE twitch_login=?", (login.lower(),))
            c.execute("DELETE FROM twitch_live_state WHERE streamer_login=?", (login.lower(),))
        return f"{login} entfernt"

    @twitch_group.command(name="remove")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_remove(self, ctx: commands.Context, login: str):
        await ctx.reply(await self._cmd_remove(login))

    @twitch_group.command(name="list")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_list(self, ctx: commands.Context):
        with storage.get_conn() as c:
            rows = c.execute("SELECT twitch_login, require_discord_link, last_link_ok FROM twitch_streamers ORDER BY twitch_login").fetchall()
        if not rows:
            await ctx.reply("Keine Streamer gespeichert.")
            return
        lines = [f"• {r['twitch_login']}  (require_link={'ja' if r['require_discord_link'] else 'nein'}, has_link={'ja' if r['last_link_ok'] else 'nein'})" for r in rows]
        await ctx.reply("
".join(lines)[:1900])

    @twitch_group.command(name="forcecheck")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_forcecheck(self, ctx: commands.Context):
        await ctx.reply("Prüfe jetzt…")
        await self._tick()

    # -----------------------------
    # Polling & posting
    # -----------------------------
    async def _ensure_category_id(self):
        if not self.api:
            return
        try:
            self._category_id = await self.api.get_category_id(DEADLOCK_GAME_NAME)
            if self._category_id:
                log.info("Deadlock category_id = %s", self._category_id)
            else:
                log.warning("Deadlock category not found via Search Categories; will use fallback filter by game_name.")
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
        # load streamer list
        with storage.get_conn() as c:
            rows = c.execute("SELECT twitch_login, twitch_user_id, require_discord_link, last_link_ok FROM twitch_streamers").fetchall()
        if not rows:
            return
        logins = [r["twitch_login"] for r in rows]
        require_map = {r["twitch_login"].lower(): (bool(r["require_discord_link"]), bool(r["last_link_ok"])) for r in rows}

        # fetch streams in bulk
        streams = await self.api.get_streams(user_logins=logins, game_id=self._category_id, language=self._language_filter)
        # fallback: if no category id, filter by game_name
        if not self._category_id and streams:
            streams = [s for s in streams if (s.get("game_name") or "").lower() == DEADLOCK_GAME_NAME.lower()]
        live_by_login = {s.get("user_login", "").lower(): s for s in streams}

        # current states
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
                    continue  # skip until profile links our Discord
                s = live_by_login[login_l]
                stream_id = s.get("id")
                started_at = s.get("started_at")
                title = s.get("title")

                if not st or not st.get("is_live") or st.get("last_stream_id") != stream_id:
                    now_live.append(login_l)
                # update state
                with storage.get_conn() as c:
                    c.execute(
                        "INSERT INTO twitch_live_state (twitch_user_id, streamer_login, last_stream_id, last_started_at, last_title, last_game_id, is_live)
"
                        "VALUES (?, ?, ?, ?, ?, ?, 1)
"
                        "ON CONFLICT(twitch_user_id) DO UPDATE SET last_stream_id=excluded.last_stream_id, last_started_at=excluded.last_started_at, last_title=excluded.last_title, last_game_id=excluded.last_game_id, is_live=1",
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

        # periodic logging for stats
        self._tick_count += 1
        if self._tick_count % self._log_every_n == 0 and streams:
            with storage.get_conn() as c:
                for s in streams:
                    c.execute(
                        "INSERT INTO twitch_stream_logs (streamer_login, user_id, title, viewers, started_at, language, game_id, game_name) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            s.get("user_login"), s.get("user_id"), s.get("title"), s.get("viewer_count"), s.get("started_at"), s.get("language"), s.get("game_id"), s.get("game_name"),
                        ),
                    )

    async def _post_go_live(self, logins: List[str], live_by_login: Dict[str, dict]):
        # Prefer explicit channel override if configured
        target_channel = None
        if self._notify_channel_id:
            target_channel = self.bot.get_channel(self._notify_channel_id)
        for g in self.bot.guilds:
            settings = self._get_settings(g.id)
            channel = None
            if not target_channel and settings:
                channel = g.get_channel(int(settings["channel_id"]))
            else:
                channel = target_channel
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
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
                    msg = await channel.send(content=f"🔴 **{s.get('user_name')}** ist live: {url}", embed=embed)
                    with storage.get_conn() as c:
                        c.execute(
                            "UPDATE twitch_live_state SET last_discord_message_id=?, last_notified_at=CURRENT_TIMESTAMP WHERE streamer_login=?",
                            (str(msg.id), login),
                        )
                except Exception as e:
                    log.warning("failed to post go-live for %s: %s", login, e)

    async def _mark_offline(self, logins: List[str]):
        # Try both override and per-guild settings
        targets: List[discord.abc.Messageable] = []
        if self._notify_channel_id:
            ch = self.bot.get_channel(self._notify_channel_id)
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                targets.append(ch)
        if not targets:
            for g in self.bot.guilds:
                settings = self._get_settings(g.id)
                if not settings:
                    continue
                ch = g.get_channel(int(settings["channel_id"]))
                if isinstance(ch, (discord.TextChannel, discord.Thread)):
                    targets.append(ch)
        # edit last live message if possible
        for ch in targets:
            with storage.get_conn() as c:
                qmarks = ",".join(["?" for _ in logins])
                rows = c.execute(f"SELECT streamer_login, last_discord_message_id FROM twitch_live_state WHERE streamer_login IN ({qmarks})", tuple(logins)).fetchall()
            for r in rows:
                mid = r["last_discord_message_id"]
                if not mid:
                    continue
                try:
                    msg = await ch.fetch_message(int(mid))
                    await msg.edit(content=(msg.content + " (beendet)"))
                except Exception as e:
                    log.debug("cannot edit message %s: %s", mid, e)

    # -----------------------------
    # Daily re-verify (30d)
    # -----------------------------
    @tasks.loop(hours=24)
    async def link_reverify(self):
        if not self.api:
            return
        with storage.get_conn() as c:
            rows = c.execute(
                "SELECT twitch_login, require_discord_link, last_link_ok, next_link_check_at FROM twitch_streamers WHERE require_discord_link=1"
            ).fetchall()
        for r in rows:
            login = r["twitch_login"]
            due = not r["next_link_check_at"] or True
            # sqlite returns str; compare in SQL next time
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

