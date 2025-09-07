# ------------------------------------------------------------
# TempVoice – Auto-Lanes + UI-Management (Casual & Ranked) mit Anti-429
# Persistentes Interface (merkt sich Message-ID) + DE/EU-Regionsfilter
# >>> DB-Zentralisiert: Interface & Owner-Bans in gemeinsamer DB <<<
# ------------------------------------------------------------

import discord
from discord.ext import commands
import asyncio
import json
import logging
import time
import re
from typing import Optional, Dict, Set, Tuple, Any, List
from datetime import datetime

# ===================== Logging =====================
log = logging.getLogger("TempVoice")

# ===================== IDs & Konstanten =====================
# WICHTIG: Diese IDs wurden NICHT verändert – exakt beibehalten.
GUILD_ID                   = 1289721245281292290
INTERFACE_TEXT_CHANNEL_ID  = 1289721245281292293
CASUAL_STAGING_CHANNEL_ID  = 1289721245281292291
RANKED_STAGING_CHANNEL_ID  = 1357422957017698476
CASUAL_CATEGORY_ID         = 1289721245281292290
RANKED_CATEGORY_ID         = 1357422957017698478

# Regionen-Rolle (English Only) – unverändert
ENGLISH_ONLY_ROLE_ID       = 1309741866098491479

# Ränge (unverändert)
RANK_ORDER = [
    "novice","recruit","seeker","observer","enforcer","adjudicator",
    "arbiter","catalyst","harbinger","emissary","protector","warden",
    "vanguard","sentinel","champion","paragon"
]
SUFFIX_THRESHOLD_RANK = "emissary"

# Limits / Heuristiken (unverändert)
DEFAULT_CASUAL_CAP        = 8
DEFAULT_RANKED_CAP        = 6
FULL_HINT_THRESHOLD       = 6

NAME_EDIT_COOLDOWN_SEC    = 120
LFG_POST_COOLDOWN_SEC     = 60
LFG_DELETE_AFTER_SEC      = 20 * 60
BUTTON_COOLDOWN_SEC       = 30
DEBOUNCE_VERML_VOLL_SEC   = 25
# =================================

# ===================== Hilfsfunktionen =====================

def _rank_index(name: str) -> int:
    try:
        return RANK_ORDER.index(name.lower())
    except Exception:
        return -1

