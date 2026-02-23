from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable

import discord
from discord.ext import commands, tasks

from cogs import privacy_core as privacy
from cogs.steam.steam_master import SteamTaskClient
from service import db
from service.config import settings

log = logging.getLogger(__name__)


class SteamLeaveCleanup(commands.Cog):
    """Bereinigt Steam-Links und Freundschaften, wenn User den Server verlassen."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tasks = SteamTaskClient(poll_interval=0.5, default_timeout=20.0)
        self._recent: dict[int, float] = {}
        self._last_relationship_probe: float = 0.0
        # Reconcile safety net: confirmation via member_events tracker + optional grace
        self._reconcile_suspects: dict[int, float] = {}
        self._reconcile_grace_seconds: float = 6 * 60 * 60  # 6h

    async def cog_load(self) -> None:
        self.reconcile_orphaned_links.start()

    def cog_unload(self) -> None:
        self.reconcile_orphaned_links.cancel()

    def _is_recent(self, user_id: int, window: float = 20.0) -> bool:
        now = time.monotonic()
        last = self._recent.get(user_id)
        self._recent[user_id] = now
        # keep cache small
        if len(self._recent) > 500:
            cutoff = now - window
            self._recent = {uid: ts for uid, ts in self._recent.items() if ts >= cutoff}
        return bool(last and now - last < window)

    def _confirmed_left(self, user_id: int, guild_id: int) -> bool:
        """
        Verifiziert einen Leave über den vorhandenen member_events Tracker.
        Nur wenn das letzte Event ein leave/ban war, wird aufgeräumt.
        """
        row = db.query_one(
            """
            SELECT event_type
              FROM member_events
             WHERE user_id = ? AND guild_id = ?
             ORDER BY timestamp DESC
             LIMIT 1
            """,
            (int(user_id), int(guild_id)),
        )
        if not row:
            return False
        event = str(row["event_type"] or "").lower()
        return event in ("leave", "ban")

    async def _handle_leave_event(
        self,
        user_id: int,
        guild_id: int | None,
        display_name: str | None,
        reason: str,
    ) -> None:
        if self._is_recent(user_id):
            return
        await self._cleanup_user(user_id, guild_id, display_name, reason)

    async def _cleanup_user(
        self,
        user_id: int,
        guild_id: int | None,
        display_name: str | None,
        reason: str,
    ) -> None:
        try:
            if privacy.is_opted_out(int(user_id)):
                return

            rows = db.query_all(
                """
                SELECT user_id, steam_id, name, verified, primary_account,
                       deadlock_rank, deadlock_rank_name, deadlock_subrank,
                       deadlock_badge_level, deadlock_rank_updated_at,
                       created_at, updated_at
                  FROM steam_links
                 WHERE user_id=?
                """,
                (int(user_id),),
            )
            if not rows:
                return

            steam_ids = [str(r["steam_id"]).strip() for r in rows if r and r["steam_id"]]
            if not steam_ids:
                return

            rel_map = await self._fetch_relationships(steam_ids)

            left_at = int(time.time())
            async with db.transaction() as conn:
                for r in rows:
                    steam_id = str(r["steam_id"]).strip()
                    if not steam_id:
                        continue
                    conn.execute(
                        """
                        INSERT INTO steam_links_archive(
                            user_id, steam_id, name, verified, primary_account,
                            deadlock_rank, deadlock_rank_name, deadlock_subrank, deadlock_badge_level,
                            deadlock_rank_updated_at, created_at, updated_at,
                            left_at, guild_id, leave_reason, display_name
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(user_id, steam_id) DO UPDATE SET
                            name=excluded.name,
                            -- keep relationship snapshot for debugging cleanup issues
                            leave_reason=excluded.leave_reason,
                            verified=excluded.verified,
                            primary_account=excluded.primary_account,
                            deadlock_rank=excluded.deadlock_rank,
                            deadlock_rank_name=excluded.deadlock_rank_name,
                            deadlock_subrank=excluded.deadlock_subrank,
                            deadlock_badge_level=excluded.deadlock_badge_level,
                            deadlock_rank_updated_at=excluded.deadlock_rank_updated_at,
                            created_at=COALESCE(steam_links_archive.created_at, excluded.created_at),
                            updated_at=excluded.updated_at,
                            left_at=excluded.left_at,
                            guild_id=excluded.guild_id,
                            display_name=COALESCE(excluded.display_name, steam_links_archive.display_name)
                        """,
                        (
                            int(user_id),
                            steam_id,
                            r["name"],
                            r["verified"],
                            r["primary_account"],
                            r["deadlock_rank"],
                            r["deadlock_rank_name"],
                            r["deadlock_subrank"],
                            r["deadlock_badge_level"],
                            r["deadlock_rank_updated_at"],
                            r["created_at"],
                            r["updated_at"],
                            left_at,
                            int(guild_id) if guild_id else None,
                            f"{reason} | rel={rel_map.get(steam_id)}",
                            display_name,
                        ),
                    )

                conn.execute("DELETE FROM steam_links WHERE user_id=?", (int(user_id),))
                conn.executemany(
                    "DELETE FROM steam_friend_requests WHERE steam_id=?",
                    [(sid,) for sid in steam_ids],
                )

            await self._unfriend_all(steam_ids)
            log.info(
                "Steam cleanup after leave: user=%s, steam_ids=%d, reason=%s",
                user_id,
                len(steam_ids),
                reason,
            )
        except Exception as exc:  # noqa: BLE001 - we want full context in logs
            log.warning("Steam cleanup failed for user %s: %s", user_id, exc, exc_info=True)

    async def _unfriend_all(self, steam_ids: Iterable[str]) -> None:
        unique_ids = sorted({sid for sid in steam_ids if sid})
        if not unique_ids:
            return

        futures = [
            self.tasks.run("AUTH_REMOVE_FRIEND", {"steam_id": sid}, timeout=25.0)
            for sid in unique_ids
        ]

        results = await asyncio.gather(*futures, return_exceptions=True)
        failed = 0
        for res in results:
            if isinstance(res, Exception):
                failed += 1
                continue
            if not getattr(res, "ok", False):
                failed += 1
        if failed:
            log.warning(
                "Steam unfriend tasks finished with %d/%d failures",
                failed,
                len(unique_ids),
            )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        await self._handle_leave_event(member.id, member.guild.id, member.display_name, "leave")

    @commands.Cog.listener()
    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        user = payload.user
        if user and user.bot:
            return
        user_id = getattr(payload, "user_id", None) or (user.id if user else None)
        if not user_id:
            return
        display = getattr(user, "display_name", None) or getattr(user, "name", None)
        await self._handle_leave_event(int(user_id), payload.guild_id, display, "raw_leave")

    @tasks.loop(hours=24)
    async def reconcile_orphaned_links(self) -> None:
        await self.bot.wait_until_ready()
        guild_id = settings.guild_id
        if not guild_id:
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            log.debug("SteamLeaveCleanup: Guild %s not found for reconciliation", guild_id)
            return

        rows = db.query_all("SELECT DISTINCT user_id FROM steam_links WHERE user_id != 0")
        missing: list[int] = []
        for r in rows:
            uid = int(r["user_id"])
            member = guild.get_member(uid)
            if member is not None:
                # Previously flagged as missing? clear it again
                self._reconcile_suspects.pop(uid, None)
                continue
            try:
                member = await guild.fetch_member(uid)
            except discord.NotFound:
                missing.append(uid)
            except discord.HTTPException as exc:  # rate limits / temporary failures
                log.debug("SteamLeaveCleanup: fetch_member failed for %s: %s", uid, exc)
                continue
            else:
                if member is None:
                    missing.append(uid)

        if not missing:
            return

        now = time.time()
        confirmed: list[int] = []
        for uid in missing:
            # Prefer explicit leave evidence from the tracker
            if self._confirmed_left(uid, guild.id):
                confirmed.append(uid)
                self._reconcile_suspects.pop(uid, None)
                continue

            # Fallback: grace-based double detection to avoid false positives
            first_seen = self._reconcile_suspects.get(uid)
            if first_seen is None:
                self._reconcile_suspects[uid] = now
                log.info(
                    "SteamLeaveCleanup: %s missing (no leave log), deferring cleanup for %.0fh",
                    uid,
                    self._reconcile_grace_seconds / 3600,
                )
                continue
            if now - first_seen < self._reconcile_grace_seconds:
                log.debug(
                    "SteamLeaveCleanup: %s still missing but within grace (%.0fs left)",
                    uid,
                    self._reconcile_grace_seconds - (now - first_seen),
                )
                continue
            confirmed.append(uid)
            self._reconcile_suspects.pop(uid, None)

        if not confirmed:
            return

        for uid in confirmed:
            await self._cleanup_user(uid, guild.id, None, "reconcile")

    @reconcile_orphaned_links.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _fetch_relationships(self, steam_ids: Iterable[str]) -> dict[str, str]:
        """
        Bot-seitige Prüfung der aktuellen Beziehung (inkl. Pending) direkt via Steam-Task.
        Hard-throttled auf 1x/24h, um Steam nicht unnötig zu belasten.
        """
        now = time.time()
        if now - self._last_relationship_probe < 24 * 60 * 60:
            return {}
        self._last_relationship_probe = now

        out: dict[str, str] = {}
        tasks = []
        for sid in {s for s in steam_ids if s}:
            tasks.append(self.tasks.run("AUTH_CHECK_FRIENDSHIP", {"steam_id": sid}, timeout=15.0))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception) or not getattr(res, "ok", False):
                continue
            data = (res.result or {}).get("data") if isinstance(res.result, dict) else None
            if not data:
                continue
            sid = str(data.get("steam_id64") or "").strip()
            rel = data.get("relationship_name") or str(data.get("relationship") or "")
            if sid:
                out[sid] = rel
        return out


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SteamLeaveCleanup(bot))
