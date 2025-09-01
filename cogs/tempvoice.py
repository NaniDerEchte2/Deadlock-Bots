# cogs/neu_TempVoice.py
# ------------------------------------------------------------
# TempVoice – Auto-Lanes + UI-Management (Casual & Ranked) mit Anti-429
# + Match-Status (RAM-only): ▶ Match gestartet / 🏁 Match beendet
# + AFK-Autoshift: Voll-Mute ≥5 Min -> AFK; beim Entmuten zurück in Lane/Staging
#
# • Join in CASUAL_STAGING_CHANNEL_ID oder RANKED_STAGING_CHANNEL_ID -> Auto-Lane + Move
# • Basisname: "Lane N" (kein "Casual" mehr im Namen)
# • Casual initial: "• Spieler gesucht" (für Sichtbarkeit), Ranked ohne
# • UI (persistente View in INTERFACE_TEXT_CHANNEL_ID):
#     Row0: ✅ Voll • ↩️ Nicht voll  (per-Lane Button-Cooldown 30s)
#     Row1: ▼ Mindest-Rang (nur Casual; Ranked -> Hinweis)
#     Row2: 👢 Kick • 🚫 Ban • ♻️ Unban
#     Row3: ▶ Match gestartet • 🏁 Match beendet  (für alle im Voice möglich)
# • „Im Match (Min X)“: Timer läuft pro Lane, Name-Update alle 5 Minuten (bypass Name-CD)
# • Suffix-Reihenfolge: Basis • ab <Rang> • Im Match (Min X) • (voll | vermutlich voll | Spieler gesucht | Wartend)
# • Anti-429: Locks, Name-Cooldown, atomare Edits, Button-Cooldown, Debounce
# • Min-Rang (Casual): diff-basierte Overwrites; Ranked unberührt
# • Persistenz: nur Owner-Bans in JSON, Match-Status im RAM
# • Admin: !tempvoice_setup (Interface neu bauen)
# ------------------------------------------------------------

import discord
from discord.ext import commands
import asyncio
import json
import logging
import time
import re
from pathlib import Path
from typing import Optional, Dict, Set, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

# ============ KONFIG (IDs anpassen) ============
CASUAL_STAGING_CHANNEL_ID = 1330278323145801758      # Casual Staging Voice
RANKED_STAGING_CHANNEL_ID = 1357422958544420944      # Ranked Staging Voice
RANKED_CATEGORY_ID        = 1357422957017698478      # Ranked Kategorie (Min-Rang ignorieren)
INTERFACE_TEXT_CHANNEL_ID = 1371927143537315890      # Textkanal, wo UI-Nachricht steht
LFG_TEXT_CHANNEL_ID       = 1376335502919335936      # Zielkanal für "Spieler gesucht"-Posts

# AFK-Autoshift
MUTE_MONITOR_CATEGORY_ID  = 1289721245281292290      # In DIESER Kategorie gilt AFK-Autoshift
AFK_CHANNEL_ID            = 1407787129899057242      # AFK-Voice-Channel
AFK_MOVE_DELAY_SEC        = 300                      # 5 Minuten

DEFAULT_CASUAL_CAP        = 8
DEFAULT_RANKED_CAP        = 6
FULL_HINT_THRESHOLD       = 6                        # ab X Leuten Namenszusatz "• vermutlich voll"
BAN_DATA_PATH             = Path("tempvoice_data.json")

NAME_EDIT_COOLDOWN_SEC    = 120                      # Cooldown pro Channelname-PATCH (Match-Ticker bypassed)
LFG_POST_COOLDOWN_SEC     = 60                       # Cooldown pro Lane für LFG-Posts
BUTTON_COOLDOWN_SEC       = 30                       # Pro Lane Anti-Spam für Voll/Nicht voll/Match
DEBOUNCE_VERML_VOLL_SEC   = 25                       # Debounce für „vermutlich voll“
# ================================================

# Ränge (Rollennamen 1:1 im Server, case-insensitiv)
RANK_ORDER = [
    "unknown", "initiate", "seeker", "alchemist", "arcanist",
    "ritualist", "emissary", "archon", "oracle", "phantom",
    "ascendant", "eternus"
]
RANK_SET = set(RANK_ORDER)
SUFFIX_THRESHOLD_RANK = "emissary"  # Suffix „• ab <Rang>“ erst ab diesem Rang

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

