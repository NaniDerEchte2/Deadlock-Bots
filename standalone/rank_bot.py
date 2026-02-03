import discord
from discord.ext import commands, tasks
import aiohttp
import json
import sqlite3
import asyncio
from datetime import datetime, timedelta, tzinfo
import os
import logging
import time
from pathlib import Path
import sys
import atexit
from typing import Optional, Dict, List, Any
from zoneinfo import ZoneInfo

# ---------- Repo-Root robust finden, damit "service" importierbar ist ----------
def _add_repo_root_for_imports(marker="service/db.py") -> str:
    here = Path(__file__).resolve()
    # gehe aufwärts bis wir einen Ordner finden, der marker enthält
    for parent in [here.parent] + list(here.parents):
        if (parent / marker).exists():
            if str(parent) not in sys.path:
                sys.path.insert(0, str(parent))
            return str(parent)
    # Fallback: Eltern von /standalone
    fallback = here.parent.parent
    if str(fallback) not in sys.path:
        sys.path.insert(0, str(fallback))
    return str(fallback)

REPO_ROOT = _add_repo_root_for_imports()

# zentrale DB via service.db
from service import db as central_db
from service.http_client import build_resilient_connector
DB_FILE = str(Path(central_db.db_path()))

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('StandaloneRankBot')

# Load secrets from Windows Vault
try:
    import keyring
    service_name = "DeadlockBot"
    token_val = keyring.get_password(service_name, "DISCORD_TOKEN_RANKED")
    if token_val:
        os.environ["DISCORD_TOKEN_RANKED"] = token_val
except Exception as e:
    logger.warning(f"Fehler beim Laden aus Tresor: {e}")

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Deadlock-Ränge (alle kleingeschrieben)
ranks = [
    "initiate", "seeker", "alchemist", "arcanist", "ritualist",
    "emissary", "archon", "oracle", "phantom", "ascendant", "eternus"
]

# Standard Rang-Intervalle (in Tagen)
RANK_INTERVALS = {
    "initiate": 45, "seeker": 45, "alchemist": 45, "arcanist": 45, "ritualist": 45,
    "emissary": 45, "archon": 45, "oracle": 45, "phantom": 60, "ascendant": 60, "eternus": 60
}

# Konfiguration - IDs bitte ggf. anpassen
RANK_MESSAGE_ID = 1332790527145414657
ETHERNUS_RANK_ROLE_ID = 1331458087349129296
ENGLISH_ONLY_ROLE_ID = 1309741866098491479
NO_DEADLOCK_ROLE_ID = 1397676560231698502
NO_NOTIFICATION_ROLE_ID = 1397688110959165462
PHANTOM_NOTIFICATION_CHANNEL_ID = 1374364800817303632
RANK_SELECTION_CHANNEL_ID = 1398021105339334666  # Channel für automatische View-Wiederherstellung

# Deadlock MMR Sync
MMR_API_URL = "https://api.deadlock-api.com/v1/players/mmr"
MMR_SYNC_START_HOUR = int(os.getenv("DEADLOCK_MMR_SYNC_HOUR", "5"))
MMR_SYNC_TZ = os.getenv("DEADLOCK_MMR_SYNC_TZ", "Europe/Berlin")
MMR_SYNC_MAX_RPS = float(os.getenv("DEADLOCK_MMR_MAX_RPS", "20"))
MMR_SYNC_BATCH_SIZE = int(os.getenv("DEADLOCK_MMR_BATCH_SIZE", "1000"))
MMR_SYNC_TIMEOUT = float(os.getenv("DEADLOCK_MMR_TIMEOUT", "20"))
MMR_SYNC_NS = "mmr_sync"
MMR_SYNC_LAST_RUN_KEY = "last_run_date"

# Test-User System - für normalen Betrieb leer lassen
test_users: List[discord.Member] = []

# MMR Sync Lock (verhindert parallele Läufe)
MMR_SYNC_LOCK = asyncio.Lock()

COMMAND_BOT_KEY = "rank"
COMMAND_POLL_LIMIT = 5
COMMAND_POLL_INTERVAL = 5  # seconds
STATE_PUBLISH_INTERVAL = 30  # seconds

# Deutsche Uhrzeiten (8-22 Uhr)
NOTIFICATION_START_HOUR = 8
NOTIFICATION_END_HOUR = 22

# ============= ZENTRALE DB - immer ueber service.db =============

def _vacuum_db():
    try:
        with central_db.get_conn() as conn:
            conn.execute("VACUUM")
    except Exception as e:
        logger.warning(f"Database vacuum failed: {e}")

atexit.register(_vacuum_db)


def init_database():
    """
    Initialisiert/verwaltet Tabellen innerhalb der zentralen DB.
    WICHTIG: Die DB-Datei selbst wird NICHT angelegt – sie muss existieren.
    """
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        # WAL/Sync für Skalierung (idempotent)
        cursor.execute('PRAGMA journal_mode=WAL')
        cursor.execute('PRAGMA synchronous=NORMAL')

        # Kern-Tabellen dieses Bots (Rank-System):
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_data (
                user_id TEXT PRIMARY KEY,
                custom_interval INTEGER,
                paused_until TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                rank TEXT NOT NULL,
                notification_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                count INTEGER DEFAULT 1
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                rank TEXT NOT NULL,
                queue_date TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed BOOLEAN DEFAULT FALSE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS persistent_views (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                view_type TEXT NOT NULL,
                user_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dm_response_tracking (
                user_id TEXT PRIMARY KEY,
                last_dm_sent TIMESTAMP NOT NULL,
                response_count INTEGER DEFAULT 0,
                last_response TIMESTAMP,
                status TEXT DEFAULT 'pending'
            )
        ''')

        # Kleine idempotente Migration (user_id-Spalte sicherstellen)
        try:
            cursor.execute('ALTER TABLE persistent_views ADD COLUMN user_id TEXT')
        except sqlite3.OperationalError:
            # Spalte existiert schon -> ok
            logger.debug("Migration persistent_views.user_id: bereits vorhanden")

        conn.commit()
        logger.info("✅ Zentrale DB geöffnet (rw) und Tabellen sind bereit.")

# ---------- DB Helper ----------
def get_user_data(user_id: str) -> dict:
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT custom_interval, paused_until FROM user_data WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        if result:
            custom_interval, paused_until = result
            return {'custom_interval': custom_interval, 'paused_until': paused_until}
        return {}

def save_user_data(user_id: str, data: dict):
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO user_data (user_id, custom_interval, paused_until, updated_at)
            VALUES (?, ?, ?, ?)
        ''', (user_id, data.get('custom_interval'), data.get('paused_until'), datetime.now().isoformat()))
        conn.commit()

def save_persistent_view(message_id: str, channel_id: str, guild_id: str, view_type: str, user_id: str = None):
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO persistent_views (message_id, channel_id, guild_id, view_type, user_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (message_id, channel_id, guild_id, view_type, user_id))
        conn.commit()

def remove_persistent_view(message_id: str):
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM persistent_views WHERE message_id = ?', (message_id,))
        conn.commit()

def load_persistent_views():
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(persistent_views)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'user_id' in columns:
            cursor.execute('SELECT message_id, channel_id, guild_id, view_type, user_id FROM persistent_views')
        else:
            cursor.execute('SELECT message_id, channel_id, guild_id, view_type FROM persistent_views')
            results = cursor.fetchall()
            return [(*row, None) for row in results]
        return cursor.fetchall()

def cleanup_old_views(guild_id: str, view_type: str):
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM persistent_views WHERE guild_id = ? AND view_type = ?', (guild_id, view_type))
        deleted_count = cursor.rowcount
        conn.commit()
        return deleted_count


# ---------- Standalone Dashboard Integration ----------

def fetch_pending_commands(limit: int = COMMAND_POLL_LIMIT) -> List[sqlite3.Row]:
    with central_db.get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, command, payload
              FROM standalone_commands
             WHERE bot = ?
               AND status = 'pending'
          ORDER BY id ASC
             LIMIT ?
            """,
            (COMMAND_BOT_KEY, limit),
        )
        rows = cursor.fetchall()
    return rows

def mark_command_running(command_id: int) -> bool:
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE standalone_commands
               SET status = 'running',
                   started_at = CURRENT_TIMESTAMP
             WHERE id = ?
               AND status = 'pending'
            """,
            (command_id,),
        )
        conn.commit()
        return cursor.rowcount == 1

def _truncate_error(message: Optional[str], limit: int = 1500) -> Optional[str]:
    if not message:
        return None
    text = str(message)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."

