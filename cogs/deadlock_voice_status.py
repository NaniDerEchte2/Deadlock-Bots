from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import aiohttp
import aiosqlite
import discord
from discord.ext import commands

from service.db import db_path

log = logging.getLogger("DeadlockVoiceStatus")
trace_log = logging.getLogger("DeadlockVoiceStatus.trace")

TARGET_CATEGORY_IDS: Set[int] = {
    1289721245281292290,
    1412804540994162789,
    1357422957017698478,
}

POLL_INTERVAL_SECONDS = 60
PRESENCE_STALE_SECONDS = 180
RENAME_COOLDOWN_SECONDS = 360
RENAME_REASON = "Deadlock Voice Status Update"
MIN_ACTIVE_PLAYERS = 1

_SUFFIX_REGEX = re.compile(
    r"\s*-\s*\d+/\d+\s+(?:in der Lobby|im Match Min (?:\d+|\d+\+))$",
    re.IGNORECASE,
)

_MATCH_STATUS_REGEX = re.compile(
    r"\{deadlock[:}][^}]*\}.*?\((\d{1,3})[.,]?\s*min\.?\)",
    re.IGNORECASE | re.DOTALL,
)


class DeadlockVoiceStatus(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Optional[aiosqlite.Connection] = None
        self.channel_states: Dict[int, Dict[str, object]] = {}
        self._task: Optional[asyncio.Task[None]] = None
        self.last_observation: Dict[int, Dict[str, Any]] = {}

        trace_env = (os.getenv("DEADLOCK_VS_TRACE") or "1").strip().lower()
        self.trace_enabled = trace_env not in {"0", "false", "no", "off"}
        self.trace_channel_filter: Set[int] = set()
        self.trace_file = Path(os.getenv("DEADLOCK_VS_TRACE_FILE", "logs/deadlock_voice_status.log"))
        channel_filter_raw = os.getenv("DEADLOCK_VS_TRACE_CHANNELS", "")
        for part in re.split(r"[\\s,;]+", channel_filter_raw):
            part = part.strip()
            if part.isdigit():
                self.trace_channel_filter.add(int(part))
        self._trace_handler: Optional[logging.Handler] = None
        self._trace_logger = trace_log
        if self.trace_enabled:
            self._enable_trace_logger()

    async def cog_load(self) -> None:
        await self._ensure_db()
        self._task = asyncio.create_task(self._run_loop())
        log.info("DeadlockVoiceStatus background task started")

    async def cog_unload(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.db:
            await self.db.close()
            self.db = None
        log.info("DeadlockVoiceStatus shut down")

    async def _ensure_db(self) -> None:
        if self.db:
            return
        path = Path(db_path())
        self.db = await aiosqlite.connect(str(path))
        self.db.row_factory = aiosqlite.Row

    def _enable_trace_logger(self) -> None:
        if self._trace_handler:
            return
        try:
            self.trace_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        handler = logging.FileHandler(self.trace_file, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s [TRACE] %(message)s"))
        self._trace_logger.addHandler(handler)
        self._trace_logger.setLevel(logging.DEBUG)
        self._trace_logger.propagate = False  # nur Datei, kein Root/Console
        self._trace_handler = handler
        log.info("DeadlockVoiceStatus trace logging enabled at %s", self.trace_file)

    def _disable_trace_logger(self) -> None:
        if not self._trace_handler:
            self.trace_enabled = False
            return
        try:
            self._trace_logger.removeHandler(self._trace_handler)
            self._trace_handler.close()
        except Exception:
            pass
        finally:
            self._trace_handler = None
        self.trace_enabled = False
        log.info("DeadlockVoiceStatus trace logging disabled")

    @staticmethod
    def _json_fallback(obj: Any) -> Any:
        if isinstance(obj, set):
            return list(obj)
        if isinstance(obj, Path):
            return str(obj)
        return str(obj)

    def _store_trace(self, channel_id: int, payload: Dict[str, Any]) -> None:
        if not payload:
            return
        try:
            self.last_observation[channel_id] = payload
        except Exception:
            pass
        if not self.trace_enabled:
            return
        if self.trace_channel_filter and channel_id not in self.trace_channel_filter:
            return
        try:
            self._trace_logger.debug(json.dumps(payload, ensure_ascii=True, default=self._json_fallback))
        except Exception:
            self._trace_logger.debug("trace %r", payload)

    async def _run_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self._update_all_channels()
            except Exception as exc:  # noqa: BLE001
                log.exception("DeadlockVoiceStatus update failed: %s", exc)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _update_all_channels(self) -> None:
        channels = self._collect_monitored_channels()
        if not channels:
            return

        members_per_channel: Dict[int, List[discord.Member]] = {}
        user_ids: Set[int] = set()
        for channel in channels:
            members = [m for m in channel.members if not m.bot]
            members_per_channel[channel.id] = members
            user_ids.update(member.id for member in members)

        steam_map = await self._fetch_primary_steam_ids(user_ids)
        steam_ids = {sid for sid in steam_map.values() if sid}
        presence_map = await self._fetch_presence_rows(steam_ids)
        now = int(time.time())

        voice_watch_entries: Dict[str, Tuple[str, int, int]] = {}

        for channel in channels:
            members = members_per_channel.get(channel.id, [])
            if members:
                for member in members:
                    steam_id = steam_map.get(member.id)
                    if steam_id:
                        voice_watch_entries[steam_id] = (
                            str(steam_id),
                            channel.guild.id,
                            channel.id,
                        )
            await self._process_channel(channel, members, steam_map, presence_map, now)

        await self._persist_voice_watch_entries(list(voice_watch_entries.values()))

    def _collect_monitored_channels(self) -> List[discord.VoiceChannel]:
        result: List[discord.VoiceChannel] = []
        for guild in self.bot.guilds:
            for channel in guild.voice_channels:
                if channel.category_id in TARGET_CATEGORY_IDS:
                    result.append(channel)
        return result

    async def _fetch_primary_steam_ids(self, user_ids: Iterable[int]) -> Dict[int, str]:
        ids = {int(uid) for uid in user_ids if uid}
        if not ids or not self.db:
            return {}

        placeholders = ",".join("?" for _ in ids)
        query = (
            "SELECT user_id, steam_id, primary_account, verified, updated_at "
            f"FROM steam_links WHERE user_id IN ({placeholders}) "
            "AND steam_id IS NOT NULL AND steam_id != '' "
            "ORDER BY primary_account DESC, verified DESC, updated_at DESC"
        )
        cursor = await self.db.execute(query, tuple(ids))
        rows = await cursor.fetchall()
        await cursor.close()

        mapping: Dict[int, str] = {}
        for row in rows:
            uid = int(row["user_id"])
            if uid not in mapping:
                mapping[uid] = str(row["steam_id"])
        return mapping

    async def _fetch_presence_rows(self, steam_ids: Iterable[str]) -> Dict[str, aiosqlite.Row]:
        ids = {sid for sid in steam_ids if sid}
        if not ids or not self.db:
            return {}

        placeholders = ",".join("?" for _ in ids)
        query = (
            "SELECT steam_id, deadlock_stage, deadlock_minutes, deadlock_localized, "
            "deadlock_updated_at, last_seen_ts, in_deadlock_now, in_match_now_strict, "
            "last_server_id, deadlock_party_hint "
            f"FROM live_player_state WHERE steam_id IN ({placeholders})"
        )
        cursor = await self.db.execute(query, tuple(ids))
        rows = await cursor.fetchall()
        await cursor.close()

        return {str(row["steam_id"]): row for row in rows}

    async def _persist_voice_watch_entries(self, entries: List[Tuple[str, int, int]]) -> None:
        await self._ensure_db()
        if not self.db:
            return
        now_ts = int(time.time())
        if not entries:
            await self.db.execute("DELETE FROM deadlock_voice_watch")
            await self.db.commit()
            return

        try:
            rows = [(steam_id, guild_id, channel_id, now_ts) for (steam_id, guild_id, channel_id) in entries]
            await self.db.executemany(
                """
                INSERT INTO deadlock_voice_watch(steam_id, guild_id, channel_id, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(steam_id) DO UPDATE SET
                  guild_id=excluded.guild_id,
                  channel_id=excluded.channel_id,
                  updated_at=excluded.updated_at
                """,
                rows,
            )
            placeholders = ",".join("?" for _ in entries)
            await self.db.execute(
                f"DELETE FROM deadlock_voice_watch WHERE steam_id NOT IN ({placeholders})",
                [steam_id for (steam_id, _, _) in entries],
            )
            await self.db.commit()
        except Exception as exc:
            log.warning("Failed to persist voice watch entries: %s", exc)

    async def _process_channel(
        self,
        channel: discord.VoiceChannel,
        members: Sequence[discord.Member],
        steam_map: Dict[int, str],
        presence_map: Dict[str, aiosqlite.Row],
        now: int,
    ) -> None:
        base_name, current_suffix = self._split_suffix(channel.name)
        total_members = len(members)
        trace_payload: Dict[str, Any] = {
            "channel_id": channel.id,
            "channel_name": channel.name,
            "base_name": base_name,
            "current_suffix": current_suffix,
            "total_members": total_members,
            "presence": [],
            "decision": {},
        }
        if total_members == 0:
            trace_payload["decision"] = {"reason": "empty_channel"}
            await self._apply_channel_name(channel, base_name, None, None, None, current_suffix, None, None, trace_payload)
            return
        presence_entries: List[Tuple[str, int, Optional[str]]] = []
        for member in members:
            steam_id = steam_map.get(member.id)
            presence = self._evaluate_presence(steam_id, presence_map, now)
            row = presence_map.get(str(steam_id)) if steam_id else None
            trace_entry: Dict[str, Any] = {
                "member_id": member.id,
                "member": member.display_name,
                "steam_id": steam_id,
                "stage": presence[0] if presence else None,
                "minutes": presence[1] if presence else None,
                "server_id": presence[2] if presence else None,
                "raw_stage": self._safe_row_value(row, "deadlock_stage"),
                "raw_minutes": self._safe_row_value(row, "deadlock_minutes"),
                "raw_updated_at": self._safe_row_value(row, "deadlock_updated_at") or self._safe_row_value(row, "last_seen_ts"),
                "raw_localized": self._safe_row_value(row, "deadlock_localized"),
                "raw_party_hint": self._safe_row_value(row, "deadlock_party_hint"),
                "raw_last_server_id": self._safe_row_value(row, "last_server_id"),
            }
            trace_payload["presence"].append(trace_entry)
            if not presence:
                continue
            stage, minutes, server_id = presence
            if stage not in {"lobby", "match"}:
                continue
            presence_entries.append((stage, minutes or 0, server_id))

        if not presence_entries:
            trace_payload["decision"] = {"reason": "no_presence_entries"}
            await self._apply_channel_name(channel, base_name, None, None, None, current_suffix, None, None, trace_payload)
            return
        candidate_stage: Optional[str] = None
        candidate_minutes: List[int] = []
        candidate_count = 0
        chosen_server_id: Optional[str] = None

        lobby_groups: Dict[str, List[int]] = {}
        lobby_unknown: List[int] = []
        match_groups: Dict[str, List[int]] = {}
        match_unknown: List[int] = []

        for stage, minutes, server_id in presence_entries:
            if stage == "match":
                if server_id:
                    match_groups.setdefault(server_id, []).append(minutes)
                else:
                    match_unknown.append(minutes)
            elif stage == "lobby":
                if server_id:
                    lobby_groups.setdefault(server_id, []).append(minutes)
                else:
                    lobby_unknown.append(minutes)

        if match_groups:
            server_id, minute_values = max(match_groups.items(), key=lambda item: len(item[1]))
            if len(minute_values) >= MIN_ACTIVE_PLAYERS:
                candidate_stage = "match"
                candidate_minutes = minute_values
                candidate_count = len(minute_values)
                chosen_server_id = server_id

        if candidate_stage != "match" and len(match_unknown) >= MIN_ACTIVE_PLAYERS:
            candidate_stage = "match"
            candidate_minutes = match_unknown
            candidate_count = len(match_unknown)
            chosen_server_id = None

        if lobby_groups:
            lobby_server_id, lobby_values = max(lobby_groups.items(), key=lambda item: len(item[1]))
            if len(lobby_values) >= MIN_ACTIVE_PLAYERS:
                if candidate_stage != "match" or len(lobby_values) > candidate_count:
                    candidate_stage = "lobby"
                    candidate_minutes = lobby_values
                    candidate_count = len(lobby_values)
                    chosen_server_id = lobby_server_id

        if candidate_stage != "match" and len(lobby_unknown) >= MIN_ACTIVE_PLAYERS:
            if len(lobby_unknown) > candidate_count:
                candidate_stage = "lobby"
                candidate_minutes = lobby_unknown
                candidate_count = len(lobby_unknown)
                chosen_server_id = None

        if not candidate_stage or candidate_count < MIN_ACTIVE_PLAYERS:
            trace_payload["decision"] = {
                "reason": "no_candidate",
                "candidate_stage": candidate_stage,
                "candidate_count": candidate_count,
            }
            await self._apply_channel_name(channel, base_name, None, None, None, current_suffix, None, None, trace_payload)
            return

        player_count_raw = len(candidate_minutes)
        player_count = min(player_count_raw, 6)
        effective_total = min(total_members, 6)
        voice_slots = max(player_count, effective_total)
        trace_payload["decision"] = {
            "reason": "candidate_selected",
            "candidate_stage": candidate_stage,
            "candidate_count": candidate_count,
            "candidate_minutes": candidate_minutes,
            "chosen_server_id": chosen_server_id,
            "player_count_raw": player_count_raw,
            "voice_slots": voice_slots,
            "member_count": total_members,
        }

        if candidate_stage == "lobby":
            suffix = f"{player_count}/{voice_slots} in der Lobby"
            trace_payload["decision"].update(
                {
                    "suffix": suffix,
                    "player_count": player_count,
                    "voice_slots_effective": voice_slots,
                }
            )
            await self._apply_channel_name(
                channel,
                base_name,
                suffix,
                candidate_stage,
                None,
                current_suffix,
                player_count,
                voice_slots,
                chosen_server_id,
                trace_payload,
            )
            return

        if candidate_stage == "match":
            max_minutes = max(candidate_minutes) if candidate_minutes else 0
            bucket = self._bucket_minutes(max_minutes)
            suffix = f"{player_count}/{voice_slots} im Match Min {bucket}"
            trace_payload["decision"].update(
                {
                    "suffix": suffix,
                    "player_count": player_count,
                    "voice_slots_effective": voice_slots,
                    "bucket": bucket,
                    "max_minutes": max_minutes,
                }
            )
            await self._apply_channel_name(
                channel,
                base_name,
                suffix,
                candidate_stage,
                bucket,
                current_suffix,
                player_count,
                voice_slots,
                chosen_server_id,
                trace_payload,
            )
            return

        trace_payload["decision"]["reason"] = "fallback"
        await self._apply_channel_name(channel, base_name, None, None, None, current_suffix, None, None, trace_payload)

    @staticmethod
    def _safe_row_value(row: Optional[aiosqlite.Row], key: str) -> Optional[Any]:
        if row is None:
            return None
        try:
            return row[key]
        except Exception:
            return None

    def _evaluate_presence(
        self,
        steam_id: Optional[str],
        presence_map: Dict[str, aiosqlite.Row],
        now: int,
    ) -> Optional[Tuple[str, Optional[int], Optional[str]]]:
        if not steam_id:
            return None
        row = presence_map.get(str(steam_id))
        if not row:
            return None

        updated_at = row["deadlock_updated_at"] or row["last_seen_ts"]
        if not updated_at:
            return None
        if now - int(updated_at) > PRESENCE_STALE_SECONDS:
            return None

        localized_raw = row["deadlock_localized"] or ""
        localized = localized_raw.strip()
        match_info = _MATCH_STATUS_REGEX.search(localized)
        server_id_raw = row["last_server_id"] or row["deadlock_party_hint"]
        server_id = str(server_id_raw).strip() if server_id_raw else None

        if match_info:
            minutes_raw = row["deadlock_minutes"]
            if minutes_raw is None:
                try:
                    minutes_val = int(match_info.group(1))
                except (ValueError, TypeError):
                    minutes_val = 0
            else:
                try:
                    minutes_val = max(0, int(minutes_raw))
                except (ValueError, TypeError):
                    minutes_val = 0
            return "match", minutes_val, server_id

        if server_id:
            minutes_raw = row["deadlock_minutes"]
            minutes_val: Optional[int]
            if minutes_raw is None:
                minutes_val = None
            else:
                try:
                    minutes_val = max(0, int(minutes_raw))
                except (ValueError, TypeError):
                    minutes_val = None
            return "lobby", minutes_val, server_id

        return None

    async def _apply_channel_name(
        self,
        channel: discord.VoiceChannel,
        base_name: str,
        desired_suffix: Optional[str],
        stage_label: Optional[str],
        bucket_label: Optional[str],
        current_suffix: Optional[str],
        player_count: Optional[int],
        voice_slots: Optional[int],
        server_identifier: Optional[str] = None,
        debug_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        base_clean = base_name.rstrip()
        target_name = base_clean if not desired_suffix else f"{base_clean} - {desired_suffix}"
        trace_data = debug_payload or {}
        rename_info: Dict[str, Any] = {
            "base": base_clean,
            "target_name": target_name,
            "current_name": channel.name,
            "desired_suffix": desired_suffix,
            "current_suffix": current_suffix,
            "stage": stage_label,
            "bucket": bucket_label,
            "player_count": player_count,
            "voice_slots": voice_slots,
            "server_id": server_identifier,
            "attempted": False,
        }

        if channel.name == target_name:
            state = self.channel_states.setdefault(channel.id, {})
            state.update(
                {
                    "base": base_clean,
                    "stage": stage_label,
                    "bucket": bucket_label,
                    "suffix": desired_suffix,
                    "players": player_count,
                    "voice_slots": voice_slots,
                    "server_id": server_identifier,
                }
            )
            rename_info["result"] = "noop_target_matches"
            trace_data["rename"] = rename_info
            self._store_trace(channel.id, trace_data)
            return

        state = self.channel_states.setdefault(channel.id, {})
        last_rename = state.get("last_rename", 0.0)
        elapsed = time.time() - float(last_rename)

        should_rename = desired_suffix != current_suffix or base_clean != channel.name.rstrip()
        rename_info.update(
            {
                "should_rename": should_rename,
                "elapsed_since_last": elapsed,
                "cooldown_remaining": max(0.0, RENAME_COOLDOWN_SECONDS - elapsed),
            }
        )

        if not should_rename:
            state.update(
                {
                    "base": base_clean,
                    "stage": stage_label,
                    "bucket": bucket_label,
                    "suffix": desired_suffix,
                    "players": player_count,
                    "voice_slots": voice_slots,
                    "server_id": server_identifier,
                }
            )
            rename_info["result"] = "noop_same_suffix"
            trace_data["rename"] = rename_info
            self._store_trace(channel.id, trace_data)
            return

        if elapsed < RENAME_COOLDOWN_SECONDS:
            rename_info["result"] = "cooldown"
            trace_data["rename"] = rename_info
            self._store_trace(channel.id, trace_data)
            return

        try:
            await channel.edit(name=target_name, reason=RENAME_REASON)
            await asyncio.sleep(1)  # gentle pacing against rate limits
        except (discord.HTTPException, aiohttp.ClientError, OSError) as exc:
            log.warning("Failed to rename voice channel %s: %s", channel.id, exc)
            state["last_rename"] = time.time()
            rename_info.update({"result": "error", "error": str(exc)})
            trace_data["rename"] = rename_info
            self._store_trace(channel.id, trace_data)
            return

        rename_info["result"] = "renamed"
        rename_info["attempted"] = True
        trace_data["rename"] = rename_info
        self.channel_states[channel.id] = {
            "base": base_clean,
            "stage": stage_label,
            "bucket": bucket_label,
            "suffix": desired_suffix,
            "players": player_count,
            "voice_slots": voice_slots,
            "server_id": server_identifier,
            "last_rename": time.time(),
        }
        self._store_trace(channel.id, trace_data)

    @commands.group(name="dlvs", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    async def dlvs_group(self, ctx: commands.Context) -> None:
        status = "aktiv" if self.trace_enabled else "aus"
        filter_info = (
            "alle Kanaele"
            if not self.trace_channel_filter
            else ", ".join(str(cid) for cid in sorted(self.trace_channel_filter))
        )
        await ctx.send(
            f"DeadlockVoiceStatus Trace: {status} | Filter: {filter_info} | Logfile: {self.trace_file}"
        )

    @dlvs_group.command(name="trace")
    @commands.has_permissions(manage_guild=True)
    async def dlvs_trace(
        self,
        ctx: commands.Context,
        mode: Optional[str] = None,
        channel: Optional[discord.VoiceChannel] = None,
    ) -> None:
        if not mode:
            await self.dlvs_group(ctx)
            return

        mode_l = mode.lower()
        if mode_l in {"on", "an", "start", "enable"}:
            self.trace_enabled = True
            target_channel = channel or (ctx.author.voice.channel if ctx.author.voice else None)
            self.trace_channel_filter = {target_channel.id} if target_channel else set()
            self._enable_trace_logger()
            target_label = f"Channel {target_channel.name} ({target_channel.id})" if target_channel else "alle"
            await ctx.send(f"Trace an ({target_label}), schreibt nach {self.trace_file}")
            return

        if mode_l in {"off", "aus", "stop", "disable"}:
            self.trace_channel_filter.clear()
            self._disable_trace_logger()
            await ctx.send("Trace aus.")
            return

        await ctx.send("Nutze `on/an` oder `off/aus`. Optional kannst du einen Voice-Channel angeben.")

    @dlvs_group.command(name="snapshot")
    @commands.has_permissions(manage_guild=True)
    async def dlvs_snapshot(
        self,
        ctx: commands.Context,
        channel: Optional[discord.VoiceChannel] = None,
    ) -> None:
        target_channel = channel or (ctx.author.voice.channel if ctx.author.voice else None)
        if not target_channel:
            await ctx.send("Bitte gib einen Voice-Channel an oder sei in einem VC.")
            return

        snapshot = self.last_observation.get(target_channel.id)
        if not snapshot:
            await ctx.send("Keine Beobachtung fuer diesen Kanal vorhanden.")
            return

        decision = snapshot.get("decision", {}) or {}
        rename = snapshot.get("rename", {}) or {}
        lines = [
            f"{target_channel.name} ({target_channel.id})",
            f"Entscheidung: {decision.get('candidate_stage')} | Server: {decision.get('chosen_server_id')} | Suffix: {decision.get('suffix')}",
            f"Bucket/Min: {decision.get('bucket') or decision.get('max_minutes')} | Spieler: {decision.get('player_count')} / {decision.get('voice_slots_effective')}",
            f"Rename: {rename.get('result')} | should={rename.get('should_rename')} | cooldown={rename.get('cooldown_remaining')}s",
        ]

        presence_lines = []
        for entry in (snapshot.get("presence") or [])[:10]:
            presence_lines.append(
                f"- {entry.get('member')} ({entry.get('steam_id') or '-'}) -> {entry.get('stage')} "
                f"{entry.get('minutes')}m srv={entry.get('server_id')} raw_stage={entry.get('raw_stage')}"
            )
        if not presence_lines:
            presence_lines.append("- Keine Presence-Daten.")
        if len((snapshot.get("presence") or [])) > len(presence_lines):
            presence_lines.append("... gekuerzt ...")

        await ctx.send("\n".join(lines + presence_lines))

    @staticmethod
    def _split_suffix(name: str) -> Tuple[str, Optional[str]]:
        match = _SUFFIX_REGEX.search(name)
        if not match:
            return name.strip(), None
        base = name[: match.start()].rstrip()
        suffix = name[match.start():].strip()
        return base if base else name.strip(), suffix if suffix else None

    @staticmethod
    def _bucket_minutes(minutes: int) -> str:
        if minutes >= 50:
            return "50+"
        bucket = (minutes // 5) * 5
        return str(bucket)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DeadlockVoiceStatus(bot))
    log.info("DeadlockVoiceStatus cog added")
