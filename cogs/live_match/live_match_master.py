# cogs/live_match/live_match_master.py
# ------------------------------------------------------------
# LiveMatchMaster v3 – präzise Match-Status-Erkennung + Telemetrie
#
# NEU/Änderungen (gemäß Vorgabe):
#   • MATCH nur nach Mehrheits-Serverwechsel (server_id change).
#   • Join-Grace (GROUP_JOIN_GRACE_SEC) greift nur bei Eskalation nach Serverwechsel
#     und nur, wenn majority_n >= MIN_MATCH_GROUP.
#   • Keine MATCH-Eskalation durch "lange Zeit auf gleicher server_id" – nur loggen.
#   • De-Eskalation aus MATCH nur bei Serverwechsel oder wenn niemand mehr in Deadlock ist.
#   • Baseline-Server: erste Mehrheits-server_id; solange diese gleich bleibt -> kein MATCH.
#   • Umfangreicher Reason-String für Diagnose.
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

REQUIRE_STABILITY_SEC = int(os.getenv("REQUIRE_STABILITY_SEC", "30"))    # Stabilität der NEUEN Mehrheit bis MATCH
LOBBY_GRACE_SEC       = int(os.getenv("LOBBY_GRACE_SEC", "90"))          # wird nur noch bei OFF/kein Deadlock verwendet
STATUS_TTL_SEC        = int(os.getenv("STATUS_TTL_SEC", "120"))          # Konsumenten-Filter (unverändert)

# Overrides (optional)
AUTO_VOICE_OVERRIDE_DEFAULT = int(os.getenv("AUTO_VOICE_OVERRIDE_DEFAULT", "0"))

# Cohort-Imputation (nur Telemetrie)
COHORT_COUNT_IN_DISPLAY  = int(os.getenv("COHORT_COUNT_IN_DISPLAY", "0"))
COHORT_MIN_STABILITY_SEC = int(os.getenv("COHORT_MIN_STABILITY_SEC", "30"))

# ==== NEU (für Telemetrie/Graces; NICHT zur Entscheidungs-Eskalation außer Join-Grace) ====
GROUP_JOIN_GRACE_SEC = int(os.getenv("GROUP_JOIN_GRACE_SEC", "90"))      # Wartezeit nach 1->2 (und allgemein bis Gruppe „ready“)
MATCH_CERTAINTY_SEC  = int(os.getenv("MATCH_CERTAINTY_SEC", "1200"))     # nur Logging
HUB_MIN_LIFETIME_SEC = int(os.getenv("HUB_MIN_LIFETIME_SEC", "21600"))   # nur Logging
HUB_MIN_USERS        = int(os.getenv("HUB_MIN_USERS", "20"))             # nur Logging
HUB_MIN_LANES        = int(os.getenv("HUB_MIN_LANES", "5"))              # nur Logging

PHASE_OFF   = "OFF"
PHASE_LOBBY = "LOBBY"
PHASE_GAME  = "GAME"
PHASE_MATCH = "MATCH"