def finalize_command(command_id: int, status: str, *, result: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> None:
    result_json = json.dumps(result, ensure_ascii=False) if result is not None else None
    error_text = _truncate_error(error)
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE standalone_commands
               SET status = ?,
                   result = ?,
                   error = ?,
                   finished_at = CURRENT_TIMESTAMP
             WHERE id = ?
            """,
            (status, result_json, error_text, command_id),
        )
        conn.commit()

def _loop_running(loop_obj: Any) -> bool:
    try:
        return bool(loop_obj and loop_obj.is_running())
    except Exception:
        return False

def collect_rank_bot_snapshot() -> Dict[str, Any]:
    now = datetime.utcnow()
    today = now.strftime('%Y-%m-%d')
    snapshot: Dict[str, Any] = {
        "timestamp": now.isoformat(),
        "guild_count": len(bot.guilds),
        "test_user_count": len(test_users),
        "loops": {
            "daily_notification": _loop_running(globals().get("daily_notification_check")),
            "daily_cleanup": _loop_running(globals().get("daily_cleanup_check")),
            "daily_mmr_sync": _loop_running(globals().get("daily_mmr_sync_check")),
            "command_poller": _loop_running(globals().get("standalone_command_poller")),
            "state_publisher": _loop_running(globals().get("standalone_state_publisher")),
        },
    }

    try:
        with central_db.get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM notification_queue WHERE processed = 0")
            queue_pending_total = cursor.fetchone()[0] or 0

            cursor.execute("SELECT COUNT(*) FROM notification_queue")
            queue_total_entries = cursor.fetchone()[0] or 0

            cursor.execute(
                "SELECT COUNT(*) FROM notification_queue WHERE queue_date = ?",
                (today,),
            )
            queue_today_total = cursor.fetchone()[0] or 0

            cursor.execute(
                "SELECT COUNT(*) FROM notification_queue WHERE queue_date = ? AND processed = 0",
                (today,),
            )
            queue_today_pending = cursor.fetchone()[0] or 0

            cursor.execute(
                """
                SELECT queue_date,
                       COUNT(*) AS total,
                       SUM(CASE WHEN processed = 0 THEN 1 ELSE 0 END) AS pending
                  FROM notification_queue
              GROUP BY queue_date
              ORDER BY queue_date DESC
                 LIMIT 5
                """
            )
            queue_by_date = [
                {"date": row[0], "total": row[1], "pending": row[2] or 0}
                for row in cursor.fetchall()
                if row[0]
            ]

            cursor.execute(
                "SELECT notification_time FROM notification_log ORDER BY notification_time DESC LIMIT 1"
            )
            row = cursor.fetchone()
            last_notification = row[0] if row else None

            cursor.execute(
                "SELECT COUNT(*) FROM notification_log WHERE DATE(notification_time) = ?",
                (today,),
            )
            notifications_today = cursor.fetchone()[0] or 0

            cursor.execute("SELECT COUNT(*) FROM dm_response_tracking WHERE status = 'pending'")
            dm_pending = cursor.fetchone()[0] or 0

            cursor.execute("SELECT COUNT(*) FROM persistent_views WHERE view_type = 'dm_rank_select'")
            dm_open_views = cursor.fetchone()[0] or 0

            cursor.execute("SELECT status, COUNT(*) FROM dm_response_tracking GROUP BY status")
            dm_status = {row[0] or "unknown": row[1] for row in cursor.fetchall()}

            cursor.execute(
                "SELECT COUNT(*) FROM user_data WHERE paused_until IS NOT NULL AND paused_until > ?",
                (datetime.now().isoformat(),),
            )
            paused_users = cursor.fetchone()[0] or 0

            cursor.execute(
                """
                SELECT id, command, status, finished_at
                  FROM standalone_commands
                 WHERE bot = ?
              ORDER BY id DESC
                 LIMIT 5
                """,
                (COMMAND_BOT_KEY,),
            )
            recent_commands = [
                {
                    "id": row[0],
                    "command": row[1],
                    "status": row[2],
                    "finished_at": row[3],
                }
                for row in cursor.fetchall()
            ]

        snapshot["queue"] = {
            "pending_total": queue_pending_total,
            "total_entries": queue_total_entries,
            "today": {
                "total": queue_today_total,
                "pending": queue_today_pending,
            },
            "by_date": queue_by_date,
        }
        snapshot["notifications"] = {
            "today": notifications_today,
            "last_sent": last_notification,
        }
        snapshot["dm"] = {
            "pending": dm_pending,
            "open_views": dm_open_views,
            "statuses": dm_status,
        }
        snapshot["opt_out"] = {"paused_users": paused_users}
        snapshot["recent_commands"] = recent_commands
    except sqlite3.Error as exc:
        logger.error("Snapshot collection failed: %s", exc)

    return snapshot

def update_standalone_state(snapshot: Dict[str, Any]) -> None:
    heartbeat = int(time.time())
    payload = json.dumps(snapshot, ensure_ascii=False)
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO standalone_bot_state(bot, heartbeat, payload, updated_at)
            VALUES(?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(bot) DO UPDATE SET
                heartbeat = excluded.heartbeat,
                payload = excluded.payload,
                updated_at = CURRENT_TIMESTAMP
            """,
            (COMMAND_BOT_KEY, heartbeat, payload),
        )
        conn.commit()

def _compute_and_store_state() -> Dict[str, Any]:
    snapshot = collect_rank_bot_snapshot()
    update_standalone_state(snapshot)
    return snapshot

async def push_rank_bot_state() -> Dict[str, Any]:
    return await asyncio.to_thread(_compute_and_store_state)

def ensure_notification_tasks_running(mode: str = "normal", interval: int = 30) -> Dict[str, Any]:
    started_flags = {}
    if not daily_notification_check.is_running():
        daily_notification_check.start()
        started_flags["daily_notification"] = True
    else:
        started_flags["daily_notification"] = False

    if not daily_cleanup_check.is_running():
        daily_cleanup_check.start()
        started_flags["daily_cleanup"] = True
    else:
        started_flags["daily_cleanup"] = False

    if not daily_mmr_sync_check.is_running():
        daily_mmr_sync_check.start()
        started_flags["daily_mmr_sync"] = True
    else:
        started_flags["daily_mmr_sync"] = False

    return {
        "mode": mode,
        "interval": interval,
        "started": started_flags,
        "loops": {
            "daily_notification": daily_notification_check.is_running(),
            "daily_cleanup": daily_cleanup_check.is_running(),
            "daily_mmr_sync": daily_mmr_sync_check.is_running(),
        },
    }

def stop_notification_tasks() -> Dict[str, Any]:
    stopped_flags = {}
    if daily_notification_check.is_running():
        daily_notification_check.stop()
        stopped_flags["daily_notification"] = True
    else:
        stopped_flags["daily_notification"] = False

    if daily_cleanup_check.is_running():
        daily_cleanup_check.stop()
        stopped_flags["daily_cleanup"] = True
    else:
        stopped_flags["daily_cleanup"] = False

    if daily_mmr_sync_check.is_running():
        daily_mmr_sync_check.stop()
        stopped_flags["daily_mmr_sync"] = True
    else:
        stopped_flags["daily_mmr_sync"] = False

    return {
        "stopped": stopped_flags,
        "loops": {
            "daily_notification": daily_notification_check.is_running(),
            "daily_cleanup": daily_cleanup_check.is_running(),
            "daily_mmr_sync": daily_mmr_sync_check.is_running(),
        },
    }

