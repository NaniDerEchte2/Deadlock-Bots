"""
Synchronize Steam Bot friends to database.

This module provides functionality to sync all current friends of the Steam bot
into the steam_links table, ensuring all bot friends are tracked in the database.
"""

from __future__ import annotations

import logging

from discord.ext import commands

from cogs.steam.steam_master import SteamTaskClient
from service import db

log = logging.getLogger(__name__)


def _save_steam_friend_to_db(steam_id64: str, discord_id: int | None = None) -> None:
    """
    Save a Steam friend to the database.

    Args:
        steam_id64: The Steam ID64 of the friend
        discord_id: Optional Discord ID if known (defaults to 0 for unknown)
    """
    uid = int(discord_id) if discord_id else 0

    with db.get_conn() as conn:
        # Check if this steam_id already exists with a real Discord ID
        existing = conn.execute(
            "SELECT user_id FROM steam_links WHERE steam_id = ? AND user_id != 0 LIMIT 1",
            (steam_id64,),
        ).fetchone()

        if existing:
            # Already linked to a Discord account, just update verified status
            conn.execute(
                """
                UPDATE steam_links
                SET verified = 1, updated_at = CURRENT_TIMESTAMP
                WHERE steam_id = ? AND user_id = ?
                """,
                (steam_id64, existing["user_id"]),
            )
            log.info(
                "Updated existing steam_link: steam=%s, discord=%s",
                steam_id64,
                existing["user_id"],
            )
        else:
            # New friend or unlinked friend
            conn.execute(
                """
                INSERT INTO steam_links(user_id, steam_id, name, verified)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(user_id, steam_id) DO UPDATE SET
                  verified=1,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (uid, steam_id64, "", 1),
            )
            log.info("Saved new steam_link: steam=%s, discord=%s", steam_id64, uid)


async def sync_all_friends(tasks: SteamTaskClient | None = None) -> dict:
    """
    Synchronize all current Steam bot friends to the database.

    Args:
        tasks: Optional SteamTaskClient instance (creates new one if not provided)

    Returns:
        Dictionary with sync results:
        - success: bool
        - count: int (number of friends synced)
        - error: Optional str
    """
    if tasks is None:
        tasks = SteamTaskClient(poll_interval=0.5, default_timeout=30.0)

    try:
        # Request friends list from Node.js service
        log.info("Requesting friends list from Steam service...")
        outcome = await tasks.run("AUTH_GET_FRIENDS_LIST", timeout=30.0)

        if not outcome.ok:
            error_msg = outcome.error or "Failed to get friends list"
            log.error("Failed to get friends list: %s", error_msg)
            return {"success": False, "count": 0, "error": error_msg}

        if not outcome.result or not isinstance(outcome.result, dict):
            log.error("Invalid result format from AUTH_GET_FRIENDS_LIST")
            return {"success": False, "count": 0, "error": "Invalid result format"}

        data = outcome.result.get("data", {})
        friends = data.get("friends", [])

        if not friends:
            log.warning("No friends found in Steam bot's friend list")
            return {"success": True, "count": 0, "error": None}

        log.info("Found %d friends, syncing to database...", len(friends))

        # Sync each friend to database
        synced = 0
        for friend in friends:
            steam_id64 = friend.get("steam_id64")
            if not steam_id64:
                continue

            try:
                _save_steam_friend_to_db(steam_id64)
                synced += 1
            except Exception as e:
                log.error("Failed to save friend %s: %s", steam_id64, e)

        log.info("Synced %d/%d friends to database", synced, len(friends))
        return {"success": True, "count": synced, "error": None}

    except Exception as e:
        log.exception("Failed to sync friends")
        return {"success": False, "count": 0, "error": str(e)}


class SteamFriendsSync(commands.Cog):
    """Commands for syncing Steam bot friends to database."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tasks = SteamTaskClient(poll_interval=0.5, default_timeout=30.0)

    @commands.command(name="sync_steam_friends")
    @commands.has_permissions(administrator=True)
    async def cmd_sync_friends(self, ctx: commands.Context) -> None:
        """
        Synchronize all Steam bot friends to the database.

        This command fetches all current friends of the Steam bot and saves them
        to the steam_links table, ensuring all bot friends are tracked.
        """
        async with ctx.typing():
            result = await sync_all_friends(self.tasks)

        if result["success"]:
            await ctx.reply(
                f"✅ Synced {result['count']} Steam friends to database.",
                mention_author=False,
            )
        else:
            error = result.get("error", "Unknown error")
            await ctx.reply(f"❌ Failed to sync friends: {error}", mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SteamFriendsSync(bot))
