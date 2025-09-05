# utils/deadlock_db.py
from __future__ import annotations
import os
import asyncio
from pathlib import Path
from typing import Optional

try:
    import aiosqlite
except ImportError as e:
    raise RuntimeError(
        "aiosqlite ist nicht installiert. Bitte ausführen: pip install aiosqlite"
    ) from e

# -----------------------------------------------------------
# ZIELPFAD:
# Standard:  %USERPROFILE%/Documents/Deadlock/service/deadlock.sqlite3
# Override:  Umgebungsvariable DEADLOCK_DB_DIR (z. B. C:\Users\Nani-Admin\Documents\Deadlock\service)
# -----------------------------------------------------------
_default_dir = Path.home() / "Documents" / "Deadlock" / "service"
_env_dir = os.getenv("DEADLOCK_DB_DIR")
DB_DIR = Path(_env_dir) if _env_dir else _default_dir
DB_PATH = DB_DIR / "deadlock.sqlite3"


class Database:
    """Singleton für eine geteilte aiosqlite-Connection inkl. Schema & Helper."""
    _instance: Optional["Database"] = None
    _lock = asyncio.Lock()

    def __init__(self) -> None:
        self.conn: Optional[aiosqlite.Connection] = None

    @classmethod
    async def instance(cls) -> "Database":
        async with cls._lock:
            if cls._instance is None:
                db = Database()
                await db._init()
                cls._instance = db
            return cls._instance

    async def _init(self) -> None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(DB_PATH)
        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("PRAGMA foreign_keys=ON;")
        await self._create_schema()

    async def _create_schema(self) -> None:
        assert self.conn is not None
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS user_threads (
                user_id    INTEGER PRIMARY KEY,
                thread_id  INTEGER NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS templates (
                key        TEXT PRIMARY KEY,
                content    TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        await self.conn.commit()

    # ---------- Templates ----------
    async def get_template(self, key: str, default: Optional[str] = None) -> Optional[str]:
        assert self.conn is not None
        cur = await self.conn.execute("SELECT content FROM templates WHERE key = ?", (key,))
        row = await cur.fetchone()
        await cur.close()
        if row:
            return row[0]
        if default is not None:
            await self.conn.execute(
                "INSERT OR IGNORE INTO templates (key, content) VALUES (?, ?)",
                (key, default),
            )
            await self.conn.commit()
            return default
        return None

    async def set_template(self, key: str, content: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            """
            INSERT INTO templates (key, content)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE
            SET content = excluded.content, updated_at = datetime('now')
            """,
            (key, content),
        )
        await self.conn.commit()

    # ---------- KV ----------
    async def kv_get(self, key: str) -> Optional[str]:
        assert self.conn is not None
        cur = await self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,))
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    # ---------- UserThreads ----------
    async def get_user_thread(self, user_id: int) -> Optional[int]:
        assert self.conn is not None
        cur = await self.conn.execute("SELECT thread_id FROM user_threads WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row else None

    async def set_user_thread(self, user_id: int, thread_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute(
            """
            INSERT INTO user_threads (user_id, thread_id)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE
            SET thread_id = excluded.thread_id, created_at = datetime('now')
            """,
            (user_id, thread_id),
        )
        await self.conn.commit()

    async def delete_user_thread(self, user_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute("DELETE FROM user_threads WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def delete_user_thread_by_thread_id(self, thread_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute("DELETE FROM user_threads WHERE thread_id = ?", (thread_id,))
        await self.conn.commit()

    # ---------- Pflege ----------
    async def vacuum(self) -> None:
        assert self.conn is not None
        await self.conn.execute("VACUUM")
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None


# Bequemer Helper (liest + seedet Defaults)
async def tpl(key: str, default: str) -> str:
    db = await Database.instance()
    val = await db.get_template(key, default)
    return val if val is not None else default
