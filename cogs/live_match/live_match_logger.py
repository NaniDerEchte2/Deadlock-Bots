# cogs/live_match/live_match_logger.py
# ------------------------------------------------------------
# LiveMatchLogger – schreibt alle 2 Minuten Snapshots in die DB:
#   - lane_snapshot             (ein Datensatz pro Voice-Channel)
#   - lane_snapshot_member      (ein Datensatz pro Mitglied im Channel)
#
# Unabhängig vom Renamer. Es werden ALLE Voice-Mitglieder geprüft:
#   steam_links -> GetPlayerSummaries -> Deadlock/Server-Status.
#
# Feste Defaults, KEINE ENV-Schlacht. Teamgröße = 6.
# sqlite3.Row: Zugriff nur per ["spalte"], kein .get().
# ------------------------------------------------------------

import os
import time
import logging
from collections import Counter, defaultdict
from typing import Dict, List

import aiohttp
import discord
from discord.ext import commands, tasks

from shared import db

log = logging.getLogger("LiveMatchLogger")

SCAN_INTERVAL_SEC = 120
TEAM_SIZE = 6
STEAM_APP_DEADLOCK = "1422450"
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "").strip()

def _ensure_schema():
    db.execute("""
        CREATE TABLE IF NOT EXISTS lane_snapshot(
          snapshot_id        INTEGER PRIMARY KEY AUTOINCREMENT,
          channel_id         INTEGER NOT NULL,
          channel_name       TEXT,
          observed_at        INTEGER NOT NULL,
          phase              TEXT,
          suffix             TEXT,
          majority_server_id TEXT,
          majority_n         INTEGER,
          in_game_n          INTEGER,
          in_deadlock_n      INTEGER,
          voice_n            INTEGER,
          cap                INTEGER,
          reason             TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS lane_snapshot_member(
          snapshot_id   INTEGER NOT NULL,
          user_id       INTEGER NOT NULL,
          steam_id      TEXT,
          personaname   TEXT,
          in_deadlock   INTEGER NOT NULL,
          in_match      INTEGER NOT NULL,
          server_id     TEXT,
          PRIMARY KEY(snapshot_id, user_id)
        )
    """)

