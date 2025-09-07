# ------------------------------------------------------------
# TempVoice – Auto-Lanes + UI-Management (ohne Match/Voll-Features)
# Persistentes Interface (merkt sich Message-ID)
# DB-zentral: Interface, Owner-Bans, Staging-Channels, Lanes
#
# Änderungen ggü. Vorversion:
#  - ❌ Entfernt: Voll/Nicht-voll Buttons & Match Start/Stop & Timer
#  - ✅ Limit-Änderung per Button + Modal (Zahleingabe)
#  - ✅ Min-Rank NUR für MINRANK_CATEGORY_ID aktiv; sonst deaktiviert
#  - ✅ Neuer zusätzlicher Staging-Channel (STAGING_CHANNEL_IDS)
#  - ✅ Reihenfolge/Rows der Komponenten so angepasst, dass kein Row-Width-Fehler auftritt
#  - ✅ Rank-Emoji-Anzeige (falls :rang: Emojis existieren)
#  - ✅ Startup-Cleanup: verwaiste/fehlende Channels aus DB räumen; leere Lanes löschen
# ------------------------------------------------------------

import discord
from discord.ext import commands
import asyncio
import logging
import time
import re
from typing import Optional, Dict, Set, Tuple, List
from datetime import datetime

import aiosqlite
from utils.deadlock_db import DB_PATH  # zentrale DB

logger = logging.getLogger(__name__)

# ============ FESTE IDs (vom User vorgegeben) ============
# Staging-Voice-Channels: Join => Lane wird erstellt
STAGING_CHANNEL_IDS = {
    1330278323145801758,  # Casual Staging
    1357422958544420944,  # Ranked Staging (Voice)
    1412804671432818890,  # NEU: zusätzlicher Staging-Channel (Voice)
}

# Kategorie, in der MinRank-Feature AKTIV sein darf
MINRANK_CATEGORY_ID = 1412804540994162789

# Ranked-Kategorie (nur für einige Defaults/Suffix-Logik)
RANKED_CATEGORY_ID  = 1357422957017698478

# Interface-Textkanal (fix)
INTERFACE_TEXT_CHANNEL_ID = 1371927143537315890

# LFG-Textkanal für Such-Posts (optional, falls vorhanden)
LFG_TEXT_CHANNEL_ID       = 1376335502919335936

# "English Only"-Rolle für Sprachfilter (Region)
ENGLISH_ONLY_ROLE_ID      = 1309741866098491479

# ============ KONFIG ============
DEFAULT_CASUAL_CAP        = 8
DEFAULT_RANKED_CAP        = 6
FULL_HINT_THRESHOLD       = 6

NAME_EDIT_COOLDOWN_SEC    = 120
LFG_POST_COOLDOWN_SEC     = 60
LFG_DELETE_AFTER_SEC      = 20 * 60
BUTTON_COOLDOWN_SEC       = 30
DEBOUNCE_VERML_VOLL_SEC   = 25

STARTUP_PURGE_DELAY_SEC   = 3     # kleiner Delay nach on_ready
# =================================

RANK_ORDER = [
    "unknown", "initiate", "seeker", "alchemist", "arcanist",
    "ritualist", "emissary", "archon", "oracle", "phantom",
    "ascendant", "eternus"
]
RANK_SET = set(RANK_ORDER)
SUFFIX_THRESHOLD_RANK = "emissary"

# ===================== Hilfsfunktionen =====================

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

def _find_rank_emoji(guild: Optional[discord.Guild], rank: str) -> Optional[discord.PartialEmoji]:
    """Sucht ein Custom-Emoji mit exakt dem Rang-Namen (:rank:)."""
    if not guild:
        return None
    return discord.utils.get(guild.emojis, name=rank)

def _is_managed_lane(ch: Optional[discord.VoiceChannel]) -> bool:
    return isinstance(ch, discord.VoiceChannel) and ch.name.startswith("Lane ")

def _default_cap(ch: discord.VoiceChannel) -> int:
    return DEFAULT_RANKED_CAP if ch.category_id == RANKED_CATEGORY_ID else DEFAULT_CASUAL_CAP

def _strip_suffixes(current: str) -> str:
    base = current
    for marker in (" • ab ", " • Spieler gesucht", " • Wartend"):
        if marker in base:
            base = base.split(marker)[0]
    return base

# ===================== DB-Layer (zentral) =====================

