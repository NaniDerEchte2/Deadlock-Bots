"""Helper-Cog fuer Roland: verwaltet service.db Reloads ueber Dashboard/Cog-Reload.
Die eigentliche DB-Logik liegt weiterhin in service/db.py.
"""

from __future__ import annotations

import importlib
import logging

from discord.ext import commands

from service import db

log = logging.getLogger(__name__)


class DBHelperCog(commands.Cog):
    """Reload/close the central service.db connection as a helper tool."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self._refresh_db("load")

    def cog_unload(self) -> None:
        try:
            db.close_connection()
            log.info("service.db connection closed via cog unload")
        except Exception as exc:
            log.warning("service.db close failed on unload: %s", exc)

    def _refresh_db(self, reason: str) -> None:
        try:
            db.close_connection()
        except Exception as exc:
            log.debug("service.db close failed before reload (%s): %s", reason, exc)
        try:
            importlib.reload(db)
            log.info("service.db reloaded (%s)", reason)
        except Exception as exc:
            log.error("service.db reload failed (%s): %s", reason, exc)
            return
        try:
            db.query_one("SELECT 1")
            log.info("service.db connection ready (%s) at %s", reason, db.db_path())
        except Exception as exc:
            log.error("service.db connect failed after reload (%s): %s", reason, exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DBHelperCog(bot))
