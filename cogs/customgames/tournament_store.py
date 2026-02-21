from __future__ import annotations

import asyncio
import sqlite3
import time
import uuid
from typing import Any

from service import db

RANK_KEYS: list[str] = [
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
RANK_VALUES: dict[str, int] = {rank: idx + 1 for idx, rank in enumerate(RANK_KEYS)}

TEAM_NAME_MIN = 2
TEAM_NAME_MAX = 32
TEAM_MAX_SIZE = 6

TEAMS_TABLE = "customgames_tournament_teams"
SIGNUPS_TABLE = "customgames_tournament_signups"
PERIODS_TABLE = "tournament_periods"


def normalize_rank(raw: str) -> str:
    normalized = (raw or "").strip().lower()
    if normalized in RANK_VALUES:
        return normalized
    return RANK_KEYS[0]


def rank_value(rank_key: str) -> int:
    return RANK_VALUES.get(normalize_rank(rank_key), 1)


def rank_label(rank_key: str) -> str:
    return normalize_rank(rank_key).capitalize()


def rank_choices() -> list[tuple[str, str, int]]:
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


def _row_to_dict(row: Any) -> dict[str, Any]:
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
            """
            CREATE TABLE IF NOT EXISTS customgames_tournament_teams(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              guild_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              name_key TEXT NOT NULL,
              created_by INTEGER,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(guild_id, name_key)
            );

            CREATE TABLE IF NOT EXISTS customgames_tournament_signups(
              guild_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL,
              registration_mode TEXT NOT NULL CHECK (registration_mode IN ('solo', 'team')),
              rank TEXT NOT NULL,
              rank_value INTEGER NOT NULL,
              rank_subvalue INTEGER NOT NULL DEFAULT 0,
              team_id INTEGER,
              assigned_by_admin INTEGER NOT NULL DEFAULT 0,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(guild_id, user_id),
              FOREIGN KEY(team_id) REFERENCES customgames_tournament_teams(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS tournament_periods(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              guild_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              registration_start DATETIME NOT NULL,
              registration_end DATETIME NOT NULL,
              is_active INTEGER NOT NULL DEFAULT 1,
              team_size INTEGER NOT NULL DEFAULT 6,
              created_by INTEGER,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS turnier_auth_tokens(
              token TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              display_name TEXT NOT NULL,
              expires_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_customgames_tournament_teams_guild
              ON customgames_tournament_teams(guild_id, name_key);

            CREATE INDEX IF NOT EXISTS idx_customgames_tournament_signups_guild
              ON customgames_tournament_signups(guild_id, team_id);

            CREATE INDEX IF NOT EXISTS idx_tournament_periods_guild
              ON tournament_periods(guild_id, is_active);
            """
        )
        # Migrations for existing deployments
        for alter_sql in (
            "ALTER TABLE customgames_tournament_signups ADD COLUMN rank_subvalue INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE tournament_periods ADD COLUMN team_size INTEGER NOT NULL DEFAULT 6",
            "ALTER TABLE customgames_tournament_signups ADD COLUMN display_name TEXT",
        ):
            try:
                conn.execute(alter_sql)
            except Exception:
                pass  # Column already exists


async def ensure_schema_async() -> None:
    await asyncio.to_thread(ensure_schema)


async def team_exists_async(guild_id: int, team_id: int) -> bool:
    row = await db.query_one_async(
        "SELECT 1 FROM customgames_tournament_teams WHERE guild_id = ? AND id = ?",
        (int(guild_id), int(team_id)),
    )
    return bool(row)


async def get_or_create_team_async(
    guild_id: int,
    team_name: str,
    *,
    created_by: int | None = None,
) -> dict[str, Any]:
    clean_name = clean_team_name(team_name)
    key = team_name_key(clean_name)
    guild = int(guild_id)
    creator = int(created_by) if created_by is not None else None

    existing = await db.query_one_async(
        """
        SELECT id, guild_id, name, created_by, created_at
        FROM customgames_tournament_teams
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
            """
            INSERT INTO customgames_tournament_teams(guild_id, name, name_key, created_by)
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
        """
        SELECT id, guild_id, name, created_by, created_at
        FROM customgames_tournament_teams
        WHERE guild_id = ? AND name_key = ?
        """,
        (guild, key),
    )
    data = _row_to_dict(row)
    if not data:
        raise RuntimeError("Team row missing after insert")
    data["created"] = created
    return data


