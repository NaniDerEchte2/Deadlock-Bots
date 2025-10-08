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

LIVE_CATEGORIES: List[int] = [
    1289721245281292290,
    1412804540994162789,
]

CHECK_INTERVAL_SEC = 15
PRESENCE_FRESH_SEC = 120
MIN_MATCH_GROUP = 2
MAX_MATCH_CAP = 6

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


def _fmt_suffix(dl_count: int, voice_n: int, label: str) -> str:
    voice_n = max(0, int(voice_n))
    dl_count = max(0, min(int(dl_count), voice_n))
    return f"• {dl_count}/{voice_n} (max {MAX_MATCH_CAP}) {label}".strip()


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
        match_signals = 0
        lobby_signals = 0
        group_match_counter: Counter[str] = Counter()

        for member in voice_members:
            presence = self.get_presence_for_discord_user(member.id)
            if not presence:
                continue
            dl_count += 1
            if presence.player_group and presence.is_match:
                group_match_counter[presence.player_group] += 1
            if presence.is_match:
                match_signals += 1
            if presence.is_lobby:
                lobby_signals += 1

        majority_id: Optional[str] = None
        majority_n = 0
        if group_match_counter:
            majority_id, majority_n = group_match_counter.most_common(1)[0]

        phase = PHASE_OFF
        suffix = None
        if dl_count == 0:
            phase = PHASE_OFF
        elif majority_id and majority_n >= MIN_MATCH_GROUP:
            phase = PHASE_MATCH
            suffix = _fmt_suffix(dl_count, voice_n, "Im Match")
        elif lobby_signals > match_signals and lobby_signals > 0:
            phase = PHASE_LOBBY
            suffix = _fmt_suffix(dl_count, voice_n, "In der Lobby")
        else:
            phase = PHASE_GAME
            suffix = _fmt_suffix(dl_count, voice_n, "Im Spiel")

        reason = (
            f"voice={voice_n};dl={dl_count};match={match_signals};lobby={lobby_signals};"
            f"majority={majority_id or '-'}:{majority_n};phase={phase}"
        )
        log.info(
            "PHASE_DECISION channel_members=%d dl_count=%d match=%d lobby=%d majority_id=%s majority_n=%d phase=%s",
            voice_n,
            dl_count,
            match_signals,
            lobby_signals,
            majority_id,
            majority_n,
            phase,
        )
        return {
            "voice_n": voice_n,
            "dl_count": dl_count,
            "match_signals": match_signals,
            "lobby_signals": lobby_signals,
            "majority_id": majority_id,
            "majority_n": majority_n,
            "phase": phase,
            "suffix": suffix,
            "reason": reason,
        }

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

        members_per_channel: Dict[int, List[discord.Member]] = {}
        all_members: List[discord.Member] = []
        for channel in channels:
            members = [m for m in channel.members if not m.bot]
            members_per_channel[channel.id] = members
            all_members.extend(members)

        self._links_cache = self._load_links(member.id for member in all_members)
        all_steam_ids = [sid for ids in self._links_cache.values() for sid in ids]
        self._presence_cache = self._load_presence_map(all_steam_ids, now)

        for channel in channels:
            members = members_per_channel.get(channel.id, [])
            if not members:
                phase_result = {
                    "voice_n": 0,
                    "dl_count": 0,
                    "match_signals": 0,
                    "lobby_signals": 0,
                    "majority_id": None,
                    "majority_n": 0,
                    "phase": PHASE_OFF,
                    "suffix": None,
                    "reason": "phase=OFF;voice=0",
                }
                log.info(
                    "PHASE_DECISION channel_members=0 dl_count=0 match=0 lobby=0 majority_id=None majority_n=0 phase=%s",
                    PHASE_OFF,
                )
                self._write_lane_state(channel.id, phase_result, now)
                continue

            phase_result = self._determine_phase(members)
            self._write_lane_state(channel.id, phase_result, now)


async def setup(bot: commands.Bot):
    await bot.add_cog(LiveMatchMaster(bot))
