from __future__ import annotations

import logging
from typing import Iterable

from service import db
from cogs.steam.logging_utils import safe_log_extra, sanitize_log_value

log = logging.getLogger("SteamFriendRequests")


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS steam_friend_requests(
  steam_id TEXT PRIMARY KEY,
  status TEXT DEFAULT 'pending',
  requested_at INTEGER DEFAULT (strftime('%s','now')),
  last_attempt INTEGER,
  attempts INTEGER DEFAULT 0,
  error TEXT
)
"""


def _ensure_table() -> None:
    try:
        db.execute(_CREATE_SQL)
    except Exception:
        log.exception("Failed to ensure steam_friend_requests table exists")
        raise


def _queue_single(steam_id: str) -> None:
    if not steam_id:
        return
    sid = str(steam_id).strip()
    if not sid:
        return
    try:
        db.execute(
            """
            INSERT INTO steam_friend_requests(steam_id, status)
            VALUES(?, 'pending')
            ON CONFLICT(steam_id) DO UPDATE SET
              status=excluded.status,
              last_attempt=NULL,
              attempts=0,
              error=NULL
            WHERE steam_friend_requests.status != 'sent'
            """,
            (sid,),
        )
    except Exception:
        safe_sid = sanitize_log_value(sid)
        log.exception(
            "Failed to queue Steam friend request",
            extra={"steam_id": safe_sid},
        )


def queue_friend_requests(steam_ids: Iterable[str]) -> None:
    """Queue outgoing Steam friend requests for the given SteamIDs."""
    if not steam_ids:
        return
    _ensure_table()
    for steam_id in steam_ids:
        _queue_single(steam_id)


def queue_friend_request(steam_id: str) -> None:
    """Queue a single outgoing Steam friend request."""
    _ensure_table()
    _queue_single(steam_id)


__all__ = ["queue_friend_request", "queue_friend_requests"]

