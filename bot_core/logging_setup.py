from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from bot_core.bootstrap import _RedactSecretsFilter


class LoggingMixin:
    """Logging-Setup inkl. Secret-Filter."""

    def setup_logging(self):
        log_dir = self.root_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        runtime_role = str(os.getenv("TWITCH_SPLIT_RUNTIME_ROLE", "")).strip().lower()
        safe_role = "".join(ch for ch in runtime_role if ch.isalnum() or ch in {"-", "_"})
        log_prefix = f"master_bot.{safe_role}" if safe_role else "master_bot"

        # Default output: INFO. Still capture DEBUG to a dedicated log file.
        root_handlers: list[logging.Handler] = []

        info_file = logging.handlers.RotatingFileHandler(
            log_dir / f"{log_prefix}.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        info_file.setLevel(logging.INFO)
        root_handlers.append(info_file)

        debug_file = logging.handlers.RotatingFileHandler(
            log_dir / f"{log_prefix}.debug.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        debug_file.setLevel(logging.DEBUG)
        root_handlers.append(debug_file)

        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(logging.INFO)
        root_handlers.append(stream)

        logging.getLogger().handlers.clear()
        logging.basicConfig(
            level=logging.DEBUG,  # allow DEBUG to flow to the dedicated file handler
            handlers=root_handlers,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )

        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("discord.http").setLevel(logging.WARNING)

        # Suppress noisy library logs (security & clutter)
        logging.getLogger("twitchio").setLevel(logging.INFO)
        logging.getLogger("twitchio.http").setLevel(logging.INFO)
        logging.getLogger("twitchio.websocket").setLevel(logging.INFO)
        logging.getLogger("aiohttp").setLevel(logging.INFO)

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