def _is_managed_lane(ch: Optional[discord.VoiceChannel]) -> bool:
    return isinstance(ch, discord.VoiceChannel) and ch.name.startswith("Lane ")

def _default_cap(ch: discord.VoiceChannel) -> int:
    return DEFAULT_RANKED_CAP if ch.category_id == RANKED_CATEGORY_ID else DEFAULT_CASUAL_CAP

def _strip_suffixes(current: str) -> str:
    base = current
    for marker in (" • ab ", " • Im Match (Min", " • vermutlich voll", " • voll", " • Spieler gesucht", " • Wartend"):
        if marker in base:
            base = base.split(marker)[0]
    return base

def _is_full_muted_state(vs: Optional[discord.VoiceState]) -> bool:
    if not vs:
        return False
    # self/server mute OR deaf gilt als „voll gemutet“
    return bool(vs.self_mute or vs.self_deaf or vs.mute or vs.deaf)

# ---------- Persistente Bans ----------
class BanStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Dict[str, list]] = {"bans": {}}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "bans" in raw and isinstance(raw["bans"], dict):
                    self.data = {"bans": {str(k): list(map(int, v)) for k, v in raw["bans"].items()}}
                else:
                    self.data = {"bans": {}}
            except Exception as e:
                logger.warning(f"BanStore load error: {e}")
                self.data = {"bans": {}}
        else:
            self._save()

    def _save(self):
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"BanStore save error: {e}")

    def is_banned_by_owner(self, owner_id: int, user_id: int) -> bool:
        return int(user_id) in self.data["bans"].get(str(owner_id), [])

    def add_ban(self, owner_id: int, user_id: int):
        key = str(owner_id)
        arr = self.data["bans"].setdefault(key, [])
        if int(user_id) not in arr:
            arr.append(int(user_id))
            self._save()

    def remove_ban(self, owner_id: int, user_id: int):
        key = str(owner_id)
        arr = self.data["bans"].get(key, [])
        if int(user_id) in arr:
            arr.remove(int(user_id))
            self._save()

