# service/tempvoice_worker_bot.py
import os, asyncio, logging
import discord
from discord.ext import commands
from shared.socket_bus import SocketServer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")

INTENTS = discord.Intents.none()
INTENTS.guilds = True
INTENTS.members = True
INTENTS.voice_states = True  # für move

TOKEN = os.getenv("DISCORD_TOKEN_WORKER", "")
WORKER_HOST = os.getenv("TV_WORKER_HOST", "127.0.0.1")
WORKER_PORT = int(os.getenv("TV_WORKER_PORT", "45678"))
WORKER_SECRET = os.getenv("TV_WORKER_SECRET", "")

bot = commands.Bot(command_prefix="!", intents=INTENTS)

async def _get_channel(channel_id: int):
    ch = bot.get_channel(int(channel_id))
    if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
        raise RuntimeError("channel_not_found_or_not_voice")
    return ch

async def _get_guild(guild_id: int):
    g = bot.get_guild(int(guild_id))
    if not isinstance(g, discord.Guild):
        raise RuntimeError("guild_not_found")
    return g

# Handlers
async def h_channel_edit(data):
    ch = await _get_channel(data["channel_id"])
    kw = {}
    if data.get("name") is not None:
        kw["name"] = data["name"]
    if data.get("user_limit") is not None:
        kw["user_limit"] = int(data["user_limit"])
    if data.get("bitrate") is not None:
        kw["bitrate"] = int(data["bitrate"])
    if not kw:
        return {"changed": False}
    await ch.edit(**kw, reason=data.get("reason") or "Worker: channel_edit")
    return {"changed": True}

async def h_set_permissions_connect(data):
    ch = await _get_channel(data["channel_id"])
    tgt_id = int(data["target_id"])
    tgt = ch.guild.get_member(tgt_id) or ch.guild.get_role(tgt_id) or discord.Object(id=tgt_id)
    ow = ch.overwrites_for(tgt)
    state = data.get("connect")  # True/False/None
    ow.connect = (None if state is None else bool(state))
    await ch.set_permissions(tgt, overwrite=ow, reason="Worker: set_permissions_connect")
    return {"ok": True}

async def h_clear_overwrite(data):
    ch = await _get_channel(data["channel_id"])
    tgt_id = int(data["target_id"])
    tgt = ch.guild.get_member(tgt_id) or ch.guild.get_role(tgt_id) or discord.Object(id=tgt_id)
    await ch.set_permissions(tgt, overwrite=None, reason="Worker: clear_overwrite")
    return {"ok": True}

async def h_create_voice(data):
    g = await _get_guild(data["guild_id"])
    cat = g.get_channel(int(data["category_id"])) if data.get("category_id") else None
    overwrites = getattr(cat, "overwrites", None)
    ch = await g.create_voice_channel(
        name=data["name"],
        category=cat if isinstance(cat, discord.CategoryChannel) else None,
        user_limit=int(data.get("user_limit") or 0),
        bitrate=int(data.get("bitrate") or 64000),
        overwrites=overwrites,
        reason=data.get("reason") or "Worker: create_voice"
    )
    return {"channel_id": ch.id}

async def h_delete_channel(data):
    ch = await _get_channel(data["channel_id"])
    await ch.delete(reason=data.get("reason") or "Worker: delete_channel")
    return {"deleted": True}

async def h_move_member(data):
    g = await _get_guild(data["guild_id"])
    member = g.get_member(int(data["user_id"]))
    if not isinstance(member, discord.Member):
        raise RuntimeError("member_not_found")
    dest = g.get_channel(int(data["dest_channel_id"]))
    if not isinstance(dest, (discord.VoiceChannel, discord.StageChannel)):
        raise RuntimeError("dest_not_voice")
    await member.move_to(dest, reason=data.get("reason") or "Worker: move_member")
    return {"moved": True}

@bot.event
async def on_ready():
    log.info(f"Worker logged in as {bot.user} | guilds={len(bot.guilds)}")
    # Socket Server starten
    server = SocketServer(WORKER_HOST, WORKER_PORT, WORKER_SECRET)
    server.add_handler("channel_edit", h_channel_edit)
    server.add_handler("set_permissions_connect", h_set_permissions_connect)
    server.add_handler("clear_overwrite", h_clear_overwrite)
    server.add_handler("create_voice", h_create_voice)
    server.add_handler("delete_channel", h_delete_channel)
    server.add_handler("move_member", h_move_member)
    await server.start()

def main():
    if not TOKEN:
        raise RuntimeError("Set DISCORD_TOKEN_WORKER env")
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
