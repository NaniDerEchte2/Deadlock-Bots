from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import List

from bot_core.bootstrap import _RedactSecretsFilter


class LoggingMixin:
    """Logging-Setup inkl. Secret-Filter."""

    def setup_logging(self):
        log_dir = self.root_dir / "logs"
        log_dir.mkdir(exist_ok=True)

        # Fixed log level (no ENV override) to keep LFG/Flow-Debugging sichtbar
        level = logging.DEBUG

        root_handlers: List[logging.Handler] = [
            logging.handlers.RotatingFileHandler(
                log_dir / "master_bot.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
            logging.StreamHandler(sys.stdout),
        ]

        logging.getLogger().handlers.clear()
        logging.basicConfig(
            level=level,
            handlers=root_handlers,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("discord.http").setLevel(logging.WARNING)

        # Immer Secrets redaktieren, ohne ENV-Flag
        redact_keys = [
            "DISCORD_TOKEN",
            "BOT_TOKEN",
            "RANK_BOT_TOKEN",
            "STEAM_API_KEY",
            "STEAM_WEB_API_KEY",
            "DISCORD_TOKEN_WORKER",
        ]
        flt = _RedactSecretsFilter(redact_keys)
        for h in logging.getLogger().handlers:
            h.addFilter(flt)

        logging.info("Master Bot logging initialized")
