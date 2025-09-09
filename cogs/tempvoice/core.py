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

from utils.deadlock_db import DB_PATH  # zentraler Pfad zu shared.db

log = logging.getLogger("TempVoiceCore")

# --------- IDs / Konfiguration ---------
STAGING_CHANNEL_IDS: Set[int] = {
    1330278323145801758,  # Casual Staging
    1357422958544420944,  # Ranked Staging
    1412804671432818890,  # Spezial Staging
}
MINRANK_CATEGORY_ID: int = 1412804540994162789
RANKED_CATEGORY_ID: int = 1357422957017698478
INTERFACE_TEXT_CHANNEL_ID: int = 1371927143537315890
ENGLISH_ONLY_ROLE_ID: int = 1309741866098491479

DEFAULT_CASUAL_CAP = 8
DEFAULT_RANKED_CAP = 6
NAME_EDIT_COOLDOWN_SEC = 120
STARTUP_PURGE_DELAY_SEC = 3

RANK_ORDER = [
    "unknown","initiate","seeker","alchemist","arcanist",
    "ritualist","emissary","archon","oracle","phantom","ascendant","eternus"
]
RANK_SET = set(RANK_ORDER)
SUFFIX_THRESHOLD_RANK = "emissary"


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
            CREATE TABLE IF NOT EXISTS tempvoice_interface (
                guild_id    INTEGER PRIMARY KEY,
                channel_id  INTEGER NOT NULL,
                message_id  INTEGER NOT NULL,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        except Exception:
            return False

    async def list_bans(self, owner_id: int) -> List[int]:
        try:
            rows = await self.db.fetchall(
                "SELECT banned_id FROM tempvoice_bans WHERE owner_id=?",
                (int(owner_id),)
            )
            return [int(r["banned_id"]) for r in rows]
        except Exception:
            return []

    async def add_ban(self, owner_id: int, user_id: int):
        try:
            await self.db.exec(
                "INSERT OR IGNORE INTO tempvoice_bans(owner_id, banned_id) VALUES(?,?)",
                (int(owner_id), int(user_id))
            )
        except Exception:
            pass

    async def remove_ban(self, owner_id: int, user_id: int):
        try:
            await self.db.exec(
                "DELETE FROM tempvoice_bans WHERE owner_id=? AND banned_id=?",
                (int(owner_id), int(user_id))
            )
        except Exception:
            pass


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
        # zweiter Sweep (manchmal fehlen Member nach Cold-Start)
        self._track(self._delayed_purge(30))
        log.info("TempVoiceCore bereit • verwaltete Lanes: %d", len(self.created_channels))

    async def _delayed_purge(self, delay: int):
        try:
            await asyncio.sleep(delay)
            if self._shutting_down or not self._tvdb.connected:
                return
            await self._purge_empty_lanes_once()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("TempVoice delayed purge failed")

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
        except Exception:
            return

        for r in rows:
            lane_id = int(r["channel_id"])
            lane: Optional[discord.VoiceChannel] = guild.get_channel(lane_id)  # type: ignore
            if not isinstance(lane, discord.VoiceChannel):
                try:
                    await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (lane_id,))
                except Exception:
                    pass
                continue

            self.created_channels.add(lane.id)
            self.lane_owner[lane.id] = int(r["owner_id"])
            self.lane_base[lane.id] = str(r["base_name"])
            self.lane_min_rank.setdefault(lane.id, "unknown")
            self.join_time.setdefault(lane.id, {})

            await self._apply_owner_bans(lane, self.lane_owner[lane.id])
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
        except Exception:
            return

        for r in rows:
            lane_id = int(r["channel_id"])
            lane = guild.get_channel(lane_id)
            if not isinstance(lane, discord.VoiceChannel):
                try:
                    await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (lane_id,))
                except Exception:
                    pass
                continue

            try:
                if len(lane.members) == 0:
                    try:
                        await lane.delete(reason="TempVoice: Cleanup (leer)")
                    except Exception:
                        pass
                    try:
                        await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (lane_id,))
                    except Exception:
                        pass
            except Exception:
                pass

        # Fallback: alle Lane * ohne DB-Eintrag löschen, wenn leer
        for ch in list(guild.voice_channels):
            if _is_managed_lane(ch):
                try:
                    if len(ch.members) == 0:
                        try:
                            await ch.delete(reason="TempVoice: Sweep (leer)")
                        except Exception:
                            pass
                        try:
                            await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (int(ch.id),))
                        except Exception:
                            pass
                except Exception:
                    pass

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
        except Exception:
            pass

    async def get_region_pref(self, owner_id: int) -> str:
        try:
            row = await self._tvdb.fetchone(
                "SELECT region FROM tempvoice_owner_prefs WHERE owner_id=?",
                (int(owner_id),)
            )
            if row and str(row["region"]) in ("DE", "EU"):
                return str(row["region"])
        except Exception:
            pass
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
        except Exception:
            pass

    async def claim_owner(self, lane: discord.VoiceChannel, member: discord.Member):
        self.lane_owner[lane.id] = member.id
        try:
            await self._tvdb.exec(
                "UPDATE tempvoice_lanes SET owner_id=? WHERE channel_id=?",
                (int(member.id), int(lane.id))
            )
        except Exception:
            pass
                    
