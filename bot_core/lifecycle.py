from __future__ import annotations

import asyncio
import importlib
import logging
import time
from collections.abc import Callable

from bot_core.bootstrap import _load_env_robust

logger = logging.getLogger(__name__)


class BotLifecycle:
    """
    Kleiner Supervisor, der den Bot-Prozess im selben Interpreter neustarten kann.
    """

    def __init__(self, token: str | None = None, token_loader: Callable[[], str] | None = None):
        if token_loader is None and not token:
            raise ValueError("BotLifecycle benötigt entweder token oder token_loader")
        self.token = token
        self._token_loader = token_loader
        self._restart_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._current_bot: MasterBot | None = None
        self._restart_requested_at: float | None = None
        self._last_restart_at: float | None = None
        self._restart_reason: str | None = None

    def _resolve_token(self) -> str:
        if self._token_loader is not None:
            return self._token_loader()
        if not self.token:
            raise RuntimeError("Kein Discord-Token verfügbar")
        return self.token

    # ------------------ Public API ------------------
    async def request_restart(self, reason: str = "manual") -> bool:
        """
        Signalisiert dem Lifecycle, den laufenden Bot sauber zu beenden
        und unmittelbar neu zu starten.
        """
        if self._stop_event.is_set():
            return False
        if self._restart_event.is_set():
            return False

        self._restart_reason = reason
        self._restart_requested_at = time.time()
        self._restart_event.set()

        bot = self._current_bot
        if bot and not bot.is_closed():
            asyncio.create_task(bot.close())
        return True

    async def request_stop(self, reason: str = "signal") -> None:
        """
        Stoppt den Lifecycle komplett (kein Neustart mehr).
        """
        self._stop_event.set()
        self._restart_event.clear()
        bot = self._current_bot
        if bot and not bot.is_closed():
            logger.info("Stop requested (%s) -> closing bot", reason)
            asyncio.create_task(bot.close())

    def snapshot(self) -> dict:
        """
        Zustandsinfo für Dashboard/Monitoring.
        """
        return {
            "enabled": True,
            "running": bool(self._current_bot and not self._current_bot.is_closed()),
            "restart_requested": self._restart_event.is_set(),
            "restart_requested_at": self._restart_requested_at,
            "last_restart_at": self._last_restart_at,
            "restart_reason": self._restart_reason,
        }

    # ------------------ Internals -------------------
    def _build_bot(self) -> tuple[MasterBot, object]:
        """
        Lädt die relevanten Module neu, damit Code-Änderungen beim Restart greifen.
        """
        try:
            import service.config as service_config

            importlib.reload(service_config)
        except Exception as exc:  # pragma: no cover - defensive reload
            logger.warning("Module reload skipped for service.config: %s", exc)

        try:
            import service.db as service_db

            importlib.reload(service_db)
        except Exception as exc:  # pragma: no cover - defensive reload
            logger.warning("Module reload skipped for service.db: %s", exc)

        try:
            import service.dashboard as service_dashboard

            importlib.reload(service_dashboard)
        except Exception as exc:  # pragma: no cover - defensive reload
            logger.warning("Module reload skipped for service.dashboard: %s", exc)

        import bot_core.control as control_module_ref
        import bot_core.master_bot as master_module_ref

        master_module = importlib.reload(master_module_ref)
        control_module = importlib.reload(control_module_ref)  # nosemgrep

        bot: MasterBot = master_module.MasterBot(lifecycle=self)
        control_cog_cls = control_module.MasterControlCog
        return bot, control_cog_cls

    async def run_forever(self) -> None:
        """
        Startet den Bot, reagiert auf Restart-Signale und endet erst
        wenn ein Stop angefordert wird oder kein Restart mehr pending ist.
        """
        while not self._stop_event.is_set():
            _load_env_robust()
            try:
                token = self._resolve_token()
            except Exception as exc:
                logger.critical(
                    "Discord-Anmeldedaten konnten nicht bestimmt werden (%s).",
                    exc.__class__.__name__,
                )
                self._stop_event.set()
                break

            bot, control_cog_cls = self._build_bot()
            self._current_bot = bot

            try:
                await bot.add_cog(control_cog_cls(bot))  # type: ignore[arg-type]
                await bot.start(token)
            except KeyboardInterrupt:
                logger.info("Keyboard interrupt received, shutting down lifecycle ...")
                await self.request_stop("keyboard")
            except Exception as exc:
                logger.error("Bot crashed: %s", exc, exc_info=True)
            finally:
                if not bot.is_closed():
                    try:
                        await bot.close()
                    except Exception:
                        logger.exception("Error while closing bot after crash")
                self._current_bot = None

            if self._stop_event.is_set():
                break

            if self._restart_event.is_set():
                self._restart_event.clear()
                self._last_restart_at = time.time()
                logger.info("Restart request processed -> launching new bot instance")
                continue

            # Normal exit without restart
            break
