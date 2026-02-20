"""
Shared Discord/Database utilities - eliminiert Duplicate Code über Cogs hinweg.
"""

import asyncio
import logging
import re
from pathlib import Path
from typing import Iterable, List, Optional

import discord

from service import db as central_db

logger = logging.getLogger(__name__)
_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ========= Discord Member/Role Resolution =========


async def resolve_guild_and_role(
    bot: discord.Client, guild_id: int, role_id: int
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
    guild: discord.Guild, user_id: int
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


class _BufferedAsyncCursor:
    """Buffered async cursor compatibility layer for legacy utilities."""

    def __init__(self, rows, rowcount: int, lastrowid: int) -> None:
        self._rows = list(rows or [])
        self._idx = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    async def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return row

    async def fetchall(self):
        if self._idx >= len(self._rows):
            return []
        rows = self._rows[self._idx :]
        self._idx = len(self._rows)
        return rows

    async def fetchmany(self, size: int):
        if size <= 0 or self._idx >= len(self._rows):
            return []
        end = min(self._idx + size, len(self._rows))
        rows = self._rows[self._idx : end]
        self._idx = end
        return rows

    async def close(self) -> None:
        return None


class _CentralAsyncDBAdapter:
    """
    Async compatibility adapter over service.db.
    Avoids direct sqlite/aiosqlite connections outside service/db.py.
    """

    async def execute(self, sql: str, params: Iterable = ()) -> _BufferedAsyncCursor:
        def _run():
            with central_db.get_conn() as conn:
                cur = conn.execute(sql, tuple(params))
                rows = cur.fetchall() if cur.description else []
                return rows, int(cur.rowcount or 0), int(cur.lastrowid or 0)

        rows, rowcount, lastrowid = await asyncio.to_thread(_run)
        return _BufferedAsyncCursor(rows, rowcount, lastrowid)

    async def executemany(
        self, sql: str, seq_of_params: Iterable[Iterable]
    ) -> _BufferedAsyncCursor:
        def _run():
            with central_db.get_conn() as conn:
                cur = conn.executemany(sql, seq_of_params)
                return [], int(cur.rowcount or 0), int(cur.lastrowid or 0)

        rows, rowcount, lastrowid = await asyncio.to_thread(_run)
        return _BufferedAsyncCursor(rows, rowcount, lastrowid)

    async def executescript(self, sql_script: str) -> _BufferedAsyncCursor:
        def _run():
            with central_db.get_conn() as conn:
                cur = conn.executescript(sql_script)
                return [], int(cur.rowcount or 0), int(cur.lastrowid or 0)

        rows, rowcount, lastrowid = await asyncio.to_thread(_run)
        return _BufferedAsyncCursor(rows, rowcount, lastrowid)

    async def commit(self) -> None:
        # service.db runs in autocommit mode; kept for API compatibility.
        return None

    async def close(self) -> None:
        # shared connection lifecycle is handled by service.db
        return None


async def connect_db(db_path: Path | str) -> _CentralAsyncDBAdapter:
    """
    Legacy async DB connector.
    Returns an async adapter backed by the shared central service.db connection.
    Genutzt von: deadlock_voice_status.py, rank_voice_manager.py, tempvoice/core.py
    """
    requested = str(Path(db_path))
    active = str(Path(central_db.db_path()))
    if requested != active:
        logger.warning(
            "connect_db ignored requested path %s (active central DB is %s)",
            requested,
            active,
        )
    await asyncio.to_thread(central_db.connect)
    return _CentralAsyncDBAdapter()


async def ensure_table_exists(
    db: _CentralAsyncDBAdapter,
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
        create_table_sql = (
            "CREATE TABLE IF NOT EXISTS " + create_table_sql_or_name + " " + schema
        )
    else:
        create_table_sql = create_table_sql_or_name

    stripped = create_table_sql.strip()
    # Reject multi-statement payloads; only one CREATE TABLE statement is allowed.
    if ";" in stripped.rstrip(";"):
        raise ValueError("ensure_table_exists expects a single SQL statement")

    normalized = " ".join(stripped.rstrip(";").split())
    if not normalized.upper().startswith("CREATE TABLE IF NOT EXISTS "):
        raise ValueError(
            "ensure_table_exists expects a CREATE TABLE IF NOT EXISTS statement"
        )

    remainder = normalized[len("CREATE TABLE IF NOT EXISTS ") :]
    name_match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\s|\()", remainder)
    if not name_match:
        raise ValueError("ensure_table_exists could not parse a safe table name")

    await db.executescript(stripped if stripped.endswith(";") else stripped + ";")
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

    return channel.category_id in category_ids and name.startswith("lane ")


async def get_voice_channel_members(
    channel: discord.VoiceChannel, *, exclude_bots: bool = True
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
