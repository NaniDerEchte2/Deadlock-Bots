# =========================================
# Deadlock-Bots – Zentrale SQLite-DB (KANONISCH)
# =========================================
# - Eine gemeinsame SQLite-Datei für alle Bots/Cogs.
# - Konfiguration (nur noch diese ENV-Keys):
#     DEADLOCK_DB_PATH  -> kompletter Dateipfad (höchste Priorität)
#     DEADLOCK_DB_DIR   -> Verzeichnis; Datei heißt dann deadlock.sqlite3
# - KEIN automatisches Setzen von LIVE_DB_PATH mehr (ENV bleibt sauber).
# - WAL, FOREIGN_KEYS, Busy-Timeout, Autocheckpoint, Journal-Limit aktiviert.
# - Python ≥ 3.10
# =========================================

from __future__ import annotations

import os
import atexit
import sqlite3
import threading
import logging
from pathlib import Path
from typing import Any, Iterable, Optional

# ---- Env-Keys (nur diese beiden werden unterstützt) ----
ENV_DB_PATH = "DEADLOCK_DB_PATH"   # kompletter Pfad zur DB-Datei (höchste Prio)
ENV_DB_DIR  = "DEADLOCK_DB_DIR"    # nur Verzeichnis; Datei = deadlock.sqlite3

# ---- Default-Dateiname/Ort (plattform-sicher) ----
def _default_dir() -> str:
    # Windows: %USERPROFILE%\Documents\Deadlock\service
    up = os.environ.get("USERPROFILE")
    if up:
        return str(Path(up) / "Documents" / "Deadlock" / "service")
    # Linux/Mac/Container: ~/Documents/Deadlock/service
    return str(Path.home() / "Documents" / "Deadlock" / "service")

DEFAULT_DIR = _default_dir()
DB_NAME     = "deadlock.sqlite3"

# ---- Modulweiter Zustand ----
_CONN: Optional[sqlite3.Connection] = None
_LOCK = threading.RLock()
_DB_PATH_CACHED: Optional[str] = None

logger = logging.getLogger(__name__)


# ---------- Pfad-Auflösung ----------

def _resolve_db_path() -> str:
    """
    Ermittelt den endgültigen DB-Pfad (eine Quelle der Wahrheit).
    Prio:
      1) DEADLOCK_DB_PATH (vollständiger Pfad)
      2) DEADLOCK_DB_DIR + DB_NAME
      3) DEFAULT_DIR + DB_NAME
    """
    p = os.environ.get(ENV_DB_PATH)
    if p:
        return str(Path(p))

    d = os.environ.get(ENV_DB_DIR) or DEFAULT_DIR
    return str(Path(d) / DB_NAME)


def _ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def db_path() -> str:
    """
    Liefert den tatsächlich verwendeten DB-Pfad.
    Hinweis: Der Pfad ist nach dem ersten Aufruf gecached.
    """
    global _DB_PATH_CACHED
    if _DB_PATH_CACHED:
        return _DB_PATH_CACHED
    _DB_PATH_CACHED = _resolve_db_path()
    return _DB_PATH_CACHED


# Praktischer Alias für Altcode:
DB_PATH: Path = Path(db_path())


# ---------- Verbindung / PRAGMA / Schema ----------

def connect() -> sqlite3.Connection:
    """
    Stellt eine einzelne, thread-safe geteilte Verbindung her.
    - Autocommit (isolation_level=None)
    - Row-Factory = sqlite3.Row
    - PRAGMAs gesetzt
    - Schema (idempotent) initialisiert
    """
    global _CONN, _DB_PATH_CACHED, DB_PATH
    if _CONN is not None:
        return _CONN

    path = _resolve_db_path()
    _ensure_parent(path)
    _DB_PATH_CACHED = path
    DB_PATH = Path(path)  # Alias aktualisieren

    _CONN = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=None,   # Autocommit
    )
    _CONN.row_factory = sqlite3.Row

    with _LOCK:
        # PRAGMAs: stabil & praxiserprobt
        _CONN.execute("PRAGMA journal_mode=WAL;")
        _CONN.execute("PRAGMA synchronous=NORMAL;")
        _CONN.execute("PRAGMA foreign_keys=ON;")
        _CONN.execute("PRAGMA busy_timeout=5000;")               # 5s
        _CONN.execute("PRAGMA wal_autocheckpoint=1000;")         # ~1000 Seiten
        _CONN.execute("PRAGMA journal_size_limit=104857600;")    # 100 MB
        _CONN.execute("PRAGMA temp_store=MEMORY;")
        # Optional tunable:
        # _CONN.execute("PRAGMA cache_size=-20000;")             # ~20MB
        # _CONN.execute('PRAGMA mmap_size=268435456;')           # 256MB (falls Filesystem erlaubt)

        init_schema(_CONN)

    return _CONN


