# ------------------------------------------------------------
# TempVoice – Auto-Lanes + UI-Management (Casual & Ranked) mit Anti-429
# Persistentes Interface (merkt sich Message-ID) + DE/EU-Regionsfilter
# ------------------------------------------------------------

import discord
from discord.ext import commands
import asyncio
import json
import logging
import time
import re
from pathlib import Path
from typing import Optional, Dict, Set, Tuple, Any, List
from datetime import datetime

logger = logging.getLogger(__name__)

# ---- Sicherer Import von WorkerProxy mit Fallback (zusätzlich zum globalen Shim) ----
try:
    from shared.worker_client import WorkerProxy  # type: ignore
except Exception:  # pragma: no cover
    class WorkerProxy:  # type: ignore
        def __init__(self, *a, **kw): pass
        def request(self, *a, **kw): return {"ok": False, "error": "worker_stub"}
        def edit_channel(self, *a, **kw): return {"ok": False, "error": "worker_stub"}
        def set_permissions(self, *a, **kw): return {"ok": False, "error": "worker_stub"}

# ============ KONFIG ============
CASUAL_STAGING_CHANNEL_ID = 1330278323145801758
RANKED_STAGING_CHANNEL_ID = 1357422958544420944
RANKED_CATEGORY_ID        = 1357422957017698478
INTERFACE_TEXT_CHANNEL_ID = 1371927143537315890
LFG_TEXT_CHANNEL_ID       = 1376335502919335936

# AFK komplett deaktiviert (Logik auskommentiert)
MUTE_MONITOR_CATEGORY_ID  = 1289721245281292290
AFK_CHANNEL_ID            = 1407787129899057242
AFK_MOVE_DELAY_SEC        = 300

DEFAULT_CASUAL_CAP        = 8
DEFAULT_RANKED_CAP        = 6
FULL_HINT_THRESHOLD       = 6
BAN_DATA_PATH             = Path("tempvoice_data.json")

NAME_EDIT_COOLDOWN_SEC    = 120
LFG_POST_COOLDOWN_SEC     = 60
LFG_DELETE_AFTER_SEC      = 20 * 60
BUTTON_COOLDOWN_SEC       = 30
DEBOUNCE_VERML_VOLL_SEC   = 25

# Persistenter Speicher für Interface-Message
INTERFACE_STATE_PATH      = Path("tempvoice_interface.json")
# =================================

# Feste ID der "English Only"-Rolle (Regionsfilter)
ENGLISH_ONLY_ROLE_ID = 1309741866098491479

