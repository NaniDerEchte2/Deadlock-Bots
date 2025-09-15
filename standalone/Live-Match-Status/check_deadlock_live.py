import os
import sys
import re
import json
import time
import argparse
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv
from pathlib import Path
from typing import List, Tuple, Optional

# .env explizit laden (traditionell ordentlich)
DOTENV_PATH = Path(r"C:\Users\Nani-Admin\Documents\.env")
load_dotenv(dotenv_path=DOTENV_PATH, override=True)

STEAM_API_SUMMARIES = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
STEAM_API_VANITY    = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/"

DEADLOCK_APP_ID = "1422450"
TEAM_SIZE = 6  # Deadlock 6er Teams
CHUNK = 100    # Steam API erlaubt bis zu 100 IDs pro Call

def get_env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    if v is None:
        return default
    return v.strip()

# ---------- Host-Check (gehärtet) --------------------------------------------
def _is_allowed_steamcommunity_host(host: Optional[str]) -> bool:
    """
    Erlaubt exakt steamcommunity.com oder gültige Subdomains davon.
    Nutzt den geparsten Hostnamen (u.hostname), nicht netloc/String-Contains.
    """
    if not host:
        return False
    h = host.lower().rstrip(".")
    return h == "steamcommunity.com" or h.endswith(".steamcommunity.com")

