# cogs/steam_link_oauth.py
import os, time, uuid, logging
from typing import List, Dict, Optional
import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks
from shared import db

log = logging.getLogger("SteamLink")

DISCORD_API = "https://discord.com/api"
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID") or ""
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET") or ""
REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI") or ""
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL") or (REDIRECT_URI.rsplit("/oauth/callback", 1)[0] if REDIRECT_URI else "")
HTTP_HOST = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT = int(os.getenv("HTTP_PORT", "8888"))

class SteamLink(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.app = web.Application()
        self.app.router.add_get("/oauth/callback", self.handle_callback)
        self._runner: Optional[web.AppRunner] = None
        self._states: Dict[str, Dict[str, float]] = {}

    async def cog_load(self):
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=HTTP_HOST, port=HTTP_PORT)
        await site.start()
        log.info("OAuth Callback server on %s:%s", HTTP_HOST, HTTP_PORT)

    async def cog_unload(self):
        if self._runner:
            await self._runner.cleanup()

    def _mk_state(self, uid: int) -> str:
        s = uuid.uuid4().hex
        self._states[s] = {"uid": str(uid), "ts": time.time()}
        return s

    def _pop_state(self, s: str) -> Optional[str]:
        m = self._states.pop(s, None)
        if not m: return None
        if time.time() - m["ts"] > 600: return None
        return m["uid"]

    def _auth_url(self, uid: int) -> str:
        from urllib.parse import urlencode
        q = urlencode({
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": "identify connections",
            "prompt": "consent",
            "state": self._mk_state(uid),
        })
        return f"{DISCORD_API}/oauth2/authorize?{q}"

    async def handle_callback(self, request: web.Request) -> web.Response:
        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state:
            return web.Response(text="missing code/state", status=400)
        uid = self._pop_state(state)
        if not uid:
            return web.Response(text="invalid/expired state", status=400)
        async with aiohttp.ClientSession() as s:
            # token
            data = {
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": REDIRECT_URI,
            }
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            async with s.post(f"{DISCORD_API}/oauth2/token", data=data, headers=headers) as r:
                if r.status != 200:
                    return web.Response(text=f"token err {r.status}", status=400)
                tok = await r.json()
            at = tok.get("access_token")
            if not at:
                return web.Response(text="no access_token", status=400)
            # connections
            async with s.get(f"{DISCORD_API}/users/@me/connections", headers={"Authorization": f"Bearer {at}"} ) as r:
                if r.status != 200:
                    return web.Response(text=f"connections err {r.status}", status=400)
                conns = await r.json()
        # alle steam-Connections speichern (Mehrfach-Accounts!)
        cnt = 0
        for c in conns:
            if c.get("type") != "steam": continue
            steam_id = str(c.get("id") or "")
            if not steam_id: continue
            name = c.get("name") or ""
            verified = 1 if c.get("verified") else 0
            db.execute(
                """
                INSERT INTO steam_links(user_id,steam_id,name,verified)
                VALUES(?,?,?,?)
                ON CONFLICT(user_id,steam_id) DO UPDATE SET
                  name=excluded.name, verified=excluded.verified, updated_at=CURRENT_TIMESTAMP
                """,
                (int(uid), steam_id, name, verified),
            )
            cnt += 1
        body = f"<h3>OK</h3><p>{cnt} Steam-Account(s) verknüpft.</p>"
        return web.Response(text=body, content_type="text/html")

    @commands.hybrid_command(name="link", description="Verknüpfe deine Steam-Accounts")
    async def link(self, ctx: commands.Context):
        url = self._auth_url(ctx.author.id)
        await ctx.reply(f"Klicke zum Verknüpfen:\n{url}\nMehrfach-Accounts werden gespeichert.", ephemeral=True)

    @commands.hybrid_command(name="links", description="Zeigt deine gespeicherten Steam-Links")
    async def links(self, ctx: commands.Context):
        rows = db.query_all("SELECT steam_id,name,verified,primary_account FROM steam_links WHERE user_id=?", (ctx.author.id,))
        if not rows:
            return await ctx.reply("Keine Steam-Links gefunden. Nutze `/link`.", ephemeral=True)
        lines = [f"- {r['steam_id']}  ({r['name'] or 'kein Name'}){' ✅' if r['verified'] else ''}{' [primary]' if r['primary_account'] else ''}" for r in rows]
        await ctx.reply("Deine Links:\n" + "\n".join(lines), ephemeral=True)

    @commands.hybrid_command(name="unlink", description="Entfernt einen Steam-Link")
    async def unlink(self, ctx: commands.Context, steam_id: str):
        db.execute("DELETE FROM steam_links WHERE user_id=? AND steam_id=?", (ctx.author.id, steam_id))
        await ctx.reply("Entfernt.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(SteamLink(bot))
