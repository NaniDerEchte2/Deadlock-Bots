"""Deadlock rank lookup + auto-sync for Steam friends.

This module is intentionally isolated from the rest of the Steam cogs.
It provides:
- `/steam_rank` lookup command
- periodic rank sync for Discord users whose Steam account is a bot-friend
- automatic Discord rank-role assignment from synced rank data
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import discord
from discord.ext import commands, tasks

from cogs.steam.steam_master import SteamTaskClient, SteamTaskOutcome
from service import db

log = logging.getLogger(__name__)

STEAM_ID64_RE = re.compile(r"^\d{17,20}$")
ACCOUNT_ID_RE = re.compile(r"^\d{1,10}$")
DISCORD_MENTION_RE = re.compile(r"^<@!?(\d+)>$")

MIN_DISCORD_SNOWFLAKE = 10_000_000_000_000_000
AUTO_SYNC_INTERVAL_MINUTES = 20.0

RANK_TIERS: dict[int, str] = {
    0: "Obscurus",
    1: "Initiate",
    2: "Seeker",
    3: "Alchemist",
    4: "Arcanist",
    5: "Ritualist",
    6: "Emissary",
    7: "Archon",
    8: "Oracle",
    9: "Phantom",
    10: "Ascendant",
    11: "Eternus",
}

# Keep this in sync with the existing rank-role setup used by other cogs.
RANK_ROLE_IDS: dict[int, int] = {
    1: 1331457571118387210,   # Initiate
    2: 1331457652877955072,   # Seeker
    3: 1331457699992436829,   # Alchemist
    4: 1331457724848017539,   # Arcanist
    5: 1331457879345070110,   # Ritualist
    6: 1331457898781474836,   # Emissary
    7: 1331457949654319114,   # Archon
    8: 1316966867033653338,   # Oracle
    9: 1331458016356208680,   # Phantom
    10: 1331458049637875785,  # Ascendant
    11: 1331458087349129296,  # Eternus
}
RANK_ROLE_ID_SET = frozenset(RANK_ROLE_IDS.values())
SUBRANK_MIN = 1
SUBRANK_MAX = 6
SUBRANK_ROLE_MAP_TABLE = "deadlock_subrank_roles"
RANK_NAME_TO_VALUE = {
    str(name).casefold(): int(value)
    for value, name in RANK_TIERS.items()
}
_SUBRANK_RANK_NAMES = tuple(
    str(RANK_TIERS[value])
    for value in sorted(RANK_ROLE_IDS.keys())
    if value in RANK_TIERS
)
SUBRANK_ROLE_NAME_RE = re.compile(
    r"^(%s)\s+([%d-%d])$"
    % ("|".join(re.escape(name) for name in _SUBRANK_RANK_NAMES), SUBRANK_MIN, SUBRANK_MAX),
    re.IGNORECASE,
)


@dataclass(slots=True)
class RankLookupTarget:
    payload: Dict[str, Any]
    label: str


@dataclass(slots=True)
class RankSnapshot:
    steam_id: str
    account_id: Optional[int]
    rank_value: Optional[int]
    rank_name: Optional[str]
    subrank: Optional[int]
    badge_level: Optional[int]


@dataclass(slots=True)
class SyncStats:
    friends_total: int = 0
    linked_users: int = 0
    rank_requests: int = 0
    rank_success: int = 0
    rank_failed: int = 0
    rank_rows_written: int = 0
    roles_added: int = 0
    roles_removed: int = 0
    members_not_found: int = 0
    guilds_targeted: int = 0


class DeadlockFriendRank(commands.Cog):
    """Steam/Deadlock rank feature backed by GC profile cards."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tasks = SteamTaskClient(poll_interval=0.5, default_timeout=30.0)
        self._sync_lock = asyncio.Lock()
        self._startup_sync_task: Optional[asyncio.Task[None]] = None
        self._last_stats: Optional[SyncStats] = None

    async def cog_load(self) -> None:
        await self._ensure_subrank_role_table()
        if not self.auto_sync_friend_ranks.is_running():
            self.auto_sync_friend_ranks.start()
        if self._startup_sync_task is None or self._startup_sync_task.done():
            self._startup_sync_task = asyncio.create_task(self._run_startup_sync())

    def cog_unload(self) -> None:
        if self.auto_sync_friend_ranks.is_running():
            self.auto_sync_friend_ranks.cancel()
        if self._startup_sync_task and not self._startup_sync_task.done():
            self._startup_sync_task.cancel()
        self._startup_sync_task = None

    @tasks.loop(minutes=AUTO_SYNC_INTERVAL_MINUTES)
    async def auto_sync_friend_ranks(self) -> None:
        try:
            stats = await self._run_friend_rank_sync(trigger="loop")
            log.info(
                "Deadlock friend-rank sync done",
                extra={
                    "friends_total": stats.friends_total,
                    "linked_users": stats.linked_users,
                    "rank_requests": stats.rank_requests,
                    "rank_success": stats.rank_success,
                    "rank_failed": stats.rank_failed,
                    "rank_rows_written": stats.rank_rows_written,
                    "roles_added": stats.roles_added,
                    "roles_removed": stats.roles_removed,
                },
            )
        except Exception:
            log.exception("Deadlock friend-rank auto sync failed")

    @auto_sync_friend_ranks.before_loop
    async def _before_auto_sync_friend_ranks(self) -> None:
        await self.bot.wait_until_ready()

    async def _run_startup_sync(self) -> None:
        await self.bot.wait_until_ready()
        try:
            await self._run_friend_rank_sync(trigger="startup")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Deadlock friend-rank startup sync failed")

    async def _ensure_subrank_role_table(self) -> None:
        await db.execute_async(
            f"""
            CREATE TABLE IF NOT EXISTS {SUBRANK_ROLE_MAP_TABLE}(
              guild_id INTEGER NOT NULL,
              rank_value INTEGER NOT NULL,
              subrank INTEGER NOT NULL,
              role_id INTEGER NOT NULL,
              role_name TEXT,
              updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
              PRIMARY KEY(guild_id, rank_value, subrank),
              UNIQUE(guild_id, role_id)
            )
            """
        )
        await db.execute_async(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{SUBRANK_ROLE_MAP_TABLE}_guild
            ON {SUBRANK_ROLE_MAP_TABLE}(guild_id)
            """
        )

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _linked_steam_ids_for_discord_user(discord_user_id: int) -> list[str]:
        rows = db.query_all(
            """
            SELECT steam_id
            FROM steam_links
            WHERE user_id = ? AND steam_id IS NOT NULL AND steam_id != ''
            ORDER BY primary_account DESC, verified DESC, updated_at DESC
            """,
            (int(discord_user_id),),
        )
        if not rows:
            return []

        out: list[str] = []
        seen: set[str] = set()
        for row in rows:
            steam_id = str(row["steam_id"]).strip()
            if not steam_id or not STEAM_ID64_RE.fullmatch(steam_id):
                continue
            if steam_id in seen:
                continue
            seen.add(steam_id)
            out.append(steam_id)
        return out

    @staticmethod
    def _linked_steam_id_for_discord_user(discord_user_id: int) -> Optional[str]:
        steam_ids = DeadlockFriendRank._linked_steam_ids_for_discord_user(discord_user_id)
        return steam_ids[0] if steam_ids else None

    @staticmethod
    def _extract_rank_fields(card: Dict[str, Any]) -> tuple[Optional[int], Optional[int], Optional[int], Optional[str]]:
        badge = DeadlockFriendRank._safe_int(card.get("ranked_badge_level"))
        rank_num = DeadlockFriendRank._safe_int(card.get("ranked_rank"))
        subrank = DeadlockFriendRank._safe_int(card.get("ranked_subrank"))

        if badge is not None and badge >= 0:
            # Canonical mapping: badge XY -> tier X, subrank Y (1..6).
            rank_num = badge // 10
            parsed_subrank = badge % 10
            subrank = parsed_subrank if SUBRANK_MIN <= parsed_subrank <= SUBRANK_MAX else None

        if rank_num is not None and rank_num < 0:
            rank_num = None
        if subrank is not None and (subrank < SUBRANK_MIN or subrank > SUBRANK_MAX):
            subrank = None

        rank_name = RANK_TIERS.get(rank_num) if rank_num is not None else None
        return rank_num, subrank, badge, rank_name

    @staticmethod
    def _expected_subrank_role_name(
        rank_value: Optional[int],
        subrank_value: Optional[int],
    ) -> Optional[str]:
        rank_num = DeadlockFriendRank._safe_int(rank_value)
        subrank_num = DeadlockFriendRank._safe_int(subrank_value)
        if rank_num is None or rank_num not in RANK_ROLE_IDS:
            return None
        if subrank_num is None or subrank_num < SUBRANK_MIN or subrank_num > SUBRANK_MAX:
            return None
        rank_name = RANK_TIERS.get(rank_num)
        if not rank_name:
            return None
        return f"{rank_name} {subrank_num}"

    @staticmethod
    def _parse_subrank_role_name(role_name: str) -> Optional[tuple[int, int]]:
        match = SUBRANK_ROLE_NAME_RE.fullmatch(str(role_name or "").strip())
        if not match:
            return None
        rank_value = RANK_NAME_TO_VALUE.get(match.group(1).casefold())
        if rank_value is None:
            return None
        try:
            subrank = int(match.group(2))
        except (TypeError, ValueError):
            return None
        if subrank < SUBRANK_MIN or subrank > SUBRANK_MAX:
            return None
        return rank_value, subrank

    @staticmethod
    def _collect_subrank_role_ids(guild: discord.Guild) -> set[int]:
        out: set[int] = set()
        for role in guild.roles:
            if DeadlockFriendRank._parse_subrank_role_name(role.name):
                out.add(role.id)
        return out

    @staticmethod
    def _find_role_by_name_casefold(guild: discord.Guild, role_name: str) -> Optional[discord.Role]:
        wanted = str(role_name or "").strip().casefold()
        if not wanted:
            return None
        for role in guild.roles:
            if role.name.casefold() == wanted:
                return role
        return None

    async def _load_subrank_role_map_for_guild(self, guild_id: int) -> dict[tuple[int, int], int]:
        rows = await db.query_all_async(
            f"""
            SELECT rank_value, subrank, role_id
            FROM {SUBRANK_ROLE_MAP_TABLE}
            WHERE guild_id = ?
            """,
            (int(guild_id),),
        )
        out: dict[tuple[int, int], int] = {}
        for row in rows or []:
            rank_value = self._safe_int(row["rank_value"])
            subrank = self._safe_int(row["subrank"])
            role_id = self._safe_int(row["role_id"])
            if rank_value is None or rank_value not in RANK_ROLE_IDS:
                continue
            if subrank is None or subrank < SUBRANK_MIN or subrank > SUBRANK_MAX:
                continue
            if role_id is None or role_id <= 0:
                continue
            out[(rank_value, subrank)] = role_id
        return out

    def _resolve_lookup_target(
        self,
        author_id: int,
        raw_target: Optional[str],
    ) -> RankLookupTarget:
        target = (raw_target or "").strip()

        if not target:
            steam_id = self._linked_steam_id_for_discord_user(author_id)
            if not steam_id:
                raise ValueError(
                    "Kein verknüpfter Steam-Account gefunden. Nutze zuerst `/steam link` oder gib eine SteamID an."
                )
            return RankLookupTarget(
                payload={"steam_id": steam_id},
                label=f"dein Account (`{steam_id}`)",
            )

        mention = DISCORD_MENTION_RE.fullmatch(target)
        if mention:
            discord_user_id = int(mention.group(1))
            steam_id = self._linked_steam_id_for_discord_user(discord_user_id)
            if not steam_id:
                raise ValueError("Der erwähnte Discord-User hat keinen verknüpften Steam-Account.")
            return RankLookupTarget(
                payload={"steam_id": steam_id},
                label=f"<@{discord_user_id}> (`{steam_id}`)",
            )

        normalized = target.lower()
        if normalized.startswith("account:"):
            account_text = target.split(":", 1)[1].strip()
            if not ACCOUNT_ID_RE.fullmatch(account_text):
                raise ValueError("`account:` erwartet eine numerische Deadlock Account-ID.")
            account_id = int(account_text)
            if account_id <= 0:
                raise ValueError("Account-ID muss > 0 sein.")
            return RankLookupTarget(
                payload={"account_id": account_id},
                label=f"Account `{account_id}`",
            )

        if STEAM_ID64_RE.fullmatch(target):
            return RankLookupTarget(
                payload={"steam_id": target},
                label=f"Steam `{target}`",
            )

        if ACCOUNT_ID_RE.fullmatch(target):
            account_id = int(target)
            if account_id <= 0:
                raise ValueError("Account-ID muss > 0 sein.")
            return RankLookupTarget(
                payload={"account_id": account_id},
                label=f"Account `{account_id}`",
            )

        raise ValueError(
            "Ungültiges Ziel. Nutze SteamID64 (`17-20` Ziffern), `account:<id>` oder einen Discord-Mention."
        )

    @staticmethod
    def _format_rank_line(card: Dict[str, Any]) -> str:
        rank_num, subrank_num, badge, rank_name = DeadlockFriendRank._extract_rank_fields(card)
        if rank_num is None and badge is None:
            return "Kein Ranked-Badge gefunden."

        tier_label = rank_name or (f"Tier {rank_num}" if rank_num is not None else "Unbekannt")
        if subrank_num is not None:
            return f"{tier_label} · Subrank {subrank_num} (Badge {badge})"
        return f"{tier_label} (Badge {badge})"

    async def _fetch_profile_card(
        self,
        payload: Dict[str, Any],
        *,
        timeout: float = 45.0,
    ) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], SteamTaskOutcome]:
        outcome = await self.tasks.run(
            "GC_GET_PROFILE_CARD",
            payload,
            timeout=timeout,
        )

        if outcome.timed_out or not outcome.ok:
            return None, None, outcome

        result = outcome.result if isinstance(outcome.result, dict) else {}
        data = result.get("data") if isinstance(result, dict) else {}
        if not isinstance(data, dict):
            return None, None, outcome

        card = data.get("card")
        if not isinstance(card, dict):
            return None, data, outcome

        return card, data, outcome

    async def _fetch_bot_friend_ids(self) -> set[str]:
        outcome = await self.tasks.run("AUTH_GET_FRIENDS_LIST", timeout=40.0)
        if outcome.timed_out:
            raise RuntimeError(f"AUTH_GET_FRIENDS_LIST timed out (Task #{outcome.task_id})")
        if not outcome.ok:
            raise RuntimeError(outcome.error or "AUTH_GET_FRIENDS_LIST fehlgeschlagen")

        result = outcome.result if isinstance(outcome.result, dict) else {}
        data = result.get("data") if isinstance(result, dict) else {}
        friends = data.get("friends") if isinstance(data, dict) else None
        if not isinstance(friends, list):
            raise RuntimeError("Ungültiges Antwortformat von AUTH_GET_FRIENDS_LIST")

        ids: set[str] = set()
        for item in friends:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("steam_id64") or "").strip()
            if STEAM_ID64_RE.fullmatch(sid):
                ids.add(sid)
        return ids

    async def _refresh_friend_rows(self, friend_ids: set[str]) -> None:
        if not friend_ids:
            return

        all_real_rows = await db.query_all_async(
            "SELECT DISTINCT steam_id FROM steam_links WHERE user_id != 0"
        )
        known_real_ids = {
            str(row["steam_id"]).strip()
            for row in (all_real_rows or [])
            if row and row["steam_id"]
        }

        update_rows = [(sid,) for sid in sorted(friend_ids)]
        await db.executemany_async(
            """
            UPDATE steam_links
            SET verified = 1, updated_at = CURRENT_TIMESTAMP
            WHERE steam_id = ?
            """,
            update_rows,
        )

        placeholder_rows = [(sid,) for sid in sorted(friend_ids) if sid not in known_real_ids]
        if not placeholder_rows:
            return

        await db.executemany_async(
            """
            INSERT INTO steam_links(user_id, steam_id, name, verified)
            VALUES(0, ?, '', 1)
            ON CONFLICT(user_id, steam_id) DO UPDATE SET
              verified=1,
              updated_at=CURRENT_TIMESTAMP
            """,
            placeholder_rows,
        )

    async def _select_linked_friend_accounts(self, friend_ids: set[str]) -> dict[int, str]:
        if not friend_ids:
            return {}

        rows = await db.query_all_async(
            """
            SELECT user_id, steam_id, primary_account, verified, updated_at
            FROM steam_links
            WHERE user_id >= ?
            ORDER BY user_id ASC, primary_account DESC, verified DESC, updated_at DESC
            """,
            (MIN_DISCORD_SNOWFLAKE,),
        )

        out: dict[int, str] = {}
        for row in rows or []:
            sid = str(row["steam_id"] or "").strip()
            if sid not in friend_ids:
                continue
            uid = self._safe_int(row["user_id"])
            if uid is None or uid <= 0:
                continue
            if uid not in out:
                out[uid] = sid
        return out

    async def _fetch_rank_snapshots(
        self,
        steam_ids: set[str],
        stats: SyncStats,
    ) -> dict[str, RankSnapshot]:
        snapshots: dict[str, RankSnapshot] = {}
        for steam_id in sorted(steam_ids):
            card, data, outcome = await self._fetch_profile_card({"steam_id": steam_id}, timeout=45.0)
            if outcome.timed_out or not outcome.ok:
                stats.rank_failed += 1
                log.warning(
                    "Profile card lookup failed",
                    extra={
                        "steam_id": steam_id,
                        "timed_out": outcome.timed_out,
                        "error": outcome.error,
                    },
                )
                continue

            if not isinstance(card, dict):
                stats.rank_failed += 1
                log.warning("Profile card missing in GC response", extra={"steam_id": steam_id})
                continue

            rank_value, subrank, badge_level, rank_name = self._extract_rank_fields(card)
            account_id = self._safe_int(card.get("account_id"))
            if account_id is None and isinstance(data, dict):
                account_id = self._safe_int(data.get("account_id"))

            snapshots[steam_id] = RankSnapshot(
                steam_id=steam_id,
                account_id=account_id,
                rank_value=rank_value,
                rank_name=rank_name,
                subrank=subrank,
                badge_level=badge_level,
            )
            stats.rank_success += 1

        return snapshots

    async def _persist_rank_snapshots(self, snapshots: dict[str, RankSnapshot]) -> int:
        if not snapshots:
            return 0

        now_ts = int(time.time())
        rows = [
            (
                snap.rank_value,
                snap.rank_name,
                snap.subrank,
                snap.badge_level,
                now_ts,
                snap.steam_id,
            )
            for snap in snapshots.values()
        ]

        await db.executemany_async(
            """
            UPDATE steam_links
            SET
              deadlock_rank = ?,
              deadlock_rank_name = ?,
              deadlock_subrank = ?,
              deadlock_badge_level = ?,
              deadlock_rank_updated_at = ?,
              updated_at = CURRENT_TIMESTAMP
            WHERE steam_id = ?
            """,
            rows,
        )
        return len(rows)

    def _target_guilds_for_rank_roles(self) -> list[discord.Guild]:
        targets: list[discord.Guild] = []
        for guild in self.bot.guilds:
            if any(guild.get_role(role_id) for role_id in RANK_ROLE_ID_SET):
                targets.append(guild)
        return targets

    async def _resolve_member(self, guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException:
            return None

    async def _apply_rank_role_for_member(
        self,
        guild: discord.Guild,
        member: discord.Member,
        target_rank_value: Optional[int],
        target_subrank_value: Optional[int],
        subrank_role_map: dict[tuple[int, int], int],
        stats: SyncStats,
    ) -> None:
        me = guild.me
        if me is None:
            return
        if not me.guild_permissions.manage_roles:
            return

        target_role: Optional[discord.Role] = None
        target_role_id = RANK_ROLE_IDS.get(int(target_rank_value or 0))
        if target_role_id:
            role = guild.get_role(target_role_id)
            if role and role.position < me.top_role.position:
                target_role = role

        target_subrank_role: Optional[discord.Role] = None
        target_rank_num = self._safe_int(target_rank_value)
        target_subrank_num = self._safe_int(target_subrank_value)
        if target_rank_num is not None and target_subrank_num is not None:
            mapped_role_id = subrank_role_map.get((target_rank_num, target_subrank_num))
            if mapped_role_id:
                role = guild.get_role(mapped_role_id)
                if role and role.position < me.top_role.position:
                    target_subrank_role = role

        if target_subrank_role is None:
            target_subrank_role_name = self._expected_subrank_role_name(
                target_rank_value,
                target_subrank_value,
            )
            if target_subrank_role_name:
                role = self._find_role_by_name_casefold(guild, target_subrank_role_name)
                if role and role.position < me.top_role.position:
                    target_subrank_role = role

        mapped_subrank_role_ids = {
            role_id
            for role_id in subrank_role_map.values()
            if role_id > 0
        }
        subrank_role_id_set = mapped_subrank_role_ids | self._collect_subrank_role_ids(guild)
        target_role_ids = {
            role.id
            for role in (target_role, target_subrank_role)
            if role is not None
        }

        removable_roles = [
            role
            for role in member.roles
            if role.position < me.top_role.position
            and (role.id in RANK_ROLE_ID_SET or role.id in subrank_role_id_set)
        ]
        roles_to_remove = [
            role
            for role in removable_roles
            if role.id not in target_role_ids
        ]

        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Deadlock rank auto-sync")
                stats.roles_removed += len(roles_to_remove)
        except discord.Forbidden:
            log.warning("Missing permissions to remove rank roles", extra={"guild_id": guild.id, "user_id": member.id})
            return
        except discord.HTTPException as exc:
            log.warning(
                "HTTP error while removing rank roles",
                extra={"guild_id": guild.id, "user_id": member.id, "error": str(exc)},
            )
            return

        roles_to_add: list[discord.Role] = []
        if target_role is not None and target_role not in member.roles:
            roles_to_add.append(target_role)
        if target_subrank_role is not None and target_subrank_role not in member.roles:
            roles_to_add.append(target_subrank_role)

        if not roles_to_add:
            return

        try:
            await member.add_roles(*roles_to_add, reason="Deadlock rank auto-sync")
            stats.roles_added += len(roles_to_add)
        except discord.Forbidden:
            log.warning("Missing permissions to add rank role", extra={"guild_id": guild.id, "user_id": member.id})
        except discord.HTTPException as exc:
            log.warning(
                "HTTP error while adding rank role",
                extra={"guild_id": guild.id, "user_id": member.id, "error": str(exc)},
            )

    async def _sync_rank_roles(
        self,
        user_to_steam: dict[int, str],
        snapshots: dict[str, RankSnapshot],
        stats: SyncStats,
    ) -> None:
        target_guilds = self._target_guilds_for_rank_roles()
        stats.guilds_targeted = len(target_guilds)
        if not target_guilds:
            return

        subrank_role_maps: dict[int, dict[tuple[int, int], int]] = {}
        for guild in target_guilds:
            subrank_role_maps[guild.id] = await self._load_subrank_role_map_for_guild(guild.id)

        for user_id, steam_id in user_to_steam.items():
            snapshot = snapshots.get(steam_id)
            if not snapshot:
                continue

            for guild in target_guilds:
                member = await self._resolve_member(guild, user_id)
                if member is None:
                    stats.members_not_found += 1
                    continue
                await self._apply_rank_role_for_member(
                    guild,
                    member,
                    snapshot.rank_value,
                    snapshot.subrank,
                    subrank_role_maps.get(guild.id, {}),
                    stats,
                )

    async def _run_friend_rank_sync(self, *, trigger: str) -> SyncStats:
        del trigger
        async with self._sync_lock:
            stats = SyncStats()
            friend_ids = await self._fetch_bot_friend_ids()
            stats.friends_total = len(friend_ids)

            await self._refresh_friend_rows(friend_ids)
            user_to_steam = await self._select_linked_friend_accounts(friend_ids)
            stats.linked_users = len(user_to_steam)

            steam_ids = set(user_to_steam.values())
            stats.rank_requests = len(steam_ids)
            snapshots = await self._fetch_rank_snapshots(steam_ids, stats)
            stats.rank_rows_written = await self._persist_rank_snapshots(snapshots)

            await self._sync_rank_roles(user_to_steam, snapshots, stats)
            self._last_stats = stats
            return stats

    @staticmethod
    async def _defer_if_interaction(ctx: commands.Context) -> None:
        interaction = getattr(ctx, "interaction", None)
        if interaction is None:
            return
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except discord.HTTPException:
            pass

    @staticmethod
    async def _send_ctx_response(ctx: commands.Context, content: str) -> None:
        interaction = getattr(ctx, "interaction", None)
        if interaction is not None:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(content)
                else:
                    await interaction.response.send_message(content)
                return
            except discord.HTTPException:
                pass
        await ctx.reply(content, mention_author=False)

    @staticmethod
    def _render_sync_stats(stats: SyncStats) -> str:
        return "\n".join(
            [
                "✅ **Steam Friend Rank Sync abgeschlossen**",
                f"- Freunde vom Bot: `{stats.friends_total}`",
                f"- Verknüpfte Discord-User: `{stats.linked_users}`",
                f"- Rank-Abfragen: `{stats.rank_requests}`",
                f"- Rank-Erfolge: `{stats.rank_success}`",
                f"- Rank-Fehler: `{stats.rank_failed}`",
                f"- DB-Updates: `{stats.rank_rows_written}`",
                f"- Rollen hinzugefügt: `{stats.roles_added}`",
                f"- Rollen entfernt: `{stats.roles_removed}`",
                f"- User nicht im Guild-Cache/fetchbar: `{stats.members_not_found}`",
                f"- Ziel-Guilds: `{stats.guilds_targeted}`",
            ]
        )

    async def _run_manual_sync_command(self, ctx: commands.Context, *, trigger_label: str) -> None:
        await self._defer_if_interaction(ctx)
        async with ctx.typing():
            try:
                stats = await self._run_friend_rank_sync(trigger=f"{trigger_label}:{ctx.author.id}")
            except Exception as exc:
                log.exception("Manual steam rank sync failed")
                await self._send_ctx_response(ctx, f"❌ Steam-Rank-Sync fehlgeschlagen: {exc}")
                return
        await self._send_ctx_response(ctx, self._render_sync_stats(stats))

    @commands.hybrid_command(
        name="steam_rank_sync",
        description="Synchronisiert Friend-Ranks in steam_links und weist Rank-Rollen automatisch zu.",
    )
    @commands.has_permissions(administrator=True)
    async def cmd_steam_rank_sync(self, ctx: commands.Context) -> None:
        """Manual one-shot sync for bot-friend ranks + roles."""
        await self._run_manual_sync_command(ctx, trigger_label="manual")

    @commands.hybrid_command(
        name="subrank_sync",
        description="Startet sofort den Deadlock Subrank-Auto-Sync inkl. Rollenvergabe.",
    )
    @commands.has_permissions(administrator=True)
    async def cmd_subrank_sync(self, ctx: commands.Context) -> None:
        """Manual one-shot sync command focused on subrank role updates."""
        await self._run_manual_sync_command(ctx, trigger_label="subrank_manual")

    @commands.hybrid_command(
        name="steam_rank",
        description="Fragt den Deadlock-Rang über die Steam PlayerCard (GC) ab.",
    )
    async def cmd_steam_rank(self, ctx: commands.Context, *, target: Optional[str] = None) -> None:
        """Lookup Deadlock rank for the caller (default) or a specific Steam/account target."""

        await self._defer_if_interaction(ctx)
        try:
            lookup = self._resolve_lookup_target(
                author_id=int(ctx.author.id),
                raw_target=target,
            )
        except ValueError as exc:
            await self._send_ctx_response(ctx, f"❌ {exc}")
            return

        await self._reply_rank_for_lookup(ctx, lookup)

    async def _reply_rank_for_lookup(self, ctx: commands.Context, lookup: RankLookupTarget) -> None:
        await self._defer_if_interaction(ctx)
        async with ctx.typing():
            card, data, outcome = await self._fetch_profile_card(lookup.payload, timeout=45.0)

        if outcome.timed_out:
            await self._send_ctx_response(
                ctx,
                f"⏳ Rank-Abfrage für {lookup.label} läuft noch (Task #{outcome.task_id}).",
            )
            return

        if not outcome.ok:
            await self._send_ctx_response(
                ctx,
                f"❌ Rank-Abfrage fehlgeschlagen: {outcome.error or 'Unbekannter Fehler'}",
            )
            return

        if not isinstance(data, dict):
            await self._send_ctx_response(ctx, "❌ Ungültiges Antwortformat vom Steam-Bridge-Task.")
            return

        if not isinstance(card, dict):
            await self._send_ctx_response(
                ctx,
                "❌ PlayerCard konnte nicht gelesen werden (keine `card` im Ergebnis).",
            )
            return

        account_id = card.get("account_id") or data.get("account_id")
        steam_id = data.get("steam_id64")
        rank_line = self._format_rank_line(card)

        lines = [
            f"🎯 **Deadlock Rank** für {lookup.label}",
            f"- Rank: {rank_line}",
            f"- Account-ID: `{account_id}`" if account_id is not None else "- Account-ID: `-`",
        ]
        if steam_id:
            lines.append(f"- SteamID64: `{steam_id}`")

        await self._send_ctx_response(ctx, "\n".join(lines))

    @commands.hybrid_command(
        name="checkrank",
        description="Prüft den Deadlock-Rang eines Discord-Users per @Mention.",
    )
    async def cmd_checkrank(
        self,
        ctx: commands.Context,
        user: Optional[discord.Member] = None,
    ) -> None:
        """Rank lookup by Discord user mention (defaults to caller)."""

        await self._defer_if_interaction(ctx)
        target = user or ctx.author
        steam_ids = self._linked_steam_ids_for_discord_user(int(target.id))
        if not steam_ids:
            await self._send_ctx_response(
                ctx,
                f"❌ Für {getattr(target, 'mention', f'`{target}`')} ist kein Steam-Link gespeichert.",
            )
            return

        mention = getattr(target, "mention", f"`{target}`")
        if len(steam_ids) == 1:
            steam_id = steam_ids[0]
            lookup = RankLookupTarget(
                payload={"steam_id": steam_id},
                label=f"{mention} (`{steam_id}`)",
            )
            await self._reply_rank_for_lookup(ctx, lookup)
            return

        max_accounts = 5
        lines = [
            f"🎯 **Deadlock Rank** für {mention}",
            f"- Gefundene Steam-Links: `{len(steam_ids)}`",
        ]

        for index, steam_id in enumerate(steam_ids[:max_accounts], start=1):
            card, data, outcome = await self._fetch_profile_card({"steam_id": steam_id}, timeout=45.0)

            if outcome.timed_out:
                lines.append(f"- {index}. `{steam_id}`: ⏳ Timeout (Task #{outcome.task_id})")
                continue

            if not outcome.ok:
                lines.append(
                    f"- {index}. `{steam_id}`: ❌ {outcome.error or 'Unbekannter Fehler'}"
                )
                continue

            if not isinstance(card, dict):
                lines.append(f"- {index}. `{steam_id}`: ❌ Keine PlayerCard im Ergebnis")
                continue

            account_id = card.get("account_id")
            if account_id is None and isinstance(data, dict):
                account_id = data.get("account_id")

            rank_line = self._format_rank_line(card)
            if account_id is not None:
                lines.append(f"- {index}. `{steam_id}`: {rank_line} · Account `{account_id}`")
            else:
                lines.append(f"- {index}. `{steam_id}`: {rank_line}")

        remaining = len(steam_ids) - max_accounts
        if remaining > 0:
            lines.append(f"- … plus `{remaining}` weitere verknüpfte Accounts")

        lines.append("- Tipp: Nutze `/steam_rank <steamid64>` für eine gezielte Einzelabfrage.")
        await self._send_ctx_response(ctx, "\n".join(lines))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DeadlockFriendRank(bot))
