#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Commit Watcher -> Discord (Diff-Vorschau, .patch, .env, Rate-Limit-Backoff)
- Beobachtet öffentliche Repos/Branches und postet neue Commits über Discord-Webhook
- Bei 1 neuem Commit: Dateiliste + Diff-Snippets im Embed, optional .patch anhängen
- ETag-Caching; SQLite-State
- .env-Unterstützung (ohne externe Pakete)
- NEU: Sauberes Rate-Limit-Handling (X-RateLimit-Remaining/Reset + 403-Backoff)
"""

import os
import sys
import time
import json
import sqlite3
import logging
from typing import Optional, Tuple, Dict, Any, List

import requests

# -------------------- .env LOADER (ohne Abhängigkeiten) -------------------- #
def _parse_env_line(line: str):
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if s.lower().startswith("export "):
        s = s[7:].lstrip()
    if "=" not in s:
        return None
    k, v = s.split("=", 1)
    k, v = k.strip(), v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        quote = v[0]
        v = v[1:-1]
        if quote == '"':
            v = (v.replace("\\n", "\n")
                   .replace("\\r", "\r")
                   .replace("\\t", "\t")
                   .replace('\\"', '"')
                   .replace("\\\\", "\\"))
    return k, v

def _load_env_file(path: str) -> Dict[str, str]:
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                kv = _parse_env_line(line)
                if kv:
                    out[kv[0]] = kv[1]
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"Warnung: .env konnte nicht gelesen werden ({path}): {e}", file=sys.stderr)
    return out

def load_dotenv_fallback() -> None:
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        script_dir = os.getcwd()
    cwd = os.getcwd()
    candidates = [
        os.path.join(script_dir, ".env.local"),
        os.path.join(script_dir, ".env"),
        os.path.join(cwd, ".env.local"),
        os.path.join(cwd, ".env"),
    ]
    merged: Dict[str, str] = {}
    for p in candidates:
        for k, v in _load_env_file(p).items():
            if k not in merged:
                merged[k] = v
    for k, v in merged.items():
        if k not in os.environ:
            os.environ[k] = v

load_dotenv_fallback()
# --------------------------------------------------------------------------- #

# ---------- Konfiguration ----------
DISCORD_WEBHOOK = (os.getenv("DISCORD_WEBHOOK") or "").strip()
WATCH_LIST = os.getenv("WATCH_LIST", "SteamDatabase/GameTracking-Deadlock@master")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SEC", "60"))
DB_PATH = os.getenv("DB_PATH", "./commit_watcher.sqlite3")
GITHUB_TOKEN = (os.getenv("GITHUB_TOKEN") or "").strip()

ATTACH_PATCH = (os.getenv("ATTACH_PATCH", "false").lower() == "true")
MAX_FILES_IN_EMBED = max(1, int(os.getenv("MAX_FILES_IN_EMBED", "6")))
MAX_DIFF_LINES_PER_FILE = max(3, int(os.getenv("MAX_DIFF_LINES_PER_FILE", "12")))

# Rate-Limit-Handling
MIN_REMAINING_BEFORE_PAUSE = int(os.getenv("RATE_MIN_REMAINING", "2"))  # wenn <=, dann bis Reset schlafen
EXTRA_BACKOFF_SEC = int(os.getenv("RATE_EXTRA_BACKOFF_SEC", "3"))       # kleiner Puffer

if not DISCORD_WEBHOOK:
    print("ERROR: DISCORD_WEBHOOK ist nicht gesetzt (ENV oder .env).", file=sys.stderr)
    sys.exit(2)
if not DISCORD_WEBHOOK.startswith("https://"):
    print("WARNUNG: DISCORD_WEBHOOK scheint keine valide HTTPS-URL zu sein.", file=sys.stderr)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("commit-watcher")

# ---------- DB Layer ----------
DDL = """
CREATE TABLE IF NOT EXISTS state (
  repo TEXT NOT NULL,
  branch TEXT NOT NULL,
  last_commit_sha TEXT,
  etag TEXT,
  PRIMARY KEY (repo, branch)
);
"""

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(DDL)
    return conn

def db_get_state(conn: sqlite3.Connection, repo: str, branch: str) -> Tuple[Optional[str], Optional[str]]:
    cur = conn.execute("SELECT last_commit_sha, etag FROM state WHERE repo=? AND branch=?", (repo, branch))
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)

def db_set_state(conn: sqlite3.Connection, repo: str, branch: str, sha: Optional[str], etag: Optional[str]) -> None:
    conn.execute(
        "INSERT INTO state(repo,branch,last_commit_sha,etag) VALUES(?,?,?,?) "
        "ON CONFLICT(repo,branch) DO UPDATE SET last_commit_sha=excluded.last_commit_sha, etag=excluded.etag",
        (repo, branch, sha, etag)
    )
    conn.commit()

# ---------- HTTP / GitHub Helper ----------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "deadlock-discord-commit-watcher/1.3"})
if GITHUB_TOKEN:
    SESSION.headers.update({"Authorization": f"Bearer {GITHUB_TOKEN}"})

def _respect_rate_and_sleep(resp: requests.Response) -> None:
    """
    Prüft X-RateLimit-Remaining/Reset und schläft ggf. bis zum Reset (plus Puffer).
    Wird bei JEDEM erfolgreichen GitHub-Call ausgeführt.
    """
    try:
        remaining = int(resp.headers.get("X-RateLimit-Remaining", "-1"))
        reset_unix = int(resp.headers.get("X-RateLimit-Reset", "0"))
    except Exception:
        return

    if remaining <= MIN_REMAINING_BEFORE_PAUSE and reset_unix:
        now = int(time.time())
        wait = max(0, reset_unix - now) + EXTRA_BACKOFF_SEC
        if wait > 0:
            log.warning("Rate-Limit nahe Null (remaining=%s). Pausiere bis Reset: %ss", remaining, wait)
            time.sleep(wait)

def _github_get(url: str, *, params=None, headers=None, timeout=30, allow_404=False) -> Optional[requests.Response]:
    """
    GET mit 403-Backoff:
    - Bei 403 mit Rate-Limit-Message: bis X-RateLimit-Reset+Puffer schlafen und einmal retryen.
    - Gibt Response oder None (bei harten Fehlern) zurück.
    """
    hdrs = {"Accept": "application/vnd.github+json"}
    if headers:
        hdrs.update(headers)
    try:
        resp = SESSION.get(url, params=params, headers=hdrs, timeout=timeout)
    except Exception:
        log.exception("GitHub GET fehlgeschlagen: %s", url)
        return None

    # 304, 200, 404 (wenn erlaubt) direkt durchreichen
    if resp.status_code in (200, 304) or (allow_404 and resp.status_code == 404):
        _respect_rate_and_sleep(resp)
        return resp

    # 403 Rate-Limit?
    if resp.status_code == 403 and "rate limit exceeded" in resp.text.lower():
        try:
            reset_unix = int(resp.headers.get("X-RateLimit-Reset", "0"))
        except Exception:
            reset_unix = 0
        now = int(time.time())
        wait = max(10, reset_unix - now) + EXTRA_BACKOFF_SEC
        log.warning("Rate-Limit exceeded. Warte %ss und versuche erneut.", wait)
        time.sleep(wait)
        try:
            resp2 = SESSION.get(url, params=params, headers=hdrs, timeout=timeout)
        except Exception:
            log.exception("GitHub GET retry fehlgeschlagen: %s", url)
            return None
        _respect_rate_and_sleep(resp2)
        if resp2.status_code in (200, 304) or (allow_404 and resp2.status_code == 404):
            return resp2
        log.warning("GitHub nach Backoff weiterhin Fehler: %s %s", resp2.status_code, resp2.text[:200])
        return None

    # Andere Fehler
    log.warning("GitHub API %s -> %s %s", url, resp.status_code, resp.text[:200])
    return None

# ---------- GitHub API Wrappers ----------
def gh_list_commits(repo: str, branch: str, etag: Optional[str]):
    url = f"https://api.github.com/repos/{repo}/commits"
    params = {"sha": branch, "per_page": 10}
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    resp = _github_get(url, params=params, headers=headers)
    if resp is None:
        return 520, None, None
    new_etag = resp.headers.get("ETag")
    if resp.status_code == 304:
        return 304, new_etag, None
    if resp.status_code != 200:
        return resp.status_code, new_etag, None
    try:
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError("Unexpected JSON for list commits")
        return 200, new_etag, data
    except Exception as e:
        log.exception("JSON-Parse list commits: %s", e)
        return 500, new_etag, None

def gh_get_commit(repo: str, sha: str) -> Optional[Dict[str, Any]]:
    url = f"https://api.github.com/repos/{repo}/commits/{sha}"
    resp = _github_get(url)
    if resp is None or resp.status_code != 200:
        return None
    return resp.json()

def gh_get_patch_bytes(repo: str, sha: str) -> Optional[bytes]:
    # Patch über die HTML-Seite laden (GitHub limitiert hier meist großzügiger)
    url = f"https://github.com/{repo}/commit/{sha}.patch"
    try:
        resp = SESSION.get(url, timeout=30)
        if resp.status_code != 200:
            log.warning("Patch laden fehlgeschlagen %s -> %s", url, resp.status_code)
            return None
        return resp.content
    except Exception:
        log.exception("Patch-Download Fehler")
        return None

# ---------- Discord Webhook ----------
def discord_post_embed(embed: Dict[str, Any]) -> bool:
    try:
        r = SESSION.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=30)
        return r.status_code in (200, 204)
    except Exception:
        log.exception("Discord Webhook Fehler")
        return False

def discord_post_embed_with_file(embed: Dict[str, Any], filename: str, filebytes: bytes) -> bool:
    try:
        files = {"file": (filename, filebytes, "text/plain")}
        payload = {"payload_json": json.dumps({"embeds": [embed]})}
        r = SESSION.post(DISCORD_WEBHOOK, data=payload, files=files, timeout=60)
        return r.status_code in (200, 204)
    except Exception:
        log.exception("Discord Webhook Upload Fehler")
        return False

# ---------- Helpers ----------
def escape_md(s: str) -> str:
    for ch in ("*", "_", "`", "~"):
        s = s.replace(ch, f"\\{ch}")
    return s

def parse_watch_list(raw: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for item in [x.strip() for x in raw.split(",") if x.strip()]:
        if "@" in item:
            repo, branch = item.split("@", 1)
        else:
            repo, branch = item, "master"
        out.append((repo, branch))
    return out

def truncate_text(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"

def build_overview_embed(repo: str, branch: str, commits: List[Dict[str, Any]]) -> Dict[str, Any]:
    show = commits[:5]
    lines = []
    for c in show:
        sha = (c.get("sha") or "")[:7]
        msg = (c.get("commit", {}).get("message") or "").strip().splitlines()[0]
        author = (c.get("commit", {}).get("author", {}).get("name")) or (c.get("author") or {}).get("login") or "unknown"
        url = c.get("html_url")
        lines.append(f"- [`{sha}`]({url}) {escape_md(msg)} — {escape_md(author)}")
    desc = "\n".join(lines) if lines else "Keine Details verfügbar."
    repo_url = f"https://github.com/{repo}"
    compare_url = f"https://github.com/{repo}/commits/{branch}"
    title = f"Pushed {len(commits)} commit{'s' if len(commits)!=1 else ''} to {branch}"
    return {
        "title": title,
        "url": compare_url,
        "description": desc,
        "color": 0x7289DA,
        "author": {"name": repo, "url": repo_url},
        "footer": {"text": "GitHub → Discord"},
    }

def build_single_commit_embed(repo: str, commit: Dict[str, Any]) -> Dict[str, Any]:
    sha_full = commit.get("sha") or ""
    sha7 = sha_full[:7]
    msg_full = (commit.get("commit", {}).get("message") or "").strip()
    title_line = msg_full.splitlines()[0] if msg_full else sha7
    author = (commit.get("commit", {}).get("author", {}).get("name")) or (commit.get("author") or {}).get("login") or "unknown"
    html_url = commit.get("html_url") or f"https://github.com/{repo}/commit/{sha_full}"

    files = commit.get("files") or []
    added = sum(1 for f in files if (f.get("status") == "added"))
    removed = sum(1 for f in files if (f.get("status") == "removed"))
    modified = sum(1 for f in files if (f.get("status") not in ("added","removed")))

    desc = f"**{escape_md(title_line)}**\n"
    desc += f"`{sha7}` by {escape_md(author)} • {added} ⊕  {removed} ⊖  {modified} ✎\n"
    desc += f"[Commit anzeigen]({html_url})\n"

    embed: Dict[str, Any] = {
        "title": f"Commit {sha7}",
        "url": html_url,
        "description": truncate_text(desc, 4096),
        "color": 0x43B581,
        "author": {"name": repo, "url": f"https://github.com/{repo}"},
        "footer": {"text": "GitHub → Discord (Diff preview)"},
        "fields": []
    }

    count = 0
    for f in files:
        if count >= MAX_FILES_IN_EMBED or len(embed["fields"]) >= 25:
            break
        fname = f.get("filename") or "(unbenannt)"
        patch = f.get("patch") or ""
        lines = patch.splitlines()
        snippet_lines = []
        for ln in lines:
            if ln.startswith(("@@", "+", "-", " ")):
                snippet_lines.append(ln)
            if len(snippet_lines) >= MAX_DIFF_LINES_PER_FILE:
                break
        if not snippet_lines:
            snippet_lines = ["(kein Patch verfügbar)"]
        codeblock = "```diff\n" + "\n".join(snippet_lines) + "\n```"
        embed["fields"].append({
            "name": truncate_text(fname, 256),
            "value": truncate_text(codeblock, 1024),
            "inline": False
        })
        count += 1

    return embed

# ---------- Watcher ----------
def process_repo(conn: sqlite3.Connection, repo: str, branch: str) -> None:
    last_sha, etag = db_get_state(conn, repo, branch)
    status, new_etag, commits = gh_list_commits(repo, branch, etag)
    if status == 304:
        return
    if status != 200 or not commits:
        return

    new_items: List[Dict[str, Any]] = []
    if last_sha:
        for c in commits:
            if c.get("sha") == last_sha:
                break
            new_items.append(c)
    else:
        new_items = [commits[0]]  # Erstlauf: nur neuesten posten

    if not new_items:
        db_set_state(conn, repo, branch, last_sha or commits[0]["sha"], new_etag)
        return

    if len(new_items) == 1:
        sha = new_items[0].get("sha")
        detail = gh_get_commit(repo, sha)
        if detail:
            embed = build_single_commit_embed(repo, detail)
            if ATTACH_PATCH:
                patch_bytes = gh_get_patch_bytes(repo, sha) or b""
                ok = discord_post_embed_with_file(embed, f"{sha}.patch", patch_bytes)
            else:
                ok = discord_post_embed(embed)
            if ok:
                db_set_state(conn, repo, branch, sha, new_etag)
                log.info("[%s@%s] 1 Commit gemeldet (%s)", repo, branch, sha[:7])
                return

    embed = build_overview_embed(repo, branch, new_items)
    if discord_post_embed(embed):
        newest_sha = new_items[0]["sha"]
        db_set_state(conn, repo, branch, newest_sha, new_etag)
        log.info("[%s@%s] %d Commits gemeldet (overview), last=%s", repo, branch, len(new_items), newest_sha[:7])

def main():
    watch = parse_watch_list(WATCH_LIST)
    if not watch:
        log.error("WATCH_LIST leer – nichts zu tun.")
        sys.exit(1)
    conn = db_connect()
    log.info("Watcher gestartet. Interval=%ss, DB=%s, Repos=%s",
             POLL_INTERVAL, DB_PATH, ", ".join([f"{r}@{b}" for r,b in watch]))
    try:
        while True:
            for repo, branch in watch:
                try:
                    process_repo(conn, repo, branch)
                except Exception:
                    log.exception("Fehler bei %s@%s", repo, branch)
                time.sleep(1.5)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("Beendet.")
    finally:
        try:
            conn.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