RANK_ORDER = [
    "unknown", "initiate", "seeker", "alchemist", "arcanist",
    "ritualist", "emissary", "archon", "oracle", "phantom",
    "ascendant", "eternus"
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

# ----------------- Persistenz Interface-ID -----------------

def _load_interface_state() -> Dict[str, int]:
    try:
        if INTERFACE_STATE_PATH.exists():
            raw = json.loads(INTERFACE_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                out = {}
                if "message_id" in raw:
                    out["message_id"] = int(raw["message_id"])
                if "channel_id" in raw:
                    out["channel_id"] = int(raw["channel_id"])
                return out
    except Exception as e:
        logger.warning(f"Interface state load error: {e}")
    return {}

def _save_interface_state(message_id: int, channel_id: int) -> None:
    try:
        INTERFACE_STATE_PATH.write_text(
            json.dumps({"message_id": int(message_id), "channel_id": int(channel_id)}, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"Interface state save error: {e}")

# ----------------------------------------------------------

def _is_full_muted_state(vs: Optional[discord.VoiceState]) -> bool:
    if not vs:
        return False
    return bool(vs.self_mute or vs.self_deaf or vs.mute or vs.deaf)

class TVWorker:
    def __init__(self):
        self._proxy = WorkerProxy()
        try:
            resp = self._proxy.request({"op": "ping"})
            self.enabled: bool = bool(resp and resp.get("ok"))
        except Exception:
            self.enabled = False

    async def edit_channel(self, channel_id: int,
                           name: Optional[str] = None,
                           user_limit: Optional[int] = None,
                           bitrate: Optional[int] = None,
                           reason: Optional[str] = None) -> bool:
        if not self.enabled:
            return False
        def _call():
            resp = self._proxy.edit_channel(channel_id, name=name,
                                            user_limit=user_limit, bitrate=bitrate)
            return bool(resp.get("ok"))
        return await asyncio.to_thread(_call)

    async def set_connect(self, channel_id: int, target_id: int, allow: Optional[bool]) -> bool:
        if not self.enabled:
            return False
        def _call():
            ow = {"connect": allow}
            resp = self._proxy.set_permissions(channel_id, target_id, ow)
            return bool(resp.get("ok"))
        return await asyncio.to_thread(_call)

    async def clear_overwrite(self, channel_id: int, target_id: int) -> bool:
        if not self.enabled:
            return False
        def _call():
            resp = self._proxy.set_permissions(channel_id, target_id, {})
            return bool(resp.get("ok"))
        return await asyncio.to_thread(_call)

    async def create_voice(self, **kwargs) -> Optional[int]:
        return None
    async def delete_channel(self, channel_id: int, reason: Optional[str] = None) -> bool:
        return False
    async def move_member(self, guild_id: int, user_id: int, dest_channel_id: int, reason: Optional[str] = None) -> bool:
        return False

class BanStore:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Dict[str, List[int]]] = {"bans": {}}
        self._load()
    def _load(self):
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "bans" in raw and isinstance(raw["bans"], dict):
                    self.data = {"bans": {str(k): [int(x) for x in v] for k, v in raw["bans"].items()}}
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

class TempVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bans = BanStore(BAN_DATA_PATH)
        self.worker = TVWorker()

        self.created_channels: Set[int] = set()
        self.create_channels = self.created_channels  # Back-compat
        self.lane_owner: Dict[int, int] = {}
        self.lane_base: Dict[int, str] = {}
        self.lane_min_rank: Dict[int, str] = {}
        self.lane_full_choice: Dict[int, Optional[bool]] = {}
        self.lane_searching: Dict[int, bool] = {}
        self.join_time: Dict[int, Dict[int, float]] = {}

        self.lane_match_active: Dict[int, bool] = {}
        self.lane_match_start_ts: Dict[int, float] = {}

        self._edit_locks: Dict[int, asyncio.Lock] = {}
        self._last_name_desired: Dict[int, str] = {}
        self._last_name_patch_ts: Dict[int, float] = {}
        self._last_lfg_ts: Dict[int, float] = {}

        self._lfg_cleanup_tasks: Set[int] = set()
        self._last_button_ts: Dict[int, float] = {}
        self._debounce_tasks: Dict[int, asyncio.Task] = {}

        # AFK-Strukturen bleiben bestehen, Logik ist unten auskommentiert
        self._afk_tasks: Dict[Tuple[int, int], asyncio.Task] = {}
        self._return_lane: Dict[Tuple[int, int], int] = {}
        self._afk_escape_tasks: Dict[Tuple[int, int], asyncio.Task] = {}
        self._afk_penalty_level: Dict[Tuple[int, int], int] = {}

    async def cog_load(self):
        # Persistente View registrieren (wichtig für Restart)
        self.bot.add_view(MainView(self))
        asyncio.create_task(self._startup())
        asyncio.create_task(self._match_tick_loop())

    async def _startup(self):
        await self.bot.wait_until_ready()
        await self._ensure_interface()

    async def _match_tick_loop(self):
        await self.bot.wait_until_ready()
        while True:
            await asyncio.sleep(300)
            try:
                for lane_id, active in list(self.lane_match_active.items()):
                    if not active:
                        continue
                    lane = self.bot.get_channel(lane_id)
                    if isinstance(lane, discord.VoiceChannel) and _is_managed_lane(lane):
                        await self._refresh_name(lane, force=True)
            except Exception:
                pass

    async def _ensure_interface(self, *, target_channel_id: Optional[int] = None, force_recreate: bool = False):
        """
        Stellt sicher, dass GENAU EINE Interface-Message existiert.
        - Wenn gespeichert: re-use, nur Embed aktualisieren (KEIN neuer Post).
        - Wenn fehlt oder force_recreate: neu posten und IDs speichern.
        """
        # Zielkanal bestimmen (explizit > gespeichert > config)
        state = _load_interface_state()
        ch_id = target_channel_id or state.get("channel_id") or INTERFACE_TEXT_CHANNEL_ID
        ch = self.bot.get_channel(ch_id)

        if not isinstance(ch, discord.TextChannel):
            logger.warning("Interface-Textkanal %s nicht gefunden/kein Textkanal.", ch_id)
            return

        embed = discord.Embed(
            title="Lanes & Steuerung (Casual/Ranked)",
            description=(
                "• **Join Staging (Casual/Ranked)** → ich **erstelle automatisch** deine Lane und move dich rüber.\n"
                "• **Steuerung hier im Interface**:\n"
                "  - **Voll / Nicht voll** (Caps: Casual 8 / Ranked 6, 30s Button-CD)\n"
                "  - **🇩🇪 DE / 🇪🇺 EU** (Regionsfilter: *English Only* in Lane sperren/aufheben)\n"
                "  - **Mindest-Rang** (nur Casual; setzt nur *deny* für zu niedrige Ränge)\n"
                "  - **Kick / Ban / Unban** (Ban/Unban per @Mention **oder** ID)\n"
                "  - **▶ Match gestartet / 🏁 Match beendet** (Status & Timer im Titel, **Update alle 5 Min**)\n\n"
                "💡 Ab **6 Spielern** erscheint nach kurzer Zeit **„• vermutlich voll“**, sofern kein Status gesetzt ist.\n"
                "👑 Owner wechselt automatisch an den am längsten anwesenden User, wenn der Owner geht.\n"
                "🛌 AFK-Automatik ist derzeit **deaktiviert** (Mute/Deaf wird nicht verschoben)."
            ),
            color=0x2ecc71
        )
        embed.set_footer(text="Deadlock DACH • TempVoice")

        msg_id = state.get("message_id")

        if not force_recreate and msg_id:
            # Versuche, bestehende Message zu verwenden
            try:
                msg = await ch.fetch_message(msg_id)
                # Nur Embed aktualisieren; View ist persistent über bot.add_view()
                try:
                    await msg.edit(embed=embed)
                except Exception:
                    pass
                return  # fertig, NICHT neu posten
            except Exception:
                logger.info("Gespeicherte Interface-Message-ID %s nicht gefunden – wird neu erstellt.", msg_id)

        # Neu erstellen (entweder fehlend oder force)
        try:
            msg = await ch.send(embed=embed, view=MainView(self))
            _save_interface_state(message_id=msg.id, channel_id=ch.id)
        except Exception as e:
            logger.warning(f"Konnte Interface nicht posten: {e}")

    # ----- Admin: gezielt neu erstellen/verschieben -----
    @commands.command(name="tempvoice_setup", help="Interface neu erstellen (optional: #channel erwähnen)")
    @commands.has_permissions(administrator=True)
    async def tempvoice_setup(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        await self._ensure_interface(target_channel_id=(channel.id if channel else None), force_recreate=True)
        await ctx.reply("✅ TempVoice-Interface neu (er)stellt und gespeichert.", delete_after=8)

    # ----- Slash-Command: Panel setzen/verschieben -----
    @discord.app_commands.command(name="tempvoice", description="TempVoice Interface setzen/verschieben")
    @discord.app_commands.describe(channel="Textkanal, in den das Interface gepostet werden soll")
    @discord.app_commands.checks.has_permissions(manage_channels=True)
    async def tempvoice_panel(self, interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
        await interaction.response.defer(ephemeral=True, thinking=False)
        if channel is None:
            # Nur sicherstellen, NICHT neu posten
            await self._ensure_interface(force_recreate=False)
            return await interaction.followup.send("ℹ️ Interface geprüft – bestehende Message weiterverwendet.", ephemeral=True)
        # explizit in anderen Kanal verschieben/neu erstellen
        await self._ensure_interface(target_channel_id=channel.id, force_recreate=True)
        await interaction.followup.send(f"✅ Interface in {channel.mention} (neu) erstellt und gespeichert.", ephemeral=True)

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
        force_name: bool = False,
        desired_bitrate: Optional[int] = None,
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
                        if now - last_ts >= NAME_EDIT_COOLDOWN_SEC:
                            kwargs["name"] = desired_name
                    else:
                        kwargs["name"] = desired_name
                    self._last_name_desired[lane.id] = desired_name

            if desired_limit is not None and desired_limit != lane.user_limit:
                kwargs["user_limit"] = desired_limit

            if desired_bitrate is not None and desired_bitrate != lane.bitrate:
                kwargs["bitrate"] = desired_bitrate

            if not kwargs:
                return

            used_worker = False
            if self.worker.enabled:
                ok = await self.worker.edit_channel(
                    lane.id,
                    name=kwargs.get("name"),
                    user_limit=kwargs.get("user_limit"),
                    bitrate=kwargs.get("bitrate"),
                    reason=reason or "TempVoice: Update (via worker)"
                )
                used_worker = True
                if ok:
                    if "name" in kwargs:
                        self._last_name_patch_ts[lane.id] = now
                    return

            try:
                await lane.edit(**kwargs, reason=reason or ("TempVoice: Update" + (" (fallback)" if used_worker else "")))
                if "name" in kwargs:
                    self._last_name_patch_ts[lane.id] = now
            except discord.HTTPException as e:
                logger.warning(f"channel.edit {lane.id} failed: {e}")

    def _compose_name(self, lane: discord.VoiceChannel) -> str:
        base = self.lane_base.get(lane.id) or _strip_suffixes(lane.name)
        min_rank = self.lane_min_rank.get(lane.id, "unknown")
        full_choice = self.lane_full_choice.get(lane.id)
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

    async def _create_lane(self, member: discord.Member, staging: discord.VoiceChannel):
        guild = member.guild
        cat = staging.category
        is_ranked = cat and cat.id == RANKED_CATEGORY_ID
        prefix = "Lane"
        base = await self._next_name(cat, prefix)

        bitrate = getattr(guild, "bitrate_limit", None) or 256000
        cap = DEFAULT_RANKED_CAP if is_ranked else DEFAULT_CASUAL_CAP

        initial_name = base if is_ranked else f"{base} • Spieler gesucht"

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

        self.created_channels.add(lane.id)
        self.lane_owner[lane.id] = member.id
        self.lane_base[lane.id] = base
        self.lane_min_rank[lane.id] = "unknown"
        self.lane_full_choice[lane.id] = None
        self.lane_searching[lane.id] = (not is_ranked)
        self.join_time.setdefault(lane.id, {})

        self.lane_match_active.pop(lane.id, None)
        self.lane_match_start_ts.pop(lane.id, None)

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
            try:
                if self.worker.enabled:
                    await self.worker.set_connect(lane.id, int(uid), False)
                else:
                    obj = lane.guild.get_member(int(uid)) or discord.Object(id=int(uid))
                    current = lane.overwrites_for(obj)
                    current.connect = False
                    await lane.set_permissions(obj, overwrite=current, reason="Owner-Ban (persistent)")
                await asyncio.sleep(0.02)
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

    async def _set_connect_if_diff(self, lane: discord.VoiceChannel, target: Optional[bool], target_obj: discord.abc.Snowflake):
        if self.worker.enabled:
            ok = await self.worker.set_connect(lane.id, target_obj.id, target)
            if (not ok) and target is None:
                await self.worker.clear_overwrite(lane.id, target_obj.id)
            return
        current = lane.overwrites_for(target_obj)
        cur = current.connect
        if cur is target:
            return
        current.connect = target
        try:
            await lane.set_permissions(target_obj, overwrite=current)
        except Exception:
            pass

    # Mindest-Rang: NUR deny für zu niedrige Ränge, KEINE expliziten allows
    async def _apply_min_rank(self, lane: discord.VoiceChannel, min_rank: str):
        if lane.category_id == RANKED_CATEGORY_ID:
            return
        guild = lane.guild
        ranks = _rank_roles(guild)

        if min_rank == "unknown":
            # alle Denies entfernen, Default greifen lassen
            for role in ranks.values():
                ow = lane.overwrites_for(role)
                if ow.connect is not None:
                    await self._set_connect_if_diff(lane, None, role)
                    await asyncio.sleep(0.02)
            return

        min_idx = _rank_index(min_rank)

        for name, role in ranks.items():
            idx = _rank_index(name)
            if idx < min_idx:
                await self._set_connect_if_diff(lane, False, role)  # deny für zu niedrige
            else:
                ow = lane.overwrites_for(role)
                if ow.connect is not None:
                    await self._set_connect_if_diff(lane, None, role)  # Overwrite entfernen
            await asyncio.sleep(0.02)

    # ---- AFK LOGIK (AUF WUNSCH DEAKTIVIERT) ----
    # def _in_mute_scope(self, ch: Optional[discord.VoiceChannel]) -> bool:
    #     return isinstance(ch, discord.VoiceChannel) and ch.category_id == MUTE_MONITOR_CATEGORY_ID
    # async def _ensure_afk_task(self, member: discord.Member): ...
    # async def _cancel_afk_task(self, guild_id: int, user_id: int): ...
    # async def _ensure_escape_task(self, member: discord.Member): ...
    # async def _cancel_escape_task(self, guild_id: int, user_id: int): ...
    # async def _handle_mute_afk(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState): ...

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        try:
            if after and after.channel and isinstance(after.channel, discord.VoiceChannel):
                if after.channel.id in (CASUAL_STAGING_CHANNEL_ID, RANKED_STAGING_CHANNEL_ID):
                    await self._create_lane(member, after.channel)
        except Exception as e:
            logger.warning(f"Auto-lane create failed: {e}")

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

        # AFK-Handling AUS:
        # try:
        #     await self._handle_mute_afk(member, before, after)
        # except Exception:
        #     pass

# ===================== UI =====================

class MainView(discord.ui.View):
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

    @discord.ui.button(label="✅ Voll", style=discord.ButtonStyle.success, row=0, custom_id="tv_full")
    async def btn_full(self, itx: discord.Interaction, _button: discord.ui.Button):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Tritt zuerst **deiner Lane** bei.", ephemeral=True)
        if not await self._cooldown_ok(lane.id):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Bitte warte kurz (30s) bevor du erneut klickst.", ephemeral=True)
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
    async def btn_notfull(self, itx: discord.Interaction, _button: discord.ui.Button):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Tritt zuerst **deiner Lane** bei.", ephemeral=True)
        if not await self._cooldown_ok(lane.id):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Bitte warte kurz (30s) bevor du erneut klickst.", ephemeral=True)
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

    # Regionsfilter-Buttons 🇩🇪 / 🇪🇺
    @discord.ui.button(label="🇩🇪 DE", style=discord.ButtonStyle.primary, row=0, custom_id="tv_region_de")
    async def btn_region_de(self, itx: discord.Interaction, _button: discord.ui.Button):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Tritt zuerst **deiner Lane** bei.", ephemeral=True)
        user: discord.Member = itx.user  # type: ignore
        perms = lane.permissions_for(user)
        if not (self.cog.lane_owner.get(lane.id) == user.id or perms.manage_channels or perms.administrator):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Nur **Owner** der Lane (oder Mods) dürfen den Regionsfilter ändern.", ephemeral=True)
        role = lane.guild.get_role(ENGLISH_ONLY_ROLE_ID)
        if not role:
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Rolle **English Only** nicht gefunden. Bitte ID prüfen.", ephemeral=True)
        await self.cog._set_connect_if_diff(lane, False, role)  # deny connect
        sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
        await sender("🇩🇪 **Deutsch-Only** aktiv – *English Only* ist in dieser Lane gesperrt.", ephemeral=True)

    @discord.ui.button(label="🇪🇺 EU", style=discord.ButtonStyle.secondary, row=0, custom_id="tv_region_eu")
    async def btn_region_eu(self, itx: discord.Interaction, _button: discord.ui.Button):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Tritt zuerst **deiner Lane** bei.", ephemeral=True)
        user: discord.Member = itx.user  # type: ignore
        perms = lane.permissions_for(user)
        if not (self.cog.lane_owner.get(lane.id) == user.id or perms.manage_channels or perms.administrator):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Nur **Owner** der Lane (oder Mods) dürfen den Regionsfilter ändern.", ephemeral=True)
        role = lane.guild.get_role(ENGLISH_ONLY_ROLE_ID)
        if not role:
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Rolle **English Only** nicht gefunden. Bitte ID prüfen.", ephemeral=True)
        await self.cog._set_connect_if_diff(lane, None, role)  # remove overwrite
        sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
        await sender("🌐 **Sprachfilter aufgehoben** – *English Only* darf wieder joinen.", ephemeral=True)

    @discord.ui.button(label="👢 Kick", style=discord.ButtonStyle.secondary, row=2, custom_id="tv_kick")
    async def btn_kick(self, itx: discord.Interaction, _button: discord.ui.Button):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Du musst in einer Lane sein.", ephemeral=True)
        user: discord.Member = itx.user  # type: ignore
        perms = lane.permissions_for(user)
        if not (self.cog.lane_owner.get(lane.id) == user.id or perms.manage_channels or perms.administrator):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Nur **Owner** der Lane (oder Mods) dürfen kicken.", ephemeral=True)
        options = [discord.SelectOption(label=m.display_name, value=str(m.id)) for m in lane.members if m.id != user.id]
        if not options:
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Niemand zum Kicken vorhanden.", ephemeral=True)
        view = KickSelectView(self.cog, lane, options)
        try:
            await itx.response.send_message("Wen möchtest du kicken?", view=view, ephemeral=True)
        except discord.InteractionResponded:
            await itx.followup.send("Wen möchtest du kicken?", view=view, ephemeral=True)

    @discord.ui.button(label="🚫 Ban", style=discord.ButtonStyle.danger, row=2, custom_id="tv_ban")
    async def btn_ban(self, itx: discord.Interaction, _button: discord.ui.Button):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Du musst in einer Lane sein.", ephemeral=True)
        user: discord.Member = itx.user  # type: ignore
        perms = lane.permissions_for(user)
        if not (self.cog.lane_owner.get(lane.id) == user.id or perms.manage_channels or perms.administrator):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Nur **Owner** der Lane (oder Mods) dürfen bannen.", ephemeral=True)
        modal = BanModal(self.cog, lane, action="ban")
        await itx.response.send_modal(modal)

    @discord.ui.button(label="♻️ Unban", style=discord.ButtonStyle.primary, row=2, custom_id="tv_unban")
    async def btn_unban(self, itx: discord.Interaction, _button: discord.ui.Button):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Du musst in einer Lane sein.", ephemeral=True)
        user: discord.Member = itx.user  # type: ignore
        perms = lane.permissions_for(user)
        if not (self.cog.lane_owner.get(lane.id) == user.id or perms.manage_channels or perms.administrator):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Nur **Owner** der Lane (oder Mods) dürfen entbannen.", ephemeral=True)
        modal = BanModal(self.cog, lane, action="unban")
        await itx.response.send_modal(modal)

    @discord.ui.button(label="▶ Match gestartet", style=discord.ButtonStyle.primary, row=3, custom_id="tv_match_start")
    async def btn_match_start(self, itx: discord.Interaction, _button: discord.ui.Button):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Tritt zuerst **deiner Lane** bei.", ephemeral=True)
        if not await self._cooldown_ok(lane.id):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Bitte warte kurz (30s) bevor du erneut klickst.", ephemeral=True)
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
    async def btn_match_end(self, itx: discord.Interaction, _button: discord.ui.Button):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Tritt zuerst **deiner Lane** bei.", ephemeral=True)
        if not await self._cooldown_ok(lane.id):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Bitte warte kurz (30s) bevor du erneut klickst.", ephemeral=True)
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
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Tritt zuerst **deiner Lane** bei.", ephemeral=True)
        lane: discord.VoiceChannel = m.voice.channel

        if lane.category_id == RANKED_CATEGORY_ID:
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("ℹ️ **Ranked** wird extern verwaltet – Mindest-Rang hier nicht anwendbar.", ephemeral=True)

        choice = self.values[0]
        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        self.cog.lane_min_rank[lane.id] = choice
        await self.cog._apply_min_rank(lane, choice)
        await self.cog._refresh_name(lane, force=False)

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
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("User ist nicht (mehr) in der Lane.", ephemeral=True)
        staging = self.lane.guild.get_channel(CASUAL_STAGING_CHANNEL_ID)
        if not isinstance(staging, discord.VoiceChannel):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Staging-Channel nicht gefunden.", ephemeral=True)
        try:
            await target.move_to(staging, reason=f"Kick durch {itx.user}")
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            await sender(f"👢 **{target.display_name}** wurde in **Casual Staging** verschoben.", ephemeral=True)
        except Exception:
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            await sender("Konnte nicht verschieben.", ephemeral=True)

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
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Nur **Owner** der Lane (oder Mods) dürfen (un)bannen.", ephemeral=True)

        raw = str(self.target.value).strip()
        uid = None
        if raw.startswith("<@") and raw.endswith(">"):
            digits = "".join(ch for ch in raw if ch.isdigit())
            if digits:
                uid = int(digits)
        elif raw.isdigit():
            uid = int(raw)
        if not uid:
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Bitte @Mention ODER numerische ID angeben.", ephemeral=True)

        guild = self.lane.guild
        target_member = guild.get_member(uid)

        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        if self.action == "ban":
            self.cog.bans.add_ban(owner_id, uid)  # type: ignore
            try:
                if self.cog.worker.enabled:
                    ok = await self.cog.worker.set_connect(self.lane.id, uid, False)
                    if not ok:
                        await self.lane.set_permissions(target_member or discord.Object(id=uid), connect=False, reason=f"Owner-Ban durch {user} (fallback)")
                else:
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
                if self.cog.worker.enabled:
                    ok = await self.cog.worker.clear_overwrite(self.lane.id, uid)
                    if not ok:
                        await self.lane.set_permissions(target_member or discord.Object(id=uid), overwrite=None, reason=f"Owner-Unban durch {user} (fallback)")
                else:
                    await self.lane.set_permissions(target_member or discord.Object(id=uid), overwrite=None, reason=f"Owner-Unban durch {user}")
                await itx.followup.send("♻️ Nutzer entbannt.", ephemeral=True)
            except Exception:
                await itx.followup.send("Konnte Unban nicht setzen.", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoiceCog(bot))
