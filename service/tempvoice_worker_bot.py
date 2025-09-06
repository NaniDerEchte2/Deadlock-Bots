# service/tempvoice_worker_bot.py
# TempVoice Worker – zentralisierte .env, robustes Env-Loading, erweiterte Ops
#START IMMER ALS: python -m service.tempvoice_worker_bot

import asyncio
import logging
import os
import signal
from typing import Any, Dict, Optional, Union

import discord
from discord.ext import commands

# --- zentrale .env laden (nur .env im Projekt-Root) ---
try:
    from dotenv import load_dotenv, find_dotenv  # type: ignore
    load_dotenv(find_dotenv(".env") or ".env")
except Exception:
    # dotenv optional – wenn nicht installiert, werden nur echte Env-Vars genutzt
    pass

# interne Imports (müssen als Modul gestartet werden: python -m service.tempvoice_worker_bot)
from shared.socket_bus import JSONLineServer  # type: ignore

LOG_LEVEL = os.getenv("WORKER_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("tempvoice_worker")

# Token: erst DISCORD_TOKEN_WORKER, dann DISCORD_TOKEN
TOKEN = os.getenv("DISCORD_TOKEN_WORKER") or os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit(
        "❌ Kein Bot-Token gefunden. Bitte in der zentralen .env setzen:\n"
        "   DISCORD_TOKEN_WORKER=<dein_bot_token>\n"
        "   (alternativ akzeptiert: DISCORD_TOKEN)"
    )

# Socket-Host/Port: eigene Prefixe erlaubt, sonst Fallbacks
HOST = (
    os.getenv("WORKER_SOCKET_HOST")
    or os.getenv("SOCKET_HOST")
    or "127.0.0.1"
)
PORT = int(
    os.getenv("WORKER_SOCKET_PORT")
    or os.getenv("SOCKET_PORT")
    or "45679"
)

# Minimale Intents (der Worker bearbeitet nur Channel/Permissions/Move)
intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

_socket_server: Optional[JSONLineServer] = None


def _as_int(x: Union[str, int, None]) -> Optional[int]:
    try:
        return int(x) if x is not None else None
    except Exception:
        return None


async def _get_channel_anywhere(channel_id: int) -> Optional[discord.abc.GuildChannel]:
    """Hole Channel zuerst aus Cache, ansonsten via REST."""
    ch = bot.get_channel(channel_id)
    if ch:
        return ch
    try:
        return await bot.fetch_channel(channel_id)
    except Exception as e:
        logger.debug("fetch_channel(%s) failed: %s", channel_id, e)
        return None


def _overwrite_from_delta(
    channel: Union[discord.VoiceChannel, discord.StageChannel],
    target: Union[discord.Member, discord.Role],
    delta: Dict[str, Optional[bool]],
) -> discord.PermissionOverwrite:
    """
    Wendet ein diff-artiges Mapping auf bestehende Overwrite an:
      True/False setzen, None entfernt die einzelne Permission.
    """
    current = channel.overwrites_for(target)
    for key, value in delta.items():
        setattr(current, key, value)
    return current


async def handle_op(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Unterstützte Ops:
      - ping
      - edit_channel {channel_id, name?, user_limit?, bitrate?}
      - set_permissions {channel_id, target_id, overwrite: {perm: True|False|None} | {} -> clear}
      - move_member {guild_id|channel_id, user_id, dest_channel_id}
      - create_voice {guild_id|channel_id, name, category_id?, user_limit?, bitrate?, reason?}
      - delete_channel {channel_id, reason?}
    """
    try:
        op = (payload.get("op") or "").lower()

        if op == "ping":
            return {"ok": True, "pong": True}

        # --- channel-basierte Ops brauchen channel_id ---
        if op in {"edit_channel", "set_permissions", "delete_channel"}:
            channel_id = _as_int(payload.get("channel_id"))
            if not channel_id:
                return {"ok": False, "error": "channel_id fehlt/ungültig"}

            channel = await _get_channel_anywhere(channel_id)
            if channel is None:
                return {"ok": False, "error": f"Channel {channel_id} nicht gefunden"}

            # --- edit_channel ---
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

            # --- set_permissions ---
            if op == "set_permissions":
                target_id = _as_int(payload.get("target_id"))
                overwrite = payload.get("overwrite")
                if target_id is None:
                    return {"ok": False, "error": "target_id fehlt/ungültig"}
                if not isinstance(overwrite, dict):
                    return {"ok": False, "error": "overwrite fehlt/ungültig (dict erwartet)"}

                guild = channel.guild
                target = guild.get_member(target_id) or guild.get_role(target_id)
                if target is None:
                    return {"ok": False, "error": f"target {target_id} nicht gefunden"}

                try:
                    # Empty dict => Overwrite entfernen (clear)
                    if overwrite == {}:
                        await channel.set_permissions(target, overwrite=None, reason="TempVoice Worker: clear overwrite")
                    else:
                        perms = _overwrite_from_delta(channel, target, overwrite)  # True/False/None unterstützen
                        await channel.set_permissions(target, overwrite=perms, reason="TempVoice Worker: set_permissions")
                    return {"ok": True}
                except Exception as e:
                    return {"ok": False, "error": f"set_permissions fehlgeschlagen: {type(e).__name__}: {e}"}

            # --- delete_channel ---
            if op == "delete_channel":
                reason = payload.get("reason") or "TempVoice Worker: delete_channel"
                try:
                    await channel.delete(reason=reason)
                    return {"ok": True}
                except Exception as e:
                    return {"ok": False, "error": f"delete_channel fehlgeschlagen: {type(e).__name__}: {e}"}

        # --- move_member ---
        if op == "move_member":
            user_id = _as_int(payload.get("user_id"))
            dest_channel_id = _as_int(payload.get("dest_channel_id"))
            if not user_id or not dest_channel_id:
                return {"ok": False, "error": "user_id oder dest_channel_id fehlt/ungültig"}

            # Quelle für die Guild bestimmen: bevorzugt guild_id, dann channel_id
            guild: Optional[discord.Guild] = None
            guild_id = _as_int(payload.get("guild_id"))
            if guild_id:
                guild = bot.get_guild(guild_id)
            if guild is None:
                # Fallback: dest channel -> guild
                dest_ch = await _get_channel_anywhere(dest_channel_id)
                if isinstance(dest_ch, discord.abc.GuildChannel):
                    guild = dest_ch.guild
            if guild is None:
                return {"ok": False, "error": "Guild konnte nicht bestimmt werden"}

            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)  # type: ignore
                except Exception as e:
                    return {"ok": False, "error": f"Member {user_id} nicht gefunden: {e}"}

            dest_ch = await _get_channel_anywhere(dest_channel_id)
            if not isinstance(dest_ch, (discord.VoiceChannel, discord.StageChannel)):
                return {"ok": False, "error": f"Zielchannel {dest_channel_id} ist kein Voice/StageChannel"}

            try:
                await member.move_to(dest_ch, reason="TempVoice Worker: move_member")
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": f"move_member fehlgeschlagen: {type(e).__name__}: {e}"}

        # --- create_voice ---
        if op == "create_voice":
            name = payload.get("name")
            if not name:
                return {"ok": False, "error": "name fehlt"}
            reason = payload.get("reason") or "TempVoice Worker: create_voice"

            # Ziel-Guild ermitteln: bevorzugt guild_id, alternativ via channel_id (dessen Guild)
            guild: Optional[discord.Guild] = None
            guild_id = _as_int(payload.get("guild_id"))
            if guild_id:
                guild = bot.get_guild(guild_id)

            if guild is None:
                channel_id = _as_int(payload.get("channel_id"))
                if channel_id:
                    ch = await _get_channel_anywhere(channel_id)
                    if isinstance(ch, discord.abc.GuildChannel):
                        guild = ch.guild

            if guild is None:
                return {"ok": False, "error": "Guild konnte nicht bestimmt werden (guild_id oder channel_id übergeben?)"}

            category_id = _as_int(payload.get("category_id"))
            category = guild.get_channel(category_id) if category_id else None

            user_limit = _as_int(payload.get("user_limit")) or 0
            bitrate = _as_int(payload.get("bitrate")) or getattr(guild, "bitrate_limit", 64000)

            try:
                vc = await guild.create_voice_channel(
                    name=str(name),
                    category=category if isinstance(category, discord.CategoryChannel) else None,
                    user_limit=user_limit,
                    bitrate=bitrate,
                    reason=reason,
                )
                return {"ok": True, "channel_id": vc.id}
            except Exception as e:
                return {"ok": False, "error": f"create_voice fehlgeschlagen: {type(e).__name__}: {e}"}

        return {"ok": False, "error": f"unbekannte op: {payload.get('op')!r}"}

    except Exception as e:
        logger.exception("handle_op – unerwarteter Fehler")
        return {"ok": False, "error": f"unexpected: {type(e).__name__}: {e}"}


def start_socket_server(loop: asyncio.AbstractEventLoop) -> None:
    global _socket_server

    def handler(req: Dict[str, Any]) -> Dict[str, Any]:
        # delegiere in den Bot-Loop und warte synchron (Server-Thread)
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


@bot.event
async def on_ready():
    logger.info("✅ Worker Bot eingeloggt als %s (ID: %s)", bot.user, bot.user.id)  # type: ignore
    # kleine Übersicht
    g = ", ".join(f"{guild.name}({guild.id})" for guild in bot.guilds)
    logger.info("   Guilds: %s", g or "–")
    loop = asyncio.get_running_loop()
    start_socket_server(loop)


def _install_signal_handlers():
    # sauberes Herunterfahren (Unix); unter Windows greift KeyboardInterrupt
    def _graceful_shutdown(signum, frame):
        logger.info("Beende Worker (%s)...", signal.Signals(signum).name)
        stop_socket_server()
        try:
            # Bot-Loop schließen
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
