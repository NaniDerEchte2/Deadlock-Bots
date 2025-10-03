#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import json
import time
import argparse
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# ------------------------------------------------------------
# Konfiguration
# ------------------------------------------------------------

# .env optional laden (für STEAM_API_KEY o.ä.)
DOTENV_PATH = Path(r"C:\Users\Nani-Admin\Documents\.env")
if DOTENV_PATH.exists():
    load_dotenv(dotenv_path=DOTENV_PATH, override=True)

STEAM_API_KEY_ENV = "STEAM_API_KEY"

# Fallback (ohne ENV): hier kannst du Komma-getrennt hardcoden
# Beispiel: HARDCODED_IDS = "76561198000000000,myvanity,https://steamcommunity.com/id/foo"
HARDCODED_IDS = "76561198375566764,76561198055737030"

DEADLOCK_APP_ID_DEFAULT = "1422450"
CHUNK = 100  # Steam API erlaubt bis 100 IDs pro Call

ENDPOINTS = {
    "summaries": "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
    "vanity":    "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/",
    "bans":      "https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/",
    "level":     "https://api.steampowered.com/IPlayerService/GetSteamLevel/v1/",
    "stats":     "https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v0002/",
    "srv_by_sid":"https://api.steampowered.com/IGameServersService/GetServerIPsBySteamID/v1/",
}

TEAM_SIZE = 6  # Deadlock Teams

# ------------------------------------------------------------
# Utilities
# ------------------------------------------------------------

def _is_allowed_steamcommunity_host(host: Optional[str]) -> bool:
    if not host:
        return False
    h = host.lower().rstrip(".")
    return h == "steamcommunity.com" or h.endswith(".steamcommunity.com")

