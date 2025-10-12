import asyncio
import json
import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

import discord
from discord.ext import commands, tasks

from service import db

log = logging.getLogger("LiveMatchMaster")

# Nur diese Kategorien werden gescannt (wie gehabt)
LIVE_CATEGORIES: List[int] = [
    1289721245281292290,
    1412804540994162789,
]

DEBUG_CHANNEL_ID = 1374364800817303632

# Scan-Intervalle/Frische
CHECK_INTERVAL_SEC = 15
PRESENCE_FRESH_SEC = 120

# Neu: Debounce für identische Zustände
LOG_RATE_SEC = 600  # 10 Minuten

PHASE_OFF = "OFF"
PHASE_GAME = "GAME"
PHASE_LOBBY = "LOBBY"
PHASE_MATCH = "MATCH"

_MATCH_TERMS = (
    "#deadlock_status_inmatch",
    "in match",
    "match",
    "playing match",
)
_LOBBY_TERMS = (
    "lobby",
    "queue",
    "warteschlange",
    "search",
    "searching",
    "suche",
)
_GAME_TERMS = (
    "#deadlock_status_ingame",
    "ingame",
    "im spiel",
    "playing",
    "spiel",
    "game",
)


def _fmt_suffix(majority_n: int, voice_n: int, label: str, dl_count: int) -> str:
    voice_n = max(0, int(voice_n))
    majority_n = max(0, min(int(majority_n), voice_n))
    dl_count = max(0, min(int(dl_count), voice_n))
    suffix = f"• {majority_n}/{voice_n} {label}"
    if dl_count:
        suffix = f"{suffix} ({dl_count} DL)"
    return suffix.strip()


@dataclass
class PresenceInfo:
    steam_id: str
    updated_at: int
    display: Optional[str]
    status: Optional[str]
    status_text: Optional[str]
    player_group: Optional[str]
    player_group_size: Optional[int]
    connect: Optional[str]
    mode: Optional[str]
    map_name: Optional[str]
    party_size: Optional[int]
    raw: Dict[str, Any]
    phase_hint: Optional[str]
    is_match: bool
    is_lobby: bool
    is_deadlock: bool


