# cogs/live_match/live_match_logger.py
# ------------------------------------------------------------
# LiveMatchLogger (V2-Snapshots)
#
# Schreibt in die DB:
#   - lane_snapshot_v2           (ein Datensatz pro Voice-Channel / Scan)
#   - lane_snapshot_member_v2    (ein Datensatz pro Mitglied im Channel / Scan)
#
# Falls Legacy-Tabellen vorhanden sind, kann optional gespiegelt werden:
#   - lane_snapshot
#   - lane_snapshot_member
#
# Signals:
#   - in_deadlock: Steam erkennt Deadlock (gameid=1422450 oder gameextrainfo='Deadlock')
#   - in_match:    Steam liefert gameserversteamid (→ Ground Truth)
#
# Abhängigkeiten:
#   - service.db (Wrapper mit .connect(), .execute(sql, params?), .query_one(), .query_all(), .executemany())
#   - discord.py
#   - aiohttp
#
# ENVs:
#   - STEAM_API_KEY (optional; ohne Key wird geloggt, aber in_deadlock/in_match bleiben 0)
#   - SCAN_INTERVAL_SEC (default 120)
#   - TEAM_SIZE_CAP (default 6)  -> nur informativ im Snapshot (cap)
#   - MIRROR_TO_V1 (default 0)   -> 1 = zusätzlich in legacy-Tabellen spiegeln, falls vorhanden
# ------------------------------------------------------------

from __future__ import annotations

import os
import time
import json
import logging
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple, Iterator