def _parse_steam_input(raw: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Liefert (original, type, value):
      ("id64", <17digits>)             -> SteamID64
      ("profiles_url", <17digits>)     -> aus /profiles/<id>
      ("vanity_url", <vanity>)         -> aus /id/<vanity>
      ("vanity", <vanity>)             -> nackter Vanity-String
      (None, None)                     -> unbrauchbar
    """
    s = (raw or "").strip()
    if not s:
        return raw, None, None

    if re.fullmatch(r"\d{17}", s):
        return raw, "id64", s

    u = None
    try:
        u = urlparse(s)
    except Exception:
        pass

    if u and _is_allowed_steamcommunity_host(getattr(u, "hostname", None)):
        path = (u.path or "").rstrip("/")
        m = re.search(r"/profiles/(\d{17})$", path)
        if m:
            return raw, "profiles_url", m.group(1)
        m = re.search(r"/id/([^/]+)$", path)
        if m:
            return raw, "vanity_url", m.group(1)

    if re.fullmatch(r"[A-Za-z0-9_.\-]+", s):
        return raw, "vanity", s

    return raw, None, None

def _resolve_vanity(steam_api_key: str, vanity: str, timeout: float = 10.0) -> Optional[str]:
    params = {"key": steam_api_key, "vanityurl": vanity}
    r = requests.get(ENDPOINTS["vanity"], params=params, timeout=timeout)
    r.raise_for_status()
    resp = (r.json() or {}).get("response", {})
    if resp.get("success") == 1:
        sid = str(resp.get("steamid", "")).strip()
        if re.fullmatch(r"\d{17}", sid):
            return sid
    return None

def resolve_inputs_to_id64s(steam_api_key: str, inputs: List[str]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for raw in inputs:
        original, typ, val = _parse_steam_input(raw)
        if typ in ("id64", "profiles_url") and val:
            out.append((original, val)); continue
        if typ in ("vanity_url", "vanity") and val:
            try:
                sid = _resolve_vanity(steam_api_key, val)
            except requests.RequestException as e:
                print(f"[resolve] Fehler für '{original}': {e}", file=sys.stderr)
                sid = None
            if sid:
                out.append((original, sid))
            else:
                print(f"[resolve] Konnte Vanity/Link nicht auflösen: {original}", file=sys.stderr)
            continue
        print(f"[resolve] Ungültiger Input ignoriert: {original}", file=sys.stderr)

    # dedup
    seen = set()
    dedup: List[Tuple[str, str]] = []
    for orig, sid in out:
        if sid in seen: continue
        seen.add(sid); dedup.append((orig, sid))
    return dedup

def chunked(seq: List[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i+size]

# ------------------------------------------------------------
# Steam calls
# ------------------------------------------------------------

def fetch_player_summaries(steam_api_key: str, steam_ids: List[str], timeout: float = 10.0) -> Dict[str, dict]:
    by_id: Dict[str, dict] = {}
    for chunk in chunked(steam_ids, CHUNK):
        params = {"key": steam_api_key, "steamids": ",".join(chunk)}
        r = requests.get(ENDPOINTS["summaries"], params=params, timeout=timeout)
        r.raise_for_status()
        players = (r.json() or {}).get("response", {}).get("players", [])
        for p in players:
            sid = str(p.get("steamid", "")).strip()
            if sid:
                by_id[sid] = p
    return by_id

def fetch_player_bans(steam_api_key: str, steam_ids: List[str], timeout: float = 10.0) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for chunk in chunked(steam_ids, CHUNK):
        params = {"key": steam_api_key, "steamids": ",".join(chunk)}
        r = requests.get(ENDPOINTS["bans"], params=params, timeout=timeout)
        r.raise_for_status()
        for b in (r.json() or {}).get("players", []):
            sid = str(b.get("SteamId", "")).strip()
            if sid:
                out[sid] = b
    return out

def fetch_steam_levels(steam_api_key: str, steam_ids: List[str], timeout: float = 10.0) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for sid in steam_ids:
        params = {"key": steam_api_key, "steamid": sid}
        try:
            r = requests.get(ENDPOINTS["level"], params=params, timeout=timeout)
            r.raise_for_status()
            lvl = (r.json() or {}).get("response", {}).get("player_level")
            if isinstance(lvl, int):
                out[sid] = lvl
        except requests.RequestException:
            pass
    return out

def fetch_user_stats_for_game(steam_api_key: str, steam_id: str, app_id: str, timeout: float = 10.0) -> Optional[dict]:
    params = {"key": steam_api_key, "steamid": steam_id, "appid": app_id}
    try:
        r = requests.get(ENDPOINTS["stats"], params=params, timeout=timeout)
        # Viele Spiele geben 403/404, also robust behandeln
        if r.status_code != 200:
            return None
        data = r.json() or {}
        return data.get("playerstats")
    except requests.RequestException:
        return None

def fetch_server_ips_by_steamids(steam_api_key: str, server_ids: List[str], timeout: float = 10.0) -> Dict[str, List[str]]:
    """
    Mappt gameserversteamid -> ["ip:port", ...]
    """
    if not server_ids:
        return {}
    out: Dict[str, List[str]] = {}
    for chunk in chunked(server_ids, CHUNK):
        params = {"key": steam_api_key, "server_steamids": ",".join(chunk)}
        try:
            r = requests.get(ENDPOINTS["srv_by_sid"], params=params, timeout=timeout)
            r.raise_for_status()
            servers = (r.json() or {}).get("response", {}).get("servers", [])
            for s in servers:
                sid = str(s.get("steamid", "")).strip()
                addr = s.get("addr")
                if sid:
                    out.setdefault(sid, [])
                    if addr and addr not in out[sid]:
                        out[sid].append(addr)
        except requests.RequestException as e:
            print(f"[server_ips] Fehler: {e}", file=sys.stderr)
    return out

# ------------------------------------------------------------
# Analyse & Darstellung
# ------------------------------------------------------------

KNOWN_SUMMARY_KEYS = {
    "steamid","communityvisibilitystate","profilestate","personaname","profileurl",
    "avatar","avatarmedium","avatarfull","avatarhash","lastlogoff","personastate",
    "commentpermission","realname","primaryclanid","timecreated","gameid",
    "gameserverip","gameextrainfo","cityid","loccountrycode","locstatecode","loccityid",
    # in der Praxis häufig vorhanden, aber nicht offiziell garantiert:
    "gameserversteamid","lobbysteamid"
}

def decide_match_state(p: dict, deadlock_app_id: str) -> dict:
    gid   = str(p.get("gameid", "") or "")
    ginfo = str(p.get("gameextrainfo", "") or "")
    lobby = p.get("lobbysteamid")
    server= p.get("gameserversteamid")
    in_deadlock = (gid == str(deadlock_app_id)) or (ginfo.lower() == "deadlock")
    return {
        "steam_id": p.get("steamid"),
        "personaname": p.get("personaname"),
        "in_deadlock_now": bool(in_deadlock),
        "in_lobby": bool(in_deadlock and lobby and not server),
        "in_match": bool(in_deadlock and server),
        "lobbysteamid": lobby,
        "gameserversteamid": server,
        "gameserverip": p.get("gameserverip"),
        "gameid": gid or None,
        "gameextrainfo": ginfo or None,
        "ts": int(time.time()),
    }

def analyze_group(results: List[dict]) -> dict:
    lobby_groups: Dict[str, List[dict]] = {}
    match_groups: Dict[str, List[dict]] = {}
    for r in results:
        if r["in_lobby"] and r["lobbysteamid"]:
            lobby_groups.setdefault(r["lobbysteamid"], []).append(r)
        if r["in_match"] and r["gameserversteamid"]:
            match_groups.setdefault(r["gameserversteamid"], []).append(r)
    return {"lobby_groups": lobby_groups, "match_groups": match_groups}

def pretty_status(r: dict) -> str:
    if r["in_match"]: return "✅ Im Match"
    if r["in_lobby"]: return "🟡 In Lobby"
    if r["in_deadlock_now"]: return "⚪ Spiel offen"
    return "❌ Nicht im Spiel"

# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------

def parse_id_args(raw: Optional[str], list_args: List[str]) -> List[str]:
    """
    Erlaubt:
      --ids "id1,id2,https://.../id/foo"
      --ids id1,id2  (ohne Anführungszeichen)
      --steam-ids id1 id2 id3 (space-getrennt)
      HARDCODED_IDS (Komma-getrennt)
    """
    toks: List[str] = []
    def add_chunk(s: str):
        for part in re.split(r"[,\s]+", s.strip()):
            if part:
                toks.append(part)
    if raw:
        add_chunk(raw)
    if list_args:
        for s in list_args:
            add_chunk(s)
    if not toks and HARDCODED_IDS:
        add_chunk(HARDCODED_IDS)
    return toks

def main():
    ap = argparse.ArgumentParser(description="Deadlock Steam Probe – dumpt alle verfügbaren Details für gegebene Accounts.")
    ap.add_argument("--key", default=os.getenv(STEAM_API_KEY_ENV, ""), help="Steam Web API Key (oder via ENV STEAM_API_KEY).")
    ap.add_argument("--ids", default="", help="Komma/Leerzeichen-getrennte Liste aus IDs/Vanities/Profil-Links.")
    ap.add_argument("--steam-ids", nargs="*", help="Alternative: space-getrennte Liste.")
    ap.add_argument("--app-id", default=os.getenv("DEADLOCK_APP_ID", DEADLOCK_APP_ID_DEFAULT), help="Deadlock AppID (default 1422450).")
    ap.add_argument("--all", action="store_true", help="Zusätzliche Endpoints probieren: Bans, Level, UserStatsForGame.")
    ap.add_argument("--server-ips", action="store_true", help="Server-IPs für gefundene gameserversteamid nachschlagen.")
    ap.add_argument("--dump-unknown", action="store_true", help="Zeige unbekannte Summary-Felder (Key-Diff).")
    ap.add_argument("--raw", action="store_true", help="Roh-JSON der Summaries ausgeben.")
    ap.add_argument("--pretty", action="store_true", help="Formatiertes JSON am Ende ausgeben.")
    args = ap.parse_args()

    key = (args.key or "").strip()
    if not key:
        print("ERROR: Steam API Key fehlt (Flag --key oder ENV STEAM_API_KEY).", file=sys.stderr)
        sys.exit(2)

    raw_inputs = parse_id_args(args.ids, args.steam_ids or [])
    if not raw_inputs:
        print("ERROR: Keine Eingaben (nutze --ids oder --steam-ids oder HARDCODED_IDS am Datei-Header).", file=sys.stderr)
        sys.exit(2)

    try:
        # 1) IDs normalisieren
        resolved = resolve_inputs_to_id64s(key, raw_inputs)
        if not resolved:
            print("ERROR: Keine gültigen SteamIDs nach Auflösung.", file=sys.stderr)
            sys.exit(2)
        input_map = {orig: sid for (orig, sid) in resolved}
        id_list = [sid for (_, sid) in resolved]

        # 2) Summaries
        summaries = fetch_player_summaries(key, id_list)
        results: List[dict] = []
        unknown_keys: Dict[str, List[str]] = {}

        for orig, sid in resolved:
            p = summaries.get(sid)
            if not p:
                print(f"[{orig} → {sid}] ⚠️ Keine öffentlichen Daten (privat/offline?).")
                continue
            r = decide_match_state(p, args.app_id)
            r["_input"] = orig
            results.append(r)

            if args.dump_unknown:
                unk = sorted(set(p.keys()) - KNOWN_SUMMARY_KEYS)
                if unk:
                    unknown_keys[sid] = unk

        # 3) Extra Endpoints
        bans = levels = {}
        stats: Dict[str, Any] = {}
        if args.all:
            bans = fetch_player_bans(key, id_list)
            levels = fetch_steam_levels(key, id_list)
            # Warnung: stats sind oft nicht verfügbar; robust probieren
            for sid in id_list:
                ps = fetch_user_stats_for_game(key, sid, args.app_id)
                if ps:
                    stats[sid] = ps

        # 4) Gruppierung + optional Server-IP Mapping
        groups = analyze_group(results)
        server_ip_map: Dict[str, List[str]] = {}
        if args.server_ips:
            server_ids = list(groups.get("match_groups", {}).keys())
            server_ip_map = fetch_server_ips_by_steamids(key, server_ids)

        # 5) Konsolenausgabe
        for r in results:
            print(f"[{r['_input']} → {r['steam_id']}] {r['personaname']}: {pretty_status(r)}")
            if r.get("gameserversteamid"):
                print(f"   server_id: {r['gameserversteamid']}  server_ip: {r.get('gameserverip')}")
            if r.get("lobbysteamid"):
                print(f"   lobby_id:  {r['lobbysteamid']}")

        if groups["lobby_groups"]:
            for lobby_id, members in groups["lobby_groups"].items():
                print(f"🎮 Lobby {lobby_id}: {len(members)}/{TEAM_SIZE} Spieler")
        if groups["match_groups"]:
            for server_id, members in groups["match_groups"].items():
                extra = ""
                if server_ip_map.get(server_id):
                    extra = f"  ({', '.join(server_ip_map[server_id])})"
                print(f"🔥 Match {server_id}: {len(members)} Spieler{extra}")

        if args.dump_unknown and unknown_keys:
            print("\n🔎 Unbekannte/undokumentierte Summary-Felder (Key-Diff):")
            for sid, ks in unknown_keys.items():
                print(f" - {sid}: {', '.join(ks)}")

        if args.raw:
            print("\n--- RAW SUMMARIES ---")
            print(json.dumps(summaries, ensure_ascii=False, indent=2))

        # 6) JSON Payload
        payload = {
            "input_map": input_map,
            "results": results,
            "groups": groups,
            "server_ips": server_ip_map,
            "extras": {
                "bans": bans,
                "levels": levels,
                "stats": stats
            }
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
