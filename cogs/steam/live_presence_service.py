import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from service import db
from service import steam as steam_service


log = logging.getLogger("SteamPresenceService")


@dataclass
class SteamPresenceInfo:
    steam_id: str
    updated_at: int
    display: Optional[str]
    status: Optional[str]
    status_text: Optional[str]
    player_group: Optional[str]
    player_group_size: Optional[int]
    connect: Optional[str]
    mode: Optional[str]
    map_name: Optional[str]
    party_size: Optional[int]
    raw: Dict[str, Any]
    summary_raw: Optional[Dict[str, Any]]
    friend_snapshot_raw: Optional[Dict[str, Any]]
    phase_hint: Optional[str]
    is_match: bool
    is_lobby: bool
    is_deadlock: bool


class SteamPresenceService:
    """Kapselt sämtliche Steam-bezogenen Hilfsfunktionen für Live-Match."""

    def __init__(self, *, steam_api_key: str, deadlock_app_id: str):
        self._steam_api_key = steam_api_key.strip()
        self._deadlock_app_id = deadlock_app_id.strip()

    # ------------------------------------------------------------------ schema
    def ensure_schema(self) -> None:
        """Stellt sicher, dass die Steam-Link-Tabelle vorhanden ist."""
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

    # ---------------------------------------------------------------- presence
    def load_presence_map(
        self,
        steam_ids: Iterable[str],
        now: int,
        *,
        freshness_sec: int,
    ) -> Dict[str, SteamPresenceInfo]:
        ids = sorted({str(sid) for sid in steam_ids if sid})
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        min_ts = max(0, int(now) - freshness_sec)
        rows = db.query_all(
            f"""
            SELECT steam_id, app_id, status, status_text, display, player_group,
                   player_group_size, connect, mode, map, party_size, raw_json,
                   updated_at, last_update
            FROM steam_rich_presence
            WHERE steam_id IN ({placeholders})
              AND COALESCE(updated_at, last_update, 0) >= ?
            """,
            (*ids, int(min_ts)),
        )
        presence: Dict[str, SteamPresenceInfo] = {}
        for row in rows:
            raw_json = row["raw_json"] if isinstance(row, dict) else row[11]
            try:
                raw = {} if raw_json in (None, "") else dict(json.loads(raw_json))
            except Exception:
                raw = {}
            steam_id = str(row["steam_id"] if isinstance(row, dict) else row[0])
            try:
                updated_at = int(
                    (row["updated_at"] if isinstance(row, dict) else row[12])
                    or (row["last_update"] if isinstance(row, dict) else row[13])
                    or 0
                )
            except Exception:
                updated_at = 0
            info_dict = {
                "steam_id": steam_id,
                "updated_at": updated_at,
                "status": row["status"] if isinstance(row, dict) else row[2],
                "status_text": row["status_text"] if isinstance(row, dict) else row[3],
                "display": row["display"] if isinstance(row, dict) else row[4],
                "player_group": row["player_group"] if isinstance(row, dict) else row[5],
                "player_group_size": row["player_group_size"] if isinstance(row, dict) else row[6],
                "connect": row["connect"] if isinstance(row, dict) else row[7],
                "mode": row["mode"] if isinstance(row, dict) else row[8],
                "map_name": row["map"] if isinstance(row, dict) else row[9],
                "party_size": row["party_size"] if isinstance(row, dict) else row[10],
                "raw": raw,
            }
            presence[steam_id] = self._build_presence_info(info_dict)
        return presence

    def load_friend_snapshots(self, steam_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        ids = [str(sid) for sid in steam_ids if sid]
        if not ids:
            return {}
        return steam_service.load_friend_snapshots(ids)

    def attach_friend_snapshots(
        self,
        presence: Dict[str, SteamPresenceInfo],
        friend_snapshots: Dict[str, Dict[str, Any]],
    ) -> None:
        for steam_id, snapshot in friend_snapshots.items():
            info = presence.get(steam_id)
            if info:
                info.friend_snapshot_raw = snapshot
        for steam_id, info in presence.items():
            if steam_id not in friend_snapshots:
                info.friend_snapshot_raw = None

    # ----------------------------------------------------------------- helpers
    def _build_presence_info(self, entry: Dict[str, Any]) -> SteamPresenceInfo:
        steam_id = str(entry.get("steam_id") or "")
        updated_at = int(entry.get("updated_at") or 0)
        status = entry.get("status")
        status_text = entry.get("status_text")
        display = entry.get("display")
        player_group = entry.get("player_group") or entry.get("raw", {}).get("steam_player_group")
        raw_group_size = entry.get("player_group_size") or entry.get("raw", {}).get("steam_player_group_size")
        try:
            player_group_size = int(raw_group_size) if raw_group_size is not None else None
        except (TypeError, ValueError):
            player_group_size = None
        connect = entry.get("connect") or entry.get("raw", {}).get("connect")
        mode = entry.get("mode") or entry.get("raw", {}).get("mode")
        map_name = entry.get("map_name") or entry.get("raw", {}).get("map")
        raw_party_size = entry.get("party_size") or entry.get("raw", {}).get("party_size")
        try:
            party_size = int(raw_party_size) if raw_party_size is not None else None
        except (TypeError, ValueError):
            party_size = None
        raw = entry.get("raw") if isinstance(entry.get("raw"), dict) else {}
        summary_payload = entry.get("summary_raw")
        if isinstance(summary_payload, str):
            try:
                summary_raw: Optional[Dict[str, Any]] = json.loads(summary_payload)
            except json.JSONDecodeError:
                summary_raw = None
        elif isinstance(summary_payload, dict):
            summary_raw = dict(summary_payload)
        else:
            summary_raw = None

        friend_snapshot_raw = entry.get("friend_snapshot_raw")
        if isinstance(friend_snapshot_raw, dict):
            friend_snapshot = dict(friend_snapshot_raw)
        else:
            friend_snapshot = friend_snapshot_raw if friend_snapshot_raw is None else friend_snapshot_raw

        phase_hint = self._presence_phase_hint(
            {
                "status": status,
                "status_text": status_text,
                "display": display,
                "player_group": player_group,
                "player_group_size": player_group_size,
                "connect": connect,
                "raw": raw,
            }
        )
        is_match = phase_hint == "MATCH"
        is_lobby = phase_hint == "LOBBY"
        is_deadlock = self._presence_in_deadlock(
            {
                "status": status,
                "status_text": status_text,
                "display": display,
                "raw": raw,
            }
        )

        return SteamPresenceInfo(
            steam_id=steam_id,
            updated_at=updated_at,
            display=display,
            status=status,
            status_text=status_text,
            player_group=str(player_group) if player_group else None,
            player_group_size=player_group_size,
            connect=connect,
            mode=mode,
            map_name=map_name,
            party_size=party_size,
            raw=raw,
            summary_raw=summary_raw,
            friend_snapshot_raw=friend_snapshot if isinstance(friend_snapshot, dict) else friend_snapshot,
            phase_hint=phase_hint,
            is_match=is_match,
            is_lobby=is_lobby,
            is_deadlock=is_deadlock,
        )

    @staticmethod
    def _presence_phase_hint(data: Dict[str, Any]) -> Optional[str]:
        connect = data.get("connect") or data.get("raw", {}).get("connect")
        if isinstance(connect, str) and connect:
            return "MATCH"

        group = data.get("player_group")
        group_size = data.get("player_group_size")
        try:
            group_size_int = int(group_size) if group_size is not None else 0
        except (TypeError, ValueError):
            group_size_int = 0
        if group:
            if group_size is None or group_size_int:
                return "LOBBY"

        texts: List[str] = []
        for key in ("status", "status_text", "display"):
            val = data.get(key)
            if val:
                texts.append(str(val))
        raw = data.get("raw") or {}
        for key in ("status", "steam_display", "display", "rich_presence"):
            val = raw.get(key)
            if val:
                texts.append(str(val))
        blob = " ".join(texts).lower()

        match_terms = (
            "#deadlock_status_inmatch",
            "in match",
            "match",
            "playing match",
        )
        lobby_terms = (
            "lobby",
            "queue",
            "warteschlange",
            "search",
            "searching",
            "suche",
        )
        game_terms = (
            "#deadlock_status_ingame",
            "ingame",
            "im spiel",
            "playing",
            "spiel",
            "game",
        )

        if any(term in blob for term in match_terms):
            return "MATCH"
        if any(term in blob for term in lobby_terms):
            return "LOBBY"
        if any(term in blob for term in game_terms):
            return "GAME"
        return None

    @staticmethod
    def _presence_in_deadlock(data: Dict[str, Any]) -> bool:
        raw = data.get("raw") or {}
        texts = [
            str(data.get("status") or ""),
            str(data.get("status_text") or ""),
            str(data.get("display") or ""),
            str(raw.get("steam_display") or ""),
            str(raw.get("status") or ""),
        ]
        blob = " ".join(t for t in texts if t).lower()
        return "deadlock" in blob


__all__ = ["SteamPresenceService", "SteamPresenceInfo"]

