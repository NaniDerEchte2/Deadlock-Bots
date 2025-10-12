import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
    display_activity: Optional[str]
    hero: Optional[str]
    session_minutes: Optional[int]
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
    source: str
    is_stale: bool


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
            """,
            tuple(ids),
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
                "source": "presence",
            }
            info = self._build_presence_info(info_dict)
            info.is_stale = bool(updated_at <= 0 or updated_at < min_ts)
            presence[steam_id] = info
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
        *,
        now: Optional[int] = None,
        freshness_sec: Optional[int] = None,
    ) -> None:
        for steam_id, snapshot in friend_snapshots.items():
            info = presence.get(steam_id)
            if info:
                self._apply_friend_snapshot(info, snapshot, now=now, freshness_sec=freshness_sec)
                continue
            entry = self._entry_from_friend_snapshot(snapshot)
            info = self._build_presence_info(entry)
            info.source = "snapshot"
            if now is not None and freshness_sec is not None and info.updated_at:
                info.is_stale = bool((now - int(info.updated_at)) > int(freshness_sec))
            else:
                info.is_stale = False
            self._apply_friend_snapshot(info, snapshot, now=now, freshness_sec=freshness_sec)
            presence[steam_id] = info
        for steam_id, info in presence.items():
            if steam_id not in friend_snapshots:
                info.friend_snapshot_raw = None
                if info.source == "presence+snapshot":
                    info.source = "presence"
                elif info.source == "snapshot":
                    info.source = "snapshot-stale"
                    info.is_stale = True
                self._refresh_presence_flags(info)

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

        display_activity, hero, minutes = self._extract_deadlock_activity(
            display,
            status,
            status_text,
            raw,
        )

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
            display_activity=display_activity,
            hero=hero,
            session_minutes=minutes,
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
            source=str(entry.get("source") or "presence"),
            is_stale=bool(entry.get("is_stale") or False),
        )

    @staticmethod
    def _extract_deadlock_activity(
        display: Optional[str],
        status: Optional[str],
        status_text: Optional[str],
        raw: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[str], Optional[int]]:
        """Ermittelt Aktivität, Heldenname und Spielzeit aus den Presence-Daten."""

        def _clean(text: Optional[str]) -> Optional[str]:
            if not text:
                return None
            cleaned = str(text).strip()
            return cleaned or None

        primary_display = _clean(display) or _clean(raw.get("steam_display"))
        fallback_status = _clean(status_text) or _clean(status)

        activity = None
        hero = _clean(raw.get("hero") or raw.get("character") or raw.get("role"))
        minutes: Optional[int] = None

        target = primary_display or fallback_status
        if target:
            # Erwartetes Format: "Deadlock: Abrams (7 Min.)" o.Ä.
            parts = target.split(":", 1)
            if len(parts) == 2 and parts[0].strip().lower() == "deadlock":
                activity = _clean(parts[1])
            else:
                activity = _clean(target)

        if activity:
            # Extrahiere optionale Minutenangabe und Heldenname aus dem Aktivitätstext
            minute_match = re.search(
                r"\((?P<num>\d{1,3})\s*(?:min(?:\.|uten)?|minutes?|mins?)\)",
                activity,
                flags=re.IGNORECASE,
            )
            if minute_match:
                try:
                    minutes = int(minute_match.group("num"))
                except ValueError:
                    minutes = None
                activity = _clean(activity[: minute_match.start()].strip())

        if not hero and activity:
            hero = activity

        return activity, hero, minutes

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

    @staticmethod
    def _snapshot_fields(snapshot: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str], Optional[str], Dict[str, Any]]:
        if not isinstance(snapshot, dict):
            return None, None, None, {}
        rp_raw = snapshot.get("rich_presence_raw") if isinstance(snapshot.get("rich_presence_raw"), dict) else {}
        rp_raw = dict(rp_raw) if isinstance(rp_raw, dict) else {}
        persona_raw = snapshot.get("persona_raw") if isinstance(snapshot.get("persona_raw"), dict) else {}
        display = rp_raw.get("steam_display") or rp_raw.get("display")
        if not display and persona_raw:
            display = persona_raw.get("gameextrainfo") or persona_raw.get("game_name")
        status = rp_raw.get("status")
        status_text = rp_raw.get("status_text") or status
        return display, status, status_text, rp_raw

    def _snapshot_in_deadlock(self, snapshot: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(snapshot, dict):
            return False
        app_id = snapshot.get("game_app_id")
        if app_id is None:
            persona_raw = snapshot.get("persona_raw")
            if isinstance(persona_raw, dict):
                app_id = (
                    persona_raw.get("game_played_app_id")
                    or persona_raw.get("gameid")
                    or persona_raw.get("gameid_appid")
                )
        if app_id is None:
            rp_raw = snapshot.get("rich_presence_raw")
            if isinstance(rp_raw, dict):
                app_id = rp_raw.get("game_played_app_id") or rp_raw.get("app_id")
        name = snapshot.get("game_name")
        app_id_str = str(app_id or "").strip()
        if app_id_str and self._deadlock_app_id and app_id_str == self._deadlock_app_id:
            return True
        if isinstance(name, str) and "deadlock" in name.lower():
            return True
        return False

    def _entry_from_friend_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        steam_id = str(snapshot.get("steam_id") or "")
        display, status, status_text, rp_raw = self._snapshot_fields(snapshot)
        persona_raw = snapshot.get("persona_raw") if isinstance(snapshot.get("persona_raw"), dict) else None
        try:
            updated_at = int(snapshot.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated_at = 0
        entry_raw = dict(rp_raw) if isinstance(rp_raw, dict) else {}
        return {
            "steam_id": steam_id,
            "updated_at": updated_at,
            "display": display,
            "status": status,
            "status_text": status_text,
            "player_group": entry_raw.get("steam_player_group"),
            "player_group_size": entry_raw.get("steam_player_group_size"),
            "connect": entry_raw.get("connect"),
            "mode": entry_raw.get("mode"),
            "map_name": entry_raw.get("map"),
            "party_size": entry_raw.get("party_size"),
            "raw": entry_raw,
            "summary_raw": persona_raw,
            "friend_snapshot_raw": snapshot,
            "source": "snapshot",
        }

    def _apply_friend_snapshot(
        self,
        info: SteamPresenceInfo,
        snapshot: Dict[str, Any],
        *,
        now: Optional[int],
        freshness_sec: Optional[int],
    ) -> None:
        if not isinstance(snapshot, dict):
            info.friend_snapshot_raw = None
            self._refresh_presence_flags(info)
            return
        snapshot_copy = dict(snapshot)
        info.friend_snapshot_raw = snapshot_copy
        persona_raw = snapshot_copy.get("persona_raw")
        if isinstance(persona_raw, dict):
            info.summary_raw = dict(persona_raw)
        display, status, status_text, rp_raw = self._snapshot_fields(snapshot_copy)
        base_raw = dict(info.raw) if isinstance(info.raw, dict) else {}
        for key, value in rp_raw.items():
            if key not in base_raw or base_raw.get(key) in (None, ""):
                base_raw[key] = value
        info.raw = base_raw
        if not info.display and display:
            info.display = display
        if not info.status and status:
            info.status = status
        if not info.status_text and status_text:
            info.status_text = status_text
        if not info.player_group and base_raw.get("steam_player_group"):
            info.player_group = str(base_raw.get("steam_player_group"))
        if info.player_group_size is None and base_raw.get("steam_player_group_size") is not None:
            try:
                info.player_group_size = int(base_raw.get("steam_player_group_size"))
            except (TypeError, ValueError):
                pass
        if not info.connect and base_raw.get("connect"):
            info.connect = base_raw.get("connect")
        if not info.mode and base_raw.get("mode"):
            info.mode = base_raw.get("mode")
        if not info.map_name and base_raw.get("map"):
            info.map_name = base_raw.get("map")
        if info.party_size is None and base_raw.get("party_size") is not None:
            try:
                info.party_size = int(base_raw.get("party_size"))
            except (TypeError, ValueError):
                pass
        try:
            snapshot_updated = int(snapshot_copy.get("updated_at") or 0)
        except (TypeError, ValueError):
            snapshot_updated = 0
        if snapshot_updated:
            info.updated_at = max(int(info.updated_at or 0), snapshot_updated)
        if now is not None and freshness_sec is not None and snapshot_updated:
            info.is_stale = bool((now - snapshot_updated) > int(freshness_sec))
        if info.source == "presence":
            info.source = "presence+snapshot"
        elif not info.source:
            info.source = "snapshot"
        self._refresh_presence_flags(info)

    def _refresh_presence_flags(self, info: SteamPresenceInfo) -> None:
        snapshot_display, snapshot_status, snapshot_status_text, snapshot_raw = self._snapshot_fields(
            info.friend_snapshot_raw
        )
        base_raw = dict(info.raw) if isinstance(info.raw, dict) else {}
        for key, value in snapshot_raw.items():
            if key not in base_raw or base_raw.get(key) in (None, ""):
                base_raw[key] = value
        display = info.display or snapshot_display
        status = info.status or snapshot_status
        status_text = info.status_text or snapshot_status_text or status
        activity, hero, minutes = self._extract_deadlock_activity(display, status, status_text, base_raw)
        info.display = display
        info.status = status
        info.status_text = status_text
        if activity:
            info.display_activity = activity
        if hero:
            info.hero = hero
        elif activity and not info.hero:
            info.hero = activity
        if minutes is not None:
            info.session_minutes = minutes
        if not info.mode and base_raw.get("mode"):
            info.mode = base_raw.get("mode")
        if not info.map_name and base_raw.get("map"):
            info.map_name = base_raw.get("map")
        if info.party_size is None and base_raw.get("party_size") is not None:
            try:
                info.party_size = int(base_raw.get("party_size"))
            except (TypeError, ValueError):
                pass
        if not info.player_group and base_raw.get("steam_player_group"):
            info.player_group = str(base_raw.get("steam_player_group"))
        if info.player_group_size is None and base_raw.get("steam_player_group_size") is not None:
            try:
                info.player_group_size = int(base_raw.get("steam_player_group_size"))
            except (TypeError, ValueError):
                pass
        if not info.connect and base_raw.get("connect"):
            info.connect = base_raw.get("connect")
        info.raw = base_raw
        phase_data = {
            "status": info.status,
            "status_text": info.status_text,
            "display": info.display,
            "player_group": info.player_group,
            "player_group_size": info.player_group_size,
            "connect": info.connect,
            "raw": base_raw,
        }
        phase_hint = self._presence_phase_hint(phase_data)
        if not phase_hint and snapshot_raw:
            phase_hint = self._presence_phase_hint(
                {
                    "status": snapshot_status,
                    "status_text": snapshot_status_text,
                    "display": snapshot_display,
                    "player_group": base_raw.get("steam_player_group"),
                    "player_group_size": base_raw.get("steam_player_group_size"),
                    "connect": base_raw.get("connect"),
                    "raw": base_raw,
                }
            )
        if phase_hint:
            info.phase_hint = phase_hint
        info.is_match = info.phase_hint == "MATCH"
        info.is_lobby = info.phase_hint == "LOBBY"
        presence_deadlock = self._presence_in_deadlock(phase_data)
        snapshot_deadlock = self._snapshot_in_deadlock(info.friend_snapshot_raw)
        info.is_deadlock = presence_deadlock or snapshot_deadlock
        if info.is_deadlock and not info.phase_hint and snapshot_raw:
            snapshot_hint = self._presence_phase_hint(
                {
                    "status": snapshot_status,
                    "status_text": snapshot_status_text,
                    "display": snapshot_display,
                    "player_group": base_raw.get("steam_player_group"),
                    "player_group_size": base_raw.get("steam_player_group_size"),
                    "connect": base_raw.get("connect"),
                    "raw": base_raw,
                }
            )
            if snapshot_hint:
                info.phase_hint = snapshot_hint
                info.is_match = info.phase_hint == "MATCH"
                info.is_lobby = info.phase_hint == "LOBBY"


__all__ = ["SteamPresenceService", "SteamPresenceInfo"]