# ===== Öffentliche Facade für das Interface =====

    @property
    def db(self):
        """DB-Zugriff für das Interface (zentral, shared.db)."""
        return self._tvdb

    def first_guild(self):
        """Öffentliche Variante von _first_guild()."""
        return self._first_guild()

    async def safe_edit_channel(self, lane: discord.VoiceChannel,
                                *, desired_name: str | None = None,
                                desired_limit: int | None = None,
                                reason: str | None = None):
        """Wrapper für _safe_edit_channel()."""
        await self._safe_edit_channel(lane, desired_name=desired_name,
                                      desired_limit=desired_limit, reason=reason)

    async def refresh_name(self, lane: discord.VoiceChannel):
        """Wrapper für _refresh_name()."""
        await self._refresh_name(lane)

    async def set_owner_region(self, owner_id: int, region: str):
        """Wrapper: persistiert Region (DE/EU) pro Owner."""
        await self.set_region_pref(owner_id, region)  # nutzt bestehende Logik

    async def apply_owner_region_to_lane(self, lane: discord.VoiceChannel, owner_id: int):
        """Wrapper: liest Owner-Region und setzt Channel-Permissions entsprechend."""
        region = await self.get_region_pref(owner_id)
        await self.apply_region(lane, region)

    async def transfer_owner(self, lane: discord.VoiceChannel, member_id: int):
        """Wrapper: Owner-Claim / Transfer (ohne Zeitfenster)."""
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
            if desired_name is not None and lane.name != desired_name:
                last_desired = self._last_name_desired.get(lane.id)
                if last_desired == desired_name:
                    last_ts = self._last_name_patch_ts.get(lane.id, 0.0)
                    if now - last_ts >= NAME_EDIT_COOLDOWN_SEC:
                        kwargs["name"] = desired_name
                else:
                    kwargs["name"] = desired_name
                self._last_name_desired[lane.id] = desired_name

            if desired_limit is not None and desired_limit != lane.user_limit:
                kwargs["user_limit"] = max(0, min(99, desired_limit))

            if not kwargs:
                return

            try:
                await lane.edit(**kwargs, reason=reason or "TempVoice: Update")
                if "name" in kwargs:
                    self._last_name_patch_ts[lane.id] = now
            except discord.HTTPException:
                pass

    def _compose_name(self, lane: discord.VoiceChannel) -> str:
        base = self.lane_base.get(lane.id) or _strip_suffixes(lane.name)
        parts = [base]
        if lane.category_id == MINRANK_CATEGORY_ID:
            min_rank = self.lane_min_rank.get(lane.id, "unknown")
            if (min_rank and min_rank != "unknown" and
                    _rank_index(min_rank) >= _rank_index(SUFFIX_THRESHOLD_RANK)):
                parts.append(f" • ab {min_rank.capitalize()}")
        return "".join(parts)

    async def _refresh_name(self, lane: discord.VoiceChannel):
        await self._safe_edit_channel(
            lane,
            desired_name=self._compose_name(lane),
            reason="TempVoice: Name aktualisiert"
        )

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
                except Exception:
                    pass
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
            except Exception:
                pass

    async def _apply_min_rank(self, lane: discord.VoiceChannel, min_rank: str):
        if lane.category_id != MINRANK_CATEGORY_ID:
            return
        guild = lane.guild
        ranks = _rank_roles(guild)

        if min_rank == "unknown":
            for role in ranks.values():
                ow = lane.overwrites_for(role)
                if ow.connect is not None:
                    try:
                        await lane.set_permissions(role, overwrite=None, reason="TempVoice: MinRank reset")
                    except Exception:
                        pass
                    await asyncio.sleep(0.02)
            return

        min_idx = _rank_index(min_rank)
        for name, role in ranks.items():
            idx = _rank_index(name)
            if idx < min_idx:
                try:
                    ow = lane.overwrites_for(role); ow.connect = False
                    await lane.set_permissions(role, overwrite=ow, reason="TempVoice: MinRank deny")
                except Exception:
                    pass
            else:
                ow = lane.overwrites_for(role)
                if ow.connect is not None:
                    try:
                        await lane.set_permissions(role, overwrite=None, reason="TempVoice: MinRank clear")
                    except Exception:
                        pass
            await asyncio.sleep(0.02)

    # --------- Lane-Erstellung ---------
    async def _create_lane(self, member: discord.Member, staging: discord.VoiceChannel):
        guild = member.guild
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
        except Exception:
            pass

        # Owner-Prefs (Region) anwenden
        region = await self.get_region_pref(member.id)
        await self.apply_region(lane, region)

        await self._apply_owner_bans(lane, member.id)

        try:
            await member.move_to(lane, reason="TempVoice: Auto-Lane erstellt")
        except Exception:
            pass

        await self._refresh_name(lane)

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
                        self.lane_owner[ch.id] = candidates[0].id
                        try:
                            await self._tvdb.exec(
                                "UPDATE tempvoice_lanes SET owner_id=? WHERE channel_id=?",
                                (int(self.lane_owner[ch.id]), int(ch.id))
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            await ch.delete(reason="TempVoice: Lane leer")
                        except Exception:
                            pass
                        try:
                            await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (int(ch.id),))
                        except Exception:
                            pass
                        self.created_channels.discard(ch.id)
                        for d in (self.lane_owner, self.lane_base, self.lane_min_rank, self.join_time,
                                  self._last_name_desired, self._last_name_patch_ts):
                            d.pop(ch.id, None)
        except Exception:
            pass

        # Join-Zeit & Bannprüfung
        try:
            if after and after.channel and isinstance(after.channel, discord.VoiceChannel):
                ch = after.channel
                self.join_time.setdefault(ch.id, {})
                self.join_time[ch.id][member.id] = datetime.utcnow().timestamp()

                owner_id = self.lane_owner.get(ch.id)
                if owner_id and await self.bans.is_banned_by_owner(owner_id, member.id):
                    # In verfügbaren Staging verschieben (erste passende ID)
                    staging = None
                    for cid in STAGING_CHANNEL_IDS:
                        s = ch.guild.get_channel(cid)
                        if isinstance(s, discord.VoiceChannel):
                            staging = s
                            break
                    if staging:
                        try:
                            await member.move_to(staging, reason="Owner-Ban aktiv")
                        except Exception:
                            pass

                if _is_managed_lane(ch):
                    await self._refresh_name(ch)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoiceCore(bot))