import aiohttp
import discord
from discord.ext import commands, tasks
import asyncio
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
    """Erzeugt V2-Tabellen & Indexe idempotent. Spiegelt NICHTs automatisch."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS lane_snapshot_v2(
          snapshot_id        INTEGER PRIMARY KEY AUTOINCREMENT,
          channel_id         INTEGER NOT NULL,
          channel_name       TEXT,
          observed_at        INTEGER NOT NULL,         -- epoch seconds
          phase              TEXT,                     -- MATCH/GAME/LOBBY/OFF
          suffix             TEXT,
          majority_server_id TEXT,
          majority_n         INTEGER,
          in_game_n          INTEGER,                  -- Anzahl Member mit server_id (ground truth)
          in_deadlock_n      INTEGER,                  -- Anzahl Member mit Deadlock offen
          voice_n            INTEGER,                  -- Anzahl anwesender Nicht-Bot-Mitglieder
          cap                INTEGER,                  -- erwartete Teamgröße (UI/Info)
          reason             TEXT                      -- Debug-String "k=v;..."; Quelle: live_lane_state.reason
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
    # Sinnvolle Indexe
    db.execute("CREATE INDEX IF NOT EXISTS idx_lsv2_observed ON lane_snapshot_v2(observed_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lsmv2_snap ON lane_snapshot_member_v2(snapshot_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lsmv2_user ON lane_snapshot_member_v2(user_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lsmv2_server ON lane_snapshot_member_v2(server_id)")

def _legacy_tables_exist() -> bool:
    r1 = db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='lane_snapshot'")
    r2 = db.query_one("SELECT name FROM sqlite_master WHERE type='table' AND name='lane_snapshot_member'")
    return bool(r1 and r1.get("name")) and bool(r2 and r2.get("name"))

def _parse_phase_from_reason(reason: Optional[str], suffix: Optional[str]) -> str:
    """Extrahiert 'phase' aus 'reason' (k=v;..). Fallback via Suffix. OFF als Default."""
    # reason: "phase=GAME;capY=6;..." -> GAME
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
    if "im match" in s:
        return "MATCH"
    if "im spiel" in s:
        return "GAME"
    if "lobby/queue" in s:
        return "LOBBY"
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
            log.warning("STEAM_API_KEY fehlt – Snapshots ohne Steam-Status (in_deadlock/in_match bleiben 0).")
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

    # -------------------------------------------------------------------------
    @tasks.loop(seconds=SCAN_INTERVAL_SEC)
    async def scan(self):
        await self.bot.wait_until_ready()
        now = int(time.time())

        # 1) alle Voice-Channels sammeln
        voice_channels: List[discord.VoiceChannel] = []
        for g in self.bot.guilds:
            voice_channels.extend(g.voice_channels)
        if not voice_channels:
            return

        # 2) nicht-bot Mitglieder sammeln
        members: List[discord.Member] = []
        for ch in voice_channels:
            members.extend([m for m in ch.members if not m.bot])
        user_ids = sorted({m.id for m in members})
        if not user_ids:
            return

        # 3) steam_links mappen
        links = defaultdict(list)  # user_id -> [steam_id,...]
        qs = ",".join("?" for _ in user_ids)
        try:
            rows = db.query_all(
                f"SELECT user_id, steam_id FROM steam_links WHERE user_id IN ({qs})",
                tuple(user_ids),
            )
            for r in rows:
                links[int(r["user_id"])].append(str(r["steam_id"]))
        except Exception as e:
            log.error("Fehler beim Lesen steam_links: %s", e)
            return

        # 4) Steam PlayerSummaries abrufen (chunkweise)
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
                    except asyncio.TimeoutError:
                        log.warning("Steam GetPlayerSummaries Timeout (chunk size=%d)", len(chunk))
                    except Exception as e:
                        log.warning("Steam GetPlayerSummaries Fehler: %s", e)

        # 5) pro Channel Snapshot bilden
        for ch in voice_channels:
            nonbots = [m for m in ch.members if not m.bot]
            if not nonbots:
                continue

            # live_lane_state für Phase/Suffix/Reason lesen (falls vorhanden)
            lane_row = db.query_one(
                "SELECT is_active, suffix, reason FROM live_lane_state WHERE channel_id=?",
                (ch.id,),
            )
            lane_suffix = ""
            lane_reason = None
            if lane_row:
                lane_suffix = (lane_row.get("suffix") or "").strip()
                lane_reason = lane_row.get("reason")

            lane_phase = _parse_phase_from_reason(lane_reason, lane_suffix)

            in_game_with_server: List[str] = []  # server_ids
            in_deadlock_users: List[int] = []
            member_rows: List[Tuple[int, str | None, str, int, int, str | None]] = []

            # Mitglieder durchgehen
            for m in nonbots:
                sids = links.get(m.id, [])
                best_sid = None
                best_persona = ""
                best_server = None
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
                    (
                        m.id,
                        best_sid,
                        best_persona,
                        1 if in_dl else 0,
                        1 if in_match else 0,
                        best_server,
                    )
                )

            # Mehrheits-Server-ID (optional)
            maj_server = None
            maj_n = 0
            if in_game_with_server:
                cnt = Counter(in_game_with_server)
                maj_server, maj_n = cnt.most_common(1)[0]

            voice_n = len(nonbots)
            in_game_n = len(in_game_with_server)
            in_deadlock_n = len(in_deadlock_users)

            # ---- INSERT Snapshot (V2) ----------------------------------------
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
                        int(ch.id),
                        ch.name,
                        now,
                        lane_phase,
                        lane_suffix or None,
                        maj_server,
                        int(maj_n),
                        int(in_game_n),
                        int(in_deadlock_n),
                        int(voice_n),
                        int(TEAM_SIZE_CAP),
                        lane_reason,
                    ),
                )
                snap_id = db.query_one("SELECT last_insert_rowid() AS id")["id"]
            except Exception as e:
                log.error("INSERT lane_snapshot_v2 fehlgeschlagen (%s): %s", ch.id, e)
                continue

            # ---- INSERT Members (V2) -----------------------------------------
            try:
                db.executemany(
                    """
                    INSERT INTO lane_snapshot_member_v2(
                        snapshot_id, user_id, steam_id, personaname,
                        in_deadlock, in_match, server_id
                    )
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            int(snap_id),
                            int(uid),
                            sid,
                            pname,
                            int(in_dl),
                            int(in_match),
                            srv,
                        )
                        for (uid, sid, pname, in_dl, in_match, srv) in member_rows
                    ],
                )
            except Exception as e:
                log.error("INSERT lane_snapshot_member_v2 fehlgeschlagen (%s): %s", ch.id, e)

            # ---- Optional: Legacy-Spiegelung ---------------------------------
            if self._legacy_mirror:
                try:
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
                            int(ch.id),
                            ch.name,
                            now,
                            lane_phase,
                            lane_suffix or None,
                            maj_server,
                            int(maj_n),
                            int(in_game_n),
                            int(in_deadlock_n),
                            int(voice_n),
                            int(TEAM_SIZE_CAP),
                            lane_reason,
                        ),
                    )
                    legacy_snap = db.query_one("SELECT last_insert_rowid() AS id")["id"]
                    db.executemany(
                        """
                        INSERT INTO lane_snapshot_member(
                            snapshot_id, user_id, steam_id, personaname,
                            in_deadlock, in_match, server_id
                        )
                        VALUES(?,?,?,?,?,?,?)
                        """,
                        [
                            (
                                int(legacy_snap),
                                int(uid),
                                sid,
                                pname,
                                int(in_dl),
                                int(in_match),
                                srv,
                            )
                            for (uid, sid, pname, in_dl, in_match, srv) in member_rows
                        ],
                    )
                except Exception as e:
                    log.warning("Legacy-Spiegelung fehlgeschlagen (%s): %s", ch.id, e)

    # -------------------------------------------------------------------------

async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchLogger(bot))
