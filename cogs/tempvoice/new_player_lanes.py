from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import discord
from discord.ext import commands

log = logging.getLogger("NewPlayerAdaptiveLanes")

TARGET_CATEGORY_ID = 1465839366634209361
ANCHOR_CHANNEL_ID = 1470126503252721845
LANE_BASE_NAME = "🆕Neue Spieler Lane"
EXPAND_THRESHOLD = 6
SYNC_DEBOUNCE_SECONDS = 1.0
STARTUP_SYNC_DELAY_SECONDS = 5.0
LANE_NAME_RE = re.compile(rf"^{re.escape(LANE_BASE_NAME)}\s+(?P<index>[2-9]\d*)$")


@dataclass(frozen=True, slots=True)
class ManagedLaneSnapshot:
    channel_id: int
    current_index: int
    member_count: int


@dataclass(frozen=True, slots=True)
class ManagedLanePlan:
    reassignments: tuple[tuple[int, int], ...]
    delete_ids: tuple[int, ...]
    create_indices: tuple[int, ...]


def lane_name_for_index(index: int) -> str:
    return LANE_BASE_NAME if index <= 1 else f"{LANE_BASE_NAME} {index}"


def parse_lane_index(channel_id: int, name: str) -> int | None:
    if int(channel_id) == ANCHOR_CHANNEL_ID:
        return 1

    match = LANE_NAME_RE.match(str(name).strip())
    if not match:
        return None

    try:
        return int(match.group("index"))
    except (TypeError, ValueError):
        return None


def plan_managed_lanes(
    anchor_member_count: int, extra_snapshots: list[ManagedLaneSnapshot]
) -> ManagedLanePlan:
    occupied = sorted(
        (snapshot for snapshot in extra_snapshots if snapshot.member_count > 0),
        key=lambda snapshot: (snapshot.current_index, snapshot.channel_id),
    )
    empty = sorted(
        (snapshot for snapshot in extra_snapshots if snapshot.member_count <= 0),
        key=lambda snapshot: (snapshot.current_index, snapshot.channel_id),
    )

    highest_occupied_index = 1 if anchor_member_count > 0 else 0
    if occupied:
        highest_occupied_index = 1 + len(occupied)

    highest_full_index = 1 if anchor_member_count >= EXPAND_THRESHOLD else 0
    for desired_index, snapshot in enumerate(occupied, start=2):
        if snapshot.member_count >= EXPAND_THRESHOLD:
            highest_full_index = max(highest_full_index, desired_index)

    desired_total = max(
        1, highest_occupied_index, highest_full_index + 1 if highest_full_index else 1
    )
    extras_to_keep = max(0, desired_total - 1)

    kept_existing = occupied + empty[: max(0, extras_to_keep - len(occupied))]
    reassignments = tuple(
        (snapshot.channel_id, desired_index)
        for desired_index, snapshot in enumerate(kept_existing, start=2)
    )
    delete_ids = tuple(
        snapshot.channel_id for snapshot in empty[max(0, extras_to_keep - len(occupied)) :]
    )
    create_indices = tuple(range(len(kept_existing) + 2, desired_total + 1))

    return ManagedLanePlan(
        reassignments=reassignments,
        delete_ids=delete_ids,
        create_indices=create_indices,
    )


