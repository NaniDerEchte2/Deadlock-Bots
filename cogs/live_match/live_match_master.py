# cogs/live_match/live_match_master.py
# ------------------------------------------------------------
# LiveMatchMaster v3 – präzise Match-Status-Erkennung + Telemetrie
#
# Kerngedanke:
#   • Gold-Signal: Steam "gameserversteamid" ⇒ IN_MATCH
#   • Ohne server_id aber Deadlock offen ⇒ IM_SPIEL (Lobby/Menu)
#   • Hysterese/Grace: verhindert Flackern und kurze Disconnect-FPs
#   • Clamping: n = min(voice_count, MAX_MATCH_CAP)
#   • Upsert in live_lane_state + live_lane_members (mit checked_ts/TTL)
#   • Telemetrie in:
#       - live_decision_metrics_v3 (Lane/Tick)
#       - live_member_metrics_v3   (Member/Tick)
#
# Erwartete DB-Strukturen (werden bei Bedarf automatisch erstellt):
#   - steam_links(user_id, steam_id, ...)
#   - live_match_overrides(user_id PRIMARY KEY, force_in_match INT, note TEXT)
#   - live_lane_members(channel_id, user_id, in_match, server_id, checked_ts, PK(channel_id,user_id))
#   - live_lane_state(channel_id PRIMARY KEY, is_active, last_update, suffix, reason)
#   - live_decision_metrics_v3(...)
#   - live_member_metrics_v3(...)
#
# Abhängigkeiten:
#   - discord.py (commands, tasks)
#   - aiohttp
#   - service.db (Wrapper mit execute/query_all/executemany)
#
# Hinweise:
#   - Keine "empty except" – Fehler werden geloggt.
#   - ENV-Variablen s. Abschnitt "Konfiguration".
#   - Cohort-Imputation wird NUR als Telemetrie gezählt, nicht für Anzeige.
# ------------------------------------------------------------

import os
import time
import logging
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple, Any

import aiohttp
import discord
from discord.ext import commands, tasks

from service import db  # erwartet: execute(...), query_all(...), executemany(...)

log = logging.getLogger("LiveMatchMaster")

# ===== Konfiguration über ENV =====
LIVE_CATEGORIES: List[int] = [
    int(x) for x in os.getenv("LIVE_MATCH_CATEGORY_IDS", "1289721245281292290").split(",") if x.strip()
]

DEADLOCK_APP_ID = os.getenv("DEADLOCK_APP_ID", "1422450")
STEAM_API_KEY   = os.getenv("STEAM_API_KEY", "").strip()

CHECK_INTERVAL_SEC    = int(os.getenv("LIVE_CHECK_INTERVAL_SEC", "30"))
MIN_MATCH_GROUP       = int(os.getenv("MIN_MATCH_GROUP", "2"))
MAX_MATCH_CAP         = int(os.getenv("MAX_MATCH_CAP", "6"))

REQUIRE_STABILITY_SEC = int(os.getenv("REQUIRE_STABILITY_SEC", "30"))   # Stabilität bis MATCH
LOBBY_GRACE_SEC       = int(os.getenv("LOBBY_GRACE_SEC", "90"))         # Drop-Grace für Rückfall
STATUS_TTL_SEC        = int(os.getenv("STATUS_TTL_SEC", "120"))         # Konsumenten-Filter

# Overrides
AUTO_VOICE_OVERRIDE_DEFAULT = int(os.getenv("AUTO_VOICE_OVERRIDE_DEFAULT", "0"))  # 1 = treat voice as in_match

# Cohort-Imputation (nur Telemetrie)
COHORT_COUNT_IN_DISPLAY  = int(os.getenv("COHORT_COUNT_IN_DISPLAY", "0"))         # NICHT für Anzeige verwendet
COHORT_MIN_STABILITY_SEC = int(os.getenv("COHORT_MIN_STABILITY_SEC", "30"))

PHASE_OFF   = "OFF"
PHASE_LOBBY = "LOBBY"
PHASE_GAME  = "GAME"
PHASE_MATCH = "MATCH"