def _ensure_schema() -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS steam_links(
          user_id    INTEGER NOT NULL,
          steam_id   TEXT    NOT NULL,
          name       TEXT,
          verified   INTEGER DEFAULT 0,
          primary_account INTEGER DEFAULT 0,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(user_id, steam_id)
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_user  ON steam_links(user_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_steam ON steam_links(steam_id)")

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS live_lane_state(
          channel_id  INTEGER PRIMARY KEY,
          is_active   INTEGER DEFAULT 0,
          last_update INTEGER,
          suffix      TEXT,
          reason      TEXT
        )
        """
    )


class LiveMatchMaster(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._started = False
        self._links_cache: Dict[int, List[str]] = {}
        self._presence_cache: Dict[str, PresenceInfo] = {}
        # Neu: Merker des letzten geschriebenen Zustands pro Channel
        self._last_state: Dict[int, Dict[str, Any]] = {}
        self._last_debug_payload: Dict[int, str] = {}

    async def cog_load(self):
        db.connect()
        _ensure_schema()

        try:
            await self._run_once()
            await asyncio.sleep(2)
            await self._run_once()
            log.info("LiveMatchMaster Cold-Start-Resync abgeschlossen.")
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Cold-Start-Resync Fehler: %r", exc)

        if not self._started:
            self.scan_loop.start()
            self._started = True
            log.info("LiveMatchMaster gestartet (Tick=%ss).", CHECK_INTERVAL_SEC)

    async def cog_unload(self):
        if self._started:
            try:
                self.scan_loop.cancel()
            except Exception:  # pragma: no cover - defensive
                log.debug("scan_loop cancel beim Unload ignoriert")
            self._started = False

    @tasks.loop(seconds=CHECK_INTERVAL_SEC)
    async def scan_loop(self):
        await self.bot.wait_until_ready()
        await self._run_once()

    # ------------------------------------------------------------------ helpers
    def _collect_voice_channels(self) -> List[discord.VoiceChannel]:
        channels: Dict[int, discord.VoiceChannel] = {}
        guild_categories = LIVE_CATEGORIES or []
        for guild in self.bot.guilds:
            if guild_categories:
                for category_id in guild_categories:
                    category = guild.get_channel(category_id)
                    if isinstance(category, discord.CategoryChannel):
                        for voice in category.voice_channels:
                            channels[voice.id] = voice
            else:
                for voice in guild.voice_channels:
                    channels[voice.id] = voice
        return list(channels.values())

    def _load_links(self, user_ids: Iterable[int]) -> Dict[int, List[str]]:
        ids = list({int(uid) for uid in user_ids})
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = db.query_all(
            f"SELECT user_id, steam_id FROM steam_links WHERE user_id IN ({placeholders})",
            tuple(ids),
        )
        mapping: Dict[int, List[str]] = defaultdict(list)
        for row in rows:
            try:
                user_id = int(row["user_id"] if isinstance(row, dict) else row[0])
                steam_id = str(row["steam_id"] if isinstance(row, dict) else row[1])
            except Exception:
                continue
            if steam_id:
                mapping[user_id].append(steam_id)
        return dict(mapping)

    def _load_presence_map(self, steam_ids: Iterable[str], now: int) -> Dict[str, PresenceInfo]:
        ids = sorted({str(sid) for sid in steam_ids if sid})
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        min_ts = max(0, int(now) - PRESENCE_FRESH_SEC)
        rows = db.query_all(
            f"""
            SELECT steam_id, app_id, status, status_text, display, player_group,
                   player_group_size, connect, mode, map, party_size, raw_json,
                   updated_at, last_update
            FROM steam_rich_presence
            WHERE steam_id IN ({placeholders})
              AND COALESCE(updated_at, last_update, 0) >= ?
            """,
            (*ids, int(min_ts)),
        )
        presence: Dict[str, PresenceInfo] = {}
        for row in rows:
            raw_json = row["raw_json"] if isinstance(row, dict) else row[11]
            try:
                raw = {} if raw_json in (None, "") else dict(json.loads(raw_json))
            except Exception:
                raw = {}
            steam_id = str(row["steam_id"] if isinstance(row, dict) else row[0])
            try:
                updated_at = int(
                    (row["updated_at"] if isinstance(row, dict) else row[12])
                    or (row["last_update"] if isinstance(row, dict) else row[13])
                    or 0
                )
            except Exception:
                updated_at = 0
            status = row["status"] if isinstance(row, dict) else row[2]
            status_text = row["status_text"] if isinstance(row, dict) else row[3]
            display = row["display"] if isinstance(row, dict) else row[4]
            player_group = row["player_group"] if isinstance(row, dict) else row[5]
            player_group_size = row["player_group_size"] if isinstance(row, dict) else row[6]
            connect = row["connect"] if isinstance(row, dict) else row[7]
            mode = row["mode"] if isinstance(row, dict) else row[8]
            map_name = row["map"] if isinstance(row, dict) else row[9]
            party_size = row["party_size"] if isinstance(row, dict) else row[10]

            info_dict = {
                "steam_id": steam_id,
                "updated_at": updated_at,
                "status": status,
                "status_text": status_text,
                "display": display,
                "player_group": player_group,
                "player_group_size": player_group_size,
                "connect": connect,
                "mode": mode,
                "map_name": map_name,
                "party_size": party_size,
                "raw": raw,
            }
            presence[steam_id] = self._build_presence_info(info_dict)
        return presence

    def _build_presence_info(self, entry: Dict[str, Any]) -> PresenceInfo:
        steam_id = str(entry.get("steam_id") or "")
        updated_at = int(entry.get("updated_at") or 0)
        status = entry.get("status")
        status_text = entry.get("status_text")
        display = entry.get("display")
        player_group = entry.get("player_group") or entry.get("raw", {}).get("steam_player_group")
        raw_group_size = entry.get("player_group_size") or entry.get("raw", {}).get("steam_player_group_size")
        try:
            player_group_size = int(raw_group_size) if raw_group_size is not None else None
        except (TypeError, ValueError):
            player_group_size = None
        connect = entry.get("connect") or entry.get("raw", {}).get("connect")
        mode = entry.get("mode") or entry.get("raw", {}).get("mode")
        map_name = entry.get("map_name") or entry.get("raw", {}).get("map")
        raw_party_size = entry.get("party_size") or entry.get("raw", {}).get("party_size")
        try:
            party_size = int(raw_party_size) if raw_party_size is not None else None
        except (TypeError, ValueError):
            party_size = None
        raw = entry.get("raw") if isinstance(entry.get("raw"), dict) else {}

        phase_hint = self._presence_phase_hint(
            {
                "status": status,
                "status_text": status_text,
                "display": display,
                "player_group": player_group,
                "player_group_size": player_group_size,
                "connect": connect,
                "raw": raw,
            }
        )
        is_match = phase_hint == PHASE_MATCH
        is_lobby = phase_hint == PHASE_LOBBY
        is_deadlock = self._presence_in_deadlock(
            {
                "status": status,
                "status_text": status_text,
                "display": display,
                "raw": raw,
            }
        )

        return PresenceInfo(
            steam_id=steam_id,
            updated_at=updated_at,
            display=display,
            status=status,
            status_text=status_text,
            player_group=str(player_group) if player_group else None,
            player_group_size=player_group_size,
            connect=connect,
            mode=mode,
            map_name=map_name,
            party_size=party_size,
            raw=raw,
            phase_hint=phase_hint,
            is_match=is_match,
            is_lobby=is_lobby,
            is_deadlock=is_deadlock,
        )

    def _presence_phase_hint(self, data: Dict[str, Any]) -> Optional[str]:
        connect = data.get("connect") or data.get("raw", {}).get("connect")
        if isinstance(connect, str) and connect:
            return PHASE_MATCH

        group = data.get("player_group")
        group_size = data.get("player_group_size")
        try:
            group_size_int = int(group_size) if group_size is not None else 0
        except (TypeError, ValueError):
            group_size_int = 0
        if group and group_size_int:
            return PHASE_LOBBY

        texts: List[str] = []
        for key in ("status", "status_text", "display"):
            val = data.get(key)
            if val:
                texts.append(str(val))
        raw = data.get("raw") or {}
        for key in ("status", "steam_display", "display", "rich_presence"):
            val = raw.get(key)
            if val:
                texts.append(str(val))
        blob = " ".join(texts).lower()

        if any(term in blob for term in _MATCH_TERMS):
            return PHASE_MATCH
        if any(term in blob for term in _LOBBY_TERMS):
            return PHASE_LOBBY
        if any(term in blob for term in _GAME_TERMS):
            return PHASE_GAME
        return None

    def _presence_in_deadlock(self, data: Dict[str, Any]) -> bool:
        raw = data.get("raw") or {}
        texts = [
            str(data.get("status") or ""),
            str(data.get("status_text") or ""),
            str(data.get("display") or ""),
            str(raw.get("steam_display") or ""),
            str(raw.get("status") or ""),
        ]
        blob = " ".join(t for t in texts if t).lower()
        return "deadlock" in blob

    def get_presence_for_discord_user(self, discord_id: int) -> Optional[PresenceInfo]:
        steam_ids = self._links_cache.get(int(discord_id))
        if not steam_ids:
            return None
        for steam_id in steam_ids:
            info = self._presence_cache.get(str(steam_id))
            if info:
                return info
        return None

    def _determine_phase(
        self,
        voice_members: List[discord.Member],
    ) -> Dict[str, Any]:
        voice_n = len(voice_members)
        dl_count = 0
        state_counts: Counter[str] = Counter()
        member_states: List[Dict[str, Any]] = []

        for member in voice_members:
            presence = self.get_presence_for_discord_user(member.id)
            links = self._links_cache.get(member.id, [])
            state: Optional[str] = None
            if presence:
                state = PHASE_OFF
                dl_count += 1
                if presence.is_match:
                    state = PHASE_MATCH
                elif presence.is_lobby:
                    state = PHASE_LOBBY
                elif presence.is_deadlock:
                    state = PHASE_GAME
                state_counts[state] += 1

            member_states.append(
                {
                    "id": int(member.id),
                    "name": str(member.display_name),
                    "steam_ids": list(links),
                    "status": state or "NO_LINK",
                    "phase_hint": presence.phase_hint if presence else None,
                    "is_match": bool(presence.is_match) if presence else False,
                    "is_lobby": bool(presence.is_lobby) if presence else False,
                    "is_deadlock": bool(presence.is_deadlock) if presence else False,
                }
            )

        match_signals = state_counts.get(PHASE_MATCH, 0)
        lobby_signals = state_counts.get(PHASE_LOBBY, 0)
        game_signals = state_counts.get(PHASE_GAME, 0)
        off_signals = state_counts.get(PHASE_OFF, 0)

        majority_phase = PHASE_OFF
        majority_n = 0
        for candidate in (PHASE_MATCH, PHASE_LOBBY, PHASE_GAME, PHASE_OFF):
            count = state_counts.get(candidate, 0)
            if count > majority_n:
                majority_phase = candidate
                majority_n = count

        phase = PHASE_OFF
        suffix = None
        if majority_phase == PHASE_MATCH and majority_n > 0:
            phase = PHASE_MATCH
            suffix = _fmt_suffix(majority_n, voice_n, "Im Match", dl_count)
        elif majority_phase == PHASE_LOBBY and majority_n > 0:
            phase = PHASE_LOBBY
            suffix = _fmt_suffix(majority_n, voice_n, "In der Lobby", dl_count)
        elif majority_phase == PHASE_GAME and dl_count > 0:
            phase = PHASE_GAME
            suffix = _fmt_suffix(majority_n, voice_n, "Im Spiel", dl_count)

        reason = (
            f"voice={voice_n};dl={dl_count};match={match_signals};lobby={lobby_signals};game={game_signals};"
            f"off={off_signals};majority={majority_phase}:{majority_n};phase={phase}"
        )
        return {
            "voice_n": voice_n,
            "dl_count": dl_count,
            "match_signals": match_signals,
            "lobby_signals": lobby_signals,
            "game_signals": game_signals,
            "off_signals": off_signals,
            "majority_phase": majority_phase,
            "majority_n": majority_n,
            "phase": phase,
            "suffix": suffix,
            "reason": reason,
            "state_counts": dict(state_counts),
            "member_states": member_states,
        }

    def _should_write_state(self, channel_id: int, phase_result: Dict[str, Any], now: int) -> bool:
        """Nur schreiben, wenn sich der Zustand geändert hat oder das Re-Log-Intervall abgelaufen ist."""
        prev = self._last_state.get(channel_id)
        current_key = (phase_result.get("phase"), phase_result.get("suffix"))
        if prev:
            prev_key = (prev.get("phase"), prev.get("suffix"))
            same = prev_key == current_key
            recent = (now - int(prev.get("ts", 0))) < LOG_RATE_SEC
            if same and recent:
                # Nichts getan – zu frisch, wir sparen uns DB/INFO-Log
                return False
        return True

    def _remember_state(self, channel_id: int, phase_result: Dict[str, Any], now: int) -> None:
        self._last_state[channel_id] = {
            "phase": phase_result.get("phase"),
            "suffix": phase_result.get("suffix"),
            "reason": phase_result.get("reason"),
            "ts": int(now),
        }

    def _format_debug_payload(
        self,
        channel: discord.VoiceChannel,
        phase_result: Dict[str, Any],
        *,
        will_write: bool,
    ) -> Optional[str]:
        state_counts = phase_result.get("state_counts", {})
        counts_line = (
            f"Match={state_counts.get(PHASE_MATCH, 0)} | "
            f"Lobby={state_counts.get(PHASE_LOBBY, 0)} | "
            f"Spiel={state_counts.get(PHASE_GAME, 0)} | "
            f"Off={state_counts.get(PHASE_OFF, 0)}"
        )

        lines = [
            f"Channel: {channel.name} ({channel.id})",
            (
                "Phase={phase} | Mehrheit={majority}:{count} | WillWrite={write}".format(
                    phase=phase_result.get("phase"),
                    majority=phase_result.get("majority_phase"),
                    count=phase_result.get("majority_n"),
                    write="ja" if will_write else "nein",
                )
            ),
            f"Suffix={phase_result.get('suffix') or '-'}",
            (
                "Voice={voice} | Deadlock={dl}".format(
                    voice=phase_result.get("voice_n"),
                    dl=phase_result.get("dl_count"),
                )
            ),
            f"Counts: {counts_line}",
            f"Reason: {phase_result.get('reason')}",
            "Mitglieder:",
        ]

        member_lines: List[str] = []
        for member in phase_result.get("member_states", []):
            steam_ids = ", ".join(member.get("steam_ids") or []) or "-"
            flags: List[str] = []
            if member.get("is_match"):
                flags.append("match")
            if member.get("is_lobby"):
                flags.append("lobby")
            if member.get("is_deadlock"):
                flags.append("deadlock")
            flag_text = ",".join(flags) if flags else "-"
            member_lines.append(
                (
                    "- {name} ({mid}): {status} | steam={steam} | flags={flags} | hint={hint}".format(
                        name=member.get("name"),
                        mid=member.get("id"),
                        status=member.get("status"),
                        steam=steam_ids,
                        flags=flag_text,
                        hint=member.get("phase_hint") or "-",
                    )
                )
            )

        max_members = 15
        if len(member_lines) > max_members:
            extra = len(member_lines) - max_members
            member_lines = member_lines[:max_members]
            member_lines.append(f"… ({extra} weitere Mitglieder)")

        lines.extend(member_lines)
        content = "\n".join(lines)
        if len(content) > 1900:
            content = f"{content[:1897]}…"
        return content

    async def _send_debug_report(
        self,
        channel: discord.VoiceChannel,
        phase_result: Dict[str, Any],
        *,
        will_write: bool,
    ) -> None:
        debug_channel = self.bot.get_channel(DEBUG_CHANNEL_ID)
        if not isinstance(debug_channel, discord.TextChannel):
            return

        payload = self._format_debug_payload(channel, phase_result, will_write=will_write)
        if not payload:
            return

        last_payload = self._last_debug_payload.get(channel.id)
        if last_payload == payload:
            return

        try:
            await debug_channel.send(payload)
            self._last_debug_payload[channel.id] = payload
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            log.debug("Debug-Ausgabe fehlgeschlagen für %s: %s", channel.id, exc)

    def _write_lane_state(
        self,
        channel_id: int,
        phase_result: Dict[str, Any],
        now: int,
    ) -> None:
        suffix = phase_result["suffix"]
        is_active = 1 if suffix else 0
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
            (int(channel_id), int(is_active), int(now), suffix, phase_result["reason"]),
        )
        # INFO nur wenn wir wirklich schreiben
        log.info(
            "STATE_WRITE channel=%d phase=%s suffix=%s reason=%s",
            channel_id,
            phase_result["phase"],
            suffix or "",
            phase_result["reason"],
        )

    # ---------------------------------------------------------------- core loop
    async def _run_once(self) -> None:
        channels = self._collect_voice_channels()
        now = int(time.time())

        # Vorab alle relevanten Voice-Mitglieder einsammeln (ohne Bots)
        members_per_channel: Dict[int, List[discord.Member]] = {}
        all_members: List[discord.Member] = []
        for channel in channels:
            members = [m for m in channel.members if not m.bot]
            members_per_channel[channel.id] = members
            all_members.extend(members)

        # Steam-Links nur für tatsächlich anwesende User laden
        self._links_cache = self._load_links(member.id for member in all_members)
        all_steam_ids = [sid for ids in self._links_cache.values() for sid in ids]
        # Rich Presence nur laden, wenn überhaupt Links da sind
        self._presence_cache = self._load_presence_map(all_steam_ids, now) if all_steam_ids else {}

        for channel in channels:
            members = members_per_channel.get(channel.id, [])

            # Guard 1: Keine Voice-Mitglieder -> komplett skip (kein Log/Write)
            if not members:
                log.debug("skip channel=%s: no voice members", channel.id)
                continue

            # Guard 2: Keiner im Channel ist verknüpft -> skip
            linked_members = [m for m in members if self._links_cache.get(m.id)]
            if not linked_members:
                log.debug("skip channel=%s: no linked steam accounts", channel.id)
                continue

            # Phase bestimmen (nutzt intern Presence aus dem Cache)
            phase_result = self._determine_phase(members)

            # Log der Entscheidung: nur INFO, wenn aktiv oder sich was ändert
            will_write = self._should_write_state(channel.id, phase_result, now)
            if phase_result["phase"] == PHASE_OFF and not will_write:
                log.debug(
                    "PHASE_DECISION (no-change) channel_members=%d dl_count=%d match=%d lobby=%d game=%d off=%d majority_phase=%s majority_n=%d phase=%s",
                    phase_result["voice_n"],
                    phase_result["dl_count"],
                    phase_result["match_signals"],
                    phase_result["lobby_signals"],
                    phase_result["game_signals"],
                    phase_result["off_signals"],
                    phase_result["majority_phase"],
                    phase_result["majority_n"],
                    phase_result["phase"],
                )
            else:
                log.info(
                    "PHASE_DECISION channel_members=%d dl_count=%d match=%d lobby=%d game=%d off=%d majority_phase=%s majority_n=%d phase=%s",
                    phase_result["voice_n"],
                    phase_result["dl_count"],
                    phase_result["match_signals"],
                    phase_result["lobby_signals"],
                    phase_result["game_signals"],
                    phase_result["off_signals"],
                    phase_result["majority_phase"],
                    phase_result["majority_n"],
                    phase_result["phase"],
                )

            await self._send_debug_report(channel, phase_result, will_write=will_write)

            # Nur schreiben, wenn nötig (Change / Re-Log Intervall)
            if will_write:
                self._write_lane_state(channel.id, phase_result, now)
                self._remember_state(channel.id, phase_result, now)

async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchMaster(bot))
