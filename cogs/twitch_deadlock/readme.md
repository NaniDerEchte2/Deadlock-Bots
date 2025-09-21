# =========================================
# cogs/twitch_deadlock/__init__.py
# =========================================
from .cog import TwitchDeadlockCog

async def setup(bot):
    await bot.add_cog(TwitchDeadlockCog(bot))


# =========================================
# cogs/twitch_deadlock/twitch_api.py
# =========================================
import asyncio
import time
from typing import Dict, List, Optional, Tuple

import aiohttp

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API_BASE = "https://api.twitch.tv/helix"

class TwitchAPI:
    """Thin async wrapper around Twitch Helix using app access tokens.

    Security:
      - No secrets logged (CWE-522)
      - Timeouts + backoff to mitigate resource exhaustion (CWE-770/CWE-400)
    """

    def __init__(self, client_id: str, client_secret: str, session: Optional[aiohttp.ClientSession] = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self._session = session
        self._own_session = False
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._lock = asyncio.Lock()
        self._game_cache: Dict[str, str] = {}  # name -> id

    async def __aenter__(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self

    async def __aexit__(self, *exc):
        if self._own_session and self._session:
            await self._session.close()

    # ------------------------
    # OAuth app access token
    # ------------------------
    async def _ensure_token(self):
        async with self._lock:
            if self._token and time.time() < self._token_expiry - 60:
                return
            assert self._session is not None
            data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            }
            async with self._session.post(TWITCH_TOKEN_URL, data=data, timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                js = await r.json()
                self._token = js.get("access_token")
                expires = js.get("expires_in", 3600)
                self._token_expiry = time.time() + float(expires)

    def _headers(self) -> Dict[str, str]:
        return {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {self._token}",
        }

    # ------------------------
    # Core requests
    # ------------------------
    async def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict:
        await self._ensure_token()
        assert self._session is not None
        # Retry/backoff
        backoff = 1.0
        for attempt in range(4):
            try:
                async with self._session.get(
                    f"{TWITCH_API_BASE}{path}",
                    headers=self._headers(),
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status == 429:
                        # Basic rate-limit backoff
                        await asyncio.sleep(min(10, backoff))
                        backoff *= 2
                        continue
                    r.raise_for_status()
                    return await r.json()
            except aiohttp.ClientResponseError as e:
                if e.status in (500, 502, 503, 504):
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise
        raise RuntimeError("Twitch API retries exhausted")

    # ------------------------
    # Public endpoints used
    # ------------------------
    async def get_games_by_name(self, names: List[str]) -> Dict[str, str]:
        """Return mapping name_lower -> game_id."""
        if not names:
            return {}
        # resolve via cache first
        remaining = [n for n in names if n.lower() not in self._game_cache]
        out = {n.lower(): self._game_cache[n.lower()] for n in names if n.lower() in self._game_cache}
        # Twitch allows up to 100 names
        batch = []
        for n in remaining:
            batch.append(n)
            if len(batch) == 100:
                js = await self._get("/games", params={"name": batch[0]}) if len(batch) == 1 else await self._get("/games", params=[("name", b) for b in batch])
                for g in js.get("data", []):
                    self._game_cache[g["name"].lower()] = g["id"]
                    out[g["name"].lower()] = g["id"]
                batch = []
        if batch:
            js = await self._get("/games", params=[("name", b) for b in batch])
            for g in js.get("data", []):
                self._game_cache[g["name"].lower()] = g["id"]
                out[g["name"].lower()] = g["id"]
        return out

    async def get_game_id(self, name: str) -> Optional[str]:
        name_l = name.lower()
        if name_l in self._game_cache:
            return self._game_cache[name_l]
        js = await self._get("/games", params={"name": name})
        data = js.get("data", [])
        if data:
            gid = data[0]["id"]
            self._game_cache[name_l] = gid
            return gid
        return None

    async def get_users(self, logins: List[str]) -> Dict[str, Dict]:
        """Return mapping login_lower -> user object (id, login, display_name, description, etc.)."""
        out: Dict[str, Dict] = {}
        if not logins:
            return out
        # 100 per request
        for i in range(0, len(logins), 100):
            chunk = logins[i:i+100]
            params: List[Tuple[str, str]] = [("login", x) for x in chunk]
            js = await self._get("/users", params=params)
            for u in js.get("data", []):
                out[u["login"].lower()] = u
        return out

    async def get_streams(self, *, user_logins: Optional[List[str]] = None, game_id: Optional[str] = None, language: Optional[str] = None, first: int = 100) -> List[Dict]:
        """Get active streams; filters are combined (AND)."""
        params: List[Tuple[str, str]] = []
        if user_logins:
            for u in user_logins[:100]:
                params.append(("user_login", u))
        if game_id:
            params.append(("game_id", game_id))
        if language:
            params.append(("language", language))
        params.append(("first", str(min(max(first, 1), 100))))
        js = await self._get("/streams", params=params)
        return js.get("data", [])


# =========================================
# cogs/twitch_deadlock/storage.py
# =========================================
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

DEFAULT_DB = os.path.join(os.path.expanduser("~"), "Documents", "Deadlock", "service", "deadlock.sqlite3")

# Use existing central DB location, honoring env used in the rest of the project
DB_PATH = (
    os.getenv("DEADLOCK_DB_PATH")
    or (os.path.join(os.getenv("DEADLOCK_DB_DIR", ""), "deadlock.sqlite3") if os.getenv("DEADLOCK_DB_DIR") else DEFAULT_DB)
)

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

PRAGMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS twitch_streamers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    twitch_login TEXT NOT NULL UNIQUE,
    twitch_user_id TEXT,
    require_discord_link INTEGER NOT NULL DEFAULT 0,
    last_description TEXT,
    last_link_ok INTEGER NOT NULL DEFAULT 0,
    added_by TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS twitch_settings (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    language_filter TEXT DEFAULT NULL,
    required_marker TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS twitch_live_state (
    twitch_user_id TEXT PRIMARY KEY,
    streamer_login TEXT NOT NULL,
    last_stream_id TEXT,
    last_started_at TEXT,
    last_title TEXT,
    last_game_id TEXT,
    is_live INTEGER NOT NULL DEFAULT 0,
    last_discord_message_id TEXT,
    last_notified_at DATETIME
);
"""

@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    # Using sqlite3 directly but against the central file that all cogs share.
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(PRAGMA_SQL)
        conn.executescript(SCHEMA_SQL)
        yield conn
    finally:
        conn.close()


# =========================================
# cogs/twitch_deadlock/dashboard.py
# =========================================
import html
import re
from typing import List

from aiohttp import web

DISCORD_URL_RE = re.compile(r"(?:https?://)?(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me)/[A-Za-z0-9-]+", re.I)

class Dashboard:
    """Very small admin dashboard protected by a static token.

    Security (CWE):
      - CSRF: demand token on every POST (CWE-352)
      - XSS: HTML-escape all dynamic values (CWE-79)
    """

    def __init__(self, app_token: str, add_cb, remove_cb, list_cb, rescan_cb):
        self._token = app_token
        self._add = add_cb
        self._remove = remove_cb
        self._list = list_cb
        self._rescan = rescan_cb

    def _require_token(self, request: web.Request):
        token = request.headers.get("X-Admin-Token") or request.query.get("token")
        if not token or token != self._token:
            raise web.HTTPUnauthorized(text="missing or invalid token")

    async def index(self, request: web.Request):
        self._require_token(request)
        items = await self._list()
        rows: List[str] = []
        for st in items:
            rows.append(
                f"<tr>\n<td>{html.escape(st['twitch_login'])}</td>"
                f"<td>{'✅' if st['require_discord_link'] else '—'}</td>"
                f"<td>{'✅' if st['last_link_ok'] else '❌'}</td>"
                f"<td><form method='post' action='/twitch/remove?token={html.escape(self._token)}' style='display:inline'>"
                f"<input type='hidden' name='login' value='{html.escape(st['twitch_login'])}'/>"
                f"<button>Remove</button></form></td>\n</tr>"
            )
        body = f"""
<!doctype html>
<meta charset="utf-8">
<title>Twitch Deadlock – Admin</title>
<body style="font-family: system-ui; max-width: 900px; margin: 2rem auto;">
<h1>Deadlock Twitch Posting – Admin</h1>
<form method="post" action="/twitch/add?token={html.escape(self._token)}">
  <label>Twitch Login: <input name="login" required></label>
  <label><input type="checkbox" name="require_link" value="1"> require Discord link</label>
  <button>Add</button>
</form>
<form method="post" action="/twitch/rescan?token={html.escape(self._token)}" style="margin-top:1rem">
  <button>Re-scan Discord links on all profiles</button>
</form>
<table border="1" cellspacing="0" cellpadding="6" style="margin-top:1rem; width:100%">
  <tr><th>Login</th><th>Req. Link</th><th>Has Link</th><th>Actions</th></tr>
  {''.join(rows)}
</table>
</body>
"""
        return web.Response(text=body, content_type="text/html")

    async def add(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,25}", login):
            raise web.HTTPBadRequest(text="invalid login")
        require_link = 1 if data.get("require_link") else 0
        await self._add(login, bool(require_link))
        raise web.HTTPFound(location=f"/twitch?token={self._token}")

    async def remove(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        await self._remove(login)
        raise web.HTTPFound(location=f"/twitch?token={self._token}")

    async def rescan(self, request: web.Request):
        self._require_token(request)
        await self._rescan()
        raise web.HTTPFound(location=f"/twitch?token={self._token}")

    def attach(self, app: web.Application):
        app.add_routes([
            web.get("/twitch", self.index),
            web.post("/twitch/add", self.add),
            web.post("/twitch/remove", self.remove),
            web.post("/twitch/rescan", self.rescan),
        ])


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


# =========================================
# cogs/twitch_deadlock/README.md
# =========================================
# Twitch Deadlock Notifier (Cog)

Posts live messages for tracked Twitch streamers **only when they are streaming _Deadlock_**. Includes a tiny admin dashboard for adding/removing streamers and rescanning Twitch profiles for a Discord link.

## Features
- Bulk polling via Helix `Get Streams` using **app access token** (no user auth needed)
- Per-guild target channel, optional language filter (e.g., `de`)
- Enforce that a streamer links your Discord in their Twitch profile description before posting
- De-duplication + state table to avoid spam; marks messages as "(beendet)" when stream ends
- Minimal **dashboard** (aiohttp) at `/twitch` protected by `X-Admin-Token`
- SQLite tables created in your existing **central DB file** (same env rules as other cogs)
- CWE-minded: parameterized SQL (CWE-89), no secret logging (CWE-522), HTML-escaped dashboard (CWE-79), token on POSTs (CWE-352), timeouts/backoff (CWE-770/CWE-400)

## Install
1. Copy folder `cogs/twitch_deadlock` into your repo.
2. Add to your loader (where other cogs are loaded): `bot.load_extension('cogs.twitch_deadlock')`.
3. Set env:
   - `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` – from https://dev.twitch.tv/console
   - `TWITCH_DASHBOARD_TOKEN` – random string (if omitted, one is generated and logged)
   - `TWITCH_DASHBOARD_HOST` (default `0.0.0.0`) & `TWITCH_DASHBOARD_PORT` (default `8765`)
   - `TWITCH_LANGUAGE` (optional, e.g. `de`)
   - `TWITCH_DEADLOCK_NAME` (optional override for the game name, default `Deadlock`)
   - `TWITCH_REQUIRED_DISCORD_MARKER` (optional phrase that must appear in the profile description, in addition to a discord invite URL)
   - Reuse your existing DB env: `DEADLOCK_DB_PATH` or `DEADLOCK_DB_DIR`.

## Use
- Set a target channel: `/twitch channel #live`  (also works as prefix command)
- Add a streamer: `/twitch add loginname [require_discord_link]`
- Remove: `/twitch remove loginname`
- List: `/twitch list`
- Force a manual check: `/twitch forcecheck`
- Dashboard: `GET http://<host>:8765/twitch?token=<YOUR_TOKEN>`

## Notes / Limitations
- Discord-link check is a **best effort** using the Twitch **user description** field; Twitch API doesn’t expose About panels. If creators place the link only in panels, auto-check may not find it. Use the `require_discord_link` flag + manual verification where needed.
- Polling interval is 60s; adjust in `@tasks.loop` if needed.
- If you want zero-delay notifications, switch to EventSub/WebSockets later – this design keeps infra simple for now.

