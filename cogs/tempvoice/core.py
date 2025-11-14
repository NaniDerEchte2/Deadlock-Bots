# cogs/tempvoice/core.py
# TempVoiceCore – Auto-Lanes, Owner-Logik, Persistenz (zentrale DB)
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional, Dict, Set, List, Tuple, Any
from datetime import datetime

import discord
from discord.ext import commands
import aiosqlite
from service.db import db_path
from pathlib import Path
DB_PATH = Path(db_path())  # alias, damit alter Code weiterläuft


log = logging.getLogger("TempVoiceCore")

# --------- IDs / Konfiguration ---------
STAGING_CHANNEL_IDS: Set[int] = {
    1330278323145801758,  # Casual Staging
    1357422958544420944,  # Ranked Staging
    1412804671432818890,  # Spezial Staging
}
MINRANK_CATEGORY_IDS: Set[int] = {
    1412804540994162789,  # Grind Lanes
    1289721245281292290,  # Normal Lanes (MinRank freigeschaltet)
}
# Legacy-Alias für ältere Imports, zeigt weiterhin auf die ursprüngliche Grind-ID
MINRANK_CATEGORY_ID: int = 1412804540994162789
RANKED_CATEGORY_ID: int = 1357422957017698478
INTERFACE_TEXT_CHANNEL_ID: int = 1371927143537315890  # exportiert (wird vom Interface genutzt)
ENGLISH_ONLY_ROLE_ID: int = 1309741866098491479

DEFAULT_CASUAL_CAP = 8
DEFAULT_RANKED_CAP = 6
NAME_EDIT_COOLDOWN_SEC = 120
STARTUP_PURGE_DELAY_SEC = 3
PURGE_INTERVAL_SECONDS = 60

# LiveMatch-Suffix (vom Worker) – NICHT von TempVoice anfassen
LIVE_SUFFIX_RX = re.compile(
    r"\s+•\s+\d+/\d+\s+(Im\s+Match|Im\s+Spiel|Lobby/Queue)",
    re.IGNORECASE
)
# TempVoice darf nur in diesem Zeitfenster nach Erstellung Namen setzen
ONLY_SET_NAME_ON_CREATE = True
CREATE_RENAME_WINDOW_SEC = 45

RANK_ORDER = [
    "unknown","initiate","seeker","alchemist","arcanist",
    "ritualist","emissary","archon","oracle","phantom","ascendant","eternus"
]
RANK_SET = set(RANK_ORDER)
SUFFIX_THRESHOLD_RANK = "emissary"

# Export-Intent für andere Module (verhindert "unused global variable")
__all__ = [
    "STAGING_CHANNEL_IDS",
    "MINRANK_CATEGORY_ID",
    "MINRANK_CATEGORY_IDS",
    "RANKED_CATEGORY_ID",
    "INTERFACE_TEXT_CHANNEL_ID",
    "ENGLISH_ONLY_ROLE_ID",
    "RANK_ORDER",
]

# --------- Hilfen ---------
def _is_managed_lane(ch: Optional[discord.VoiceChannel]) -> bool:
    return isinstance(ch, discord.VoiceChannel) and ch.name.startswith("Lane ")

def _default_cap(ch: discord.abc.GuildChannel) -> int:
    cat_id = getattr(ch, "category_id", None)
    return DEFAULT_RANKED_CAP if cat_id == RANKED_CATEGORY_ID else DEFAULT_CASUAL_CAP

def _rank_index(name: str) -> int:
    n = name.lower()
    return RANK_ORDER.index(n) if n in RANK_SET else 0

def _rank_roles(guild: discord.Guild) -> Dict[str, discord.Role]:
    out: Dict[str, discord.Role] = {}
    for r in guild.roles:
        n = r.name.lower()
        if n in RANK_SET:
            out[n] = r
    return out

def _strip_suffixes(current: str) -> str:
    base = current
    for marker in (" • ab ",):
        if marker in base:
            base = base.split(marker)[0]
    return base

def _has_live_suffix(name: str) -> bool:
    return LIVE_SUFFIX_RX.search(name) is not None

def _age_seconds(ch: discord.VoiceChannel) -> float:
    try:
        return (discord.utils.utcnow() - ch.created_at).total_seconds()
    except Exception as e:
        log.debug("age_seconds failed for %s: %r", getattr(ch, "id", "?"), e)
        return 999999.0

