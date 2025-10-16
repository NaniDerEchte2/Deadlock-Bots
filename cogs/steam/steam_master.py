"""Steam hub cog.

This cog intentionally no longer handles Steam logins itself.  The Node.js
bridge located in :mod:`cogs.steam.steam_presence` is responsible for the
real-time Rich Presence connection.  The cog merely surfaces shared state and
housekeeping helpers so other Python modules can interact with the database and
stored tokens in a consistent way.
"""

from __future__ import annotations

import enum
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional

from discord.ext import commands

from service import db

log = logging.getLogger(__name__)


class SteamMasterMode(enum.Enum):
    """Operational mode for the hub cog."""

    HUB = "hub"
    DISABLED = "disabled"


def _presence_data_dir() -> Path:
    """Resolve the Node.js presence bridge data directory."""

    configured = (os.getenv("STEAM_PRESENCE_DATA_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()

    return Path(__file__).resolve().parent / "steam_presence" / ".steam-data"


def _refresh_token_path() -> Path:
    return _presence_data_dir() / "refresh.token"


def _machine_auth_path() -> Path:
    return _presence_data_dir() / "machine_auth_token.txt"


def _determine_mode() -> SteamMasterMode:
    """Determine whether the hub should be active."""

    raw = (os.getenv("STEAM_MASTER_MODE") or "hub").strip().lower()
    if raw in {"disabled", "off", "none"}:
        return SteamMasterMode.DISABLED
    return SteamMasterMode.HUB


def _fetch_group_counts(sql: str) -> Dict[str, int]:
    stats: Dict[str, int] = defaultdict(int)
    try:
        rows = db.connect().execute(sql).fetchall()
        for row in rows:
            status = str(row[0]) if row[0] is not None else "unknown"
            try:
                stats[status] += int(row[1])
            except (TypeError, ValueError):
                stats[status] += 0
    except Exception:  # pragma: no cover - logging only
        log.exception("Failed to collect Steam statistics", extra={"query": sql})
    return stats


def _count_single(sql: str) -> Optional[int]:
    try:
        row = db.connect().execute(sql).fetchone()
        if not row:
            return 0
        return int(row[0])
    except Exception:  # pragma: no cover - logging only
        log.exception("Failed to fetch Steam counter", extra={"query": sql})
        return None


class SteamMaster(commands.Cog):
    """Discord cog providing hub-style Steam helpers."""

    def __init__(self, bot: commands.Bot, *, mode: Optional[SteamMasterMode] = None) -> None:
        self.bot = bot
        self.mode = mode or _determine_mode()
        log.info("SteamMaster initialised in %s mode", self.mode.value)

    @staticmethod
    def _format_stats(title: str, stats: Dict[str, int]) -> str:
        if not stats:
            return f"{title}: keine Einträge"
        parts = ", ".join(f"{status}={count}" for status, count in sorted(stats.items()))
        return f"{title}: {parts}"

    def _hub_status(self) -> str:
        lines = ["mode=hub"]
        refresh = _refresh_token_path()
        machine = _machine_auth_path()
        lines.append(f"refresh_token={'yes' if refresh.exists() else 'no'} ({refresh})")
        lines.append(f"machine_auth={'yes' if machine.exists() else 'no'} ({machine})")

        fr_stats = _fetch_group_counts(
            "SELECT status, COUNT(*) FROM steam_friend_requests GROUP BY status"
        )
        lines.append(self._format_stats("friend_requests", fr_stats))

        invite_stats = _fetch_group_counts(
            "SELECT status, COUNT(*) FROM steam_quick_invites GROUP BY status"
        )
        lines.append(self._format_stats("quick_invites", invite_stats))

        watch_count = _count_single("SELECT COUNT(*) FROM steam_presence_watchlist")
        if watch_count is not None:
            lines.append(f"presence_watchlist={watch_count}")

        links_count = _count_single(
            "SELECT COUNT(DISTINCT steam_id) FROM steam_links WHERE steam_id IS NOT NULL AND steam_id != ''"
        )
        if links_count is not None:
            lines.append(f"linked_accounts={links_count}")

        return "\n".join(lines)

    # ---------- commands ----------
    @commands.command(name="sg", aliases=["steam_guard", "steamguard"])
    @commands.has_permissions(administrator=True)
    async def cmd_sg(self, ctx: commands.Context, code: str) -> None:  # noqa: ARG002
        """Steam Guard codes are handled by the Node.js bridge."""

        await ctx.reply(
            "ℹ️ Der Steam Guard-Code muss direkt im Node.js Dienst eingegeben werden."
        )

    @commands.command(name="steam_status")
    @commands.has_permissions(administrator=True)
    async def cmd_status(self, ctx: commands.Context) -> None:
        """Show current hub state."""

        await ctx.reply(f"```{self._hub_status()}```")

    @commands.command(name="steam_token")
    @commands.has_permissions(administrator=True)
    async def cmd_token(self, ctx: commands.Context) -> None:
        """Display stored token information."""

        refresh = _refresh_token_path()
        machine = _machine_auth_path()
        await ctx.reply(
            "🔐 refresh_token: {r}\n📁 Pfad: `{rp}`\n🖥️ machine_auth: {m}\n📁 Pfad: `{mp}`".format(
                r="vorhanden" if refresh.exists() else "nicht vorhanden",
                rp=refresh,
                m="vorhanden" if machine.exists() else "nicht vorhanden",
                mp=machine,
            )
        )

    @commands.command(name="steam_token_clear")
    @commands.has_permissions(administrator=True)
    async def cmd_token_clear(self, ctx: commands.Context) -> None:
        """Clean up persisted refresh/machine tokens."""

        refresh = _refresh_token_path()
        machine = _machine_auth_path()
        removed = []
        for path, label in ((refresh, "refresh_token"), (machine, "machine_auth")):
            try:
                if path.exists():
                    path.unlink()
                    removed.append(label)
            except Exception:  # pragma: no cover - best effort cleanup
                log.exception("Failed to delete Steam token", extra={"path": str(path)})

        if removed:
            await ctx.reply("🧹 Gelöscht: {}".format(", ".join(removed)))
        else:
            await ctx.reply("ℹ️ Keine Tokens gefunden.")


async def setup(bot: commands.Bot) -> None:
    mode = _determine_mode()
    if mode is SteamMasterMode.DISABLED:
        log.info("SteamMaster cog disabled via STEAM_MASTER_MODE=%s", os.getenv("STEAM_MASTER_MODE"))
        return
    await bot.add_cog(SteamMaster(bot, mode=mode))
