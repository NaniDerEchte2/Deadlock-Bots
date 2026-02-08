"""
Shared Discord/Database utilities - eliminiert Duplicate Code über Cogs hinweg.
"""
import logging
import re
from typing import Optional, List
import discord
import aiosqlite
from pathlib import Path

logger = logging.getLogger(__name__)
_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ========= Discord Member/Role Resolution =========

async def resolve_guild_and_role(
    bot: discord.Client,
    guild_id: int,
    role_id: int
) -> tuple[Optional[discord.Guild], Optional[discord.Role]]:
    """
    Shared utility für Guild + Role Resolution.
    Genutzt von: steam_verified_role.py, feedback_hub.py
    """
    guild = bot.get_guild(guild_id)
    if not guild:
        try:
            guild = await bot.fetch_guild(guild_id)
        except (discord.NotFound, discord.HTTPException):
            return None, None

    if not guild:
        return None, None

    role = guild.get_role(role_id)
    if not role:
        logger.warning(f"Role {role_id} not found in guild {guild_id}")
        return guild, None

    return guild, role


async def resolve_member(
    guild: discord.Guild,
    user_id: int
) -> Optional[discord.Member]:
    """
    Resolve Member mit Fetch-Fallback.
    Genutzt von: tempvoice/core.py, mehrere andere Cogs
    """
    member = guild.get_member(user_id)
    if member:
        return member

    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        logger.debug(f"Member {user_id} not found in guild {guild.id}")
    except discord.HTTPException as exc:
        logger.debug(f"Failed to fetch member {user_id}: {exc}")

    return None


# ========= Database Connection Helpers =========

async def connect_db(db_path: Path | str) -> aiosqlite.Connection:
    """
    Standard DB-Connection mit WAL mode & NORMAL sync.
    Genutzt von: deadlock_voice_status.py, rank_voice_manager.py, tempvoice/core.py
    """
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    return db


async def ensure_table_exists(
    db: aiosqlite.Connection,
    create_table_sql_or_name: str,
    schema: Optional[str] = None,
) -> None:
    """
    Ensures a table exists.
    Supports:
    - full SQL: ensure_table_exists(db, "CREATE TABLE IF NOT EXISTS ...")
    - legacy form: ensure_table_exists(db, "table_name", "(...)")
    """
    if schema is not None:
        if not _SQL_IDENTIFIER_RE.fullmatch(create_table_sql_or_name):
            raise ValueError(f"Unsafe SQL table name: {create_table_sql_or_name!r}")
        create_table_sql = "CREATE TABLE IF NOT EXISTS " + create_table_sql_or_name + " " + schema
    else:
        create_table_sql = create_table_sql_or_name

    normalized = " ".join(create_table_sql.strip().split())
    if not normalized.upper().startswith("CREATE TABLE IF NOT EXISTS "):
        raise ValueError("ensure_table_exists expects a CREATE TABLE IF NOT EXISTS statement")

    await db.execute(create_table_sql)
    await db.commit()


# ========= Voice Channel Helpers =========

def is_tempvoice_lane(channel: discord.VoiceChannel, category_ids: set[int]) -> bool:
    """
    Check ob Channel eine TempVoice Lane ist.
    Genutzt von: tempvoice/core.py, rank_voice_manager.py, deadlock_voice_status.py
    """
    try:
        name = channel.name.lower()
    except Exception:
        return False

    return (
        channel.category_id in category_ids
        and name.startswith("lane ")
    )


async def get_voice_channel_members(
    channel: discord.VoiceChannel,
    *,
    exclude_bots: bool = True
) -> List[discord.Member]:
    """
    Iteriere über Channel-Members mit optionalem Bot-Filter.
    Genutzt von: voice_activity_tracker.py, tempvoice/core.py
    """
    members = []
    for member in channel.members:
        if exclude_bots and member.bot:
            continue
        members.append(member)
    return members


# ========= Environment/Config Helpers =========

def get_env_int(key: str, default: int) -> int:
    """Safe int parsing from environment variable."""
    import os
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(f"Invalid int for {key}={raw}, using default {default}")
        return default


def get_env_bool(key: str, default: bool) -> bool:
    """Safe bool parsing from environment variable."""
    import os
    raw = os.getenv(key, "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")