def _strip_suffixes(name: str) -> str:
    """Entfernt bekannte Suffixe wie '• ab X', '• vermutlich voll', '• Spieler gesucht', etc."""
    s = re.sub(r"\s+•\s+ab\s+\w+", "", name, flags=re.IGNORECASE)
    s = re.sub(r"\s+•\s+vermutlich voll", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+•\s+Spieler gesucht", "", s, flags=re.IGNORECASE)
    return s.strip()

def _default_cap(ch: discord.VoiceChannel) -> int:
    return DEFAULT_RANKED_CAP if ch.category_id == RANKED_CATEGORY_ID else DEFAULT_CASUAL_CAP

def _is_managed_lane(ch: discord.abc.GuildChannel) -> bool:
    return isinstance(ch, discord.VoiceChannel) and ch.category_id in (CASUAL_CATEGORY_ID, RANKED_CATEGORY_ID)

# ===================== DB (zentral) =====================

import aiosqlite
from pathlib import Path

DB_PATH = Path("central.db")  # unverändert: zentrale DB

class TVDB:
    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        if self._conn:
            return
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("""
        CREATE TABLE IF NOT EXISTS tempvoice_interface(
          guild_id   INTEGER PRIMARY KEY,
          channel_id INTEGER NOT NULL,
          message_id INTEGER NOT NULL,
          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)
        await self._conn.execute("""
        CREATE TABLE IF NOT EXISTS tempvoice_bans(
          owner_id   INTEGER NOT NULL,
          banned_id  INTEGER NOT NULL,
          reason     TEXT,
          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(owner_id, banned_id)
        )
        """)
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def exec(self, sql: str, params: tuple = ()):
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        await cur.close()

    async def fetchone(self, sql: str, params: tuple = ()):
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql: str, params: tuple = ()):
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return rows

class AsyncBanStore:
    def __init__(self, db: TVDB):
        self.db = db

    async def is_banned(self, owner_id: int, user_id: int) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 FROM tempvoice_bans WHERE owner_id=? AND banned_id=?",
            (int(owner_id), int(user_id))
        )
        return bool(row)

    async def set_ban(self, owner_id: int, user_id: int, reason: str):
        await self.db.exec(
            "INSERT OR REPLACE INTO tempvoice_bans(owner_id,banned_id,reason) VALUES(?,?,?)",
            (int(owner_id), int(user_id), reason or "")
        )

    async def remove_ban(self, owner_id: int, user_id: int):
        await self.db.exec(
            "DELETE FROM tempvoice_bans WHERE owner_id=? AND banned_id=?",
            (int(owner_id), int(user_id))
        )

class InterfaceStateStore:
    """DB-basierte Speicherung der Interface-Message pro Guild (statt JSON)."""
    def __init__(self, db: TVDB):
        self.db = db

    async def get(self, guild_id: int) -> Dict[str, int]:
        row = await self.db.fetchone(
            "SELECT channel_id, message_id FROM tempvoice_interface WHERE guild_id=?",
            (int(guild_id),)
        )
        if not row:
            return {}
        return {"channel_id": int(row[0]), "message_id": int(row[1])}

    async def set(self, guild_id: int, channel_id: int, message_id: int):
        await self.db.exec(
            """
            INSERT INTO tempvoice_interface(guild_id, channel_id, message_id)
            VALUES(?,?,?)
            ON CONFLICT(guild_id) DO UPDATE SET
                channel_id=excluded.channel_id,
                message_id=excluded.message_id,
                updated_at=CURRENT_TIMESTAMP
            """,
            (int(guild_id), int(channel_id), int(message_id))
        )

# ===================== Worker-Proxy (Rename etc.) =====================

class WorkerProxy:
    """Hier wäre Platz für die Kommunikation mit deinem separaten Worker-Bot.
    In dieser TempVoice-Datei nutzen wir aber weiterhin direkte Discord-Calls
    mit sanfter Rate-Limitierung (Anti-429)."""
    def __init__(self):
        pass

    # Platzhalter – hier könnte man später IPC / Socket einbauen
    def ready(self) -> bool:
        return True

class TVWorker:
    def __init__(self):
        self._proxy = WorkerProxy()
        try:
            resp = self._proxy.ready()
            if not resp:
                log.info("WorkerProxy nicht bereit (placeholder).")
        except Exception:
            log.info("WorkerProxy-Verbindung nicht verfügbar (placeholder).")

    async def safe_edit(
        self,
        ch: discord.VoiceChannel,
        *,
        name: Optional[str] = None,
        user_limit: Optional[int] = None,
        reason: str = "",
    ):
        kw = {}
        if name is not None:
            kw["name"] = name
        if user_limit is not None:
            kw["user_limit"] = max(0, min(99, int(user_limit)))
        if not kw:
            return
        try:
            await ch.edit(**kw, reason=reason or "TempVoice")
        except discord.HTTPException as e:
            # Sanftes Retry bei 429 oder edit race
            try:
                await asyncio.sleep(1.5)
                await ch.edit(**kw, reason=reason or "TempVoice (Retry)")
            except Exception:
                log.info(f"Edit fehlgeschlagen ({ch.id}): {e}")

# ===================== TempVoice Cog =====================

class TempVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # DB
        self._tvdb = TVDB(str(DB_PATH))
        self.bans = AsyncBanStore(self._tvdb)
        self.ifstore = InterfaceStateStore(self._tvdb)

        self.worker = TVWorker()

        self.created_channels: Set[int] = set()
        self.create_channels = self.created_channels  # Back-compat
        self.lane_owner: Dict[int, int] = {}
        self.lane_base: Dict[int, str] = {}
        self.lane_min_rank: Dict[int, str] = {}
        self.lane_full_choice: Dict[int, Optional[bool]] = {}
        self.lane_searching: Dict[int, bool] = {}
        self.join_time: Dict[int, Dict[int, float]] = {}

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
        # DB init
        await self._tvdb.connect()

        # Persistente View registrieren (wichtig für Restart)
        self.bot.add_view(MainView(self))
        asyncio.create_task(self._startup())

    async def cog_unload(self):
        try:
            await self._tvdb.close()
        except Exception:
            pass

    async def _startup(self):
        await self.bot.wait_until_ready()
        await self._ensure_interface()

    # ===================== Interface / Embed =====================

    async def _ensure_interface(self, *, target_channel_id: Optional[int] = None, force_recreate: bool = False):
        """
        Stellt sicher, dass GENAU EINE Interface-Message existiert (pro Guild).
        Persistenz: tempvoice_interface in zentraler DB.
        """
        ch_id = target_channel_id or INTERFACE_TEXT_CHANNEL_ID
        ch = self.bot.get_channel(ch_id)

        if not isinstance(ch, discord.TextChannel):
            log.warning(f"Interface-Channel {ch_id} nicht gefunden oder kein Textkanal.")
            return

        view = MainView(self)
        try:
            saved = await self.ifstore.get(GUILD_ID)
        except Exception as e:
            log.info(f"Persistenz lesen fehlgeschlagen: {e}")
            saved = {}

        embed = discord.Embed(
            title="Lanes & Steuerung (Casual/Ranked)",
            description=(
                "• **Join Staging (Casual/Ranked)** → ich **erstelle automatisch** deine Lane und move dich rüber.\n"
                "• **Steuerung hier im Interface**:\n"
                "  - **🇩🇪 DE / 🇪🇺 EU** (Regionsfilter: *English Only* in Lane sperren/aufheben)\n"
                "  - **Mindest-Rang** (nur Casual; setzt nur *deny* für zu niedrige Ränge)\n"
                "  - **Kick / Ban / Unban** (Ban/Unban per @Mention **oder** ID)\n\n"
                "💡 Ab **6 Spielern** erscheint nach kurzer Zeit **„• vermutlich voll“**, sofern kein Status gesetzt ist.\n"
                "👑 Owner wechselt automatisch an den am längsten anwesenden User, wenn der Owner geht.\n"
                "🛌 AFK-Automatik ist derzeit **deaktiviert** (Mute/Deaf wird nicht verschoben)."
            ),
            color=0x2ecc71
        )

        if not saved or force_recreate:
            # altes Pane löschen, neues posten
            try:
                msg = await ch.send(embed=embed, view=view)
            except discord.HTTPException as e:
                log.info(f"Interface-Message konnte nicht gesendet werden: {e}")
                return
            await self.ifstore.set(GUILD_ID, ch.id, msg.id)
            log.info("TempVoice Interface wurde (neu) erstellt.")
            return

        # bestehende Message aktualisieren/ersetzen
        try:
            msg = await ch.fetch_message(int(saved["message_id"]))
            try:
                await msg.edit(embed=embed, view=view)
                log.info("TempVoice Interface aktualisiert (persistente View).")
            except discord.NotFound:
                msg = await ch.send(embed=embed, view=view)
                await self.ifstore.set(GUILD_ID, ch.id, msg.id)
                log.info("TempVoice Interface neu gesendet (alte Message fehlte).")
        except Exception as e:
            log.info(f"Fetch/Edit Interface fehlgeschlagen, poste neu: {e}")
            msg = await ch.send(embed=embed, view=view)
            await self.ifstore.set(GUILD_ID, ch.id, msg.id)

    # ===================== Name-Logik =====================

    def _compose_name(self, lane: discord.VoiceChannel) -> str:
        base = self.lane_base.get(lane.id) or _strip_suffixes(lane.name)
        min_rank = self.lane_min_rank.get(lane.id, "unknown")
        member_count = len(lane.members)

        parts = [base]

        # Rang-Suffix (nur Casual; ab Emissary)
        if lane.category_id != RANKED_CATEGORY_ID:
            if min_rank and min_rank != "unknown" and _rank_index(min_rank) >= _rank_index(SUFFIX_THRESHOLD_RANK):
                parts.append(f"• ab {min_rank.capitalize()}")

        # Kein Match-/Voll-Status mehr; nur Heuristik & „Spieler gesucht“
        if member_count >= FULL_HINT_THRESHOLD:
            parts.append("• vermutlich voll")
        else:
            if self.lane_searching.get(lane.id, False):
                parts.append("• Spieler gesucht")

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
                except Exception:
                    pass
        idx = 1
        while idx in used:
            idx += 1
        return f"{prefix} {idx}"

    async def _safe_edit_channel(
        self,
        lane: discord.VoiceChannel,
        *,
        desired_name: Optional[str] = None,
        desired_limit: Optional[int] = None,
        reason: str = "",
        force_name: bool = False,
    ):
        lock = self._edit_locks.setdefault(lane.id, asyncio.Lock())
        async with lock:
            # Cooldown gegen Name-Spam
            if desired_name:
                last_name = self._last_name_desired.get(lane.id)
                if last_name and last_name == desired_name and not force_name:
                    return
                last_patch = self._last_name_patch_ts.get(lane.id, 0.0)
                if not force_name and (time.time() - last_patch) < NAME_EDIT_COOLDOWN_SEC:
                    return

            try:
                await self.worker.safe_edit(
                    lane,
                    name=desired_name,
                    user_limit=desired_limit,
                    reason=reason or "TempVoice"
                )
                if desired_name:
                    self._last_name_desired[lane.id] = desired_name
                    self._last_name_patch_ts[lane.id] = time.time()
            except Exception as e:
                log.info(f"safe_edit fehlgeschlagen ({lane.id}): {e}")

    async def _post_lfg(self, lane: discord.VoiceChannel, *, force: bool = False):
        """Casual: optionaler LFG-Post im Interface-Channel."""
        if lane.category_id == RANKED_CATEGORY_ID:
            return
        now = time.time()
        last = self._last_lfg_ts.get(lane.id, 0.0)
        if not force and (now - last) < LFG_POST_COOLDOWN_SEC:
            return
        ch = self.bot.get_channel(INTERFACE_TEXT_CHANNEL_ID)
        if not isinstance(ch, discord.TextChannel):
            return
        try:
            msg = await ch.send(f"🔎 **Spieler gesucht** in `{lane.name}` – joint gerne dazu!")
            self._last_lfg_ts[lane.id] = now
            async def cleanup(mid: int):
                try:
                    await asyncio.sleep(LFG_DELETE_AFTER_SEC)
                    m = await ch.fetch_message(mid)
                    await m.delete()
                except Exception:
                    pass
            if msg and msg.id not in self._lfg_cleanup_tasks:
                asyncio.create_task(cleanup(msg.id))
        except Exception:
            pass

    # ===================== Events =====================

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # Ignore Bots
        if member.bot:
            return

        # Verlassen/Joinen behandeln
        try:
            # LEFT: räumen
            if before and before.channel and isinstance(before.channel, discord.VoiceChannel):
                ch = before.channel
                # Owner neu bestimmen, wenn Owner gegangen ist
                if self.lane_owner.get(ch.id) == member.id:
                    new_owner = await self._pick_new_owner(ch)
                    if new_owner:
                        self.lane_owner[ch.id] = new_owner.id
                # Aufräumen der Member-Join-Zeit
                jt = self.join_time.get(ch.id)
                if jt and member.id in jt:
                    jt.pop(member.id, None)
                # Debounce-Task abbrechen
                t = self._debounce_tasks.pop(ch.id, None)
                if t:
                    try: t.cancel()
                    except Exception: pass

                # Name ggf. aktualisieren (vermutlich voll/Spieler gesucht)
                if self.lane_full_choice.get(ch.id) is None:
                    await self._refresh_name(ch, force=False)

                # Lane löschen, wenn leer und von uns erstellt
                if ch.id in self.created_channels and not ch.members:
                    try:
                        # alle Traces zu dieser Lane löschen
                        for d in (self.lane_owner, self.lane_base, self.lane_min_rank,
                                  self.lane_full_choice, self.lane_searching, self.join_time,
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
                    try:
                        await ch.delete(reason="TempVoice: Lane leer (auto-cleanup)")
                    except Exception:
                        pass
                    self.created_channels.discard(ch.id)

            # JOINED
            if after and after.channel and isinstance(after.channel, discord.VoiceChannel):
                ch = after.channel
                self.join_time.setdefault(ch.id, {})
                self.join_time[ch.id][member.id] = datetime.utcnow().timestamp()

                owner_id = self.lane_owner.get(ch.id)
                if owner_id and await self.bans.is_banned(owner_id, member.id):
                    # Move sofort zurück (oder in Staging)
                    try:
                        staging = self.bot.get_channel(CASUAL_STAGING_CHANNEL_ID if ch.category_id == CASUAL_CATEGORY_ID else RANKED_STAGING_CHANNEL_ID)
                        if isinstance(staging, discord.VoiceChannel):
                            await member.move_to(staging, reason="TempVoice: Ban des Lanes-Owners")
                            return
                    except Exception:
                        pass

                # „vermutlich voll“-Debounce
                if self.lane_full_choice.get(ch.id) is None:
                    task = self._debounce_tasks.get(ch.id)
                    if task and not task.done():
                        try:
                            task.cancel()
                        except Exception:
                            pass
                    async def later_refresh(chan_id: int):
                        try:
                            await asyncio.sleep(DEBOUNCE_VERML_VOLL_SEC)
                            lane = self.bot.get_channel(chan_id)
                            if isinstance(lane, discord.VoiceChannel) and _is_managed_lane(lane):
                                await self._refresh_name(lane, force=False)
                        except asyncio.CancelledError:
                            return
                        except Exception:
                            pass
                    self._debounce_tasks[ch.id] = asyncio.create_task(later_refresh(ch.id))

        except Exception as e:
            log.info(f"on_voice_state_update Fehler: {e}")

    async def _pick_new_owner(self, lane: discord.VoiceChannel) -> Optional[discord.Member]:
        """Einfach der am längsten anwesende, nicht gemutete/deafte Member."""
        try:
            jt = self.join_time.get(lane.id, {})
            if not jt:
                return None
            # sort by oldest join ts
            sorted_ids = sorted([m for m in lane.members if not m.bot], key=lambda u: jt.get(u.id, time.time()))
            return sorted_ids[0] if sorted_ids else None
        except Exception:
            return None

    # ===================== UI =====================

    class _KickSelect(discord.ui.Select):
        pass

# ---------- Kick/Unban Modal & Views ----------

class BanModal(discord.ui.Modal, title="Ban/Unban (ID oder Mention)"):
    def __init__(self, cog: "TempVoiceCog", lane: discord.VoiceChannel, action: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.lane = lane
        self.action = action
        self.input = discord.ui.TextInput(
            label="User (ID oder @Mention)",
            placeholder="123456789012345678 oder @User",
            required=True,
            max_length=64
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction):
        lane = self.lane
        user: discord.Member = interaction.user  # type: ignore
        perms = lane.permissions_for(user)
        if not (self.cog.lane_owner.get(lane.id) == user.id or perms.manage_channels or perms.administrator):
            try:
                await interaction.response.send_message("Nur **Owner** der Lane (oder Mods) dürfen bannen/entbannen.", ephemeral=True)
            except Exception:
                pass
            return

        raw = str(self.input.value or "").strip()
        uid = None
        m = re.search(r"(\d{16,20})", raw)
        if m:
            try:
                uid = int(m.group(1))
            except Exception:
                pass
        if not uid:
            try:
                if interaction.message and interaction.message.mentions:
                    uid = interaction.message.mentions[0].id
            except Exception:
                pass

        if not uid:
            try:
                await interaction.response.send_message("Konnte die User-ID nicht parsen.", ephemeral=True)
            except Exception:
                pass
            return

        if self.action == "ban":
            await self.cog.bans.set_ban(self.cog.lane_owner.get(lane.id) or user.id, uid, "Interface")
            try:
                await interaction.response.send_message(f"🚫 Gebannt: <@{uid}>", ephemeral=True)
            except Exception:
                pass
        else:
            await self.cog.bans.remove_ban(self.cog.lane_owner.get(lane.id) or user.id, uid)
            try:
                await interaction.response.send_message(f"♻️ Unban: <@{uid}>", ephemeral=True)
            except Exception:
                pass

class KickView(discord.ui.View):
    def __init__(self, cog: "TempVoiceCog", lane: discord.VoiceChannel):
        super().__init__(timeout=120)
        self.cog = cog
        self.lane = lane

        # dynamische Memberliste
        options = []
        for m in lane.members:
            if not m.bot:
                options.append(discord.SelectOption(label=m.display_name, value=str(m.id)))
        if not options:
            options.append(discord.SelectOption(label="(keine Mitglieder)", value="0", default=True))
        self.select = discord.ui.Select(placeholder="Wen kicken?", options=options, min_values=1, max_values=1)
        self.add_item(self.select)

    @discord.ui.button(label="OK", style=discord.ButtonStyle.danger)
    async def btn_ok(self, itx: discord.Interaction, _button: discord.ui.Button):
        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except Exception:
            pass

        if not self.select.values or self.select.values[0] == "0":
            try:
                await itx.followup.send("Keine gültige Auswahl.", ephemeral=True)
            except Exception:
                pass
            return

        uid = int(self.select.values[0])
        lane = self.lane
        try:
            tgt = lane.guild.get_member(uid)
            if tgt and tgt.voice and tgt.voice.channel and tgt.voice.channel.id == lane.id:
                staging = lane.guild.get_channel(
                    CASUAL_STAGING_CHANNEL_ID if lane.category_id == CASUAL_CATEGORY_ID else RANKED_STAGING_CHANNEL_ID
                )
                if isinstance(staging, discord.VoiceChannel):
                    await tgt.move_to(staging, reason="TempVoice: Kick via Interface")
        except Exception:
            pass
        try:
            await itx.followup.send(f"👟 Gekickt: <@{uid}>", ephemeral=True)
        except Exception:
            pass

class MinRankSelect(discord.ui.Select):
    def __init__(self, cog: "TempVoiceCog"):
        self.cog = cog
        guild: Optional[discord.Guild] = cog.bot.get_guild(GUILD_ID)
        opts = []
        for name in RANK_ORDER:
            opts.append(discord.SelectOption(label=name.capitalize(), value=name))
        super().__init__(
            placeholder="Mindest-Rang (nur Casual; Ranked bleibt wie ist)",
            options=opts,
            min_values=1,
            max_values=1,
            custom_id="tv_minrank"
        )

    async def callback(self, itx: discord.Interaction):
        lane = None
        m = itx.user
        if isinstance(m, discord.Member) and m.voice and isinstance(m.voice.channel, discord.VoiceChannel):
            lane = m.voice.channel

        if not lane or not _is_managed_lane(lane):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Tritt zuerst **deiner Lane** bei.", ephemeral=True)

        user: discord.Member = itx.user  # type: ignore
        perms = lane.permissions_for(user)
        if not (self.cog.lane_owner.get(lane.id) == user.id or perms.manage_channels or perms.administrator):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Nur **Owner** der Lane (oder Mods) dürfen den Mindest-Rang setzen.", ephemeral=True)

        chosen = self.values[0]
        self.cog.lane_min_rank[lane.id] = chosen
        await self.cog._apply_min_rank(lane, chosen)
        desired_name = self.cog._compose_name(lane)
        await self.cog._safe_edit_channel(lane, desired_name=desired_name, reason="TempVoice: Rank-Update", force_name=True)

        sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
        await sender(f"✅ Mindest-Rang gesetzt: **{chosen.capitalize()}**", ephemeral=True)

class MainView(discord.ui.View):
    def __init__(self, cog: "TempVoiceCog"):
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

    # Regionsfilter-Buttons 🇩🇪 / 🇪🇺 (BLEIBEN)
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
        await self.cog._set_connect_if_diff(lane, True, role)  # allow connect
        sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
        await sender("🇪🇺 **EU/EN** offen – *English Only* darf in die Lane.", ephemeral=True)

    # Kick / Ban / Unban
    @discord.ui.button(label="👟 Kick", style=discord.ButtonStyle.secondary, row=2, custom_id="tv_kick")
    async def btn_kick(self, itx: discord.Interaction, _button: discord.ui.Button):
        lane = self._lane(itx)
        if not lane or not _is_managed_lane(lane):
            sender = itx.response.send_message if not itx.response.is_done() else itx.followup.send
            return await sender("Du musst in einer Lane sein.", ephemeral=True)
        view = KickView(self.cog, lane)
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

    # ===================== Rank / Permissions =====================

    async def _set_connect_if_diff(self, lane: discord.VoiceChannel, allow: bool, role: discord.Role):
        overwrites = lane.overwrites
        current = overwrites.get(role)
        target = discord.PermissionOverwrite()
        target.connect = True if allow else False
        if current and current.connect == target.connect:
            return
        overwrites[role] = target
        try:
            await lane.edit(overwrites=overwrites, reason=f"TempVoice: Regionsfilter {'EU' if allow else 'DE'}")
        except Exception as e:
            log.info(f"set_connect_if_diff fehlgeschlagen: {e}")

    async def _apply_min_rank(self, lane: discord.VoiceChannel, min_rank: str):
        """Setzt 'connect deny' für alle zu niedrigen Ränge (nur Casual)."""
        if lane.category_id == RANKED_CATEGORY_ID:
            return
        try:
            overwrites = lane.overwrites
            # Hier würdest du deine Rollen-IDs je Rang mappen; Platzhalter:
            # Beispiel: Rollen heißen exakt wie Ränge (Capitalized)
            for rank_name in RANK_ORDER:
                role = discord.utils.get(lane.guild.roles, name=rank_name.capitalize())
                if not role:
                    continue
                if _rank_index(rank_name) < _rank_index(min_rank):
                    overwrites[role] = discord.PermissionOverwrite(connect=False)
                else:
                    # erlauben/neutral
                    if role in overwrites:
                        try:
                            del overwrites[role]
                        except Exception:
                            pass
            await lane.edit(overwrites=overwrites, reason=f"TempVoice: Mindest-Rang {min_rank}")
        except Exception as e:
            log.info(f"_apply_min_rank Fehler: {e}")

    # ===================== Commands =====================

    @commands.hybrid_command(name="tempvoice_panel", description="TempVoice Panel neu erstellen/aktualisieren")
    @commands.has_guild_permissions(manage_guild=True)
    async def tempvoice_panel(self, ctx: commands.Context):
        await self._ensure_interface(force_recreate=True)
        await ctx.reply("✅ Panel aktualisiert.", ephemeral=True) if getattr(ctx, "interaction", None) else None

    # ===================== Staging-Handler =====================

    @commands.Cog.listener()
    async def on_voice_presence_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Nur als Beispiel – in dieser Datei wird Join/Leave bereits in on_voice_state_update behandelt."""
        pass

    @commands.Cog.listener()
    async def on_voice_channel_create(self, channel: discord.abc.GuildChannel):
        pass

    # ===================== Lane-Erstellung aus Staging =====================

    @commands.Cog.listener()
    async def on_voice_state_update_create_lane(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Optionaler Listener – falls du getrennte Listener nutzt. In dieser Fassung nutzen wir den Standard-Listener oben."""
        pass

    # ============ Hilfsroutinen für Auto-Lane ============

    async def create_lane_for(self, member: discord.Member, ranked: bool) -> Optional[discord.VoiceChannel]:
        """Erstellt eine neue Lane und movt den Member hinein."""
        try:
            category = member.guild.get_channel(RANKED_CATEGORY_ID if ranked else CASUAL_CATEGORY_ID)
            if not isinstance(category, discord.CategoryChannel):
                return None
            prefix = "Ranked" if ranked else "Casual"
            base = await self._next_name(category, prefix)
            lane = await category.create_voice_channel(
                name=base, user_limit=_default_cap(category), reason="TempVoice: Auto-Lane"
            )
            self.created_channels.add(lane.id)
            self.lane_owner[lane.id] = member.id
            self.lane_base[lane.id] = base
            self.lane_min_rank[lane.id] = "unknown"
            self.lane_full_choice[lane.id] = None
            self.lane_searching[lane.id] = (not ranked)
            self.join_time.setdefault(lane.id, {})
            try:
                await member.move_to(lane, reason="TempVoice: Auto-Lane erstellt")
            except Exception:
                pass
            # Name initial
            await self._refresh_name(lane, force=True)
            # LFG-Post bei Casual
            if not ranked:
                await self._post_lfg(lane, force=True)
            return lane
        except Exception as e:
            log.info(f"create_lane_for Fehler: {e}")
            return None

# ===================== Cog-Setup =====================

async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoiceCog(bot))