class TVDB:
    """DB-Layer für TempVoice (Interface, Owner-Bans, Staging, Lanes)."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute('PRAGMA journal_mode=WAL')
        await self.db.execute('PRAGMA synchronous=NORMAL')
        await self.create_tables()

    async def create_tables(self):
        # Owner-Bans (pro Owner mehrere User)
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS tempvoice_bans (
                owner_id    INTEGER NOT NULL,
                banned_id   INTEGER NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (owner_id, banned_id)
            )
        ''')

        # Interface-State pro Guild
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS tempvoice_interface (
                guild_id    INTEGER PRIMARY KEY,
                channel_id  INTEGER NOT NULL,
                message_id  INTEGER NOT NULL,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Staging-Channels (damit beim Neustart geprüft werden kann)
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS tempvoice_staging_channels (
                guild_id    INTEGER NOT NULL,
                channel_id  INTEGER NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, channel_id)
            )
        ''')

        # Erstellte Lanes (für Cleanup nach Neustart)
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS tempvoice_lanes (
                channel_id  INTEGER PRIMARY KEY,
                guild_id    INTEGER NOT NULL,
                owner_id    INTEGER NOT NULL,
                base_name   TEXT NOT NULL,
                category_id INTEGER NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await self.db.commit()

    async def fetchone(self, q: str, p: tuple = ()):  # -> aiosqlite.Row | None
        cur = await self.db.execute(q, p)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, q: str, p: tuple = ()):  # -> list[aiosqlite.Row]
        cur = await self.db.execute(q, p)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def exec(self, q: str, p: tuple = ()):  # -> None
        await self.db.execute(q, p)
        await self.db.commit()

    async def close(self):
        if self.db:
            try:
                await self.db.close()
            except Exception:
                pass

class AsyncBanStore:
    """Owner-Ban-Store (persistiert in DB)."""
    def __init__(self, db: TVDB):
        self.db = db

    async def is_banned_by_owner(self, owner_id: int, user_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 FROM tempvoice_bans WHERE owner_id=? AND banned_id=?",
            (int(owner_id), int(user_id))
        )
        return row is not None

    async def list_bans(self, owner_id: int) -> List[int]:
        rows = await self.db.fetchall(
            "SELECT banned_id FROM tempvoice_bans WHERE owner_id=?",
            (int(owner_id),)
        )
        return [int(r["banned_id"]) for r in rows]

    async def add_ban(self, owner_id: int, user_id: int):
        await self.db.exec(
            "INSERT OR IGNORE INTO tempvoice_bans(owner_id, banned_id) VALUES(?,?)",
            (int(owner_id), int(user_id))
        )

    async def remove_ban(self, owner_id: int, user_id: int):
        await self.db.exec(
            "DELETE FROM tempvoice_bans WHERE owner_id=? AND banned_id=?",
            (int(owner_id), int(user_id))
        )

# ===================== TempVoice Cog =====================

class TempVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # DB
        self._tvdb = TVDB(str(DB_PATH))
        self.bans = AsyncBanStore(self._tvdb)

        # Laufzeit-State
        self.created_channels: Set[int] = set()        # Lane-IDs
        self.lane_owner: Dict[int, int] = {}           # lane_id -> owner_id
        self.lane_base: Dict[int, str] = {}            # lane_id -> "Lane N"
        self.lane_min_rank: Dict[int, str] = {}        # lane_id -> min rank
        self.join_time: Dict[int, Dict[int, float]] = {}    # lane_id -> {user_id: ts}

        self._edit_locks: Dict[int, asyncio.Lock] = {}
        self._last_name_desired: Dict[int, str] = {}
        self._last_name_patch_ts: Dict[int, float] = {}
        self._last_lfg_ts: Dict[int, float] = {}
        self._lfg_cleanup_tasks: Set[int] = set()
        self._last_button_ts: Dict[int, float] = {}
        self._debounce_tasks: Dict[int, asyncio.Task] = {}

    # -------- Lifecycle --------
    async def cog_load(self):
        await self._tvdb.connect()
        # Staging-Channels in DB sicherstellen
        guild = self._first_guild()
        gid = guild.id if guild else 0
        for scid in STAGING_CHANNEL_IDS:
            await self._tvdb.exec(
                "INSERT OR IGNORE INTO tempvoice_staging_channels(guild_id, channel_id) VALUES(?,?)",
                (gid, int(scid))
            )

        # Persistente UI registrieren
        self.bot.add_view(MainView(self))
        # Startup-Tasks
        asyncio.create_task(self._startup())

    async def cog_unload(self):
        try:
            await self._tvdb.close()
        except Exception:
            pass

    async def _startup(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(STARTUP_PURGE_DELAY_SEC)
        await self._ensure_interface()
        await self._startup_cleanup()

    def _first_guild(self) -> Optional[discord.Guild]:
        if self.bot.guilds:
            return self.bot.guilds[0]
        return None

    # -------- Interface --------
    async def _ensure_interface(self):
        ch = self.bot.get_channel(INTERFACE_TEXT_CHANNEL_ID)
        if not isinstance(ch, discord.TextChannel):
            logger.warning("Interface-Textkanal %s nicht gefunden/kein Textkanal.", INTERFACE_TEXT_CHANNEL_ID)
            return

        # gespeicherten Zustand laden
        saved = await self._tvdb.fetchone(
            "SELECT channel_id, message_id FROM tempvoice_interface WHERE guild_id=?",
            (int(ch.guild.id),)
        )

        embed = discord.Embed(
            title="Lanes & Steuerung (Casual/Ranked)",
            description=(
                "• **Join Staging (Casual/Ranked)** → es wird **automatisch** eine Lane erstellt und du wirst rüber gemoved.\n"
                "• **Steuerung hier im Interface**:\n"
                "  - **🇩🇪 / 🇪🇺** Sprachfilter (via Rolle *English Only*)\n"
                "  - **Mindest-Rang** (nur in spezieller Kategorie aktiv)\n"
                "  - **👢 Kick / 🚫 Ban / ♻️ Unban** (Ban/Unban per @ oder ID; Ban ist owner-persistent)\n"
                "  - **🎚️ Limit setzen** (Zahleingabe)\n\n"
                "👑 Owner wechselt automatisch an den, der am längsten in der Lane ist, wenn der Owner leavt."
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
                logger.info("Interface-Message nicht gefunden – wird neu erstellt.")

        try:
            msg = await ch.send(embed=embed, view=MainView(self))
            await self._tvdb.exec(
                (
                    "INSERT INTO tempvoice_interface(guild_id, channel_id, message_id, updated_at)\n"
                    "VALUES(?,?,?,CURRENT_TIMESTAMP)\n"
                    "ON CONFLICT(guild_id) DO UPDATE SET\n"
                    "  channel_id=excluded.channel_id,\n"
                    "  message_id=excluded.message_id,\n"
                    "  updated_at=CURRENT_TIMESTAMP"
                ),
                (int(ch.guild.id), int(ch.id), int(msg.id))
            )
        except Exception as e:
            logger.warning(f"Konnte Interface nicht posten: {e}")

    # -------- Startup-Cleanup --------
    async def _startup_cleanup(self):
        guild = self._first_guild()
        if not guild:
            return

        # Staging-Channels prüfen – nicht existierende löschen aus DB
        rows = await self._tvdb.fetchall(
            "SELECT channel_id FROM tempvoice_staging_channels WHERE guild_id=?",
            (int(guild.id),)
        )
        for r in rows:
            cid = int(r["channel_id"])
            ch = guild.get_channel(cid)
            if not isinstance(ch, discord.VoiceChannel):
                await self._tvdb.exec(
                    "DELETE FROM tempvoice_staging_channels WHERE guild_id=? AND channel_id=?",
                    (int(guild.id), cid)
                )

        # Lanes prüfen: fehlende -> DB löschen; leere existierende -> Channel löschen + DB löschen
        rows = await self._tvdb.fetchall(
            "SELECT channel_id FROM tempvoice_lanes WHERE guild_id=?",
            (int(guild.id),)
        )
        for r in rows:
            lane_id = int(r["channel_id"])
            lane = guild.get_channel(lane_id)
            if not isinstance(lane, discord.VoiceChannel):
                await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (lane_id,))
                continue
            if len(lane.members) == 0:
                try:
                    await lane.delete(reason="TempVoice: Startup-Cleanup (leer)")
                except Exception:
                    pass
                await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (lane_id,))

    # -------- Helper --------
    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        lock = self._edit_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._edit_locks[channel_id] = lock
        return lock

    async def _safe_edit_channel(
        self,
        lane: discord.VoiceChannel,
        *,
        desired_name: Optional[str] = None,
        desired_limit: Optional[int] = None,
        reason: Optional[str] = None,
    ):
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

            if not kwargs:
                return

            try:
                await lane.edit(**kwargs, reason=reason or "TempVoice: Update")
                if "name" in kwargs:
                    self._last_name_patch_ts[lane.id] = now
            except discord.HTTPException as e:
                logger.warning(f"channel.edit {lane.id} failed: {e}")

    def _compose_name(self, lane: discord.VoiceChannel) -> str:
        base = self.lane_base.get(lane.id) or _strip_suffixes(lane.name)
        parts = [base]

        # MinRank-Suffix NUR in erlaubter Kategorie
        if lane.category_id == MINRANK_CATEGORY_ID:
            min_rank = self.lane_min_rank.get(lane.id, "unknown")
            if min_rank and min_rank != "unknown" and _rank_index(min_rank) >= _rank_index(SUFFIX_THRESHOLD_RANK):
                parts.append(f"• ab {min_rank.capitalize()}")

        # Optionale Status-Suffixe (kein Match/Voll mehr)
        if "Spieler gesucht" not in base and lane.category_id != RANKED_CATEGORY_ID:
            parts.append("• Spieler gesucht")
        else:
            parts.append("• Wartend")

        return " ".join(parts)

    async def _refresh_name(self, lane: discord.VoiceChannel):
        desired = self._compose_name(lane)
        await self._safe_edit_channel(lane, desired_name=desired, reason="TempVoice: Name aktualisiert")

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
                except ValueError:
                    continue
        n = 1
        while n in used:
            n += 1
        return f"{prefix} {n}"

    async def _apply_owner_bans(self, lane: discord.VoiceChannel, owner_id: int):
        """Bei LANE-Create: alle Owner-Bans als connect=False setzen."""
        banned = await self.bans.list_bans(owner_id)
        for uid in banned:
            try:
                obj = lane.guild.get_member(int(uid)) or discord.Object(id=int(uid))
                ow = lane.overwrites_for(obj)
                ow.connect = False
                await lane.set_permissions(obj, overwrite=ow, reason="Owner-Ban (persistent)")
                await asyncio.sleep(0.02)
            except Exception:
                pass

    async def _post_lfg(self, lane: discord.VoiceChannel, *, force: bool = False):
        now = time.time()
        last = self._last_lfg_ts.get(lane.id, 0.0)
        if not force and now - last < LFG_POST_COOLDOWN_SEC:
            return
        self._last_lfg_ts[lane.id] = now

        lfg = lane.guild.get_channel(LFG_TEXT_CHANNEL_ID)
        if not isinstance(lfg, discord.TextChannel):
            return
        need = max(0, 6 - len(lane.members))
        txt = f"🔎 {lane.mention}: **Es werden noch Spieler gesucht** (+{need} bis 6)." if need > 0 else f"🔎 {lane.mention}: **Es werden noch Spieler gesucht**."
        try:
            msg = await lfg.send(txt)
            asyncio.create_task(self._delete_after(msg, LFG_DELETE_AFTER_SEC))
        except Exception:
            pass

    async def _delete_after(self, msg: discord.Message, seconds: int):
        if not isinstance(msg, discord.Message):
            return
        mid = msg.id
        if mid in self._lfg_cleanup_tasks:
            return
        self._lfg_cleanup_tasks.add(mid)
        try:
            await asyncio.sleep(seconds)
            try:
                await msg.delete()
            except Exception:
                pass
        finally:
            self._lfg_cleanup_tasks.discard(mid)

    def _schedule_vermutlich_voll(self, lane: discord.VoiceChannel):
        # noch genutzt, um Namen kurz nach Betreten zu aktualisieren
        t = self._debounce_tasks.get(lane.id)
        if t and not t.done():
            t.cancel()
        async def _job():
            try:
                await asyncio.sleep(DEBOUNCE_VERML_VOLL_SEC)
                if _is_managed_lane(lane):
                    await self._refresh_name(lane)
            except asyncio.CancelledError:
                return
            except Exception:
                pass
        self._debounce_tasks[lane.id] = asyncio.create_task(_job())

    # -------- Lane Create --------
    async def _create_lane(self, member: discord.Member, staging: discord.VoiceChannel):
        guild = member.guild
        cat = staging.category
        prefix = "Lane"
        base = await self._next_name(cat, prefix)

        bitrate = getattr(guild, "bitrate_limit", None) or 256000
        cap = _default_cap(staging)
        initial_name = f"{base} • Spieler gesucht" if (cat and cat.id != RANKED_CATEGORY_ID) else base

        lane: Optional[discord.VoiceChannel] = None
        try:
            lane = await guild.create_voice_channel(
                name=initial_name,
                category=cat,
                user_limit=cap,
                bitrate=bitrate,
                reason=f"Auto-Lane für {member.display_name}",
                overwrites=cat.overwrites if cat else None
            )
        except discord.Forbidden:
            logger.error("Fehlende Rechte: VoiceChannel erstellen.")
            return
        except Exception as e:
            logger.error(f"create_lane error: {e}")
            return

        # Runtime-Registrierung
        self.created_channels.add(lane.id)
        self.lane_owner[lane.id] = member.id
        self.lane_base[lane.id] = base
        self.lane_min_rank[lane.id] = "unknown"
        self.join_time.setdefault(lane.id, {})

        # DB-Registrierung (für Neustart-Cleanup)
        await self._tvdb.exec(
            "INSERT OR REPLACE INTO tempvoice_lanes(channel_id, guild_id, owner_id, base_name, category_id) VALUES(?,?,?,?,?)",
            (int(lane.id), int(guild.id), int(member.id), base, int(cat.id) if cat else 0)
        )

        # Owner-Bans anwenden
        await self._apply_owner_bans(lane, member.id)

        # Move & LFG
        try:
            await member.move_to(lane, reason="TempVoice: Auto-Lane erstellt")
        except Exception:
            pass
        await self._post_lfg(lane, force=True)
        logger.info(f"Auto-Lane erstellt: {lane.name} (owner={member.id}, cap={cap}, bitrate={bitrate})")

    # -------- Events --------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # 1) Join Staging => neue Lane
        try:
            if after and after.channel and isinstance(after.channel, discord.VoiceChannel):
                if after.channel.id in STAGING_CHANNEL_IDS:
                    await self._create_lane(member, after.channel)
        except Exception as e:
            logger.warning(f"Auto-lane create failed: {e}")

        # 2) Owner-Handover / Lane-Delete bei leer
        try:
            if before and before.channel and isinstance(before.channel, discord.VoiceChannel):
                ch = before.channel
                # Zeitstempel entfernen
                if ch.id in self.join_time:
                    self.join_time[ch.id].pop(member.id, None)

                # Owner geht?
                if ch.id in self.lane_owner and self.lane_owner[ch.id] == member.id:
                    if len(ch.members) > 0:
                        # längste Anwesenheit
                        tsmap = self.join_time.get(ch.id, {})
                        candidates = list(ch.members)
                        candidates.sort(key=lambda m: tsmap.get(m.id, float("inf")))
                        self.lane_owner[ch.id] = candidates[0].id
                    else:
                        # Lane leer -> löschen + DB bereinigen
                        try:
                            await ch.delete(reason="TempVoice: Lane leer")
                        except Exception:
                            pass
                        await self._tvdb.exec("DELETE FROM tempvoice_lanes WHERE channel_id=?", (int(ch.id),))
                        self.created_channels.discard(ch.id)
                        for d in (self.lane_owner, self.lane_base, self.lane_min_rank, self.join_time,
                                  self._last_name_desired, self._last_name_patch_ts,
                                  self._last_lfg_ts, self._last_button_ts):
                            d.pop(ch.id, None)
                        t = self._debounce_tasks.pop(ch.id, None)
                        if t:
                            try:
                                t.cancel()
                            except Exception:
                                pass
        except Exception:
            pass

        # 3) Join Lane -> Präsenz registrieren, evtl. Name anpassen
        try:
            if after and after.channel and isinstance(after.channel, discord.VoiceChannel):
                ch = after.channel
                self.join_time.setdefault(ch.id, {})
                self.join_time[ch.id][member.id] = datetime.utcnow().timestamp()

                if _is_managed_lane(ch):
                    self._schedule_vermutlich_voll(ch)
        except Exception:
            pass

    # ===================== UI =====================

class MainView(discord.ui.View):
    def __init__(self, cog: TempVoiceCog):
        super().__init__(timeout=None)
        self.cog = cog
        # Row 0: Region & Limit
        self.add_item(RegionDEButton(cog))
        self.add_item(RegionEUButton(cog))
        self.add_item(LimitButton(cog))
        # Row 1: MinRank (Select nimmt volle Breite, eigener Row!)
        self.add_item(MinRankSelect(cog))
        # Row 2: Kick/Ban/Unban
        self.add_item(KickButton(cog))
        self.add_item(BanButton(cog))
        self.add_item(UnbanButton(cog))

    def _lane(self, itx: discord.Interaction) -> Optional[discord.VoiceChannel]:
        m = itx.user
        if isinstance(m, discord.Member) and m.voice and isinstance(m.voice.channel, discord.VoiceChannel):
            return m.voice.channel
        return None

    async def _cooldown_ok(self, lane_id: int) -> bool:
        now = time.time()
        last = self.cog._last_button_ts.get(lane_id, 0.0)
        if now - last < BUTTON_COOLDOWN_SEC:
            return False
        self.cog._last_button_ts[lane_id] = now
        return True

# ---------- Region Buttons ----------
class RegionDEButton(discord.ui.Button):
    def __init__(self, cog: TempVoiceCog):
        super().__init__(label="🇩🇪 DE", style=discord.ButtonStyle.primary, row=0, custom_id="tv_region_de")
        self.cog = cog
    async def callback(self, itx: discord.Interaction):
        lane = self.cog.bot.get_channel(itx.channel_id)  # default
        m: discord.Member = itx.user  # type: ignore
        # Bestimme Lane über User-Voice
        if isinstance(m, discord.Member) and m.voice and isinstance(m.voice.channel, discord.VoiceChannel):
            lane = m.voice.channel
        if not isinstance(lane, discord.VoiceChannel) or not _is_managed_lane(lane):
            return await itx.response.send_message("Tritt zuerst **deiner Lane** bei.", ephemeral=True)
        owner_id = self.cog.lane_owner.get(lane.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            return await itx.response.send_message("Nur **Owner** (oder Mods) dürfen den Sprachfilter ändern.", ephemeral=True)
        role = lane.guild.get_role(ENGLISH_ONLY_ROLE_ID)
        if not role:
            return await itx.response.send_message("Rolle **English Only** nicht gefunden. Bitte ID prüfen.", ephemeral=True)
        current = lane.overwrites_for(role)
        if current.connect is False:
            return await itx.response.send_message("Schon **Deutsch-Only**.", ephemeral=True)
        current.connect = False
        try:
            await lane.set_permissions(role, overwrite=current, reason="TempVoice: Deutsch-Only")
        except Exception:
            pass
        await itx.response.send_message("🇩🇪 **Deutsch-Only** aktiv – *English Only* gesperrt.", ephemeral=True)

class RegionEUButton(discord.ui.Button):
    def __init__(self, cog: TempVoiceCog):
        super().__init__(label="🇪🇺 EU", style=discord.ButtonStyle.secondary, row=0, custom_id="tv_region_eu")
        self.cog = cog
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = m.voice.channel if (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)) else None
        if not isinstance(lane, discord.VoiceChannel) or not _is_managed_lane(lane):
            return await itx.response.send_message("Tritt zuerst **deiner Lane** bei.", ephemeral=True)
        owner_id = self.cog.lane_owner.get(lane.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            return await itx.response.send_message("Nur **Owner** (oder Mods) dürfen den Sprachfilter ändern.", ephemeral=True)
        role = lane.guild.get_role(ENGLISH_ONLY_ROLE_ID)
        if not role:
            return await itx.response.send_message("Rolle **English Only** nicht gefunden. Bitte ID prüfen.", ephemeral=True)
        try:
            await lane.set_permissions(role, overwrite=None, reason="TempVoice: Sprachfilter aufgehoben")
        except Exception:
            pass
        await itx.response.send_message("🌐 **Sprachfilter aufgehoben** – *English Only* darf wieder joinen.", ephemeral=True)

# ---------- Limit per Modal ----------
class LimitButton(discord.ui.Button):
    def __init__(self, cog: TempVoiceCog):
        super().__init__(label="🎚️ Limit setzen", style=discord.ButtonStyle.secondary, row=0, custom_id="tv_limit_btn")
        self.cog = cog
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = m.voice.channel if (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)) else None
        if not isinstance(lane, discord.VoiceChannel) or not _is_managed_lane(lane):
            return await itx.response.send_message("Tritt zuerst **deiner Lane** bei.", ephemeral=True)
        owner_id = self.cog.lane_owner.get(lane.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            return await itx.response.send_message("Nur **Owner** (oder Mods) dürfen das Limit setzen.", ephemeral=True)
        await itx.response.send_modal(LimitModal(self.cog, lane))

class LimitModal(discord.ui.Modal, title="Limit setzen"):
    value = discord.ui.TextInput(
        label="Limit (0-99)", placeholder="z.B. 6", required=True, max_length=2
    )
    def __init__(self, cog: TempVoiceCog, lane: discord.VoiceChannel):
        super().__init__(timeout=120)
        self.cog = cog
        self.lane = lane
    async def on_submit(self, itx: discord.Interaction):
        txt = str(self.value.value).strip()
        try:
            val = int(txt)
        except ValueError:
            return await itx.response.send_message("Bitte eine Zahl (0-99) eingeben.", ephemeral=True)
        if val < 0 or val > 99:
            return await itx.response.send_message("Limit muss zwischen 0 und 99 liegen.", ephemeral=True)
        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass
        await self.cog._safe_edit_channel(self.lane, desired_limit=val, reason="TempVoice: Limit gesetzt")
        await self.cog._refresh_name(self.lane)
        try:
            await itx.followup.send(f"🎚️ Limit auf **{val}** gesetzt.", ephemeral=True)
        except Exception:
            pass

# ---------- MinRank (nur in spezieller Kategorie aktiv) ----------
class MinRankSelect(discord.ui.Select):
    def __init__(self, cog: TempVoiceCog):
        self.cog = cog
        # Emojis anhand von Server-Emoji-Namen suchen
        guild: Optional[discord.Guild] = None
        ch = cog.bot.get_channel(INTERFACE_TEXT_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            guild = ch.guild

        options = []
        unknown_emoji = _find_rank_emoji(guild, "unknown")
        options.append(discord.SelectOption(label="Kein Limit (Jeder)", value="unknown", emoji=unknown_emoji or "✅"))
        for r in RANK_ORDER[1:]:
            emoji = _find_rank_emoji(guild, r)
            options.append(discord.SelectOption(label=r.capitalize(), value=r, emoji=emoji))

        super().__init__(
            placeholder="Mindest-Rang (nur in spezieller Kategorie)",
            min_values=1, max_values=1, options=options, row=1, custom_id="tv_minrank"
        )

    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        if not (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)):
            return await itx.response.send_message("Tritt zuerst **deiner Lane** bei.", ephemeral=True)
        lane: discord.VoiceChannel = m.voice.channel

        # Nur in erlaubter Kategorie
        if lane.category_id != MINRANK_CATEGORY_ID:
            return await itx.response.send_message("Mindest-Rang ist hier deaktiviert.", ephemeral=True)

        choice = self.values[0]
        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        self.cog.lane_min_rank[lane.id] = choice
        await self.cog._apply_min_rank(lane, choice)
        await self.cog._refresh_name(lane)

    async def _apply_min_rank(self, lane: discord.VoiceChannel, min_rank: str):
        # (Legacy-Helper existierte im Cog; für Klarheit belassen wir dort die Implementierung)
        pass

# ---------- Kick / Ban / Unban ----------
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
            return await itx.response.send_message("Nur **Owner** (oder Mods) dürfen kicken.", ephemeral=True)
        options = [discord.SelectOption(label=u.display_name, value=str(u.id)) for u in lane.members if u.id != m.id]
        if not options:
            return await itx.response.send_message("Niemand zum Kicken vorhanden.", ephemeral=True)
        view = KickSelectView(self.cog, lane, options)
        try:
            await itx.response.send_message("Wen möchtest du kicken?", view=view, ephemeral=True)
        except discord.InteractionResponded:
            await itx.followup.send("Wen möchtest du kicken?", view=view, ephemeral=True)

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
            return await itx.response.send_message("Nur **Owner** oder Mods dürfen bannen.", ephemeral=True)
        modal = BanModal(self.cog, lane, action="ban")
        await itx.response.send_modal(modal)

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
            return await itx.response.send_message("Nur **Owner** oder Mods dürfen entbannen.", ephemeral=True)
        modal = BanModal(self.cog, lane, action="unban")
        await itx.response.send_modal(modal)

class KickSelect(discord.ui.Select):
    def __init__(self, options, placeholder="Mitglied wählen …"):
        super().__init__(min_values=1, max_values=1, options=options, placeholder=placeholder)
    async def callback(self, itx: discord.Interaction):
        view: "KickSelectView" = self.view  # type: ignore
        await view.handle_kick(itx, int(self.values[0]))

class KickSelectView(discord.ui.View):
    def __init__(self, cog: TempVoiceCog, lane: discord.VoiceChannel, options):
        super().__init__(timeout=60)
        self.cog = cog
        self.lane = lane
        self.add_item(KickSelect(options))

    async def handle_kick(self, itx: discord.Interaction, target_id: int):
        target = self.lane.guild.get_member(target_id)
        if not target or not target.voice or target.voice.channel != self.lane:
            return await itx.response.send_message("User ist nicht (mehr) in der Lane.", ephemeral=True)
        staging = self.lane.guild.get_channel(next(iter(STAGING_CHANNEL_IDS)))
        if not isinstance(staging, discord.VoiceChannel):
            return await itx.response.send_message("Staging-Channel nicht gefunden.", ephemeral=True)
        try:
            await target.move_to(staging, reason=f"Kick durch {itx.user}")
            await itx.response.send_message(f"👢 **{target.display_name}** wurde in **{staging.name}** verschoben.", ephemeral=True)
        except Exception:
            await itx.response.send_message("Konnte nicht verschieben.", ephemeral=True)

class BanModal(discord.ui.Modal, title="User (Un)Ban"):
    target = discord.ui.TextInput(
        label="User (@Mention ODER numerische ID)",
        placeholder="@Name oder 123456789012345678",
        required=True,
        max_length=64
    )
    def __init__(self, cog: TempVoiceCog, lane: discord.VoiceChannel, action: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.lane = lane
        self.action = action  # "ban" | "unban"

    async def on_submit(self, itx: discord.Interaction):
        user: discord.Member = itx.user  # type: ignore
        owner_id = self.cog.lane_owner.get(self.lane.id)
        perms = self.lane.permissions_for(user)
        if not (owner_id == user.id or perms.manage_channels or perms.administrator):
            return await itx.response.send_message("Nur **Owner** (oder Mods) dürfen (un)bannen.", ephemeral=True)

        raw = str(self.target.value).strip()
        uid = None
        if raw.startswith("<@") and raw.endswith(">"):
            digits = "".join(ch for ch in raw if ch.isdigit())
            if digits:
                uid = int(digits)
        elif raw.isdigit():
            uid = int(raw)
        if not uid:
            return await itx.response.send_message("Bitte @Mention ODER numerische ID angeben.", ephemeral=True)

        guild = self.lane.guild
        target_member = guild.get_member(uid)

        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        if self.action == "ban":
            await self.cog.bans.add_ban(owner_id, uid)  # DB
            try:
                # Permission-Deny in aktueller Lane setzen
                await self.lane.set_permissions(target_member or discord.Object(id=uid), connect=False, reason=f"Owner-Ban durch {user}")
                # Falls User gerade drin ist: in einen Staging-Channel schieben
                if target_member and target_member.voice and target_member.voice.channel == self.lane:
                    # nimm irgendeinen verfügbaren Staging-Channel
                    staging = None
                    for cid in STAGING_CHANNEL_IDS:
                        ch = guild.get_channel(cid)
                        if isinstance(ch, discord.VoiceChannel):
                            staging = ch
                            break
                    if staging:
                        try:
                            await target_member.move_to(staging, reason="Owner-Ban")
                        except Exception:
                            pass
                await itx.followup.send("🚫 Nutzer gebannt (owner-persistent).", ephemeral=True)
            except Exception:
                await itx.followup.send("Konnte Ban nicht setzen.", ephemeral=True)
        else:
            await self.cog.bans.remove_ban(owner_id, uid)  # DB
            try:
                await self.lane.set_permissions(target_member or discord.Object(id=uid), overwrite=None, reason=f"Owner-Unban durch {user}")
                await itx.followup.send("♻️ Nutzer entbannt.", ephemeral=True)
            except Exception:
                await itx.followup.send("Konnte Unban nicht setzen.", ephemeral=True)

# -------- Cog Setup --------
async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoiceCog(bot))
