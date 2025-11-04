from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import aiosqlite
import discord
from discord.ext import commands

from service.db import db_path

log = logging.getLogger("DeadlockVoiceStatus")

TARGET_CATEGORY_IDS: Set[int] = {
    1289721245281292290,
    1412804540994162789,
    1357422957017698478,
}

POLL_INTERVAL_SECONDS = 60
PRESENCE_STALE_SECONDS = 180
RENAME_COOLDOWN_SECONDS = 620
RENAME_REASON = "Deadlock Voice Status Update"
MIN_ACTIVE_PLAYERS = 1

_SUFFIX_REGEX = re.compile(
    r"\s*-\s*\d+/\d+\s+(?:in der Lobby|im Match Min (?:\d+|\d+\+))$",
    re.IGNORECASE,
)


class DeadlockVoiceStatus(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Optional[aiosqlite.Connection] = None
        self.channel_states: Dict[int, Dict[str, object]] = {}
        self._task: Optional[asyncio.Task[None]] = None

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
            "deadlock_updated_at, last_seen_ts, in_deadlock_now, in_match_now_strict "
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

        if total_members == 0:
            await self._apply_channel_name(channel, base_name, None, None, None, current_suffix, None, None)
            return

        stage_minutes: Dict[str, List[int]] = {}
        presence_detected = False
        for member in members:
            steam_id = steam_map.get(member.id)
            presence = self._evaluate_presence(steam_id, presence_map, now)
            if not presence:
                continue
            stage, minutes = presence
            if stage not in {"lobby", "match"}:
                continue
            presence_detected = True
            stage_minutes.setdefault(stage, []).append(minutes or 0)

        if not presence_detected:
            await self._apply_channel_name(channel, base_name, None, None, None, current_suffix, None, None)
            return

        candidate_stage: Optional[str] = None
        candidate_minutes: List[int] = []
        candidate_count = 0

        for stage, values in stage_minutes.items():
            size = len(values)
            if size < MIN_ACTIVE_PLAYERS:
                continue
            prefer = False
            if size > candidate_count:
                prefer = True
            elif size == candidate_count:
                if candidate_stage != "match" and stage == "match":
                    prefer = True
            if prefer:
                candidate_stage = stage
                candidate_minutes = values
                candidate_count = size

        if not candidate_stage and stage_minutes:
            stage, values = max(stage_minutes.items(), key=lambda item: len(item[1]))
            candidate_stage = stage
            candidate_minutes = values
            candidate_count = len(values)

        if not candidate_stage or candidate_count < MIN_ACTIVE_PLAYERS:
            await self._apply_channel_name(channel, base_name, None, None, None, current_suffix, None, None)
            return

        player_count_raw = len(candidate_minutes)
        player_count = min(player_count_raw, 6)
        effective_total = min(total_members, 6)
        voice_slots = max(player_count, effective_total)

        if candidate_stage == "lobby":
            suffix = f"{player_count}/{voice_slots} in der Lobby"
            await self._apply_channel_name(
                channel, base_name, suffix, candidate_stage, None, current_suffix, player_count, voice_slots
            )
            return

        if candidate_stage == "match":
            max_minutes = max(candidate_minutes) if candidate_minutes else 0
            bucket = self._bucket_minutes(max_minutes)
            suffix = f"{player_count}/{voice_slots} im Match Min {bucket}"
            await self._apply_channel_name(
                channel, base_name, suffix, candidate_stage, bucket, current_suffix, player_count, voice_slots
            )
            return

        await self._apply_channel_name(channel, base_name, None, None, None, current_suffix, None, None)

    def _evaluate_presence(
        self,
        steam_id: Optional[str],
        presence_map: Dict[str, aiosqlite.Row],
        now: int,
    ) -> Optional[Tuple[str, Optional[int]]]:
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

        stage = (row["deadlock_stage"] or "").lower()
        in_deadlock = bool(row["in_deadlock_now"])
        in_match = bool(row["in_match_now_strict"])

        if stage in {"", "offline", "unknown"}:
            if in_match:
                stage = "match"
            elif in_deadlock:
                stage = "lobby"
            else:
                return None

        if stage not in {"lobby", "match"}:
            return None

        minutes_raw = row["deadlock_minutes"]
        minutes: Optional[int]
        if minutes_raw is None:
            minutes = 0 if stage == "match" else None
        else:
            minutes = max(0, int(minutes_raw))
        return stage, minutes

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
    ) -> None:
        base_clean = base_name.rstrip()
        target_name = base_clean if not desired_suffix else f"{base_clean} - {desired_suffix}"

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
                }
            )
            return

        state = self.channel_states.setdefault(channel.id, {})
        last_stage = state.get("stage")
        last_bucket = state.get("bucket")
        last_suffix = state.get("suffix")
        last_base = state.get("base")
        last_rename = state.get("last_rename", 0.0)
        elapsed = time.time() - float(last_rename)

        should_rename = desired_suffix != current_suffix or base_clean != channel.name.rstrip()

        if not should_rename:
            state.update(
                {
                    "base": base_clean,
                    "stage": stage_label,
                    "bucket": bucket_label,
                    "suffix": desired_suffix,
                    "players": player_count,
                    "voice_slots": voice_slots,
                }
            )
            return

        allow_rename = False
        if stage_label != last_stage:
            allow_rename = True
        elif last_base and last_base != base_clean:
            allow_rename = True
        elif stage_label == "match" and bucket_label != last_bucket:
            allow_rename = elapsed >= RENAME_COOLDOWN_SECONDS
        elif desired_suffix is None and last_suffix is not None:
            allow_rename = elapsed >= RENAME_COOLDOWN_SECONDS
        else:
            allow_rename = elapsed >= RENAME_COOLDOWN_SECONDS

        if not allow_rename:
            return

        try:
            await channel.edit(name=target_name, reason=RENAME_REASON)
            await asyncio.sleep(1)  # gentle pacing against rate limits
        except discord.HTTPException as exc:
            log.warning("Failed to rename voice channel %s: %s", channel.id, exc)
            return

        self.channel_states[channel.id] = {
            "base": base_clean,
            "stage": stage_label,
            "bucket": bucket_label,
            "suffix": desired_suffix,
            "players": player_count,
            "voice_slots": voice_slots,
            "last_rename": time.time(),
        }

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
        bucket = (minutes // 10) * 10
        return str(bucket)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DeadlockVoiceStatus(bot))
    log.info("DeadlockVoiceStatus cog added")
