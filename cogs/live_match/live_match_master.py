# cogs/live_match_master.py
# ------------------------------------------------------------
# LiveMatchMaster – Steam-Status auswerten & pro Voice-Lane gruppieren
# (Keine Channel-Umbenennungen hier! Das macht der Worker-Bot.)
#
# DB-Tabellen (werden automatisch angelegt):
#   steam_links(user_id BIGINT, steam_id TEXT, ...)
#   live_lane_members(channel_id BIGINT, user_id BIGINT, in_match INT, server_id TEXT, checked_ts INT)
#   live_lane_state(channel_id BIGINT PRIMARY KEY, is_active INT, last_update INT, suffix TEXT, reason TEXT)
#
# Anzeige/Suffix:
#   • n/cap Im Match        -> stabile Mehrheit (>= MIN_MATCH_GROUP) teilt gameserversteamid über REQUIRE_STABILITY_SEC
#   • x/cap Im Spiel        -> mind. ein Server, aber keine stabile Mehrheit (Queue/Pre-Game/verschiedene Server)
#   • x/cap Lobby/Queue     -> in Deadlock, aber ohne Server-ID (reine Lobby/Loading)
# ------------------------------------------------------------

import os
import time
import logging
from collections import Counter, defaultdict
from typing import Dict, List, Optional

import aiohttp
import discord
from discord.ext import commands, tasks

try:
    from shared import db  # synchrones Wrapper-Modul (execute/query_all/executemany)
except Exception as e:
    raise SystemExit("shared.db nicht gefunden – bitte Projektstruktur prüfen.") from e

log = logging.getLogger("LiveMatchMaster")

# ===== Konfiguration über ENV =====
LIVE_CATEGORIES = [int(x) for x in os.getenv(
    "LIVE_CATEGORIES",
    "1289721245281292290,1357422957017698478"  # Beispiel: Casual, Ranked
).split(",") if x.strip()]

DEADLOCK_APP_ID = os.getenv("DEADLOCK_APP_ID", "1422450")
STEAM_API_KEY   = os.getenv("STEAM_API_KEY", "")

CHECK_INTERVAL_SEC       = int(os.getenv("LIVE_CHECK_INTERVAL_SEC", "30"))
MIN_MATCH_GROUP          = int(os.getenv("MIN_MATCH_GROUP", "2"))

DEFAULT_CASUAL_CAP       = int(os.getenv("DEFAULT_CASUAL_CAP", "8"))
RANKED_CATEGORY_ID       = int(os.getenv("RANKED_CATEGORY_ID", "1357422957017698478"))
DEFAULT_RANKED_CAP       = int(os.getenv("DEFAULT_RANKED_CAP", "6"))

# Heuristik-Parameter
REQUIRE_STABILITY_SEC    = int(os.getenv("REQUIRE_STABILITY_SEC", "30"))   # Mehrheit muss so lange stabil sein
LOBBY_GRACE_SEC          = int(os.getenv("LOBBY_GRACE_SEC", "90"))        # kurze Lücke (Ladebildschirm)
MATCH_MIN_MINUTES        = int(os.getenv("MATCH_MIN_MINUTES", "15"))      # rein informativ

PHASE_OFF   = "OFF"
PHASE_LOBBY = "LOBBY"   # in DL, aber keine Server-ID -> Queue/Lobby/Loading
PHASE_GAME  = "GAME"    # Leute auf Servern, aber keine stabile Mehrheit
PHASE_MATCH = "MATCH"   # stabile Mehrheit auf einem Server

