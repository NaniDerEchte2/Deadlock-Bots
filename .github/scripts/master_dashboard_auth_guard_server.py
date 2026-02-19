from __future__ import annotations

import asyncio
import datetime as dt
import os
import signal
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from service.dashboard import DashboardServer


class _DummyBot:
    def __init__(self) -> None:
        self.application_id = 424242
        self.lifecycle = None
        self.guilds: List[Any] = []
        self.cogs: Dict[str, Any] = {}
        self.cogs_list: List[str] = []
        self.cog_status: Dict[str, str] = {}
        self.blocked_namespaces: set[str] = set()
        self.per_cog_unload_timeout = 3.0
        self.startup_time = dt.datetime.now(dt.timezone.utc)
        self.user = None
        self.latency = 0.0
        self.extensions: Dict[str, Any] = {}

    def active_cogs(self) -> List[str]:
        return []

    def get_cog(self, _name: str) -> Any:
        return None

    def get_guild(self, _guild_id: int) -> Any:
        return None

    def get_user(self, _user_id: int) -> Any:
        return None

    def resolve_cog_identifier(self, _raw: str) -> Tuple[Optional[str], List[str]]:
        return None, []

    def normalize_namespace(self, namespace: Any) -> str:
        return str(namespace or "").strip()

    def is_namespace_blocked(self, _namespace: str, *, assume_normalized: bool = False) -> bool:
        _ = assume_normalized
        return False

    def auto_discover_cogs(self) -> None:
        return None

    async def reload_cog(self, _name: str) -> Tuple[bool, str]:
        return False, "unsupported"

    async def unload_many(self, _names: Iterable[str], *, timeout: float = 3.0) -> Dict[str, Any]:
        _ = timeout
        return {}

    async def reload_all_cogs_with_discovery(self) -> Tuple[bool, str]:
        return False, "unsupported"

    async def reload_namespace(self, _namespace: str) -> List[Any]:
        return []

    async def block_namespace(self, _path: str) -> Dict[str, Any]:
        return {"ok": False, "error": "unsupported"}

    async def unblock_namespace(self, _path: str) -> Dict[str, Any]:
        return {"ok": False, "error": "unsupported"}


async def _run() -> None:
    host = (os.getenv("MASTER_GUARD_HOST") or "127.0.0.1").strip()
    try:
        port = int((os.getenv("MASTER_GUARD_PORT") or "8790").strip())
    except ValueError:
        port = 8790

    session_id = (os.getenv("MASTER_GUARD_SESSION_ID") or "guard-session").strip()
    csrf_token = (os.getenv("MASTER_GUARD_CSRF_TOKEN") or "guard-csrf").strip()

    bot = _DummyBot()
    dashboard = DashboardServer(bot, host=host, port=port)

    # Force a deterministic session-auth path for CSRF/Origin regression tests.
    dashboard._discord_auth_required = True
    dashboard._auth_misconfigured = False
    dashboard._bot_restart_min_interval_seconds = 0.0
    now = time.time()
    dashboard._discord_sessions[session_id] = {
        "user_id": 1,
        "username": "ci-user",
        "display_name": "CI Guard",
        "reason": "ci",
        "csrf_token": csrf_token,
        "created_at": now,
        "last_seen_at": now,
        "expires_at": now + 3600,
    }

    # Avoid touching real services in CI.
    dashboard._schedule_nssm_service_restart = lambda: (True, "guard-restart-scheduled")  # type: ignore[method-assign]

    await dashboard.start()
    print(f"MASTER_GUARD_URL=http://{host}:{port}", flush=True)
    print(f"MASTER_GUARD_SESSION_ID={session_id}", flush=True)
    print(f"MASTER_GUARD_CSRF_TOKEN={csrf_token}", flush=True)
    print("MASTER_GUARD_READY=1", flush=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            continue

    try:
        await stop_event.wait()
    finally:
        await dashboard.stop()


if __name__ == "__main__":
    asyncio.run(_run())
