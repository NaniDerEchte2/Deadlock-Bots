# migrate_steam_links_to_id64.py
import os
import re
import sys
import time
import json
import sqlite3
import argparse
import requests
from urllib.parse import urlparse
from typing import Optional, Tuple, List, Dict

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # Falls python-dotenv nicht installiert ist

STEAM_API_VANITY = "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/"

# ======================= ENV-Loading =======================
def try_load_env(paths: List[str]) -> List[str]:
    loaded = []
    if load_dotenv is None:
        return loaded
    for p in paths:
        if p and os.path.exists(p):
            load_dotenv(p, override=True)
            loaded.append(p)
    return loaded

def find_default_env_paths() -> List[str]:
    paths = []
    # 1) dein Standard
    paths.append(r"C:\Users\Nani-Admin\Documents\.env")
    # 2) CWD
    paths.append(os.path.join(os.getcwd(), ".env"))
    # 3) Skript-Verzeichnis
    script_dir = os.path.dirname(os.path.abspath(__file__))
    paths.append(os.path.join(script_dir, ".env"))
    # 4) Projekt-nahe (.env im Parent)
    paths.append(os.path.join(os.path.dirname(script_dir), ".env"))
    # 5) User-Home
    paths.append(os.path.join(os.path.expanduser("~"), ".env"))
    # dedup
    seen, out = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

def get_api_key(cli_key: Optional[str], env_file: Optional[str]) -> Tuple[Optional[str], Dict[str, List[str]]]:
    tried: List[str] = []
    loaded: List[str] = []

    if env_file:
        tried.append(env_file)
        loaded += try_load_env([env_file])
    else:
        defaults = find_default_env_paths()
        tried += defaults
        loaded += try_load_env(defaults)

    key = cli_key or os.getenv("STEAM_API_KEY")
    return key, {"tried_env_files": tried, "loaded_env_files": loaded}

# ======================= DB-Finder =========================
COMMON_DB_NAMES = [
    "bot.db", "database.db", "app.db",
    "deadlock.db", "data.db"
]
COMMON_DB_PATHS = [
    ".", "./data", "./storage", "./db", "./.data",
]

def is_sqlite_file(path: str) -> bool:
    lower = path.lower()
    return lower.endswith(".db") or lower.endswith(".sqlite") or lower.endswith(".sqlite3")

def list_candidate_paths() -> List[str]:
    candidates: List[str] = []
    cwd = os.getcwd()
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # 1) Env DB_PATH
    env_db = os.getenv("DB_PATH")
    if env_db:
        candidates.append(env_db)

    # 2) Sehr wahrscheinliche Pfade relativ zu CWD
    for base in COMMON_DB_PATHS:
        for name in COMMON_DB_NAMES:
            candidates.append(os.path.abspath(os.path.join(cwd, base, name)))

    # 3) Relativ zum Skript
    for base in COMMON_DB_PATHS:
        for name in COMMON_DB_NAMES:
            candidates.append(os.path.abspath(os.path.join(script_dir, base, name)))

    # 4) „Dokumente/Deadlock“ (deine Umgebung)
    candidates.append(r"C:\Users\Nani-Admin\Documents\Deadlock\bot.db")
    candidates.append(r"C:\Users\Nani-Admin\Documents\Deadlock\data\bot.db")

    # dedup & nur existierende Dateien behalten (später)
    seen, out = set(), []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out

def walk_for_sqlite_roots(root: str, max_depth: int = 3, limit: int = 200) -> List[str]:
    """Durchsuche root rekursiv bis max_depth nach *.db/*.sqlite* (sanft begrenzt)."""
    found: List[str] = []
    root = os.path.abspath(root)
    for cur_root, dirs, files in os.walk(root):
        depth = cur_root[len(root):].count(os.sep)
        if depth > max_depth:
            # nicht tiefer gehen
            dirs[:] = []
            continue
        for f in files:
            if is_sqlite_file(f):
                found.append(os.path.join(cur_root, f))
                if len(found) >= limit:
                    return found
    return found

