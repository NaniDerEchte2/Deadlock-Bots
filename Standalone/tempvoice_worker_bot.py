# service/tempvoice_worker_bot.py
# TempVoice Worker – Socket-Server + LiveMatch-Renamer
# Start: python -m service.tempvoice_worker_bot
# python -m shared.tempvoice_worker_bot 
#######################################################################
import asyncio
import logging
import os
import signal
import sqlite3
import re
import unicodedata
from typing import Any, Dict, Optional, Union, Tuple

import discord
from discord.ext import commands

# .env laden
try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    _env_file = find_dotenv(usecwd=True)
    if _env_file:
        load_dotenv(_env_file)
except Exception:
    pass

# interner Socket-Server
from shared.socket_bus import JSONLineServer  # type: ignore

# ===== Logging =====
LOG_LEVEL = os.getenv("WORKER_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tempvoice_worker")

# ===== Token / Intents =====
TOKEN = os.getenv("DISCORD_TOKEN_WORKER") or os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("❌ DISCORD_TOKEN_WORKER/DISCORD_TOKEN fehlt")

intents = discord.Intents.none()
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ===== Socket-Server =====
HOST = os.getenv("SOCKET_HOST", "127.0.0.1")
PORT = int(os.getenv("SOCKET_PORT", "45679"))

# ===== LiveMatch-Renamer (feste Defaults, keine ENV-Konfig-Schlacht) =====
LIVE_MATCH_ENABLE = (os.getenv("LIVE_MATCH_ENABLE", "1") == "1")
LIVE_DB_PATH = (os.getenv("DEADLOCK_DB_PATH") or
                os.getenv("LIVE_DB_PATH") or
                os.path.expandvars(r"%USERPROFILE%/Documents/Deadlock/service/deadlock.sqlite3")).strip()
LIVE_TICK_SEC = 20
NAME_EDIT_COOLDOWN_SEC = 300        # << pro Channel 1 Rename / 5 Minuten
RATE_LIMIT_BACKOFF_SEC = 380        # Backoff bei 429

# Regex: „ • n/cap Im Match|Im Spiel|Lobby/Queue“
MATCH_SUFFIX_RX = re.compile(
    r"\s+•\s+\d+/\d+\s+(im\s+match|im\s+spiel|lobby/queue)",
    re.IGNORECASE,
)

_socket_server: Optional[JSONLineServer] = None
_last_rename_ts: Dict[int, float] = {}        # channel_id -> last successful attempt ts
_ratelimit_until: Dict[int, float] = {}       # channel_id -> monotonic ts bis wir wieder dürfen

