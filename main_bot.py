from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading

from bot_core.bootstrap import bootstrap_runtime

# Frühe Initialisierung, damit .env/Logging bereitstehen bevor Settings geladen werden.
bootstrap_runtime()

from bot_core import BotLifecycle, MasterBot, MasterControlCog  # noqa: E402
from service.config import settings  # noqa: E402

__all__ = ["MasterBot", "MasterControlCog", "BotLifecycle"]


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, lifecycle: BotLifecycle) -> None:
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


async def main() -> None:
    token = settings.discord_token.get_secret_value()
    if not token:
        raise SystemExit("DISCORD_TOKEN fehlt in ENV/.env")

    lifecycle = BotLifecycle(token=token)
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, lifecycle)

    try:
        await lifecycle.run_forever()
    finally:
        logging.info("Lifecycle beendet")


if __name__ == "__main__":
    asyncio.run(main())
