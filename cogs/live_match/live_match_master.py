# ------------------------------------------------------------
# LiveMatchMaster – Steam-Status auswerten & pro Voice-Lane gruppieren
# Schreibt in shared DB-Tabellen (aus shared/db.py):
#   - steam_links           (wird nur gelesen)
#   - live_lane_members     (per-User Cache je Channel, inkl. server_id)
#   - live_lane_state       (pro Channel: is_active, suffix, last_update, reason)
#
# Anzeige-Idee im Worker:
#   Suffix: "• n/cap im Match", aktiv wenn >= MIN_MATCH_GROUP Spieler
#   dieselbe gameserversteamid teilen.
# ------------------------------------------------------------

import os
import time
import logging
from collections import Counter, defaultdict
from typing import Dict, List, Optional

import aiohttp
import discord
from discord.ext import commands, tasks

from shared import db

log = logging.getLogger("LiveMatchMaster")

# ===== Konfiguration über ENV =====
LIVE_CATEGORIES = [int(x) for x in os.getenv(
    "LIVE_CATEGORIES",
    "1289721245281292290,1357422957017698478"
).split(",") if x.strip()]

DEADLOCK_APP_ID = os.getenv("DEADLOCK_APP_ID", "1422450")
STEAM_API_KEY   = os.getenv("STEAM_API_KEY", "")

CHECK_INTERVAL_SEC = int(os.getenv("LIVE_CHECK_INTERVAL_SEC", "60"))
MIN_MATCH_GROUP    = int(os.getenv("MIN_MATCH_GROUP", "2"))

DEFAULT_CASUAL_CAP = int(os.getenv("DEFAULT_CASUAL_CAP", "8"))
RANKED_CATEGORY_ID = int(os.getenv("RANKED_CATEGORY_ID", "1357422957017698478"))
DEFAULT_RANKED_CAP = int(os.getenv("DEFAULT_RANKED_CAP", "6"))


