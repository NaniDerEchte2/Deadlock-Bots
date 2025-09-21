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

    Key points:
      - Uses central SQLite DB (same file as other cogs)
      - CWE aware: parameterized SQL, no secret logs, HTML escaping, CSRF token
      - Deadlock-only filtering via Twitch game_id
      - Optional language filter and Discord-link requirement
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
        self._game_id: Optional[str] = None
        self._language_filter = os.getenv("TWITCH_LANGUAGE", "").strip() or None  # e.g., 'de'
        self._dashboard_token = os.getenv("TWITCH_DASHBOARD_TOKEN") or os.urandom(16).hex()
        self._dashboard_host = os.getenv("TWITCH_DASHBOARD_HOST", "0.0.0.0")
        self._dashboard_port = int(os.getenv("TWITCH_DASHBOARD_PORT", "8765"))
        self._required_marker_default = os.getenv("TWITCH_REQUIRED_DISCORD_MARKER", "") or None

        self._web: Optional[web.AppRunner] = None
        self._web_app: Optional[web.Application] = None

        self.poll_streams.start()
        self.bot.loop.create_task(self._ensure_game_id())
        self.bot.loop.create_task(self._start_dashboard())

    def cog_unload(self):
        try:
            self.poll_streams.cancel()
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

        # Handlers use small async wrappers
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

        Dashboard(self._dashboard_token, add, remove, list_items, rescan).attach(self._web_app)

        runner = web.AppRunner(self._web_app)
        await runner.setup()
        site = web.TCPSite(runner, self._dashboard_host, self._dashboard_port)
        await site.start()
        self._web = runner
        log.info("Twitch dashboard running on http://%s:%d/twitch (token=%s)", self._dashboard_host, self._dashboard_port, self._dashboard_token)

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
                "INSERT INTO twitch_settings (guild_id, channel_id, language_filter, required_marker) VALUES (?, ?, ?, ?)\n"
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
                "UPDATE twitch_streamers SET last_description=?, last_link_ok=? WHERE twitch_login=?",
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
    # Commands (hybrid = slash + prefix)
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
                "INSERT OR IGNORE INTO twitch_streamers (twitch_login, twitch_user_id, require_discord_link) VALUES (?, ?, ?)",
                (u["login"].lower(), u["id"], int(require_link)),
            )
        # initial link check (best effort)
        try:
            await self._check_discord_link(login)
        except Exception:
            pass
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
        await ctx.reply("\n".join(lines)[:1900])

    @twitch_group.command(name="forcecheck")
    @commands.has_guild_permissions(manage_guild=True)
    async def twitch_forcecheck(self, ctx: commands.Context):
        await ctx.reply("Prüfe jetzt…")
        await self._tick()

    # -----------------------------
    # Polling & posting
    # -----------------------------
    async def _ensure_game_id(self):
        if not self.api:
            return
        try:
            self._game_id = await self.api.get_game_id(DEADLOCK_GAME_NAME)
            log.info("Deadlock game_id = %s", self._game_id)
        except Exception as e:
            log.error("could not resolve game id: %s", e)

    @tasks.loop(seconds=60.0)
    async def poll_streams(self):
        try:
            await self._tick()
        except Exception as e:
            log.warning("tick failed: %s", e)

    async def _tick(self):
        if not self.api:
            return
        if not self._game_id:
            await self._ensure_game_id()
            if not self._game_id:
                return
        # load streamer list
        with storage.get_conn() as c:
            rows = c.execute("SELECT twitch_login, twitch_user_id, require_discord_link, last_link_ok FROM twitch_streamers").fetchall()
        if not rows:
            return
        logins = [r["twitch_login"] for r in rows]
        require_map = {r["twitch_login"].lower(): (bool(r["require_discord_link"]), bool(r["last_link_ok"])) for r in rows}

        # fetch streams in bulk (live only)
        streams = await self.api.get_streams(user_logins=logins, game_id=self._game_id, language=self._language_filter)
        live_by_login = {s["user_login"].lower(): s for s in streams}

        # compute on/offline
        with storage.get_conn() as c:
            states = {r["streamer_login"].lower(): dict(r) for r in c.execute("SELECT * FROM twitch_live_state").fetchall()}

        now_live: List[str] = []
        now_offline: List[str] = []

        # check each tracked login
        for login in logins:
            login_l = login.lower()
            is_live = login_l in live_by_login
            st = states.get(login_l)

            if is_live:
                # Enforce Discord link if configured and not satisfied
                req, has = require_map.get(login_l, (False, False))
                if req and not has:
                    continue  # skip posting until link present
                s = live_by_login[login_l]
                stream_id = s.get("id")
                started_at = s.get("started_at")
                title = s.get("title")

                if not st or not st.get("is_live") or st.get("last_stream_id") != stream_id:
                    now_live.append(login_l)
                # update state
                with storage.get_conn() as c:
                    c.execute(
                        "INSERT INTO twitch_live_state (twitch_user_id, streamer_login, last_stream_id, last_started_at, last_title, last_game_id, is_live)\n"
                        "VALUES (?, ?, ?, ?, ?, ?, 1)\n"
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

    async def _post_go_live(self, logins: List[str], live_by_login: Dict[str, dict]):
        # group by guild settings (we currently support one channel per guild)
        for g in self.bot.guilds:
            settings = self._get_settings(g.id)
            if not settings:
                continue
            channel = g.get_channel(int(settings["channel_id"]))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            for login in logins:
                s = live_by_login[login]
                embed = discord.Embed(
                    title=f"{s.get('user_name')} ist LIVE in Deadlock!",
                    description=s.get("title") or "",
                    colour=discord.Colour.purple(),
                )
                thumb = s.get("thumbnail_url", "").replace("{width}", "640").replace("{height}", "360")
                if thumb:
                    embed.set_image(url=thumb)
                embed.add_field(name="Viewer", value=str(s.get("viewer_count")))
                embed.add_field(name="Kategorie", value=s.get("game_name") or "Deadlock", inline=True)
                url = f"https://twitch.tv/{login}"
                embed.add_field(name="Link", value=url, inline=False)
                msg = await channel.send(content=f"🔴 **{s.get('user_name')}** ist live: {url}", embed=embed)
                with storage.get_conn() as c:
                    c.execute(
                        "UPDATE twitch_live_state SET last_discord_message_id=?, last_notified_at=CURRENT_TIMESTAMP WHERE streamer_login=?",
                        (str(msg.id), login),
                    )

    async def _mark_offline(self, logins: List[str]):
        for g in self.bot.guilds:
            settings = self._get_settings(g.id)
            if not settings:
                continue
            channel = g.get_channel(int(settings["channel_id"]))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            with storage.get_conn() as c:
                rows = c.execute("SELECT streamer_login, last_discord_message_id FROM twitch_live_state WHERE streamer_login IN (%s)" % ",".join([
                    "?" for _ in logins
                ]), tuple(logins)).fetchall()
            for r in rows:
                try:
                    mid = r["last_discord_message_id"]
                    if mid:
                        msg = await channel.fetch_message(int(mid))
                        await msg.edit(content=(msg.content + " (beendet)"))
                except Exception:
                    pass
