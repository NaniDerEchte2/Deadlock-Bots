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

def get_env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    if v is None:
        return default
    return v.strip()

def fetch_player_summary(steam_api_key: str, steam_id: str, timeout: float = 10.0) -> dict:
    params = {
        "key": steam_api_key,
        "steamids": steam_id,
    }
    r = requests.get(STEAM_API, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    players = data.get("response", {}).get("players", [])
    return players[0] if players else {}

def decide_match_state(p: dict, deadlock_app_id: str) -> dict:
    # Fields of interest
    gameid = str(p.get("gameid", "")) if p.get("gameid") is not None else ""
    gameextrainfo = p.get("gameextrainfo")
    lobby = p.get("lobbysteamid")
    server = p.get("gameserversteamid")
    visibility = p.get("communityvisibilitystate")  # 3=public, 1=private
    profile_state = p.get("profilestate")          # 1=setup complete

    in_deadlock_now = (gameid == str(deadlock_app_id))
    # "Streng": wir werten "im Match" nur dann als True,
    # wenn zusätzlich Lobby ODER Server-ID gesetzt ist.
    in_match_now_strict = in_deadlock_now and (bool(lobby) or bool(server))

    return {
        "steam_id": p.get("steamid"),
        "personaname": p.get("personaname"),
        "communityvisibilitystate": visibility,
        "profilestate": profile_state,
        "in_deadlock_now": in_deadlock_now,
        "in_match_now_strict": in_match_now_strict,
        "gameid": gameid or None,
        "gameextrainfo": gameextrainfo,
        "lobbysteamid": lobby,
        "gameserversteamid": server,
        "ts": int(time.time()),
    }

def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Check: Is player in a Deadlock match (now)?")
    parser.add_argument("--steam-id", dest="steam_id", help="SteamID64 (17-stellig). Falls leer, wird .env STEAM_ID genutzt.")
    parser.add_argument("--app-id", dest="app_id", default=get_env("DEADLOCK_APP_ID", "1422450"),
                        help="Deadlock AppID (default 1422450).")
    parser.add_argument("--pretty", action="store_true", help="Hübsch formatiertes JSON ausgeben.")
    args = parser.parse_args()

    steam_api_key = get_env("STEAM_API_KEY")
    if not steam_api_key:
        print("ERROR: STEAM_API_KEY fehlt (in .env setzen).", file=sys.stderr)
        sys.exit(2)

    steam_id = args.steam_id or get_env("STEAM_ID")
    if not steam_id:
        print("ERROR: SteamID fehlt (Argument --steam-id oder .env STEAM_ID).", file=sys.stderr)
        sys.exit(2)

    try:
        p = fetch_player_summary(steam_api_key, steam_id)
        if not p:
            print("WARN: Kein Player gefunden. Ist die SteamID korrekt/öffentlich?")
            sys.exit(3)

        result = decide_match_state(p, args.app_id)

        # Kurzer Klartext + JSON
        status_txt = "✅ JA (streng)" if result["in_match_now_strict"] else ("🟡 Im Spiel, aber nicht eindeutig im Match" if result["in_deadlock_now"] else "❌ NEIN")
        print(f"[{steam_id}] Deadlock-Match jetzt? {status_txt}")
        if args.pretty:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(result, ensure_ascii=False))
        # Exit-Code: 0 = erreichbar, 1 = nicht im Match, 0 bleibt sinnvoll
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
