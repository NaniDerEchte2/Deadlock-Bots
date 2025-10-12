# shared/steam.py
from typing import Dict, List
import json
import aiohttp
from service import db

STEAM_API = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"

async def batch_get_summaries(session: aiohttp.ClientSession, api_key: str, steam_ids: List[str]) -> Dict[str, dict]:
    if not steam_ids:
        return {}
    # Steam erlaubt bis 100 IDs pro Call
    out: Dict[str, dict] = {}
    for i in range(0, len(steam_ids), 100):
        ids = ",".join(steam_ids[i:i+100])
        async with session.get(STEAM_API, params={"key": api_key, "steamids": ids}, timeout=15) as r:
            if r.status != 200:
                continue
            data = await r.json()
            for p in data.get("response", {}).get("players", []):
                out[str(p.get("steamid"))] = p
    return out

def eval_live_state(summary: dict, deadlock_app_id: str) -> dict:
    gameid = str(summary.get("gameid", "")) if summary.get("gameid") is not None else ""
    lobby = summary.get("lobbysteamid")
    server = summary.get("gameserversteamid")
    in_deadlock_now = (gameid == str(deadlock_app_id))
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
        INSERT INTO live_player_state(steam_id,last_gameid,last_server_id,last_seen_ts,in_deadlock_now,in_match_now_strict)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(steam_id) DO UPDATE SET
          last_gameid=excluded.last_gameid,
          last_server_id=excluded.last_server_id,
          last_seen_ts=excluded.last_seen_ts,
          in_deadlock_now=excluded.in_deadlock_now,
          in_match_now_strict=excluded.in_match_now_strict
        """,
        (
            row["steam_id"], row["last_gameid"], row["last_server_id"],
            row.get("ts") or __import__("time").time(),
            row["in_deadlock_now"], row["in_match_now_strict"]
        ),
    )


def load_rich_presence(steam_ids: List[str]) -> Dict[str, dict]:
    """Lädt die zuletzt gespeicherten Rich-Presence-Daten für die angegebenen Steam-IDs."""
    if not steam_ids:
        return {}
    placeholders = ",".join("?" for _ in steam_ids)
    rows = db.query_all(
        f"""
        SELECT steam_id, app_id, status, status_text, display, player_group, player_group_size,
               connect, mode, map, party_size, raw_json, last_update, updated_at
        FROM steam_rich_presence
        WHERE steam_id IN ({placeholders})
        """,
        tuple(steam_ids),
    )
    out: Dict[str, dict] = {}
    for r in rows:
        raw_json = r["raw_json"] if isinstance(r, dict) else r[7]
        try:
            raw = json.loads(raw_json) if raw_json else {}
        except json.JSONDecodeError:
            raw = {}
        entry = {
            "steam_id": str(r["steam_id"] if isinstance(r, dict) else r[0]),
            "app_id": r["app_id"] if isinstance(r, dict) else r[1],
            "status": r["status"] if isinstance(r, dict) else r[2],
            "status_text": r["status_text"] if isinstance(r, dict) else r[3],
            "display": r["display"] if isinstance(r, dict) else r[4],
            "player_group": r["player_group"] if isinstance(r, dict) else r[5],
            "player_group_size": r["player_group_size"] if isinstance(r, dict) else r[6],
            "connect": r["connect"] if isinstance(r, dict) else r[7],
            "mode": r["mode"] if isinstance(r, dict) else r[8],
            "map": r["map"] if isinstance(r, dict) else r[9],
            "party_size": r["party_size"] if isinstance(r, dict) else r[10],
            "last_update": r["last_update"] if isinstance(r, dict) else r[12],
            "updated_at": r["updated_at"] if isinstance(r, dict) else r[13],
            "raw": raw,
        }
        out[entry["steam_id"]] = entry
    return out


def load_friend_snapshots(steam_ids: List[str]) -> Dict[str, dict]:
    """Lädt die letzten Steam-Freundes-Snapshots inkl. Persona- und Rich-Presence-Rohdaten."""
    if not steam_ids:
        return {}
    placeholders = ",".join("?" for _ in steam_ids)
    rows = db.query_all(
        f"""
        SELECT steam_id, relationship, persona_state, persona_name, game_app_id, game_name,
               last_logoff, last_logon, persona_flags, avatar_hash, persona_json, rich_presence_json, updated_at
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
