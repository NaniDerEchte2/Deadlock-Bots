"""Utilities for reserving pre-generated Steam quick-invite links from the shared DB."""
from __future__ import annotations

import dataclasses
import logging
import sqlite3
import time
from typing import Optional

from service import db

log = logging.getLogger(__name__)


@dataclasses.dataclass(slots=True)
class QuickInvite:
    token: str
    invite_link: str
    invite_limit: int
    invite_duration: Optional[int]
    created_at: int
    expires_at: Optional[int]
    status: str
    reserved_by: Optional[int]
    reserved_at: Optional[int]


_SELECT_AVAILABLE = """
SELECT token, invite_link, invite_limit, invite_duration, created_at,
       expires_at, status, reserved_by, reserved_at
FROM steam_quick_invites
WHERE status = 'available'
  AND (expires_at IS NULL OR expires_at > strftime('%s','now'))
ORDER BY created_at ASC
LIMIT 1
"""

_MARK_SHARED = """
UPDATE steam_quick_invites
SET status = 'shared',
    reserved_by = ?,
    reserved_at = strftime('%s','now')
WHERE token = ? AND status = 'available'
"""


def reserve_quick_invite(discord_user_id: Optional[int] = None) -> Optional[QuickInvite]:
    """Reserve a single-use quick invite link for the given Discord user.

    Returns ``None`` if no invite link is currently available.
    """

    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        log.debug("Failed to open transaction for quick invite reservation: %s", exc)
        return None

    try:
        row = conn.execute(_SELECT_AVAILABLE).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None

        token = row["token"]
        cursor = conn.execute(_MARK_SHARED, (int(discord_user_id) if discord_user_id else None, token))
        if cursor.rowcount < 1:
            conn.execute("ROLLBACK")
            return None

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    reserved_ts = int(time.time())

    return QuickInvite(
        token=row["token"],
        invite_link=row["invite_link"],
        invite_limit=int(row["invite_limit"] or 0),
        invite_duration=row["invite_duration"],
        created_at=int(row["created_at"] or 0),
        expires_at=row["expires_at"],
        status="shared",
        reserved_by=int(discord_user_id) if discord_user_id else None,
        reserved_at=reserved_ts,
    )


__all__ = ["QuickInvite", "reserve_quick_invite"]
