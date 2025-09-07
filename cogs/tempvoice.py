import discord
from discord.ext import commands
import asyncio, logging, time, re
from typing import Optional, Dict, Set, Tuple, List
from datetime import datetime
import aiosqlite
from utils.deadlock_db import DB_PATH

logger = logging.getLogger(__name__)

STAGING_CHANNEL_IDS = {
    1330278323145801758,
    1357422958544420944,
    1412804671432818890,
}
MINRANK_CATEGORY_ID = 1412804540994162789
RANKED_CATEGORY_ID  = 1357422957017698478
INTERFACE_TEXT_CHANNEL_ID = 1371927143537315890
ENGLISH_ONLY_ROLE_ID = 1309741866098491479

DEFAULT_CASUAL_CAP = 8
DEFAULT_RANKED_CAP = 6
NAME_EDIT_COOLDOWN_SEC = 120
STARTUP_PURGE_DELAY_SEC = 3

RANK_ORDER = [
    "unknown","initiate","seeker","alchemist","arcanist",
    "ritualist","emissary","archon","oracle","phantom",
    "ascendant","eternus"
]
RANK_SET = set(RANK_ORDER)
SUFFIX_THRESHOLD_RANK = "emissary"

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

def _find_rank_emoji(guild: Optional[discord.Guild], rank: str):
    if not guild: return None
    return discord.utils.get(guild.emojis, name=rank)

def _is_managed_lane(ch: Optional[discord.VoiceChannel]) -> bool:
    return isinstance(ch, discord.VoiceChannel) and ch.name.startswith("Lane ")

def _default_cap(ch: discord.abc.GuildChannel) -> int:
    cat_id = getattr(ch, "category_id", None)
    return DEFAULT_RANKED_CAP if cat_id == RANKED_CATEGORY_ID else DEFAULT_CASUAL_CAP

def _strip_suffixes(current: str) -> str:
    base = current
    for marker in (" • ab ",):
        if marker in base:
            base = base.split(marker)[0]
    return base

class TVDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA synchronous=NORMAL")
        await self.create_tables()

    async def create_tables(self):
        await self.db.execute("""
            CREATE TABLE IF NOT EXISTS tempvoice_bans (
                owner_id    INTEGER NOT NULL,
                banned_id   INTEGER NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (owner_id, banned_id)
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
            CREATE TABLE IF NOT EXISTS tempvoice_staging_channels (
                guild_id    INTEGER NOT NULL,
                channel_id  INTEGER NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, channel_id)
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
        await self.db.commit()

    async def fetchone(self, q: str, p: tuple = ()):
        cur = await self.db.execute(q, p)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, q: str, p: tuple = ()):
        cur = await self.db.execute(q, p)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def exec(self, q: str, p: tuple = ()):
        await self.db.execute(q, p)
        await self.db.commit()

    async def close(self):
        if self.db:
            try: await self.db.close()
            except: pass

class AsyncBanStore:
    def __init__(self, db: TVDB): self.db = db
    async def is_banned_by_owner(self, owner_id: int, user_id: int) -> bool:
        row = await self.db.fetchone("SELECT 1 FROM tempvoice_bans WHERE owner_id=? AND banned_id=?", (int(owner_id), int(user_id)))
        return row is not None
    async def list_bans(self, owner_id: int) -> List[int]:
        rows = await self.db.fetchall("SELECT banned_id FROM tempvoice_bans WHERE owner_id=?", (int(owner_id),))
        return [int(r["banned_id"]) for r in rows]
    async def add_ban(self, owner_id: int, user_id: int):
        await self.db.exec("INSERT OR IGNORE INTO tempvoice_bans(owner_id, banned_id) VALUES(?,?)", (int(owner_id), int(user_id)))
    async def remove_ban(self, owner_id: int, user_id: int):
        await self.db.exec("DELETE FROM tempvoice_bans WHERE owner_id=? AND banned_id=?", (int(owner_id), int(user_id)))

class TempVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._tvdb = TVDB(str(DB_PATH))
        self.bans = AsyncBanStore(self._tvdb)
        self.created_channels: Set[int] = set()
        self.lane_owner: Dict[int, int] = {}
        self.lane_base: Dict[int, str] = {}
        self.lane_min_rank: Dict[int, str] = {}
        self.join_time: Dict[int, Dict[int, float]] = {}
        self._edit_locks: Dict[int, asyncio.Lock] = {}
        self._last_name_desired: Dict[int, str] = {}
        self._last_name_patch_ts: Dict[int, float] = {}

    async def cog_load(self):
        await self._tvdb.connect()
        guild = self._first_guild()
        gid = guild.id if guild else 0
        for scid in STAGING_CHANNEL_IDS:
            await self._tvdb.exec("INSERT OR IGNORE INTO tempvoice_staging_channels(guild_id, channel_id) VALUES(?,?)", (gid, int(scid)))
        self.bot.add_view(MainView(self))
        asyncio.create_task(self._startup())

    async def cog_unload(self):
        try: await self._tvdb.close()
        except: pass

    async def _startup(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(STARTUP_PURGE_DELAY_SEC)
        await self._ensure_interface()
        await self._startup_cleanup()

    def _first_guild(self) -> Optional[discord.Guild]:
        return self.bot.guilds[0] if self.bot.guilds else None

    async def _ensure_interface(self):
        ch = self.bot.get_channel(INTERFACE_TEXT_CHANNEL_ID)
        if not isinstance(ch, discord.TextChannel):
            logger.warning("Interface-Channel %s nicht gefunden.", INTERFACE_TEXT_CHANNEL_ID)
            return
        saved = await self._tvdb.fetchone("SELECT channel_id, message_id FROM tempvoice_interface WHERE guild_id=?", (int(ch.guild.id),))
        embed = discord.Embed(
            title="TempVoice Interface",
            description=(
                "• Join einen Staging-Channel → deine Lane wird erstellt und du wirst gemoved.\n"
                "• Steuerung hier:\n"
                "  - 🇩🇪/🇪🇺 Sprachfilter (Rolle „English Only“)\n"
                "  - Mindest-Rang (nur in spezieller Kategorie)\n"
                "  - 👢 Kick / 🚫 Ban / ♻️ Unban\n"
                "  - 🎚️ Limit setzen"
            ),
            color=0x2ecc71
        )
        embed.set_footer(text="Deadlock DACH • TempVoice")
        if saved:
            try:
                use_ch = self.bot.get_channel(int(saved["channel_id"])) or ch
                if isinstance(use_ch, discord.TextChannel):
                    msg = await use_ch.fetch_message(int(saved["message_id"]))
                    await msg.edit(embed=embed, view=MainView(self))
                    return
            except Exception:
                logger.info("Interface-Message fehlt – neu erstellen.")
        try:
            msg = await ch.send(embed=embed, view=MainView(self))
            await self._tvdb.exec(
                "INSERT INTO tempvoice_interface(guild_id, channel_id, message_id, updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id, updated_at=CURRENT_TIMESTAMP",
                (int(ch.guild.id), int(ch.id), int(msg.id))
            )
        except Exception as e:
            logger.warning(f"Interface posten fehlgeschlagen: {e}")

    async def _startup_cleanup(self):
        guild = self._first_guild()
        if not guild: return
        rows = await self._tvdb.fetchall("SELECT channel_id FROM tempvoice_staging_channels WHERE guild_id=?", (int(guild.id),))
        for r in rows:
            cid = int(r["channel_id"])
            ch = guild.get_channel(cid)
            if not isinstance(ch, discord.VoiceChannel):
                await self._tvdb.exec("DELETE FROM tempvoice_staging_channels WHERE guild_id=? AND channel_id=?", (int(guild.id), cid))
        rows = await self._tvdb.fetchall("SELECT channel_id FROM tempvoice_lanes WHERE guild_id=?", (int(guild.id),))
        for r in rows:
            lane_id = int(r["channel_id"])
            lane = guild.get_channel(lane_id)
            if not isinstance(lane, discord.VoiceChannel):
                await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (lane_id,))
                continue
            if len(lane.members) == 0:
                try: await lane.delete(reason="TempVoice: Startup-Cleanup (leer)")
                except: pass
                await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (lane_id,))

    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        lock = self._edit_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._edit_locks[channel_id] = lock
        return lock

    async def _safe_edit_channel(self, lane: discord.VoiceChannel, *, desired_name: Optional[str] = None, desired_limit: Optional[int] = None, reason: Optional[str] = None):
        lock = self._lock_for(lane.id)
        async with lock:
            kwargs = {}
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
            if not kwargs: return
            try:
                await lane.edit(**kwargs, reason=reason or "TempVoice: Update")
                if "name" in kwargs: self._last_name_patch_ts[lane.id] = now
            except discord.HTTPException as e:
                logger.warning(f"channel.edit {lane.id} failed: {e}")

    def _compose_name(self, lane: discord.VoiceChannel) -> str:
        base = self.lane_base.get(lane.id) or _strip_suffixes(lane.name)
        parts = [base]
        if lane.category_id == MINRANK_CATEGORY_ID:
            min_rank = self.lane_min_rank.get(lane.id, "unknown")
            if min_rank and min_rank != "unknown" and _rank_index(min_rank) >= _rank_index(SUFFIX_THRESHOLD_RANK):
                parts.append(f"• ab {min_rank.capitalize()}")
        return " ".join(parts)

    async def _refresh_name(self, lane: discord.VoiceChannel):
        await self._safe_edit_channel(lane, desired_name=self._compose_name(lane), reason="TempVoice: Name aktualisiert")

    async def _next_name(self, category: Optional[discord.CategoryChannel], prefix: str) -> str:
        if not category: return f"{prefix} 1"
        used: Set[int] = set()
        pat = re.compile(rf"^{re.escape(prefix)}\s+(\d+)\b")
        for c in category.voice_channels:
            m = pat.match(c.name)
            if m:
                try: used.add(int(m.group(1)))
                except: pass
        n = 1
        while n in used: n += 1
        return f"{prefix} {n}"

    async def _apply_owner_bans(self, lane: discord.VoiceChannel, owner_id: int):
        banned = await self.bans.list_bans(owner_id)
        for uid in banned:
            try:
                obj = lane.guild.get_member(int(uid)) or discord.Object(id=int(uid))
                ow = lane.overwrites_for(obj); ow.connect = False
                await lane.set_permissions(obj, overwrite=ow, reason="Owner-Ban (persistent)")
                await asyncio.sleep(0.02)
            except: pass

    async def _apply_min_rank(self, lane: discord.VoiceChannel, min_rank: str):
        if lane.category_id != MINRANK_CATEGORY_ID: return
        ranks = _rank_roles(lane.guild)
        if min_rank == "unknown":
            for role in ranks.values():
                ow = lane.overwrites_for(role)
                if ow.connect is not None:
                    try: await lane.set_permissions(role, overwrite=None, reason="TempVoice: MinRank reset")
                    except: pass
                    await asyncio.sleep(0.02)
            return
        min_idx = _rank_index(min_rank)
        for name, role in ranks.items():
            idx = _rank_index(name)
            if idx < min_idx:
                try:
                    ow = lane.overwrites_for(role); ow.connect = False
                    await lane.set_permissions(role, overwrite=ow, reason="TempVoice: MinRank deny")
                except: pass
            else:
                ow = lane.overwrites_for(role)
                if ow.connect is not None:
                    try: await lane.set_permissions(role, overwrite=None, reason="TempVoice: MinRank clear")
                    except: pass
            await asyncio.sleep(0.02)

    async def _create_lane(self, member: discord.Member, staging: discord.VoiceChannel):
        guild = member.guild
        cat = staging.category
        base = await self._next_name(cat, "Lane")
        bitrate = getattr(guild, "bitrate_limit", None) or 256000
        cap = _default_cap(staging)
        initial_name = base
        try:
            lane = await guild.create_voice_channel(
                name=initial_name, category=cat, user_limit=cap, bitrate=bitrate,
                reason=f"Auto-Lane für {member.display_name}", overwrites=cat.overwrites if cat else None
            )
        except discord.Forbidden:
            logger.error("Fehlende Rechte: VoiceChannel erstellen."); return
        except Exception as e:
            logger.error(f"create_lane error: {e}"); return
        self.created_channels.add(lane.id)
        self.lane_owner[lane.id] = member.id
        self.lane_base[lane.id] = base
        self.lane_min_rank[lane.id] = "unknown"
        self.join_time.setdefault(lane.id, {})
        await self._tvdb.exec(
            "INSERT OR REPLACE INTO tempvoice_lanes(channel_id, guild_id, owner_id, base_name, category_id) VALUES(?,?,?,?,?)",
            (int(lane.id), int(guild.id), int(member.id), base, int(cat.id) if cat else 0)
        )
        await self._apply_owner_bans(lane, member.id)
        try: await member.move_to(lane, reason="TempVoice: Auto-Lane erstellt")
        except: pass
        await self._refresh_name(lane)
        logger.info(f"Auto-Lane erstellt: {lane.name} (owner={member.id}, cap={cap}, bitrate={bitrate})")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        try:
            if after and after.channel and isinstance(after.channel, discord.VoiceChannel):
                if after.channel.id in STAGING_CHANNEL_IDS:
                    await self._create_lane(member, after.channel)
        except Exception as e:
            logger.warning(f"Auto-lane create failed: {e}")
        try:
            if before and before.channel and isinstance(before.channel, discord.VoiceChannel):
                ch = before.channel
                if ch.id in self.join_time: self.join_time[ch.id].pop(member.id, None)
                if ch.id in self.lane_owner and self.lane_owner[ch.id] == member.id:
                    if len(ch.members) > 0:
                        tsmap = self.join_time.get(ch.id, {})
                        candidates = list(ch.members)
                        candidates.sort(key=lambda m: tsmap.get(m.id, float("inf")))
                        self.lane_owner[ch.id] = candidates[0].id
                    else:
                        try: await ch.delete(reason="TempVoice: Lane leer")
                        except: pass
                        await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (int(ch.id),))
                        self.created_channels.discard(ch.id)
                        for d in (self.lane_owner, self.lane_base, self.lane_min_rank, self.join_time,
                                  self._last_name_desired, self._last_name_patch_ts):
                            d.pop(ch.id, None)
        except Exception: pass
        try:
            if after and after.channel and isinstance(after.channel, discord.VoiceChannel):
                ch = after.channel
                self.join_time.setdefault(ch.id, {})
                self.join_time[ch.id][member.id] = datetime.utcnow().timestamp()
                if _is_managed_lane(ch): await self._refresh_name(ch)
        except Exception: pass

class MainView(discord.ui.View):
    def __init__(self, cog: TempVoiceCog):
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(RegionDEButton(cog))
        self.add_item(RegionEUButton(cog))
        self.add_item(LimitButton(cog))
        self.add_item(MinRankSelect(cog))
        self.add_item(KickButton(cog))
        self.add_item(BanButton(cog))
        self.add_item(UnbanButton(cog))

class RegionDEButton(discord.ui.Button):
    def __init__(self, cog: TempVoiceCog):
        super().__init__(label="🇩🇪 DE", style=discord.ButtonStyle.primary, row=0, custom_id="tv_region_de")
        self.cog = cog
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = m.voice.channel if (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)) else None
        if not isinstance(lane, discord.VoiceChannel) or not _is_managed_lane(lane):
            return await itx.response.send_message("Tritt zuerst deiner Lane bei.", ephemeral=True)
        owner_id = self.cog.lane_owner.get(lane.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            return await itx.response.send_message("Nur Owner/Mods dürfen den Sprachfilter ändern.", ephemeral=True)
        role = lane.guild.get_role(ENGLISH_ONLY_ROLE_ID)
        if not role:
            return await itx.response.send_message("Rolle „English Only“ nicht gefunden.", ephemeral=True)
        cur = lane.overwrites_for(role)
        if cur.connect is False:
            return await itx.response.send_message("Schon Deutsch-Only.", ephemeral=True)
        cur.connect = False
        try: await lane.set_permissions(role, overwrite=cur, reason="TempVoice: Deutsch-Only")
        except: pass
        await itx.response.send_message("Deutsch-Only aktiv.", ephemeral=True)

class RegionEUButton(discord.ui.Button):
    def __init__(self, cog: TempVoiceCog):
        super().__init__(label="🇪🇺 EU", style=discord.ButtonStyle.secondary, row=0, custom_id="tv_region_eu")
        self.cog = cog
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = m.voice.channel if (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)) else None
        if not isinstance(lane, discord.VoiceChannel) or not _is_managed_lane(lane):
            return await itx.response.send_message("Tritt zuerst deiner Lane bei.", ephemeral=True)
        owner_id = self.cog.lane_owner.get(lane.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            return await itx.response.send_message("Nur Owner/Mods dürfen den Sprachfilter ändern.", ephemeral=True)
        role = lane.guild.get_role(ENGLISH_ONLY_ROLE_ID)
        if not role:
            return await itx.response.send_message("Rolle „English Only“ nicht gefunden.", ephemeral=True)
        try: await lane.set_permissions(role, overwrite=None, reason="TempVoice: Sprachfilter frei")
        except: pass
        await itx.response.send_message("Sprachfilter aufgehoben.", ephemeral=True)

class LimitButton(discord.ui.Button):
    def __init__(self, cog: TempVoiceCog):
        super().__init__(label="🎚️ Limit setzen", style=discord.ButtonStyle.secondary, row=0, custom_id="tv_limit_btn")
        self.cog = cog
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = m.voice.channel if (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)) else None
        if not isinstance(lane, discord.VoiceChannel) or not _is_managed_lane(lane):
            return await itx.response.send_message("Tritt zuerst deiner Lane bei.", ephemeral=True)
        owner_id = self.cog.lane_owner.get(lane.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            return await itx.response.send_message("Nur Owner/Mods dürfen das Limit setzen.", ephemeral=True)
        await itx.response.send_modal(LimitModal(self.cog, lane))

class LimitModal(discord.ui.Modal, title="Limit setzen"):
    value = discord.ui.TextInput(label="Limit (0-99)", placeholder="z.B. 6", required=True, max_length=2)
    def __init__(self, cog: TempVoiceCog, lane: discord.VoiceChannel):
        super().__init__(timeout=120); self.cog = cog; self.lane = lane
    async def on_submit(self, itx: discord.Interaction):
        txt = str(self.value.value).strip()
        try: val = int(txt)
        except ValueError: return await itx.response.send_message("Bitte Zahl (0-99) eingeben.", ephemeral=True)
        if val < 0 or val > 99: return await itx.response.send_message("Limit muss 0-99 sein.", ephemeral=True)
        try: await itx.response.defer(ephemeral=True, thinking=False)
        except: pass
        await self.cog._safe_edit_channel(self.lane, desired_limit=val, reason="TempVoice: Limit gesetzt")
        await self.cog._refresh_name(self.lane)
        try: await itx.followup.send(f"Limit auf {val} gesetzt.", ephemeral=True)
        except: pass

class MinRankSelect(discord.ui.Select):
    def __init__(self, cog: TempVoiceCog):
        self.cog = cog
        guild = None
        ch = cog.bot.get_channel(INTERFACE_TEXT_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel): guild = ch.guild
        options = []
        options.append(discord.SelectOption(label="Kein Limit (Jeder)", value="unknown", emoji=_find_rank_emoji(guild,"unknown") or "✅"))
        for r in RANK_ORDER[1:]:
            options.append(discord.SelectOption(label=r.capitalize(), value=r, emoji=_find_rank_emoji(guild,r)))
        super().__init__(placeholder="Mindest-Rang (nur in spezieller Kategorie)", min_values=1, max_values=1, options=options, row=1, custom_id="tv_minrank")
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        if not (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)):
            return await itx.response.send_message("Tritt zuerst deiner Lane bei.", ephemeral=True)
        lane: discord.VoiceChannel = m.voice.channel
        if lane.category_id != MINRANK_CATEGORY_ID:
            return await itx.response.send_message("Mindest-Rang ist hier deaktiviert.", ephemeral=True)
        choice = self.values[0]
        try: await itx.response.defer(ephemeral=True, thinking=False)
        except: pass
        self.cog.lane_min_rank[lane.id] = choice
        await self.cog._apply_min_rank(lane, choice)
        await self.cog._refresh_name(lane)

class KickButton(discord.ui.Button):
    def __init__(self, cog: TempVoiceCog):
        super().__init__(label="👢 Kick", style=discord.ButtonStyle.secondary, row=2, custom_id="tv_kick")
        self.cog = cog
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = m.voice.channel if (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)) else None
        if not isinstance(lane, discord.VoiceChannel) or not _is_managed_lane(lane):
            return await itx.response.send_message("Du musst in einer Lane sein.", ephemeral=True)
        owner_id = self.cog.lane_owner.get(lane.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            return await itx.response.send_message("Nur Owner/Mods dürfen kicken.", ephemeral=True)
        options = [discord.SelectOption(label=u.display_name, value=str(u.id)) for u in lane.members if u.id != m.id]
        if not options: return await itx.response.send_message("Niemand zum Kicken vorhanden.", ephemeral=True)
        view = KickSelectView(self.cog, lane, options)
        try: await itx.response.send_message("Wen möchtest du kicken?", view=view, ephemeral=True)
        except discord.InteractionResponded: await itx.followup.send("Wen möchtest du kicken?", view=view, ephemeral=True)

class BanButton(discord.ui.Button):
    def __init__(self, cog: TempVoiceCog):
        super().__init__(label="🚫 Ban", style=discord.ButtonStyle.danger, row=2, custom_id="tv_ban")
        self.cog = cog
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = m.voice.channel if (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)) else None
        if not isinstance(lane, discord.VoiceChannel) or not _is_managed_lane(lane):
            return await itx.response.send_message("Du musst in einer Lane sein.", ephemeral=True)
        owner_id = self.cog.lane_owner.get(lane.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            return await itx.response.send_message("Nur Owner/Mods dürfen bannen.", ephemeral=True)
        await itx.response.send_modal(BanModal(self.cog, lane, action="ban"))

class UnbanButton(discord.ui.Button):
    def __init__(self, cog: TempVoiceCog):
        super().__init__(label="♻️ Unban", style=discord.ButtonStyle.primary, row=2, custom_id="tv_unban")
        self.cog = cog
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = m.voice.channel if (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)) else None
        if not isinstance(lane, discord.VoiceChannel) or not _is_managed_lane(lane):
            return await itx.response.send_message("Du musst in einer Lane sein.", ephemeral=True)
        owner_id = self.cog.lane_owner.get(lane.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            return await itx.response.send_message("Nur Owner/Mods dürfen entbannen.", ephemeral=True)
        await itx.response.send_modal(BanModal(self.cog, lane, action="unban"))

class KickSelect(discord.ui.Select):
    def __init__(self, options, placeholder="Mitglied wählen …"):
        super().__init__(min_values=1, max_values=1, options=options, placeholder=placeholder)
    async def callback(self, itx: discord.Interaction):
        view: "KickSelectView" = self.view  # type: ignore
        await view.handle_kick(itx, int(self.values[0]))

class KickSelectView(discord.ui.View):
    def __init__(self, cog: TempVoiceCog, lane: discord.VoiceChannel, options):
        super().__init__(timeout=60); self.cog = cog; self.lane = lane; self.add_item(KickSelect(options))
    async def handle_kick(self, itx: discord.Interaction, target_id: int):
        target = self.lane.guild.get_member(target_id)
        if not target or not target.voice or target.voice.channel != self.lane:
            return await itx.response.send_message("User ist nicht (mehr) in der Lane.", ephemeral=True)
        staging = None
        for cid in STAGING_CHANNEL_IDS:
            ch = self.lane.guild.get_channel(cid)
            if isinstance(ch, discord.VoiceChannel): staging = ch; break
        if not staging: return await itx.response.send_message("Staging-Channel nicht gefunden.", ephemeral=True)
        try:
            await target.move_to(staging, reason=f"Kick durch {itx.user}")
            await itx.response.send_message(f"👢 {target.display_name} → {staging.name}.", ephemeral=True)
        except: await itx.response.send_message("Konnte nicht verschieben.", ephemeral=True)

class BanModal(discord.ui.Modal, title="User (Un)Ban"):
    target = discord.ui.TextInput(label="User (@Mention ODER numerische ID)", placeholder="@Name oder 123456789012345678", required=True, max_length=64)
    def __init__(self, cog: TempVoiceCog, lane: discord.VoiceChannel, action: str):
        super().__init__(timeout=120); self.cog = cog; self.lane = lane; self.action = action
    async def on_submit(self, itx: discord.Interaction):
        user: discord.Member = itx.user  # type: ignore
        owner_id = self.cog.lane_owner.get(self.lane.id)
        perms = self.lane.permissions_for(user)
        if not (owner_id == user.id or perms.manage_channels or perms.administrator):
            return await itx.response.send_message("Nur Owner/Mods dürfen (un)bannen.", ephemeral=True)
        raw = str(self.target.value).strip()
        uid = None
        if raw.startswith("<@") and raw.endswith(">"):
            digits = "".join(ch for ch in raw if ch.isdigit())
            if digits: uid = int(digits)
        elif raw.isdigit(): uid = int(raw)
        if not uid: return await itx.response.send_message("Bitte @Mention ODER numerische ID angeben.", ephemeral=True)
        guild = self.lane.guild
        target_member = guild.get_member(uid)
        try: await itx.response.defer(ephemeral=True, thinking=False)
        except: pass
        if self.action == "ban":
            await self.cog.bans.add_ban(owner_id, uid)
            try:
                await self.lane.set_permissions(target_member or discord.Object(id=uid), connect=False, reason=f"Owner-Ban durch {user}")
                if target_member and target_member.voice and target_member.voice.channel == self.lane:
                    staging = None
                    for cid in STAGING_CHANNEL_IDS:
                        ch = guild.get_channel(cid)
                        if isinstance(ch, discord.VoiceChannel): staging = ch; break
                    if staging:
                        try: await target_member.move_to(staging, reason="Owner-Ban")
                        except: pass
                await itx.followup.send("Nutzer gebannt (owner-persistent).", ephemeral=True)
            except: await itx.followup.send("Konnte Ban nicht setzen.", ephemeral=True)
        else:
            await self.cog.bans.remove_ban(owner_id, uid)
            try:
                await self.lane.set_permissions(target_member or discord.Object(id=uid), overwrite=None, reason=f"Owner-Unban durch {user}")
                await itx.followup.send("Nutzer entbannt.", ephemeral=True)
            except: await itx.followup.send("Konnte Unban nicht setzen.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoiceCog(bot))