class LiveMatchLogger(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False

    async def cog_load(self):
        db.connect()
        _ensure_schema()
        if not STEAM_API_KEY:
            log.warning("STEAM_API_KEY fehlt – Snapshots laufen, aber ohne Steam-Status.")
        if not self._started:
            self.scan.start()
            self._started = True
        log.info("LiveMatchLogger aktiv (Intervall=%ss)", SCAN_INTERVAL_SEC)

    async def cog_unload(self):
        try:
            if self._started:
                self.scan.cancel()
        except Exception:
            pass

    @tasks.loop(seconds=SCAN_INTERVAL_SEC)
    async def scan(self):
        await self.bot.wait_until_ready()
        now = int(time.time())

        # 1) Alle Voice-Channels
        voice_channels: List[discord.VoiceChannel] = []
        for g in self.bot.guilds:
            voice_channels.extend(g.voice_channels)
        if not voice_channels:
            return

        # 2) Voice-Mitglieder (ohne Bots)
        members: List[discord.Member] = []
        for ch in voice_channels:
            members.extend([m for m in ch.members if not m.bot])
        user_ids = sorted({m.id for m in members})
        if not user_ids:
            return

        # 3) steam_links mappen
        links = defaultdict(list)  # user_id -> [steam_id,...]
        qs = ",".join("?" for _ in user_ids)
        rows = db.query_all(
            f"SELECT user_id, steam_id FROM steam_links WHERE user_id IN ({qs})",
            tuple(user_ids)
        )
        for r in rows:
            links[int(r["user_id"])].append(str(r["steam_id"]))

        # 4) Steam Summaries (chunkweise)
        summaries: Dict[str, dict] = {}
        if STEAM_API_KEY:
            async with aiohttp.ClientSession() as session:
                for chunk in _chunks(sorted({sid for arr in links.values() for sid in arr}), 100):
                    url = ("https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
                           f"?key={STEAM_API_KEY}&steamids={','.join(chunk)}")
                    try:
                        async with session.get(url, timeout=10) as resp:
                            data = await resp.json()
                            for p in data.get("response", {}).get("players", []):
                                sid = str(p.get("steamid"))
                                if sid:
                                    summaries[sid] = p
                    except Exception as e:
                        log.warning("Steam Summaries Fehler: %s", e)

        # 5) Snapshot pro Channel
        for ch in voice_channels:
            nonbots = [m for m in ch.members if not m.bot]
            if not nonbots:
                continue

            lane_row = db.query_one(
                "SELECT is_active, suffix, reason FROM live_lane_state WHERE channel_id=?",
                (ch.id,)
            )
            lane_suffix = ""
            lane_reason = None
            if lane_row:
                if lane_row["suffix"] is not None:
                    lane_suffix = str(lane_row["suffix"]).strip()
                lane_reason = lane_row["reason"]

            in_game_with_server: List[str] = []
            in_deadlock_users: List[int] = []
            member_rows: List[tuple] = []

            for m in nonbots:
                sids = links.get(m.id, [])
                best_sid = None
                best_server = None
                best_persona = None
                in_dl = False
                in_match = False

                for sid in sids:
                    p = summaries.get(sid)
                    if not p:
                        continue
                    gameid = str(p.get("gameid", "") or "")
                    gex = str(p.get("gameextrainfo", "") or "")
                    if (gameid == STEAM_APP_DEADLOCK) or (gex.lower() == "deadlock"):
                        in_dl = True
                        srv = p.get("gameserversteamid")
                        if srv:
                            in_match = True
                            best_sid = sid
                            best_server = str(srv)
                            best_persona = p.get("personaname")
                            break
                        else:
                            best_sid = sid
                            best_persona = p.get("personaname")

                if in_dl:
                    in_deadlock_users.append(m.id)
                if in_match and best_server:
                    in_game_with_server.append(best_server)

                member_rows.append((
                    m.id,
                    best_sid,
                    best_persona or "",
                    1 if in_dl else 0,
                    1 if in_match else 0,
                    best_server
                ))

            maj_server = None
            maj_n = 0
            if in_game_with_server:
                cnt = Counter(in_game_with_server)
                maj_server, maj_n = cnt.most_common(1)[0]

            cap = TEAM_SIZE
            voice_n = len(nonbots)
            in_game_n = len(in_game_with_server)
            in_deadlock_n = len(in_deadlock_users)

            if "Im Match" in lane_suffix:
                phase = "MATCH"
            elif "Im Spiel" in lane_suffix:
                phase = "GAME"
            elif "Lobby/Queue" in lane_suffix:
                phase = "LOBBY"
            else:
                phase = "OFF"

            db.execute(
                """
                INSERT INTO lane_snapshot(
                    channel_id, channel_name, observed_at, phase, suffix,
                    majority_server_id, majority_n, in_game_n, in_deadlock_n,
                    voice_n, cap, reason
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ch.id, ch.name, now, phase, (lane_suffix or None),
                    maj_server, maj_n, in_game_n, in_deadlock_n,
                    voice_n, cap, lane_reason
                )
            )
            snap_id = db.query_one("SELECT last_insert_rowid() AS id")["id"]

            for (uid, sid, pname, in_dl, in_match, srv) in member_rows:
                db.execute(
                    """
                    INSERT INTO lane_snapshot_member(
                        snapshot_id, user_id, steam_id, personaname,
                        in_deadlock, in_match, server_id
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (snap_id, int(uid), sid, pname, int(in_dl), int(in_match), srv)
                )

def _chunks(seq: List[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i+n]

async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchLogger(bot))
