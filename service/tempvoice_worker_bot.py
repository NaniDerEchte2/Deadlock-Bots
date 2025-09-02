import asyncio
import logging
import os
from typing import Any, Dict

import discord
from discord.ext import commands

from shared.socket_bus import JSONLineServer
from shared.worker_client import WorkerProxy  # nur für Typen/Protokoll-Referenz, nicht genutzt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tempvoice_worker")

TOKEN = os.getenv("DISCORD_TOKEN")
HOST = os.getenv("SOCKET_HOST", "127.0.0.1")
PORT = int(os.getenv("SOCKET_PORT", "45679"))

# Minimaler Intents-Satz, da der Worker keine Messages o.ä. senden muss.
intents = discord.Intents.none()
# Er braucht Guilds + Channels, um Channels zu finden/bearbeiten
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def handle_op(payload: Dict[str, Any]) -> Dict[str, Any]:
    op = payload.get("op")
    if op == "ping":
        return {"ok": True, "pong": True}

    # Wir benötigen eine Guild-agnostische Suche nach Channel-ID
    channel_id = payload.get("channel_id")
    if not channel_id:
        return {"ok": False, "error": "channel_id fehlt"}

    channel = bot.get_channel(int(channel_id))
    if channel is None:
        # Versuch: über alle Guilds fetchen
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except Exception as e:
            return {"ok": False, "error": f"Channel nicht gefunden: {e}"}

    if op == "edit_channel":
        # Nur VoiceChannel zulassen (TempVoice)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return {"ok": False, "error": f"Channel {channel.id} ist kein Voice/StageChannel"}
        kw = {}
        if "name" in payload:
            kw["name"] = payload["name"]
        if "user_limit" in payload:
            kw["user_limit"] = payload["user_limit"]
        if "bitrate" in payload:
            kw["bitrate"] = payload["bitrate"]
        try:
            await channel.edit(**kw, reason="TempVoice Worker")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"edit_channel fehlgeschlagen: {type(e).__name__}: {e}"}

    if op == "set_permissions":
        target_id = payload.get("target_id")
        overwrite = payload.get("overwrite")
        if target_id is None or not isinstance(overwrite, dict):
            return {"ok": False, "error": "target_id oder overwrite fehlt/ungültig"}
        # target kann Member- oder Role-ID sein:
        guild = channel.guild
        target = guild.get_member(target_id) or guild.get_role(target_id)
        if target is None:
            return {"ok": False, "error": "target nicht gefunden"}
        try:
            # overwrite-Keys: view_channel, connect, speak, etc. (True/False/None)
            perms = discord.PermissionOverwrite(**overwrite)
            await channel.set_permissions(target, overwrite=perms, reason="TempVoice Worker")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"set_permissions fehlgeschlagen: {type(e).__name__}: {e}"}

    return {"ok": False, "error": f"unbekannte op: {op}"}


def start_socket_server(loop: asyncio.AbstractEventLoop) -> None:
    # Handler im Server-Thread -> delegiert in den Bot-Loop
    def handler(req: Dict[str, Any]) -> Dict[str, Any]:
        fut = asyncio.run_coroutine_threadsafe(handle_op(req), loop)
        return fut.result(timeout=10.0)

    server = JSONLineServer(HOST, PORT, handler)
    server.start()
    logger.info(f"Worker Socket-Server läuft auf {HOST}:{PORT}")


@bot.event
async def on_ready():
    logger.info(f"Worker Bot eingeloggt als {bot.user} (ID: {bot.user.id})")
    # Socket-Server nach Bot-Loop-Start hochfahren
    loop = asyncio.get_running_loop()
    start_socket_server(loop)


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN für Worker fehlt (z.B. in .env.worker)")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
