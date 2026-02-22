"""Helper zum Speichern und Aktualisieren von Problem-/Bug-Reports."""

from __future__ import annotations

import logging
import time
from typing import Any

from . import db

log = logging.getLogger(__name__)

DEFAULT_STATUS = "pending"
VALID_STATUSES = {"pending", "processing", "answered", "failed", "handoff"}


def _now_ts() -> int:
    return int(time.time())


def _normalize_status(status: str | None) -> str:
    if not status:
        return DEFAULT_STATUS
    if status not in VALID_STATUSES:
        return DEFAULT_STATUS
    return status


async def create_report(
    *,
    user_id: int | None,
    guild_id: int | None,
    channel_id: int | None,
    message_id: int | None,
    category: str | None,
    title: str | None,
    description: str,
    status: str = DEFAULT_STATUS,
) -> int:
    """Erzeugt einen neuen Report und gibt die ID zurück."""

    safe_status = _normalize_status(status)
    ts = _now_ts()
    try:
        async with db.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO issue_reports(
                  user_id,
                  guild_id,
                  channel_id,
                  message_id,
                  category,
                  title,
                  description,
                  status,
                  created_at,
                  updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    guild_id,
                    channel_id,
                    message_id,
                    category,
                    title,
                    description,
                    safe_status,
                    ts,
                    ts,
                ),
            )
            return int(cur.lastrowid or 0)
    except Exception:
        log.exception("Report konnte nicht gespeichert werden")
        return 0


async def update_status(
    report_id: int,
    *,
    status: str,
    ai_response: str | None,
    ai_model: str | None,
    ai_error: str | None = None,
) -> None:
    """Setzt Status und AI-Ergebnis für einen Report."""

    safe_status = _normalize_status(status)
    ts = _now_ts()
    answered_at = ts if safe_status != "pending" else None

    try:
        async with db.transaction() as conn:
            conn.execute(
                """
                UPDATE issue_reports
                SET status = ?,
                    ai_response = ?,
                    ai_model = ?,
                    ai_error = ?,
                    updated_at = ?,
                    answered_at = ?
                WHERE id = ?
                """,
                (
                    safe_status,
                    ai_response,
                    ai_model,
                    ai_error,
                    ts,
                    answered_at,
                    report_id,
                ),
            )
    except Exception:
        log.exception("Report-Status konnte nicht aktualisiert werden (id=%s)", report_id)


async def fetch_report(report_id: int) -> dict[str, Any] | None:
    """Liefert einen Report als Dictionary oder None."""

    try:
        row = await db.query_one_async(
            "SELECT * FROM issue_reports WHERE id = ?",
            (report_id,),
        )
    except Exception:
        log.exception("Report konnte nicht geladen werden (id=%s)", report_id)
        return None

    if not row:
        return None

    try:
        return dict(row)
    except Exception:
        # Fallback falls row kein Mapping ist
        try:
            return {k: row[idx] for idx, k in enumerate(getattr(row, "keys", lambda: [])())}
        except Exception:
            return None
