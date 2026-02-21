"""Helper zum Speichern von Server-FAQ-Dialogen."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from . import db

log = logging.getLogger(__name__)


def _dump_metadata(metadata: Mapping[str, Any] | None) -> str | None:
    if not metadata:
        return None
    try:
        return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        log.debug("Konnte FAQ-Metadaten nicht serialisieren", exc_info=True)
        return None


def store_exchange(
    *,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    question: str,
    answer: str | None,
    model: str | None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Schreibt eine Frage-Antwort-Interaktion in die Datenbank."""

    payload = _dump_metadata(metadata)
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO server_faq_logs(
              guild_id,
              channel_id,
              user_id,
              question,
              answer,
              model,
              metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                user_id,
                question,
                answer,
                model,
                payload,
            ),
        )