async def list_teams_async(guild_id: int) -> list[dict[str, Any]]:
    rows = await db.query_all_async(
        """
        SELECT
            t.id,
            t.guild_id,
            t.name,
            t.created_by,
            t.created_at,
            COALESCE(COUNT(s.user_id), 0) AS member_count
        FROM customgames_tournament_teams t
        LEFT JOIN customgames_tournament_signups s
          ON s.guild_id = t.guild_id
         AND s.team_id = t.id
        WHERE t.guild_id = ?
        GROUP BY t.id, t.guild_id, t.name, t.created_by, t.created_at
        ORDER BY lower(t.name) ASC
        """,
        (int(guild_id),),
    )
    return [_row_to_dict(row) for row in rows or []]


async def list_signups_async(guild_id: int) -> list[dict[str, Any]]:
    rows = await db.query_all_async(
        """
        SELECT
            s.guild_id,
            s.user_id,
            s.registration_mode,
            s.rank,
            s.rank_value,
            s.rank_subvalue,
            s.display_name,
            s.team_id,
            s.assigned_by_admin,
            s.created_at,
            s.updated_at,
            t.name AS team_name
        FROM customgames_tournament_signups s
        LEFT JOIN customgames_tournament_teams t
          ON t.guild_id = s.guild_id
         AND t.id = s.team_id
        WHERE s.guild_id = ?
        ORDER BY s.rank_value DESC, s.updated_at DESC
        """,
        (int(guild_id),),
    )
    return [_row_to_dict(row) for row in rows or []]


async def get_signup_async(guild_id: int, user_id: int) -> dict[str, Any]:
    row = await db.query_one_async(
        """
        SELECT
            s.guild_id,
            s.user_id,
            s.registration_mode,
            s.rank,
            s.rank_value,
            s.rank_subvalue,
            s.display_name,
            s.team_id,
            s.assigned_by_admin,
            s.created_at,
            s.updated_at,
            t.name AS team_name
        FROM customgames_tournament_signups s
        LEFT JOIN customgames_tournament_teams t
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
    rank_subvalue: int = 0,
    team_id: int | None = None,
    assigned_by_admin: bool = False,
    display_name: str | None = None,
) -> dict[str, Any]:
    guild = int(guild_id)
    user = int(user_id)
    mode = normalize_mode(registration_mode)
    rank_key = normalize_rank(rank)
    rank_num = rank_value(rank_key)
    rank_sub = max(0, min(6, int(rank_subvalue)))
    dname: str | None = str(display_name).strip() if display_name else None

    if mode == "team" and team_id is None:
        raise ValueError("team_id is required for team registrations")

    team_ref: int | None = int(team_id) if team_id is not None else None
    if team_ref is not None and not await team_exists_async(guild, team_ref):
        raise ValueError("team_id does not exist in this guild")

    assigned_flag = 1 if assigned_by_admin else 0
    existing = await db.query_one_async(
        """
        SELECT registration_mode, rank, rank_value, rank_subvalue, team_id, assigned_by_admin, display_name
        FROM customgames_tournament_signups
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild, user),
    )
    status = "inserted"
    if existing:
        prev_mode = str(existing["registration_mode"])
        prev_rank = str(existing["rank"])
        prev_rank_value = int(existing["rank_value"])
        prev_rank_sub = int(existing["rank_subvalue"] or 0)
        prev_team_id = int(existing["team_id"]) if existing["team_id"] is not None else None
        prev_assigned = int(existing["assigned_by_admin"] or 0)
        prev_dname = existing["display_name"]
        unchanged = (
            prev_mode == mode
            and prev_rank == rank_key
            and prev_rank_value == rank_num
            and prev_rank_sub == rank_sub
            and prev_team_id == team_ref
            and prev_assigned == assigned_flag
            and (dname is None or prev_dname == dname)
        )
        if unchanged:
            status = "unchanged"
        else:
            # Preserve existing display_name if not provided
            effective_dname = dname if dname is not None else prev_dname
            await db.execute_async(
                """
                UPDATE customgames_tournament_signups
                SET registration_mode = ?,
                    rank = ?,
                    rank_value = ?,
                    rank_subvalue = ?,
                    team_id = ?,
                    assigned_by_admin = ?,
                    display_name = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND user_id = ?
                """,
                (
                    mode,
                    rank_key,
                    rank_num,
                    rank_sub,
                    team_ref,
                    assigned_flag,
                    effective_dname,
                    guild,
                    user,
                ),
            )
            status = "updated"
    else:
        await db.execute_async(
            """
            INSERT INTO customgames_tournament_signups(
                guild_id,
                user_id,
                registration_mode,
                rank,
                rank_value,
                rank_subvalue,
                team_id,
                assigned_by_admin,
                display_name
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (guild, user, mode, rank_key, rank_num, rank_sub, team_ref, assigned_flag, dname),
        )

    current = await get_signup_async(guild, user)
    current["status"] = status
    return current


async def assign_signup_team_async(
    guild_id: int,
    user_id: int,
    *,
    team_id: int | None,
) -> bool:
    guild = int(guild_id)
    user = int(user_id)
    team_ref: int | None = int(team_id) if team_id is not None else None

    exists = await db.query_one_async(
        "SELECT 1 FROM customgames_tournament_signups WHERE guild_id = ? AND user_id = ?",
        (guild, user),
    )
    if not exists:
        return False

    if team_ref is not None and not await team_exists_async(guild, team_ref):
        raise ValueError("team_id does not exist in this guild")

    assigned_by_admin = 1 if team_ref is not None else 0
    await db.execute_async(
        """
        UPDATE customgames_tournament_signups
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
        "SELECT 1 FROM customgames_tournament_signups WHERE guild_id = ? AND user_id = ?",
        (guild, user),
    )
    if not exists:
        return False
    await db.execute_async(
        "DELETE FROM customgames_tournament_signups WHERE guild_id = ? AND user_id = ?",
        (guild, user),
    )
    return True


