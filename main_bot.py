from __future__ import annotations

import asyncio
import importlib
import logging
import os
import signal
import threading
from typing import Callable

from bot_core.bootstrap import _load_env_robust, bootstrap_runtime

# Frühe Initialisierung, damit .env/Logging bereitstehen bevor Settings geladen werden.
bootstrap_runtime()

from bot_core import BotLifecycle, MasterBot, MasterControlCog  # noqa: E402

__all__ = ["MasterBot", "MasterControlCog", "BotLifecycle"]


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
                    logging.error(
                        f"Kill watchdog fired after {kill_after:.1f}s -> os._exit(2)"
                    ),
                    os._exit(2),
                ),
            )
            kill_timer.daemon = True
            kill_timer.start()
        except Exception as exc:  # pragma: no cover - defensive
            logging.getLogger(__name__).debug(
                "Kill-Timer konnte nicht gestartet werden: %r", exc
            )

    def _cancel_kill_timer() -> None:
        nonlocal kill_timer
        if not kill_timer:
            return
        try:
            kill_timer.cancel()
        except Exception as exc:  # pragma: no cover - defensive
            logging.getLogger(__name__).debug(
                "Kill-Timer konnte nicht gestoppt werden: %r", exc
            )
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
    asyncio.run(main())
