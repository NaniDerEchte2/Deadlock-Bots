import os
import sys
import json
import time
import argparse
import requests
from dotenv import load_dotenv
from pathlib import Path

# .env explizit laden
DOTENV_PATH = Path(r"C:\Users\Nani-Admin\Documents\.env")
load_dotenv(dotenv_path=DOTENV_PATH, override=True)

STEAM_API = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
DEADLOCK_APP_ID = "1422450"
TEAM_SIZE = 6  # Deadlock 6er Teams

def get_env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    if v is None:
        return default
    return v.strip()

def fetch_player_summaries(steam_api_key: str, steam_ids: list[str], timeout: float = 10.0) -> list[dict]:
    params = {
        "key": steam_api_key,
        "steamids": ",".join(steam_ids),
    }
    r = requests.get(STEAM_API, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data.get("response", {}).get("players", [])

def decide_match_state(p: dict, deadlock_app_id: str) -> dict:
    gameid = str(p.get("gameid", "")) if p.get("gameid") is not None else ""
    lobby = p.get("lobbysteamid")
    server = p.get("gameserversteamid")

    in_deadlock_now = (gameid == str(deadlock_app_id))
    in_lobby = in_deadlock_now and lobby and not server
    in_match = in_deadlock_now and bool(server)

    return {
        "steam_id": p.get("steamid"),
        "personaname": p.get("personaname"),
        "in_deadlock_now": in_deadlock_now,
        "in_lobby": in_lobby,
        "in_match": in_match,
        "lobbysteamid": lobby,
        "gameserversteamid": server,
        "ts": int(time.time()),
    }

def analyze_group(results: list[dict]) -> dict:
    """ Analysiert ob Spieler in gleicher Lobby oder Match sind """
    lobby_groups = {}
    match_groups = {}

    for r in results:
        if r["in_lobby"]:
            lobby_groups.setdefault(r["lobbysteamid"], []).append(r)
        if r["in_match"]:
            match_groups.setdefault(r["gameserversteamid"], []).append(r)

    return {
        "lobby_groups": lobby_groups,
        "match_groups": match_groups,
    }

def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Check group Deadlock status")
    parser.add_argument("--steam-ids", dest="steam_ids", nargs="+",
                        help="SteamID64 Liste (17-stellig). Falls leer, wird .env STEAM_ID genutzt.")
    parser.add_argument("--app-id", dest="app_id", default=get_env("DEADLOCK_APP_ID", DEADLOCK_APP_ID),
                        help="Deadlock AppID (default 1422450).")
    parser.add_argument("--pretty", action="store_true", help="Hübsch formatiertes JSON ausgeben.")
    args = parser.parse_args()

    steam_api_key = get_env("STEAM_API_KEY")
    if not steam_api_key:
        print("ERROR: STEAM_API_KEY fehlt (in .env setzen).", file=sys.stderr)
        sys.exit(2)

    steam_ids = args.steam_ids or [get_env("STEAM_ID")]
    if not steam_ids or not steam_ids[0]:
        print("ERROR: SteamIDs fehlen (Argument --steam-ids oder .env STEAM_ID).", file=sys.stderr)
        sys.exit(2)

    try:
        players = fetch_player_summaries(steam_api_key, steam_ids)
        results = [decide_match_state(p, args.app_id) for p in players]
        group_info = analyze_group(results)

        # Konsolen-Ausgabe
        for r in results:
            if r["in_match"]:
                status = "✅ Im Match"
            elif r["in_lobby"]:
                status = "🟡 In Lobby"
            elif r["in_deadlock_now"]:
                status = "⚪ Spiel offen, aber idle"
            else:
                status = "❌ Nicht im Spiel"
            print(f"[{r['steam_id']}] {r['personaname']}: {status}")

        # Gruppenauswertung für Voice-Channel
        for lobby_id, members in group_info["lobby_groups"].items():
            print(f"🎮 Lobby {lobby_id}: {len(members)}/{TEAM_SIZE} Spieler ({TEAM_SIZE - len(members)} frei)")
        for server_id, members in group_info["match_groups"].items():
            print(f"🔥 Match {server_id}: {len(members)} Spieler im gleichen Match")

        if args.pretty:
            print(json.dumps({"results": results, "groups": group_info}, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"results": results, "groups": group_info}, ensure_ascii=False))

        sys.exit(0)

    except requests.HTTPError as e:
        print(f"HTTP ERROR: {e} — Body: {getattr(e.response, 'text', '')}", file=sys.stderr)
        sys.exit(5)
    except requests.RequestException as e:
        print(f"NETZWERKFEHLER: {e}", file=sys.stderr)
        sys.exit(6)
    except Exception as e:
        print(f"UNBEKANNTER FEHLER: {e}", file=sys.stderr)
        sys.exit(7)

if __name__ == "__main__":
    main()