# ===== Schema / Indizes sicherstellen =====
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

    # overrides
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_match_overrides(
          user_id        INTEGER PRIMARY KEY,
          force_in_match INTEGER NOT NULL DEFAULT 0,
          note           TEXT
        )
    """)

    # live_lane_members – Upsert-Ziel für Anzeige
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_lane_members(
          channel_id  INTEGER NOT NULL,
          user_id     INTEGER NOT NULL,
          in_match    INTEGER NOT NULL DEFAULT 0,
          server_id   TEXT,
          checked_ts  INTEGER NOT NULL,
          PRIMARY KEY (channel_id, user_id)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_llm_checked ON live_lane_members(checked_ts)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_llm_channel ON live_lane_members(channel_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_llm_server  ON live_lane_members(server_id)")

    # live_lane_state – Suffix/Reason/HUD
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_lane_state(
          channel_id  INTEGER PRIMARY KEY,
          is_active   INTEGER NOT NULL DEFAULT 0,
          last_update INTEGER NOT NULL,
          suffix      TEXT,
          reason      TEXT
        )
    """)

    # Telemetrie v3: Lane/Tick
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
          override_in_match_count  INTEGER NOT NULL,
          cohort_imputable_count   INTEGER NOT NULL,
          stable_for_sec           INTEGER NOT NULL,
          reason                   TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_ldm3_ts          ON live_decision_metrics_v3(ts)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_ldm3_channel_ts ON live_decision_metrics_v3(channel_id, ts)")

    # Telemetrie v3: Member/Tick
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
    db.execute("CREATE INDEX IF NOT EXISTS idx_lmm3_user_ts    ON live_member_metrics_v3(user_id, ts)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_lmm3_channel_ts ON live_member_metrics_v3(channel_id, ts)")


