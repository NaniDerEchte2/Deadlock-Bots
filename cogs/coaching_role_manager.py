"""
Coaching Role Manager - Automatische Rollen-Verwaltung
- Entfernt die Coaching-Rolle nach Ablauf der Request-Frist.
- Reminder sind bewusst deaktiviert.
"""

import asyncio
import logging
import time

import discord
from discord.ext import commands

from service import db
from service.config import settings

log = logging.getLogger(__name__)


class CoachingRoleManagerCog(commands.Cog):
    """Coaching Role Manager - Handles automatic role assignment and removal"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._role_check_task: asyncio.Task | None = None

    async def cog_load(self):
        if self._role_check_task is None or self._role_check_task.done():
            self._role_check_task = asyncio.create_task(self._role_check_loop())

    async def cog_unload(self):
        if self._role_check_task:
            self._role_check_task.cancel()
            self._role_check_task = None

    async def _role_check_loop(self):
        """Background loop to check and manage coaching roles"""
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._check_expired_roles()
            except Exception as e:
                log.error(f"Role check loop error: {e}")
            await asyncio.sleep(60)  # Check every minute

    async def _check_expired_roles(self):
        """Remove expired coaching active roles."""
        now = int(time.time())

        rows = db.query_all(
            """SELECT * FROM coaching_requests
               WHERE role_removed_at IS NULL
               AND role_expires_at IS NOT NULL
               AND role_expires_at < ?""",
            (now,),
        )

        for request in rows:
            guild = self.bot.guilds[0] if self.bot.guilds else None
            if not guild:
                continue

            member = guild.get_member(request["discord_user_id"])
            if not member:
                db.execute(
                    "UPDATE coaching_requests SET role_removed_at=?, updated_at=? WHERE id=?",
                    (now, now, request["id"]),
                )
                continue

            role = guild.get_role(settings.coaching_active_role_id)
            if role and role in member.roles:
                await member.remove_roles(role, reason="Coaching-Rolle abgelaufen (48h)")
                log.info(f"Removed coaching role from {member.display_name}")

            db.execute(
                "UPDATE coaching_requests SET role_removed_at=?, updated_at=? WHERE id=?",
                (now, now, request["id"]),
            )

            session = db.query_one(
                """SELECT * FROM coaching_sessions
                   WHERE request_id=? AND status IN ('active', 'waiting_survey')
                   ORDER BY created_at DESC LIMIT 1""",
                (request["id"],),
            )
            if not session:
                continue
            try:
                thread_id = session["discord_thread_id"]
            except Exception as e:
                log.error(f"Could not read session thread id: {e}")
                continue
            if not thread_id:
                continue
            thread = guild.get_channel_or_thread(thread_id)
            if thread:
                await thread.send(
                    "⏰ Die 48h Coaching-Phase ist abgelaufen. Falls ihr noch keine Voice-Session hattet, "
                    "müsst ihr eine neue Anfrage stellen."
                )

    async def assign_coaching_role(self, user_id: int, guild: discord.Guild, thread_id: int):
        """Assign the coaching active role to user"""
        member = guild.get_member(user_id)
        if not member:
            return False

        role = guild.get_role(settings.coaching_active_role_id)
        if not role:
            log.error(f"Role {settings.coaching_active_role_id} not found")
            return False

        try:
            await member.add_roles(role, reason="Coaching-Anfrage angenommen")
            log.info(f"Assigned coaching role to {member.display_name}")
            return True
        except Exception as e:
            log.error(f"Could not assign role: {e}")
            return False

    async def remove_coaching_role(self, user_id: int, guild: discord.Guild):
        """Remove the coaching active role from user"""
        member = guild.get_member(user_id)
        if not member:
            return False

        role = guild.get_role(settings.coaching_active_role_id)
        if not role:
            return False

        try:
            await member.remove_roles(role, reason="Coaching-Phase beendet")
            log.info(f"Removed coaching role from {member.display_name}")
            return True
        except Exception as e:
            log.error(f"Could not remove role: {e}")
            return False


async def setup(bot: commands.Bot):
    await bot.add_cog(CoachingRoleManagerCog(bot))
