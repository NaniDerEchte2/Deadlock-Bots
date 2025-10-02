# filename: cogs/live_match/live_match_master.py
# ------------------------------------------------------------
# LiveMatchMaster v3 – Server-ID-Muster + Zeit-Heuristik
#   • MATCH-Einstieg nur nach Mehrheits-Serverwechsel gegenüber Baseline
#   • Stabilitäts-/Join-Grace-Hysterese verhindert Flapping
#   • Confidence-Score (0..100) auf 15s-Basis; 20+ Minuten -> hohe/very_high Confidence
#   • Suffix mit Zähler (• n/cap …) wird in live_lane_state vorgeschlagen
#   • Worker (v2.2) übernimmt Rename mit 5-Minuten-Cooldown
#
# Datenhaltung (neu/erweitert):
#   • live_estimated_match_v1: 15s-Schreibungen (state_est, confidence, reason, …)
#   • live_decision_metrics_v3: bestehende Telemetrie
#   • live_lane_state: bestehender Suffix-Vorschlag (Worker liest ihn)
# ------------------------------------------------------------

import os
import time
import logging
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple, Any

import aiohttp
import discord
from discord.ext import commands, tasks

from service import db

log = logging.getLogger("LiveMatchMaster")

# ---- Konfiguration -----------------------------------------------------------

# Kategorien, deren Voice-Channels beobachtet werden
LIVE_CATEGORIES = [int(x) for x in os.getenv("LIVE_CATEGORIES", "").split(",") if x.strip().isdigit()]

DEADLOCK_APP_ID = os.getenv("DEADLOCK_APP_ID", "1422450")
STEAM_API_KEY   = os.getenv("STEAM_API_KEY", "").strip()

# Taktung / Schwellwerte
CHECK_INTERVAL_SEC    = int(os.getenv("LIVE_CHECK_INTERVAL_SEC", "15"))   # 15s – feinere Telemetrie
MIN_MATCH_GROUP       = int(os.getenv("MIN_MATCH_GROUP", "2"))            # Mehrheit mind. N Spieler
MAX_MATCH_CAP         = int(os.getenv("MAX_MATCH_CAP", "6"))              # Anzeige-Cap für Zähler

REQUIRE_STABILITY_SEC = int(os.getenv("REQUIRE_STABILITY_SEC", "30"))     # Stabilität der NEUEN Mehrheit
LOBBY_GRACE_SEC       = int(os.getenv("LOBBY_GRACE_SEC", "90"))           # „Gruppen-Join“-Grace (nur für Eskalation)
STATUS_TTL_SEC        = int(os.getenv("STATUS_TTL_SEC", "120"))           # Konsumenten-Filter (unverändert)

# Optionale Custom-Texte
AUTO_SUFFIX_MATCH  = os.getenv("AUTO_SUFFIX_MATCH",  "")
AUTO_SUFFIX_GAME   = os.getenv("AUTO_SUFFIX_GAME",   "")
AUTO_SUFFIX_LOBBY  = os.getenv("AUTO_SUFFIX_LOBBY",  "")

PHASE_OFF   = "OFF"
PHASE_GAME  = "GAME"
PHASE_LOBBY = "LOBBY"
PHASE_MATCH = "MATCH"

# ---- Schema & Helpers --------------------------------------------------------

