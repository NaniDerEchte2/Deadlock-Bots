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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional


log = logging.getLogger(__name__)

# ---- Timeouts (per-connection) ---------------------------------------------
# Default: wait up to 15s on busy locks; override with ENV if needed.
DB_BUSY_TIMEOUT_MS = int(os.environ.get("DEADLOCK_DB_BUSY_TIMEOUT_MS", "15000"))
DB_CONNECT_TIMEOUT = float(os.environ.get("DEADLOCK_DB_TIMEOUT", str(DB_BUSY_TIMEOUT_MS / 1000)))

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
log.debug("DB_PATH alias initialisiert: %s", DB_PATH)


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
    log.debug("DB_PATH alias aktualisiert: %s", DB_PATH)

    _CONN = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=None,   # Autocommit
        timeout=DB_CONNECT_TIMEOUT,
    )
    _CONN.row_factory = sqlite3.Row

    with _LOCK:
        # PRAGMAs: stabil & praxiserprobt
        _CONN.execute("PRAGMA journal_mode=WAL;")
        _CONN.execute("PRAGMA synchronous=NORMAL;")
        _CONN.execute("PRAGMA foreign_keys=ON;")
        _CONN.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS};")
        _CONN.execute("PRAGMA wal_autocheckpoint=1000;")         # ~1000 Seiten
        _CONN.execute("PRAGMA journal_size_limit=104857600;")    # 100 MB
        _CONN.execute("PRAGMA temp_store=MEMORY;")
        # Optional tunable:
        # _CONN.execute("PRAGMA cache_size=-20000;")             # ~20MB
        # _CONN.execute('PRAGMA mmap_size=268435456;')           # 256MB (falls Filesystem erlaubt)

        init_schema(_CONN)

    return _CONN


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Contextmanager, der die zentrale Verbindung thread-safe bereitstellt."""
    conn = connect()
    with _LOCK:
        yield conn


def init_schema(conn: Optional[sqlite3.Connection] = None) -> None:
    """
    Legt das Schema an (idempotent). Enthält u. a.:
      - schema_version, kv_store
      - voice_stats (inkl. Migrations-Update)
      - steam_links, live_player_state
      - steam_friend_requests, steam_quick_invites, steam_tasks
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

            -- Voice Stats (aggregiert)
            CREATE TABLE IF NOT EXISTS voice_stats(
              user_id       INTEGER PRIMARY KEY,
              total_seconds INTEGER NOT NULL DEFAULT 0,
              total_points  INTEGER NOT NULL DEFAULT 0,
              last_update   DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Voice Session Log (historisch, f\u00fcr Zeitverl\u00e4ufe)
            CREATE TABLE IF NOT EXISTS voice_session_log(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              display_name TEXT,
              guild_id INTEGER,
              channel_id INTEGER,
              channel_name TEXT,
              started_at DATETIME NOT NULL,
              ended_at DATETIME NOT NULL,
              duration_seconds INTEGER NOT NULL DEFAULT 0,
              points INTEGER NOT NULL DEFAULT 0,
              peak_users INTEGER,
              user_counts_json TEXT
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
              in_match_now_strict INTEGER DEFAULT 0,
              deadlock_stage TEXT,
              deadlock_minutes INTEGER,
              deadlock_localized TEXT,
              deadlock_hero TEXT,
              deadlock_party_hint TEXT,
              deadlock_updated_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS deadlock_voice_watch(
              steam_id TEXT PRIMARY KEY,
              guild_id INTEGER,
              channel_id INTEGER,
              updated_at INTEGER NOT NULL
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

            -- Deadlock-Beta-Einladungs-Workflow (Discord ↔ Steam)
            CREATE TABLE IF NOT EXISTS steam_beta_invites(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              discord_id INTEGER NOT NULL,
              steam_id64 TEXT NOT NULL,
              account_id INTEGER,
              status TEXT NOT NULL,
              last_error TEXT,
              friend_requested_at INTEGER,
              friend_confirmed_at INTEGER,
              invite_sent_at INTEGER,
              last_notified_at INTEGER,
              created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
              updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
              UNIQUE(discord_id),
              UNIQUE(steam_id64)
            );

            -- Steuer-Tabelle für den Steam-Task-Consumer
            CREATE TABLE IF NOT EXISTS steam_tasks(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              type TEXT NOT NULL,
              payload TEXT,
              status TEXT NOT NULL DEFAULT 'PENDING',
              result TEXT,
              error TEXT,
              created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
              updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
              started_at INTEGER,
              finished_at INTEGER
            );

            -- Protokollierte Fragen & Antworten des Server-FAQ-Bots
            CREATE TABLE IF NOT EXISTS server_faq_logs(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              guild_id INTEGER,
              channel_id INTEGER,
              user_id INTEGER,
              question TEXT NOT NULL,
              answer TEXT,
              model TEXT,
              metadata TEXT
            );
            -- Standalone Bot Steuerung & Monitoring
            CREATE TABLE IF NOT EXISTS standalone_commands(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              bot TEXT NOT NULL,
              command TEXT NOT NULL,
              payload TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              result TEXT,
              error TEXT,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
              started_at DATETIME,
              finished_at DATETIME
            );

            CREATE TABLE IF NOT EXISTS standalone_bot_state(
              bot TEXT PRIMARY KEY,
              heartbeat INTEGER NOT NULL,
              payload TEXT,
              updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            -- Persistent Discord UI Views (for bot restarts)
            CREATE TABLE IF NOT EXISTS persistent_views(
              message_id TEXT PRIMARY KEY,
              channel_id TEXT NOT NULL,
              guild_id TEXT NOT NULL,
              view_type TEXT NOT NULL,
              user_id TEXT,
              created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            """
        )
        # Nachträglich hinzugefügte Spalten idempotent sicherstellen
        try:
            c.execute(
                "ALTER TABLE voice_stats ADD COLUMN total_points INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        try:
            c.execute(
                "ALTER TABLE voice_session_log ADD COLUMN display_name TEXT"
            )
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
        # Live player state extensions
        for alter_sql in (
            "ALTER TABLE live_player_state ADD COLUMN deadlock_stage TEXT",
            "ALTER TABLE live_player_state ADD COLUMN deadlock_minutes INTEGER",
            "ALTER TABLE live_player_state ADD COLUMN deadlock_localized TEXT",
            "ALTER TABLE live_player_state ADD COLUMN deadlock_hero TEXT",
            "ALTER TABLE live_player_state ADD COLUMN deadlock_party_hint TEXT",
            "ALTER TABLE live_player_state ADD COLUMN deadlock_updated_at INTEGER",
        ):
            try:
                c.execute(alter_sql)
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
        # Indizes ergänzen (idempotent)
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_user  ON steam_links(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_steam_links_steam ON steam_links(steam_id)")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_quick_invites_status ON steam_quick_invites(status, expires_at)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_quick_invites_reserved ON steam_quick_invites(reserved_by)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_beta_invites_status ON steam_beta_invites(status)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_beta_invites_account ON steam_beta_invites(account_id)"
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_steam_tasks_status ON steam_tasks(status, id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_steam_tasks_updated ON steam_tasks(updated_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_standalone_commands_status ON standalone_commands(bot, status, id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_standalone_commands_created ON standalone_commands(created_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_standalone_state_updated ON standalone_bot_state(updated_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_voice_log_started ON voice_session_log(started_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_voice_log_user ON voice_session_log(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_voice_log_guild ON voice_session_log(guild_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_voice_log_display_name ON voice_session_log(display_name)")

            # Performance-Indizes für häufige Queries
            # Leaderboard-Query: ORDER BY total_points DESC, total_seconds DESC
            c.execute("CREATE INDEX IF NOT EXISTS idx_voice_stats_leaderboard ON voice_stats(total_points DESC, total_seconds DESC)")
            # User Stats Lookup mit allen Feldern (covering index)
            c.execute("CREATE INDEX IF NOT EXISTS idx_voice_stats_user_lookup ON voice_stats(user_id, total_seconds, total_points)")
            # TempVoice Rehydration: WHERE guild_id=? (composite index)
            c.execute("CREATE INDEX IF NOT EXISTS idx_tempvoice_lanes_guild ON tempvoice_lanes(guild_id, channel_id)")
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
