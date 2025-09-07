# ========================================= 
# Deadlock-Bots – Umstellung auf zentrale SQLite-DB
# =========================================
# Hinweis:
# - Alle Bots/Cogs nutzen nun EIN gemeinsames SQLite-File:
#   %USERPROFILE%/Documents/Deadlock/service/deadlock.sqlite3
#   (überschreibbar über Env:
#      DEADLOCK_DB_PATH  -> kompletter Dateipfad (höchste Prio)
#      DEADLOCK_DB_DIR   -> Verzeichnis, Datei heißt dann deadlock.sqlite3
#    Kompat: Wenn LIVE_DB_PATH gesetzt ist, wird es als Fallback akzeptiert.)
# - JSON-Dateien & Einzel-DBs sind entfernt. Ein einmaliges Migrations-
#   script ist enthalten (service/migrate_to_central_db.py).
# - WAL, FOREIGN_KEYS, Vacuum-on-shutdown aktiviert.
# - Python ≥ 3.10, discord.py ≥ 2.3
#
# Dateien in diesem Refactor:
#  - shared/db.py (DB-Core, Schema, Helpers, Migrations-Infra)
#  - service/migrate_to_central_db.py (Einmaliges Merge-Script JSON→DB)
#  - main_bot.py (lädt Cogs, init DB, Admin-Kommandos)
#  - cogs/tempvoice.py (TempVoice mit zentraler DB)
#  - cogs/voice_activity_tracker.py (Voice-Tracking in DB)
#  - cogs/rank_voice_manager.py (Ranked-Voice Gatekeeping)
#  - cogs/deadlock_team_balancer.py (Team Balance, nutzt ranks-Tabelle)
#  - cogs/welcome_dm.py (Onboarding/Präferenzen in DB)
#  - Standalone/rank_bot/standalone_rank_bot.py (Ranks UI + DB)
#  - live_match_master/worker nutzen dieselbe Datei via LIVE_DB_PATH
#
# =========================================
# File: shared/db.py
# =========================================

import os
import atexit
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

# ---- Env-Keys ----
ENV_DB_PATH = "DEADLOCK_DB_PATH"   # kompletter Pfad zur DB-Datei (höchste Prio)
ENV_DB_DIR  = "DEADLOCK_DB_DIR"    # nur Verzeichnis; Datei = deadlock.sqlite3
ENV_LIVE    = "LIVE_DB_PATH"       # Kompatibilität für Worker/alte Skripte

# ---- Default-Dateiname/Ort ----
DEFAULT_DIR = os.path.expandvars(r"%USERPROFILE%/Documents/Deadlock/service")
DB_NAME     = "deadlock.sqlite3"

_CONN: Optional[sqlite3.Connection] = None
_LOCK = threading.RLock()
_DB_PATH_CACHED: Optional[str] = None


def _resolve_db_path() -> str:
    """
    Ermittelt den endgültigen DB-Pfad (eine Quelle der Wahrheit).
    Prio:
      1) DEADLOCK_DB_PATH (vollständiger Pfad)
      2) LIVE_DB_PATH (vollständiger Pfad, Kompat)
      3) DEADLOCK_DB_DIR + DB_NAME
      4) DEFAULT_DIR + DB_NAME
    """
    # 1) Vollpfad via DEADLOCK_DB_PATH
    p = os.environ.get(ENV_DB_PATH)
    if p:
        return str(Path(p))

    # 2) Kompatibilität: LIVE_DB_PATH akzeptieren
    p = os.environ.get(ENV_LIVE)
    if p:
        return str(Path(p))

    # 3) Verzeichnis + Standardname
    d = os.environ.get(ENV_DB_DIR) or DEFAULT_DIR
    return str(Path(d) / DB_NAME)


def _ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def db_path() -> str:
    """Gibt den tatsächlich verwendeten DB-Pfad zurück (nach connect() garantiert gesetzt)."""
    global _DB_PATH_CACHED
    if _DB_PATH_CACHED:
        return _DB_PATH_CACHED
    # Falls connect() noch nicht aufgerufen wurde:
    _DB_PATH_CACHED = _resolve_db_path()
    return _DB_PATH_CACHED