def _ensure_schema() -> None:
    # Nutzer<->Steam Links (bestehend)
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

    # Momentane Lane-Mitglieder (bestehend)
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

    # Suffix-Ziel/Status je Channel (bestehend)
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_lane_state(
          channel_id  INTEGER PRIMARY KEY,
          is_active   INTEGER NOT NULL DEFAULT 0,
          last_update INTEGER NOT NULL,
          suffix      TEXT,
          reason      TEXT
        )
    """)

    # Decision Telemetrie (bestehend)
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

    # NEU: Estimated Match Status / Confidence (15s)
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_estimated_match_v1(
          id                 INTEGER PRIMARY KEY AUTOINCREMENT,
          ts                 INTEGER NOT NULL,
          channel_id         INTEGER NOT NULL,
          state_est          TEXT NOT NULL,        -- MATCH | GAME | LOBBY
          confidence         INTEGER NOT NULL,     -- 0..100
          confidence_label   TEXT NOT NULL,        -- low|medium|high|very_high
          current_server_id  TEXT,
          epoch_id           INTEGER,
          epoch_started_at   INTEGER,
          since_change_sec   INTEGER,
          baseline_server_id TEXT,
          short_lobby_before INTEGER NOT NULL DEFAULT 0, -- kurzer Lobby-Abschnitt vor MATCH (<=5min)
          long_match_flag    INTEGER NOT NULL DEFAULT 0, -- MATCH >=20min
          sticky_active      INTEGER NOT NULL DEFAULT 0, -- hohe Confidence => sticky
          reason             TEXT
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_lem1_chan_ts ON live_estimated_match_v1(channel_id, ts)")

def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))

def _fmt_suffix(dl_count: int, y_cap: int, label: str) -> str:
    """Formatiert den Suffix mit Zähler: '• n/cap label'."""
    try:
        cap = int(y_cap)
    except Exception:
        cap = 0
    try:
        n = int(dl_count)
    except Exception:
        n = 0
    cap = max(0, cap)
    n = max(0, min(n, cap if cap > 0 else n))
    base = label.strip()
    return f"• {n}/{cap} {base}" if base else f"• {n}/{cap}"

class LiveMatchMaster(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False
        # Cache pro Lane
        self._lane_cache: Dict[int, Dict[str, Any]] = {}

    async def cog_load(self):
        db.connect()
        _ensure_schema()
        if not self._started:
            self.scan_loop.start()
            self._started = True
        if not STEAM_API_KEY:
            log.warning("STEAM_API_KEY fehlt – Live-Scan läuft, aber ohne Steam-Daten ist die Erkennung eingeschränkt.")

    async def cog_unload(self):
        if self._started:
            try:
                self.scan_loop.cancel()
            except Exception as e:
                log.debug("scan_loop cancel beim Unload: %r", e)

    # -------- Steam Helpers --------

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

    # -------- Main Loop --------

    @tasks.loop(seconds=CHECK_INTERVAL_SEC)
    async def scan_loop(self):
        await self.bot.wait_until_ready()
        await self._run_once()

    async def _run_once(self):
        # 1) Lanes der konfigurierten Kategorien einsammeln
        lanes: List[discord.VoiceChannel] = []
        for g in self.bot.guilds:
            for cat_id in LIVE_CATEGORIES:
                cat = g.get_channel(cat_id)
                if isinstance(cat, discord.CategoryChannel):
                    lanes.extend(cat.voice_channels)

        # 2) Mitglieder/Links/Overrides
        members = [m for ch in lanes for m in ch.members if not m.bot]
        user_ids = sorted({m.id for m in members})

        links: Dict[int, List[str]] = defaultdict(list)
        if user_ids:
            qs = ",".join("?" for _ in user_ids)
            for r in db.query_all(f"SELECT user_id, steam_id FROM steam_links WHERE user_id IN ({qs})", tuple(user_ids)):
                links[int(r["user_id"])].append(str(r["steam_id"]))

        overrides: Dict[int, int] = {}
        if user_ids:
            try:
                qs = ",".join("?" for _ in user_ids)
                for r in db.query_all(
                    f"SELECT user_id, force_in_match FROM live_match_overrides WHERE user_id IN ({qs})",
                    tuple(user_ids)
                ):
                    overrides[int(r["user_id"])] = int(r["force_in_match"] or 0)
            except Exception as e:
                # Tabelle optional – wenn nicht vorhanden, ohne Overrides fortfahren
                log.debug("Overrides nicht verfügbar/fehlerhaft: %r", e)
                overrides = {}

        # 3) Steam Summaries ziehen
        all_steam = sorted({sid for arr in links.values() for sid in arr})
        summaries: Dict[str, dict] = {}
        async with aiohttp.ClientSession() as session:
            summaries = await self._steam_summaries(session, all_steam)

        now = int(time.time())

        # 4) Pro Lane entscheiden
        for ch in lanes:
            nonbots = [m for m in ch.members if not m.bot]
            voice_count = len(nonbots)
            y_cap = min(voice_count, MAX_MATCH_CAP)
            is_active = 1 if voice_count > 0 else 0

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
            cohort_imputable_count = 0  # ggf. später genutzt (Imputation)

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

            # Cache laden
            prev = self._lane_cache.get(ch.id, {})
            prev_phase = prev.get("phase", PHASE_OFF)
            prev_match_server = prev.get("server_id")  # nur gesetzt, wenn wir in MATCH waren
            last_majority_id = prev.get("last_majority_id")
            server_since = int(prev.get("server_since") or now)
            server_changed_at = int(prev.get("server_changed_at") or 0)
            baseline_server = prev.get("baseline_server_id")
            group_ready_since = prev.get("group_ready_since")
            last_seen = int(prev.get("last_seen") or now)

            # Majority Tracking & Baseline
            if majority_id and majority_id != last_majority_id:
                last_majority_id = majority_id
                server_since = now
                server_changed_at = now
                if baseline_server is None:
                    baseline_server = majority_id

            # Group-Ready
            if majority_n >= max(1, MIN_MATCH_GROUP):
                if not group_ready_since:
                    group_ready_since = now
            else:
                group_ready_since = None

            # Phase bestimmen
            phase = PHASE_LOBBY
            server_for_phase: Optional[str] = None
            suffix: Optional[str] = None

            if dl_count == 0:
                phase = PHASE_LOBBY if voice_count > 0 else PHASE_OFF
                if voice_count > 0:
                    suffix = _fmt_suffix(dl_count, y_cap, AUTO_SUFFIX_LOBBY or "In der Lobby")
                else:
                    suffix = None
                baseline_server = None  # Reset bei Leere
            else:
                if prev_phase == PHASE_MATCH and prev_match_server and majority_id != prev_match_server:
                    # harter Exit bei echtem Serverwechsel während MATCH
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

            # Suffix (Zähler-Form)
            if phase == PHASE_MATCH:
                suffix = _fmt_suffix(dl_count, y_cap, AUTO_SUFFIX_MATCH or "Im Match")
            elif phase == PHASE_GAME:
                suffix = _fmt_suffix(dl_count, y_cap, AUTO_SUFFIX_GAME or "Im Spiel")
            elif phase == PHASE_LOBBY:
                suffix = _fmt_suffix(dl_count, y_cap, AUTO_SUFFIX_LOBBY or "In der Lobby")

            # Reason-String
            join_left = -1
            if group_ready_since:
                need = LOBBY_GRACE_SEC - (now - int(group_ready_since))
                join_left = int(need if need > 0 else 0)
            srvchg = 1 if (server_changed_at and (now - server_changed_at) <= CHECK_INTERVAL_SEC) else 0

            bits = [
                f"phase={phase}",
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
            reason = ";".join(bits)

            # Persistieren (Lane-State, für Worker)
            self._write_lane_state(ch.id, active=is_active, suffix=suffix, ts=now, reason=reason)

            # ---------- Estimated Confidence (15s) + Suffix-Vorschlag ggf. erhöhen ----------
            try:
                est_conf, est_label, epoch_id, epoch_started_at, sticky_active, rextra = self._estimate_confidence(
                    ch.id, now, phase, majority_id, baseline_server, server_since, server_changed_at,
                    prev, majority_n, dl_count
                )
                # State-Schätzung für Analyse/Reporting
                est_state = "MATCH" if (phase == PHASE_MATCH or est_conf >= 75) else ("GAME" if ig_count > 0 else "LOBBY")
                self._write_estimated(
                    now, ch.id, est_state, est_conf, est_label, majority_id, epoch_id, epoch_started_at,
                    max(0, now - server_since), baseline_server,
                    short_lobby_before=1 if (phase != PHASE_MATCH and (now - server_since) <= 300) else 0,
                    long_match_flag=1 if (phase == PHASE_MATCH and (now - server_since) >= 1200) else 0,
                    sticky_active=1 if sticky_active else 0,
                    reason=reason + ";" + rextra
                )
                # Bei hoher Confidence Suffix klar auf „Im Match“ setzen (Worker greift es später auf)
                if est_state == "MATCH" and est_conf >= 80:
                    suffix = _fmt_suffix(dl_count, y_cap, AUTO_SUFFIX_MATCH or "Im Match")
                else:
                    suffix = _fmt_suffix(
                        dl_count, y_cap,
                        (AUTO_SUFFIX_GAME or "Im Spiel") if ig_count > 0 else (AUTO_SUFFIX_LOBBY or "In der Lobby")
                    )
                self._write_lane_state(ch.id, active=is_active, suffix=suffix, ts=now, reason=reason+";est")
            except Exception as e:
                log.debug("estimate/error: %r", e)

            # Cache aktualisieren
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

            # Decision-Metrics (bestehend)
            self._write_decision_metrics(now, ch.id, phase, voice_count, y_cap,
                                         majority_server_id=majority_id, majority_n=majority_n,
                                         ig_count=ig_count, dl_count=dl_count,
                                         override_in_match_count=override_in_match_count,
                                         cohort_imputable_count=cohort_imputable_count,
                                         stable_for_sec=max(0, now - server_since),
                                         reason=reason)

    # ---------- Confidence Estimator (Server-ID-Event + Dauer) ----------
    def _estimate_confidence(self, ch_id: int, now: int, phase: str, majority_id: Optional[str],
                             baseline_server_id: Optional[str], server_since: int,
                             last_server_change_at: int, prev: dict,
                             majority_n: int, dl_count: int) -> tuple[int, str, int, int, bool, str]:
        """
        Returns: (score 0..100, label, epoch_id, epoch_started_at, sticky_active, reason_extra)
        Heuristik:
          • Event-getrieben: Wechsel ggü. Baseline ist Primärsignal
          • Zeitkurve: 0..5min -> 20..50, 5..20 -> 50..80, 20..40 -> 80..95, >40 -> 96
          • Mehrheits-Boosts (majority_n >=3, dl_count >=3)
        """
        # Epoch inkrementieren, wenn Mehrheit-ID wechselt
        epoch_id = int(prev.get("epoch_id") or 0)
        epoch_started_at = int(prev.get("epoch_started_at") or server_since)
        last_majority_id = prev.get("last_majority_id")
        if majority_id and majority_id != last_majority_id:
            epoch_id += 1
            epoch_started_at = now

        dur = max(0, now - server_since)  # Dauer auf aktueller majority_id
        score = 0
        label = "low"

        # Wenn echter Serverwechsel ggü. Baseline (oder bereits MATCH), dann Zeitkurve anwenden
        if phase == PHASE_MATCH or (majority_id and baseline_server_id and majority_id != baseline_server_id):
            if dur < 300:
                score = 20 + int(dur * 30 / 300)          # 20..50
            elif dur < 1200:
                score = 50 + int((dur - 300) * 30 / 900)  # 50..80
            elif dur < 2400:
                score = 80 + int((dur - 1200) * 15 / 1200)  # 80..95
            else:
                score = 96
        else:
            # Keine klare Event-Situation -> niedrig
            score = 10 + min(20, dur // 60)

        # Boosts
        if majority_n >= 3:
            score += 5
        if dl_count >= 3:
            score += 3

        score = _clamp(score, 0, 100)

        if score >= 90:
            label = "very_high"
        elif score >= 75:
            label = "high"
        elif score >= 60:
            label = "medium"
        else:
            label = "low"

        sticky_active = score >= 75
        r = f"dur={dur}s;majN={majority_n};dlN={dl_count};epoch={epoch_id}"
        return score, label, epoch_id, epoch_started_at, sticky_active, r

    # ---- Persist Helpers -----------------------------------------------------

    def _write_estimated(self, ts:int, channel_id:int, state_est:str, confidence:int, label:str,
                         current_server_id:Optional[str], epoch_id:int, epoch_started_at:int,
                         since_change_sec:int, baseline_server_id:Optional[str],
                         short_lobby_before:int, long_match_flag:int, sticky_active:int, reason:str) -> None:
        db.execute(
            """
            INSERT INTO live_estimated_match_v1(
              ts, channel_id, state_est, confidence, confidence_label, current_server_id,
              epoch_id, epoch_started_at, since_change_sec, baseline_server_id,
              short_lobby_before, long_match_flag, sticky_active, reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (int(ts), int(channel_id), state_est, int(confidence), label, current_server_id,
             int(epoch_id) if epoch_id is not None else None,
             int(epoch_started_at) if epoch_started_at is not None else None,
             int(since_change_sec) if since_change_sec is not None else None,
             baseline_server_id,
             int(short_lobby_before or 0), int(long_match_flag or 0), int(sticky_active or 0), reason)
        )

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

async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchMaster(bot))
