# filename: cogs/live_match/live_match_master.py
# ------------------------------------------------------------
# LiveMatchMaster v3.4 – Cold-Start-Resync + Category-Scoped Scan + Confidence
# Fix: SQLite-Migration ohne nicht-konstante DEFAULTs (backfill via UPDATE)
# ------------------------------------------------------------

import os
import time
import asyncio
import logging
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple, Any

import aiohttp
import discord
from discord.ext import commands, tasks

from service import db, steam as steam_service_module

log = logging.getLogger("LiveMatchMaster")

# ---- Konfiguration -----------------------------------------------------------

LIVE_CATEGORIES = [1289721245281292290, 1412804540994162789, 1357422957017698478]
DEADLOCK_APP_ID = os.getenv("DEADLOCK_APP_ID", "1422450")
STEAM_API_KEY   = os.getenv("STEAM_API_KEY", "").strip()

CHECK_INTERVAL_SEC    = int(os.getenv("LIVE_CHECK_INTERVAL_SEC", "15"))
MIN_MATCH_GROUP       = int(os.getenv("MIN_MATCH_GROUP", "2"))
MAX_MATCH_CAP         = int(os.getenv("MAX_MATCH_CAP", "6"))

ENABLE_RICH_PRESENCE = os.getenv("ENABLE_RICH_PRESENCE", "1").lower() not in {"0", "false", "no"}
RICH_PRESENCE_STALE_SEC = int(os.getenv("RICH_PRESENCE_STALE_SEC", "60"))

REQUIRE_STABILITY_SEC = int(os.getenv("REQUIRE_STABILITY_SEC", "30"))
LOBBY_GRACE_SEC       = int(os.getenv("LOBBY_GRACE_SEC", "90"))

AUTO_SUFFIX_MATCH  = os.getenv("AUTO_SUFFIX_MATCH",  "")
AUTO_SUFFIX_GAME   = os.getenv("AUTO_SUFFIX_GAME",   "")
AUTO_SUFFIX_LOBBY  = os.getenv("AUTO_SUFFIX_LOBBY",  "")

PHASE_OFF   = "OFF"
PHASE_GAME  = "GAME"
PHASE_LOBBY = "LOBBY"
PHASE_MATCH = "MATCH"

_RP_MATCH_TERMS = (
    "#deadlock_status_inmatch",
    "im match",
    "in match",
    "match",
    "playing match",
)
_RP_LOBBY_TERMS = (
    "lobby",
    "queue",
    "warteschlange",
    "search",
    "searching",
    "suche",
)
_RP_GAME_TERMS = (
    "#deadlock_status_ingame",
    "im spiel",
    "ingame",
    "playing",
    "spiel",
    "game",
)

# ---- Helpers ----------------------------------------------------------------

def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def _fmt_suffix(dl_count: int, voice_n: int, label: str) -> str:
    voice_n = max(0, int(voice_n))
    n = max(0, min(int(dl_count), voice_n))
    return f"• {n}/{voice_n} (max {MAX_MATCH_CAP}) {label}".strip()

# --- Schema helpers for safe migrations (ohne nicht-konstante DEFAULTs) ------
def _has_column(table: str, col: str) -> bool:
    rows = db.query_all(f"PRAGMA table_info({table})")
    for r in rows:
        name = r["name"] if isinstance(r, dict) else r[1]
        if name == col:
            return True
    return False

def _add_column_no_default(table: str, col: str, decl: str) -> bool:
    """
    Fügt eine Spalte ohne DEFAULT hinzu (SQLite erlaubt nur konstante DEFAULTs).
    Return: True, wenn neu hinzugefügt wurde.
    """
    if _has_column(table, col):
        return False
    db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    return True