async def execute_control_command(command: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = payload or {}
    normalized = command.strip().lower().lstrip('!')
    result: Dict[str, Any] = {"command": normalized}

    if normalized in {"queue.daily", "queue.create", "rqueue"}:
        await create_daily_queue()
        result["message"] = "Daily queue created"
    elif normalized in {"system.start", "rstart"}:
        mode = str(payload.get("mode", "normal"))
        interval = int(payload.get("interval", 30))
        result.update(ensure_notification_tasks_running(mode=mode, interval=interval))
        result["message"] = "Notification system running"
    elif normalized in {"system.stop", "rstop"}:
        result.update(stop_notification_tasks())
        result["message"] = "Notification system stopped"
    elif normalized in {"dm.cleanup", "cleanup.dm"}:
        removed = await cleanup_old_dm_views_auto()
        result["removed_views"] = removed
        result["message"] = "DM cleanup executed"
    elif normalized in {"state.refresh", "state"}:
        state = await push_rank_bot_state()
        result["state"] = state
        result["message"] = "State refreshed"
        return result
    else:
        raise ValueError(f"Unknown control command '{command}'")

    state = await push_rank_bot_state()
    result["state"] = state
    return result


# ---------- Utils ----------
def get_user_current_rank(user: discord.Member):
    """Ermittelt den aktuellen Rang eines Users basierend auf seinen Rollen"""
    for role in user.roles:
        role_name_lower = role.name.lower()
        if role_name_lower in ranks:
            return role_name_lower
    return None

async def remove_all_rank_roles(member: discord.Member, guild: discord.Guild):
    """Entfernt alle Rang-Rollen von einem Member"""
    for role_name in ranks:
        role = discord.utils.get(guild.roles, name=role_name.capitalize())
        if role and role in member.roles:
            try:
                await member.remove_roles(role)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("remove_all_rank_roles: konnte Rolle nicht entfernen: %s", e)

# ---------- MMR Sync ----------
STEAM_ID64_BASE = 76561197960265728

def _get_mmr_tzinfo() -> tzinfo:
    try:
        return ZoneInfo(MMR_SYNC_TZ)
    except Exception:
        logger.warning("MMR Sync: Ungueltige Zeitzone '%s' -> fallback lokal.", MMR_SYNC_TZ)
        return datetime.now().astimezone().tzinfo

def _mmr_today_str() -> str:
    tzinfo = _get_mmr_tzinfo()
    return datetime.now(tzinfo).date().isoformat()

def _mmr_should_run_now() -> bool:
    tzinfo = _get_mmr_tzinfo()
    now = datetime.now(tzinfo)
    if now.hour < MMR_SYNC_START_HOUR:
        return False
    last_run = central_db.get_kv(MMR_SYNC_NS, MMR_SYNC_LAST_RUN_KEY)
    return last_run != now.date().isoformat()

def _set_mmr_last_run(date_str: str) -> None:
    central_db.set_kv(MMR_SYNC_NS, MMR_SYNC_LAST_RUN_KEY, date_str)

def steamid64_to_account_id(steam_id64: str) -> Optional[int]:
    try:
        sid = int(str(steam_id64).strip())
    except (TypeError, ValueError):
        return None
    account_id = sid - STEAM_ID64_BASE
    if account_id <= 0:
        return None
    return account_id

def _fetch_steam_links() -> List[tuple[int, str]]:
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT user_id, steam_id
              FROM steam_links
             WHERE steam_id IS NOT NULL
               AND steam_id != ''
            """
        )
        rows = cursor.fetchall()
    links: List[tuple[int, str]] = []
    for user_id, steam_id in rows:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            continue
        if steam_id is None:
            continue
        links.append((uid, str(steam_id).strip()))
    return links

def _build_account_id_map(
    links: List[tuple[int, str]]
) -> tuple[Dict[int, List[int]], Dict[int, str]]:
    account_to_users: Dict[int, List[int]] = {}
    account_to_steam: Dict[int, str] = {}
    for user_id, steam_id in links:
        account_id = steamid64_to_account_id(steam_id)
        if account_id is None:
            logger.warning("MMR Sync: Ungueltige SteamID64: %s (user_id=%s)", steam_id, user_id)
            continue
        account_to_users.setdefault(account_id, [])
        if user_id not in account_to_users[account_id]:
            account_to_users[account_id].append(user_id)
        account_to_steam.setdefault(account_id, steam_id)
    return account_to_users, account_to_steam

def _chunked(seq: List[int], size: int) -> List[List[int]]:
    size = max(1, size)
    return [seq[i:i + size] for i in range(0, len(seq), size)]

def _entry_sort_key(entry: Dict[str, Any]) -> int:
    for key in ("start_time", "match_id"):
        value = entry.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0

def _normalize_mmr_response(data: Any) -> Dict[int, Dict[str, Any]]:
    if isinstance(data, dict) and "data" in data:
        data = data.get("data")
    if not isinstance(data, list):
        return {}
    normalized: Dict[int, Dict[str, Any]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        account_id = entry.get("account_id")
        try:
            account_id_int = int(account_id)
        except (TypeError, ValueError):
            continue
        current = normalized.get(account_id_int)
        if current is None or _entry_sort_key(entry) > _entry_sort_key(current):
            normalized[account_id_int] = entry
    return normalized

def _merge_mmr_entries(
    target: Dict[int, Dict[str, Any]],
    incoming: Dict[int, Dict[str, Any]]
) -> None:
    for account_id, entry in incoming.items():
        current = target.get(account_id)
        if current is None or _entry_sort_key(entry) > _entry_sort_key(current):
            target[account_id] = entry

def _rank_name_from_mmr(entry: Dict[str, Any]) -> Optional[str]:
    division = entry.get("division")
    if division is None:
        rank_value = entry.get("rank")
        try:
            division = int(rank_value) // 10
        except (TypeError, ValueError):
            return None
    try:
        division_int = int(division)
    except (TypeError, ValueError):
        return None
    if 0 <= division_int < len(ranks):
        return ranks[division_int]
    return None

async def _fetch_mmr_batch(
    session: aiohttp.ClientSession,
    account_ids: List[int]
) -> List[Dict[str, Any]]:
    params = {"account_ids": ",".join(str(aid) for aid in account_ids)}
    async with session.get(MMR_API_URL, params=params) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"MMR API {resp.status}: {text[:200]}")
        data = await resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        return data.get("data") or []
    return []

async def _fetch_all_mmr_entries(
    account_ids: List[int]
) -> tuple[Dict[int, Dict[str, Any]], bool]:
    if not account_ids:
        return {}, False
    entries: Dict[int, Dict[str, Any]] = {}
    had_errors = False
    connector = build_resilient_connector(limit=50, limit_per_host=10)
    timeout = aiohttp.ClientTimeout(total=MMR_SYNC_TIMEOUT)
    min_delay = 1.0 / max(MMR_SYNC_MAX_RPS, 1.0)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        batches = _chunked(account_ids, MMR_SYNC_BATCH_SIZE)
        for idx, batch in enumerate(batches, start=1):
            started = time.monotonic()
            try:
                raw = await _fetch_mmr_batch(session, batch)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError) as exc:
                had_errors = True
                logger.warning("MMR Sync: Batch %s/%s fehlgeschlagen: %s", idx, len(batches), exc)
            else:
                normalized = _normalize_mmr_response(raw)
                _merge_mmr_entries(entries, normalized)

            elapsed = time.monotonic() - started
            delay = max(0.0, min_delay - elapsed)
            if delay:
                await asyncio.sleep(delay)

    return entries, had_errors

async def _apply_rank_to_member(
    member: discord.Member,
    guild: discord.Guild,
    rank_name: str,
    *,
    reason: str
) -> bool:
    if rank_name not in ranks:
        return False
    current_rank = get_user_current_rank(member)
    if current_rank == rank_name:
        return False

    role = discord.utils.get(guild.roles, name=rank_name.capitalize())
    if not role:
        try:
            role = await guild.create_role(name=rank_name.capitalize(), reason=reason)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("MMR Sync: Rolle %s konnte nicht erstellt werden: %s", rank_name, exc)
            return False

    try:
        if role not in member.roles:
            await member.add_roles(role, reason=reason)
    except (discord.Forbidden, discord.HTTPException) as exc:
        logger.warning("MMR Sync: Konnte Rolle %s bei %s nicht hinzufuegen: %s", rank_name, member.id, exc)
        return False

    for role_name in ranks:
        if role_name == rank_name:
            continue
        role_to_remove = discord.utils.get(guild.roles, name=role_name.capitalize())
        if role_to_remove and role_to_remove in member.roles:
            try:
                await member.remove_roles(role_to_remove, reason=reason)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("MMR Sync: Entfernen von %s bei %s fehlgeschlagen: %s", role_name, member.id, exc)
    return True

async def sync_mmr_roles(
    *,
    only_steam_ids: Optional[List[str]] = None,
    dry_run: bool = False,
    update_last_run: bool = False
) -> Dict[str, Any]:
    summary = {
        "checked_links": 0,
        "accounts_requested": 0,
        "entries_received": 0,
        "users_total": 0,
        "members_updated": 0,
        "members_skipped": 0,
        "missing_mmr": 0,
        "missing_rank": 0,
        "missing_member": 0,
        "dry_run": dry_run,
    }

    links = _fetch_steam_links()
    if only_steam_ids:
        filtered = []
        allowed = {str(sid).strip() for sid in only_steam_ids if sid}
        for user_id, steam_id in links:
            if steam_id in allowed:
                filtered.append((user_id, steam_id))
        links = filtered

    summary["checked_links"] = len(links)
    account_to_users, account_to_steam = _build_account_id_map(links)
    account_ids = list(account_to_users.keys())
    summary["accounts_requested"] = len(account_ids)

    if not account_ids:
        return summary

    mmr_entries, had_errors = await _fetch_all_mmr_entries(account_ids)
    summary["entries_received"] = len(mmr_entries)

    reason = "Deadlock MMR Sync"
    for account_id, user_ids in account_to_users.items():
        entry = mmr_entries.get(account_id)
        if not entry:
            summary["missing_mmr"] += len(user_ids)
            continue

        rank_name = _rank_name_from_mmr(entry)
        if not rank_name:
            summary["missing_rank"] += len(user_ids)
            continue

        for user_id in user_ids:
            summary["users_total"] += 1
            member_found = False
            for guild in bot.guilds:
                member = guild.get_member(int(user_id))
                if not member:
                    continue
                member_found = True
                if dry_run:
                    summary["members_skipped"] += 1
                    logger.info(
                        "MMR Sync (dry): user_id=%s steam_id=%s -> %s",
                        user_id,
                        account_to_steam.get(account_id, "unknown"),
                        rank_name,
                    )
                    continue

                updated = await _apply_rank_to_member(member, guild, rank_name, reason=reason)
                if updated:
                    summary["members_updated"] += 1
                else:
                    summary["members_skipped"] += 1

            if not member_found:
                summary["missing_member"] += 1

    if update_last_run and not had_errors:
        _set_mmr_last_run(_mmr_today_str())
    elif update_last_run and had_errors:
        logger.warning("MMR Sync: Fehler aufgetreten, last_run wird nicht gesetzt.")

    return summary

def track_dm_sent(user_id: str):
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO dm_response_tracking 
            (user_id, last_dm_sent, response_count, status)
            VALUES (?, ?, COALESCE((SELECT response_count FROM dm_response_tracking WHERE user_id = ?), 0), 'pending')
        ''', (user_id, datetime.now().isoformat(), user_id))
        conn.commit()

def track_dm_response(user_id: str):
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE dm_response_tracking 
            SET response_count = response_count + 1, 
                last_response = ?, 
                status = 'responded'
            WHERE user_id = ?
        ''', (datetime.now().isoformat(), user_id))
        conn.commit()

# ---------- Views ----------
class RankSelectView(discord.ui.View):
    def __init__(self, user_id: int, guild_id: int, persistent=False):
        super().__init__(timeout=None if persistent else 900)
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        self.persistent = persistent
        self.add_item(RankSelectDropdown(user_id, guild_id))
        self.add_item(IntervalSelectDropdown(user_id))
        self.add_item(NoNotificationButton(user_id, guild_id))
        self.add_item(NoDeadlockButton(user_id, guild_id))
        self.add_item(FinishedButton(user_id, guild_id))

class RankSelectDropdown(discord.ui.Select):
    def __init__(self, user_id: int, guild_id: int):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)

        options = [
            discord.SelectOption(label=rank.capitalize(), value=rank, description=f"Setze {rank.capitalize()} als deinen Rang")
            for rank in ranks
        ]
        super().__init__(
            placeholder="🎮 Wähle deinen aktuellen Deadlock-Rang...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="dm_rank_select"
        )

    async def callback(self, interaction: discord.Interaction):
        selected_rank = self.values[0]
        guild = bot.get_guild(self.guild_id)
        member = guild.get_member(self.user_id) if guild else None

        if not guild or not member:
            await interaction.response.send_message("❌ Fehler: Server oder User nicht gefunden.", ephemeral=True)
            return

        await remove_all_rank_roles(member, guild)

        role = discord.utils.get(guild.roles, name=selected_rank.capitalize())
        if not role:
            role = await guild.create_role(name=selected_rank.capitalize())
        await member.add_roles(role)

        # Phantom+ Benachrichtigung
        if selected_rank in ["phantom", "ascendant", "eternus"]:
            notification_channel = guild.get_channel(PHANTOM_NOTIFICATION_CHANNEL_ID)
            if notification_channel:
                rank_emoji = discord.utils.get(guild.emojis, name=selected_rank)
                emoji_display = str(rank_emoji) if rank_emoji else ""
                notification_embed = discord.Embed(
                    title="🔥 Phantom+ Rang Update",
                    description=f"{emoji_display} **{member.display_name}** hat sich den Rang **{selected_rank.capitalize()}** gegeben!",
                    color=0xff6b35
                )
                notification_embed.add_field(name="User", value=f"{member.mention} ({member.id})", inline=True)
                notification_embed.add_field(name="Rang", value=f"{emoji_display} {selected_rank.capitalize()}", inline=True)
                notification_embed.timestamp = datetime.now()
                try:
                    await notification_channel.send(embed=notification_embed)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # Nur Info – Benachrichtigung ist optional
                    logger.info("Konnte Phantom+-Benachrichtigung nicht senden: %r", e, exc_info=True)

        rank_emoji = discord.utils.get(guild.emojis, name=selected_rank)
        await interaction.response.send_message(
            f"✅ {rank_emoji or ''} Rang erfolgreich auf **{selected_rank.capitalize()}** gesetzt!",
            ephemeral=True
        )

class IntervalSelectDropdown(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = int(user_id)
        options = [
            discord.SelectOption(label="30 Tage", value="30", description="Alle 30 Tage nach Rang fragen", emoji="📅"),
            discord.SelectOption(label="45 Tage", value="45", description="Alle 45 Tage nach Rang fragen", emoji="📆"),
            discord.SelectOption(label="60 Tage", value="60", description="Alle 60 Tage nach Rang fragen", emoji="🗓️"),
            discord.SelectOption(label="90 Tage", value="90", description="Alle 90 Tage nach Rang fragen", emoji="📋"),
        ]
        super().__init__(
            placeholder="⏰ Wähle dein Benachrichtigungs-Intervall...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="interval_select"
        )

    async def callback(self, interaction: discord.Interaction):
        selected_interval = int(self.values[0])
        user_id = str(self.user_id)
        user_data = get_user_data(user_id)
        user_data['custom_interval'] = selected_interval
        save_user_data(user_id, user_data)
        await interaction.response.send_message(
            f"⏰ Benachrichtigungs-Intervall auf **{selected_interval} Tage** gesetzt!",
            ephemeral=True
        )

class NoNotificationButton(discord.ui.Button):
    def __init__(self, user_id: int, guild_id: int):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        super().__init__(style=discord.ButtonStyle.secondary, label="Keine Benachrichtigungen mehr", emoji="⏸️", custom_id="no_notification_btn")

    async def callback(self, interaction: discord.Interaction):
        try:
            guild = bot.get_guild(self.guild_id)
            member = guild.get_member(self.user_id) if guild else None
            if not guild or not member:
                await interaction.response.send_message("❌ Fehler: Server oder User nicht gefunden.", ephemeral=True)
                return
            role = discord.utils.get(guild.roles, id=NO_NOTIFICATION_ROLE_ID)
            if role:
                await member.add_roles(role)

            embed = discord.Embed(
                title="⏸️ Benachrichtigungen deaktiviert",
                description="Du wirst nicht mehr nach deinem Rang gefragt.\n\nDein Rang bleibt erhalten. Du kannst ihn jederzeit im Rang-Kanal ändern!",
                color=0xffaa00
            )
            await interaction.response.edit_message(embed=embed, view=None)
            track_dm_response(str(interaction.user.id))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Error in NoNotificationButton callback: {e}")
            try:
                await interaction.response.send_message("❌ Fehler beim Deaktivieren der Benachrichtigungen.", ephemeral=True)
            except asyncio.CancelledError:
                raise
            except Exception as e2:
                logger.debug("Followup send after error failed: %r", e2)

        try:
            with central_db.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM persistent_views WHERE message_id = ? AND view_type = ?', (str(interaction.message.id), 'dm_rank_select'))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to remove DM view from database: {e}")

        await asyncio.sleep(300)
        try:
            await interaction.delete_original_response()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("delete_original_response (NoNotification) failed: %r", e)

class NoDeadlockButton(discord.ui.Button):
    def __init__(self, user_id: int, guild_id: int):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        super().__init__(style=discord.ButtonStyle.danger, label="Spiele kein Deadlock mehr", emoji="🚫", custom_id="no_deadlock_btn")

    async def callback(self, interaction: discord.Interaction):
        try:
            guild = bot.get_guild(self.guild_id)
            member = guild.get_member(self.user_id) if guild else None
            if not guild or not member:
                await interaction.response.send_message("❌ Fehler: Server oder User nicht gefunden.", ephemeral=True)
                return
            await remove_all_rank_roles(member, guild)
            role = discord.utils.get(guild.roles, id=NO_DEADLOCK_ROLE_ID)
            if role:
                await member.add_roles(role)

            embed = discord.Embed(
                title="🚫 Kein Deadlock mehr",
                description="Du wirst nicht mehr nach deinem Rang gefragt.\n\nFalls du wieder anfängst zu spielen, kannst du deine Rolle jederzeit im Rang-Kanal ändern!",
                color=0xff0000
            )
            await interaction.response.edit_message(embed=embed, view=None)
            track_dm_response(str(interaction.user.id))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Error in NoDeadlockButton callback: {e}")
            try:
                await interaction.response.send_message("❌ Ein Fehler ist aufgetreten.", ephemeral=True)
            except asyncio.CancelledError:
                raise
            except Exception as e2:
                logger.debug("Followup send after error failed: %r", e2)

        try:
            with central_db.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM persistent_views WHERE message_id = ? AND view_type = ?', (str(interaction.message.id), 'dm_rank_select'))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to remove DM view from database: {e}")

        await asyncio.sleep(300)
        try:
            await interaction.delete_original_response()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("delete_original_response (NoDeadlock) failed: %r", e)

class FinishedButton(discord.ui.Button):
    def __init__(self, user_id: int, guild_id: int):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        super().__init__(style=discord.ButtonStyle.success, label="Fertig", emoji="✅", custom_id="finished_btn")

    async def callback(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="✅ Einstellungen gespeichert!",
            description="Deine Rang- und Intervall-Einstellungen wurden erfolgreich gespeichert.",
            color=0x00ff00
        )
        await interaction.response.edit_message(embed=embed, view=None)
        track_dm_response(str(interaction.user.id))
        try:
            with central_db.get_conn() as conn:
                cur = conn.cursor()
                cur.execute('DELETE FROM persistent_views WHERE message_id = ? AND view_type = ?', (str(interaction.message.id), 'dm_rank_select'))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to remove persistent view: {e}")
        await asyncio.sleep(30)
        try:
            await interaction.delete_original_response()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("delete_original_response (Finished) failed: %r", e)

# Server-Rang-Auswahl
class ServerRankSelectView(discord.ui.View):
    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=None)
        self.guild = guild
        self.add_item(ServerRankSelectDropdown(guild))

class ServerRankSelectDropdown(discord.ui.Select):
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        options = []
        for rank in ranks:
            emoji = discord.utils.get(guild.emojis, name=rank)
            options.append(discord.SelectOption(
                label=rank.capitalize(),
                value=rank,
                description=f"Setze {rank.capitalize()} als deinen Rang",
                emoji=emoji
            ))
        super().__init__(placeholder="🎮 Wähle deinen Deadlock-Rang...", min_values=1, max_values=1, options=options, custom_id="server_rank_select")

    async def callback(self, interaction: discord.Interaction):
        selected_rank = self.values[0]
        member = interaction.user
        await remove_all_rank_roles(member, self.guild)
        role = discord.utils.get(self.guild.roles, name=selected_rank.capitalize())
        if not role:
            role = await self.guild.create_role(name=selected_rank.capitalize())
        await member.add_roles(role)

        if selected_rank in ["phantom", "ascendant", "eternus"]:
            notification_channel = self.guild.get_channel(PHANTOM_NOTIFICATION_CHANNEL_ID)
            if notification_channel:
                rank_emoji = discord.utils.get(self.guild.emojis, name=selected_rank)
                emoji_display = str(rank_emoji) if rank_emoji else ""
                embed = discord.Embed(
                    title="🔥 Phantom+ Rang Update",
                    description=f"{emoji_display} **{member.display_name}** hat sich den Rang **{selected_rank.capitalize()}** gegeben!",
                    color=0xff6b35
                )
                embed.add_field(name="User", value=f"{member.mention} ({member.id})", inline=True)
                embed.add_field(name="Rang", value=f"{emoji_display} {selected_rank.capitalize()}", inline=True)
                embed.timestamp = datetime.now()
                try:
                    await notification_channel.send(embed=embed)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.info("Konnte Phantom+-Benachrichtigung nicht senden: %r", e, exc_info=True)

        rank_emoji = discord.utils.get(self.guild.emojis, name=selected_rank)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"✅ {rank_emoji or ''} Dein Rang wurde erfolgreich auf **{selected_rank.capitalize()}** gesetzt!",
                    ephemeral=True
                )
        except (discord.NotFound, discord.HTTPException) as e:
            logger.debug("Interaction response send failed (likely timeout/deletion): %r", e)

        track_dm_response(str(interaction.user.id))

# ---------- Persistente Views wiederherstellen ----------
async def restore_persistent_views():
    """Restore persistent views after bot is ready"""
    logger.info("Restoring persistent views...")
    persistent_views = load_persistent_views()

    for message_id, channel_id, guild_id, view_type, user_id in persistent_views:
        try:
            guild = bot.get_guild(int(guild_id))
            if not guild:
                logger.warning(f"Guild {guild_id} not found for persistent view")
                continue

            if view_type == 'server_rank_select':
                view = ServerRankSelectView(guild)
                bot.add_view(view, message_id=int(message_id))
                logger.info(f"Re-registered ServerRankSelectView for message {message_id}")

            elif view_type == 'dm_rank_select':
                # DM View wiederherstellen
                if user_id:
                    try:
                        view = RankSelectView(int(user_id), int(guild_id), persistent=True)
                        bot.add_view(view, message_id=int(message_id))
                        logger.info(f"Re-registered DM RankSelectView for user {user_id} (message {message_id})")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"Failed to restore DM view for user {user_id}: {e}")
                else:
                    # Legacy ohne user_id -> Fallback-View (best effort)
                    try:
                        view = RankSelectView(0, int(guild_id), persistent=True)
                        bot.add_view(view, message_id=int(message_id))
                        logger.info(f"Re-registered fallback DM RankSelectView for message {message_id}")
                    except asyncio.CancelledError:
                        raise
                    except Exception as fallback_e:
                        logger.error(f"Fallback restoration failed for {message_id}: {fallback_e}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Failed to restore persistent view {message_id}: {e}")

    logger.info("Persistent views restoration completed")

# ---------- DM Helper ----------
async def get_existing_dm_view(user_id: str) -> Optional[Dict[str, Any]]:
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT message_id, channel_id FROM persistent_views WHERE user_id = ? AND view_type = ? LIMIT 1',
                       (user_id, 'dm_rank_select'))
        result = cursor.fetchone()

        if result:
            message_id, channel_id = result
            try:
                channel = bot.get_channel(int(channel_id))
                if channel:
                    message = await channel.fetch_message(int(message_id))
                    if message:
                        logger.info(f"Found existing DM view for user {user_id}: message {message_id}")
                        return {'message': message, 'message_id': message_id, 'channel_id': channel_id}
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Existing DM message {message_id} no longer accessible: {e}")
                cursor.execute('DELETE FROM persistent_views WHERE message_id = ?', (message_id,))
                conn.commit()
        return None

async def cleanup_old_dm_views(user_id: str):
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT message_id, channel_id FROM persistent_views WHERE user_id = ? AND view_type = ?',
                       (user_id, 'dm_rank_select'))
        old_views = cursor.fetchall()

    # Nachrichten löschen (asynchron) – DB Cleanup danach
    for message_id, channel_id in old_views:
        try:
            channel = bot.get_channel(int(channel_id))
            if channel:
                message = await channel.fetch_message(int(message_id))
                if message:
                    await message.delete()
                    logger.info(f"Deleted old DM view message {message_id} for user {user_id}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Could not delete old DM message {message_id}: {e}")

    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM persistent_views WHERE user_id = ? AND view_type = ?',
                       (user_id, 'dm_rank_select'))
        conn.commit()

    if old_views:
        logger.info(f"Cleaned up {len(old_views)} old DM views for user {user_id}")

async def cleanup_old_dm_views_auto() -> int:
    """Automatisches Cleanup von DM Views älter als 7 Tage"""
    cutoff_date = (datetime.now() - timedelta(days=7)).isoformat()
    cleaned_count = 0

    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT message_id, channel_id, user_id 
            FROM persistent_views 
            WHERE view_type = 'dm_rank_select' 
              AND created_at < ?
        ''', (cutoff_date,))
        old_views = cursor.fetchall()

    for message_id, channel_id, user_id in old_views:
        try:
            channel = bot.get_channel(int(channel_id))
            if channel:
                message = await channel.fetch_message(int(message_id))
                if message:
                    await message.delete()
                    logger.info(f"Auto-deleted old DM view {message_id} for user {user_id}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Could not delete old DM message {message_id}: {e}")
        cleaned_count += 1

    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM persistent_views WHERE view_type = ? AND created_at < ?',
                       ('dm_rank_select', cutoff_date))
        cursor.execute('''
            UPDATE dm_response_tracking 
               SET status = 'dropped_no_response'
             WHERE last_dm_sent < ? AND status = 'pending'
        ''', (cutoff_date,))
        conn.commit()

    if cleaned_count > 0:
        logger.info(f"Auto-cleanup: Removed {cleaned_count} old DM views (7+ days old)")
    return cleaned_count

# ---------- Rank-Update Nachricht ----------
async def ask_rank_update(member: discord.Member, current_rank: str, guild: discord.Guild):
    try:
        existing_dm_info = await get_existing_dm_view(str(member.id))
        user_data = get_user_data(str(member.id))
        custom_interval = user_data.get('custom_interval')
        interval_days = custom_interval if custom_interval else RANK_INTERVALS.get(current_rank, 30)

        if current_rank == "unranked":
            embed = discord.Embed(
                title="🎯 Willkommen zum Deadlock Rank Bot!",
                description=f"Hey {member.display_name} :)\n\nIch bin der **Deadlock Rank Bot** und möchte mal nett nachfragen, welchen Rang du aktuell hast :)\n\n🆕 **Du hast noch keinen Rang im Server!**\n\nWähle unten aus den Dropdown-Menüs deinen aktuellen Deadlock-Rang und dein gewünschtes Benachrichtigungs-Intervall aus!",
                color=0x7289DA
            )
        else:
            current_emoji = discord.utils.get(guild.emojis, name=current_rank)
            emoji_display = str(current_emoji) if current_emoji else ""
            embed = discord.Embed(
                title="🎯 Deadlock Rang-Update",
                description=f"Hey {member.display_name} :)\n\nIch bin der **Deadlock Rank Bot** und möchte mal nett nachfragen, welchen Rang du aktuell hast :)\n\n{emoji_display} **Dein aktueller Rang: {current_rank.capitalize()}**\n\nWähle unten aus den Dropdown-Menüs deinen aktuellen Rang und dein gewünschtes Benachrichtigungs-Intervall aus!",
                color=0x7289DA
            )

        embed.add_field(name="⏰ Aktuelles Intervall", value=f"{interval_days} Tage", inline=True)
        embed.add_field(name="📋 Bitte ehrlich sein", value="Gib deinen **tatsächlichen** Rang an! Bei schwankenden Rängen wähle den, in dem du die meiste Zeit verbringst.", inline=False)
        embed.add_field(name="🔗 Alternative", value="Du kannst deinen Rang auch direkt im Server ändern:\nhttps://discord.com/channels/1289721245281292288/1398021105339334666/1398062470244995267", inline=False)
        embed.set_footer(text="🎮 Deadlock Rank Bot")

        view = RankSelectView(member.id, guild.id, persistent=True)

        if existing_dm_info:
            existing_message = existing_dm_info['message']
            await existing_message.edit(embed=embed, view=view)
            logger.info(f"Refreshed existing DM view for {member.display_name}")
            bot.add_view(view, message_id=int(existing_dm_info['message_id']))
        else:
            message = await member.send(embed=embed, view=view)
            logger.info(f"Sent new DM view to {member.display_name}")
            save_persistent_view(str(message.id), str(message.channel.id), str(guild.id), 'dm_rank_select', str(member.id))

        track_dm_sent(str(member.id))

    except discord.Forbidden as e:
        logger.warning(f"Could not send DM to {member.display_name} ({member.id}): {e}")
    except Exception as e:
        logger.error("ask_rank_update unexpected error: %r", e)

# ---------- Auto-Restore im Rang-Kanal ----------
async def create_rank_selection_message(channel: discord.TextChannel, guild: discord.Guild) -> Optional[discord.Message]:
    try:
        embed = discord.Embed(
            title="🎯 Deadlock Rang-Auswahl",
            description="Wähle deinen aktuellen Deadlock-Rang aus dem Dropdown-Menü.\n\nDie Auswahl ist nur für dich sichtbar und wird automatisch als Rolle zugewiesen.",
            color=0x7289DA
        )
        embed.add_field(name="📋 Hinweise", value="• Wähle deinen **tatsächlichen** Rang\n• Bei schwankenden Rängen: Den wo du die meiste Zeit verbringst\n• Die Auswahl ist **nur für dich sichtbar**", inline=False)
        embed.set_footer(text="🎮 Deadlock Rank Bot - Auto-Wiederhergestellt")

        view = ServerRankSelectView(guild)
        message = await channel.send(embed=embed, view=view)
        save_persistent_view(str(message.id), str(channel.id), str(guild.id), 'server_rank_select')
        logger.info(f"[AUTO RESTORE] Created new rank selection message {message.id}")
        return message
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[AUTO RESTORE] Error creating new rank selection message: {e}")
        return None

async def auto_restore_rank_channel_view() -> None:
    try:
        channel = bot.get_channel(RANK_SELECTION_CHANNEL_ID)
        if not channel:
            logger.error(f"Rank selection channel {RANK_SELECTION_CHANNEL_ID} not found")
            return

        guild = channel.guild
        logger.info(f"[AUTO RESTORE] Checking rank channel: #{channel.name}")

        bot_messages: List[discord.Message] = []
        async for message in channel.history(limit=50):
            if message.author == bot.user:
                bot_messages.append(message)

        logger.info(f"[AUTO RESTORE] Found {len(bot_messages)} bot messages in rank channel")

        if not bot_messages:
            logger.info("[AUTO RESTORE] No bot messages found - creating new rank selection message")
            await create_rank_selection_message(channel, guild)
            return

        latest_message = bot_messages[0]
        logger.info(f"[AUTO RESTORE] Latest bot message ID: {latest_message.id}")

        if latest_message.embeds:
            embed = latest_message.embeds[0]
            if "Rang-Auswahl" in embed.title or "Deadlock-Rang" in str(embed.description):
                logger.info(f"[AUTO RESTORE] Found rank selection message - attaching view")
                view = ServerRankSelectView(guild)
                try:
                    await latest_message.edit(embed=embed, view=view)
                    save_persistent_view(str(latest_message.id), str(channel.id), str(guild.id), 'server_rank_select')
                    logger.info(f"[AUTO RESTORE] Successfully restored view to message {latest_message.id}")
                except asyncio.CancelledError:
                    raise
                except discord.NotFound:
                    logger.warning(f"[AUTO RESTORE] Message {latest_message.id} not found - creating new one")
                    await create_rank_selection_message(channel, guild)
                except Exception as e:
                    logger.error(f"[AUTO RESTORE] Error attaching view: {e}")
                    await create_rank_selection_message(channel, guild)
            else:
                logger.info("[AUTO RESTORE] Latest message is not a rank selection - creating new one")
                await create_rank_selection_message(channel, guild)
        else:
            logger.info("[AUTO RESTORE] Latest message has no embeds - creating new rank selection")
            await create_rank_selection_message(channel, guild)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"[AUTO RESTORE] Error in auto restore: {e}")

# ---------- Commands ----------
@bot.command(name='rsetup')
@commands.has_permissions(administrator=True)
async def setup_rank_roles(ctx: commands.Context):
    removed_count = cleanup_old_views(str(ctx.guild.id), 'server_rank_select')

    embed = discord.Embed(
        title="🎯 Deadlock Rang-Auswahl",
        description="Wähle deinen aktuellen Deadlock-Rang aus dem Dropdown-Menü.\n\nDie Auswahl ist nur für dich sichtbar und wird automatisch als Rolle zugewiesen.",
        color=0x7289DA
    )
    embed.add_field(name="📋 Hinweise", value="• Wähle deinen **tatsächlichen** Rang\n• Bei schwankenden Rängen: Den wo du die meiste Zeit verbringst\n• Die Auswahl ist **nur für dich sichtbar**", inline=False)
    embed.set_footer(text="🎮 Deadlock Rank Bot")

    view = ServerRankSelectView(ctx.guild)
    message = await ctx.send(embed=embed, view=view)

    save_persistent_view(str(message.id), str(ctx.channel.id), str(ctx.guild.id), 'server_rank_select')

    global RANK_MESSAGE_ID
    RANK_MESSAGE_ID = message.id

    confirm = discord.Embed(
        title="✅ Rang-Auswahl erstellt!",
        description=f"Dropdown-Menü wurde erfolgreich erstellt.\nMessage-ID: {message.id}",
        color=0x00ff00
    )
    if removed_count > 0:
        confirm.add_field(name="🧹 Cleanup", value=f"{removed_count} alte View(s) automatisch entfernt", inline=False)
    confirm.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=confirm)

@bot.command(name='rtest', aliases=['test_rank_message'])
@commands.has_permissions(administrator=True)
async def test_rank_message(ctx: commands.Context, user: discord.Member = None):
    guild = ctx.guild
    test_user = user or (test_users[0] if test_users else None)
    if not test_user:
        embed = discord.Embed(
            title="❌ Keine Test-User",
            description="Keine Test-User gesetzt! Verwende `!rtest_users @user1 @user2 @user3`",
            color=0xff0000
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return

    current_rank = get_user_current_rank(test_user)
    if not current_rank:
        embed = discord.Embed(
            title="❌ Kein Rang gefunden",
            description=f"User {test_user.mention} hat keinen Rang!\nVerfügbare Rollen: {[role.name for role in test_user.roles]}",
            color=0xff0000
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return

    await ask_rank_update(test_user, current_rank, guild)
    done = discord.Embed(
        title="✅ Test-Nachricht gesendet!",
        description=f"Rang-Update-Nachricht an {test_user.mention} gesendet.",
        color=0x00ff00
    )
    done.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=done)

@bot.command(name='rtest_users')
@commands.has_permissions(administrator=True)
async def set_test_users(ctx: commands.Context, *users: discord.Member):
    global test_users
    if not users:
        embed = discord.Embed(
            title="❌ Keine User angegeben",
            description="Verwende: `!rtest_users @user1 @user2 @user3`",
            color=0xff0000
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return

    test_users = list(users)
    embed = discord.Embed(
        title="✅ Test-User gesetzt!",
        description=f"**{len(test_users)}** Test-User wurden gesetzt:",
        color=0x00ff00
    )
    user_list = []
    for user in test_users:
        current_rank = get_user_current_rank(user)
        user_list.append(f"{user.mention} - Rang: {current_rank or 'Kein Rang'}")
    embed.add_field(name="👥 Test-User", value="\n".join(user_list), inline=False)
    embed.add_field(name="ℹ️ Info", value="Diese User werden beim Daily-Check benachrichtigt", inline=False)
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)

@bot.command(name='rqueue')
@commands.has_permissions(administrator=True)
async def create_queue_manually(ctx: commands.Context):
    msg = await ctx.send(embed=discord.Embed(title="🔄 Queue wird erstellt...", description="Erstelle Benachrichtigungs-Queue für heute", color=0xffaa00))
    await create_daily_queue()
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM notification_queue WHERE queue_date = ?', (datetime.now().strftime('%Y-%m-%d'),))
        queue_count = cursor.fetchone()[0]
    final = discord.Embed(title="✅ Queue erstellt!", description=f"**{queue_count}** User in der heutigen Queue", color=0x00ff00)
    final.add_field(name="📋 Anzeigen", value="Verwende `!rdb queue` um die Queue anzuzeigen", inline=False)
    final.set_footer(text="🎮 Deadlock Rank Bot")
    await msg.edit(embed=final)

@bot.command(name='rqueue_remaining', aliases=['rqr'])
@commands.has_permissions(administrator=True)
async def create_remaining_queue(ctx: commands.Context):
    msg = await ctx.send(embed=discord.Embed(title="🔄 Remaining Queue wird erstellt...", description="Erstelle Queue nur mit noch nicht verarbeiteten Usern", color=0xffaa00))
    today = datetime.now().strftime('%Y-%m-%d')
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, guild_id, rank FROM notification_queue WHERE queue_date = ? AND processed = FALSE', (today,))
        remaining_users = cursor.fetchall()

    if not remaining_users:
        await msg.edit(embed=discord.Embed(title="ℹ️ Keine verbleibenden User", description="Alle User aus der heutigen Queue wurden bereits verarbeitet!", color=0x0099ff))
        return

    new_queue_data = [(user_id, guild_id, rank, today, False) for user_id, guild_id, rank in remaining_users]
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM notification_queue WHERE queue_date = ?', (today,))
        cursor.executemany('''
            INSERT INTO notification_queue (user_id, guild_id, rank, queue_date, processed)
            VALUES (?, ?, ?, ?, ?)
        ''', new_queue_data)
        conn.commit()

    final = discord.Embed(title="✅ Remaining Queue erstellt!", description=f"**{len(new_queue_data)}** noch nicht verarbeitete User in der neuen Queue", color=0x00ff00)
    final.add_field(name="📋 Anzeigen", value="Verwende `!rdb queue` um die Queue anzuzeigen", inline=False)
    final.set_footer(text="🎮 Deadlock Rank Bot")
    await msg.edit(embed=final)

@bot.command(name='rqueue_never_contacted')
@commands.has_permissions(administrator=True)
async def create_never_contacted_queue(ctx: commands.Context):
    msg = await ctx.send(embed=discord.Embed(title="🔍 Suche nach noch nie kontaktierten Usern...", description="Erstelle Queue mit Usern die noch keine DM erhalten haben (30 Tage Wartezeit)", color=0xffaa00))
    guild = ctx.guild
    today = datetime.now().strftime('%Y-%m-%d')
    all_members = [m for m in guild.members if not m.bot]

    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT user_id FROM notification_log')
        contacted_users = {row[0] for row in cursor.fetchall()}

    never_contacted = []
    for member in all_members:
        if str(member.id) not in contacted_users:
            user_rank = "unranked"
            for rank in ranks:
                role = discord.utils.get(guild.roles, name=rank.capitalize())
                if role and role in member.roles:
                    user_rank = rank
                    break
            never_contacted.append((str(member.id), str(guild.id), user_rank, today, False))

    if not never_contacted:
        await msg.edit(embed=discord.Embed(title="ℹ️ Alle User bereits kontaktiert", description="Alle User mit Rang-Rollen haben bereits mindestens eine DM erhalten!", color=0x0099ff))
        return

    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM notification_queue WHERE queue_date = ?', (today,))
        cursor.executemany('''
            INSERT INTO notification_queue (user_id, guild_id, rank, queue_date, processed)
            VALUES (?, ?, ?, ?, ?)
        ''', never_contacted)
        conn.commit()

    final = discord.Embed(
        title="✅ Queue für noch nie kontaktierte User erstellt!",
        description=f"**{len(never_contacted)}** User werden heute kontaktiert",
        color=0x00ff00
    )
    final.add_field(name="📋 Nächste Schritte", value="• `!rdb queue` - Queue anzeigen\n• `!rstart` - Benachrichtigungen starten", inline=False)
    final.set_footer(text="🎮 Deadlock Rank Bot")
    await msg.edit(embed=final)

@bot.command(name='rcheck_never_contacted')
@commands.has_permissions(administrator=True)
async def check_never_contacted(ctx: commands.Context):
    guild = ctx.guild
    all_server_users = set()
    user_mapping = {}
    for member in guild.members:
        if not member.bot:
            all_server_users.add(str(member.id))
            user_rank = "unranked"
            for rank in ranks:
                role = discord.utils.get(guild.roles, name=rank.capitalize())
                if role and role in member.roles:
                    user_rank = rank
                    break
            user_mapping[str(member.id)] = (member, user_rank)

    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT user_id FROM notification_log')
        contacted_users = {row[0] for row in cursor.fetchall()}

    never_contacted = all_server_users - contacted_users

    embed = discord.Embed(title="🔍 User-Kontakt Analyse", description="Analyse aller Server-User (außer Bots)", color=0x0099ff)
    embed.add_field(
        name="📊 Statistiken",
        value=f"**Gesamt Server-User**: {len(all_server_users)}\n"
              f"**Bereits kontaktiert**: {len(contacted_users)}\n"
              f"**Noch nie kontaktiert**: {len(never_contacted)}",
        inline=False
    )
    if never_contacted:
        sample_users = []
        for user_id in list(never_contacted)[:10]:
            if user_id in user_mapping:
                member, rank = user_mapping[user_id]
                rank_display = rank.capitalize() if rank != "unranked" else "Kein Rang"
                sample_users.append(f"• **{member.display_name}** ({rank_display})")
        embed.add_field(name="👥 Beispiele nie kontaktierter User", value="\n".join(sample_users) + (f"\n... und {len(never_contacted)-10} weitere" if len(never_contacted) > 10 else ""), inline=False)
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)


@bot.command(name='rstart')
@commands.has_permissions(administrator=True)
async def start_notification_system(ctx: commands.Context, mode: str = "normal", interval: int = 30):
    info = ensure_notification_tasks_running(mode=mode, interval=interval)

    embed = discord.Embed(
        title="✅ Rank Bot gestartet!",
        description=f"**Modus:** {mode}\n**Intervall:** {interval}",
        color=0x00ff00
    )
    embed.add_field(name="🔁 Queue erstellen", value="Verwende `!rqueue` um Queue manuell zu erstellen", inline=True)
    embed.add_field(name="🕒 Aktive Zeiten", value="8-22 Uhr deutsche Zeit", inline=True)
    embed.add_field(name="🧹 Auto-Cleanup", value="Alte DM Views (7+ Tage) werden täglich entfernt", inline=True)

    loop_status = "\n".join(
        f"{'✅' if active else '⚠️'} {name.replace('_', ' ').title()}"
        for name, active in info["loops"].items()
    )
    embed.add_field(name="Loop Status", value=loop_status, inline=False)
    embed.set_footer(text="✅ Deadlock Rank Bot")
    await ctx.send(embed=embed)

    try:
        await push_rank_bot_state()
    except Exception as exc:
        logger.debug("State push after !rstart failed: %s", exc)
@bot.command(name='rstop')
@commands.has_permissions(administrator=True)
async def stop_notification_system(ctx: commands.Context):
    info = stop_notification_tasks()

    embed = discord.Embed(
        title="🛑 Rank Bot gestoppt!",
        description="Automatische Benachrichtigungen wurden gestoppt.",
        color=0xff6600
    )
    loop_status = "\n".join(
        f"{'✅' if active else '⏸️'} {name.replace('_', ' ').title()}"
        for name, active in info["loops"].items()
    )
    embed.add_field(name="Loop Status", value=loop_status, inline=False)
    embed.set_footer(text="✅ Deadlock Rank Bot")
    await ctx.send(embed=embed)

    try:
        await push_rank_bot_state()
    except Exception as exc:
        logger.debug("State push after !rstop failed: %s", exc)

@bot.command(name='mmrsync')
@commands.has_permissions(administrator=True)
async def mmr_sync_command(ctx: commands.Context, mode: str = None):
    dry_run = str(mode or "").lower() in {"dry", "dryrun", "test"}

    if MMR_SYNC_LOCK.locked():
        await ctx.send(embed=discord.Embed(
            title="⏳ MMR Sync läuft bereits",
            description="Bitte warte, bis der aktuelle Sync abgeschlossen ist.",
            color=0xffaa00
        ))
        return

    async with MMR_SYNC_LOCK:
        async with ctx.typing():
            summary = await sync_mmr_roles(dry_run=dry_run)

    embed = discord.Embed(
        title="✅ MMR Sync abgeschlossen",
        description="Daily Sync (manuell gestartet)",
        color=0x00cc66
    )
    embed.add_field(name="🔗 Links", value=str(summary.get("checked_links")), inline=True)
    embed.add_field(name="🧾 Accounts", value=str(summary.get("accounts_requested")), inline=True)
    embed.add_field(name="📦 Entries", value=str(summary.get("entries_received")), inline=True)
    embed.add_field(name="✅ Updated", value=str(summary.get("members_updated")), inline=True)
    embed.add_field(name="⏭️ Skipped", value=str(summary.get("members_skipped")), inline=True)
    embed.add_field(name="❓ Missing MMR", value=str(summary.get("missing_mmr")), inline=True)
    embed.add_field(name="❓ Missing Rank", value=str(summary.get("missing_rank")), inline=True)
    embed.add_field(name="❓ Missing Member", value=str(summary.get("missing_member")), inline=True)
    embed.add_field(name="🧪 Dry Run", value=str(summary.get("dry_run")), inline=True)
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)

@bot.command(name='mmrtest')
@commands.has_permissions(administrator=True)
async def mmr_test_command(ctx: commands.Context, steam_id64: str, mode: str = None):
    dry_run = str(mode or "").lower() in {"dry", "dryrun", "test"}

    if MMR_SYNC_LOCK.locked():
        await ctx.send(embed=discord.Embed(
            title="⏳ MMR Sync läuft bereits",
            description="Bitte warte, bis der aktuelle Sync abgeschlossen ist.",
            color=0xffaa00
        ))
        return

    async with MMR_SYNC_LOCK:
        async with ctx.typing():
            summary = await sync_mmr_roles(only_steam_ids=[steam_id64], dry_run=dry_run)

    embed = discord.Embed(
        title="✅ MMR Test abgeschlossen",
        description=f"SteamID64: `{steam_id64}`",
        color=0x00cc66
    )
    embed.add_field(name="🔗 Links", value=str(summary.get("checked_links")), inline=True)
    embed.add_field(name="🧾 Accounts", value=str(summary.get("accounts_requested")), inline=True)
    embed.add_field(name="📦 Entries", value=str(summary.get("entries_received")), inline=True)
    embed.add_field(name="✅ Updated", value=str(summary.get("members_updated")), inline=True)
    embed.add_field(name="⏭️ Skipped", value=str(summary.get("members_skipped")), inline=True)
    embed.add_field(name="❓ Missing MMR", value=str(summary.get("missing_mmr")), inline=True)
    embed.add_field(name="❓ Missing Rank", value=str(summary.get("missing_rank")), inline=True)
    embed.add_field(name="❓ Missing Member", value=str(summary.get("missing_member")), inline=True)
    embed.add_field(name="🧪 Dry Run", value=str(summary.get("dry_run")), inline=True)
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)

@bot.command(name='radd')
@commands.has_permissions(administrator=True)
async def add_user_to_queue(ctx: commands.Context, user: discord.Member):
    today = datetime.now().strftime('%Y-%m-%d')
    current_rank = get_user_current_rank(user)
    if not current_rank:
        embed = discord.Embed(
            title="❌ Kein Rang gefunden",
            description=f"User {user.mention} hat keinen Rang!",
            color=0xff0000
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return

    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO notification_queue (user_id, guild_id, rank, queue_date)
            VALUES (?, ?, ?, ?)
        ''', (str(user.id), str(ctx.guild.id), current_rank, today))
        conn.commit()

    embed = discord.Embed(
        title="✅ User zur Queue hinzugefügt!",
        description=f"{user.mention} wurde zur heutigen Queue hinzugefügt.\n**Rang:** {current_rank.capitalize()}",
        color=0x00ff00
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)

@bot.command(name='rdb')
@commands.has_permissions(administrator=True)
async def view_database(ctx: commands.Context, table: str = None):
    if not table:
        with central_db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM user_data')
            user_count = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM notification_log')
            notification_count = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM notification_queue WHERE queue_date = ?', (datetime.now().strftime('%Y-%m-%d'),))
            queue_count = cursor.fetchone()[0]
            cursor.execute('SELECT COUNT(*) FROM persistent_views')
            views_count = cursor.fetchone()[0]

        embed = discord.Embed(title="📊 Datenbank Übersicht", color=0x0099ff)
        embed.add_field(name="📋 Tabellen", value="`users`, `notifications`, `queue`, `views`", inline=True)
        embed.add_field(name="📊 Statistiken", value=f"👥 {user_count} User\n📧 {notification_count} Logs\n📋 {queue_count} Queue\n🖼️ {views_count} Views", inline=True)
        embed.add_field(name="🔧 Commands", value="`!rdb [table]`", inline=True)
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return

    t = table.lower()
    if t == 'users':
        with central_db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, custom_interval, paused_until FROM user_data LIMIT 10')
            results = cursor.fetchall()

        embed = discord.Embed(title="👥 User-Daten", color=0x0099ff)
        if results:
            lines = []
            for user_id, custom_interval, paused_until in results:
                try:
                    user = bot.get_user(int(user_id))
                    name = user.display_name if user else f"User {user_id}"
                    interval_text = f"{custom_interval}d" if custom_interval else "Standard"
                    pause_text = "Pausiert" if paused_until else "Aktiv"
                    lines.append(f"**{name}**: {interval_text}, {pause_text}")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    lines.append(f"**User {user_id}**: Fehler beim Laden")
            embed.add_field(name="📋 User (Top 10)", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📋 User", value="Keine Daten vorhanden", inline=False)
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return

    if t == 'notifications':
        with central_db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, rank, notification_time, count FROM notification_log ORDER BY notification_time DESC LIMIT 10')
            results = cursor.fetchall()

        embed = discord.Embed(title="📧 Benachrichtigungs-Log", color=0x0099ff)
        if results:
            lines = []
            for user_id, rank, notif_time, count in results:
                try:
                    user = bot.get_user(int(user_id))
                    name = user.display_name if user else f"User {user_id}"
                    time_str = datetime.fromisoformat(notif_time).strftime('%d.%m %H:%M')
                    lines.append(f"**{name}**: {rank.capitalize()} ({time_str}) #{count}")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    lines.append(f"**User {user_id}**: {rank} - Fehler")
            embed.add_field(name="📋 Letzte Benachrichtigungen", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="📋 Benachrichtigungen", value="Keine Daten vorhanden", inline=False)
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return

    if t == 'queue':
        with central_db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, rank, queue_date FROM notification_queue WHERE queue_date = ? ORDER BY user_id', (datetime.now().strftime('%Y-%m-%d'),))
            results = cursor.fetchall()

        embed = discord.Embed(title="📋 Heutige Benachrichtigungs-Queue", color=0x0099ff)
        if results:
            lines = []
            for user_id, rank, queue_date in results:
                try:
                    user = bot.get_user(int(user_id))
                    name = user.display_name if user else f"User {user_id}"
                    lines.append(f"**{name}**: {rank.capitalize()}")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    lines.append(f"**{user_id}**: {rank}")
            queue_text = "\n".join(lines)
            if len(queue_text) > 1000:
                queue_text = "\n".join(lines[:15]) + f"\n... und {len(lines)-15} weitere"
            embed.add_field(name=f"📅 Queue für {datetime.now().strftime('%d.%m.%Y')} ({len(lines)} User)", value=queue_text, inline=False)
        else:
            embed.add_field(name="📋 Queue", value="Keine Einträge für heute", inline=False)
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return

    if t == 'views':
        with central_db.get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT message_id, channel_id, guild_id, view_type, user_id FROM persistent_views')
            results = cursor.fetchall()

        embed = discord.Embed(title="🖼️ Persistent Views", color=0x0099ff)
        if results:
            views_list = []
            dm_count = 0
            server_count = 0
            for message_id, channel_id, guild_id, view_type, user_id in results:
                try:
                    if view_type == 'dm_rank_select' and user_id:
                        dm_count += 1
                        if dm_count <= 10:
                            user = bot.get_user(int(user_id))
                            user_name = user.display_name if user else f"User {user_id}"
                            views_list.append(f"**{view_type}**: DM to {user_name}")
                    else:
                        server_count += 1
                        channel = bot.get_channel(int(channel_id))
                        channel_name = channel.name if channel else f"Channel {channel_id}"
                        views_list.append(f"**{view_type}**: #{channel_name}")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    views_list.append(f"**{view_type}**: Unknown")
            summary = f"📊 **Gesamt:** {len(results)} Views ({server_count} Server, {dm_count} DMs)"
            if dm_count > 10:
                summary += f"\n*(Zeige nur erste 10 von {dm_count} DM Views)*"
            embed.add_field(name="📋 Aktive Views", value=summary + "\n\n" + "\n".join(views_list[:20]), inline=False)
        else:
            embed.add_field(name="📋 Views", value="Keine persistent Views vorhanden", inline=False)
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return

    await ctx.send(embed=discord.Embed(title="❌ Unbekannte Tabelle", description="Verfügbare Tabellen: `users`, `notifications`, `queue`, `views`", color=0xff0000))

# ---------- Scheduler ----------
def log_notification(user_id: str, rank: str):
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO notification_log (user_id, rank) VALUES (?, ?)', (user_id, rank))
        conn.commit()

def is_notification_time() -> bool:
    now = datetime.now()
    if test_users:
        return True
    return NOTIFICATION_START_HOUR <= now.hour < NOTIFICATION_END_HOUR

def save_queue_data(queue_data: list, date: str):
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM notification_queue WHERE queue_date = ?', (date,))
        for item in queue_data:
            cursor.execute('''
                INSERT INTO notification_queue (user_id, guild_id, rank, queue_date)
                VALUES (?, ?, ?, ?)
            ''', (item['user_id'], item['guild_id'], item['rank'], date))
        conn.commit()

def load_queue_data(date: str) -> list:
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, guild_id, rank 
              FROM notification_queue 
             WHERE queue_date = ? AND processed = FALSE
             ORDER BY added_at
        ''', (date,))
        results = cursor.fetchall()
        return [{'user_id': row[0], 'guild_id': row[1], 'rank': row[2]} for row in results]

def mark_queue_item_processed(user_id: str, guild_id: str, date: str):
    with central_db.get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE notification_queue 
               SET processed = TRUE 
             WHERE user_id = ? AND guild_id = ? AND queue_date = ?
        ''', (user_id, guild_id, date))
        conn.commit()


@tasks.loop(seconds=COMMAND_POLL_INTERVAL)
async def standalone_command_poller():
    try:
        pending = fetch_pending_commands()
        if not pending:
            return
        for row in pending:
            command_id = row["id"]
            if not mark_command_running(command_id):
                continue

            payload_data: Dict[str, Any] = {}
            raw_payload = row["payload"]
            if raw_payload:
                try:
                    payload_data = json.loads(raw_payload)
                except Exception as exc:
                    logger.warning("Invalid payload for command %s: %s", command_id, exc)

            try:
                result = await execute_control_command(row["command"], payload_data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Command %s failed: %s", row["command"], exc)
                finalize_command(command_id, "error", error=str(exc))
            else:
                finalize_command(command_id, "success", result=result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Standalone command poller error: %s", exc)


@tasks.loop(seconds=STATE_PUBLISH_INTERVAL)
async def standalone_state_publisher():
    try:
        await push_rank_bot_state()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Standalone state publisher error: %s", exc)


@tasks.loop(minutes=1)
async def daily_notification_check():
    if is_notification_time():
        await process_notification_queue()

@tasks.loop(hours=24)
async def daily_cleanup_check():
    logger.info("Starting daily DM cleanup...")
    cleaned_count = await cleanup_old_dm_views_auto()
    if cleaned_count > 0:
        logger.info(f"Daily cleanup completed: {cleaned_count} old DM views removed")
    else:
        logger.info("Daily cleanup completed: No old DM views to remove")

@tasks.loop(minutes=1)
async def daily_mmr_sync_check():
    if not _mmr_should_run_now():
        return
    if MMR_SYNC_LOCK.locked():
        return
    async with MMR_SYNC_LOCK:
        summary = await sync_mmr_roles(update_last_run=True)
        logger.info(
            "MMR Sync fertig: links=%s accounts=%s entries=%s updated=%s skipped=%s missing_mmr=%s missing_rank=%s missing_member=%s dry=%s",
            summary.get("checked_links"),
            summary.get("accounts_requested"),
            summary.get("entries_received"),
            summary.get("members_updated"),
            summary.get("members_skipped"),
            summary.get("missing_mmr"),
            summary.get("missing_rank"),
            summary.get("missing_member"),
            summary.get("dry_run"),
        )

async def create_daily_queue():
    today = datetime.now().strftime('%Y-%m-%d')
    queue_data = []

    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue

            # Opt-Out-Rollen
            if any(role.id in [NO_NOTIFICATION_ROLE_ID, NO_DEADLOCK_ROLE_ID] for role in member.roles):
                continue

            current_rank = get_user_current_rank(member)
            if not current_rank:
                continue

            user_data = get_user_data(str(member.id))
            if user_data.get('paused_until'):
                try:
                    pause_until = datetime.fromisoformat(user_data['paused_until'])
                    if datetime.now() < pause_until:
                        continue
                except Exception as e:
                    logger.debug("paused_until parse failed for %s: %r", member.id, e)

            custom_interval = user_data.get('custom_interval')
            interval_days = custom_interval if custom_interval else RANK_INTERVALS.get(current_rank, 30)

            with central_db.get_conn() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT notification_time 
                      FROM notification_log 
                     WHERE user_id = ? 
                  ORDER BY notification_time DESC LIMIT 1
                ''', (str(member.id),))
                result = cursor.fetchone()
                if result:
                    try:
                        last_notification = datetime.fromisoformat(result[0])
                        days_since = (datetime.now() - last_notification).days
                        if days_since < interval_days:
                            continue
                    except Exception as e:
                        logger.debug("last_notification parse failed for %s: %r", member.id, e)

            if test_users and member not in test_users:
                continue

            queue_data.append({'user_id': str(member.id), 'guild_id': str(guild.id), 'rank': current_rank})

    save_queue_data(queue_data, today)
    logger.info(f"Daily queue created with {len(queue_data)} users for {today}")

async def process_notification_queue():
    today = datetime.now().strftime('%Y-%m-%d')
    queue_data = load_queue_data(today)
    if not queue_data:
        return

    user_to_notify = queue_data[0]
    try:
        guild = bot.get_guild(int(user_to_notify["guild_id"]))
        if not guild:
            mark_queue_item_processed(user_to_notify["user_id"], user_to_notify["guild_id"], today)
            return

        member = guild.get_member(int(user_to_notify["user_id"]))
        if not member:
            mark_queue_item_processed(user_to_notify["user_id"], user_to_notify["guild_id"], today)
            return

        await ask_rank_update(member, user_to_notify["rank"], guild)
        log_notification(user_to_notify["user_id"], user_to_notify["rank"])
        mark_queue_item_processed(user_to_notify["user_id"], user_to_notify["guild_id"], today)
        logger.info(f"Sent notification to {member.display_name} ({user_to_notify['rank']})")

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        mark_queue_item_processed(user_to_notify["user_id"], user_to_notify["guild_id"], today)

# ---------- Events ----------
@bot.event
async def on_ready():
    # Vorab-Hinweis: prüfe, dass die zentrale DB-Datei wirklich existiert
    if not Path(DB_FILE).exists():
        logger.critical(
            "❌ Zentrale DB-Datei existiert nicht: %s\n"
            "Bitte zuerst anlegen/kopieren. Der Bot beendet sich.", DB_FILE
        )
        await bot.close()
        return

    logger.info(f'Deadlock Rank Bot ist online! ({bot.user})')
    logger.info("Standalone Rank Bot bereit für Commands:")
    logger.info("   !rsetup - Rang-Auswahl erstellen")
    logger.info("   !rqueue - Queue manuell erstellen")
    logger.info("   !rqueue_remaining - Queue mit verbleibenden Usern")
    logger.info("   !rqueue_never_contacted - Queue mit neuen Usern (30 Tage Wartezeit)")
    logger.info("   !rcheck_never_contacted - Analyse der noch nie kontaktierten User")
    logger.info("   !radd @user - User zur Queue hinzufügen")
    logger.info("   !rtest - Test-Nachricht senden")
    logger.info("   !rtest_users @user1 @user2 - Test-User setzen")
    logger.info("   !rstart - System starten")
    logger.info("   !rdb - Datenbank anzeigen")

    if not standalone_command_poller.is_running():
        standalone_command_poller.start()
    if not standalone_state_publisher.is_running():
        standalone_state_publisher.start()
    try:
        await push_rank_bot_state()
    except Exception as exc:
        logger.warning(f'Initial state push failed: {exc}')

    await restore_persistent_views()
    await auto_restore_rank_channel_view()

# ---------- Main ----------
if __name__ == "__main__":
    # Harte Abbruchbedingung: DB-Datei muss existieren
    if not Path(DB_FILE).exists():
        print("❌ FEHLER: Zentrale DB-Datei existiert nicht:")
        print(f"   {DB_FILE}")
        print("Bitte zuerst erzeugen/kopieren (keine Auto-Neuanlage durch den Bot).")
        sys.exit(1)

    # Tabellen initialisieren/verwalten (innerhalb der bestehenden DB-Datei)
    init_database()

    token = os.getenv("DISCORD_TOKEN_RANKED")  # genau dieses ENV wird geladen
    if not token:
        print("❌ FEHLER: Kein Ranked Discord Token gefunden!")
        print("Bitte in C:\\Users\\Nani-Admin\\Documents\\.env den Key DISCORD_TOKEN_RANKED= setzen")
        sys.exit(1)

    bot.run(token)
