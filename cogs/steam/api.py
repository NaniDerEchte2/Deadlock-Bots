"""Hilfsfunktionen rund um Steam-APIs und lokale Caches.

Dieses Modul enthält die zuvor in ``service.steam`` untergebrachten Helfer und
lebt nun im Steam-Cog-Namespace.
"""

from __future__ import annotations

import json
import time
from typing import Dict, Iterable, List

import aiohttp

from service import db

STEAM_API = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"


def _chunked(iterable: Iterable[str], size: int) -> List[List[str]]:
    sequence = list(iterable)
    return [sequence[i : i + size] for i in range(0, len(sequence), size)]


async def batch_get_summaries(
    session: aiohttp.ClientSession, api_key: str, steam_ids: List[str]
) -> Dict[str, dict]:
    if not steam_ids:
        return {}
    out: Dict[str, dict] = {}
    for chunk in _chunked(steam_ids, 100):
        ids = ",".join(chunk)
        async with session.get(
            STEAM_API,
            params={"key": api_key, "steamids": ids},
            timeout=15,
        ) as response:
            if response.status != 200:
                continue
            data = await response.json()
            players = data.get("response", {}).get("players", [])
            for player in players:
                steam_id = str(player.get("steamid"))
                if steam_id:
                    out[steam_id] = player
    return out


def eval_live_state(summary: dict, deadlock_app_id: str) -> dict:
    gameid = str(summary.get("gameid", "") or "")
    lobby = summary.get("lobbysteamid")
    server = summary.get("gameserversteamid")
    in_deadlock_now = gameid == str(deadlock_app_id)
    in_match_now_strict = in_deadlock_now and (bool(lobby) or bool(server))
    server_id = server or lobby or None
    return {
        "steam_id": str(summary.get("steamid")),
        "in_deadlock_now": 1 if in_deadlock_now else 0,
        "in_match_now_strict": 1 if in_match_now_strict else 0,
        "last_gameid": gameid or None,
        "last_server_id": server_id,
    }


def cache_player_state(row: dict) -> None:
    db.execute(
        """
        INSERT INTO live_player_state(
            steam_id,
            last_gameid,
            last_server_id,
            last_seen_ts,
            in_deadlock_now,
            in_match_now_strict
        )
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(steam_id) DO UPDATE SET
          last_gameid=excluded.last_gameid,
          last_server_id=excluded.last_server_id,
          last_seen_ts=excluded.last_seen_ts,
          in_deadlock_now=excluded.in_deadlock_now,
          in_match_now_strict=excluded.in_match_now_strict
        """,
        (
            row["steam_id"],
            row["last_gameid"],
            row["last_server_id"],
            row.get("ts") or time.time(),
            row["in_deadlock_now"],
            row["in_match_now_strict"],
        ),
    )


def load_rich_presence(steam_ids: List[str]) -> Dict[str, dict]:
    if not steam_ids:
        return {}
    placeholders = ",".join("?" for _ in steam_ids)
    rows = db.query_all(
        f"""
        SELECT steam_id,
               app_id,
               status,
               status_text,
               display,
               player_group,
               player_group_size,
               connect,
               mode,
               map,
               party_size,
               raw_json,
               last_update,
               updated_at
        FROM steam_rich_presence
        WHERE steam_id IN ({placeholders})
        """,
        tuple(steam_ids),
    )
    out: Dict[str, dict] = {}
    for row in rows:
        raw_json = row["raw_json"] if isinstance(row, dict) else row[7]
        try:
            raw = json.loads(raw_json) if raw_json else {}
        except json.JSONDecodeError:
            raw = {}
        entry = {
            "steam_id": str(row["steam_id"] if isinstance(row, dict) else row[0]),
            "app_id": row["app_id"] if isinstance(row, dict) else row[1],
            "status": row["status"] if isinstance(row, dict) else row[2],
            "status_text": row["status_text"] if isinstance(row, dict) else row[3],
            "display": row["display"] if isinstance(row, dict) else row[4],
            "player_group": row["player_group"] if isinstance(row, dict) else row[5],
            "player_group_size": row["player_group_size"] if isinstance(row, dict) else row[6],
            "connect": row["connect"] if isinstance(row, dict) else row[7],
            "mode": row["mode"] if isinstance(row, dict) else row[8],
            "map": row["map"] if isinstance(row, dict) else row[9],
            "party_size": row["party_size"] if isinstance(row, dict) else row[10],
            "last_update": row["last_update"] if isinstance(row, dict) else row[12],
            "updated_at": row["updated_at"] if isinstance(row, dict) else row[13],
            "raw": raw,
        }
        out[entry["steam_id"]] = entry
    return out


def load_friend_snapshots(steam_ids: List[str]) -> Dict[str, dict]:
    if not steam_ids:
        return {}
    placeholders = ",".join("?" for _ in steam_ids)
    rows = db.query_all(
        f"""
        SELECT steam_id,
               relationship,
               persona_state,
               persona_name,
               game_app_id,
               game_name,
               last_logoff,
               last_logon,
               persona_flags,
               avatar_hash,
               persona_json,
               rich_presence_json,
               updated_at
        FROM steam_friend_snapshots
        WHERE steam_id IN ({placeholders})
        """,
        tuple(steam_ids),
    )
    snapshots: Dict[str, dict] = {}
    for row in rows:
        persona_raw_json = row["persona_json"] if isinstance(row, dict) else row[10]
        presence_raw_json = row["rich_presence_json"] if isinstance(row, dict) else row[11]
        try:
            persona_raw = json.loads(persona_raw_json) if persona_raw_json else None
        except json.JSONDecodeError:
            persona_raw = None
        try:
            presence_raw = json.loads(presence_raw_json) if presence_raw_json else None
        except json.JSONDecodeError:
            presence_raw = None
        steam_id = str(row["steam_id"] if isinstance(row, dict) else row[0])
        snapshots[steam_id] = {
            "steam_id": steam_id,
            "relationship": row["relationship"] if isinstance(row, dict) else row[1],
            "persona_state": row["persona_state"] if isinstance(row, dict) else row[2],
            "persona_name": row["persona_name"] if isinstance(row, dict) else row[3],
            "game_app_id": row["game_app_id"] if isinstance(row, dict) else row[4],
            "game_name": row["game_name"] if isinstance(row, dict) else row[5],
            "last_logoff": row["last_logoff"] if isinstance(row, dict) else row[6],
            "last_logon": row["last_logon"] if isinstance(row, dict) else row[7],
            "persona_flags": row["persona_flags"] if isinstance(row, dict) else row[8],
            "avatar_hash": row["avatar_hash"] if isinstance(row, dict) else row[9],
            "persona_raw": persona_raw,
            "rich_presence_raw": presence_raw,
            "updated_at": row["updated_at"] if isinstance(row, dict) else row[12],
        }
    return snapshots


__all__ = [
    "STEAM_API",
    "batch_get_summaries",
    "cache_player_state",
    "eval_live_state",
    "load_friend_snapshots",
    "load_rich_presence",
]
