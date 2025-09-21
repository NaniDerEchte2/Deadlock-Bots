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
from typing import Dict, List, Optional, Tuple, Union

import aiohttp

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API_BASE = "https://api.twitch.tv/helix"

class TwitchAPI:
    """Thin async wrapper around Twitch Helix using app access tokens.

    Notes:
      * Uses **Search Categories** to resolve game/category IDs (Get Games is superseded).
      * No secrets logged; short timeouts + simple backoff.
    """

    def __init__(self, client_id: str, client_secret: str, session: Optional[aiohttp.ClientSession] = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self._session = session
        self._own_session = False
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._lock = asyncio.Lock()
        self._category_cache: Dict[str, str] = {}  # name_lower -> id

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
    # Core request
    # ------------------------
    async def _get(self, path: str, params: Optional[Union[Dict[str, str], List[Tuple[str, str]]]] = None) -> Dict:
        await self._ensure_token()
        assert self._session is not None
        backoff = 1.0
        for _ in range(4):
            try:
                async with self._session.get(
                    f"{TWITCH_API_BASE}{path}",
                    headers=self._headers(),
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status == 429:
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
    # Categories (games)
    # ------------------------
    async def search_category_id(self, query: str) -> Optional[str]:
        """Resolve a category/game id via /search/categories (case-insensitive exact name preferred)."""
        if not query:
            return None
        ql = query.lower()
        if ql in self._category_cache:
            return self._category_cache[ql]
        js = await self._get("/search/categories", params={"query": query, "first": "25"})
        best: Optional[str] = None
        for item in js.get("data", []) or []:
            name = (item.get("name") or "").strip()
            if name.lower() == ql:
                best = item.get("id")
                break
            # fallback: startswith
            if not best and name.lower().startswith(ql):
                best = item.get("id")
        if best:
            self._category_cache[ql] = best
        return best

    async def get_category_id(self, name: str) -> Optional[str]:
        return await self.search_category_id(name)

    # ------------------------
    # Users & Streams
    # ------------------------
    async def get_users(self, logins: List[str]) -> Dict[str, Dict]:
        out: Dict[str, Dict] = {}
        if not logins:
            return out
        for i in range(0, len(logins), 100):
            chunk = logins[i:i+100]
            params: List[Tuple[str, str]] = [("login", x) for x in chunk]
            js = await self._get("/users", params=params)
            for u in js.get("data", []) or []:
                out[u["login"].lower()] = u
        return out

    async def get_streams(self, *, user_logins: Optional[List[str]] = None, game_id: Optional[str] = None, language: Optional[str] = None, first: int = 100) -> List[Dict]:
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

DEFAULT_DB = os.path.join(os.path.expanduser("~"), "Documents", "Deadlock", "service", "deadlock.sqlite3")

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
    last_link_checked_at DATETIME,
    next_link_check_at DATETIME,
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

CREATE TABLE IF NOT EXISTS twitch_stream_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
    streamer_login TEXT,
    user_id TEXT,
    title TEXT,
    viewers INTEGER,
    started_at TEXT,
    language TEXT,
    game_id TEXT,
    game_name TEXT
);
"""

@contextmanager
def get_conn():
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
from typing import List, Callable, Awaitable, Optional

from aiohttp import web

DISCORD_URL_RE = re.compile(r"(?:https?://)?(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me)/[A-Za-z0-9-]+", re.I)

class Dashboard:
    """Very small admin dashboard; may run with or without an admin token.

    Security:
      - If noauth=True, bind to localhost only (recommended); otherwise require X-Admin-Token.
      - HTML-escape all dynamic values; only accept safe login patterns.
    """

    def __init__(
        self,
        *,
        app_token: Optional[str],
        noauth: bool,
        add_cb: Callable[[str, bool], Awaitable[None]],
        remove_cb: Callable[[str], Awaitable[None]],
        list_cb: Callable[[], Awaitable[List[dict]]],
        rescan_cb: Callable[[], Awaitable[None]],
        stats_cb: Callable[[], Awaitable[dict]],
        export_cb: Callable[[], Awaitable[dict]],
        export_csv_cb: Callable[[], Awaitable[str]],
    ):
        self._token = app_token
        self._noauth = noauth
        self._add = add_cb
        self._remove = remove_cb
        self._list = list_cb
        self._rescan = rescan_cb
        self._stats = stats_cb
        self._export = export_cb
        self._export_csv = export_csv_cb

    def _require_token(self, request: web.Request):
        if self._noauth:
            return
        token = request.headers.get("X-Admin-Token") or request.query.get("token")
        if not token or not self._token or token != self._token:
            raise web.HTTPUnauthorized(text="missing or invalid token")

    async def index(self, request: web.Request):
        self._require_token(request)
        items = await self._list()
        rows: List[str] = []
        for st in items:
            rows.append(
                f"<tr>
<td>{html.escape(st['twitch_login'])}</td>"
                f"<td>{'✅' if st['require_discord_link'] else '—'}</td>"
                f"<td>{'✅' if st['last_link_ok'] else '❌'}</td>"
                f"<td><form method='post' action='/twitch/remove'><input type='hidden' name='login' value='{html.escape(st['twitch_login'])}'/><button>Remove</button></form></td>
</tr>"
            )
        body = f"""
<!doctype html>
<meta charset="utf-8">
<title>Twitch Deadlock – Admin</title>
<body style="font-family: system-ui; max-width: 900px; margin: 2rem auto; color:#eee; background:#0f0f23;">
<h1>Deadlock Twitch Posting – Admin</h1>
<form method="post" action="/twitch/add">
  <label>Twitch Login: <input name="login" required pattern="[A-Za-z0-9_]{{3,25}}"></label>
  <label><input type="checkbox" name="require_link" value="1"> require Discord link</label>
  <button>Add</button>
</form>
<form method="get" action="/twitch/add_url" style="margin-top:0.6rem">
  <label>twitch.tv URL: <input name="url" placeholder="https://twitch.tv/xy" size="32"></label>
  <button>Add by URL</button>
</form>
<form method="post" action="/twitch/rescan" style="margin-top:1rem">
  <button>Re-scan Discord links on all profiles</button>
</form>
<p style="margin-top:1rem"><a href="/twitch/stats">📊 Stats</a> · <a href="/twitch/export">JSON</a> · <a href="/twitch/export/csv">CSV</a></p>
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
        raise web.HTTPFound(location="/twitch")

    async def add_url(self, request: web.Request):
        self._require_token(request)
        url = (request.query.get("url") or "").strip()
        m = re.search(r"twitch\.tv/([A-Za-z0-9_]{3,25})", url, flags=re.I)
        if not m:
            raise web.HTTPBadRequest(text="invalid twitch url")
        await self._add(m.group(1), False)
        raise web.HTTPFound(location="/twitch")

    async def add_login(self, request: web.Request):
        self._require_token(request)
        login = (request.match_info.get("login") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,25}", login):
            raise web.HTTPBadRequest(text="invalid login")
        await self._add(login, False)
        raise web.HTTPFound(location="/twitch")

    async def remove(self, request: web.Request):
        self._require_token(request)
        data = await request.post()
        login = (data.get("login") or "").strip()
        await self._remove(login)
        raise web.HTTPFound(location="/twitch")

    async def rescan(self, request: web.Request):
        self._require_token(request)
        await self._rescan()
        raise web.HTTPFound(location="/twitch")

    async def stats(self, request: web.Request):
        self._require_token(request)
        stats = await self._stats()
        # Very simple HTML view
        rows = []
        for name, data in stats.get("top", [])[:10]:
            rows.append(f"<li>{html.escape(name)} — {int(data['sessions'])} sessions, avg {int(data['avg_viewers'])} viewers</li>")
        body = f"""
<!doctype html><meta charset="utf-8"><title>Stats</title>
<body style="font-family: system-ui; max-width: 900px; margin: 2rem auto; color:#eee; background:#0f0f23;">
<h1>📊 Deadlock Streams – Stats</h1>
<p>Total sessions: {stats.get('total_sessions', 0)} | Unique streamers: {stats.get('unique_streamers', 0)}</p>
<ol>{''.join(rows) or '<li>no data</li>'}</ol>
<p><a href="/twitch">← back</a></p>
</body>
"""
        return web.Response(text=body, content_type="text/html")

    async def export_json(self, request: web.Request):
        self._require_token(request)
        data = await self._export()
        return web.json_response(data)

    async def export_csv(self, request: web.Request):
        self._require_token(request)
        data = await self._export_csv()
        return web.Response(text=data, content_type="text/csv", headers={"Content-Disposition": "attachment; filename=deadlock_streams.csv"})

    def attach(self, app: web.Application):
        app.add_routes([
            web.get("/twitch", self.index),
            web.post("/twitch/add", self.add),
            web.get("/twitch/add_url", self.add_url),
            web.get(r"/twitch/add_login/{login}", self.add_login),
            web.post("/twitch/remove", self.remove),
            web.post("/twitch/rescan", self.rescan),
            web.get("/twitch/stats", self.stats),
            web.get("/twitch/export", self.export_json),
            web.get("/twitch/export/csv", self.export_csv),
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


# =========================================
# cogs/twitch_deadlock/README.md
# =========================================
# Twitch Deadlock Notifier (Cog)

Posts live messages for tracked Twitch streamers **only when they are streaming _Deadlock_**. Includes a tiny admin dashboard for adding/removing streamers, rescanning Twitch profiles for a Discord link, **basic stats & exports**, and a **30‑Tage Re‑Verifikation** der Discord‑Links.

## Features
- Deadlock‑Filter via **Search Categories** → category_id; Fallback: filter `game_name` client‑seitig.
- Bulk polling via Helix **Get Streams** (App‑Access‑Token)
- Optional Sprach‑Filter (z. B. `de`)
- **Discord‑Link Pflicht (optional)** + 30‑Tage‑Recheck mit Alert in Channel
- **Dashboard** (aiohttp) mit Add/Remove, Add‑per‑URL, Rescan, **Stats**, **JSON/CSV Export**
- SQLite‑Tabellen in eurer **zentralen DB** (keine neue DB)
- CWE‑bewusst: keine Secret‑Logs, parametrisierte SQL, XSS‑Escaping, optional Auth‑Token; Timeouts/Backoff

## Env
```
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
# Dashboard
TWITCH_DASHBOARD_NOAUTH=1           # ohne Token (empfohlen nur auf 127.0.0.1)
TWITCH_DASHBOARD_HOST=127.0.0.1     # bei NOAUTH default auf localhost
TWITCH_DASHBOARD_PORT=8765
# Optional
TWITCH_LANGUAGE=de
TWITCH_DEADLOCK_NAME=Deadlock
TWITCH_REQUIRED_DISCORD_MARKER=Deadlock Community Deutsch
# Channel Overrides
TWITCH_NOTIFY_CHANNEL_ID=1304169815505637458
TWITCH_ALERT_CHANNEL_ID=1374364800817303632
TWITCH_ALERT_MENTION=<@earlysalty>   # oder <@1234567890> / <@&ROLEID>
# Stats Logging
TWITCH_LOG_EVERY_N_TICKS=5
# zentrale DB (wie im Projekt)
DEADLOCK_DB_PATH=...   # oder DEADLOCK_DB_DIR=...
```

## Commands
- `/twitch channel #live` – per‑Guild Zielkanal
- `/twitch add <login> [require_discord_link]`
- `/twitch remove <login>`
- `/twitch list`
- `/twitch forcecheck`

## Dashboard
- `GET /twitch` – Übersicht
- `POST /twitch/add` – Login hinzufügen
- `GET  /twitch/add_url?url=https://twitch.tv/<login>` – per URL
- `GET  /twitch/add_login/<login>` – Quick‑Add
- `POST /twitch/rescan` – alle Profile neu prüfen
- `GET  /twitch/stats`, `/twitch/export`, `/twitch/export/csv`

## Hinweise
- Twitch‑Panels sind via API **nicht** verfügbar; wir prüfen die **Beschreibung** des Profils.
- Wenn `TWITCH_DASHBOARD_NOAUTH=1`, binde den Server an `127.0.0.1` oder schütze per Reverse‑Proxy/IP‑ACL.
