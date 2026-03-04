from __future__ import annotations

import asyncio
import importlib
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from bot_core.bootstrap import _load_env_robust, bootstrap_runtime

# Frühe Initialisierung, damit .env/Logging bereitstehen bevor Settings geladen werden.
bootstrap_runtime()

from bot_core import BotLifecycle, MasterBot, MasterControlCog  # noqa: E402

__all__ = ["MasterBot", "MasterControlCog", "BotLifecycle"]

_PID_FILE_BASE = Path(__file__).parent / "master_bot.pid"
_PID_LOCK_PATH: Path | None = None


def _env_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _effective_split_runtime_role() -> str:
    role = (os.getenv("TWITCH_SPLIT_RUNTIME_ROLE") or "").strip().lower()
    if role not in {"bot", "dashboard"}:
        return ""
    if not _env_truthy(os.getenv("TWITCH_SPLIT_RUNTIME_ENFORCE")):
        return ""
    return role


def _pid_file_path() -> Path:
    role = _effective_split_runtime_role()
    if not role:
        return _PID_FILE_BASE
    return _PID_FILE_BASE.with_name(f"{_PID_FILE_BASE.stem}.{role}{_PID_FILE_BASE.suffix}")


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            WAIT_OBJECT_0 = 0x00000000
            WAIT_TIMEOUT = 0x00000102
            WAIT_FAILED = 0xFFFFFFFF

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            open_process.restype = ctypes.c_void_p

            wait_for_single_object = kernel32.WaitForSingleObject
            wait_for_single_object.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            wait_for_single_object.restype = ctypes.c_uint32

            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int

            access = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
            handle = open_process(access, 0, pid)
            if not handle:
                err = ctypes.get_last_error()
                if err in {87, 1168}:  # invalid parameter / element not found
                    return False
                if err == 5:  # access denied => process exists
                    return True
                return True  # fail closed: avoid starting a duplicate instance

            try:
                wait_result = wait_for_single_object(handle, 0)
                if wait_result == WAIT_TIMEOUT:
                    return True
                if wait_result == WAIT_OBJECT_0:
                    return False
                if wait_result == WAIT_FAILED:
                    return True
                return True
            finally:
                close_handle(handle)
        except Exception:
            return True

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _acquire_pid_lock() -> None:
    """Verhindert dass zwei Instanzen gleichzeitig laufen.

    Prüft ob eine PID-Datei existiert und ob der darin gespeicherte Prozess
    noch aktiv ist. Bei Konflikt wird gewarnt und der aktuelle Start abgebrochen.
    """
    global _PID_LOCK_PATH
    pid_file = _pid_file_path()
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            old_pid = None

        if old_pid and old_pid != os.getpid():
            if _pid_exists(old_pid):
                logging.critical(
                    "Master Bot läuft bereits als PID %s. "
                    "Zweite Instanz wird NICHT gestartet (verhindert Token-Race-Conditions). "
                    "Beende PID %s zuerst oder lösche %s manuell.",
                    old_pid,
                    old_pid,
                    pid_file,
                )
                sys.exit(1)
            logging.warning(
                "Stale PID-File gefunden (PID %s nicht mehr aktiv) → wird überschrieben",
                old_pid,
            )

    pid_file.write_text(str(os.getpid()))
    _PID_LOCK_PATH = pid_file


def _release_pid_lock() -> None:
    global _PID_LOCK_PATH
    pid_file = _PID_LOCK_PATH
    if pid_file is None:
        return
    try:
        if pid_file.exists() and int(pid_file.read_text().strip()) == os.getpid():
            pid_file.unlink()
    except Exception as exc:
        logging.getLogger(__name__).debug("PID lock release failed: %r", exc)
    finally:
        _PID_LOCK_PATH = None


def _load_fresh_token() -> str:
    """
    Reload settings (and .env) so restarts pick up a rotated Discord token.
    """
    _load_env_robust()
    config_module = importlib.reload(importlib.import_module("service.config"))
    token = config_module.settings.discord_token.get_secret_value()
    if not token:
        raise SystemExit("DISCORD_TOKEN fehlt in ENV/.env")
    return token


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop, lifecycle: BotLifecycle
) -> Callable[[], None]:
    shutdown_started = False
    kill_timer: threading.Timer | None = None

    try:
        kill_after = float(os.getenv("KILL_AFTER_SECONDS", "2"))
    except ValueError:
        kill_after = 2.0

    def _arm_kill_timer() -> None:
        nonlocal kill_timer
        try:
            kill_timer = threading.Timer(
                kill_after,
                lambda: (
                    logging.error(f"Kill watchdog fired after {kill_after:.1f}s -> os._exit(2)"),
                    os._exit(2),
                ),
            )
            kill_timer.daemon = True
            kill_timer.start()
        except Exception as exc:  # pragma: no cover - defensive
            logging.getLogger(__name__).debug("Kill-Timer konnte nicht gestartet werden: %r", exc)

    def _cancel_kill_timer() -> None:
        nonlocal kill_timer
        if not kill_timer:
            return
        try:
            kill_timer.cancel()
        except Exception as exc:  # pragma: no cover - defensive
            logging.getLogger(__name__).debug("Kill-Timer konnte nicht gestoppt werden: %r", exc)
        kill_timer = None

    def _handle(signum, frame) -> None:  # pragma: no cover - system dependent
        nonlocal shutdown_started
        if shutdown_started:
            logging.error("Second signal received -> hard exit now.")
            os._exit(1)

        shutdown_started = True
        logging.info("Received signal %s, shutting down gracefully...", signum)
        _arm_kill_timer()
        try:
            loop.call_soon_threadsafe(
                asyncio.create_task,
                lifecycle.request_stop(reason=f"signal {signum}"),
            )
        except RuntimeError:
            os._exit(0)

    try:
        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "Signal-Handler Registrierung teilweise fehlgeschlagen (OS?): %r", exc
        )

    return _cancel_kill_timer


async def main() -> None:
    lifecycle = BotLifecycle(token_loader=_load_fresh_token)
    loop = asyncio.get_running_loop()
    cancel_watchdog = _install_signal_handlers(loop, lifecycle)

    try:
        await lifecycle.run_forever()
    finally:
        cancel_watchdog()
        logging.info("Lifecycle beendet")


if __name__ == "__main__":
    _acquire_pid_lock()
    try:
        asyncio.run(main())
    finally:
        _release_pid_lock()