# --------- DB-Layer (zentral) ---------
class TVDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

    @property
    def connected(self) -> bool:
        return self.db is not None

    async def connect(self):
        if self.db is not None:
            return
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=NORMAL")
        await self._create_tables()

    async def _create_tables(self):
        assert self.db is not None
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS tempvoice_bans (
                owner_id    INTEGER NOT NULL,
                banned_id   INTEGER NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (owner_id, banned_id)
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS tempvoice_lanes (
                channel_id  INTEGER PRIMARY KEY,
                guild_id    INTEGER NOT NULL,
                owner_id    INTEGER NOT NULL,
                base_name   TEXT NOT NULL,
                category_id INTEGER NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS tempvoice_owner_prefs (
                owner_id    INTEGER PRIMARY KEY,
                region      TEXT NOT NULL CHECK(region IN ('DE','EU')),
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await self.db.commit()
        await self._ensure_interface_table()

    async def _ensure_interface_table(self):
        assert self.db is not None
        cur = await self.db.execute("PRAGMA table_info(tempvoice_interface)")
        rows = await cur.fetchall()
        await cur.close()

        if not rows:
            await self._create_interface_table()
            await self.db.commit()
            return

        col_names = {str(row["name"]) for row in rows}
        pk_cols = [str(row["name"]) for row in rows if int(row["pk"]) > 0]
        required = {"guild_id", "channel_id", "message_id", "category_id", "lane_id", "created_at", "updated_at"}

        if required.issubset(col_names) and pk_cols == ["guild_id", "message_id"]:
            return

        await self._migrate_interface_table()

    async def _create_interface_table(self):
        assert self.db is not None
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS tempvoice_interface (
                guild_id    INTEGER NOT NULL,
                channel_id  INTEGER NOT NULL,
                message_id  INTEGER NOT NULL,
                category_id INTEGER,
                lane_id     INTEGER,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, message_id),
                UNIQUE(lane_id)
            )
        """)

    async def _migrate_interface_table(self):
        assert self.db is not None
        try:
            await self.db.execute("ALTER TABLE tempvoice_interface RENAME TO tempvoice_interface_old")
        except Exception as e:
            log.debug("tempvoice_interface rename failed (migration skipped): %r", e)
            await self._create_interface_table()
            await self.db.commit()
            return

        await self._create_interface_table()
        try:
            await self.db.execute("""
                INSERT INTO tempvoice_interface(guild_id, channel_id, message_id, updated_at)
                SELECT guild_id, channel_id, message_id, COALESCE(updated_at, CURRENT_TIMESTAMP)
                FROM tempvoice_interface_old
            """)
        except Exception as e:
            log.debug("tempvoice_interface migration copy failed: %r", e)
        await self.db.execute("DROP TABLE IF EXISTS tempvoice_interface_old")
        await self.db.commit()

    async def fetchone(self, q: str, p: tuple = ()):
        if not self.connected:
            raise ValueError("no active connection")
        cur = await self.db.execute(q, p)  # type: ignore
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, q: str, p: tuple = ()):
        if not self.connected:
            raise ValueError("no active connection")
        cur = await self.db.execute(q, p)  # type: ignore
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def exec(self, q: str, p: tuple = ()):
        if not self.connected:
            raise ValueError("no active connection")
        await self.db.execute(q, p)  # type: ignore
        await self.db.commit()

    async def close(self):
        if self.db:
            try:
                await self.db.close()
            except Exception as e:
                log.debug("TVDB close failed: %r", e)
            finally:
                self.db = None

# --------- Ban-Store ---------
class AsyncBanStore:
    def __init__(self, db: TVDB): self.db = db

    async def is_banned_by_owner(self, owner_id: int, user_id: int) -> bool:
        try:
            row = await self.db.fetchone(
                "SELECT 1 FROM tempvoice_bans WHERE owner_id=? AND banned_id=?",
                (int(owner_id), int(user_id))
            )
            return row is not None
        except Exception as e:
            log.warning("is_banned_by_owner failed (%s->%s): %r", owner_id, user_id, e)
            return False

    async def list_bans(self, owner_id: int) -> List[int]:
        try:
            rows = await self.db.fetchall(
                "SELECT banned_id FROM tempvoice_bans WHERE owner_id=?",
                (int(owner_id),)
            )
            return [int(r["banned_id"]) for r in rows]
        except Exception as e:
            log.warning("list_bans failed for %s: %r", owner_id, e)
            return []

    async def add_ban(self, owner_id: int, user_id: int):
        try:
            await self.db.exec(
                "INSERT OR IGNORE INTO tempvoice_bans(owner_id, banned_id) VALUES(?,?)",
                (int(owner_id), int(user_id))
            )
        except Exception as e:
            log.warning("add_ban failed (%s->%s): %r", owner_id, user_id, e)

    async def remove_ban(self, owner_id: int, user_id: int):
        try:
            await self.db.exec(
                "DELETE FROM tempvoice_bans WHERE owner_id=? AND banned_id=?",
                (int(owner_id), int(user_id))
            )
        except Exception as e:
            log.warning("remove_ban failed (%s->%s): %r", owner_id, user_id, e)

# --------- Core-Cog ---------
class TempVoiceCore(commands.Cog):
    """Kern: Auto-Lanes, Owner, Persistenz, MinRank, Region-Filter, Purge"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._tvdb = TVDB(str(DB_PATH))
        self.bans = AsyncBanStore(self._tvdb)

        # Laufzeit-State
        self.created_channels: Set[int] = set()
        self.lane_owner: Dict[int, int] = {}
        self.lane_base: Dict[int, str] = {}
        self.lane_min_rank: Dict[int, str] = {}
        self.join_time: Dict[int, Dict[int, float]] = {}
        self._edit_locks: Dict[int, asyncio.Lock] = {}
        self._lane_creation_locks: Dict[int, asyncio.Lock] = {}
        self._last_name_desired: Dict[int, str] = {}
        self._last_name_patch_ts: Dict[int, float] = {}
        self._bg_tasks: Set[asyncio.Task] = set()
        self._shutting_down: bool = False

    # --------- Lifecycle ---------
    async def cog_load(self):
        await self._tvdb.connect()
        self._track(self._startup())

    async def cog_unload(self):
        self._shutting_down = True
        for t in list(self._bg_tasks):
            t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
        await self._tvdb.close()

    def _track(self, aw: Any):
        t = asyncio.create_task(aw)
        self._bg_tasks.add(t)
        t.add_done_callback(lambda _: self._bg_tasks.discard(t))

    async def _startup(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(STARTUP_PURGE_DELAY_SEC)
        await self._rehydrate_from_db()
        await self._purge_empty_lanes_once()
        self._track(self._delayed_purge(30))
        log.info("TempVoiceCore bereit • verwaltete Lanes: %d", len(self.created_channels))

    async def _delayed_purge(self, delay: int):
        try:
            await asyncio.sleep(delay)
            while not self._shutting_down:
                if not self._tvdb.connected:
                    await asyncio.sleep(PURGE_INTERVAL_SECONDS)
                    continue
                try:
                    await self._purge_empty_lanes_once()
                except Exception as inner:
                    log.exception("TempVoice purge loop failed: %r", inner)
                await asyncio.sleep(PURGE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.exception("TempVoice purge loop crashed: %r", e)

    # --------- Rehydrierung / Purge ---------
    def _first_guild(self) -> Optional[discord.Guild]:
        return self.bot.guilds[0] if self.bot.guilds else None

    async def _rehydrate_from_db(self):
        guild = self._first_guild()
        if not guild:
            return
        try:
            rows = await self._tvdb.fetchall(
                "SELECT channel_id, owner_id, base_name, category_id FROM tempvoice_lanes WHERE guild_id=?",
                (int(guild.id),)
            )
        except Exception as e:
            log.warning("rehydrate: fetch failed: %r", e)
            return

        for r in rows:
            lane_id = int(r["channel_id"])
            lane: Optional[discord.VoiceChannel] = guild.get_channel(lane_id)  # type: ignore
            if not isinstance(lane, discord.VoiceChannel):
                try:
                    await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (lane_id,))
                except Exception as e:
                    log.debug("rehydrate: cleanup row failed for %s: %r", lane_id, e)
                continue

            self.created_channels.add(lane.id)
            self.lane_owner[lane.id] = int(r["owner_id"])
            self.lane_base[lane.id] = str(r["base_name"])
            self.lane_min_rank.setdefault(lane.id, "unknown")
            self.join_time.setdefault(lane.id, {})

            await self._apply_owner_settings(lane, self.lane_owner[lane.id])
            # KEIN aggressives Rename hier – _refresh_name() prüft Schutzbedingungen
            await self._refresh_name(lane)

    async def _purge_empty_lanes_once(self):
        guild = self._first_guild()
        if not guild or not self._tvdb.connected:
            return

        try:
            rows = await self._tvdb.fetchall(
                "SELECT channel_id FROM tempvoice_lanes WHERE guild_id=?",
                (int(guild.id),)
            )
        except Exception as e:
            log.warning("purge: fetch failed: %r", e)
            return

        processed_lane_ids: Set[int] = set()
        for r in rows:
            lane_id = int(r["channel_id"])
            processed_lane_ids.add(lane_id)
            try:
                lane = guild.get_channel(lane_id)
                if not isinstance(lane, discord.VoiceChannel):
                    await self._cleanup_lane(lane_id, channel=None, reason="TempVoice: Cleanup (missing channel)")
                    continue
                if len(lane.members) == 0:
                    await self._cleanup_lane(lane_id, channel=lane, reason="TempVoice: Cleanup (leer)")
            except Exception as e:
                log.debug("purge: inspect lane %s failed: %r", lane_id, e)

        for ch in list(guild.voice_channels):
            if not _is_managed_lane(ch) or ch.id in processed_lane_ids:
                continue
            try:
                if len(ch.members) == 0:
                    await self._cleanup_lane(int(ch.id), channel=ch, reason="TempVoice: Sweep (leer)")
            except Exception as e:
                log.debug("sweep: inspect lane %s failed: %r", ch.id, e)

    async def _cleanup_lane(
        self,
        lane_id: int,
        *,
        channel: Optional[discord.VoiceChannel],
        reason: str,
    ) -> None:
        if channel:
            try:
                await channel.delete(reason=reason)
            except discord.Forbidden as e:
                log.warning(
                    "TempVoice: missing permission to delete lane %s (%s): %s",
                    lane_id,
                    getattr(channel, "name", "?"),
                    e,
                )
            except Exception as e:
                log.warning(
                    "TempVoice: unexpected error deleting lane %s (%s): %r",
                    lane_id,
                    getattr(channel, "name", "?"),
                    e,
                )
        if self._tvdb.connected:
            try:
                await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (lane_id,))
            except Exception as e:
                log.debug("cleanup: delete row %s failed: %r", lane_id, e)
            try:
                await self._tvdb.exec("DELETE FROM tempvoice_interface WHERE lane_id=?", (lane_id,))
            except Exception as e:
                log.debug("cleanup: delete interface row %s failed: %r", lane_id, e)
        self.created_channels.discard(lane_id)
        for mapping in (
            self.lane_owner,
            self.lane_base,
            self.lane_min_rank,
            self.join_time,
            self._last_name_desired,
            self._last_name_patch_ts,
        ):
            mapping.pop(lane_id, None)
        self._edit_locks.pop(lane_id, None)
        try:
            self.bot.dispatch("tempvoice_lane_deleted", lane_id)
        except Exception as e:
            log.debug("dispatch lane_deleted failed for %s: %r", lane_id, e)

    # --------- Öffentliche Helfer (von UI aufgerufen) ---------
    async def parse_user_identifier(self, guild: discord.Guild, raw: str) -> Optional[int]:
        s = raw.strip()
        if s.startswith("<@") and s.endswith(">"):
            digits = "".join(ch for ch in s if ch.isdigit())
            if digits:
                return int(digits)
        if s.startswith("@"):
            s = s[1:].strip()
        if s.isdigit():
            return int(s)

        low = s.lower()
        matches: List[int] = []
        for m in guild.members:
            names = {m.name, getattr(m, "global_name", None), m.display_name}
            if any(n and n.lower() == low for n in names):
                matches.append(m.id)
        if len(matches) == 1:
            return matches[0]
        return None

    async def set_region_pref(self, owner_id: int, region: str):
        try:
            await self._tvdb.exec(
                """
                INSERT INTO tempvoice_owner_prefs(owner_id, region, updated_at)
                VALUES(?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(owner_id) DO UPDATE SET
                    region=excluded.region,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (int(owner_id), "DE" if region == "DE" else "EU")
            )
        except Exception as e:
            log.warning("set_region_pref failed for %s: %r", owner_id, e)

    async def get_region_pref(self, owner_id: int) -> str:
        try:
            row = await self._tvdb.fetchone(
                "SELECT region FROM tempvoice_owner_prefs WHERE owner_id=?",
                (int(owner_id),)
            )
            if row and str(row["region"]) in ("DE", "EU"):
                return str(row["region"])
        except Exception as e:
            log.debug("get_region_pref failed for %s: %r", owner_id, e)
        return "EU"

    async def apply_region(self, lane: discord.VoiceChannel, region: str):
        role = lane.guild.get_role(ENGLISH_ONLY_ROLE_ID)
        if not role:
            return
        try:
            if region == "DE":
                ow = lane.overwrites_for(role); ow.connect = False
                await lane.set_permissions(role, overwrite=ow, reason="TempVoice: Deutsch-Only")
            else:
                await lane.set_permissions(role, overwrite=None, reason="TempVoice: Sprachfilter frei")
        except Exception as e:
            log.debug("apply_region failed for lane %s: %r", lane.id, e)

    async def claim_owner(self, lane: discord.VoiceChannel, member: discord.Member):
        previous_owner = self.lane_owner.get(lane.id)
        if previous_owner == member.id:
            return
        self.lane_owner[lane.id] = member.id
        try:
            await self._tvdb.exec(
                "UPDATE tempvoice_lanes SET owner_id=? WHERE channel_id=?",
                (int(member.id), int(lane.id))
            )
        except Exception as e:
            log.debug("claim_owner: db update failed for lane %s: %r", lane.id, e)
        if previous_owner and previous_owner != member.id:
            try:
                await self._clear_owner_bans(lane, previous_owner)
            except Exception as e:
                log.debug("claim_owner: clear_owner_bans failed for lane %s: %r", lane.id, e)
        try:
            await self._apply_owner_settings(lane, member.id)
        except Exception as e:
            log.debug("claim_owner: apply_owner_settings failed for lane %s: %r", lane.id, e)
        try:
            self.bot.dispatch("tempvoice_lane_owner_changed", lane, int(member.id))
        except Exception as e:
            log.debug("claim_owner: dispatch lane_owner_changed failed for lane %s: %r", lane.id, e)

# ===== Öffentliche Fassade für das Interface =====
    @property
    def db(self):
        return self._tvdb

    def first_guild(self):
        return self._first_guild()

    async def safe_edit_channel(self, lane: discord.VoiceChannel,
                                *, desired_name: str | None = None,
                                desired_limit: int | None = None,
                                reason: str | None = None):
        await self._safe_edit_channel(lane, desired_name=desired_name,
                                      desired_limit=desired_limit, reason=reason)

    async def refresh_name(self, lane: discord.VoiceChannel):
        await self._refresh_name(lane)

    async def set_owner_region(self, owner_id: int, region: str):
        await self.set_region_pref(owner_id, region)

    async def apply_owner_region_to_lane(self, lane: discord.VoiceChannel, owner_id: int):
        region = await self.get_region_pref(owner_id)
        await self.apply_region(lane, region)

    async def transfer_owner(self, lane: discord.VoiceChannel, member_id: int):
        m = lane.guild.get_member(int(member_id))
        if m:
            await self.claim_owner(lane, m)

    # --------- Channel Updates ---------
    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        lock = self._edit_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._edit_locks[channel_id] = lock
        return lock

    async def _safe_edit_channel(self, lane: discord.VoiceChannel,
                                 *, desired_name: Optional[str] = None,
                                 desired_limit: Optional[int] = None,
                                 reason: Optional[str] = None):
        lock = self._lock_for(lane.id)
        async with lock:
            kwargs: Dict[str, Any] = {}
            now = time.time()

            # ===== Name bearbeiten? Nur beim Erstellen & wenn KEIN Live-Match-Suffix vorhanden ist =====
            if desired_name is not None and lane.name != desired_name:
                may_rename = True
                if ONLY_SET_NAME_ON_CREATE:
                    if _age_seconds(lane) > CREATE_RENAME_WINDOW_SEC:
                        may_rename = False
                if _has_live_suffix(lane.name):
                    may_rename = False

                if may_rename:
                    last_desired = self._last_name_desired.get(lane.id)
                    if last_desired == desired_name:
                        last_ts = self._last_name_patch_ts.get(lane.id, 0.0)
                        if now - last_ts >= NAME_EDIT_COOLDOWN_SEC:
                            kwargs["name"] = desired_name
                    else:
                        kwargs["name"] = desired_name
                    self._last_name_desired[lane.id] = desired_name
                # sonst: Name bleibt in Ruhe

            if desired_limit is not None and desired_limit != lane.user_limit:
                kwargs["user_limit"] = max(0, min(99, desired_limit))

            if not kwargs:
                return

            try:
                await lane.edit(**kwargs, reason=reason or "TempVoice: Update")
                if "name" in kwargs:
                    self._last_name_patch_ts[lane.id] = now
            except discord.HTTPException as e:
                log.warning(
                    "TempVoice: lane.edit failed for %s (payload=%s): %s",
                    lane.id,
                    kwargs,
                    e,
                )

    def _compose_name(self, lane: discord.VoiceChannel) -> str:
        base = self.lane_base.get(lane.id) or _strip_suffixes(lane.name)
        parts = [base]
        if lane.category_id in MINRANK_CATEGORY_IDS:
            min_rank = self.lane_min_rank.get(lane.id, "unknown")
            if (min_rank and min_rank != "unknown" and
                    _rank_index(min_rank) >= _rank_index(SUFFIX_THRESHOLD_RANK)):
                parts.append(f" • ab {min_rank.capitalize()}")
        return "".join(parts)

    async def _refresh_name(self, lane: discord.VoiceChannel):
        # Schutz: Nie rumpfuschen, wenn LiveMatch-Suffix dran ist oder Channel nicht frisch ist
        if _has_live_suffix(lane.name):
            return
        if ONLY_SET_NAME_ON_CREATE and _age_seconds(lane) > CREATE_RENAME_WINDOW_SEC:
            return
        await self._safe_edit_channel(
            lane,
            desired_name=self._compose_name(lane),
            reason="TempVoice: Name aktualisiert"
        )

    def _current_member_and_channel(
        self, guild: discord.Guild, member_id: int
    ) -> Tuple[Optional[discord.Member], Optional[discord.VoiceChannel]]:
        member = guild.get_member(int(member_id))
        channel: Optional[discord.VoiceChannel] = None
        if member and member.voice and isinstance(member.voice.channel, discord.VoiceChannel):
            channel = member.voice.channel
        return member, channel

    async def _next_name(self, category: Optional[discord.CategoryChannel], prefix: str) -> str:
        if not category:
            return f"{prefix} 1"
        used: Set[int] = set()
        pat = re.compile(rf"^{re.escape(prefix)}\s+(\d+)\b")
        for c in category.voice_channels:
            m = pat.match(c.name)
            if m:
                try:
                    used.add(int(m.group(1)))
                except Exception as e:
                    log.debug("next_name: parse existing index failed for %s: %r", c.name, e)
        n = 1
        while n in used:
            n += 1
        return f"{prefix} {n}"

    async def _apply_owner_bans(self, lane: discord.VoiceChannel, owner_id: int):
        banned = await self.bans.list_bans(owner_id)
        for uid in banned:
            try:
                obj = lane.guild.get_member(int(uid)) or discord.Object(id=int(uid))
                ow = lane.overwrites_for(obj); ow.connect = False
                await lane.set_permissions(obj, overwrite=ow, reason="Owner-Ban (persistent)")
                await asyncio.sleep(0.02)
            except Exception as e:
                log.debug("apply_owner_bans: failed for %s in lane %s: %r", uid, lane.id, e)

    async def _clear_owner_bans(self, lane: discord.VoiceChannel, owner_id: Optional[int]):
        if not owner_id:
            return
        try:
            banned = await self.bans.list_bans(owner_id)
        except Exception as e:
            log.debug("clear_owner_bans: list_bans failed for owner %s: %r", owner_id, e)
            return
        for uid in banned:
            obj = lane.guild.get_member(int(uid)) or discord.Object(id=int(uid))
            try:
                await lane.set_permissions(
                    obj,
                    overwrite=None,
                    reason="TempVoice: Ownerwechsel Ban-Reset"
                )
            except Exception as e:
                log.debug("clear_owner_bans: reset failed for target %s in lane %s: %r", uid, lane.id, e)
            await asyncio.sleep(0.02)

    async def _apply_owner_settings(self, lane: discord.VoiceChannel, owner_id: int):
        region = await self.get_region_pref(owner_id)
        await self.apply_region(lane, region)
        await self._apply_owner_bans(lane, owner_id)

    async def _apply_min_rank(self, lane: discord.VoiceChannel, min_rank: str):
        if lane.category_id not in MINRANK_CATEGORY_IDS:
            return
        guild = lane.guild
        ranks = _rank_roles(guild)

        if min_rank == "unknown":
            for role in ranks.values():
                ow = lane.overwrites_for(role)
                if ow.connect is not None:
                    try:
                        await lane.set_permissions(role, overwrite=None, reason="TempVoice: MinRank reset")
                    except Exception as e:
                        log.debug("apply_min_rank reset failed for role %s: %r", role.id, e)
                    await asyncio.sleep(0.02)
            return

        min_idx = _rank_index(min_rank)
        for name, role in ranks.items():
            if _rank_index(name) < min_idx:
                try:
                    ow = lane.overwrites_for(role); ow.connect = False
                    await lane.set_permissions(role, overwrite=ow, reason="TempVoice: MinRank deny")
                except Exception as e:
                    log.debug("apply_min_rank deny failed for role %s: %r", role.id, e)
            else:
                ow = lane.overwrites_for(role)
                if ow.connect is not None:
                    try:
                        await lane.set_permissions(role, overwrite=None, reason="TempVoice: MinRank clear")
                    except Exception as e:
                        log.debug("apply_min_rank clear failed for role %s: %r", role.id, e)
            await asyncio.sleep(0.02)

    # --------- Lane-Erstellung ---------
    async def _create_lane(self, member: discord.Member, staging: discord.VoiceChannel):
        guild = member.guild
        member_id = int(member.id)
        lock = self._lane_creation_locks.setdefault(member_id, asyncio.Lock())
        if lock.locked():
            log.debug("create_lane: duplicate request for %s ignored (staging=%s)", member_id, staging.id)
            return

        try:
            async with lock:
                fresh_member, current_channel = self._current_member_and_channel(guild, member_id)
                if not fresh_member or not current_channel or current_channel.id != staging.id:
                    return
                member = fresh_member

                cat = staging.category
                base = await self._next_name(cat, "Lane")
                bitrate = getattr(guild, "bitrate_limit", None) or 256000
                cap = _default_cap(staging)
                try:
                    lane = await guild.create_voice_channel(
                        name=base,
                        category=cat,
                        user_limit=cap,
                        bitrate=bitrate,
                        reason=f"Auto-Lane für {member.display_name}",
                        overwrites=cat.overwrites if cat else None
                    )
                except discord.Forbidden:
                    log.error("Fehlende Rechte: VoiceChannel erstellen.")
                    return
                except Exception as e:
                    log.error(f"create_lane error: {e}")
                    return

                self.created_channels.add(lane.id)
                self.lane_owner[lane.id] = member.id
                self.lane_base[lane.id] = base
                self.lane_min_rank[lane.id] = "unknown"
                self.join_time.setdefault(lane.id, {})

                try:
                    await self._tvdb.exec(
                        "INSERT OR REPLACE INTO tempvoice_lanes(channel_id, guild_id, owner_id, base_name, category_id) "
                        "VALUES(?,?,?,?,?)",
                        (int(lane.id), int(guild.id), int(member.id), base, int(cat.id) if cat else 0)
                    )
                except Exception as e:
                    log.warning(
                        "TempVoice: DB insert failed for lane %s (owner=%s, category=%s): %r",
                        lane.id,
                        member.id,
                        getattr(cat, "id", None),
                        e,
                    )

                await self._apply_owner_settings(lane, member.id)

                refreshed_member, refreshed_channel = self._current_member_and_channel(guild, member_id)
                if not refreshed_member or not refreshed_channel:
                    await self._cleanup_lane(int(lane.id), channel=lane, reason="TempVoice: Owner nicht auffindbar")
                    return
                if refreshed_channel.id != staging.id:
                    await self._cleanup_lane(int(lane.id), channel=lane, reason="TempVoice: Owner nicht mehr im Staging")
                    return
                member = refreshed_member

                try:
                    await member.move_to(lane, reason="TempVoice: Auto-Lane erstellt")
                except discord.Forbidden as e:
                    log.warning(
                        "TempVoice: move_to forbidden (member=%s staging=%s lane=%s): %s",
                        member.id,
                        staging.id,
                        lane.id,
                        e,
                    )
                    await self._cleanup_lane(
                        int(lane.id),
                        channel=lane,
                        reason="TempVoice: Move fehlgeschlagen (forbidden)",
                    )
                    return
                except discord.HTTPException as e:
                    log.warning(
                        "TempVoice: move_to HTTP error (member=%s staging=%s lane=%s): %s",
                        member.id,
                        staging.id,
                        lane.id,
                        e,
                    )
                    await self._cleanup_lane(
                        int(lane.id),
                        channel=lane,
                        reason="TempVoice: Move fehlgeschlagen (http)",
                    )
                    return
                except Exception as e:
                    log.warning(
                        "TempVoice: move_to failed unexpectedly (member=%s staging=%s lane=%s): %r",
                        member.id,
                        staging.id if staging else "?",
                        lane.id,
                        e,
                    )
                    await self._cleanup_lane(
                        int(lane.id),
                        channel=lane,
                        reason="TempVoice: Move fehlgeschlagen (unexpected)",
                    )
                    return

                # NUR hier initial den Namen setzen (innerhalb des Create-Fensters)
                await self._refresh_name(lane)
                try:
                    self.bot.dispatch("tempvoice_lane_created", lane, member)
                except Exception as e:
                    log.debug("dispatch lane_created failed for %s: %r", lane.id, e)
        finally:
            lock_ref = self._lane_creation_locks.get(member_id)
            if lock_ref and not lock_ref.locked():
                self._lane_creation_locks.pop(member_id, None)

    # --------- Events ---------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState, after: discord.VoiceState):
        # Auto-Lane bei Join in Staging
        try:
            if after and after.channel and isinstance(after.channel, discord.VoiceChannel):
                if after.channel.id in STAGING_CHANNEL_IDS:
                    await self._create_lane(member, after.channel)
        except Exception as e:
            log.warning(f"Auto-lane create failed: {e}")

        # Owner verlassen / Lane ggf. löschen
        try:
            if before and before.channel and isinstance(before.channel, discord.VoiceChannel):
                ch = before.channel
                if ch.id in self.join_time:
                    self.join_time[ch.id].pop(member.id, None)
                if ch.id in self.lane_owner and self.lane_owner[ch.id] == member.id:
                    if len(ch.members) > 0:
                        tsmap = self.join_time.get(ch.id, {})
                        candidates = list(ch.members)
                        candidates.sort(key=lambda m: tsmap.get(m.id, float("inf")))
                        new_owner_member = candidates[0]
                        self.lane_owner[ch.id] = new_owner_member.id
                        try:
                            await self._tvdb.exec(
                                "UPDATE tempvoice_lanes SET owner_id=? WHERE channel_id=?",
                                (int(self.lane_owner[ch.id]), int(ch.id))
                            )
                        except Exception as e:
                            log.debug("owner transfer db update failed for lane %s: %r", ch.id, e)
                        try:
                            await self._clear_owner_bans(ch, member.id)
                        except Exception as e:
                            log.debug("owner transfer clear_owner_bans failed for lane %s: %r", ch.id, e)
                        try:
                            await self._apply_owner_settings(ch, new_owner_member.id)
                        except Exception as e:
                            log.debug("owner transfer apply settings failed for lane %s: %r", ch.id, e)
                        try:
                            self.bot.dispatch("tempvoice_lane_owner_changed", ch, int(new_owner_member.id))
                        except Exception as e:
                            log.debug("dispatch lane_owner_changed failed for %s: %r", ch.id, e)
                    else:
                        lane_id = int(ch.id)
                        await self._cleanup_lane(lane_id, channel=ch, reason="TempVoice: Lane leer")
        except Exception as e:
            log.debug("owner/cleanup flow failed: %r", e)

        # Join-Zeit & Bannprüfung; KEIN Namens-Refresh mehr außer im Create-Fenster
        try:
            if after and after.channel and isinstance(after.channel, discord.VoiceChannel):
                ch = after.channel
                self.join_time.setdefault(ch.id, {})
                self.join_time[ch.id][member.id] = datetime.utcnow().timestamp()

                if _is_managed_lane(ch) and ch.id not in self.lane_owner:
                    base_name = self.lane_base.get(ch.id) or _strip_suffixes(ch.name)
                    self.lane_owner[ch.id] = member.id
                    self.lane_base[ch.id] = base_name
                    self.created_channels.add(ch.id)
                    try:
                        await self._tvdb.exec(
                            """
                            INSERT INTO tempvoice_lanes(channel_id, guild_id, owner_id, base_name, category_id)
                            VALUES(?,?,?,?,?)
                            ON CONFLICT(channel_id) DO UPDATE SET
                                owner_id=excluded.owner_id,
                                base_name=excluded.base_name,
                                category_id=excluded.category_id
                            """,
                            (
                                int(ch.id),
                                int(ch.guild.id),
                                int(member.id),
                                base_name,
                                int(ch.category_id) if ch.category_id else 0,
                            )
                        )
                    except Exception as e:
                        log.debug("lane owner backfill db failed for %s: %r", ch.id, e)
                    try:
                        await self._apply_owner_settings(ch, member.id)
                    except Exception as e:
                        log.debug("lane owner backfill apply settings failed for %s: %r", ch.id, e)
                    try:
                        self.bot.dispatch("tempvoice_lane_owner_changed", ch, int(member.id))
                    except Exception as e:
                        log.debug("dispatch lane_owner_changed (backfill) failed for %s: %r", ch.id, e)

                owner_id = self.lane_owner.get(ch.id)
                if owner_id and await self.bans.is_banned_by_owner(owner_id, member.id):
                    staging = None
                    for cid in STAGING_CHANNEL_IDS:
                        s = ch.guild.get_channel(cid)
                        if isinstance(s, discord.VoiceChannel):
                            staging = s
                            break
                    if staging:
                        try:
                            await member.move_to(staging, reason="Owner-Ban aktiv")
                        except Exception as e:
                            log.debug("move_to staging after ban failed for %s: %r", member.id, e)

                # Nur falls sehr frisch und noch ohne Live-Suffix – s. _refresh_name
                if _is_managed_lane(ch):
                    await self._refresh_name(ch)
        except Exception as e:
            log.debug("post-join flow failed: %r", e)

async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoiceCore(bot))