# ================== COG ==================
class TempVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bans = BanStore(BAN_DATA_PATH)

        # Laufzeit-States
        self.created_channels: Set[int] = set()
        self.create_channels = self.created_channels  # Back-compat
        self.lane_owner: Dict[int, int] = {}
        self.lane_base: Dict[int, str] = {}
        self.lane_min_rank: Dict[int, str] = {}
        self.lane_full_choice: Dict[int, Optional[bool]] = {}  # True/False/None
        self.lane_searching: Dict[int, bool] = {}              # „• Spieler gesucht“ aktiv?
        self.join_time: Dict[int, Dict[int, float]] = {}

        # Match-Status (RAM-only)
        self.lane_match_active: Dict[int, bool] = {}
        self.lane_match_start_ts: Dict[int, float] = {}

        # Anti-429
        self._edit_locks: Dict[int, asyncio.Lock] = {}
        self._last_name_desired: Dict[int, str] = {}
        self._last_name_patch_ts: Dict[int, float] = {}
        self._last_lfg_ts: Dict[int, float] = {}

        # Anti-Spam Buttons
        self._last_button_ts: Dict[int, float] = {}

        # Debounce „vermutlich voll“
        self._debounce_tasks: Dict[int, asyncio.Task] = {}

        # AFK-Autoshift (RAM)
        self._afk_tasks: Dict[Tuple[int, int], asyncio.Task] = {}  # (guild_id, user_id) -> Task
        self._return_lane: Dict[Tuple[int, int], int] = {}         # Rückkehr-Ziel

    # ---------- Lifecycle ----------
    async def cog_load(self):
        self.bot.add_view(MainView(self))  # persistente UI
        asyncio.create_task(self._startup())
        asyncio.create_task(self._match_tick_loop())

    async def _startup(self):
        await self.bot.wait_until_ready()
        await self._ensure_interface()

    async def _match_tick_loop(self):
        """Match-Minuten im Titel nur alle 5 Minuten updaten (schont API)."""
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(300)  # <- vorher 60s, jetzt 5 Minuten
            try:
                for lane_id, active in list(self.lane_match_active.items()):
                    if not active:
                        continue
                    lane = self.bot.get_channel(lane_id)
                    if isinstance(lane, discord.VoiceChannel) and _is_managed_lane(lane):
                        # force=True: wir wollen trotz Name-CD sicher patchen; Interval ist ohnehin 5 Min
                        await self._refresh_name(lane, force=True)
            except Exception:
                pass

    # ---------- Interface ----------
    async def _ensure_interface(self):
        ch = self.bot.get_channel(INTERFACE_TEXT_CHANNEL_ID)
        if not isinstance(ch, discord.TextChannel):
            logger.warning("INTERFACE_TEXT_CHANNEL_ID ist kein Textkanal.")
            return

        try:
            async for msg in ch.history(limit=50):
                if msg.author == self.bot.user and getattr(msg, "components", None):
                    return
        except Exception:
            pass
        try:
            async for msg in ch.history(limit=100):
                if msg.author == self.bot.user and getattr(msg, "components", None):
                    try:
                        await msg.delete()
                    except Exception:
                        pass
        except Exception:
            pass

        embed = discord.Embed(
            title="Lanes & Steuerung (Casual/Ranked)",
            description=(
                "• **Join Staging (Casual/Ranked)** → ich **erstelle automatisch** deine Lane und move dich rüber.\n"
                "• **Steuerung hier im Interface**:\n"
                "  - **Voll / Nicht voll** (Caps: Casual 8 / Ranked 6, 30s Button-CD)\n"
                "  - **Mindest-Rang** (nur Casual; Ranked unverändert)\n"
                "  - **Kick / Ban / Unban** (Ban/Unban per @Mention **oder** ID)\n"
                "  - **▶ Match gestartet / 🏁 Match beendet** (Status & Timer im Titel, **Update alle 5 Min**)\n\n"
                "💡 Ab **6 Spielern** erscheint nach kurzer Zeit **„• vermutlich voll“**, sofern kein Status gesetzt ist.\n"
                "👑 Owner wechselt automatisch an den am längsten anwesenden User, wenn der Owner geht.\n"
                "🛌 Voll-Mute ≥ **5 Min** → AFK; beim Entmuten zurück zur Lane (oder Staging)."
            ),
            color=0x2ecc71
        )
        embed.set_footer(text="Deadlock DACH • TempVoice")
        await ch.send(embed=embed, view=MainView(self))

    @commands.command(name="tempvoice_setup")
    @commands.has_permissions(administrator=True)
    async def tempvoice_setup(self, ctx: commands.Context):
        ch = ctx.guild.get_channel(INTERFACE_TEXT_CHANNEL_ID) if ctx.guild else None
        if not isinstance(ch, discord.TextChannel):
            return await ctx.reply("❌ INTERFACE_TEXT_CHANNEL_ID ist kein Textkanal.", delete_after=10)
        try:
            async for msg in ch.history(limit=200):
                if msg.author == self.bot.user and getattr(msg, "components", None):
                    try:
                        await msg.delete()
                    except Exception:
                        pass
        except Exception:
            pass
        await self._ensure_interface()
        await ctx.reply("✅ TempVoice-Interface neu erstellt.", delete_after=8)

    # ---------- Helpers (Anti-429) ----------
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
        force_name: bool = False
    ):
        lock = self._lock_for(lane.id)
        async with lock:
            kwargs = {}
            now = time.time()

            if desired_name is not None:
                current_name = lane.name
                if current_name != desired_name:
                    last_desired = self._last_name_desired.get(lane.id)
                    if (not force_name) and last_desired == desired_name:
                        last_ts = self._last_name_patch_ts.get(lane.id, 0.0)
                        if now - last_ts < NAME_EDIT_COOLDOWN_SEC:
                            pass
                        else:
                            kwargs["name"] = desired_name
                    else:
                        kwargs["name"] = desired_name
                    self._last_name_desired[lane.id] = desired_name

            if desired_limit is not None and desired_limit != lane.user_limit:
                kwargs["user_limit"] = desired_limit

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
        min_rank = self.lane_min_rank.get(lane.id, "unknown")
        full_choice = self.lane_full_choice.get(lane.id)  # True/False/None
        member_count = len(lane.members)

        parts = [base]

        if lane.category_id != RANKED_CATEGORY_ID:
            if min_rank and min_rank != "unknown" and _rank_index(min_rank) >= _rank_index(SUFFIX_THRESHOLD_RANK):
                parts.append(f"• ab {min_rank.capitalize()}")

        if self.lane_match_active.get(lane.id, False):
            start = self.lane_match_start_ts.get(lane.id, None)
            minutes = 0
            if start:
                minutes = int(max(0, (time.time() - start) // 60))
                # (Update erfolgt ohnehin nur alle 5 Min; Anzeige bleibt „Min X“)
            parts.append(f"• Im Match (Min {minutes})")

        if full_choice is True:
            parts.append("• voll")
        else:
            if full_choice is None and member_count >= FULL_HINT_THRESHOLD:
                parts.append("• vermutlich voll")
            elif self.lane_searching.get(lane.id, False):
                parts.append("• Spieler gesucht")
            else:
                if not self.lane_match_active.get(lane.id, False):
                    parts.append("• Wartend")

        return " ".join(parts)

    async def _refresh_name(self, lane: discord.VoiceChannel, *, force: bool = False):
        desired = self._compose_name(lane)
        await self._safe_edit_channel(lane, desired_name=desired, reason="TempVoice: Name aktualisiert", force_name=force)

    # ---------- Lanes ----------
    async def _next_name(self, category: Optional[discord.CategoryChannel], prefix: str) -> str:
        """Liefert die kleinste freie 'Lane N' Nummer. Robust via Regex (matcht auch 'Lane 3 • ...')."""
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

    async def _create_lane(self, member: discord.Member, staging: discord.VoiceChannel):
        guild = member.guild
        cat = staging.category
        is_ranked = cat and cat.id == RANKED_CATEGORY_ID
        prefix = "Lane"
        base = await self._next_name(cat, prefix)

        bitrate = getattr(guild, "bitrate_limit", None) or 256000
        cap = DEFAULT_RANKED_CAP if is_ranked else DEFAULT_CASUAL_CAP

        initial_name = base if is_ranked else f"{base} • Spieler gesucht"

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

        self.created_channels.add(lane.id)
        self.lane_owner[lane.id] = member.id
        self.lane_base[lane.id] = base
        self.lane_min_rank[lane.id] = "unknown"
        self.lane_full_choice[lane.id] = None
        self.lane_searching[lane.id] = (not is_ranked)
        self.join_time.setdefault(lane.id, {})

        # Match-Status init
        self.lane_match_active.pop(lane.id, None)
        self.lane_match_start_ts.pop(lane.id, None)

        # Reset Anti-429/Spam
        self._last_name_desired.pop(lane.id, None)
        self._last_name_patch_ts.pop(lane.id, None)
        self._last_lfg_ts.pop(lane.id, None)
        self._last_button_ts.pop(lane.id, None)
        t = self._debounce_tasks.pop(lane.id, None)
        if t:
            t.cancel()

        await self._apply_owner_bans(lane, member.id)

        try:
            await member.move_to(lane, reason="Auto-Lane")
        except Exception:
            pass

        await self._post_lfg(lane, force=True)

        logger.info(f"Auto-Lane erstellt: {lane.name} (owner={member.id}, cap={cap}, bitrate={bitrate})")

    async def _apply_owner_bans(self, lane: discord.VoiceChannel, owner_id: int):
        banned = self.bans.data["bans"].get(str(owner_id), [])
        for uid in banned:
            obj = lane.guild.get_member(int(uid)) or discord.Object(id=int(uid))
            try:
                await lane.set_permissions(obj, connect=False, reason="Owner-Ban (persistent)")
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
        if need > 0:
            txt = f"🔎 {lane.mention}: **Es werden noch Spieler gesucht** (+{need} bis 6)."
        else:
            txt = f"🔎 {lane.mention}: **Es werden noch Spieler gesucht**."
        try:
            await lfg.send(txt)
        except Exception:
            pass

    # ---------- Debounce für „vermutlich voll“ ----------
    def _schedule_vermutlich_voll(self, lane: discord.VoiceChannel):
        t = self._debounce_tasks.get(lane.id)
        if t and not t.done():
            t.cancel()

        async def _job():
            try:
                await asyncio.sleep(DEBOUNCE_VERML_VOLL_SEC)
                if not _is_managed_lane(lane):
                    return
                if self.lane_full_choice.get(lane.id) is None:
                    await self._refresh_name(lane, force=False)
            except asyncio.CancelledError:
                return
            except Exception:
                pass

        self._debounce_tasks[lane.id] = asyncio.create_task(_job())

    # ---------- Min-Rang Overwrites ----------
    async def _set_connect_if_diff(self, channel: discord.VoiceChannel, target: Optional[bool], target_obj: discord.abc.Snowflake):
        current = channel.overwrites_for(target_obj)
        cur = current.connect
        if cur is target:
            return
        current.connect = target
        try:
            await channel.set_permissions(target_obj, overwrite=current)
        except Exception:
            pass

    async def _apply_min_rank(self, lane: discord.VoiceChannel, min_rank: str):
        if lane.category_id == RANKED_CATEGORY_ID:
            return
        guild = lane.guild
        ranks = _rank_roles(guild)

        if min_rank == "unknown":
            await self._set_connect_if_diff(lane, True, guild.default_role)
            for role in ranks.values():
                ow = lane.overwrites_for(role)
                if ow.connect is not None:
                    await self._set_connect_if_diff(lane, None, role)
                    await asyncio.sleep(0.02)
            return

        min_idx = _rank_index(min_rank)
        await self._set_connect_if_diff(lane, False, guild.default_role)

        for name, role in ranks.items():
            idx = _rank_index(name)
            if idx >= min_idx:
                await self._set_connect_if_diff(lane, True, role)
            else:
                ow = lane.overwrites_for(role)
                if ow.connect is not None:
                    await self._set_connect_if_diff(lane, None, role)
            await asyncio.sleep(0.02)

    # ---------- AFK-Autoshift ----------
    def _in_mute_scope(self, ch: Optional[discord.VoiceChannel]) -> bool:
        return isinstance(ch, discord.VoiceChannel) and ch.category_id == MUTE_MONITOR_CATEGORY_ID

    async def _ensure_afk_task(self, member: discord.Member):
        key = (member.guild.id, member.id)
        if key in self._afk_tasks and not self._afk_tasks[key].done():
            return

        async def _job():
            try:
                await asyncio.sleep(AFK_MOVE_DELAY_SEC)
                m = member.guild.get_member(member.id)
                if not m or not m.voice:
                    return
                vs = m.voice
                ch = vs.channel
                if not self._in_mute_scope(ch):
                    return
                if not _is_full_muted_state(vs):
                    return
                if ch and ch.id == AFK_CHANNEL_ID:
                    return

                # Rückkehr-Ziel merken & nach AFK verschieben
                if isinstance(ch, discord.VoiceChannel):
                    self._return_lane[(m.guild.id, m.id)] = ch.id
                afk = m.guild.get_channel(AFK_CHANNEL_ID)
                if isinstance(afk, discord.VoiceChannel):
                    try:
                        await m.move_to(afk, reason="TempVoice: Voll-Mute ≥5 Min → AFK")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug(f"AFK task error for {member.id}: {e}")

        self._afk_tasks[key] = asyncio.create_task(_job())

    async def _cancel_afk_task(self, guild_id: int, user_id: int):
        key = (guild_id, user_id)
        t = self._afk_tasks.pop(key, None)
        if t and not t.done():
            try:
                t.cancel()
            except Exception:
                pass

    async def _handle_mute_afk(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # Entmuten im AFK → zurück in vorherige Lane oder Staging
        if after and after.channel:
            if after.channel.id == AFK_CHANNEL_ID and not _is_full_muted_state(after):
                key = (member.guild.id, member.id)
                back_id = self._return_lane.pop(key, None)
                dest = None
                if back_id:
                    dest = member.guild.get_channel(back_id)
                if not isinstance(dest, discord.VoiceChannel):
                    dest = member.guild.get_channel(CASUAL_STAGING_CHANNEL_ID)
                if isinstance(dest, discord.VoiceChannel):
                    try:
                        await member.move_to(dest, reason="TempVoice: AFK verlassen (Entmuten)")
                    except Exception:
                        pass
                await self._cancel_afk_task(member.guild.id, member.id)
                return

        # Task-Handling je nach Scope & Mute-Status
        if after and isinstance(after.channel, discord.VoiceChannel) and self._in_mute_scope(after.channel):
            if _is_full_muted_state(after):
                await self._ensure_afk_task(member)
            else:
                await self._cancel_afk_task(member.guild.id, member.id)
        else:
            await self._cancel_afk_task(member.guild.id, member.id)
            # Rückkanal vergessen, wenn man die Kategorie verlässt
            self._return_lane.pop((member.guild.id, member.id), None)

    # ---------- Events ----------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # Auto-Lane bei Staging-Join
        try:
            if after and after.channel and isinstance(after.channel, discord.VoiceChannel):
                if after.channel.id in (CASUAL_STAGING_CHANNEL_ID, RANKED_STAGING_CHANNEL_ID):
                    await self._create_lane(member, after.channel)
        except Exception as e:
            logger.warning(f"Auto-lane create failed: {e}")

        # Owner-Transfer / Cleanup & Debounce
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
                    else:
                        if ch.id in self.created_channels:
                            try:
                                await ch.delete(reason="TempVoice: Lane leer")
                            except Exception:
                                pass
                        self.created_channels.discard(ch.id)
                        for d in (self.lane_owner, self.lane_base, self.lane_min_rank,
                                  self.lane_full_choice, self.lane_searching, self.join_time,
                                  self._last_name_desired, self._last_name_patch_ts,
                                  self._last_lfg_ts, self._last_button_ts,
                                  self.lane_match_active, self.lane_match_start_ts):
                            d.pop(ch.id, None)
                        t = self._debounce_tasks.pop(ch.id, None)
                        if t:
                            try:
                                t.cancel()
                            except Exception:
                                pass
        except Exception:
            pass

        # Join in Lane: Join-Zeit & Bans checken + „vermutlich voll“-Debounce
        try:
            if after and after.channel and isinstance(after.channel, discord.VoiceChannel):
                ch = after.channel
                self.join_time.setdefault(ch.id, {})
                self.join_time[ch.id][member.id] = datetime.utcnow().timestamp()

                owner_id = self.lane_owner.get(ch.id)
                if owner_id and self.bans.is_banned_by_owner(owner_id, member.id):
                    staging = member.guild.get_channel(CASUAL_STAGING_CHANNEL_ID)
                    if isinstance(staging, discord.VoiceChannel):
                        try:
                            await member.move_to(staging, reason="Owner-Ban aktiv")
                        except Exception:
                            pass

                if _is_managed_lane(ch):
                    self._schedule_vermutlich_voll(ch)
        except Exception:
            pass

        # AFK-Autoshift nach Voll-Mute
        try:
            await self._handle_mute_afk(member, before, after)
        except Exception:
            pass

# ================== UI ==================

class MainView(discord.ui.View):
    """
    Kontext-UI: wirkt auf den Voice-Channel, in dem der klickende User gerade ist.
      Row0: ✅ Voll • ↩️ Nicht voll  (30s Cooldown pro Lane)
      Row1: ▼ Mindest-Rang (nur Casual; Ranked -> Hinweis)
      Row2: 👢 Kick • 🚫 Ban • ♻️ Unban
      Row3: ▶ Match gestartet • 🏁 Match beendet (für alle im Voice)
    """
    def __init__(self, cog: TempVoiceCog):
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(MinRankSelect(cog))

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

    # Row0
    @discord.ui.button(label="✅ Voll", style=discord.ButtonStyle.success, row=0, custom_id="tv_full")
    async def btn_full(self, itx: discord.Interaction, _):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Tritt zuerst **deiner Lane** bei.", ephemeral=True
            )
        if not await self._cooldown_ok(lane.id):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Bitte warte kurz (30s) bevor du erneut klickst.", ephemeral=True
            )

        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        current = max(1, len(lane.members))
        self.cog.lane_full_choice[lane.id] = True
        self.cog.lane_searching[lane.id] = False

        desired_name = self.cog._compose_name(lane)
        await self.cog._safe_edit_channel(
            lane,
            desired_name=desired_name,
            desired_limit=current,
            reason="TempVoice: Voll (lock auf aktuelle Anzahl)",
            force_name=True
        )

    @discord.ui.button(label="↩️ Nicht voll", style=discord.ButtonStyle.secondary, row=0, custom_id="tv_notfull")
    async def btn_notfull(self, itx: discord.Interaction, _):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Tritt zuerst **deiner Lane** bei.", ephemeral=True
            )
        if not await self._cooldown_ok(lane.id):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Bitte warte kurz (30s) bevor du erneut klickst.", ephemeral=True
            )

        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        self.cog.lane_full_choice[lane.id] = False
        self.cog.lane_searching[lane.id] = True

        cap = _default_cap(lane)
        desired_name = self.cog._compose_name(lane)
        await self.cog._safe_edit_channel(
            lane,
            desired_name=desired_name,
            desired_limit=cap,
            reason="TempVoice: Nicht voll (Limit geöffnet)",
            force_name=True
        )

        await self.cog._post_lfg(lane, force=False)

    # Row2 – Moderation
    @discord.ui.button(label="👢 Kick", style=discord.ButtonStyle.secondary, row=2, custom_id="tv_kick")
    async def btn_kick(self, itx: discord.Interaction, _):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Du musst in einer Lane sein.", ephemeral=True
            )
        user: discord.Member = itx.user  # type: ignore
        if not (self.cog.lane_owner.get(lane.id) == user.id or lane.permissions_for(user).manage_channels or lane.permissions_for(user).administrator):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Nur **Owner** der Lane (oder Mods) dürfen kicken.", ephemeral=True
            )

        options = [discord.SelectOption(label=m.display_name, value=str(m.id)) for m in lane.members if m.id != user.id]
        if not options:
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Niemand zum Kicken vorhanden.", ephemeral=True
            )
        view = KickSelectView(self.cog, lane, options)
        try:
            await itx.response.send_message("Wen möchtest du kicken?", view=view, ephemeral=True)
        except discord.InteractionResponded:
            await itx.followup.send("Wen möchtest du kicken?", view=view, ephemeral=True)

    @discord.ui.button(label="🚫 Ban", style=discord.ButtonStyle.danger, row=2, custom_id="tv_ban")
    async def btn_ban(self, itx: discord.Interaction, _):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Du musst in einer Lane sein.", ephemeral=True
            )
        user: discord.Member = itx.user  # type: ignore
        if not (self.cog.lane_owner.get(lane.id) == user.id or lane.permissions_for(user).manage_channels or lane.permissions_for(user).administrator):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Nur **Owner** der Lane (oder Mods) dürfen bannen.", ephemeral=True
            )
        modal = BanModal(self.cog, lane, action="ban")
        await itx.response.send_modal(modal)

    @discord.ui.button(label="♻️ Unban", style=discord.ButtonStyle.primary, row=2, custom_id="tv_unban")
    async def btn_unban(self, itx: discord.Interaction, _):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Du musst in einer Lane sein.", ephemeral=True
            )
        user: discord.Member = itx.user  # type: ignore
        if not (self.cog.lane_owner.get(lane.id) == user.id or lane.permissions_for(user).manage_channels or lane.permissions_for(user).administrator):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Nur **Owner** der Lane (oder Mods) dürfen entbannen.", ephemeral=True
            )
        modal = BanModal(self.cog, lane, action="unban")
        await itx.response.send_modal(modal)

    # Row3 – Match-Status (für alle, die im Voice sind)
    @discord.ui.button(label="▶ Match gestartet", style=discord.ButtonStyle.primary, row=3, custom_id="tv_match_start")
    async def btn_match_start(self, itx: discord.Interaction, _):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Tritt zuerst **deiner Lane** bei.", ephemeral=True
            )
        if not await self._cooldown_ok(lane.id):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Bitte warte kurz (30s) bevor du erneut klickst.", ephemeral=True
            )

        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        self.cog.lane_match_active[lane.id] = True
        self.cog.lane_match_start_ts[lane.id] = time.time()

        await self.cog._refresh_name(lane, force=True)
        try:
            await itx.followup.send("▶ Match gestartet – Timer läuft.", ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(label="🏁 Match beendet", style=discord.ButtonStyle.secondary, row=3, custom_id="tv_match_end")
    async def btn_match_end(self, itx: discord.Interaction, _):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Tritt zuerst **deiner Lane** bei.", ephemeral=True
            )
        if not await self._cooldown_ok(lane.id):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Bitte warte kurz (30s) bevor du erneut klickst.", ephemeral=True
            )

        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        self.cog.lane_match_active.pop(lane.id, None)
        self.cog.lane_match_start_ts.pop(lane.id, None)

        await self.cog._refresh_name(lane, force=True)
        try:
            await itx.followup.send("🏁 Match beendet – Timer gestoppt.", ephemeral=True)
        except Exception:
            pass