# ===== Schema / Indizes sichern =====
def _ensure_schema() -> None:
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

    db.execute("""
        CREATE TABLE IF NOT EXISTS live_match_overrides(
          user_id        INTEGER PRIMARY KEY,
          force_in_match INTEGER NOT NULL DEFAULT 0,
          note           TEXT
        )
    """)

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

    db.execute("""
        CREATE TABLE IF NOT EXISTS live_lane_state(
          channel_id  INTEGER PRIMARY KEY,
          is_active   INTEGER NOT NULL DEFAULT 0,
          last_update INTEGER NOT NULL,
          suffix      TEXT,
          reason      TEXT
        )
    """)

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
        # Cache pro Lane:
        #  phase, server_id(MATCH), baseline_server_id, last_majority_id,
        #  server_since (seit wann aktuelle Mehrheit), server_changed_at,
        #  group_ready_since (ab wann majority_n >= MIN_MATCH_GROUP),
        #  last_seen
        self._lane_cache: Dict[int, Dict[str, Any]] = {}

    async def cog_load(self):
        db.connect()
        _ensure_schema()
        if not STEAM_API_KEY:
            log.warning("STEAM_API_KEY fehlt – Live-Scan läuft, aber ohne Steam-Daten ist die Erkennung eingeschränkt.")
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
                log.debug("scan_loop.cancel() failed: %r", e)
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
        # 1) Lanes einsammeln
        lanes: List[discord.VoiceChannel] = []
        for g in self.bot.guilds:
            for cat_id in LIVE_CATEGORIES:
                cat = g.get_channel(cat_id)
                if isinstance(cat, discord.CategoryChannel):
                    lanes.extend(cat.voice_channels)

        # 2) Mitglieder + steam_links + overrides
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

        # 3) Steam Summaries
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

            # leer -> OFF & Reset
            if voice_count == 0:
                self._write_lane_state(ch.id, active=0, suffix=None, ts=now, reason="phase=OFF;capY=0;empty")
                self._clear_lane_members(ch.id)
                self._lane_cache.pop(ch.id, None)
                self._write_decision_metrics(now, ch.id, PHASE_OFF, 0, 0,
                                             majority_server_id=None, majority_n=0,
                                             ig_count=0, dl_count=0, override_in_match_count=0,
                                             cohort_imputable_count=0, stable_for_sec=0,
                                             reason="empty")
                continue

            # pro User Steam prüfen
            ig_with_server: List[Tuple[int, str]] = []
            deadlockers: List[int] = []
            override_in_match_count = 0
            lane_member_rows: List[Tuple[int, int, int, Optional[str], int]] = []
            cohort_imputable_count = 0

            for m in nonbots:
                found_server: Optional[str] = None
                in_dl = False

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

                applied_override = False
                if not found_server:
                    force_flag = overrides.get(m.id, 0)
                    if force_flag == 1 or AUTO_VOICE_OVERRIDE_DEFAULT == 1:
                        in_dl = True
                        applied_override = True
                        override_in_match_count += 1

                source = "server" if found_server else ("override" if applied_override else "none")
                in_match_db = 1 if (found_server or applied_override) else 0
                lane_member_rows.append((ch.id, m.id, in_match_db, found_server, now))
                self._write_member_metrics(now, ch.id, m.id, 1 if in_dl else 0, found_server, in_match_db, source)

                if in_dl:
                    deadlockers.append(m.id)
                if found_server:
                    ig_with_server.append((m.id, found_server))

            self._upsert_lane_members(lane_member_rows)

            # Mehrheits-Server bestimmen
            majority_id: Optional[str] = None
            majority_n = 0
            if ig_with_server:
                cnt = Counter([sid for _, sid in ig_with_server])
                majority_id, majority_n = cnt.most_common(1)[0]

            ig_count = len(ig_with_server)
            dl_count = len(deadlockers)

            prev = self._lane_cache.get(ch.id, {})
            prev_phase = prev.get("phase", PHASE_OFF)
            prev_match_server = prev.get("server_id")  # nur gesetzt, wenn wir in MATCH waren
            last_majority_id = prev.get("last_majority_id")
            server_since = int(prev.get("server_since") or now)
            server_changed_at = int(prev.get("server_changed_at") or 0)
            baseline_server = prev.get("baseline_server_id")  # erste Mehrheits-ID
            group_ready_since = prev.get("group_ready_since")  # ab wann majority_n >= MIN_MATCH_GROUP
            last_seen = int(prev.get("last_seen") or now)

            # Majority Tracking & Baseline setzen/aktualisieren
            if majority_id and majority_id != last_majority_id:
                last_majority_id = majority_id
                server_since = now
                server_changed_at = now
                # Erstes Mal eine Mehrheit? -> Baseline initialisieren
                if baseline_server is None:
                    baseline_server = majority_id

            # Group-Ready tracking
            if majority_n >= max(1, MIN_MATCH_GROUP):
                if not group_ready_since:
                    group_ready_since = now
            else:
                group_ready_since = None  # zurücksetzen, wenn Gruppe kleiner wird

            # ======= Phasen-Entscheidung nach "NUR BEI SERVERWECHSEL" =======
            phase = PHASE_OFF
            server_for_phase: Optional[str] = None

            # 1) Falls niemand mehr Deadlock offen hat -> LOBBY/OFF gemäß Counts
            if dl_count == 0:
                phase = PHASE_OFF if voice_count == 0 else PHASE_LOBBY
                # Baseline zurücksetzen – nächster Einstieg beginnt sauber
                baseline_server = majority_id if majority_id else None

            else:
                # Es gibt Deadlock-Aktivität
                if prev_phase == PHASE_MATCH:
                    # In MATCH bleiben, solange derselbe Match-Server anliegt.
                    if majority_id and prev_match_server and majority_id == prev_match_server:
                        phase = PHASE_MATCH
                        server_for_phase = prev_match_server
                    else:
                        # Serverwechsel oder kein majority_id sichtbar -> aus MATCH raus (GAME/LOBBY)
                        # (kein Grace außer OFF/kein Deadlock, gemäß Vorgabe)
                        phase = PHASE_GAME if ig_count > 0 else PHASE_LOBBY
                        server_for_phase = None
                        # Baseline für neue Pre-Match-Phase setzen
                        if majority_id:
                            baseline_server = majority_id
                            server_since = now
                            server_changed_at = now
                else:
                    # Nicht in MATCH: solange Mehrheit == Baseline -> niemals MATCH.
                    if ig_count > 0:
                        phase = PHASE_GAME
                        # Prüfen, ob es einen Mehrheits-Serverwechsel gegenüber Baseline gab
                        server_changed_vs_baseline = (
                            majority_id is not None and baseline_server is not None and majority_id != baseline_server
                        )
                        # Eskalation auf MATCH nur, wenn:
                        #   • Serverwechsel ggü. Baseline
                        #   • majority_n >= MIN_MATCH_GROUP
                        #   • seit dem Wechsel stabil (REQUIRE_STABILITY_SEC)
                        #   • Join-Grace erfüllt (GROUP_JOIN_GRACE_SEC)
                        if server_changed_vs_baseline and majority_n >= max(1, MIN_MATCH_GROUP):
                            stable_ok = (now - server_since) >= REQUIRE_STABILITY_SEC
                            join_grace_ok = (group_ready_since is not None) and ((now - int(group_ready_since)) >= GROUP_JOIN_GRACE_SEC)
                            if stable_ok and join_grace_ok:
                                phase = PHASE_MATCH
                                server_for_phase = majority_id
                            else:
                                # (nur Info/Reason) – wir bleiben GAME bis beide Bedingungen erfüllt sind
                                pass
                    else:
                        phase = PHASE_LOBBY

            # UI-Suffix
            suffix: Optional[str] = None
            is_active = 0
            if phase == PHASE_MATCH:
                n_raw = (majority_n or 0) + override_in_match_count
                n = min(n_raw, y_cap)
                suffix = f"• {n}/{y_cap} Im Match"
                is_active = 1
            elif phase == PHASE_GAME:
                n_raw = ig_count + override_in_match_count
                if COHORT_COUNT_IN_DISPLAY and (now - server_since) >= COHORT_MIN_STABILITY_SEC and majority_id:
                    # nur hypothetisch, nicht standardmäßig aktiv
                    users_with_server = {u for u, _ in ig_with_server}
                    cohort_imputable_count = sum(1 for uid in deadlockers if uid not in users_with_server)
                    n_raw += cohort_imputable_count
                n = min(n_raw, y_cap)
                suffix = f"• {n}/{y_cap} Im Spiel"
            elif phase == PHASE_LOBBY:
                n_raw = len(deadlockers)
                n = min(n_raw, y_cap)
                suffix = f"• {n}/{y_cap} Lobby/Queue"
            else:
                suffix = None

            # Reason/Debug
            on_srv = now - server_since if server_since else 0
            join_left = -1
            if group_ready_since:
                need = GROUP_JOIN_GRACE_SEC - (now - int(group_ready_since))
                join_left = int(need if need > 0 else 0)
            srvchg = 1 if (server_changed_at and (now - server_changed_at) <= CHECK_INTERVAL_SEC) else 0

            bits = [
                f"phase={phase}",
                f"capY={y_cap}",
                f"nMaj={majority_n}",
                f"nIG={ig_count}",
                f"nDL={dl_count}",
                f"nOVR={override_in_match_count}",
                f"stable={max(0, now - server_since)}",
                f"on_srv={on_srv}",
                f"srvchg={srvchg}",
                f"baseline={baseline_server or '-'}",
            ]
            if server_for_phase or majority_id:
                bits.insert(1, f"srv={server_for_phase or majority_id}")
            if group_ready_since:
                bits.append(f"join_grace_left={join_left}")
            reason = ";".join(bits)

            # Persistieren
            self._write_lane_state(ch.id, active=is_active, suffix=suffix, ts=now, reason=reason)

            # Cache aktualisieren
            self._lane_cache[ch.id] = {
                "phase": phase,
                "server_id": server_for_phase if phase == PHASE_MATCH else None,
                "baseline_server_id": baseline_server,
                "last_majority_id": last_majority_id,
                "server_since": server_since,
                "server_changed_at": server_changed_at,
                "group_ready_since": group_ready_since,
                "last_seen": now,
            }

            # Telemetrie
            self._write_decision_metrics(
                now, ch.id, phase, voice_count, y_cap,
                server_for_phase or majority_id, majority_n,
                ig_count, dl_count, override_in_match_count,
                cohort_imputable_count, max(0, now - server_since), reason
            )

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
