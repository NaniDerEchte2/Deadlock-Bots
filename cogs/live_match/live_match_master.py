import asyncio
import io
import json
import logging
import os
import time
from collections import Counter
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands, tasks

from cogs.steam.live_presence_service import (
    SteamPresenceInfo,
    SteamPresenceService,
)
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

STEAM_API_KEY = os.getenv("STEAM_API_KEY", "").strip()
DEADLOCK_APP_ID = os.getenv("DEADLOCK_APP_ID", "1422450").strip()


def _fmt_suffix(majority_n: int, voice_n: int, label: str, dl_count: int) -> str:
    voice_n = max(0, int(voice_n))
    majority_n = max(0, min(int(majority_n), voice_n))
    dl_count = max(0, min(int(dl_count), voice_n))
    suffix = f"• {majority_n}/{voice_n} {label}"
    if dl_count:
        suffix = f"{suffix} ({dl_count} DL)"
    return suffix.strip()
def _ensure_schema() -> None:
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
        self._steam = SteamPresenceService(
            steam_api_key=STEAM_API_KEY,
            deadlock_app_id=DEADLOCK_APP_ID,
        )
        self._links_cache: Dict[int, List[str]] = {}
        self._presence_cache: Dict[str, SteamPresenceInfo] = {}
        self._friend_snapshot_cache: Dict[str, Dict[str, Any]] = {}
        # Neu: Merker des letzten geschriebenen Zustands pro Channel
        self._last_state: Dict[int, Dict[str, Any]] = {}
        self._last_debug_payload: Dict[int, str] = {}
        self._last_presence_snapshot: Optional[str] = None

    async def cog_load(self):
        db.connect()
        self._steam.ensure_schema()
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

    def get_presence_for_discord_user(self, discord_id: int) -> Optional[SteamPresenceInfo]:
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
            friend_snapshot: Optional[Dict[str, Any]] = None
            for sid in links:
                snapshot = self._friend_snapshot_cache.get(str(sid))
                if snapshot:
                    friend_snapshot = snapshot
                    break
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
                    "presence_display": presence.display if presence else None,
                    "presence_status": presence.status if presence else None,
                    "presence_status_text": presence.status_text if presence else None,
                    "presence_updated_at": presence.updated_at if presence else None,
                    "presence_raw": dict(presence.raw) if presence else None,
                    "summary_raw": (
                        dict(presence.summary_raw)
                        if presence and isinstance(presence.summary_raw, dict)
                        else (presence.summary_raw if presence else None)
                    ),
                    "friend_snapshot_raw": (
                        dict(presence.friend_snapshot_raw)
                        if presence and isinstance(presence.friend_snapshot_raw, dict)
                        else (
                            dict(friend_snapshot)
                            if isinstance(friend_snapshot, dict)
                            else friend_snapshot
                        )
                    ),
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

        def _json_preview(data: Any) -> Optional[str]:
            if not data:
                return None
            try:
                text = json.dumps(data, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                text = str(data)
            if len(text) > 500:
                text = f"{text[:497]}…"
            return text

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
            presence_preview = _json_preview(member.get("presence_raw"))
            if presence_preview:
                member_lines.append(f"    rp={presence_preview}")
            summary_preview = _json_preview(member.get("summary_raw"))
            if summary_preview:
                member_lines.append(f"    summary={summary_preview}")
            friend_preview = _json_preview(member.get("friend_snapshot_raw"))
            if friend_preview:
                member_lines.append(f"    friend={friend_preview}")

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

    @staticmethod
    def _safe_json_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(k): LiveMatchMaster._safe_json_value(v)
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [LiveMatchMaster._safe_json_value(v) for v in value]
        try:
            return str(value)
        except Exception:  # pragma: no cover - defensive
            return repr(value)

    def _serialize_presence_info(self, info: SteamPresenceInfo) -> Dict[str, Any]:
        return {
            "steam_id": info.steam_id,
            "updated_at": info.updated_at,
            "display": info.display,
            "status": info.status,
            "status_text": info.status_text,
            "player_group": info.player_group,
            "player_group_size": info.player_group_size,
            "connect": info.connect,
            "mode": info.mode,
            "map_name": info.map_name,
            "party_size": info.party_size,
            "phase_hint": info.phase_hint,
            "is_match": info.is_match,
            "is_lobby": info.is_lobby,
            "is_deadlock": info.is_deadlock,
            "raw": self._safe_json_value(info.raw),
            "summary_raw": self._safe_json_value(info.summary_raw),
            "friend_snapshot_raw": self._safe_json_value(info.friend_snapshot_raw),
        }

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

    async def _send_presence_snapshot(self, now: int) -> None:
        debug_channel = self.bot.get_channel(DEBUG_CHANNEL_ID)
        if not isinstance(debug_channel, discord.TextChannel):
            return
        snapshot_items = {
            steam_id: self._serialize_presence_info(info)
            for steam_id, info in sorted(self._presence_cache.items())
        }
        try:
            payload = json.dumps(
                snapshot_items,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            log.warning("Snapshot Serialisierung fehlgeschlagen: %s", exc)
            return
        if not snapshot_items and self._last_presence_snapshot:
            self._last_presence_snapshot = None
        if payload == self._last_presence_snapshot:
            return
        data = payload.encode("utf-8")
        if len(data) > 7_500_000:
            log.warning(
                "Snapshot zu groß (%d Bytes) – Ausgabe übersprungen", len(data)
            )
            return
        file_name = f"steam_presence_{now}.json"
        try:
            await debug_channel.send(
                content=(
                    "Steam Friend Presence Snapshot ({count} Einträge) — {ts} UTC".format(
                        count=len(snapshot_items),
                        ts=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)),
                    )
                ),
                file=discord.File(io.BytesIO(data), filename=file_name),
            )
            self._last_presence_snapshot = payload
        except discord.HTTPException as exc:  # pragma: no cover - defensive
            log.debug("Snapshot-Ausgabe fehlgeschlagen: %s", exc)

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
        self._links_cache = self._steam.load_links(member.id for member in all_members)
        all_steam_ids = [sid for ids in self._links_cache.values() for sid in ids]
        # Rich Presence nur laden, wenn überhaupt Links da sind
        if all_steam_ids:
            self._presence_cache = self._steam.load_presence_map(
                all_steam_ids,
                now,
                freshness_sec=PRESENCE_FRESH_SEC,
            )
        else:
            self._presence_cache = {}

        summary_ids: List[str] = []
        for sid in all_steam_ids:
            info = self._presence_cache.get(str(sid)) if sid else None
            if not info or not info.is_deadlock or not info.display:
                summary_ids.append(str(sid))

        if summary_ids:
            summaries = await self._steam.fetch_player_summaries(summary_ids)
            self._steam.merge_with_summaries(self._presence_cache, summaries, now=now)

        friend_snapshots = self._steam.load_friend_snapshots(all_steam_ids)
        self._friend_snapshot_cache = friend_snapshots
        self._steam.attach_friend_snapshots(self._presence_cache, friend_snapshots)

        await self._send_presence_snapshot(now)

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
