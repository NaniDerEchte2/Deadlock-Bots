from __future__ import annotations

import asyncio
import re
import sqlite3
from typing import Any, Dict, List, Optional

from service import db

RANK_KEYS: List[str] = [
    "initiate",
    "seeker",
    "alchemist",
    "arcanist",
    "ritualist",
    "emissary",
    "archon",
    "oracle",
    "phantom",
    "ascendant",
    "eternus",
]
RANK_VALUES: Dict[str, int] = {rank: idx + 1 for idx, rank in enumerate(RANK_KEYS)}

TEAM_NAME_MIN = 2
TEAM_NAME_MAX = 32

TEAMS_TABLE = "customgames_tournament_teams"
SIGNUPS_TABLE = "customgames_tournament_signups"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sql_identifier(value: str) -> str:
    """Return a validated SQL identifier wrapped in double quotes."""
    if not _IDENTIFIER_RE.fullmatch(value or ""):
        raise ValueError(f"Invalid SQL identifier: {value!r}")
    return f'"{value}"'


TEAMS_TABLE_SQL = _sql_identifier(TEAMS_TABLE)
SIGNUPS_TABLE_SQL = _sql_identifier(SIGNUPS_TABLE)
TEAMS_GUILD_INDEX_SQL = _sql_identifier(f"idx_{TEAMS_TABLE}_guild")
SIGNUPS_GUILD_INDEX_SQL = _sql_identifier(f"idx_{SIGNUPS_TABLE}_guild")


def normalize_rank(raw: str) -> str:
    normalized = (raw or "").strip().lower()
    if normalized in RANK_VALUES:
        return normalized
    return RANK_KEYS[0]


def rank_value(rank_key: str) -> int:
    return RANK_VALUES.get(normalize_rank(rank_key), 1)


def rank_label(rank_key: str) -> str:
    return normalize_rank(rank_key).capitalize()


def rank_choices() -> List[tuple[str, str, int]]:
    return [(rank_label(rank), rank, rank_value(rank)) for rank in RANK_KEYS]


def normalize_mode(raw: str) -> str:
    mode = (raw or "").strip().lower()
    if mode not in {"solo", "team"}:
        raise ValueError("registration_mode must be 'solo' or 'team'")
    return mode


def clean_team_name(raw: str) -> str:
    name = " ".join((raw or "").strip().split())
    if len(name) < TEAM_NAME_MIN:
        raise ValueError(f"Team name must be at least {TEAM_NAME_MIN} chars")
    if len(name) > TEAM_NAME_MAX:
        raise ValueError(f"Team name must be at most {TEAM_NAME_MAX} chars")
    return name


def team_name_key(name: str) -> str:
    return clean_team_name(name).casefold()


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    if isinstance(row, dict):
        return dict(row)
    try:
        return dict(row)
    except Exception:
        return {}