# ===== Schema automatisch sicherstellen =====
def _ensure_schema() -> None:
    # steam_links kann aus dem Link-Cog kommen – hier nur zur Sicherheit schlank anlegen
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
    db.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_user ON steam_links(user_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_steam ON steam_links(steam_id)")

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
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_lane_state(
          channel_id  INTEGER PRIMARY KEY,
          is_active   INTEGER NOT NULL DEFAULT 0,
          last_update INTEGER NOT NULL,
          suffix      TEXT,
          reason      TEXT
        )
    """)

class LiveMatchMaster(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False
        # channel_id -> {"phase": str, "server_id": Optional[str], "since": int, "last_seen": int, "stable_since": int}
        self._lane_cache: Dict[int, Dict[str, Optional[int | str]]] = {}

    async def cog_load(self):
        db.connect()
        _ensure_schema()
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
        # 1) Alle Voice Channels aus konfigurierten Kategorien
        lanes: List[discord.VoiceChannel] = []
        for g in self.bot.guilds:
            for cat_id in LIVE_CATEGORIES:
                cat = g.get_channel(cat_id)
                if isinstance(cat, discord.CategoryChannel):
                    lanes.extend(cat.voice_channels)

        # 2) Discord->Steam Links sammeln
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

        # 3) Steam zusammengefasst abfragen
        all_steam = sorted({sid for arr in links.values() for sid in arr})
        async with aiohttp.ClientSession() as session:
            summaries = await self._steam_summaries(session, all_steam)

        now = int(time.time())

        # 4) Pro Lane auswerten & in DB schreiben
        for ch in lanes:
            nonbots = [m for m in ch.members if not m.bot]
            if not nonbots:
                self._write_lane_state(ch.id, active=0, suffix=None, ts=now, reason="empty")
                self._clear_lane_members(ch.id)
                self._lane_cache.pop(ch.id, None)
                continue

            # pro User: In DL? server_id?
            ig_with_server = []     # Mitglieder mit Server-ID
            deadlockers = []        # in Deadlock (mit oder ohne Server-ID)
            lane_members_rows = []
            for m in nonbots:
                found_sid = None
                in_dl = False
                for sid in links.get(m.id, []):
                    s = summaries.get(sid)
                    if not s:
                        continue
                    if self._in_deadlock(s):
                        in_dl = True
                        sid_server = self._server_id(s)
                        if sid_server:
                            found_sid = sid_server
                            break
                lane_members_rows.append((ch.id, m.id, 1 if found_sid else 0, found_sid, now))
                if in_dl:
                    deadlockers.append(m.id)
                if found_sid:
                    ig_with_server.append((m.id, found_sid))

            # Cache lane members aktualisieren
            self._upsert_lane_members(lane_members_rows)

            # Gruppierung per Server-ID
            server_ids = [sid for _, sid in ig_with_server]
            majority_id: Optional[str] = None
            majority_n = 0
            if server_ids:
                cnt = Counter(server_ids)
                majority_id, majority_n = cnt.most_common(1)[0]

            cap = self._cap(ch)
            ig_count = len(ig_with_server)
            dl_count = len(deadlockers)

            # --- Heuristik-Entscheidung ---
            prev = self._lane_cache.get(ch.id, {"phase": PHASE_OFF, "server_id": None, "since": None, "stable_since": None, "last_seen": None})
            phase = PHASE_OFF
            server_for_phase: Optional[str] = None
            since = int(prev.get("since") or now)
            stable_since = int(prev.get("stable_since") or now)

            # Zustände ermitteln
            if majority_id and majority_n >= max(1, MIN_MATCH_GROUP):
                # Mehrheit existiert -> stabilisieren
                if prev.get("server_id") == majority_id and prev.get("phase") in (PHASE_MATCH, PHASE_GAME, PHASE_LOBBY):
                    # gleicher Server wie vorher -> Stabilitätszeit laufen lassen
                    if (now - int(prev.get("stable_since") or now)) >= REQUIRE_STABILITY_SEC:
                        phase = PHASE_MATCH
                    else:
                        phase = PHASE_GAME  # pre-match, noch nicht stabil genug
                else:
                    # neuer (oder erster) Mehrheitsserver -> Stabilität neu starten
                    stable_since = now
                    since = now
                    phase = PHASE_GAME  # wechsle zunächst auf GAME, springe später auf MATCH
                server_for_phase = majority_id

                # Wenn Stabilität erreicht, zu MATCH befördern
                if phase == PHASE_GAME and (now - stable_since) >= REQUIRE_STABILITY_SEC:
                    phase = PHASE_MATCH

            elif ig_count > 0:
                # Leute sind auf Servern, aber keine stabile Mehrheit
                # Grace: war vorher MATCH und Lücke ist sehr kurz? -> halte MATCH
                if prev.get("phase") == PHASE_MATCH and (now - int(prev.get("last_seen") or now)) <= LOBBY_GRACE_SEC:
                    phase = PHASE_MATCH
                    server_for_phase = prev.get("server_id")  # behalte bisherigen
                else:
                    phase = PHASE_GAME
                    server_for_phase = None
                    since = prev.get("since") or now  # egal

            elif dl_count > 0:
                # In Deadlock ohne Server-ID -> Lobby/Queue
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

            # Sichtbarer Suffix + Aktiv-Flag ableiten
            suffix: Optional[str] = None
            is_active = 0
            if phase == PHASE_MATCH:
                n = majority_n if majority_n else ig_count
                suffix = f"• {n}/{cap} Im Match"
                is_active = 1
            elif phase == PHASE_GAME:
                suffix = f"• {ig_count}/{cap} Im Spiel"
            elif phase == PHASE_LOBBY:
                suffix = f"• {dl_count}/{cap} Lobby/Queue"
            else:
                suffix = None

            # Debug/Reason setzen (hilfreich fürs Loggen)
            reason_bits = [f"phase={phase}"]
            if server_for_phase:
                reason_bits.append(f"srv={server_for_phase}")
            reason_bits.append(f"cap={cap}")
            reason_bits.append(f"nMaj={majority_n}")
            reason_bits.append(f"nIG={ig_count}")
            reason_bits.append(f"nDL={dl_count}")
            reason = ";".join(reason_bits)

            self._write_lane_state(
                ch.id,
                active=is_active,
                suffix=suffix,
                ts=now,
                reason=reason
            )

            # Cache aktualisieren
            self._lane_cache[ch.id] = {
                "phase": phase,
                "server_id": server_for_phase,
                "since": since,
                "stable_since": stable_since,
                "last_seen": now,
            }

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