class NewPlayerAdaptiveLanes(commands.Cog):
    """Erweitert den festen Neue-Spieler-Voice adaptiv innerhalb einer Kategorie."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._dirty_guilds: set[int] = set()
        self._sync_tasks: dict[int, asyncio.Task] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._startup_task: asyncio.Task | None = None

    async def cog_load(self) -> None:
        self._startup_task = asyncio.create_task(self._schedule_startup_syncs())

    async def cog_unload(self) -> None:
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
        for task in list(self._sync_tasks.values()):
            if not task.done():
                task.cancel()
        self._sync_tasks.clear()
        self._dirty_guilds.clear()

    async def _schedule_startup_syncs(self) -> None:
        try:
            await self.bot.wait_until_ready()
            await asyncio.sleep(STARTUP_SYNC_DELAY_SECONDS)
            for guild in self.bot.guilds:
                self.schedule_sync(guild.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("startup sync scheduling failed: %r", exc)

    def schedule_sync(self, guild_id: int) -> None:
        if not guild_id:
            return

        guild_id = int(guild_id)
        self._dirty_guilds.add(guild_id)
        task = self._sync_tasks.get(guild_id)
        if task and not task.done():
            return
        self._sync_tasks[guild_id] = asyncio.create_task(self._drain_syncs(guild_id))

    async def _drain_syncs(self, guild_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(SYNC_DEBOUNCE_SECONDS)
                self._dirty_guilds.discard(guild_id)
                await self._sync_guild(guild_id)
                if guild_id not in self._dirty_guilds:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("new player lane sync failed for guild %s: %s", guild_id, exc)
        finally:
            self._dirty_guilds.discard(guild_id)
            self._sync_tasks.pop(guild_id, None)

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(int(guild_id))
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[int(guild_id)] = lock
        return lock

    async def _sync_guild(self, guild_id: int) -> None:
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            return

        anchor = guild.get_channel(ANCHOR_CHANNEL_ID)
        if not isinstance(anchor, discord.VoiceChannel):
            log.warning(
                "new player lane anchor %s not found in guild %s", ANCHOR_CHANNEL_ID, guild.id
            )
            return
        if anchor.category_id != TARGET_CATEGORY_ID:
            log.warning(
                "new player lane anchor %s is not in target category %s (got %s)",
                anchor.id,
                TARGET_CATEGORY_ID,
                anchor.category_id,
            )
            return

        category = anchor.category
        if not isinstance(category, discord.CategoryChannel):
            return

        lock = self._lock_for(guild.id)
        async with lock:
            if anchor.name != LANE_BASE_NAME:
                try:
                    await anchor.edit(
                        name=LANE_BASE_NAME, reason="Neue Spieler Lane: Basisname korrigieren"
                    )
                except discord.HTTPException as exc:
                    log.debug("anchor rename failed for %s: %r", anchor.id, exc)

            snapshots: list[ManagedLaneSnapshot] = []
            for channel in category.voice_channels:
                if channel.id == anchor.id:
                    continue
                lane_index = parse_lane_index(channel.id, channel.name)
                if lane_index is None:
                    continue
                snapshots.append(
                    ManagedLaneSnapshot(
                        channel_id=int(channel.id),
                        current_index=int(lane_index),
                        member_count=len(channel.members),
                    )
                )

            plan = plan_managed_lanes(len(anchor.members), snapshots)
            needs_resync = False

            for lane_id in plan.delete_ids:
                channel = guild.get_channel(int(lane_id))
                if not isinstance(channel, discord.VoiceChannel):
                    continue
                if channel.category_id != TARGET_CATEGORY_ID:
                    continue
                if channel.members:
                    needs_resync = True
                    continue
                try:
                    await channel.delete(reason="Neue Spieler Lane: Leere Lane entfernt")
                except discord.NotFound:
                    continue
                except discord.Forbidden as exc:
                    log.warning("new player lane delete forbidden for %s: %s", channel.id, exc)
                    needs_resync = True
                except discord.HTTPException as exc:
                    log.warning("new player lane delete failed for %s: %s", channel.id, exc)
                    needs_resync = True

            for lane_id, desired_index in plan.reassignments:
                channel = guild.get_channel(int(lane_id))
                if not isinstance(channel, discord.VoiceChannel):
                    needs_resync = True
                    continue
                if channel.category_id != TARGET_CATEGORY_ID:
                    needs_resync = True
                    continue
                if await self._apply_layout(channel, anchor, desired_index):
                    needs_resync = True

            for desired_index in plan.create_indices:
                if await self._create_lane(anchor, desired_index):
                    needs_resync = True

            if needs_resync:
                self.schedule_sync(guild.id)

    async def _apply_layout(
        self,
        channel: discord.VoiceChannel,
        anchor: discord.VoiceChannel,
        desired_index: int,
    ) -> bool:
        desired_name = lane_name_for_index(desired_index)
        desired_position = int(anchor.position) + desired_index - 1
        kwargs: dict[str, object] = {}
        if channel.name != desired_name:
            kwargs["name"] = desired_name
        if int(channel.position) != desired_position:
            kwargs["position"] = desired_position
        if not kwargs:
            return False

        try:
            await channel.edit(**kwargs, reason="Neue Spieler Lane: Reihenfolge synchronisiert")
        except discord.NotFound:
            return True
        except discord.Forbidden as exc:
            log.warning("new player lane edit forbidden for %s: %s", channel.id, exc)
        except discord.HTTPException as exc:
            log.warning("new player lane edit failed for %s: %s", channel.id, exc)
        return False

    async def _create_lane(self, anchor: discord.VoiceChannel, desired_index: int) -> bool:
        desired_name = lane_name_for_index(desired_index)
        try:
            created = await anchor.clone(
                name=desired_name,
                reason="Neue Spieler Lane: Erweiterung erstellt",
            )
        except discord.Forbidden as exc:
            log.warning("new player lane create forbidden at %s: %s", desired_index, exc)
            return False
        except discord.HTTPException as exc:
            log.warning("new player lane create failed at %s: %s", desired_index, exc)
            return False

        desired_position = int(anchor.position) + desired_index - 1
        try:
            if int(created.position) != desired_position:
                await created.edit(
                    position=desired_position,
                    reason="Neue Spieler Lane: Position synchronisiert",
                )
        except discord.HTTPException as exc:
            log.debug("new player lane position update failed for %s: %r", created.id, exc)

        log.info(
            "new player lane created: guild=%s lane=%s name=%s index=%s",
            anchor.guild.id,
            created.id,
            desired_name,
            desired_index,
        )
        return True

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        before_channel = before.channel if before else None
        after_channel = after.channel if after else None
        if before_channel == after_channel:
            return
        if self._is_relevant_voice_channel(before_channel) or self._is_relevant_voice_channel(
            after_channel
        ):
            self.schedule_sync(member.guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        if self._is_relevant_voice_channel(channel):
            self.schedule_sync(channel.guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if self._is_relevant_voice_channel(channel):
            self.schedule_sync(channel.guild.id)

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        if self._is_relevant_voice_channel(before) or self._is_relevant_voice_channel(after):
            self.schedule_sync(after.guild.id)

    def _is_relevant_voice_channel(self, channel: discord.abc.GuildChannel | None) -> bool:
        if not isinstance(channel, discord.VoiceChannel):
            return False
        if channel.id == ANCHOR_CHANNEL_ID:
            return True
        if channel.category_id != TARGET_CATEGORY_ID:
            return False
        return parse_lane_index(channel.id, channel.name) is not None