def ensure_schema() -> None:
    with db.get_conn() as conn:
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS {TEAMS_TABLE_SQL}(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              guild_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              name_key TEXT NOT NULL,
              created_by INTEGER,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(guild_id, name_key)
            );

            CREATE TABLE IF NOT EXISTS {SIGNUPS_TABLE_SQL}(
              guild_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL,
              registration_mode TEXT NOT NULL CHECK (registration_mode IN ('solo', 'team')),
              rank TEXT NOT NULL,
              rank_value INTEGER NOT NULL,
              team_id INTEGER,
              assigned_by_admin INTEGER NOT NULL DEFAULT 0,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(guild_id, user_id),
              FOREIGN KEY(team_id) REFERENCES {TEAMS_TABLE_SQL}(id) ON DELETE SET NULL
            );

            CREATE INDEX IF NOT EXISTS {TEAMS_GUILD_INDEX_SQL}
              ON {TEAMS_TABLE_SQL}(guild_id, name_key);

            CREATE INDEX IF NOT EXISTS {SIGNUPS_GUILD_INDEX_SQL}
              ON {SIGNUPS_TABLE_SQL}(guild_id, team_id);
            """
        )


async def ensure_schema_async() -> None:
    await asyncio.to_thread(ensure_schema)


async def team_exists_async(guild_id: int, team_id: int) -> bool:
    row = await db.query_one_async(
        f"SELECT 1 FROM {TEAMS_TABLE} WHERE guild_id = ? AND id = ?",
        (int(guild_id), int(team_id)),
    )
    return bool(row)


async def get_or_create_team_async(
    guild_id: int,
    team_name: str,
    *,
    created_by: Optional[int] = None,
) -> Dict[str, Any]:
    clean_name = clean_team_name(team_name)
    key = team_name_key(clean_name)
    guild = int(guild_id)
    creator = int(created_by) if created_by is not None else None

    existing = await db.query_one_async(
        f"""
        SELECT id, guild_id, name, created_by, created_at
        FROM {TEAMS_TABLE}
        WHERE guild_id = ? AND name_key = ?
        """,
        (guild, key),
    )
    if existing:
        data = _row_to_dict(existing)
        data["created"] = False
        return data

    created = False
    try:
        await db.execute_async(
            f"""
            INSERT INTO {TEAMS_TABLE}(guild_id, name, name_key, created_by)
            VALUES(?, ?, ?, ?)
            """,
            (guild, clean_name, key, creator),
        )
        created = True
    except sqlite3.IntegrityError:
        created = False
    except Exception as exc:
        if "UNIQUE constraint failed" not in str(exc):
            raise

    row = await db.query_one_async(
        f"""
        SELECT id, guild_id, name, created_by, created_at
        FROM {TEAMS_TABLE}
        WHERE guild_id = ? AND name_key = ?
        """,
        (guild, key),
    )
    data = _row_to_dict(row)
    if not data:
        raise RuntimeError("Team row missing after insert")
    data["created"] = created
    return data


async def list_teams_async(guild_id: int) -> List[Dict[str, Any]]:
    rows = await db.query_all_async(
        f"""
        SELECT
            t.id,
            t.guild_id,
            t.name,
            t.created_by,
            t.created_at,
            COALESCE(COUNT(s.user_id), 0) AS member_count
        FROM {TEAMS_TABLE} t
        LEFT JOIN {SIGNUPS_TABLE} s
          ON s.guild_id = t.guild_id
         AND s.team_id = t.id
        WHERE t.guild_id = ?
        GROUP BY t.id, t.guild_id, t.name, t.created_by, t.created_at
        ORDER BY lower(t.name) ASC
        """,
        (int(guild_id),),
    )
    return [_row_to_dict(row) for row in rows or []]


async def list_signups_async(guild_id: int) -> List[Dict[str, Any]]:
    rows = await db.query_all_async(
        f"""
        SELECT
            s.guild_id,
            s.user_id,
            s.registration_mode,
            s.rank,
            s.rank_value,
            s.team_id,
            s.assigned_by_admin,
            s.created_at,
            s.updated_at,
            t.name AS team_name
        FROM {SIGNUPS_TABLE} s
        LEFT JOIN {TEAMS_TABLE} t
          ON t.guild_id = s.guild_id
         AND t.id = s.team_id
        WHERE s.guild_id = ?
        ORDER BY s.rank_value DESC, s.updated_at DESC
        """,
        (int(guild_id),),
    )
    return [_row_to_dict(row) for row in rows or []]


async def get_signup_async(guild_id: int, user_id: int) -> Dict[str, Any]:
    row = await db.query_one_async(
        f"""
        SELECT
            s.guild_id,
            s.user_id,
            s.registration_mode,
            s.rank,
            s.rank_value,
            s.team_id,
            s.assigned_by_admin,
            s.created_at,
            s.updated_at,
            t.name AS team_name
        FROM {SIGNUPS_TABLE} s
        LEFT JOIN {TEAMS_TABLE} t
          ON t.guild_id = s.guild_id
         AND t.id = s.team_id
        WHERE s.guild_id = ? AND s.user_id = ?
        """,
        (int(guild_id), int(user_id)),
    )
    return _row_to_dict(row)


async def upsert_signup_async(
    guild_id: int,
    user_id: int,
    *,
    registration_mode: str,
    rank: str,
    team_id: Optional[int] = None,
    assigned_by_admin: bool = False,
) -> Dict[str, Any]:
    guild = int(guild_id)
    user = int(user_id)
    mode = normalize_mode(registration_mode)
    rank_key = normalize_rank(rank)
    rank_num = rank_value(rank_key)

    if mode == "team" and team_id is None:
        raise ValueError("team_id is required for team registrations")

    team_ref: Optional[int] = int(team_id) if team_id is not None else None
    if team_ref is not None and not await team_exists_async(guild, team_ref):
        raise ValueError("team_id does not exist in this guild")

    assigned_flag = 1 if assigned_by_admin else 0
    existing = await db.query_one_async(
        f"""
        SELECT registration_mode, rank, rank_value, team_id, assigned_by_admin
        FROM {SIGNUPS_TABLE}
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild, user),
    )
    status = "inserted"
    if existing:
        prev_mode = str(existing["registration_mode"])
        prev_rank = str(existing["rank"])
        prev_rank_value = int(existing["rank_value"])
        prev_team_id = int(existing["team_id"]) if existing["team_id"] is not None else None
        prev_assigned = int(existing["assigned_by_admin"] or 0)
        unchanged = (
            prev_mode == mode
            and prev_rank == rank_key
            and prev_rank_value == rank_num
            and prev_team_id == team_ref
            and prev_assigned == assigned_flag
        )
        if unchanged:
            status = "unchanged"
        else:
            await db.execute_async(
                f"""
                UPDATE {SIGNUPS_TABLE}
                SET registration_mode = ?,
                    rank = ?,
                    rank_value = ?,
                    team_id = ?,
                    assigned_by_admin = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND user_id = ?
                """,
                (mode, rank_key, rank_num, team_ref, assigned_flag, guild, user),
            )
            status = "updated"
    else:
        await db.execute_async(
            f"""
            INSERT INTO {SIGNUPS_TABLE}(
                guild_id,
                user_id,
                registration_mode,
                rank,
                rank_value,
                team_id,
                assigned_by_admin
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (guild, user, mode, rank_key, rank_num, team_ref, assigned_flag),
        )

    current = await get_signup_async(guild, user)
    current["status"] = status
    return current


async def assign_signup_team_async(
    guild_id: int,
    user_id: int,
    *,
    team_id: Optional[int],
) -> bool:
    guild = int(guild_id)
    user = int(user_id)
    team_ref: Optional[int] = int(team_id) if team_id is not None else None

    exists = await db.query_one_async(
        f"SELECT 1 FROM {SIGNUPS_TABLE} WHERE guild_id = ? AND user_id = ?",
        (guild, user),
    )
    if not exists:
        return False

    if team_ref is not None and not await team_exists_async(guild, team_ref):
        raise ValueError("team_id does not exist in this guild")

    assigned_by_admin = 1 if team_ref is not None else 0
    await db.execute_async(
        f"""
        UPDATE {SIGNUPS_TABLE}
        SET team_id = ?,
            assigned_by_admin = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE guild_id = ? AND user_id = ?
        """,
        (team_ref, assigned_by_admin, guild, user),
    )
    return True


async def remove_signup_async(guild_id: int, user_id: int) -> bool:
    guild = int(guild_id)
    user = int(user_id)
    exists = await db.query_one_async(
        f"SELECT 1 FROM {SIGNUPS_TABLE} WHERE guild_id = ? AND user_id = ?",
        (guild, user),
    )
    if not exists:
        return False
    await db.execute_async(
        f"DELETE FROM {SIGNUPS_TABLE} WHERE guild_id = ? AND user_id = ?",
        (guild, user),
    )
    return True


async def summary_async(guild_id: int) -> Dict[str, int]:
    row = await db.query_one_async(
        f"""
        SELECT
            COUNT(*) AS signups_total,
            COALESCE(SUM(CASE WHEN registration_mode = 'solo' THEN 1 ELSE 0 END), 0) AS solo_count,
            COALESCE(SUM(CASE WHEN registration_mode = 'team' THEN 1 ELSE 0 END), 0) AS team_count,
            COALESCE(SUM(CASE WHEN registration_mode = 'solo' AND team_id IS NULL THEN 1 ELSE 0 END), 0) AS unassigned_solo
        FROM {SIGNUPS_TABLE}
        WHERE guild_id = ?
        """,
        (int(guild_id),),
    )
    teams_row = await db.query_one_async(
        f"SELECT COUNT(*) AS teams_count FROM {TEAMS_TABLE} WHERE guild_id = ?",
        (int(guild_id),),
    )
    summary = _row_to_dict(row)
    summary["teams_count"] = int(teams_row["teams_count"] if teams_row else 0)
    return {
        "signups_total": int(summary.get("signups_total", 0) or 0),
        "solo_count": int(summary.get("solo_count", 0) or 0),
        "team_count": int(summary.get("team_count", 0) or 0),
        "unassigned_solo": int(summary.get("unassigned_solo", 0) or 0),
        "teams_count": int(summary.get("teams_count", 0) or 0),
    }


async def guild_signup_counts_async() -> Dict[int, int]:
    rows = await db.query_all_async(
        f"""
        SELECT guild_id, COUNT(*) AS signups
        FROM {SIGNUPS_TABLE}
        GROUP BY guild_id
        """
    )
    counts: Dict[int, int] = {}
    for row in rows or []:
        counts[int(row["guild_id"])] = int(row["signups"])
    return counts