def has_steam_links_table(db_path: str) -> Tuple[bool, int]:
    """Prüft, ob steam_links existiert und liefert deren Zeilenzahl zurück (falls vorhanden)."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='steam_links'")
        row = cur.fetchone()
        if not row:
            conn.close()
            return (False, 0)
        cur = conn.execute("SELECT COUNT(1) AS c FROM steam_links")
        count = int(cur.fetchone()["c"])
        conn.close()
        return (True, count)
    except Exception:
        return (False, 0)

def auto_find_db() -> Tuple[Optional[str], dict]:
    """Findet automatisch die richtige DB-Datei mit steam_links-Tabelle.
       Bevorzugt: existiert, hat steam_links, höchste mtime & größte Rowcount."""
    tried: List[str] = []

    # Start: harte Kandidaten
    hard = [p for p in list_candidate_paths() if os.path.exists(p)]
    tried += hard

    # Falls nichts: suche im CWD und Skript-Verzeichnis rekursiv
    if not hard:
        hard = []
    extra = []
    extra += walk_for_sqlite_roots(os.getcwd(), max_depth=3, limit=200)
    extra += walk_for_sqlite_roots(os.path.dirname(os.path.abspath(__file__)), max_depth=3, limit=200)
    # dedup + existierende
    seen, all_paths = set(), []
    for p in hard + extra:
        if os.path.exists(p) and p not in seen:
            seen.add(p)
            all_paths.append(p)

    # Filter: nur DBs mit steam_links
    candidates = []
    for p in all_paths:
        ok, cnt = has_steam_links_table(p)
        tried.append(p)
        if ok:
            stat = os.stat(p)
            candidates.append({"path": p, "rows": cnt, "mtime": stat.st_mtime})

    if not candidates:
        return None, {"tried_paths": tried}

    # Sortierung: zuerst nach rows (desc), dann mtime (desc)
    candidates.sort(key=lambda x: (x["rows"], x["mtime"]), reverse=True)
    chosen = candidates[0]["path"]
    return chosen, {"tried_paths": tried, "candidates": candidates}

# ======================= Steam Parsing/Resolving ==============================
def is_id64(s: str) -> bool:
    return bool(s) and len(s) == 17 and s.isdigit()

def parse_input_to_hint(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Liefert (typ, wert):
      typ in {"id64","profiles_url","vanity_url","vanity", None}
    """
    s = (raw or "").strip()
    if not s:
        return (None, None)
    if is_id64(s):
        return ("id64", s)
    try:
        u = urlparse(s)
    except Exception:
        u = None
    if u and u.netloc and "steamcommunity.com" in u.netloc:
        path = (u.path or "").rstrip("/")
        m = re.search(r"/profiles/(\d{17})$", path)
        if m:
            return ("profiles_url", m.group(1))
        m = re.search(r"/id/([^/]+)$", path)
        if m:
            return ("vanity_url", m.group(1))
    if re.fullmatch(r"[A-Za-z0-9_.\-]+", s):
        return ("vanity", s)
    return (None, None)

def resolve_vanity(api_key: str, vanity: str, timeout: float = 10.0) -> Optional[str]:
    r = requests.get(STEAM_API_VANITY, params={"key": api_key, "vanityurl": vanity}, timeout=timeout)
    r.raise_for_status()
    data = r.json() or {}
    resp = data.get("response", {})
    if resp.get("success") == 1:
        sid = str(resp.get("steamid", "")).strip()
        if is_id64(sid):
            return sid
    return None

# ======================= Migration-Kern ======================================
def ensure_columns(conn: sqlite3.Connection):
    cur = conn.execute("PRAGMA table_info(steam_links)")
    cols = {row[1] for row in cur.fetchall()}
    if "legacy_ref" not in cols:
        conn.execute("ALTER TABLE steam_links ADD COLUMN legacy_ref TEXT")
    if "migrated_at" not in cols:
        conn.execute("ALTER TABLE steam_links ADD COLUMN migrated_at INTEGER")
    conn.commit()