# ----- Mindest-Rang (Row1) -----
class MinRankSelect(discord.ui.Select):
    def __init__(self, cog: TempVoiceCog):
        self.cog = cog
        guild: Optional[discord.Guild] = None
        ch = cog.bot.get_channel(INTERFACE_TEXT_CHANNEL_ID)
        if isinstance(ch, discord.TextChannel):
            guild = ch.guild

        options = []
        unknown_emoji = (discord.utils.get(guild.emojis, name="unknown") if guild else None)
        options.append(discord.SelectOption(label="Kein Limit (Jeder)", value="unknown", emoji=unknown_emoji or "✅"))
        for r in RANK_ORDER[1:]:
            emoji = (discord.utils.get(guild.emojis, name=r) if guild else None)
            options.append(discord.SelectOption(label=r.capitalize(), value=r, emoji=emoji))

        super().__init__(
            placeholder="Mindest-Rang (nur Casual; Ranked bleibt wie ist)",
            min_values=1, max_values=1, options=options, row=1, custom_id="tv_minrank"
        )

    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        if not (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Tritt zuerst **deiner Lane** bei.", ephemeral=True
            )
        lane: discord.VoiceChannel = m.voice.channel

        if lane.category_id == RANKED_CATEGORY_ID:
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "ℹ️ **Ranked** wird extern verwaltet – Mindest-Rang hier nicht anwendbar.", ephemeral=True
            )

        choice = self.values[0]
        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        self.cog.lane_min_rank[lane.id] = choice
        await self.cog._apply_min_rank(lane, choice)
        await self.cog._refresh_name(lane, force=False)

