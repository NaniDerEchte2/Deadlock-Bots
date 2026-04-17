"""
Coaching Survey - Post-Coaching Feedback via DM
"""

import asyncio
import logging
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from service import db
from service.config import settings

log = logging.getLogger(__name__)


class SurveyModal(discord.ui.Modal, title="Coaching Session Feedback"):
    def __init__(self, session_id: str):
        super().__init__(custom_id=f"survey_modal_{session_id}")
        self.session_id = session_id

        self.add_item(
            discord.ui.TextInput(
                label="Bewertung (0-10)",
                placeholder="0 = schlecht, 10 = perfekt",
                max_length=2,
                required=True,
            )
        )
        self.add_item(
            discord.ui.TextInput(
                label="Feedback",
                style=discord.TextStyle.paragraph,
                placeholder="Was hat dir gefallen? Was könnte besser sein?",
                max_length=1000,
                required=True,
            )
        )
        self.add_item(
            discord.ui.TextInput(
                label="Verbesserungen (optional)",
                style=discord.TextStyle.paragraph,
                placeholder="Was hast du gelernt/verbessert?",
                max_length=500,
                required=False,
            )
        )
        self.add_item(
            discord.ui.TextInput(
                label="Ungeklärte Fragen (optional)",
                style=discord.TextStyle.paragraph,
                placeholder="Was wurde nicht addressed?",
                max_length=500,
                required=False,
            )
        )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rating = int(self.children[0].value)
            rating = max(0, min(10, rating))
        except ValueError:
            rating = 5

        feedback = self.children[1].value
        improved = (
            self.children[2].value if len(self.children) > 2 and self.children[2].value else None
        )
        unresolved = (
            self.children[3].value if len(self.children) > 3 and self.children[3].value else None
        )

        # Store survey
        survey_id = str(uuid.uuid4())
        db.execute(
            """INSERT INTO coaching_surveys (id, session_id, rating, feedback_text, improved_areas, unresolved_items, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (survey_id, self.session_id, rating, feedback, improved, unresolved, int(time.time())),
        )

        # Update session
        db.execute(
            "UPDATE coaching_sessions SET status='survey_completed', completed_at=? WHERE id=?",
            (int(time.time()), self.session_id),
        )

        # Update coach stats
        session = db.query_one(
            "SELECT coach_id FROM coaching_sessions WHERE id=?", (self.session_id,)
        )
        if session and session["coach_id"]:
            coach_id = session["coach_id"]
            stats = db.query_one(
                """SELECT AVG(rating) as avg, COUNT(*) as cnt
                   FROM coaching_surveys
                   WHERE session_id IN (SELECT id FROM coaching_sessions WHERE coach_id=?)""",
                (coach_id,),
            )
            if stats and stats["avg"]:
                db.execute(
                    """UPDATE coaches SET avg_rating=?, total_reviews=?, total_sessions=total_sessions+1, updated_at=?
                       WHERE id=?""",
                    (stats["avg"], stats["cnt"], int(time.time()), coach_id),
                )

        await interaction.response.send_message(
            f"✅ Danke für dein Feedback!\n\nDu hast mit **{rating}/10** bewertet.\n"
            f"Dein Feedback hilft uns die Coaching-Qualität zu verbessern!",
            ephemeral=True,
        )


class SurveyButton(discord.ui.Button):
    def __init__(self, session_id: str):
        super().__init__(
            label="Feedback geben",
            style=discord.ButtonStyle.primary,
            custom_id=f"survey_btn_{session_id}",
        )
        self.session_id = session_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SurveyModal(self.session_id))


class SurveyView(discord.ui.View):
    def __init__(self, session_id: str):
        super().__init__(timeout=None)
        self.add_item(SurveyButton(session_id))


class CoachingSurveyCog(commands.Cog):
    """Coaching Survey - Post-Coaching DM Feedback"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._survey_dispatching: set[str] = set()
        self._survey_check_task: asyncio.Task | None = None

    async def cog_load(self):
        sessions = db.query_all(
            "SELECT id FROM coaching_sessions WHERE status='waiting_survey'"
        )
        for s in sessions:
            self.bot.add_view(SurveyView(s["id"]))
        if sessions:
            log.info("Re-registered %d persistent SurveyView(s) after restart", len(sessions))
        if self._survey_check_task is None or self._survey_check_task.done():
            self._survey_check_task = asyncio.create_task(self._run_survey_checks())

    async def cog_unload(self):
        if self._survey_check_task:
            self._survey_check_task.cancel()
            self._survey_check_task = None

    def _get_primary_guild(self) -> discord.Guild | None:
        return self.bot.guilds[0] if self.bot.guilds else None

    def _get_coaching_voice_channel(
        self, member: discord.Member | None
    ) -> discord.VoiceChannel | None:
        if not member or not member.voice or not member.voice.channel:
            return None
        channel = member.voice.channel
        if not isinstance(channel, discord.VoiceChannel):
            return None
        if channel.category_id != settings.coaching_voice_category_id:
            return None
        return channel

    def _get_shared_coaching_voice_channel(
        self,
        user_member: discord.Member | None,
        coach_member: discord.Member | None,
    ) -> discord.VoiceChannel | None:
        user_channel = self._get_coaching_voice_channel(user_member)
        coach_channel = self._get_coaching_voice_channel(coach_member)
        if user_channel and coach_channel and user_channel.id == coach_channel.id:
            return user_channel
        return None

    async def _run_survey_checks(self):
        await self.bot.wait_until_ready()
        while True:
            try:
                await self._scan_active_sessions()
            except Exception:
                log.exception("Coaching survey check loop failed")
            await asyncio.sleep(60)

    async def _scan_active_sessions(self):
        guild = self._get_primary_guild()
        if not guild:
            return

        sessions = db.query_all(
            """SELECT * FROM coaching_sessions
               WHERE status='active' AND survey_sent_at IS NULL"""
        )
        for session in sessions:
            await self._process_session_voice_state(guild, session)

    async def _process_session_voice_state(self, guild: discord.Guild, session) -> None:
        session_id = session["id"]
        if session_id in self._survey_dispatching:
            return

        try:
            coach_id = int(session["coach_id"])
        except (TypeError, ValueError):
            return

        user_member = guild.get_member(session["discord_user_id"])
        coach_member = guild.get_member(coach_id)
        shared_channel = self._get_shared_coaching_voice_channel(user_member, coach_member)
        now = int(time.time())

        if shared_channel:
            db.execute(
                """UPDATE coaching_sessions
                   SET voice_channel_id=?, voice_started_at=COALESCE(voice_started_at, ?),
                       voice_last_seen_at=?
                   WHERE id=?""",
                (shared_channel.id, now, now, session["id"]),
            )
            return

        if not session["voice_started_at"]:
            return

        self._survey_dispatching.add(session_id)
        try:
            coach_name = coach_member.display_name if coach_member else f"Coach {coach_id}"
            success = await self.send_survey_dm(session["discord_user_id"], session_id, coach_name)
            if not success:
                return

            db.execute(
                """UPDATE coaching_sessions
                   SET status='waiting_survey', completed_at=?, survey_sent_at=?, voice_last_seen_at=?
                   WHERE id=?""",
                (now, now, now, session_id),
            )
            await self._remove_active_role(guild, session["discord_user_id"])
        finally:
            self._survey_dispatching.discard(session_id)

    async def _remove_active_role(self, guild: discord.Guild, user_id: int) -> None:
        member = guild.get_member(user_id)
        if not member:
            return
        coaching_role = guild.get_role(settings.coaching_active_role_id)
        if coaching_role and coaching_role in member.roles:
            await member.remove_roles(coaching_role, reason="Coaching Session beendet")

    async def send_survey_dm(self, user_id: int, session_id: str, coach_name: str) -> bool:
        """Send survey DM to user after coaching session"""
        try:
            user = self.bot.get_user(user_id)
            if not user:
                user = await self.bot.fetch_user(user_id)
        except Exception as e:
            log.error(f"Could not fetch user {user_id}: {e}")
            return False

        if not user:
            return False

        try:
            embed = discord.Embed(
                title="🎮 Coaching Session Feedback",
                description=f"Deine Coaching-Session mit **{coach_name}** ist abgeschlossen!",
                color=discord.Color.green(),
            )
            embed.add_field(
                name="Wie war's?",
                value="Bitte nimm dir 2 Minuten Zeit für unser Feedback-Formular. "
                "Dein Feedback hilft uns die Coaching-Qualität zu verbessern!",
                inline=False,
            )
            embed.add_field(
                name="⏰ Bitte innerhalb von 24h ausfüllen",
                value="Dein Feedback ist wichtig damit wir unsere Coaches verbessern können.",
                inline=False,
            )

            view = SurveyView(session_id)
            await user.send(embed=embed, view=view)
            return True
        except Exception as e:
            log.error(f"Failed to send survey DM to {user_id}: {e}")
            return False

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        guild = member.guild
        sessions = db.query_all(
            """SELECT * FROM coaching_sessions
               WHERE status='active' AND survey_sent_at IS NULL
               AND (discord_user_id=? OR coach_id=?)""",
            (member.id, member.id),
        )
        for session in sessions:
            await self._process_session_voice_state(guild, session)

    @app_commands.command(name="coaching-survey-senden", description="Survey DM senden (Admin)")
    @app_commands.describe(
        user_id="Discord User ID", session_id="Session ID", coach_name="Coach Name"
    )
    async def send_survey(
        self, interaction: discord.Interaction, user_id: str, session_id: str, coach_name: str
    ):
        """Admin command to send survey DM"""
        if not interaction.guild:
            await interaction.response.send_message("❌ Nur im Server.", ephemeral=True)
            return

        if not interaction.user.id == interaction.guild.owner_id:
            await interaction.response.send_message("❌ Nur Server-Owner.", ephemeral=True)
            return

        success = await self.send_survey_dm(int(user_id), session_id, coach_name)
        if success:
            await interaction.response.send_message("✅ Survey DM gesendet!", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Konnte DM nicht senden.", ephemeral=True)

    @app_commands.command(
        name="coaching-session-beenden", description="Session beenden und Survey senden (Admin)"
    )
    @app_commands.describe(session_id="Session ID")
    async def end_session(self, interaction: discord.Interaction, session_id: str):
        """Admin command to end session and trigger survey"""
        if not interaction.guild:
            await interaction.response.send_message("❌ Nur im Server.", ephemeral=True)
            return

        if not interaction.user.id == interaction.guild.owner_id:
            await interaction.response.send_message("❌ Nur Server-Owner.", ephemeral=True)
            return

        session = db.query_one("SELECT * FROM coaching_sessions WHERE id=?", (session_id,))
        if not session:
            await interaction.response.send_message("❌ Session nicht gefunden.", ephemeral=True)
            return

        if session["status"] == "completed":
            await interaction.response.send_message("❌ Session bereits beendet.", ephemeral=True)
            return

        # Get coach info
        coach_id = session["coach_id"]
        coach_member = interaction.guild.get_member(int(coach_id)) if coach_id else None
        coach_name = coach_member.display_name if coach_member else interaction.user.display_name

        # Update session
        db.execute(
            "UPDATE coaching_sessions SET status='waiting_survey', completed_at=?, survey_sent_at=? WHERE id=?",
            (int(time.time()), int(time.time()), session_id),
        )

        # Remove user's coaching role
        guild = interaction.guild
        await self._remove_active_role(guild, session["discord_user_id"])

        # Send survey DM
        success = await self.send_survey_dm(session["discord_user_id"], session_id, coach_name)

        if success:
            await interaction.response.send_message(
                "✅ Session beendet, Survey DM gesendet an User!", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "⚠️ Session beendet aber Survey DM konnte nicht gesendet werden.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(CoachingSurveyCog(bot))