def init_schema(conn: Optional[sqlite3.Connection] = None) -> None:
    """
    Legt das Schema an (idempotent). Enthält u. a.:
      - kv_store, templates, user_threads
      - users, guild_settings, onboarding_sessions, user_preferences
      - ranks, temp_voice_channels, voice_sessions, voice_stats
      - matches, changelog_subscriptions, posted_changelogs
      - steam_links, live_player_state, live_lane_state, live_lane_members
    """
    c = conn or connect()
    with _LOCK:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_version(
              version INTEGER NOT NULL
            );
            INSERT INTO schema_version(version)
              SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);

            -- generische KV-Ablage (namespaced)
            CREATE TABLE IF NOT EXISTS kv_store(
              ns TEXT NOT NULL,
              k  TEXT NOT NULL,
              v  TEXT NOT NULL,
              PRIMARY KEY(ns, k)
            );

            -- Templates (Kompat-Schicht)
            CREATE TABLE IF NOT EXISTS templates(
              key        TEXT PRIMARY KEY,
              content    TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- User↔Thread (Kompat-Schicht)
            CREATE TABLE IF NOT EXISTS user_threads(
              user_id    INTEGER PRIMARY KEY,
              thread_id  INTEGER NOT NULL,
              created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            -- Nutzer/Guild Basis
            CREATE TABLE IF NOT EXISTS users(
              user_id    INTEGER PRIMARY KEY,
              name       TEXT,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Mehrere Settings je Guild: (guild_id, key) als PK
            CREATE TABLE IF NOT EXISTS guild_settings(
              guild_id INTEGER NOT NULL,
              key      TEXT    NOT NULL,
              value    TEXT,
              PRIMARY KEY (guild_id, key)
            );

            -- Onboarding & Präferenzen
            CREATE TABLE IF NOT EXISTS onboarding_sessions(
              user_id   INTEGER PRIMARY KEY,
              step      TEXT,
              data_json TEXT,
              updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_preferences(
              user_id      INTEGER PRIMARY KEY,
              funny_custom INTEGER DEFAULT 0,
              grind_custom INTEGER DEFAULT 0,
              patch_notes  INTEGER DEFAULT 0,
              rank         INTEGER,
              updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Ranks
            CREATE TABLE IF NOT EXISTS ranks(
              user_id   INTEGER PRIMARY KEY,
              rank      INTEGER NOT NULL,
              updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- TempVoice
            CREATE TABLE IF NOT EXISTS temp_voice_channels(
              channel_id INTEGER PRIMARY KEY,
              owner_id   INTEGER NOT NULL,
              name       TEXT,
              user_limit INTEGER,
              privacy    TEXT DEFAULT 'public',
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              deleted_at DATETIME
            );

            -- Voice Tracking
            CREATE TABLE IF NOT EXISTS voice_sessions(
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id    INTEGER NOT NULL,
              channel_id INTEGER NOT NULL,
              joined_at  DATETIME NOT NULL,
              left_at    DATETIME,
              seconds    INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_voice_sessions_user_open
              ON voice_sessions(user_id, left_at);

            CREATE TABLE IF NOT EXISTS voice_stats(
              user_id       INTEGER PRIMARY KEY,
              total_seconds INTEGER NOT NULL DEFAULT 0,
              last_update   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Team Balancer / Matches
            CREATE TABLE IF NOT EXISTS matches(
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              guild_id   INTEGER,
              created_by INTEGER,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              data_json  TEXT
            );

            -- Changelog Bot
            CREATE TABLE IF NOT EXISTS changelog_subscriptions(
              guild_id      INTEGER PRIMARY KEY,
              channel_id    INTEGER NOT NULL,
              role_ping_id  INTEGER
            );
            CREATE TABLE IF NOT EXISTS posted_changelogs(
              id        TEXT PRIMARY KEY,
              posted_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Steam-Links (mehrere Konten pro User möglich)
            CREATE TABLE IF NOT EXISTS steam_links(
              user_id    INTEGER NOT NULL,
              steam_id   TEXT    NOT NULL,
              name       TEXT,
              verified   INTEGER DEFAULT 0,
              primary_account INTEGER DEFAULT 0,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(user_id, steam_id)
            );

            -- Live-Player-State (Cache der letzten Steam-API-Auswertung)
            CREATE TABLE IF NOT EXISTS live_player_state(
              steam_id TEXT PRIMARY KEY,
              last_gameid TEXT,
              last_server_id TEXT,
              last_seen_ts INTEGER,
              in_deadlock_now INTEGER DEFAULT 0,
              in_match_now_strict INTEGER DEFAULT 0
            );

            -- Steam Rich Presence Cache (gefüllt vom node-steam-user Service)
            CREATE TABLE IF NOT EXISTS steam_rich_presence(
              steam_id TEXT PRIMARY KEY,
              app_id INTEGER,
              status TEXT,
              status_text TEXT,
              display TEXT,
              player_group TEXT,
              player_group_size INTEGER,
              connect TEXT,
              mode TEXT,
              map TEXT,
              party_size INTEGER,
              raw_json TEXT,
              last_update INTEGER,
              updated_at INTEGER
            );

            -- Steam Freundes-Snapshots (Rohdaten aus der Freundesliste)
            CREATE TABLE IF NOT EXISTS steam_friend_snapshots(
              steam_id TEXT PRIMARY KEY,
              relationship INTEGER,
              persona_state INTEGER,
              persona_name TEXT,
              game_app_id INTEGER,
              game_name TEXT,
              last_logoff INTEGER,
              last_logon INTEGER,
              persona_flags INTEGER,
              avatar_hash TEXT,
              persona_json TEXT,
              rich_presence_json TEXT,
              updated_at INTEGER
            );

            -- Optionale zusätzliche Watchlist für den Presence-Service
            CREATE TABLE IF NOT EXISTS steam_presence_watchlist(
              steam_id TEXT PRIMARY KEY,
              note TEXT,
              added_at INTEGER DEFAULT (strftime('%s','now'))
            );

            -- Ausgehende Freundschaftsanfragen des Steam-Bots
            CREATE TABLE IF NOT EXISTS steam_friend_requests(
              steam_id TEXT PRIMARY KEY,
              status TEXT DEFAULT 'pending',
              requested_at INTEGER DEFAULT (strftime('%s','now')),
              last_attempt INTEGER,
              attempts INTEGER DEFAULT 0,
              error TEXT
            );

            -- Vorgehaltene Steam-Quick-Invite-Links (werden vom Node-Service erzeugt)
            CREATE TABLE IF NOT EXISTS steam_quick_invites(
              token TEXT PRIMARY KEY,
              invite_link TEXT NOT NULL,
              invite_limit INTEGER DEFAULT 1,
              invite_duration INTEGER,
              created_at INTEGER NOT NULL,
              expires_at INTEGER,
              status TEXT DEFAULT 'available',
              reserved_by INTEGER,
              reserved_at INTEGER,
              last_seen INTEGER
            );

            -- Gespeicherte Refresh-Token für den Steam-Bot-Login
            CREATE TABLE IF NOT EXISTS steam_refresh_tokens(
              account_name TEXT PRIMARY KEY,
              refresh_token TEXT NOT NULL,
              received_at INTEGER DEFAULT (strftime('%s','now'))
            );

            -- Live-Lane-Status (pro Voice-Channel)
            CREATE TABLE IF NOT EXISTS live_lane_state(
              channel_id  INTEGER PRIMARY KEY,
              is_active   INTEGER DEFAULT 0,
              started_at  INTEGER,
              last_update INTEGER,
              minutes     INTEGER DEFAULT 0,
              suffix      TEXT,
              reason      TEXT
            );

            -- Pro Lane und Member letzter Check
            CREATE TABLE IF NOT EXISTS live_lane_members(
              channel_id INTEGER NOT NULL,
              user_id    INTEGER NOT NULL,
              in_match   INTEGER DEFAULT 0,
              server_id  TEXT,
              checked_ts INTEGER,
              PRIMARY KEY(channel_id, user_id)
            );
            """
        )
        # Indizes ergänzen (idempotent)
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_lls_active   ON live_lane_state(is_active)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_llm_channel  ON live_lane_members(channel_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_llm_checked  ON live_lane_members(checked_ts)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_user  ON steam_links(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_steam ON steam_links(steam_id)")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_quick_invites_status ON steam_quick_invites(status, expires_at)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_quick_invites_reserved ON steam_quick_invites(reserved_by)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_rich_presence_updated ON steam_rich_presence(updated_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_friend_snapshots_updated ON steam_friend_snapshots(updated_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ranks_rank ON ranks(rank)")
        except sqlite3.Error as e:
            logger.debug("Optionale Index-Erstellung übersprungen: %s", e, exc_info=True)


# ---------- Low-Level Helpers (sicher, mit Bind-Parametern) ----------

def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with _LOCK:
        connect().execute(sql, params)


def executemany(sql: str, seq_of_params: Iterable[Iterable[Any]]) -> None:
    with _LOCK:
        connect().executemany(sql, seq_of_params)


def query_one(sql: str, params: Iterable[Any] = ()):  # -> sqlite3.Row | None
    with _LOCK:
        cur = connect().execute(sql, params)
        try:
            return cur.fetchone()
        finally:
            cur.close()


def query_all(sql: str, params: Iterable[Any] = ()):  # -> list[sqlite3.Row]
    with _LOCK:
        cur = connect().execute(sql, params)
        try:
            return cur.fetchall()
        finally:
            cur.close()


# ---------- KV (namespaced) ----------

def set_kv(ns: str, k: str, v: str) -> None:
    execute(
        """
        INSERT INTO kv_store(ns,k,v) VALUES(?,?,?)
        ON CONFLICT(ns,k) DO UPDATE SET v=excluded.v
        """,
        (ns, k, v),
    )


def get_kv(ns: str, k: str) -> Optional[str]:
    row = query_one("SELECT v FROM kv_store WHERE ns=? AND k=?", (ns, k))
    return row[0] if row else None


# ---------- Templates (Kompat-Schicht) ----------

def get_template(key: str, default: Optional[str] = None) -> Optional[str]:
    row = query_one("SELECT content FROM templates WHERE key = ?", (key,))
    if row:
        return row[0]
    if default is not None:
        execute(
            "INSERT OR IGNORE INTO templates (key, content) VALUES (?, ?)",
            (key, default),
        )
        return default
    return None


def set_template(key: str, content: str) -> None:
    execute(
        """
        INSERT INTO templates (key, content)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE
        SET content = excluded.content, updated_at = datetime('now')
        """,
        (key, content),
    )


# ---------- UserThreads (Kompat-Schicht) ----------

def get_user_thread(user_id: int) -> Optional[int]:
    row = query_one("SELECT thread_id FROM user_threads WHERE user_id = ?", (user_id,))
    return int(row[0]) if row else None


def set_user_thread(user_id: int, thread_id: int) -> None:
    execute(
        """
        INSERT INTO user_threads (user_id, thread_id)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE
        SET thread_id = excluded.thread_id, created_at = datetime('now')
        """,
        (user_id, thread_id),
    )


def delete_user_thread(user_id: int) -> None:
    execute("DELETE FROM user_threads WHERE user_id = ?", (user_id,))


def delete_user_thread_by_thread_id(thread_id: int) -> None:
    execute("DELETE FROM user_threads WHERE thread_id = ?", (thread_id,))


# ---------- Pflege ----------

@atexit.register
def _vacuum_on_shutdown() -> None:
    try:
        with _LOCK:
            if _CONN is not None:
                # kleines Timeout für sauberes Beenden unter Last
                _CONN.execute("PRAGMA busy_timeout=1000;")
                _CONN.execute("VACUUM;")
    except sqlite3.Error as e:
        logger.debug("VACUUM beim Shutdown übersprungen/fehlgeschlagen: %s", e, exc_info=True)