# ===== DB =====
def _ensure_dirs(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

def _db_connect() -> Optional[sqlite3.Connection]:
    try:
        _ensure_dirs(LIVE_DB_PATH)
        con = sqlite3.connect(LIVE_DB_PATH, check_same_thread=False)
        con.row_factory = sqlite3.Row
        return con
    except Exception as e:
        logger.error("DB connect fehlgeschlagen: %s", e)
        return None

def _ensure_schema(con: sqlite3.Connection, *, log_once: bool = True) -> None:
    cur = con.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS live_lane_state(
          channel_id  INTEGER PRIMARY KEY,
          is_active   INTEGER DEFAULT 0,
          started_at  INTEGER,
          last_update INTEGER,
          minutes     INTEGER DEFAULT 0,
          suffix      TEXT,
          reason      TEXT
        );
        CREATE TABLE IF NOT EXISTS live_lane_members(
          channel_id INTEGER NOT NULL,
          user_id    INTEGER NOT NULL,
          in_match   INTEGER DEFAULT 0,
          server_id  TEXT,
          checked_ts INTEGER,
          PRIMARY KEY(channel_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS steam_links(
          user_id   INTEGER NOT NULL,
          steam_id  TEXT    NOT NULL,
          PRIMARY KEY(user_id, steam_id)
        );
        CREATE INDEX IF NOT EXISTS idx_lls_active  ON live_lane_state(is_active);
        CREATE INDEX IF NOT EXISTS idx_llm_channel ON live_lane_members(channel_id);
        CREATE INDEX IF NOT EXISTS idx_llm_checked ON live_lane_members(checked_ts);
    """)
    con.commit()
    if log_once:
        logger.info("🗄️  DB-Schema gewährleistet (live_lane_state, live_lane_members, steam_links).")

# ===== Utils =====
def _norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = " ".join(s.split())
    return s.casefold()

def _strip_suffix(name: str) -> str:
    return MATCH_SUFFIX_RX.sub("", name).strip()

def _extract_suffix(name: str) -> str:
    m = MATCH_SUFFIX_RX.search(name)
    return m.group(0).strip() if m else ""

async def _get_channel_anywhere(channel_id: int) -> Optional[discord.abc.GuildChannel]:
    ch = bot.get_channel(channel_id)
    if ch:
        return ch
    try:
        return await bot.fetch_channel(channel_id)
    except Exception:
        return None

# ===== Voice Join Logs (Account linked) =====
def _account_linked_status(con: Optional[sqlite3.Connection], user_id: int) -> Tuple[str, Optional[int]]:
    if con is None:
        return ("DB-ERR", None)
    try:
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM steam_links WHERE user_id=?", (int(user_id),))
        row = cur.fetchone()
        n = int(row["n"]) if row else 0
        return ("OK", n) if n > 0 else ("NO-LINK", 0)
    except Exception:
        return ("DB-ERR", None)

def _join_log_prefix(member: discord.Member, channel: discord.abc.GuildChannel) -> str:
    return f"{member} -> {getattr(channel, 'name', '??')} ({getattr(channel, 'id', '??')})"

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if after.channel and (before.channel is None or (before.channel and before.channel.id != after.channel.id)):
        con = _db_connect()
        if con:
            try:
                status, n = _account_linked_status(con, member.id)
            finally:
                con.close()
        else:
            status, n = ("DB-ERR", None)
        if status == "OK":
            logger.info("JOIN: %s | linked: YES (%d)", _join_log_prefix(member, after.channel), n)  # type: ignore
        elif status == "NO-LINK":
            logger.warning("JOIN: %s | linked: NO", _join_log_prefix(member, after.channel))       # type: ignore
        else:
            logger.error("JOIN: %s | linked: (DB-ERR)", _join_log_prefix(member, after.channel))    # type: ignore

# ===== Safe Rename (mit Coalescing + 5-Min Cooldown) =====
async def _safe_rename(ch: discord.VoiceChannel, desired: str, *, reason: str) -> bool:
    if not desired:
        return False

    now = asyncio.get_event_loop().time()

    # Backoff nach 429
    until = _ratelimit_until.get(ch.id, 0.0)
    if now < until:
        return False

    # Delta-Check
    current = ch.name
    if _norm(current) == _norm(desired):
        return False

    # Per-Channel Cooldown
    last = _last_rename_ts.get(ch.id, 0.0)
    if (now - last) < NAME_EDIT_COOLDOWN_SEC:
        return False

    try:
        await ch.edit(name=desired, reason=reason)
        _last_rename_ts[ch.id] = now
        logger.info("Umbenannt: %s -> %s", current, desired)
        return True
    except discord.HTTPException as e:
        if getattr(e, "status", None) == 429:
            _ratelimit_until[ch.id] = now + RATE_LIMIT_BACKOFF_SEC
            logger.warning("Rate-limit 429 auf %s – pausiere %ss", ch.id, RATE_LIMIT_BACKOFF_SEC)
        else:
            logger.info("Rename fehlgeschlagen (%s): %s", ch.id, e)
        return False
    except Exception as e:
        logger.info("Rename fehlgeschlagen (%s): %s", ch.id, e)
        return False

# ===== Socket-OPs =====
async def handle_op(payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        op = (payload.get("op") or "").lower()

        if op == "ping":
            return {"ok": True, "pong": True}

        def _as_int(x: Union[str, int, None]) -> Optional[int]:
            try:
                return int(x) if x is not None else None
            except Exception:
                return None

        # Channel-gebundene Ops
        if op in {"edit_channel", "set_permissions", "delete_channel", "rename_match_suffix", "clear_match_suffix"}:
            channel_id = _as_int(payload.get("channel_id"))
            if not channel_id:
                return {"ok": False, "error": "channel_id fehlt/ungültig"}

            channel = await _get_channel_anywhere(channel_id)
            if channel is None:
                return {"ok": False, "error": f"Channel {channel_id} nicht gefunden"}

            if op == "edit_channel":
                if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                    return {"ok": False, "error": f"Channel {channel_id} ist kein Voice/StageChannel"}
                kw: Dict[str, Any] = {}
                if "name" in payload:
                    kw["name"] = str(payload["name"])
                if "user_limit" in payload and payload["user_limit"] is not None:
                    kw["user_limit"] = int(payload["user_limit"])
                if "bitrate" in payload and payload["bitrate"] is not None:
                    kw["bitrate"] = int(payload["bitrate"])
                try:
                    await channel.edit(**kw, reason="TempVoice Worker: edit_channel")
                    return {"ok": True}
                except Exception as e:
                    return {"ok": False, "error": f"edit_channel fehlgeschlagen: {type(e).__name__}: {e}"}

            if op == "set_permissions":
                if not isinstance(channel, discord.abc.GuildChannel):
                    return {"ok": False, "error": f"Channel {channel_id} ist kein GuildChannel"}
                target_id = _as_int(payload.get("target_id"))
                if not target_id:
                    return {"ok": False, "error": "target_id fehlt/ungültig"}
                target = channel.guild.get_member(target_id) or channel.guild.get_role(target_id)
                if target is None:
                    return {"ok": False, "error": f"Target {target_id} nicht gefunden"}
                overwrite_delta = payload.get("overwrite") or {}
                try:
                    ow = channel.overwrites_for(target)
                    for perm, val in overwrite_delta.items():
                        setattr(ow, perm, (None if val is None else bool(val)))
                    await channel.set_permissions(target, overwrite=ow, reason="TempVoice Worker: set_permissions")
                    return {"ok": True}
                except Exception as e:
                    return {"ok": False, "error": f"set_permissions fehlgeschlagen: {type(e).__name__}: {e}"}

            if op == "delete_channel":
                try:
                    await channel.delete(reason=str(payload.get("reason") or "TempVoice Worker: delete_channel"))
                    return {"ok": True}
                except Exception as e:
                    return {"ok": False, "error": f"delete_channel fehlgeschlagen: {type(e).__name__}: {e}"}

            if op == "rename_match_suffix":
                if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                    return {"ok": False, "error": f"Channel {channel_id} ist kein Voice/StageChannel"}
                current = channel.name
                base = _strip_suffix(current)
                suffix = (payload.get("suffix") or "").strip()
                desired = base if not suffix else f"{base} {suffix}"
                ok = await _safe_rename(channel, desired, reason=str(payload.get("reason") or "LiveMatch"))
                return {"ok": ok, "base": base, "desired": desired}

            if op == "clear_match_suffix":
                if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                    return {"ok": False, "error": f"Channel {channel_id} ist kein Voice/StageChannel"}
                base = _strip_suffix(channel.name)
                ok = await _safe_rename(channel, base, reason=str(payload.get("reason") or "LiveMatch clear"))
                return {"ok": ok, "base": base, "desired": base}

        # move_member
        if op == "move_member":
            user_id = _as_int(payload.get("user_id"))
            dest_channel_id = _as_int(payload.get("dest_channel_id"))
            if not user_id or not dest_channel_id:
                return {"ok": False, "error": "user_id/dest_channel_id fehlen/ungültig"}

            guild = None
            guild_id = _as_int(payload.get("guild_id"))
            if guild_id:
                guild = bot.get_guild(guild_id)
            if guild is None:
                dest_ch = await _get_channel_anywhere(dest_channel_id)
                if isinstance(dest_ch, discord.abc.GuildChannel):
                    guild = dest_ch.guild
            if guild is None:
                return {"ok": False, "error": "Guild konnte nicht bestimmt werden"}

            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
            dest_ch = await _get_channel_anywhere(dest_channel_id)
            if not isinstance(dest_ch, (discord.VoiceChannel, discord.StageChannel)):
                return {"ok": False, "error": f"Zielchannel {dest_channel_id} ist kein Voice/StageChannel"}

            try:
                await member.move_to(dest_ch, reason="TempVoice Worker: move_member")  # type: ignore
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": f"move_member fehlgeschlagen: {type(e).__name__}: {e}"}

        # create_voice
        if op == "create_voice":
            name = payload.get("name")
            if not name:
                return {"ok": False, "error": "name fehlt"}

            def _as_int(x: Union[str, int, None]) -> Optional[int]:
                try:
                    return int(x) if x is not None else None
                except Exception:
                    return None

            guild = None
            guild_id = _as_int(payload.get("guild_id"))
            if guild_id:
                guild = bot.get_guild(guild_id)
            if guild is None:
                channel_id = _as_int(payload.get("channel_id"))
                if channel_id:
                    any_ch = await _get_channel_anywhere(channel_id)
                    if isinstance(any_ch, discord.abc.GuildChannel):
                        guild = any_ch.guild
            if guild is None:
                return {"ok": False, "error": "Guild konnte nicht bestimmt werden"}

            category_id = _as_int(payload.get("category_id"))
            category = None
            if category_id:
                cat_ch = await _get_channel_anywhere(category_id)
                category = cat_ch if isinstance(cat_ch, discord.CategoryChannel) else None

            user_limit = _as_int(payload.get("user_limit"))
            bitrate = _as_int(payload.get("bitrate"))
            reason = payload.get("reason") or "TempVoice Worker: create_voice"

            try:
                new_ch = await guild.create_voice_channel(  # type: ignore
                    name=str(name),
                    category=category,
                    user_limit=user_limit,
                    bitrate=bitrate,
                    reason=str(reason),
                )
                return {"ok": True, "channel_id": new_ch.id, "name": new_ch.name}
            except Exception as e:
                return {"ok": False, "error": f"create_voice fehlgeschlagen: {type(e).__name__}: {e}"}

        return {"ok": False, "error": f"unbekannte op: {payload.get('op')!r}"}

    except Exception as e:
        logger.exception("handle_op – unerwarteter Fehler")
        return {"ok": False, "error": f"unexpected: {type(e).__name__}: {e}"}

# ===== Socket Server Control =====
def start_socket_server(loop: asyncio.AbstractEventLoop) -> None:
    global _socket_server
    def handler(req: Dict[str, Any]) -> Dict[str, Any]:
        fut = asyncio.run_coroutine_threadsafe(handle_op(req), loop)
        return fut.result(timeout=15.0)
    _socket_server = JSONLineServer(HOST, PORT, handler)
    _socket_server.start()
    logger.info("🔌 Worker Socket-Server läuft auf %s:%s", HOST, PORT)

def stop_socket_server() -> None:
    global _socket_server
    try:
        if _socket_server:
            _socket_server.stop()
            _socket_server = None
            logger.info("🔌 Worker Socket-Server gestoppt")
    except Exception:
        pass

# ===== Renamer Loop =====
async def _live_match_tick():
    con = _db_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute("SELECT channel_id, suffix FROM live_lane_state")
        rows = cur.fetchall()
    except Exception as e:
        logger.warning("live_match_tick – DB-Fehler: %s", e)
        try:
            con.close()
        except Exception:
            pass
        return
    finally:
        try:
            con.close()
        except Exception:
            pass

    for r in rows:
        ch = await _get_channel_anywhere(int(r["channel_id"]))
        if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
            continue

        current_suffix = _extract_suffix(ch.name)    # z.B. "• 1/8 Im Spiel"
        db_suffix = (r["suffix"] or "").strip() if r["suffix"] is not None else ""

        if _norm(current_suffix) == _norm(db_suffix):
            continue

        base = _strip_suffix(ch.name)
        desired = base if not db_suffix else f"{base} {db_suffix}"
        await _safe_rename(ch, desired, reason="LiveMatch-Renamer")

async def live_match_runner():
    if not LIVE_MATCH_ENABLE:
        logger.info("LiveMatch-Renamer deaktiviert (LIVE_MATCH_ENABLE!=1).")
        return
    if not LIVE_DB_PATH:
        logger.info("LiveMatch-Renamer: LIVE_DB_PATH nicht gesetzt – aus.")
        return
    logger.info("LiveMatch-Renamer aktiv (Tick=%ss, DB=%s)", LIVE_TICK_SEC, LIVE_DB_PATH)
    while not bot.is_closed():
        try:
            await _live_match_tick()
        except Exception:
            logger.exception("LiveMatch-Renamer Tick-Fehler")
        await asyncio.sleep(LIVE_TICK_SEC)

# ===== Ready / Shutdown =====
@bot.event
async def on_ready():
    logger.info("✅ Worker Bot eingeloggt als %s (ID: %s)", bot.user, bot.user.id)  # type: ignore
    g = ", ".join(f"{guild.name}({guild.id})" for guild in bot.guilds)
    logger.info("   Guilds: %s", g or "–")
    con = _db_connect()
    if con:
        try:
            _ensure_schema(con, log_once=True)
        finally:
            con.close()
    loop = asyncio.get_running_loop()
    start_socket_server(loop)
    bot.loop.create_task(live_match_runner())

def _install_signal_handlers():
    def _graceful_shutdown(signum, frame):
        logger.info("Beende Worker (%s)...", signal.Signals(signum).name)
        stop_socket_server()
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(bot.close())
        except Exception:
            pass
    for s in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if s is not None:
            try:
                signal.signal(s, _graceful_shutdown)
            except Exception:
                pass

def main():
    _install_signal_handlers()
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