def migrate_row(conn: sqlite3.Connection, api_key: str, user_id: int, bad_sid: str, name: str, primary_account: int) -> Tuple[bool, Optional[str]]:
    typ, val = parse_input_to_hint(bad_sid)
    if typ in ("id64", "profiles_url"):
        new_sid = val
    elif typ in ("vanity_url", "vanity") and val:
        try:
            new_sid = resolve_vanity(api_key, val)
        except requests.RequestException as e:
            print(f"[resolve] Fehler bei Vanity '{val}': {e}", file=sys.stderr)
            new_sid = None
    else:
        new_sid = None

    if not (new_sid and is_id64(new_sid)):
        # Markiere als legacy/unresolved
        conn.execute(
            "UPDATE steam_links SET legacy_ref=COALESCE(legacy_ref, steam_id), migrated_at=? WHERE user_id=? AND steam_id=?",
            (int(time.time()), user_id, bad_sid)
        )
        return (False, None)

    # Konfliktfall: (user_id, new_sid) existiert?
    cur = conn.execute("SELECT steam_id, name, primary_account FROM steam_links WHERE user_id=? AND steam_id=?", (user_id, new_sid))
    existing = cur.fetchone()

    if existing:
        # Merge
        if primary_account and not existing[2]:
            conn.execute("UPDATE steam_links SET primary_account=1, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND steam_id=?", (user_id, new_sid))
        if name and name.strip():
            conn.execute("UPDATE steam_links SET name=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=? AND steam_id=?", (name, user_id, new_sid))
        conn.execute(
            "UPDATE steam_links SET legacy_ref=COALESCE(legacy_ref, steam_id), migrated_at=? WHERE user_id=? AND steam_id=?",
            (int(time.time()), user_id, bad_sid)
        )
        conn.execute("DELETE FROM steam_links WHERE user_id=? AND steam_id=?", (user_id, bad_sid))
        return (True, new_sid)

    # Normalfall
    conn.execute(
        """
        INSERT INTO steam_links(user_id, steam_id, name, verified, primary_account, updated_at, legacy_ref, migrated_at)
        VALUES(?,?,?,?,?,CURRENT_TIMESTAMP,?,?)
        """,
        (user_id, new_sid, name or "", 0, int(primary_account), bad_sid, int(time.time()))
    )
    conn.execute("DELETE FROM steam_links WHERE user_id=? AND steam_id=?", (user_id, bad_sid))
    return (True, new_sid)

# ======================= Main ================================================
def main():
    ap = argparse.ArgumentParser(description="Migration: steam_links.steam_id → SteamID64 normalisieren (auto .env & DB-Find)")
    ap.add_argument("--db", help="Pfad zur SQLite-DB. Wenn weggelassen, wird automatisch gesucht.")
    ap.add_argument("--env-file", help="Pfad zur .env (optional)")
    ap.add_argument("--api-key", help="Steam API Key (überschreibt .env)")
    ap.add_argument("--pretty", action="store_true", help="JSON hübsch ausgeben")
    ap.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts ändern")
    args = ap.parse_args()

    # ENV / API-Key laden
    api_key, env_debug = get_api_key(args.api_key, args.env_file)
    if not api_key:
        print("ERROR: STEAM_API_KEY fehlt (für Vanity-Auflösung).", file=sys.stderr)
        print(json.dumps({"env_debug": env_debug}, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)

    # DB finden
    db_path = args.db
    finder_debug = {}
    if not db_path:
        chosen, info = auto_find_db()
        finder_debug = info
        db_path = chosen

    if not db_path or not os.path.exists(db_path):
        print(f"ERROR: DB nicht gefunden.", file=sys.stderr)
        debug = {
            "cwd": os.getcwd(),
            "script": os.path.abspath(__file__),
            "env_DB_PATH": os.getenv("DB_PATH"),
            "finder_debug": finder_debug,
            "env_debug": env_debug,
        }
        print(json.dumps(debug, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(2)

    # Verbindung & optional Schema-Erweiterung
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if not args.dry_run:
        ensure_columns(conn)

    # Kandidaten holen
    q = """
    SELECT user_id, steam_id, name, primary_account
    FROM steam_links
    WHERE NOT (length(steam_id)=17 AND steam_id GLOB '[0-9]*')
    ORDER BY user_id
    """
    cur = conn.execute(q)
    rows = cur.fetchall()

    report = {
        "db_path": db_path,
        "checked": len(rows),
        "migrated": 0,
        "unresolved": 0,
        "items": []
    }

    for row in rows:
        uid = int(row["user_id"])
        sid = str(row["steam_id"])
        name = row["name"] or ""
        prim = int(row["primary_account"] or 0)

        item = {"user_id": uid, "old": sid, "primary": prim}
        if args.dry_run:
            typ, val = parse_input_to_hint(sid)
            item["hint_type"] = typ
            item["hint_val"] = val
            report["items"].append(item)
            continue

        ok, new_sid = migrate_row(conn, api_key, uid, sid, name, prim)
        if ok:
            report["migrated"] += 1
            item["new"] = new_sid
        else:
            report["unresolved"] += 1
            item["new"] = None
        report["items"].append(item)

    if not args.dry_run:
        conn.commit()
    conn.close()

    if args.pretty:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=False))

if __name__ == "__main__":
    main()