# ---------- Vanity/Link -> SteamID64 -----------------------------------------
def _resolve_vanity(steam_api_key: str, vanity: str, timeout: float = 10.0) -> Optional[str]:
    params = {"key": steam_api_key, "vanityurl": vanity}
    r = requests.get(STEAM_API_VANITY, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json() or {}
    resp = data.get("response", {})
    if resp.get("success") == 1:
        sid = str(resp.get("steamid", "")).strip()
        if re.fullmatch(r"\d{17}", sid):
            return sid
    return None

def _parse_steam_input(raw: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Liefert (original, type, value):
      type/value:
        ("id64", <17-digit>)          -> fertige SteamID64
        ("profiles_url", <17-digit>)  -> aus /profiles/<id>
        ("vanity_url", <vanity>)      -> aus /id/<vanity>
        ("vanity", <vanity>)          -> nackter Vanity-String
        (None, None)                  -> unbrauchbar
    """
    s = (raw or "").strip()
    if not s:
        return raw, None, None

    # 1) pure SteamID64
    if re.fullmatch(r"\d{17}", s):
        return raw, "id64", s

    # 2) URL?
    try:
        u = urlparse(s)
    except Exception:
        u = None

    # Gehärteter Host-/Scheme-Check
    if u and _is_allowed_steamcommunity_host(getattr(u, "hostname", None)):
        path = (u.path or "").rstrip("/")
        m = re.search(r"/profiles/(\d{17})$", path)
        if m:
            return raw, "profiles_url", m.group(1)
        m = re.search(r"/id/([^/]+)$", path)
        if m:
            return raw, "vanity_url", m.group(1)

    # 3) nackter Vanity-Kandidat (konservativ)
    if re.fullmatch(r"[A-Za-z0-9_.\-]+", s):
        return raw, "vanity", s

    return raw, None, None

def resolve_inputs_to_id64s(steam_api_key: str, inputs: List[str]) -> List[Tuple[str, str]]:
    """
    Nimmt beliebige Inputs (ID/Vanity/URL) und gibt Liste (original, id64) zurück.
    Skipped ungültige Einträge mit Warnung auf STDERR.
    """
    out: List[Tuple[str, str]] = []
    for raw in inputs:
        original, typ, val = _parse_steam_input(raw)
        if typ in ("id64", "profiles_url") and val:
            out.append((original, val))
            continue
        if typ in ("vanity_url", "vanity") and val:
            try:
                sid = _resolve_vanity(steam_api_key, val)
            except requests.HTTPError as e:
                print(f"[resolve] HTTP ERROR für '{original}': {e}", file=sys.stderr)
                sid = None
            except requests.RequestException as e:
                print(f"[resolve] NETZWERKFEHLER für '{original}': {e}", file=sys.stderr)
                sid = None
            if sid:
                out.append((original, sid))
            else:
                print(f"[resolve] Konnte Vanity/Link nicht auflösen: {original}", file=sys.stderr)
            continue
        print(f"[resolve] Ungültiger Input ignoriert: {original}", file=sys.stderr)
    # Duplikate entfernen, Ordnung bewahren (traditionell sauber)
    seen = set()
    dedup: List[Tuple[str, str]] = []
    for orig, sid in out:
        if sid in seen:
            continue
        seen.add(sid)
        dedup.append((orig, sid))
    return dedup

# ---------- Player Summaries --------------------------------------------------
def fetch_player_summaries(steam_api_key: str, steam_ids: List[str], timeout: float = 10.0) -> List[dict]:
    players: List[dict] = []
    for i in range(0, len(steam_ids), CHUNK):
        chunk = steam_ids[i:i+CHUNK]
        params = {"key": steam_api_key, "steamids": ",".join(chunk)}
        r = requests.get(STEAM_API_SUMMARIES, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json() or {}
        players.extend(data.get("response", {}).get("players", []))
    return players

def decide_match_state(p: dict, deadlock_app_id: str) -> dict:
    gameid = str(p.get("gameid", "")) if p.get("gameid") is not None else ""
    lobby = p.get("lobbysteamid")
    server = p.get("gameserversteamid")

    in_deadlock_now = (gameid == str(deadlock_app_id)) or (str(p.get("gameextrainfo", "")).lower() == "deadlock")
    in_lobby = bool(in_deadlock_now and lobby and not server)
    in_match = bool(in_deadlock_now and server)  # inkl. Queue/Pre-Server: Steam liefert schon serversteamid

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

def analyze_group(results: List[dict]) -> dict:
    lobby_groups = {}
    match_groups = {}
    for r in results:
        if r["in_lobby"]:
            lobby_groups.setdefault(r["lobbysteamid"], []).append(r)
        if r["in_match"]:
            match_groups.setdefault(r["gameserversteamid"], []).append(r)
    return {"lobby_groups": lobby_groups, "match_groups": match_groups}

# ---------- CLI ---------------------------------------------------------------
def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Check group Deadlock status (IDs, Vanity, URLs werden akzeptiert)")
    parser.add_argument(
        "--steam-ids",
        dest="steam_ids",
        nargs="+",
        help="Liste aus SteamID64, vanity oder Profil-Links. Falls leer, wird .env STEAM_ID genutzt."
    )
    parser.add_argument(
        "--app-id",
        dest="app_id",
        default=get_env("DEADLOCK_APP_ID", DEADLOCK_APP_ID),
        help="Deadlock AppID (default 1422450)."
    )
    parser.add_argument("--pretty", action="store_true", help="Hübsch formatiertes JSON ausgeben.")
    args = parser.parse_args()

    steam_api_key = get_env("STEAM_API_KEY")
    if not steam_api_key:
        print("ERROR: STEAM_API_KEY fehlt (in .env setzen).", file=sys.stderr)
        sys.exit(2)

    raw_inputs = args.steam_ids or [get_env("STEAM_ID")]
    if not raw_inputs or not raw_inputs[0]:
        print("ERROR: Steam Eingaben fehlen (Argument --steam-ids oder .env STEAM_ID).", file=sys.stderr)
        sys.exit(2)

    try:
        # 1) Eingaben robust zu SteamID64 normalisieren
        resolved = resolve_inputs_to_id64s(steam_api_key, raw_inputs)
        if not resolved:
            print("ERROR: Keine gültigen SteamIDs nach Auflösung.", file=sys.stderr)
            sys.exit(2)

        # Mapping für die Ausgabe: original -> id64
        inputs_map = {orig: sid for (orig, sid) in resolved}
        id_list = [sid for (_, sid) in resolved]

        # 2) Steam Player Summaries holen
        players = fetch_player_summaries(steam_api_key, id_list)
        # Index nach ID
        by_id = {p.get("steamid"): p for p in players}

        # 3) Ergebnisse bauen (in Original-Reihenfolge ausgeben)
        results: List[dict] = []
        for orig, sid in resolved:
            p = by_id.get(sid)
            if not p:
                # kein öffentliches Profil / nicht gefunden
                print(f"[{orig} → {sid}] ⚠️ Keine öffentlichen Daten auffindbar (privat/offline?).")
                continue
            r = decide_match_state(p, args.app_id)
            r["_input"] = orig  # zur Nachvollziehbarkeit
            results.append(r)

        group_info = analyze_group(results)

        # 4) Konsolen-Ausgabe
        for r in results:
            if r["in_match"]:
                status = "✅ Im Match"
            elif r["in_lobby"]:
                status = "🟡 In Lobby"
            elif r["in_deadlock_now"]:
                status = "⚪ Spiel offen, aber idle"
            else:
                status = "❌ Nicht im Spiel"
            print(f"[{r['_input']} → {r['steam_id']}] {r['personaname']}: {status}")

        # Gruppenauswertung (Stack-Größe)
        for lobby_id, members in group_info["lobby_groups"].items():
            print(f"🎮 Lobby {lobby_id}: {len(members)}/{TEAM_SIZE} Spieler ({TEAM_SIZE - len(members)} frei)")
        for server_id, members in group_info["match_groups"].items():
            print(f"🔥 Match {server_id}: {len(members)} Spieler im gleichen Match")

        # 5) JSON
        payload = {
            "input_map": inputs_map,
            "results": results,
            "groups": group_info
        }
        if args.pretty:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(payload, ensure_ascii=False))

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
