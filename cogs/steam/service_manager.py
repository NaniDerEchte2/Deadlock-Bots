from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, List, Optional

# Discord imports für den Cog-Wrapper
import discord
from discord.ext import commands

LOGGER = logging.getLogger("steam.presence")

_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def _to_bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in _FALSE_VALUES


def _resolve_service_dir(explicit: Optional[Path]) -> Path:
    """
    1) übergebenes service_dir, 2) ENV STEAM_PRESENCE_DIR, 3) ./steam_presence neben diesem File.
    """
    if explicit is not None:
        return Path(explicit).expanduser().resolve()

    env = os.getenv("STEAM_PRESENCE_DIR")
    if env:
        return Path(env).expanduser().resolve()

    # dieses File liegt in .../cogs/steam/service_manager.py
    here = Path(__file__).resolve().parent  # .../cogs/steam
    return (here / "steam_presence").resolve()


@dataclass
class SteamServiceStatus:
    running: bool
    pid: Optional[int]
    returncode: Optional[int]
    last_start: Optional[float]
    last_exit: Optional[float]
    restarts: int
    cmd: str
    cwd: Path
    auto_start: bool


class SteamPresenceServiceManager:
    """Controls the node-based Steam rich presence bridge (KEIN Cog)."""

    def __init__(self, service_dir: Optional[Path] = None) -> None:
        self.service_dir = _resolve_service_dir(service_dir)
        self.start_command = os.getenv("STEAM_SERVICE_CMD", "npm run start")
        self.install_command = os.getenv("STEAM_SERVICE_INSTALL_CMD", "npm install")
        self.auto_start = _to_bool(os.getenv("AUTO_START_STEAM_SERVICE"), True)
        self.auto_install = _to_bool(os.getenv("STEAM_SERVICE_AUTO_INSTALL"), True)
        self.shutdown_timeout = float(os.getenv("STEAM_SERVICE_SHUTDOWN_TIMEOUT", "10"))
        self.restart_on_crash = _to_bool(os.getenv("STEAM_SERVICE_RESTART_ON_CRASH"), True)

        self._process: Optional[asyncio.subprocess.Process] = None
        self._stdout_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._lock: Optional[asyncio.Lock] = None
        self._stdout: Deque[str] = deque(maxlen=100)
        self._stderr: Deque[str] = deque(maxlen=100)
        self._last_start: Optional[float] = None
        self._last_exit: Optional[float] = None
        self._restart_count = 0
        self._deps_checked = False
        self._closing = False
        self._monitor_task: Optional[asyncio.Task[None]] = None

        LOGGER.info("Steam presence service directory resolved to: %s", self.service_dir)

    async def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def ensure_dependencies(self) -> None:
        if self._deps_checked:
            return
        self._deps_checked = True

        package_json = self.service_dir / "package.json"
        if not package_json.exists():
            LOGGER.warning("Steam presence service directory %s missing package.json", self.service_dir)
            return

        node_modules = self.service_dir / "node_modules"
        if node_modules.exists() and not _to_bool(os.getenv("STEAM_SERVICE_FORCE_INSTALL"), False):
            return

        cmd = self.install_command
        LOGGER.info("Installing steam presence dependencies via '%s' (cwd=%s)", cmd, self.service_dir)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(self.service_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout = []
        if proc.stdout is not None:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                stdout.append(text)
                LOGGER.debug("[npm install] %s", text)
        rc = await proc.wait()
        if rc != 0:
            raise RuntimeError(f"npm install failed with exit code {rc}: {'; '.join(stdout[-10:])}")
        LOGGER.info("npm dependencies ready")

    async def ensure_started(self) -> bool:
        lock = await self._get_lock()
        async with lock:
            if self.is_running:
                return False

            if not self.service_dir.exists():
                LOGGER.error("Steam presence service path does not exist: %s", self.service_dir)
                return False

            if self.auto_install:
                await self.ensure_dependencies()

            package_json = self.service_dir / "package.json"
            if not package_json.exists():
                LOGGER.warning("Abort start: no package.json in %s", self.service_dir)
                return False

            cmd = self.start_command
            LOGGER.info("Starting steam presence service using '%s' (cwd=%s)", cmd, self.service_dir)
            self._closing = False
            self._process = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(self.service_dir),
                stdin=asyncio.subprocess.PIPE,            # wichtig für !sg / /sg
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._last_start = time.time()
            self._stdout.clear()
            self._stderr.clear()
            if self._process.stdout is not None:
                self._stdout_task = asyncio.create_task(
                    self._pump_stream(self._process.stdout, self._stdout, logging.INFO, "stdout")
                )
            if self._process.stderr is not None:
                self._stderr_task = asyncio.create_task(
                    self._pump_stream(self._process.stderr, self._stderr, logging.WARNING, "stderr")
                )
            self._monitor_task = asyncio.create_task(self._monitor_process())
            LOGGER.info("Steam presence service started (pid=%s)", self._process.pid)
            return True

    async def stop(self) -> bool:
        lock = await self._get_lock()
        async with lock:
            if not self.is_running:
                return False
            assert self._process is not None
            self._closing = True
            proc = self._process
            LOGGER.info("Stopping steam presence service (pid=%s)...", proc.pid)
            try:
                proc.terminate()
            except ProcessLookupError:
                LOGGER.debug("Process already gone when attempting terminate")
            except Exception as exc:
                LOGGER.warning("Failed to terminate steam presence service: %s", exc)

            try:
                await asyncio.wait_for(proc.wait(), timeout=self.shutdown_timeout)
            except asyncio.TimeoutError:
                LOGGER.warning("Steam presence service did not exit in %.1fs, killing", self.shutdown_timeout)
                proc.kill()
                await proc.wait()

            self._last_exit = time.time()
            self._cleanup_tasks()
            LOGGER.info("Steam presence service stopped with code %s", proc.returncode)
            self._process = None
            return True

    async def restart(self) -> bool:
        restarted = False
        if self.is_running:
            await self.stop()
            restarted = True
        await self.ensure_started()
        self._restart_count += 1
        return restarted

    def _cleanup_tasks(self) -> None:
        for task in (self._stdout_task, self._stderr_task, self._monitor_task):
            if task is None:
                continue
            task.cancel()
        self._stdout_task = None
        self._stderr_task = None
        self._monitor_task = None

    async def _pump_stream(self, stream: asyncio.StreamReader, buffer: Deque[str], level: int, label: str) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                buffer.append(text)
                LOGGER.log(level, "[steam %s] %s", label, text)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            LOGGER.warning("Error while reading %s: %s", label, exc)

    async def _monitor_process(self) -> None:
        if self._process is None:
            return
        proc = self._process
        try:
            rc = await proc.wait()
        except asyncio.CancelledError:
            return
        self._last_exit = time.time()
        self._cleanup_tasks()
        if self._closing:
            LOGGER.info("Steam presence service exited with code %s", rc)
            self._process = None
            return
        LOGGER.warning("Steam presence service exited unexpectedly with code %s", rc)
        self._process = None
        if self.restart_on_crash:
            try:
                await asyncio.sleep(2)
                await self.ensure_started()
                self._restart_count += 1
            except Exception as exc:
                LOGGER.error("Failed to restart steam presence service: %s", exc)

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def tail(self, *, stderr: bool = False, limit: int = 20) -> List[str]:
        buffer: Deque[str] = self._stderr if stderr else self._stdout
        if limit <= 0:
            return []
        items = list(buffer)
        slice_start = max(0, len(items) - limit)
        return items[slice_start:]

    def status(self) -> SteamServiceStatus:
        pid = self._process.pid if self._process else None
        returncode = self._process.returncode if self._process else None
        return SteamServiceStatus(
            running=self.is_running,
            pid=pid,
            returncode=returncode,
            last_start=self._last_start,
            last_exit=self._last_exit,
            restarts=self._restart_count,
            cmd=self.start_command,
            cwd=self.service_dir,
            auto_start=self.auto_start,
        )

    async def submit_guard_code(self, code: str) -> bool:
        proc = self._process
        if not self.is_running or proc is None or proc.stdin is None:
            LOGGER.warning("Cannot submit guard code: process not running or no stdin.")
            return False
        try:
            text = (code.strip() + "\n").encode()
            proc.stdin.write(text)
            await proc.stdin.drain()
            LOGGER.info("Submitted Steam Guard code to service (len=%d).", len(code.strip()))
            return True
        except Exception as exc:
            LOGGER.error("Failed to submit guard code: %s", exc)
            return False


# =========================
# Cog-Wrapper (Service gehört dem Cog)
# =========================
class SteamPresenceServiceCog(commands.Cog):
    """Discord Cog, der den Manager hält und auto-startet."""

    def __init__(self, bot: commands.Bot, service_dir: Optional[Path] = None) -> None:
        self.bot = bot
        self.manager = SteamPresenceServiceManager(service_dir=service_dir)
        # Manager global am Bot verfügbar machen:
        setattr(self.bot, "steam_service_manager", self.manager)

    async def cog_load(self) -> None:
        # Auto-Start beim Laden des Cogs
        if self.manager.auto_start:
            self.bot.loop.create_task(self._safe_start())

    async def cog_unload(self) -> None:
        try:
            await self.manager.stop()
        except Exception:
            pass
        if getattr(self.bot, "steam_service_manager", None) is self.manager:
            delattr(self.bot, "steam_service_manager")

    async def _safe_start(self) -> None:
        try:
            await self.manager.ensure_started()
            LOGGER.info(
                "Steam presence service auto_start=%s • running=%s",
                self.manager.auto_start, self.manager.is_running
            )
        except Exception as exc:
            LOGGER.error("Failed to start steam presence service: %s", exc)

    # Debug-Kommandos (optional)
    @commands.command(name="steam_service_status")
    @commands.has_permissions(administrator=True)
    async def steam_service_status(self, ctx: commands.Context):
        s = self.manager.status()
        txt = (
            f"running={s.running} pid={s.pid} restarts={s.restarts} "
            f"cwd={s.cwd} cmd='{s.cmd}' auto_start={s.auto_start}"
        )
        await ctx.reply(f"```{txt}```")

    @commands.command(name="steam_service_restart")
    @commands.has_permissions(administrator=True)
    async def steam_service_restart(self, ctx: commands.Context):
        await self.manager.restart()
        await ctx.reply("🔁 Presence-Service neu gestartet.")


# Discord lädt den **Cog**, NICHT den Manager direkt
async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SteamPresenceServiceCog(bot))