async def summary_async(guild_id: int) -> dict[str, int]:
    row = await db.query_one_async(
        """
        SELECT
            COUNT(*) AS signups_total,
            COALESCE(SUM(CASE WHEN registration_mode = 'solo' THEN 1 ELSE 0 END), 0) AS solo_count,
            COALESCE(SUM(CASE WHEN registration_mode = 'team' THEN 1 ELSE 0 END), 0) AS team_count,
            COALESCE(SUM(CASE WHEN registration_mode = 'solo' AND team_id IS NULL THEN 1 ELSE 0 END), 0) AS unassigned_solo
        FROM customgames_tournament_signups
        WHERE guild_id = ?
        """,
        (int(guild_id),),
    )
    teams_row = await db.query_one_async(
        "SELECT COUNT(*) AS teams_count FROM customgames_tournament_teams WHERE guild_id = ?",
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


async def guild_signup_counts_async() -> dict[int, int]:
    rows = await db.query_all_async(
        """
        SELECT guild_id, COUNT(*) AS signups
        FROM customgames_tournament_signups
        GROUP BY guild_id
        """
    )
    counts: dict[int, int] = {}
    for row in rows or []:
        counts[int(row["guild_id"])] = int(row["signups"])
    return counts


async def clear_all_signups_async(guild_id: int) -> int:
    """Delete all signups for a guild. Returns number of deleted rows."""
    row = await db.query_one_async(
        "SELECT COUNT(*) AS cnt FROM customgames_tournament_signups WHERE guild_id = ?",
        (int(guild_id),),
    )
    count = int(row["cnt"] if row else 0)
    await db.execute_async(
        "DELETE FROM customgames_tournament_signups WHERE guild_id = ?",
        (int(guild_id),),
    )
    return count


async def delete_team_async(guild_id: int, team_id: int) -> bool:
    """Delete a team and unassign any signups that belonged to it."""
    guild = int(guild_id)
    tid = int(team_id)
    exists = await db.query_one_async(
        "SELECT 1 FROM customgames_tournament_teams WHERE guild_id = ? AND id = ?",
        (guild, tid),
    )
    if not exists:
        return False
    # Unassign signups first (SQLite FK enforcement not guaranteed)
    await db.execute_async(
        "UPDATE customgames_tournament_signups SET team_id = NULL WHERE guild_id = ? AND team_id = ?",
        (guild, tid),
    )
    await db.execute_async(
        "DELETE FROM customgames_tournament_teams WHERE guild_id = ? AND id = ?",
        (guild, tid),
    )
    return True


async def create_period_async(
    guild_id: int,
    name: str,
    registration_start: str,
    registration_end: str,
    team_size: int = 6,
    created_by: int | None = None,
) -> dict[str, Any]:
    """Create a new tournament period. Deactivates any existing active period first."""
    guild = int(guild_id)
    tsize = max(2, min(20, int(team_size)))
    # Deactivate existing active periods for this guild
    await db.execute_async(
        "UPDATE tournament_periods SET is_active = 0 WHERE guild_id = ? AND is_active = 1",
        (guild,),
    )
    await db.execute_async(
        """
        INSERT INTO tournament_periods(guild_id, name, registration_start, registration_end, is_active, team_size, created_by)
        VALUES(?, ?, ?, ?, 1, ?, ?)
        """,
        (
            guild,
            str(name),
            str(registration_start),
            str(registration_end),
            tsize,
            int(created_by) if created_by else None,
        ),
    )
    row = await db.query_one_async(
        "SELECT * FROM tournament_periods WHERE guild_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
        (guild,),
    )
    return _row_to_dict(row)


async def get_active_period_async(guild_id: int) -> dict[str, Any] | None:
    """Get the current active period for a guild (is_active=1), regardless of time window."""
    row = await db.query_one_async(
        """
        SELECT id, guild_id, name, registration_start, registration_end, is_active, created_by, created_at
        FROM tournament_periods
        WHERE guild_id = ? AND is_active = 1
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(guild_id),),
    )
    if row:
        return _row_to_dict(row)
    return None


async def close_period_async(guild_id: int, period_id: int) -> bool:
    """Deactivate a period by ID."""
    exists = await db.query_one_async(
        "SELECT 1 FROM tournament_periods WHERE guild_id = ? AND id = ?",
        (int(guild_id), int(period_id)),
    )
    if not exists:
        return False
    await db.execute_async(
        "UPDATE tournament_periods SET is_active = 0 WHERE guild_id = ? AND id = ?",
        (int(guild_id), int(period_id)),
    )
    return True


async def list_periods_async(guild_id: int) -> list[dict[str, Any]]:
    """List all periods for a guild, newest first."""
    rows = await db.query_all_async(
        """
        SELECT id, guild_id, name, registration_start, registration_end, is_active, created_at
        FROM tournament_periods
        WHERE guild_id = ?
        ORDER BY id DESC
        """,
        (int(guild_id),),
    )
    return [_row_to_dict(r) for r in rows or []]


async def get_team_async(guild_id: int, team_id: int) -> dict[str, Any] | None:
    """Fetch a single team by ID."""
    row = await db.query_one_async(
        """
        SELECT id, guild_id, name, name_key, created_by, created_at
        FROM customgames_tournament_teams
        WHERE guild_id = ? AND id = ?
        """,
        (int(guild_id), int(team_id)),
    )
    return _row_to_dict(row) if row else None


async def rename_team_async(guild_id: int, team_id: int, new_name: str) -> bool:
    """Rename a team. Validates name length. Returns False if team not found."""
    clean_name = clean_team_name(new_name)  # raises ValueError if invalid
    key = team_name_key(clean_name)
    exists = await db.query_one_async(
        "SELECT 1 FROM customgames_tournament_teams WHERE guild_id = ? AND id = ?",
        (int(guild_id), int(team_id)),
    )
    if not exists:
        return False
    try:
        await db.execute_async(
            "UPDATE customgames_tournament_teams SET name = ?, name_key = ? WHERE guild_id = ? AND id = ?",
            (clean_name, key, int(guild_id), int(team_id)),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise ValueError("Ein Team mit diesem Namen existiert bereits")
        raise
    return True


async def create_auth_token_async(user_id: int, display_name: str, ttl: float = 60) -> str:
    """Create a one-time auth token for the turnier site. Returns the token string."""
    token = uuid.uuid4().hex
    expires_at = time.time() + ttl
    # Purge stale tokens (best-effort)
    try:
        await db.execute_async(
            "DELETE FROM turnier_auth_tokens WHERE expires_at < ?",
            (time.time(),),
        )
    except Exception:
        pass
    await db.execute_async(
        "INSERT INTO turnier_auth_tokens(token, user_id, display_name, expires_at) VALUES(?, ?, ?, ?)",
        (token, int(user_id), str(display_name), expires_at),
    )
    return token


async def consume_auth_token_async(token: str) -> dict[str, Any] | None:
    """Read + delete a one-time auth token. Returns None if missing or expired."""
    row = await db.query_one_async(
        "SELECT user_id, display_name, expires_at FROM turnier_auth_tokens WHERE token = ?",
        (str(token),),
    )
    if not row:
        return None
    # Delete regardless (single-use)
    await db.execute_async("DELETE FROM turnier_auth_tokens WHERE token = ?", (str(token),))
    if float(row["expires_at"]) < time.time():
        return None
    return {
        "user_id": int(row["user_id"]),
        "display_name": str(row["display_name"]),
    }
