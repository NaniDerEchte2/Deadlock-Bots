"""
Synchronize Steam Bot friends to database.

This module provides functionality to sync all current friends of the Steam bot
into the steam_links table, ensuring all bot friends are tracked in the database.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from cogs.steam.steam_master import SteamTaskClient
from service import db
from service.config import settings

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
            # Already linked to a Discord account, just update verified + friend status
            conn.execute(
                """
                UPDATE steam_links
                SET verified = 1, is_steam_friend = 1, updated_at = CURRENT_TIMESTAMP
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
                INSERT INTO steam_links(user_id, steam_id, name, verified, is_steam_friend)
                VALUES(?, ?, ?, ?, 1)
                ON CONFLICT(user_id, steam_id) DO UPDATE SET
                  verified=1,
                  is_steam_friend=1,
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
            return {"success": False, "count": 0, "cleared_count": 0, "error": error_msg}

        if not outcome.result or not isinstance(outcome.result, dict):
            log.error("Invalid result format from AUTH_GET_FRIENDS_LIST")
            return {
                "success": False,
                "count": 0,
                "cleared_count": 0,
                "error": "Invalid result format",
            }

        data = outcome.result.get("data", {})
        friends = data.get("friends", [])

        if not friends:
            log.warning("No friends found in Steam bot's friend list")
            return {"success": True, "count": 0, "cleared_count": 0, "error": None}

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

        # Cleanup: is_steam_friend=0 für Steam-IDs die nicht mehr in der Freundesliste sind
        friend_steam_ids = [f.get("steam_id64") for f in friends if f.get("steam_id64")]
        cleared = 0
        if friend_steam_ids:
            placeholders = ",".join("?" * len(friend_steam_ids))
            with db.get_conn() as conn:
                cleared = conn.execute(
                    f"UPDATE steam_links SET is_steam_friend=0, updated_at=CURRENT_TIMESTAMP "
                    f"WHERE is_steam_friend=1 AND steam_id NOT IN ({placeholders})",
                    friend_steam_ids,
                ).rowcount
            if cleared:
                log.info("Cleared is_steam_friend for %d removed friends", cleared)

        return {"success": True, "count": synced, "cleared_count": cleared, "error": None}

    except Exception as e:
        log.exception("Failed to sync friends")
        return {"success": False, "count": 0, "cleared_count": 0, "error": str(e)}


class SteamFriendsSync(commands.Cog):
    """Commands for syncing Steam bot friends to database."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tasks = SteamTaskClient(poll_interval=0.5, default_timeout=30.0)

    async def cog_load(self) -> None:
        self.periodic_sync.start()
        if settings.guild_id:
            guild_obj = discord.Object(id=settings.guild_id)
            # Global-Tree → Guild-Tree verschieben, damit der Command sofort sichtbar ist
            self.bot.tree.remove_command("sync_steam_friends")
            self.bot.tree.add_command(self.slash_sync_friends, guild=guild_obj)
            try:
                synced = await self.bot.tree.sync(guild=guild_obj)
                log.info(
                    "SteamFriendsSync: Guild-Command sync abgeschlossen (%d commands)", len(synced)
                )
            except Exception as exc:
                log.warning("SteamFriendsSync: Guild-Command-Sync fehlgeschlagen: %s", exc)

    def cog_unload(self) -> None:
        self.periodic_sync.cancel()

    @tasks.loop(hours=6)
    async def periodic_sync(self) -> None:
        """Synchronisiert alle 6h die Steam-Freundesliste mit der DB (is_steam_friend Pflege)."""
        log.info("Periodischer Steam-Freunde-Sync gestartet...")
        result = await sync_all_friends(self.tasks)
        if result["success"]:
            log.info("Periodischer Sync abgeschlossen: %d Freunde", result["count"])
        else:
            log.warning("Periodischer Sync fehlgeschlagen: %s", result.get("error"))

    @periodic_sync.before_loop
    async def before_periodic_sync(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="sync_steam_friends",
        description="(Admin) Synchronisiert Steam-Freundesliste und aktualisiert Verified-Rollen.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def slash_sync_friends(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        # 1. Steam-Freunde syncen
        result = await sync_all_friends(self.tasks)

        if not result["success"]:
            error = result.get("error", "Unbekannter Fehler")
            await interaction.followup.send(
                f"❌ Steam-Sync fehlgeschlagen: `{error}`", ephemeral=True
            )
            return

        synced = result["count"]
        cleared = result.get("cleared_count", 0)

        # 2. Verified-Rollen sofort aktualisieren (add + remove)
        role_changes = 0
        try:
            verified_cog = self.bot.get_cog("SteamVerifiedRole")
            if verified_cog is not None:
                role_changes = await verified_cog._run_once()
        except Exception as exc:
            log.warning("Rolle-Update nach Sync fehlgeschlagen: %s", exc)

        lines = [f"**Steam-Freunde synchronisiert:** {synced}"]
        if cleared:
            lines.append(f"**Freundschaften beendet (is_steam_friend=0):** {cleared}")
        lines.append(f"**Rollen-Updates:** {role_changes}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @commands.command(name="sync_steam_friends")
    @commands.has_permissions(administrator=True)
    async def cmd_sync_friends(self, ctx: commands.Context) -> None:
        """Synchronize all Steam bot friends to the database."""
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