class LiveMatchMaster(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False
        # Cache: pro Channel Stabilitäts-Tracking
        # keys: "phase", "server_id", "since", "stable_since", "last_seen"
        self._lane_cache: Dict[int, Dict[str, Any]] = {}

    async def cog_load(self):
        db.connect()
        _ensure_schema()
        if not STEAM_API_KEY:
            log.warning("STEAM_API_KEY fehlt – Live-Scan wird trotzdem gestartet, aber ohne Steam-Daten ist das Ergebnis limitiert.")
        if not self._started:
            self.scan_loop.start()
            self._started = True
        log.info(
            "LiveMatchMaster v3 bereit (Categories=%s, Interval=%ss, MinGroup=%d, MaxCap=%d, TTL=%ss, AutoVoiceOverride=%d)",
            LIVE_CATEGORIES, CHECK_INTERVAL_SEC, MIN_MATCH_GROUP, MAX_MATCH_CAP, STATUS_TTL_SEC, AUTO_VOICE_OVERRIDE_DEFAULT
        )

    async def cog_unload(self):
        if self._started:
            try:
                self.scan_loop.cancel()
            except Exception as e:
                log.debug("scan_loop.cancel() beim Unload fehlgeschlagen: %r", e)
            finally:
                self._started = False

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

    # ========== Hauptschleife ==========
    @tasks.loop(seconds=CHECK_INTERVAL_SEC)
    async def scan_loop(self):
        await self.bot.wait_until_ready()
        await self._run_once()

    async def _run_once(self):
        # 1) Relevante Voice-Channels (nur aus konfigurierten Kategorien)
        lanes: List[discord.VoiceChannel] = []
        for g in self.bot.guilds:
            for cat_id in LIVE_CATEGORIES:
                cat = g.get_channel(cat_id)
                if isinstance(cat, discord.CategoryChannel):
                    lanes.extend(cat.voice_channels)

        # 2) Mitglieder (ohne Bots) + Links + Overrides
        members = [m for ch in lanes for m in ch.members if not m.bot]
        user_ids = sorted({m.id for m in members})

        links: Dict[int, List[str]] = defaultdict(list)
        if user_ids:
            qs = ",".join("?" for _ in user_ids)
            for r in db.query_all(f"SELECT user_id, steam_id FROM steam_links WHERE user_id IN ({qs})", tuple(user_ids)):
                links[int(r["user_id"])].append(str(r["steam_id"]))

        overrides: Dict[int, int] = {}
        if user_ids:
            qs = ",".join("?" for _ in user_ids)
            for r in db.query_all(f"SELECT user_id, force_in_match FROM live_match_overrides WHERE user_id IN ({qs})", tuple(user_ids)):
                overrides[int(r["user_id"])] = int(r["force_in_match"] or 0)

        # 3) Steam zusammengefasst abfragen
        all_steam = sorted({sid for arr in links.values() for sid in arr})
        summaries: Dict[str, dict] = {}
        async with aiohttp.ClientSession() as session:
            summaries = await self._steam_summaries(session, all_steam)

        now = int(time.time())

        # 4) Pro Lane auswerten
        for ch in lanes:
            nonbots = [m for m in ch.members if not m.bot]
            voice_count = len(nonbots)
            y_cap = min(MAX_MATCH_CAP, voice_count) if voice_count > 0 else 0

            if voice_count == 0:
                self._write_lane_state(ch.id, active=0, suffix=None, ts=now, reason="phase=OFF;capY=0;empty")
                self._clear_lane_members(ch.id)
                self._lane_cache.pop(ch.id, None)
                # Telemetrie
                self._write_decision_metrics(now, ch.id, PHASE_OFF, 0, 0,
                                             majority_server_id=None, majority_n=0,
                                             ig_count=0, dl_count=0, override_in_match_count=0,
                                             cohort_imputable_count=0, stable_for_sec=0,
                                             reason="empty")
                continue

            ig_with_server: List[Tuple[int, str]] = []  # (user_id, server_id)
            deadlockers: List[int] = []
            override_in_match_count = 0
            lane_member_rows: List[Tuple[int, int, int, Optional[str], int]] = []  # for DB upsert
            cohort_imputable_count = 0

            # pro User prüfen
            for m in nonbots:
                found_server: Optional[str] = None
                in_dl = False

                # Steam-Evidenz
                for sid in links.get(m.id, []):
                    s = summaries.get(sid)
                    if not s:
                        continue
                    if self._in_deadlock(s):
                        in_dl = True
                        sid_server = self._server_id(s)
                        if sid_server:
                            found_server = sid_server
                            break

                # Override/Default (optional) → zählt in Anzeige mit
                applied_override = False
                if not found_server:
                    force_flag = overrides.get(m.id, 0)
                    if force_flag == 1 or AUTO_VOICE_OVERRIDE_DEFAULT == 1:
                        in_dl = True
                        applied_override = True
                        override_in_match_count += 1

                # Member-Metrik & Upsert-Row
                source = "server" if found_server else ("override" if applied_override else "none")
                in_match_db = 1 if (found_server or applied_override) else 0
                lane_member_rows.append((ch.id, m.id, in_match_db, found_server, now))
                self._write_member_metrics(now, ch.id, m.id, 1 if in_dl else 0, found_server, in_match_db, source)

                if in_dl:
                    deadlockers.append(m.id)
                if found_server:
                    ig_with_server.append((m.id, found_server))

            # Upsert aktueller Zustand
            self._upsert_lane_members(lane_member_rows)

            # Mehrheit-Server bestimmen
            majority_id: Optional[str] = None
            majority_n = 0
            if ig_with_server:
                cnt = Counter([sid for _, sid in ig_with_server])
                majority_id, majority_n = cnt.most_common(1)[0]

            ig_count = len(ig_with_server)
            dl_count = len(deadlockers)

            # Cohort-Imputation (nur gezählt, nicht benutzt)
            if majority_id:
                users_with_server = {u for u, _ in ig_with_server}
                cohort_imputable_count = sum(1 for uid in deadlockers if uid not in users_with_server)

            # Hysterese/Grace/Phase
            prev = self._lane_cache.get(ch.id, {"phase": PHASE_OFF, "server_id": None, "since": None, "stable_since": None, "last_seen": None})
            phase = PHASE_OFF
            server_for_phase: Optional[str] = None
            since = int(prev.get("since") or now)
            stable_since = int(prev.get("stable_since") or now)

            if majority_id and majority_n >= max(1, MIN_MATCH_GROUP):
                # Server-Mehrheit vorhanden
                if prev.get("server_id") == majority_id and prev.get("phase") in (PHASE_MATCH, PHASE_GAME, PHASE_LOBBY):
                    # Stabil genug?
                    if (now - int(prev.get("stable_since") or now)) >= REQUIRE_STABILITY_SEC:
                        phase = PHASE_MATCH
                    else:
                        phase = PHASE_GAME
                else:
                    # neue Mehrheit oder neuer Server
                    stable_since = now
                    since = now
                    phase = PHASE_GAME

                server_for_phase = majority_id
                if phase == PHASE_GAME and (now - stable_since) >= REQUIRE_STABILITY_SEC:
                    phase = PHASE_MATCH

            elif ig_count > 0:
                # Es gibt server_id für einige, aber ohne Mehrheitsschwelle (oder schwankend)
                if prev.get("phase") == PHASE_MATCH and (now - int(prev.get("last_seen") or now)) <= LOBBY_GRACE_SEC:
                    phase = PHASE_MATCH
                    server_for_phase = prev.get("server_id")
                else:
                    phase = PHASE_GAME
                    server_for_phase = None
                    since = prev.get("since") or now

            elif dl_count > 0:
                # Deadlock offen, aber keine server_id
                if prev.get("phase") == PHASE_MATCH and (now - int(prev.get("last_seen") or now)) <= LOBBY_GRACE_SEC:
                    phase = PHASE_MATCH
                    server_for_phase = prev.get("server_id")
                else:
                    phase = PHASE_LOBBY
                    server_for_phase = None
                    since = prev.get("since") or now

            else:
                phase = PHASE_OFF
                server_for_phase = None
                since = now

            # UI-Suffix & Aktiv-Flag
            suffix: Optional[str] = None
            is_active = 0
            if phase == PHASE_MATCH:
                n_raw = (majority_n or 0) + override_in_match_count
                n = min(n_raw, y_cap)
                suffix = f"• {n}/{y_cap} Im Match"
                is_active = 1
            elif phase == PHASE_GAME:
                n_raw = ig_count + override_in_match_count
                # Cohort-"What-if" NUR für Telemetrie/Debug – NICHT zur Anzeige addieren
                if COHORT_COUNT_IN_DISPLAY and (now - stable_since) >= COHORT_MIN_STABILITY_SEC and majority_id:
                    n_raw += cohort_imputable_count
                n = min(n_raw, y_cap)
                suffix = f"• {n}/{y_cap} Im Spiel"
            elif phase == PHASE_LOBBY:
                n_raw = dl_count
                n = min(n_raw, y_cap)
                suffix = f"• {n}/{y_cap} Lobby/Queue"
            else:
                suffix = None

            # Reason/Debug-String
            stable_for_sec = now - int(stable_since or now)
            bits = [f"phase={phase}", f"capY={y_cap}", f"nMaj={majority_n}", f"nIG={ig_count}", f"nDL={dl_count}", f"nOVR={override_in_match_count}", f"stable={stable_for_sec}"]
            if server_for_phase:
                bits.insert(1, f"srv={server_for_phase}")
            reason = ";".join(bits)

            # Persistieren
            self._write_lane_state(ch.id, active=is_active, suffix=suffix, ts=now, reason=reason)

            # Cache aktualisieren
            self._lane_cache[ch.id] = {
                "phase": phase,
                "server_id": server_for_phase,
                "since": since,
                "stable_since": stable_since,
                "last_seen": now,
            }

            # Telemetrie
            self._write_decision_metrics(now, ch.id, phase, voice_count, y_cap,
                                         server_for_phase, majority_n,
                                         ig_count, dl_count, override_in_match_count,
                                         cohort_imputable_count, stable_for_sec, reason)

    # ========== DB-Helfer ==========
    def _write_lane_state(self, channel_id: int, *, active: int, suffix: Optional[str], ts: int, reason: str) -> None:
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

    def _clear_lane_members(self, channel_id: int) -> None:
        db.execute("DELETE FROM live_lane_members WHERE channel_id=?", (int(channel_id),))

    def _upsert_lane_members(self, rows: List[Tuple[int, int, int, Optional[str], int]]) -> None:
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

    def _write_member_metrics(
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
