from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_core.master_bot import MasterBot

__all__ = ["graceful_shutdown"]

_shutdown_started = False
_kill_timer: threading.Timer | None = None


async def graceful_shutdown(
    bot: MasterBot,
    reason: str = "signal",
    timeout_close: float = 3.0,
    timeout_total: float = 4.0,
) -> None:
    global _shutdown_started, _kill_timer
    if _shutdown_started:
        return
    _shutdown_started = True

    logging.info(f"Graceful shutdown initiated ({reason}) ...")

    # 1) Bot sauber schließen (mit Timeout)
    try:
        await asyncio.wait_for(bot.close(), timeout=timeout_close)
        logging.info("bot.close() returned")
    except asyncio.TimeoutError:
        logging.error(f"bot.close() timed out after {timeout_close:.1f}s")
    except Exception as e:
        logging.error(f"Error during bot.close(): {e}")

    # 2) Übrige Tasks abbrechen (außer dieser)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        t.cancel()
    try:
        await asyncio.wait(pending, timeout=max(0.0, timeout_total - timeout_close))
    except Exception as e:
        logging.getLogger().debug("Warten auf Pending-Tasks schlug fehl (ignoriert): %r", e)

    # Kill-Watchdog stoppen, wenn wir bis hierhin sauber sind
    try:
        if _kill_timer:
            _kill_timer.cancel()
    except Exception as exc:
        logging.getLogger().debug("Kill-Timer konnte nicht gestoppt werden: %s", exc)

    # 3) Loop stoppen + harter Exit als letzte Eskalationsstufe
    try:
        loop = asyncio.get_running_loop()
        loop.stop()
        loop.call_later(0.2, lambda: os._exit(0))
    except Exception as e:
        logging.getLogger().debug("Loop Stop/Hard Exit (ignoriert): %r", e)
        os._exit(0)

