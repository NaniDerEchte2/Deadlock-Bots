from __future__ import annotations

import json
import logging
import re
from typing import Iterable

from service import db

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


def _queue_single(steam_id: str, trigger_task: bool = False) -> None:
    if not steam_id:
        return
    sid = str(steam_id).strip()
    if not sid:
        return
    try:
        # 1. Passive Queue (für Retry/Status-Tracking)
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

        # 2. Active Task (für Sofort-Ausführung)
        if trigger_task:
            payload = json.dumps({"steam_id": sid})
            db.execute(
                "INSERT INTO steam_tasks(type, payload, status) VALUES (?, ?, 'PENDING')",
                ("AUTH_SEND_FRIEND_REQUEST", payload),
            )
    except Exception:
        log.exception(
            "Failed to queue Steam friend request",
            extra={
                "steam_id_length": len(sid),
                "steam_id_valid": bool(re.fullmatch(r"\d{17,20}", sid)),
            },
        )


def queue_friend_requests(steam_ids: Iterable[str]) -> None:
    """Queue outgoing Steam friend requests for the given SteamIDs."""
    if not steam_ids:
        return
    _ensure_table()
    for steam_id in steam_ids:
        _queue_single(steam_id, trigger_task=True)


def queue_friend_request(steam_id: str) -> None:
    """Queue a single outgoing Steam friend request."""
    _ensure_table()
    _queue_single(steam_id, trigger_task=True)


__all__ = ["queue_friend_request", "queue_friend_requests"]