def connect() -> sqlite3.Connection:
    """
    Stellt eine einzelne, thread-safe geteilte Verbindung her.
    Autocommit (isolation_level=None). Row-Factory = sqlite3.Row.
    Setzt außerdem LIVE_DB_PATH auf den tatsächlich verwendeten Pfad,
    damit Worker/Scripts garantiert dieselbe Datei nehmen.
    """
    global _CONN, _DB_PATH_CACHED
    if _CONN is not None:
        return _CONN

    path = _resolve_db_path()
    _ensure_parent(path)

    # Setze für alle Prozesse/Skripte sichtbar den Pfad:
    os.environ[ENV_LIVE] = path  # Worker liest LIVE_DB_PATH
    # Wenn DEADLOCK_DB_PATH nicht gesetzt war, aus Konsistenzgründen spiegeln:
    os.environ.setdefault(ENV_DB_PATH, path)

    _DB_PATH_CACHED = path

    _CONN = sqlite3.connect(
        path,
        check_same_thread=False,
        isolation_level=None,   # Autocommit
    )
    _CONN.row_factory = sqlite3.Row

    with _LOCK:
        # PRAGMAs: bewährt & traditionsgemäß solide
        _CONN.execute("PRAGMA journal_mode=WAL;")
        _CONN.execute("PRAGMA synchronous=NORMAL;")
        _CONN.execute("PRAGMA foreign_keys=ON;")
        init_schema(_CONN)

    return _CONN


def init_schema(conn: Optional[sqlite3.Connection] = None) -> None:
    """
    Legt das Schema an (idempotent). Enthält u. a.:
      - steam_links
      - live_player_state
      - live_lane_state
      - live_lane_members
      - sowie deine bestehenden Tabellen (users, ranks, temp_voice_channels, ...)
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

            -- generische KV-Ablage
            CREATE TABLE IF NOT EXISTS kv_store(
              ns TEXT NOT NULL,
              k  TEXT NOT NULL,
              v  TEXT NOT NULL,
              PRIMARY KEY(ns, k)
            );

            -- Nutzer/Guild Basis
            CREATE TABLE IF NOT EXISTS users(
              user_id    INTEGER PRIMARY KEY,
              name       TEXT,
              created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS guild_settings(
              guild_id INTEGER PRIMARY KEY,
              key      TEXT,
              value    TEXT
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
            CREATE INDEX IF NOT EXISTS idx_steam_links_user ON steam_links(user_id);

            -- Live-Player-State (Cache der letzten Steam-API-Auswertung)
            CREATE TABLE IF NOT EXISTS live_player_state(
              steam_id TEXT PRIMARY KEY,
              last_gameid TEXT,
              last_server_id TEXT,
              last_seen_ts INTEGER,
              in_deadlock_now INTEGER DEFAULT 0,
              in_match_now_strict INTEGER DEFAULT 0
            );

            -- Live-Lane-Status (pro Voice-Channel) – von LiveMatchMaster beschrieben,
            -- vom Worker gelesen zum Umbenennen
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
        # Indizes / Pragmas ggf. ergänzen
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_lls_active   ON live_lane_state(is_active)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_llm_channel  ON live_lane_members(channel_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_llm_checked  ON live_lane_members(checked_ts)")
        except Exception:
            pass


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with _LOCK:
        connect().execute(sql, params)


def executemany(sql: str, seq_of_params: Iterable[Iterable[Any]]) -> None:
    with _LOCK:
        connect().executemany(sql, list(seq_of_params))


def query_one(sql: str, params: Iterable[Any] = ()):  # -> sqlite3.Row | None
    with _LOCK:
        cur = connect().execute(sql, params)
        return cur.fetchone()


def query_all(sql: str, params: Iterable[Any] = ()):  # -> list[sqlite3.Row]
    with _LOCK:
        cur = connect().execute(sql, params)
        return cur.fetchall()


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


@atexit.register
def _vacuum_on_shutdown() -> None:
    try:
        with _LOCK:
            # Nur wenn bereits verbunden – sonst nix tun
            if _CONN is not None:
                _CONN.execute("VACUUM;")
    except Exception:
        pass
