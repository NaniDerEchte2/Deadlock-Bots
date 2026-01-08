from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import datetime as _dt
import pytz
import discord
from discord.ext import commands

from bot_core.bootstrap import _init_db_if_available, _log_secret_present
from bot_core.cog_loader import CogLoaderMixin
from bot_core.logging_setup import LoggingMixin
from bot_core.presence import PresenceMixin
from bot_core.standalone import StandaloneMixin
from service.config import settings
from service.http_client import build_resilient_connector

try:
    from service.dashboard import DashboardServer
except Exception as _dashboard_import_error:
    DashboardServer = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning(
        "Dashboard module unavailable: %s", _dashboard_import_error
    )

__all__ = ["MasterBot"]


class MasterBot(LoggingMixin, CogLoaderMixin, PresenceMixin, StandaloneMixin, commands.Bot):
    """
    Master Discord Bot mit:
     - Auto-Discovery + Blocklist
     - Reload/Unload Helper
     - Presence/Voice Router
     - Standalone Manager Hooks
     - Dashboard als Cog
    """

    def __init__(self, lifecycle: Optional["BotLifecycle"] = None):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        intents.guilds = True

        connector = build_resilient_connector()

        super().__init__(
            command_prefix=settings.command_prefix,
            intents=intents,
            description="Master Bot System - Verwaltet alle Bot-Funktionen",
            owner_id=settings.owner_id,
            case_insensitive=True,
            chunk_guilds_at_startup=False,
            max_messages=1000,
            member_cache_flags=discord.MemberCacheFlags.from_intents(intents),
            connector=connector,
        )

        self.lifecycle = lifecycle
        self.root_dir = Path(__file__).resolve().parent.parent

        self.setup_logging()

        self.cogs_dir = self.root_dir / "cogs"
        blocklist_path = os.getenv("COG_BLOCKLIST_FILE")
        if blocklist_path:
            self.blocklist_path = Path(blocklist_path)
        else:
            self.blocklist_path = self.cogs_dir.parent / "cog_blocklist.json"
        self.blocked_namespaces = set()
        self._load_blocklist()

        self.cogs_list = []
        self.cog_status = {}
        self.auto_discover_cogs()

        tz = pytz.timezone("Europe/Berlin")
        self.startup_time = _dt.datetime.now(tz=tz)

        # Dashboard is now loaded as a Cog (cogs/dashboard_cog.py)
        self.dashboard: Optional[DashboardServer] = None  # Set by DashboardCog
        self._dashboard_start_task: Optional[asyncio.Task[None]] = None  # Legacy, kept for compatibility

        self.standalone_manager = None
        self.setup_standalone_manager()

        try:
            self.per_cog_unload_timeout = float(os.getenv("PER_COG_UNLOAD_TIMEOUT", "3.0"))
        except ValueError:
            self.per_cog_unload_timeout = 3.0

    async def request_restart(self, reason: str = "unknown") -> bool:
        """
        Delegate a full-process restart to the lifecycle supervisor if available.
        """
        if not self.lifecycle:
            logging.warning("Restart requested (%s) aber kein Lifecycle vorhanden", reason)
            return False
        return await self.lifecycle.request_restart(reason=reason)

    async def setup_hook(self):
        logging.info("Master Bot setup starting...")

        secret_mode = (os.getenv("SECRET_LOG_MODE") or "off").lower()
        _log_secret_present("Steam API Key", ["STEAM_API_KEY", "STEAM_WEB_API_KEY"], mode=secret_mode)
        _log_secret_present("Discord Token (Master)", ["DISCORD_TOKEN", "BOT_TOKEN"], mode="off")
        _log_secret_present("Twitch Client Credentials", ["TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"], mode=secret_mode)
        _log_secret_present("Twitch Chat Token", ["TWITCH_BOT_TOKEN", "TWITCH_BOT_TOKEN_FILE"], mode=secret_mode)

        _init_db_if_available()
        await self.load_all_cogs()

        try:
            synced = await self.tree.sync()
            logging.info(f"Synced {len(synced)} slash commands")
        except Exception as e:
            logging.error(f"Failed to sync slash commands: {e}")

        logging.info("Master Bot setup completed")

    async def close(self):
        logging.info("Master Bot shutting down...")

        if self.dashboard:
            try:
                await self.dashboard.stop()
            except Exception as e:
                logging.error(f"Fehler beim Stoppen des Dashboards: {e}")

        if self.standalone_manager:
            try:
                await self.standalone_manager.shutdown()
            except Exception as exc:
                logging.error(f"Fehler beim Stoppen des Standalone-Managers: {exc}")

        to_unload = [ext for ext in list(self.extensions.keys()) if ext.startswith("cogs.")]
        if to_unload:
            logging.info(f"Unloading {len(to_unload)} cogs with timeout {self.per_cog_unload_timeout:.1f}s each ...")
            _ = await self.unload_many(to_unload, timeout=self.per_cog_unload_timeout)

        try:
            timeout = float(os.getenv("DISCORD_CLOSE_TIMEOUT", "5"))
        except ValueError:
            timeout = 5.0
        try:
            await asyncio.wait_for(super().close(), timeout=timeout)
            logging.info("discord.Client.close() returned")
        except asyncio.TimeoutError:
            logging.error(f"discord.Client.close() timed out after {timeout:.1f}s; continuing shutdown")
        except Exception as e:
            logging.error(f"Error in discord.Client.close(): {e}")

        logging.info("Master Bot shutdown complete")
