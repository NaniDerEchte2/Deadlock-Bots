from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Dict, Iterable, List, Tuple

from service import db

log = logging.getLogger(__name__)

# Namespaces with user-specific KV entries
AI_ONBOARDING_SESSIONS_NS = "ai_onboarding:sessions"
AI_ONBOARDING_VIEWS_NS = "ai_onboarding:persistent_views"
VOICE_NUDGE_NAMESPACES = ("voice_nudge_first_seen", "voice_nudge_done")

# Tables keyed by a single user column
_USER_TABLES: Tuple[Tuple[str, str], ...] = (
    ("voice_stats", "user_id"),
    ("voice_session_log", "user_id"),
    ("voice_feedback_requests", "user_id"),
    ("voice_feedback_responses", "user_id"),
    ("user_activity_patterns", "user_id"),
    ("message_activity", "user_id"),
    ("member_events", "user_id"),
    ("steam_links", "user_id"),
    ("steam_beta_invites", "discord_id"),
    ("beta_invite_intent", "discord_id"),
    ("beta_invite_audit", "discord_id"),
    ("server_faq_logs", "user_id"),
    ("persistent_views", "user_id"),
    ("user_retention_tracking", "user_id"),
    ("user_retention_messages", "user_id"),
    ("voice_channel_anchors", "user_id"),
    ("coaching_sessions", "user_id"),
    ("claimed_threads", "assigned_user_id"),
    ("claimed_threads", "claimed_by_id"),
    ("steam_nudge_state", "user_id"),
    ("twitch_streamers", "discord_user_id"),
    ("twitch_link_clicks", "discord_user_id"),
    ("user_data", "user_id"),
    ("notification_log", "user_id"),
    ("notification_queue", "user_id"),
    ("dm_response_tracking", "user_id"),
    ("tempvoice_owner_prefs", "owner_id"),
    ("tempvoice_lanes", "owner_id"),
    ("tempvoice_lurkers", "user_id"),
    ("tempvoice_bans", "owner_id"),
    ("tempvoice_bans", "banned_id"),
    ("steam_quick_invites", "reserved_by"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def is_opted_out(user_id: int) -> bool:
    """Synchronously check whether the user is opted out."""
    try:
        row = db.query_one(
            "SELECT opted_out FROM user_privacy WHERE user_id=?",
            (int(user_id),),
        )
        return bool(row and row[0])
    except Exception:
        log.debug("privacy check failed for user %s", user_id, exc_info=True)
        return False


async def set_opt_in(user_id: int) -> None:
    """Remove opt-out flag (user opt-in)."""
    ts = int(time.time())
    async with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO user_privacy(user_id, opted_out, deleted_at, reason, updated_at)
            VALUES (?, 0, NULL, 'user_opt_in', ?)
            ON CONFLICT(user_id) DO UPDATE SET
              opted_out = 0,
              deleted_at = NULL,
              reason = excluded.reason,
              updated_at = excluded.updated_at
            """,
            (int(user_id), ts),
        )


def _delete_where(conn: sqlite3.Connection, table: str, column: str, value: object) -> int:
    if not _table_exists(conn, table):
        return 0
    cur = conn.execute(f"DELETE FROM {table} WHERE {column}=?", (value,))
    return max(cur.rowcount or 0, 0)


def _delete_any_of(conn: sqlite3.Connection, table: str, columns: Iterable[str], value: object) -> int:
    cols = list(columns)
    if not cols or not _table_exists(conn, table):
        return 0
    clause = " OR ".join([f"{c}=?" for c in cols])
    cur = conn.execute(f"DELETE FROM {table} WHERE {clause}", tuple(value for _ in cols))
    return max(cur.rowcount or 0, 0)


def _fetch_rows(conn: sqlite3.Connection, table: str, column: str, value: object) -> List[Dict[str, object]]:
    if not _table_exists(conn, table):
        return []
    cur = conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (value,))
    cols = [col[0] for col in cur.description or []]
    rows = cur.fetchall() or []
    return [{col: row[idx] for idx, col in enumerate(cols)} for row in rows]


def _fetch_rows_any(conn: sqlite3.Connection, table: str, columns: Iterable[str], value: object) -> List[Dict[str, object]]:
    cols = list(columns)
    if not cols or not _table_exists(conn, table):
        return []
    clause = " OR ".join([f"{c}=?" for c in cols])
    cur = conn.execute(f"SELECT * FROM {table} WHERE {clause}", tuple(value for _ in cols))
    colnames = [col[0] for col in cur.description or []]
    rows = cur.fetchall() or []
    return [{name: row[idx] for idx, name in enumerate(colnames)} for row in rows]


def _redact_co_players(rows: List[Dict[str, object]], uid: int) -> List[Dict[str, object]]:
    sanitized: List[Dict[str, object]] = []
    for row in rows:
        user_id = row.get("user_id")
        co_player_id = row.get("co_player_id")

        # only keep rows where the user is involved
        if int(user_id or uid) != uid and int(co_player_id or uid) != uid:
            continue

        row_copy = dict(row)
        if "co_player_id" in row_copy:
            row_copy["co_player_id"] = "redacted"
        if int(user_id or uid) != uid:
            row_copy["user_id"] = uid
        sanitized.append(row_copy)
    return sanitized


def _redact_other_ids(rows: List[Dict[str, object]], uid: int, keep: str, redact_fields: Iterable[str]) -> List[Dict[str, object]]:
    cleaned: List[Dict[str, object]] = []
    for row in rows:
        r = dict(row)
        for key in redact_fields:
            if key == keep:
                continue
            if key in r and int(r.get(key) or 0) != uid:
                r[key] = "redacted"
        cleaned.append(r)
    return cleaned


def _purge_kv_entries(conn: sqlite3.Connection, user_id: int, summary: Dict[str, int]) -> None:
    if not _table_exists(conn, "kv_store"):
        return

    removed = conn.execute(
        "DELETE FROM kv_store WHERE ns=? AND k=?",
        (AI_ONBOARDING_SESSIONS_NS, str(int(user_id))),
    ).rowcount
    summary["kv_ai_onboarding_sessions"] = max(removed or 0, 0)

    removed_views = 0
    rows = conn.execute(
        "SELECT k, v FROM kv_store WHERE ns=?",
        (AI_ONBOARDING_VIEWS_NS,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["v"] if isinstance(row, sqlite3.Row) else row[1])
        except Exception:
            continue
        if int(payload.get("user_id", 0) or 0) == int(user_id):
            conn.execute(
                "DELETE FROM kv_store WHERE ns=? AND k=?",
                (AI_ONBOARDING_VIEWS_NS, str(row["k"] if isinstance(row, sqlite3.Row) else row[0])),
            )
            removed_views += 1
    summary["kv_ai_onboarding_views"] = removed_views

    nudge_removed = 0
    for ns in VOICE_NUDGE_NAMESPACES:
        cur = conn.execute(
            "DELETE FROM kv_store WHERE ns=? AND k=?",
            (ns, str(int(user_id))),
        )
        nudge_removed += max(cur.rowcount or 0, 0)
    summary["kv_voice_nudge"] = nudge_removed


def _fetch_steam_ids(conn: sqlite3.Connection, user_id: int) -> List[str]:
    if not _table_exists(conn, "steam_links"):
        return []
    rows = conn.execute(
        "SELECT steam_id FROM steam_links WHERE user_id=?",
        (int(user_id),),
    ).fetchall()
    steam_ids: List[str] = []
    for row in rows:
        try:
            steam_ids.append(str(row["steam_id"] if isinstance(row, sqlite3.Row) else row[0]))
        except Exception:
            continue
    return steam_ids


def _purge_steam_side(conn: sqlite3.Connection, steam_ids: Iterable[str], summary: Dict[str, int]) -> None:
    for sid in steam_ids:
        summary[f"live_player_state:{sid}"] = _delete_where(conn, "live_player_state", "steam_id", sid)
        summary[f"deadlock_voice_watch:{sid}"] = _delete_where(conn, "deadlock_voice_watch", "steam_id", sid)
        summary[f"steam_rich_presence:{sid}"] = _delete_where(conn, "steam_rich_presence", "steam_id", sid)
        summary[f"steam_presence_watchlist:{sid}"] = _delete_where(conn, "steam_presence_watchlist", "steam_id", sid)
        summary[f"steam_friend_requests:{sid}"] = _delete_where(conn, "steam_friend_requests", "steam_id", sid)
        summary[f"steam_beta_invites:{sid}"] = _delete_where(conn, "steam_beta_invites", "steam_id64", sid)
        summary[f"beta_invite_audit:{sid}"] = _delete_where(conn, "beta_invite_audit", "steam_id64", sid)


def export_user_data(user_id: int) -> Dict[str, object]:
    """
    Build a JSON-friendly snapshot of all known user data (read-only).
    """
    uid = int(user_id)
    snapshot: Dict[str, object] = {
        "user_id": uid,
        "generated_at": int(time.time()),
        "tables": {},
        "kv": {},
        "steam_ids": [],
    }
    with db.get_conn() as conn:
        tables: Dict[str, List[Dict[str, object]]] = {}
        for table, column in _USER_TABLES:
            key = f"{table}.{column}"
            tables[key] = _fetch_rows(conn, table, column, uid)

        co_player_rows = _fetch_rows_any(conn, "user_co_players", ("user_id", "co_player_id"), uid)
        tables["user_co_players"] = _redact_co_players(co_player_rows, uid)

        steam_ids = _fetch_steam_ids(conn, uid)
        snapshot["steam_ids"] = steam_ids
        for sid in steam_ids:
            tables[f"live_player_state:{sid}"] = _fetch_rows(conn, "live_player_state", "steam_id", sid)
            tables[f"deadlock_voice_watch:{sid}"] = _fetch_rows(conn, "deadlock_voice_watch", "steam_id", sid)
            tables[f"steam_rich_presence:{sid}"] = _fetch_rows(conn, "steam_rich_presence", "steam_id", sid)
            tables[f"steam_presence_watchlist:{sid}"] = _fetch_rows(conn, "steam_presence_watchlist", "steam_id", sid)
            tables[f"steam_friend_requests:{sid}"] = _fetch_rows(conn, "steam_friend_requests", "steam_id", sid)
            tables[f"steam_beta_invites:{sid}"] = _fetch_rows(conn, "steam_beta_invites", "steam_id64", sid)
            tables[f"beta_invite_audit:{sid}"] = _fetch_rows(conn, "beta_invite_audit", "steam_id64", sid)

        snapshot["tables"] = tables

        kv_data: Dict[str, object] = {}
        if _table_exists(conn, "kv_store"):
            kv_data["ai_onboarding_sessions"] = (
                db.get_kv(AI_ONBOARDING_SESSIONS_NS, str(uid)) or None
            )
            kv_data["ai_onboarding_views"] = []
            rows = conn.execute(
                "SELECT k, v FROM kv_store WHERE ns=?",
                (AI_ONBOARDING_VIEWS_NS,),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["v"] if isinstance(row, sqlite3.Row) else row[1])
                except Exception:
                    continue
                target_uid = int(payload.get("user_id", 0) or 0)
                if target_uid == uid:
                    kv_data["ai_onboarding_views"].append(payload)
            kv_data["voice_nudge"] = {
                "first_seen": db.get_kv(VOICE_NUDGE_NAMESPACES[0], str(uid)),
                "done": db.get_kv(VOICE_NUDGE_NAMESPACES[1], str(uid)),
            }
        snapshot["kv"] = kv_data

        if _table_exists(conn, "user_privacy"):
            snapshot["user_privacy"] = _fetch_rows(conn, "user_privacy", "user_id", uid)

        # Redaction: co_player_ids in voice logs (contains other users)
        voice_log_key = "voice_session_log.user_id"
        if voice_log_key in tables:
            for row in tables[voice_log_key]:
                if "co_player_ids" in row:
                    row["co_player_ids"] = None

        # Redaction: avoid leaking foreign IDs in mixed tables
        if "tempvoice_bans.owner_id" in tables:
            tables["tempvoice_bans.owner_id"] = _redact_other_ids(
                tables["tempvoice_bans.owner_id"], uid, keep="owner_id", redact_fields=("banned_id",)
            )
        if "tempvoice_bans.banned_id" in tables:
            tables["tempvoice_bans.banned_id"] = _redact_other_ids(
                tables["tempvoice_bans.banned_id"], uid, keep="banned_id", redact_fields=("owner_id",)
            )
        if "claimed_threads.assigned_user_id" in tables:
            tables["claimed_threads.assigned_user_id"] = _redact_other_ids(
                tables["claimed_threads.assigned_user_id"], uid, keep="assigned_user_id", redact_fields=("claimed_by_id",)
            )
        if "claimed_threads.claimed_by_id" in tables:
            tables["claimed_threads.claimed_by_id"] = _redact_other_ids(
                tables["claimed_threads.claimed_by_id"], uid, keep="claimed_by_id", redact_fields=("assigned_user_id",)
            )

    return snapshot


async def delete_user_data(user_id: int, *, reason: str = "user_request") -> Dict[str, int]:
    """
    Delete user-related data from all known tables and mark an opt-out.
    Returns a summary of deleted rows per table key.
    """
    uid = int(user_id)
    ts = int(time.time())
    summary: Dict[str, int] = {}

    async with db.transaction() as conn:
        steam_ids = _fetch_steam_ids(conn, uid)

        for table, column in _USER_TABLES:
            summary[f"{table}.{column}"] = _delete_where(conn, table, column, uid)

        summary["user_co_players"] = _delete_any_of(
            conn, "user_co_players", ("user_id", "co_player_id"), uid
        )

        _purge_steam_side(conn, steam_ids, summary)

        _purge_kv_entries(conn, uid, summary)

        conn.execute(
            """
            INSERT INTO user_privacy(user_id, opted_out, deleted_at, reason, updated_at)
            VALUES (?, 1, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              opted_out = 1,
              deleted_at = excluded.deleted_at,
              reason = excluded.reason,
              updated_at = excluded.updated_at
            """,
            (uid, ts, reason, ts),
        )
        summary["user_privacy_updated"] = 1
        summary["steam_ids_removed"] = len(steam_ids)
        summary["steam_ids"] = steam_ids

    return summary
