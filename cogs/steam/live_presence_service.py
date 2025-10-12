"""Compatibility shim for the removed Steam rich presence service.

The live match cogs still import :mod:`cogs.steam.live_presence_service` to
retrieve Steam rich presence data.  The legacy implementation depended on the
external Node.js service that has been removed in favour of the new
:mod:`cogs.steam.deadlock_presence` cog.  To keep the live-match stack loading
without the legacy service we provide a lightweight stub that focuses on the
Steam link bookkeeping while returning empty presence payloads.

This allows the remaining cogs (e.g. live match orchestration, Steam link
OAuth) to continue working with the shared ``steam_links`` table without
crashing during imports.  Whenever presence data is requested we simply return
an empty mapping and log a one-time info message so operators know the rich
presence feed is unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from service import db

log = logging.getLogger(__name__)


@dataclass
class SteamPresenceInfo:
    """Minimal data container to satisfy existing live match code paths."""

    steam_id: str
    updated_at: int = 0
    display: Optional[str] = None
    status: Optional[str] = None
    status_text: Optional[str] = None
    display_activity: Optional[str] = None
    hero: Optional[str] = None
    session_minutes: Optional[int] = None
    player_group: Optional[str] = None
    player_group_size: Optional[int] = None
    connect: Optional[str] = None
    mode: Optional[str] = None
    map_name: Optional[str] = None
    party_size: Optional[int] = None
    phase_hint: Optional[str] = None
    is_match: bool = False
    is_lobby: bool = False
    is_deadlock: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)
    summary_raw: Optional[Dict[str, Any]] = None
    friend_snapshot_raw: Optional[Dict[str, Any]] = None
    source: str = "disabled"
    is_stale: bool = True


class SteamPresenceService:
    """Stub implementation that keeps Steam link utilities available."""

    def __init__(self, *, steam_api_key: str, deadlock_app_id: str | int):
        self._steam_api_key = str(steam_api_key or "").strip()
        self._deadlock_app_id = str(deadlock_app_id or "").strip()
        self._logged_presence_notice = False

    # ------------------------------------------------------------------ schema
    def ensure_schema(self) -> None:
        """Ensure the ``steam_links`` table exists for OAuth/link flows."""

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS steam_links(
              user_id    INTEGER NOT NULL,
              steam_id   TEXT    NOT NULL,
              name       TEXT,
              verified   INTEGER DEFAULT 0,
              primary_account INTEGER DEFAULT 0,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(user_id, steam_id)
            )
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_user  ON steam_links(user_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_steam ON steam_links(steam_id)")

    # ------------------------------------------------------------------ links
    def load_links(self, user_ids: Iterable[int]) -> Dict[int, List[str]]:
        """Return the Steam IDs linked to the provided Discord user IDs."""

        ids = list({int(uid) for uid in user_ids})
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = db.query_all(
            f"SELECT user_id, steam_id FROM steam_links WHERE user_id IN ({placeholders})",
            tuple(ids),
        )
        mapping: Dict[int, List[str]] = {}
        for row in rows:
            try:
                user_id = int(row["user_id"] if isinstance(row, dict) else row[0])
                steam_id = str(row["steam_id"] if isinstance(row, dict) else row[1])
            except Exception:
                continue
            if not steam_id:
                continue
            mapping.setdefault(user_id, []).append(steam_id)
        return mapping

    # --------------------------------------------------------------- presence
    def _log_presence_disabled(self) -> None:
        if not self._logged_presence_notice:
            log.info(
                "Steam rich presence polling is disabled; returning empty snapshots."
            )
            self._logged_presence_notice = True

    def load_presence_map(
        self,
        steam_ids: Iterable[str],
        now: int,
        *,
        freshness_sec: int,
    ) -> Dict[str, SteamPresenceInfo]:
        self._log_presence_disabled()
        return {}

    def load_friend_snapshots(self, steam_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        self._log_presence_disabled()
        return {}

    def attach_friend_snapshots(
        self,
        presence: Dict[str, SteamPresenceInfo],
        friend_snapshots: Dict[str, Dict[str, Any]],
        *,
        now: Optional[int] = None,
        freshness_sec: Optional[int] = None,
    ) -> None:
        # Presence snapshots are unavailable, but we still normalise the mapping to avoid
        # stale references in the live match cog.
        if not presence:
            return
        for steam_id, info in presence.items():
            info.friend_snapshot_raw = friend_snapshots.get(steam_id)
            info.source = "presence"
            info.is_stale = True


__all__ = ["SteamPresenceInfo", "SteamPresenceService"]
