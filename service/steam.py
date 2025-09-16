# shared/steam.py
import os
from typing import Dict, List
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
