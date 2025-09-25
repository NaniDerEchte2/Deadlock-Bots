# cogs/live_match/live_match_logger.py
# ------------------------------------------------------------
# LiveMatchLogger (V2-Snapshots)
# Schreibt in:
#   - lane_snapshot_v2
#   - lane_snapshot_member_v2
# Optional (ENV MIRROR_TO_V1=1): Spiegel in legacy lane_snapshot / lane_snapshot_member
# ------------------------------------------------------------

from __future__ import annotations

import os
import time
import logging
import asyncio
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple, Iterator

import aiohttp
import discord
from discord.ext import commands, tasks

from service import db

log = logging.getLogger("LiveMatchLogger")

# ---- Konfiguration -----------------------------------------------------------

STEAM_API_KEY = os.getenv("STEAM_API_KEY", "").strip()
DEADLOCK_APP_ID = os.getenv("DEADLOCK_APP_ID", "1422450").strip()

SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "120"))
TEAM_SIZE_CAP = int(os.getenv("TEAM_SIZE_CAP", "6"))
MIRROR_TO_V1 = int(os.getenv("MIRROR_TO_V1", "0"))  # 1 = auch lane_snapshot / lane_snapshot_member

# ---- Schema & Helpers --------------------------------------------------------

def _ensure_schema_v2() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS lane_snapshot_v2(
          snapshot_id        INTEGER PRIMARY KEY AUTOINCREMENT,
          channel_id         INTEGER NOT NULL,
          channel_name       TEXT,
          observed_at        INTEGER NOT NULL,
          phase              TEXT,     -- MATCH/GAME/LOBBY/OFF
          suffix             TEXT,
          majority_server_id TEXT,
          majority_n         INTEGER,
          in_game_n          INTEGER,
          in_deadlock_n      INTEGER,
          voice_n            INTEGER,
          cap                INTEGER,
          reason             TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS lane_snapshot_member_v2(
          snapshot_id   INTEGER NOT NULL,
          user_id       INTEGER NOT NULL,
          steam_id      TEXT,
          personaname   TEXT,
          in_deadlock   INTEGER NOT NULL,
          in_match      INTEGER NOT NULL,
          server_id     TEXT,
          PRIMARY KEY(snapshot_id, user_id)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_lsv2_observed ON lane_snapshot_v2(observed_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lsmv2_snap    ON lane_snapshot_member_v2(snapshot_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lsmv2_user    ON lane_snapshot_member_v2(user_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lsmv2_server  ON lane_snapshot_member_v2(server_id)")

def _row_get(row, key, default=None):
    """Row-sicherer Getter: funktioniert für sqlite3.Row und dict."""
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        try:
            return row.get(key, default)  # type: ignore[attr-defined]
        except Exception:
            return default

def _legacy_tables_exist() -> bool:
    r1 = db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='lane_snapshot'")
    r2 = db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='lane_snapshot_member'")
    return bool(_row_get(r1, "name")) and bool(_row_get(r2, "name"))

def _parse_phase_from_reason(reason: Optional[str], suffix: Optional[str]) -> str:
    if isinstance(reason, str):
        try:
            parts = [p for p in reason.split(";") if "=" in p]
            kv = {k.strip(): v.strip() for k, v in (p.split("=", 1) for p in parts)}
            ph = kv.get("phase")
            if ph:
                return ph
        except Exception:
            pass
    s = (suffix or "").lower()
    if "im match" in s:  return "MATCH"
    if "im spiel" in s:  return "GAME"
    if "lobby/queue" in s: return "LOBBY"
    return "OFF"

def _is_deadlock(summary: dict) -> bool:
    gid = str(summary.get("gameid", "") or "")
    gex = str(summary.get("gameextrainfo", "") or "")
    return gid == DEADLOCK_APP_ID or gex.lower() == "deadlock"

def _server_id(summary: dict) -> Optional[str]:
    sid = summary.get("gameserversteamid")
    return str(sid) if sid else None

def _chunks(seq: List[str], size: int) -> Iterator[List[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]

# ---- Cog ---------------------------------------------------------------------

class LiveMatchLogger(commands.Cog):
    """Scannt Voice-Lanes und schreibt V2-Snapshots in die DB."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False
        self._legacy_mirror = False

    async def cog_load(self):
        db.connect()
        _ensure_schema_v2()
        self._legacy_mirror = bool(MIRROR_TO_V1 and _legacy_tables_exist())
        if not STEAM_API_KEY:
            log.warning("STEAM_API_KEY fehlt – Snapshots laufen, aber Steam-Status bleibt leer (in_deadlock/in_match=0).")
        if not self._started:
            self.scan.start()
            self._started = True
        log.info(
            "LiveMatchLogger aktiv (interval=%ss, cap=%d, mirror_legacy=%s)",
            SCAN_INTERVAL_SEC, TEAM_SIZE_CAP, self._legacy_mirror
        )

    async def cog_unload(self):
        if self._started:
            try:
                self.scan.cancel()
            except Exception as e:
                log.debug("scan.cancel() beim Unload: %r", e)
            self._started = False

    @tasks.loop(seconds=SCAN_INTERVAL_SEC)
    async def scan(self):
        await self.bot.wait_until_ready()
        now = int(time.time())

        # 1) Voice-Channels
        voice_channels: List[discord.VoiceChannel] = []
        for g in self.bot.guilds:
            voice_channels.extend(g.voice_channels)
        if not voice_channels:
            return

        # 2) Mitglieder (ohne Bots)
        members: List[discord.Member] = []
        for ch in voice_channels:
            members.extend([m for m in ch.members if not m.bot])
        user_ids = sorted({m.id for m in members})
        if not user_ids:
            return

        # 3) steam_links
        links = defaultdict(list)  # user_id -> [steam_id,...]
        qs = ",".join("?" for _ in user_ids)
        try:
            rows = db.query_all(
                f"SELECT user_id, steam_id FROM steam_links WHERE user_id IN ({qs})",
                tuple(user_ids),
            )
            for r in rows:
                links[int(_row_get(r, "user_id"))].append(str(_row_get(r, "steam_id")))
        except Exception as e:
            log.error("Fehler beim Lesen steam_links: %s", e)
            return

        # 4) Steam Summaries
        summaries: Dict[str, dict] = {}
        if STEAM_API_KEY:
            async with aiohttp.ClientSession() as session:
                for chunk in _chunks(sorted({sid for arr in links.values() for sid in arr}), 100):
                    url = (
                        "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
                        f"?key={STEAM_API_KEY}&steamids={','.join(chunk)}"
                    )
                    try:
                        async with session.get(url, timeout=12) as resp:
                            data = await resp.json()
                            for p in data.get("response", {}).get("players", []):
                                sid = str(p.get("steamid") or "")
                                if sid:
                                    summaries[sid] = p
                    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                        log.warning("Steam GetPlayerSummaries Netzwerkfehler/Timeout: %s (chunk=%d)", e, len(chunk))
                    except Exception as e:
                        log.warning("Steam GetPlayerSummaries unerwarteter Fehler: %s", e)

        # 5) Snapshot pro Channel
        for ch in voice_channels:
            nonbots = [m for m in ch.members if not m.bot]
            if not nonbots:
                continue

            lane_row = db.query_one(
                "SELECT is_active, suffix, reason FROM live_lane_state WHERE channel_id=?",
                (ch.id,),
            )
            lane_suffix = (_row_get(lane_row, "suffix") or "").strip()
            lane_reason = _row_get(lane_row, "reason")
            lane_phase  = _parse_phase_from_reason(lane_reason, lane_suffix)

            in_game_with_server: List[str] = []
            in_deadlock_users: List[int] = []
            member_rows: List[Tuple[int, Optional[str], str, int, int, Optional[str]]] = []

            for m in nonbots:
                sids = links.get(m.id, [])
                best_sid: Optional[str] = None
                best_persona = ""
                best_server: Optional[str] = None
                in_dl = False
                in_match = False

                if STEAM_API_KEY and sids:
                    for sid in sids:
                        p = summaries.get(sid)
                        if not p:
                            continue
                        if _is_deadlock(p):
                            in_dl = True
                            server = _server_id(p)
                            if server:
                                in_match = True
                                best_sid = sid
                                best_persona = p.get("personaname") or ""
                                best_server = server
                                break
                            else:
                                best_sid = sid
                                best_persona = p.get("personaname") or ""

                if in_dl:
                    in_deadlock_users.append(m.id)
                if in_match and best_server:
                    in_game_with_server.append(best_server)

                member_rows.append(
                    (m.id, best_sid, best_persona, 1 if in_dl else 0, 1 if in_match else 0, best_server)
                )

            maj_server = None
            maj_n = 0
            if in_game_with_server:
                cnt = Counter(in_game_with_server)
                maj_server, maj_n = cnt.most_common(1)[0]

            voice_n = len(nonbots)
            in_game_n = len(in_game_with_server)
            in_deadlock_n = len(in_deadlock_users)

            # ---- INSERT Snapshot (V2)
            try:
                db.execute(
                    """
                    INSERT INTO lane_snapshot_v2(
                      channel_id, channel_name, observed_at, phase, suffix,
                      majority_server_id, majority_n, in_game_n, in_deadlock_n,
                      voice_n, cap, reason
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        int(ch.id), ch.name, now, lane_phase, (lane_suffix or None),
                        maj_server, int(maj_n), int(in_game_n), int(in_deadlock_n),
                        int(voice_n), int(TEAM_SIZE_CAP), lane_reason
                    ),
                )
                snap_id = _row_get(db.query_one("SELECT last_insert_rowid() AS id"), "id")
            except Exception as e:
                log.error("INSERT lane_snapshot_v2 fehlgeschlagen (%s): %s", ch.id, e)
                continue

            # ---- INSERT Members (V2)
            try:
                db.executemany(
                    """
                    INSERT INTO lane_snapshot_member_v2(
                      snapshot_id, user_id, steam_id, personaname,
                      in_deadlock, in_match, server_id
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    [
                        (int(snap_id), int(uid), sid, pname, int(in_dl), int(in_match), srv)
                        for (uid, sid, pname, in_dl, in_match, srv) in member_rows
                    ],
                )
            except Exception as e:
                log.error("INSERT lane_snapshot_member_v2 fehlgeschlagen (%s): %s", ch.id, e)

            # ---- Optional: Legacy-Spiegelung
            if self._legacy_mirror:
                try:
                    db.execute(
                        """
                        INSERT INTO lane_snapshot(
                          channel_id, channel_name, observed_at, phase, suffix,
                          majority_server_id, majority_n, in_game_n, in_deadlock_n,
                          voice_n, cap, reason
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            int(ch.id), ch.name, now, lane_phase, (lane_suffix or None),
                            maj_server, int(maj_n), int(in_game_n), int(in_deadlock_n),
                            int(voice_n), int(TEAM_SIZE_CAP), lane_reason
                        ),
                    )
                    legacy_snap = _row_get(db.query_one("SELECT last_insert_rowid() AS id"), "id")
                    db.executemany(
                        """
                        INSERT INTO lane_snapshot_member(
                          snapshot_id, user_id, steam_id, personaname,
                          in_deadlock, in_match, server_id
                        ) VALUES(?,?,?,?,?,?,?)
                        """,
                        [
                            (int(legacy_snap), int(uid), sid, pname, int(in_dl), int(in_match), srv)
                            for (uid, sid, pname, in_dl, in_match, srv) in member_rows
                        ],
                    )
                except Exception as e:
                    log.warning("Legacy-Spiegelung fehlgeschlagen (%s): %s", ch.id, e)

async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchLogger(bot))