def _ensure_schema() -> None:
    # steam_links
    db.execute("""
        CREATE TABLE IF NOT EXISTS steam_links(
          user_id         INTEGER NOT NULL,
          steam_id        TEXT    NOT NULL,
          name            TEXT,
          verified        INTEGER DEFAULT 0,
          primary_account INTEGER DEFAULT 0,
          created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
          updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (user_id, steam_id)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_user  ON steam_links(user_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_steam ON steam_links(steam_id)")

    # live_lane_members (Achtung: keine nicht-konstante DEFAULTs)
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_lane_members(
          channel_id  INTEGER NOT NULL,
          user_id     INTEGER NOT NULL,
          in_deadlock INTEGER NOT NULL DEFAULT 0,
          server_id   TEXT,
          last_seen   INTEGER NOT NULL,
          PRIMARY KEY (channel_id, user_id)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_llm_channel ON live_lane_members(channel_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_llm_server  ON live_lane_members(server_id)")

    # live_lane_state
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_lane_state(
          channel_id  INTEGER PRIMARY KEY,
          is_active   INTEGER NOT NULL DEFAULT 0,
          last_update INTEGER NOT NULL,
          suffix      TEXT,
          reason      TEXT
        )
    """)

    # live_decision_metrics_v3
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_decision_metrics_v3(
          id                       INTEGER PRIMARY KEY AUTOINCREMENT,
          ts                       INTEGER NOT NULL,
          channel_id               INTEGER NOT NULL,
          voice_n                  INTEGER NOT NULL,
          y_cap                    INTEGER NOT NULL,
          phase                    TEXT NOT NULL,
          majority_server_id       TEXT,
          majority_n               INTEGER,
          ig_count                 INTEGER,
          dl_count                 INTEGER,
          override_in_match_count  INTEGER NOT NULL DEFAULT 0,
          cohort_imputable_count   INTEGER NOT NULL DEFAULT 0,
          stable_for_sec           INTEGER NOT NULL DEFAULT 0,
          reason                   TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_ldm3_ts          ON live_decision_metrics_v3(ts)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ldm3_channel_ts ON live_decision_metrics_v3(channel_id, ts)")

    # live_member_metrics_v3
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_member_metrics_v3(
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          ts          INTEGER NOT NULL,
          channel_id  INTEGER NOT NULL,
          user_id     INTEGER NOT NULL,
          in_deadlock INTEGER NOT NULL,
          server_id   TEXT,
          in_match_db INTEGER NOT NULL,
          source      TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_lmm3_ts          ON live_member_metrics_v3(ts)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lmm3_channel_ts ON live_member_metrics_v3(channel_id, ts)")

    # live_estimated_match_v1
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_estimated_match_v1(
          id                 INTEGER PRIMARY KEY AUTOINCREMENT,
          ts                 INTEGER NOT NULL,
          channel_id         INTEGER NOT NULL,
          state_est          TEXT NOT NULL,
          confidence         INTEGER NOT NULL,
          confidence_label   TEXT NOT NULL,
          current_server_id  TEXT,
          epoch_id           INTEGER,
          epoch_started_at   INTEGER,
          since_change_sec   INTEGER,
          baseline_server_id TEXT,
          short_lobby_before INTEGER NOT NULL DEFAULT 0,
          long_match_flag    INTEGER NOT NULL DEFAULT 0,
          sticky_active      INTEGER NOT NULL DEFAULT 0,
          reason             TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_lem1_chan_ts ON live_estimated_match_v1(channel_id, ts)")

    # --- migrations: nur konstante DEFAULTs / Backfill via UPDATE ---
    # live_lane_members
    newly_added = False
    newly_added |= _add_column_no_default("live_lane_members", "in_deadlock", "INTEGER")  # wir schreiben immer 0/1
    newly_added |= _add_column_no_default("live_lane_members", "server_id",   "TEXT")
    if _add_column_no_default("live_lane_members", "last_seen", "INTEGER"):
        # Backfill: jetzt-Zeit als UNIX (ohne DEFAULT in ALTER TABLE)
        db.execute("UPDATE live_lane_members SET last_seen = CAST(strftime('%s','now') AS INTEGER) WHERE last_seen IS NULL")

    # live_lane_state
    _add_column_no_default("live_lane_state", "is_active", "INTEGER")
    _add_column_no_default("live_lane_state", "reason",    "TEXT")

    # live_decision_metrics_v3 (nur falls alt)
    _add_column_no_default("live_decision_metrics_v3", "override_in_match_count", "INTEGER")
    _add_column_no_default("live_decision_metrics_v3", "cohort_imputable_count",  "INTEGER")
    _add_column_no_default("live_decision_metrics_v3", "stable_for_sec",          "INTEGER")

class LiveMatchMaster(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False
        self._lane_cache: Dict[int, Dict[str, Any]] = {}

    # ---------------- Lifecycle ----------------

    async def cog_load(self):
        db.connect()
        _ensure_schema()

        if not STEAM_API_KEY:
            log.warning("STEAM_API_KEY fehlt – Live-Scan läuft, aber ohne Steam-Daten ist die Erkennung eingeschränkt.")

        # Cold-Start-Resync (ohne Events)
        try:
            await self._run_once()
            await asyncio.sleep(2)
            await self._run_once()
            log.info("LiveMatchMaster Cold-Start-Resync abgeschlossen.")
        except Exception as e:
            log.warning("Cold-Start-Resync Fehler (ignoriere, Loop startet trotzdem): %r", e)

        if not self._started:
            self.scan_loop.start()
            self._started = True
            log.info("LiveMatchMaster gestartet (Tick=%ss).", CHECK_INTERVAL_SEC)

    async def cog_unload(self):
        if self._started:
            try:
                self.scan_loop.cancel()
            except Exception:
                log.debug("scan_loop cancel beim Unload ignoriert")
            self._started = False

    # ---------------- Hauptschleife (15s) ----------------

    @tasks.loop(seconds=CHECK_INTERVAL_SEC)
    async def scan_loop(self):
        await self.bot.wait_until_ready()
        await self._run_once()

    # ---------------- Steam Helpers ----------------

    @staticmethod
    def _in_deadlock(summary: dict) -> bool:
        gid = str(summary.get("gameid", "") or "")
        gex = str(summary.get("gameextrainfo", "") or "")
        return gid == DEADLOCK_APP_ID or gex.lower() == "deadlock"

    @staticmethod
    def _server_id(summary: dict) -> Optional[str]:
        sid = summary.get("gameserversteamid")
        return str(sid) if sid else None

    @staticmethod
    def _presence_server_id(pres: dict) -> Optional[str]:
        raw = pres.get("raw") or {}
        group = pres.get("player_group") or raw.get("steam_player_group")
        if group:
            return str(group)
        connect = pres.get("connect") or raw.get("connect")
        if isinstance(connect, str) and "joinlobby" in connect:
            parts = connect.split("/")
            if len(parts) >= 5:
                return parts[4]
        return None

    @staticmethod
    def _presence_phase_hint(pres: dict) -> Optional[str]:
        raw = pres.get("raw") or {}
        connect = pres.get("connect") or raw.get("connect")
        if isinstance(connect, str) and connect:
            return PHASE_MATCH

        group_size_val = pres.get("player_group_size") or raw.get("steam_player_group_size")
        try:
            group_size = int(group_size_val)
        except (TypeError, ValueError):
            group_size = 0
        group_id = pres.get("player_group") or raw.get("steam_player_group")
        if group_id and group_size > 0:
            return PHASE_LOBBY

        texts: List[str] = []
        for key in ("status", "display"):
            val = pres.get(key)
            if val:
                texts.append(str(val))
        for key in ("status", "steam_display"):
            val = raw.get(key)
            if val:
                texts.append(str(val))
        blob = " ".join(texts).lower()

        if any(term in blob for term in _RP_MATCH_TERMS):
            return PHASE_MATCH
        if any(term in blob for term in _RP_LOBBY_TERMS):
            return PHASE_LOBBY
        if any(term in blob for term in _RP_GAME_TERMS):
            return PHASE_GAME
        return None

    @staticmethod
    def _presence_in_deadlock(pres: dict) -> bool:
        app_id = str(pres.get("app_id") or "")
        if app_id and app_id == str(DEADLOCK_APP_ID):
            return True
        raw = pres.get("raw") or {}
        texts = [
            str(pres.get("status") or ""),
            str(pres.get("display") or ""),
            str(raw.get("steam_display") or ""),
            str(raw.get("status") or ""),
        ]
        blob = " ".join(t for t in texts if t).lower()
        return "deadlock" in blob

    def _select_presence(self, steam_ids: List[str], presence_map: Dict[str, dict], now: int) -> Optional[dict]:
        best: Optional[dict] = None
        best_ts = -1
        for sid in steam_ids:
            pres = presence_map.get(str(sid))
            if not pres:
                continue
            try:
                ts = int(pres.get("last_update") or 0)
            except (TypeError, ValueError):
                continue
            if ts <= 0 or (now - ts) > RICH_PRESENCE_STALE_SEC:
                continue
            if ts > best_ts:
                best = pres
                best_ts = ts
        return best

    async def _steam_summaries(self, session: aiohttp.ClientSession, steam_ids: List[str]) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        if not steam_ids or not STEAM_API_KEY:
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
                log.warning("Steam GetPlayerSummaries Fehler: %s", e)
        return out

    # ---------------- Kernlogik ----------------

    def _collect_voice_channels(self) -> List[discord.VoiceChannel]:
        lanes: List[discord.VoiceChannel] = []
        if LIVE_CATEGORIES:
            for g in self.bot.guilds:
                for cat_id in LIVE_CATEGORIES:
                    ch = g.get_channel(cat_id)
                    if isinstance(ch, discord.CategoryChannel):
                        lanes.extend(ch.voice_channels)
        else:
            for g in self.bot.guilds:
                for ch in g.channels:
                    if isinstance(ch, discord.VoiceChannel):
                        lanes.append(ch)
            if lanes:
                log.warning(
                    "LIVE_CATEGORIES ist leer – fallback: scanne %d Voice-Channels (alle Guilds). "
                    "Setze LIVE_CATEGORIES für präzises Scoping.",
                    len(lanes)
                )
        # dedup
        out: Dict[int, discord.VoiceChannel] = {}
        for ch in lanes:
            if isinstance(ch, discord.VoiceChannel):
                out[ch.id] = ch
        return list(out.values())

    async def _run_once(self):
        lanes = self._collect_voice_channels()

        members = [m for ch in lanes for m in ch.members if not m.bot]
        user_ids = sorted({m.id for m in members})

        links: Dict[int, List[str]] = defaultdict(list)
        if user_ids:
            qs = ",".join("?" for _ in user_ids)
            for r in db.query_all(f"SELECT user_id, steam_id FROM steam_links WHERE user_id IN ({qs})", tuple(user_ids)):
                links[int(r["user_id"])] = links.get(int(r["user_id"]), []) + [str(r["steam_id"])]

        overrides: Dict[int, int] = {}
        if user_ids:
            qs = ",".join("?" for _ in user_ids)
            try:
                for r in db.query_all(f"SELECT user_id, force_in_match FROM live_match_overrides WHERE user_id IN ({qs})", tuple(user_ids)):
                    overrides[int(r["user_id"])] = int(r["force_in_match"] or 0)
            except Exception:
                overrides = {}

        all_steam = sorted({sid for arr in links.values() for sid in arr})
        summaries: Dict[str, dict] = {}
        async with aiohttp.ClientSession() as session:
            summaries = await self._steam_summaries(session, all_steam)

        presence_map: Dict[str, dict] = {}
        if ENABLE_RICH_PRESENCE and steam_service_module:
            try:
                presence_map = steam_service_module.load_rich_presence(all_steam)
            except Exception as e:
                log.debug("rich_presence/load_failed: %r", e)
                presence_map = {}

        now = int(time.time())

        for ch in lanes:
            nonbots = [m for m in ch.members if not m.bot]
            voice_count = len(nonbots)
            y_cap = min(voice_count, MAX_MATCH_CAP)
            is_active = 1 if voice_count > 0 else 0

            if voice_count == 0:
                self._write_lane_state(ch.id, active=0, suffix=None, ts=now, reason="phase=OFF;capY=0;empty")
                self._clear_lane_members(ch.id)
                self._lane_cache.pop(ch.id, None)
                self._write_decision_metrics(now, ch.id, PHASE_OFF, 0, y_cap,
                                             majority_server_id=None, majority_n=0,
                                             ig_count=0, dl_count=0, override_in_match_count=0,
                                             cohort_imputable_count=0, stable_for_sec=0,
                                             reason="empty")
                continue

            ig_with_server: List[Tuple[int, str]] = []
            deadlockers: List[int] = []
            override_in_match_count = 0
            lane_member_rows: List[Tuple[int, int, int, Optional[str], int]] = []
            cohort_imputable_count = 0
            presence_counts_total: Counter[str] = Counter()
            presence_total = 0
            presence_age_samples: List[int] = []

            for m in nonbots:
                found_server: Optional[str] = None
                in_dl = False
                presence_info = self._select_presence(links.get(m.id, []), presence_map, now) if presence_map else None
                if presence_info:
                    presence_total += 1
                    try:
                        ts_val = int(presence_info.get("last_update") or 0)
                    except (TypeError, ValueError):
                        ts_val = 0
                    if ts_val:
                        presence_age_samples.append(max(0, now - ts_val))
                    if self._presence_in_deadlock(presence_info):
                        in_dl = True
                    hint = self._presence_phase_hint(presence_info)
                    if hint:
                        presence_counts_total[hint] += 1
                    presence_server = self._presence_server_id(presence_info)
                    if presence_server:
                        found_server = presence_server

                for sid in links.get(m.id, []):
                    s = summaries.get(sid)
                    if not s:
                        continue
                    if self._in_deadlock(s):
                        in_dl = True
                        sid_server = self._server_id(s)
                        if sid_server:
                            found_server = sid_server
                if overrides.get(m.id, 0):
                    override_in_match_count += 1
                if in_dl:
                    deadlockers.append(m.id)
                    if found_server:
                        ig_with_server.append((m.id, found_server))
                        lane_member_rows.append((now, ch.id, m.id, found_server, 1))
                    else:
                        lane_member_rows.append((now, ch.id, m.id, None, 1))
                else:
                    lane_member_rows.append((now, ch.id, m.id, None, 0))

            self._upsert_lane_members(lane_member_rows)

            majority_id: Optional[str] = None
            majority_n = 0
            if ig_with_server:
                cnt = Counter([sid for _, sid in ig_with_server])
                majority_id, majority_n = cnt.most_common(1)[0]

            ig_count = len(ig_with_server)
            dl_count = len(deadlockers)

            presence_match_n = presence_counts_total.get(PHASE_MATCH, 0)
            presence_lobby_n = presence_counts_total.get(PHASE_LOBBY, 0)
            presence_game_n = presence_counts_total.get(PHASE_GAME, 0)
            cohort_imputable_count = presence_match_n

            prev = self._lane_cache.get(ch.id, {})
            prev_phase = prev.get("phase", PHASE_OFF)
            prev_match_server = prev.get("server_id")
            last_majority_id = prev.get("last_majority_id")
            server_since = int(prev.get("server_since") or now)
            server_changed_at = int(prev.get("server_changed_at") or 0)
            baseline_server = prev.get("baseline_server_id")
            group_ready_since = prev.get("group_ready_since")
            last_seen = int(prev.get("last_seen") or now)

            if majority_id and majority_id != last_majority_id:
                last_majority_id = majority_id
                server_since = now
                server_changed_at = now
                if baseline_server is None:
                    baseline_server = majority_id

            if majority_n >= max(1, MIN_MATCH_GROUP):
                if not group_ready_since:
                    group_ready_since = now
            else:
                group_ready_since = None

            phase = PHASE_LOBBY
            server_for_phase: Optional[str] = None
            suffix: Optional[str] = None

            if dl_count == 0:
                if voice_count > 0:
                    phase = PHASE_GAME
                    suffix = _fmt_suffix(dl_count, voice_count, AUTO_SUFFIX_GAME or "Im Spiel")
                else:
                    phase = PHASE_OFF
                    suffix = None
                baseline_server = None
            else:
                if prev_phase == PHASE_MATCH and prev_match_server and majority_id != prev_match_server:
                    phase = PHASE_GAME if ig_count > 0 else PHASE_LOBBY
                    server_for_phase = None
                    if majority_id:
                        baseline_server = majority_id
                        server_since = now
                        server_changed_at = now
                else:
                    if ig_count > 0:
                        phase = PHASE_GAME
                        server_changed_vs_baseline = (
                            majority_id is not None and baseline_server is not None and majority_id != baseline_server
                        )
                        if server_changed_vs_baseline and majority_n >= max(1, MIN_MATCH_GROUP):
                            stable_ok = (now - server_since) >= REQUIRE_STABILITY_SEC
                            join_grace_ok = (group_ready_since is not None) and ((now - int(group_ready_since)) >= LOBBY_GRACE_SEC)
                            if stable_ok and join_grace_ok:
                                phase = PHASE_MATCH
                                server_for_phase = majority_id
                    else:
                        phase = PHASE_LOBBY

            if dl_count > 0 and presence_match_n >= max(1, MIN_MATCH_GROUP):
                if phase != PHASE_MATCH:
                    phase = PHASE_MATCH
                if not server_for_phase:
                    server_for_phase = majority_id or (ig_with_server[0][1] if ig_with_server else None)
            elif phase == PHASE_GAME and presence_lobby_n >= max(1, MIN_MATCH_GROUP) and presence_lobby_n > presence_match_n:
                phase = PHASE_LOBBY
            elif phase == PHASE_LOBBY and presence_game_n >= max(1, MIN_MATCH_GROUP) and presence_game_n > presence_lobby_n:
                phase = PHASE_GAME

            if suffix is None:
                if phase == PHASE_MATCH:
                    suffix = _fmt_suffix(dl_count, voice_count, AUTO_SUFFIX_MATCH or "Im Match")
                elif phase == PHASE_GAME:
                    suffix = _fmt_suffix(dl_count, voice_count, AUTO_SUFFIX_GAME or "Im Spiel")
                elif phase == PHASE_LOBBY:
                    suffix = _fmt_suffix(dl_count, voice_count, AUTO_SUFFIX_LOBBY or "In der Lobby")

            join_left = -1
            if group_ready_since:
                need = LOBBY_GRACE_SEC - (now - int(group_ready_since))
                join_left = int(need if need > 0 else 0)
            srvchg = 1 if (server_changed_at and (now - server_changed_at) <= CHECK_INTERVAL_SEC) else 0

            bits = [
                f"phase={phase}",
                f"voice={voice_count}",
                f"capY={y_cap}",
                f"nMaj={majority_n}",
                f"nIG={ig_count}",
                f"nDL={dl_count}",
                f"stable={max(0, now - server_since)}",
                f"on_srv={majority_id or '-'}",
                f"srvchg={srvchg}",
                f"baseline={baseline_server or '-'}",
            ]
            if server_for_phase or majority_id:
                bits.insert(1, f"srv={server_for_phase or majority_id}")
            if group_ready_since:
                bits.append(f"join_grace_left={join_left}")
            bits.append(f"rp_tot={presence_total}")
            bits.append(f"rp_match={presence_match_n}")
            bits.append(f"rp_game={presence_game_n}")
            bits.append(f"rp_lobby={presence_lobby_n}")
            if presence_age_samples:
                bits.append(f"rp_age_min={min(presence_age_samples)}")
                bits.append(f"rp_age_max={max(presence_age_samples)}")

            reason = ";".join(bits)

            self._write_lane_state(ch.id, active=is_active, suffix=suffix, ts=now, reason=reason)

            try:
                est_conf, est_label, epoch_id, epoch_started_at, sticky_active, rextra = self._estimate_confidence(
                    ch.id, now, phase, majority_id, baseline_server, server_since, server_changed_at,
                    prev, majority_n, dl_count
                )
                est_state = "MATCH" if (phase == PHASE_MATCH or est_conf >= 75) else ("GAME" if ig_count > 0 else "LOBBY")
                self._write_estimated(
                    now, ch.id, est_state, est_conf, est_label, majority_id, epoch_id, epoch_started_at,
                    max(0, now - server_since), baseline_server,
                    short_lobby_before=1 if (phase != PHASE_MATCH and (now - server_since) <= 300) else 0,
                    long_match_flag=1 if (phase == PHASE_MATCH and (now - server_since) >= 1200) else 0,
                    sticky_active=1 if sticky_active else 0,
                    reason=reason + ";" + rextra
                )
                if est_state == "MATCH" and est_conf >= 80:
                    suffix2 = _fmt_suffix(dl_count, voice_count, AUTO_SUFFIX_MATCH or "Im Match")
                else:
                    suffix2 = _fmt_suffix(
                        dl_count, voice_count,
                        (AUTO_SUFFIX_GAME or "Im Spiel") if ig_count > 0 else (AUTO_SUFFIX_LOBBY or "In der Lobby")
                    )
                self._write_lane_state(ch.id, active=is_active, suffix=suffix2, ts=now, reason=reason+";est")
            except Exception as e:
                log.debug("estimate/error: %r", e)

            self._lane_cache[ch.id] = {
                "phase": phase,
                "server_id": server_for_phase if phase == PHASE_MATCH else None,
                "baseline_server_id": baseline_server,
                "last_majority_id": last_majority_id,
                "epoch_id": int(self._lane_cache.get(ch.id, {}).get("epoch_id") or 0),
                "epoch_started_at": int(self._lane_cache.get(ch.id, {}).get("epoch_started_at") or server_since),
                "server_since": server_since,
                "server_changed_at": server_changed_at,
                "group_ready_since": group_ready_since,
                "last_seen": now,
            }

            self._write_decision_metrics(
                now, ch.id, phase, voice_count, y_cap,
                majority_server_id=majority_id, majority_n=majority_n,
                ig_count=ig_count, dl_count=dl_count,
                override_in_match_count=override_in_match_count,
                cohort_imputable_count=cohort_imputable_count,
                stable_for_sec=max(0, now - server_since),
                reason=reason
            )

    # ---------- Confidence Estimator ----------
    def _estimate_confidence(self, ch_id: int, now: int, phase: str, majority_id: Optional[str],
                             baseline_server_id: Optional[str], server_since: int,
                             last_server_change_at: int, prev: dict,
                             majority_n: int, dl_count: int) -> tuple[int, str, int, int, bool, str]:
        epoch_id = int(prev.get("epoch_id") or 0)
        epoch_started_at = int(prev.get("epoch_started_at") or server_since)
        last_majority_id = prev.get("last_majority_id")
        if majority_id and majority_id != last_majority_id:
            epoch_id += 1
            epoch_started_at = now

        dur = max(0, now - server_since)
        score = 0
        label = "low"

        changed_vs_baseline = (majority_id and baseline_server_id and majority_id != baseline_server_id)
        if phase == PHASE_MATCH or changed_vs_baseline:
            if dur < 300:
                score = 20 + int(dur * 30 / 300)
            elif dur < 1200:
                score = 50 + int((dur - 300) * 30 / 900)
            elif dur < 2400:
                score = 80 + int((dur - 1200) * 15 / 1200)
            else:
                score = 96
        else:
            score = 10 + min(20, dur // 60)

        if majority_n >= 3:
            score += 5
        if dl_count >= 3:
            score += 3

        score = _clamp(score, 0, 100)
        if score >= 90: label = "very_high"
        elif score >= 75: label = "high"
        elif score >= 60: label = "medium"
        else: label = "low"

        sticky_active = score >= 75
        r = f"dur={dur}s;majN={majority_n};dlN={dl_count};epoch={epoch_id}"
        return score, label, epoch_id, epoch_started_at, sticky_active, r

    # ---- Persist Helpers -----------------------------------------------------

    def _write_lane_state(self, channel_id: int, active: int, suffix: Optional[str], ts: int, reason: str) -> None:
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
            (int(channel_id), int(active), int(ts), suffix, reason)
        )

    def _clear_lane_members(self, channel_id: int) -> None:
        db.execute("DELETE FROM live_lane_members WHERE channel_id=?", (int(channel_id),))

    def _upsert_lane_members(self, rows: List[Tuple[int, int, int, Optional[str], int]]) -> None:
        for ts, ch_id, user_id, sid, in_dl in rows:
            db.execute(
                """
                INSERT INTO live_lane_members(channel_id, user_id, in_deadlock, server_id, last_seen)
                VALUES(?,?,?,?,?)
                ON CONFLICT(channel_id, user_id) DO UPDATE SET
                  in_deadlock=excluded.in_deadlock,
                  server_id=excluded.server_id,
                  last_seen=excluded.last_seen
                """,
                (int(ch_id), int(user_id), int(in_dl), sid, int(ts))
            )

    def _write_decision_metrics(
        self,
        ts: int,
        channel_id: int,
        phase: str,
        voice_n: int,
        y_cap: int,
        majority_server_id: Optional[str],
        majority_n: int,
        ig_count: int,
        dl_count: int,
        override_in_match_count: int,
        cohort_imputable_count: int,
        stable_for_sec: int,
        reason: str,
    ) -> None:
        db.execute(
            """
            INSERT INTO live_decision_metrics_v3(
              ts, channel_id, voice_n, y_cap, phase, majority_server_id, majority_n,
              ig_count, dl_count, override_in_match_count, cohort_imputable_count,
              stable_for_sec, reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(ts), int(channel_id), int(voice_n), int(y_cap), phase,
                majority_server_id, int(majority_n), int(ig_count), int(dl_count),
                int(override_in_match_count), int(cohort_imputable_count), int(stable_for_sec), reason
            )
        )

    def _write_member_metric(
        self,
        ts: int,
        channel_id: int,
        user_id: int,
        in_deadlock: int,
        server_id: Optional[str],
        in_match_db: int,
        source: str,
    ) -> None:
        db.execute(
            """
            INSERT INTO live_member_metrics_v3(
              ts, channel_id, user_id, in_deadlock, server_id, in_match_db, source
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (int(ts), int(channel_id), int(user_id), int(in_deadlock), server_id, int(in_match_db), source)
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchMaster(bot))
