# cogs/live_match_master.py
import os, time, logging, re
from collections import Counter, defaultdict
from typing import Dict, List, Tuple
import aiohttp
import discord
from discord.ext import commands, tasks
from shared import db
from shared.steam import batch_get_summaries, eval_live_state, cache_player_state
from dotenv import load_dotenv
from pathlib import Path

# .env explizit laden
DOTENV_PATH = Path(r"C:\Users\Nani-Admin\Documents\.env")
load_dotenv(dotenv_path=DOTENV_PATH, override=True)

log = logging.getLogger("LiveMatchMaster")

LIVE_CATEGORIES = [int(x) for x in os.getenv("LIVE_CATEGORIES", "1289721245281292290,1357422957017698478").split(",")]
DEADLOCK_APP_ID = os.getenv("DEADLOCK_APP_ID", "1422450")
STEAM_API_KEY = os.getenv("STEAM_API_KEY") or ""

CHECK_INTERVAL_SEC = int(os.getenv("LIVE_CHECK_INTERVAL_SEC", "60"))  # minütlich

class LiveMatchMaster(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scan_loop.start()

    def cog_unload(self):
        self.scan_loop.cancel()

    @tasks.loop(seconds=CHECK_INTERVAL_SEC)
    async def scan_loop(self):
        if not STEAM_API_KEY:
            log.warning("STEAM_API_KEY fehlt – überspringe Scan.")
            return
        guilds = list(self.bot.guilds)
        channels: List[discord.VoiceChannel] = []
        for g in guilds:
            for cat_id in LIVE_CATEGORIES:
                cat = g.get_channel(cat_id)
                if not cat: continue
                for ch in getattr(cat, "channels", []):
                    if isinstance(ch, discord.VoiceChannel):
                        channels.append(ch)

        # Alle SteamIDs der Members sammeln (Mehrfach-Links möglich)
        chan_members: Dict[int, List[discord.Member]] = {ch.id: [m for m in ch.members if not m.bot] for ch in channels}
        all_user_ids = {m.id for members in chan_members.values() for m in members}
        # Fetch links
        user_links: Dict[int, List[str]] = defaultdict(list)
        if all_user_ids:
            qs = ",".join("?" for _ in all_user_ids)
            rows = db.query_all(f"SELECT user_id, steam_id FROM steam_links WHERE user_id IN ({qs})", tuple(all_user_ids))
            for r in rows:
                user_links[int(r["user_id"])].append(str(r["steam_id"]))

        # Steam summaries (batched)
        all_steam_ids = sorted({sid for arr in user_links.values() for sid in arr})
        async with aiohttp.ClientSession() as session:
            summaries = await batch_get_summaries(session, STEAM_API_KEY, all_steam_ids)

        # pro SteamID bewerten + cachen
        now = int(time.time())
        steam_states: Dict[str, dict] = {}
        for sid in all_steam_ids:
            summary = summaries.get(sid) or {}
            row = eval_live_state(summary, DEADLOCK_APP_ID)
            row["ts"] = now
            steam_states[sid] = row
            cache_player_state(row)

        # pro User: true, wenn irgendeiner seiner Accounts gerade im Match (strict)
        user_in_match: Dict[int, Tuple[bool, str|None]] = {}
        for uid, sids in user_links.items():
            any_true = False
            server_ids: List[str] = []
            for sid in sids:
                st = steam_states.get(sid)
                if not st: continue
                if st["in_match_now_strict"]:
                    any_true = True
                    if st["last_server_id"]:
                        server_ids.append(st["last_server_id"])
            common_server = None
            if server_ids:
                common_server = Counter(server_ids).most_common(1)[0][0]
            user_in_match[uid] = (any_true, common_server)

        # Lane-Mehrheit ermitteln und DB-Status setzen
        for ch, members in chan_members.items():
            if not members:
                # Lane leer → Status resetten
                self._set_lane_inactive(ch, reason="empty")
                continue
            votes = []
            server_votes = []
            for m in members:
                ok, srv = user_in_match.get(m.id, (False, None))
                votes.append(1 if ok else 0)
                if ok and srv:
                    server_votes.append(srv)
            yes = sum(votes)
            no = len(votes) - yes

            # Mehrheit: >= 50% (bei 1 Person reicht 1/1)
            majority = yes >= max(1, (len(votes)+1)//2)

            # Gleicher Server? (mind. 2 gleiche server_ids)
            same_server_ok = False
            if len(server_votes) >= 2:
                top, cnt = Counter(server_votes).most_common(1)[0]
                same_server_ok = cnt >= 2

            activate = False
            reason = ""
            if len(votes) == 1:
                # Solo: reicht, wenn der eine wirklich in Match (strict)
                activate = (yes == 1)
                reason = "solo" if activate else "solo-not"
            else:
                # Team: Mehrheit muss im Match sein UND (falls verfügbar) idealerweise gleicher Server
                activate = majority and (same_server_ok or len(server_votes) == 0)
                reason = "majority+server" if activate and same_server_ok else ("majority" if activate else "no-majority")

            if activate:
                self._set_lane_active(ch, reason)
            else:
                self._set_lane_inactive(ch, reason)

    def _set_lane_active(self, channel_id: int, reason: str):
        row = db.query_one("SELECT is_active, started_at, minutes FROM live_lane_state WHERE channel_id=?", (channel_id,))
        now = int(time.time())
        if row and row["is_active"]:
            db.execute("UPDATE live_lane_state SET last_update=?, reason=? WHERE channel_id=?",
                       (now, reason, channel_id))
            return
        # neu aktiv
        db.execute(
            """
            INSERT INTO live_lane_state(channel_id,is_active,started_at,last_update,minutes,suffix,reason)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(channel_id) DO UPDATE SET
              is_active=1, last_update=excluded.last_update, reason=excluded.reason
            """,
            (channel_id, 1, now, now, 0, "Im Match", reason)
        )

    def _set_lane_inactive(self, channel_id: int, reason: str):
        row = db.query_one("SELECT is_active, minutes FROM live_lane_state WHERE channel_id=?", (channel_id,))
        now = int(time.time())
        if not row:
            db.execute("INSERT INTO live_lane_state(channel_id,is_active,last_update,minutes,reason) VALUES(?,?,?,?,?)",
                       (channel_id, 0, now, 0, reason))
            return
        if row["is_active"]:
            # deaktiveren + Minuten stehen lassen; Worker entfernt Suffix
            db.execute("UPDATE live_lane_state SET is_active=0, last_update=?, reason=? WHERE channel_id=?",
                       (now, reason, channel_id))
        else:
            db.execute("UPDATE live_lane_state SET last_update=?, reason=? WHERE channel_id=?",
                       (now, reason, channel_id))

async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchMaster(bot))