class LiveMatchMaster(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False

    async def cog_load(self):
        # shared DB initialisiert sich selbst (Schema etc.)
        db.connect()
        if not self._started:
            self.scan_loop.start()
            self._started = True
        log.info(
            "LiveMatchMaster bereit (Categories=%s, Interval=%ss, MinGroup=%d)",
            LIVE_CATEGORIES, CHECK_INTERVAL_SEC, MIN_MATCH_GROUP
        )

    async def cog_unload(self):
        try:
            if self._started:
                self.scan_loop.cancel()
        except Exception:
            pass

    # ========== Steam Helpers ==========
    @staticmethod
    def _in_deadlock(summary: dict) -> bool:
        gid = str(summary.get("gameid", "") or "")
        gex = str(summary.get("gameextrainfo", "") or "")
        return gid == DEADLOCK_APP_ID or gex.lower() == "deadlock"

    @staticmethod
    def _server_id(summary: dict) -> Optional[str]:
        sid = summary.get("gameserversteamid")
        return str(sid) if sid else None

    async def _steam_summaries(self, session: aiohttp.ClientSession, steam_ids: List[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        if not steam_ids:
            return out
        for i in range(0, len(steam_ids), 100):
            chunk = steam_ids[i:i+100]
            url = ("https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
                   f"?key={STEAM_API_KEY}&steamids={','.join(chunk)}")
            try:
                async with session.get(url, timeout=10) as resp:
                    data = await resp.json()
                    for p in data.get("response", {}).get("players", []):
                        sid = str(p.get("steamid"))
                        if sid:
                            out[sid] = p
            except Exception as e:
                log.info("Steam GetPlayerSummaries Fehler: %s", e)
        return out

    # ========== Hauptschleife ==========
    @tasks.loop(seconds=CHECK_INTERVAL_SEC)
    async def scan_loop(self):
        await self.bot.wait_until_ready()
        if not STEAM_API_KEY:
            log.warning("STEAM_API_KEY fehlt – Scan übersprungen.")
            return
        await self._run_once()

    async def _run_once(self):
        # Alle Voice Channels aus konfigurierten Kategorien
        lanes: List[discord.VoiceChannel] = []
        for g in self.bot.guilds:
            for cat_id in LIVE_CATEGORIES:
                cat = g.get_channel(cat_id)
                if isinstance(cat, discord.CategoryChannel):
                    lanes.extend(cat.voice_channels)

        # Discord->Steam Links sammeln
        members = [m for ch in lanes for m in ch.members if not m.bot]
        user_ids = sorted({m.id for m in members})
        links = defaultdict(list)  # user_id -> [steam_id,...]
        if user_ids:
            qs = ",".join("?" for _ in user_ids)
            rows = db.query_all(
                f"SELECT user_id, steam_id FROM steam_links WHERE user_id IN ({qs})",
                tuple(user_ids)
            )
            for r in rows:
                links[int(r["user_id"])].append(str(r["steam_id"]))

        # Steam zusammengefasst abfragen
        all_steam = sorted({sid for arr in links.values() for sid in arr})
        async with aiohttp.ClientSession() as session:
            summaries = await self._steam_summaries(session, all_steam)

        now = int(time.time())

        # Pro Lane auswerten & in DB schreiben
        for ch in lanes:
            nonbots = [m for m in ch.members if not m.bot]
            if not nonbots:
                self._write_lane_state(ch.id, active=0, suffix=None, ts=now, reason="empty")
                self._clear_lane_members(ch.id)
                continue

            # pro User: Server-ID, wenn in Deadlock
            server_ids = []
            lane_members_rows = []
            for m in nonbots:
                found_sid = None
                for sid in links.get(m.id, []):
                    s = summaries.get(sid)
                    if not s:
                        continue
                    if self._in_deadlock(s):
                        found_sid = self._server_id(s)
                        break
                # in DB live_lane_members speichern (auch None zulassen)
                lane_members_rows.append((ch.id, m.id, 1 if found_sid else 0, found_sid, now))
                if found_sid:
                    server_ids.append(found_sid)

            # Cache lane members aktualisieren
            self._upsert_lane_members(lane_members_rows)

            # Gruppierung per Server-ID
            active_suffix = None
            is_active = 0
            if server_ids:
                cnt = Counter(server_ids)
                srv_id, n = cnt.most_common(1)[0]
                if n >= max(1, MIN_MATCH_GROUP):
                    cap = self._cap(ch)
                    active_suffix = f"• {n}/{cap} im Match"
                    is_active = 1

            self._write_lane_state(
                ch.id,
                active=is_active,
                suffix=active_suffix,
                ts=now,
                reason="same_server" if is_active else "no_group"
            )

    def _cap(self, ch: discord.VoiceChannel) -> int:
        if ch.user_limit and ch.user_limit > 0:
            return int(ch.user_limit)
        return DEFAULT_RANKED_CAP if ch.category_id == RANKED_CATEGORY_ID else DEFAULT_CASUAL_CAP

    # ========== DB-Helper (shared.db, synchron) ==========
    def _write_lane_state(self, channel_id: int, *, active: int, suffix: Optional[str], ts: int, reason: str):
        db.execute(
            """
            INSERT INTO live_lane_state(channel_id, is_active, last_update, suffix, reason)
            VALUES(?,?,?,?,?)
            ON CONFLICT(channel_id) DO UPDATE SET
              is_active=excluded.is_active,
              last_update=excluded.last_update,
              suffix=excluded.suffix,
              reason=excluded.reason
            """,
            (int(channel_id), int(active), int(ts), suffix or None, reason)
        )

    def _clear_lane_members(self, channel_id: int):
        db.execute("DELETE FROM live_lane_members WHERE channel_id=?", (int(channel_id),))

    def _upsert_lane_members(self, rows: List[tuple]):
        if not rows:
            return
        db.executemany(
            """
            INSERT INTO live_lane_members(channel_id, user_id, in_match, server_id, checked_ts)
            VALUES(?,?,?,?,?)
            ON CONFLICT(channel_id, user_id) DO UPDATE SET
              in_match=excluded.in_match,
              server_id=excluded.server_id,
              checked_ts=excluded.checked_ts
            """,
            rows
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchMaster(bot))