# ----- Kick-Select (ephemeral) -----
class KickSelect(discord.ui.Select):
    def __init__(self, options, placeholder="Mitglied wählen …"):
        super().__init__(min_values=1, max_values=1, options=options, placeholder=placeholder)

    async def callback(self, itx: discord.Interaction):
        view: KickSelectView = self.view  # type: ignore
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
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "User ist nicht (mehr) in der Lane.", ephemeral=True
            )
        staging = self.lane.guild.get_channel(CASUAL_STAGING_CHANNEL_ID)
        if not isinstance(staging, discord.VoiceChannel):
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Staging-Channel nicht gefunden.", ephemeral=True
            )
        try:
            await target.move_to(staging, reason=f"Kick durch {itx.user}")
            await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                f"👢 **{target.display_name}** wurde in **Casual Staging** verschoben.", ephemeral=True
            )
        except Exception:
            await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Konnte nicht verschieben.", ephemeral=True
            )

# ----- Ban/Unban Modal -----
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
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Nur **Owner** der Lane (oder Mods) dürfen (un)bannen.", ephemeral=True
            )

        raw = str(self.target.value).strip()
        uid = None
        if raw.startswith("<@") and raw.endswith(">"):
            digits = "".join(ch for ch in raw if ch.isdigit())
            if digits:
                uid = int(digits)
        elif raw.isdigit():
            uid = int(raw)
        if not uid:
            return await (itx.response.send_message if not itx.response.is_done() else itx.followup.send)(
                "Bitte @Mention ODER numerische ID angeben.", ephemeral=True
            )

        guild = self.lane.guild
        target_member = guild.get_member(uid)

        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        if self.action == "ban":
            self.cog.bans.add_ban(owner_id, uid)  # type: ignore
            try:
                await self.lane.set_permissions(target_member or discord.Object(id=uid), connect=False, reason=f"Owner-Ban durch {user}")
                if target_member and target_member.voice and target_member.voice.channel == self.lane:
                    staging = guild.get_channel(CASUAL_STAGING_CHANNEL_ID)
                    if isinstance(staging, discord.VoiceChannel):
                        try:
                            await target_member.move_to(staging, reason="Owner-Ban")
                        except Exception:
                            pass
                await itx.followup.send("🚫 Nutzer gebannt.", ephemeral=True)
            except Exception:
                await itx.followup.send("Konnte Ban nicht setzen.", ephemeral=True)
        else:
            self.cog.bans.remove_ban(owner_id, uid)  # type: ignore
            try:
                await self.lane.set_permissions(target_member or discord.Object(id=uid), overwrite=None, reason=f"Owner-Unban durch {user}")
                await itx.followup.send("♻️ Nutzer entbannt.", ephemeral=True)
            except Exception:
                await itx.followup.send("Konnte Unban nicht setzen.", ephemeral=True)

# -------------- Setup --------------
async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoiceCog(bot))
