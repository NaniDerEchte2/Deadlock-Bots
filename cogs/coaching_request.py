"""
Coaching Request - AI Analyse und Channel Posting
"""
import asyncio
import logging
import time
import uuid
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from service import db
from service.config import settings

log = logging.getLogger(__name__)
_CLAIM_IN_PROGRESS: set[int] = set()


COACHING_ANALYSIS_SYSTEM = """Du bist ein Deadlock Coaching Koordinator.
Analysiere die Coaching-Anfrage und erstelle eine saubere, informative Zusammenfassung.

Gib zurück:
1. Eine 2-3 Sätze Zusammenfassung was der Spieler braucht
2. 3-5 Key-Fokuspunkte für den Coach
3. Priorität: low/medium/high
4. Kurze Einschätzung was der Spieler verbessern sollte

Sei spezifisch für Deadlock Gameplay."""

DISCORD_EMBED_FIELD_LIMIT = 1024


def _normalize_inline_text(value: str, *, fallback: str = "N/A", limit: int = 256) -> str:
    text = " ".join((value or "").split())
    if not text:
        return fallback
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _format_ai_summary_for_embed(value: str, *, limit: int = DISCORD_EMBED_FIELD_LIMIT) -> str:
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return "Keine AI-Analyse verfügbar."

    cleaned_lines: list[str] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        while line.startswith("#"):
            line = line[1:].strip()
        line = line.replace("**", "").strip()
        cleaned_lines.append(line)

    cleaned_text = "\n".join(cleaned_lines).strip() or "Keine AI-Analyse verfügbar."
    if len(cleaned_text) <= limit:
        return cleaned_text

    truncated = cleaned_text[: limit - 1].rstrip()
    split_at = max(truncated.rfind("\n"), truncated.rfind(". "), truncated.rfind("; "), truncated.rfind(", "))
    if split_at >= max(120, limit // 2):
        truncated = truncated[:split_at].rstrip()
    return truncated + "…"


def _get_availability_label(value: str) -> str:
    labels = {
        "weekday_evening": "Weekday Abends (18-22)",
        "weekend_morning": "Weekend Morgens (10-14)",
        "weekend_afternoon": "Weekend Nachmittags (14-18)",
        "weekday_morning": "Weekday Morgens",
        "anytime": "Jederzeit",
    }
    return labels.get(value, value)


class CoachClaimButton(discord.ui.Button):
    def __init__(self, request_id: int, author_id: int):
        super().__init__(
            label="Coach melden",
            style=discord.ButtonStyle.success,
            custom_id=f"coach_claim_{request_id}",
        )
        self.request_id = request_id
        self.author_id = author_id

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("❌ Nur im Server nutzbar.", ephemeral=True)
            return
        if self.request_id in _CLAIM_IN_PROGRESS:
            await interaction.response.send_message(
                "❌ Diese Anfrage wird gerade von einem anderen Coach bearbeitet.",
                ephemeral=True,
            )
            return
        _CLAIM_IN_PROGRESS.add(self.request_id)

        try:
            # Check if user has coach role
            coach_role = interaction.guild.get_role(settings.coach_role_id)
            if not coach_role or coach_role not in interaction.user.roles:
                await interaction.response.send_message(
                    "❌ Nur Coaches können sich für Sessions melden!",
                    ephemeral=True
                )
                return

            # Get request
            request = db.query_one(
                "SELECT * FROM coaching_requests WHERE id=?",
                (self.request_id,)
            )
            if not request:
                await interaction.response.send_message("❌ Request nicht gefunden.", ephemeral=True)
                return
            if request["status"] == "matched":
                await interaction.response.send_message(
                    "❌ Diese Anfrage wurde bereits von einem Coach geclaimt.",
                    ephemeral=True,
                )
                return

            # Get author
            author = interaction.guild.get_member(request["discord_user_id"])
            if not author:
                await interaction.response.send_message("❌ User nicht gefunden.", ephemeral=True)
                return

            # Create thread for coach and user
            thread_name = f"Coaching: {author.display_name}"

            try:
                # Create private thread
                thread = await interaction.channel.create_thread(
                    name=thread_name,
                    type=discord.ChannelType.private_thread,
                    invitable=False,
                )

                # Add both parties
                await thread.add_user(author)
                await thread.add_user(interaction.user)

                # Store session
                session_id = str(uuid.uuid4())
                expires_at = request["role_expires_at"] or (
                    int(time.time()) + (settings.coaching_role_expiry_hours * 60 * 60)
                )

                db.execute(
                    """INSERT INTO coaching_sessions (id, request_id, coach_id, discord_user_id,
                       discord_username, discord_channel_id, discord_thread_id, status,
                       role_assigned_at, role_expires_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                    (
                        session_id, request["id"], interaction.user.id, author.id,
                        author.display_name, interaction.channel.id, thread.id,
                        int(time.time()), expires_at, int(time.time())
                    )
                )

                # Update request status
                db.execute(
                    "UPDATE coaching_requests SET status='matched', updated_at=? WHERE id=?",
                    (int(time.time()), self.request_id)
                )

                # Make sure the active coaching role is present once a coach claimed the request.
                coaching_role = interaction.guild.get_role(settings.coaching_active_role_id)
                if coaching_role and coaching_role not in author.roles:
                    await author.add_roles(coaching_role, reason="Coaching Session gestartet")

                # Send messages
                await thread.send(
                    f"🎮 **Coaching Session gestartet!**\n\n"
                    f"**Coach:** {interaction.user.mention}\n"
                    f"**User:** {author.mention}\n\n"
                    f"Bitte besprecht eure Ziele und plant die Session!"
                )

                await author.send(
                    f"🎉 Ein Coach hat sich für deine Anfrage gemeldet!\n\n"
                    f"**Coach:** {interaction.user.display_name}\n"
                    f"**Thread:** {thread.mention}\n\n"
                    f"Ihr könnt euch dort direkt abstimmen und das Coaching innerhalb der nächsten "
                    f"**{settings.coaching_role_expiry_hours} Stunden** durchführen."
                )

                # Disable the button
                self.disabled = True
                await interaction.response.edit_message(view=None)

                await interaction.followup.send(
                    f"✅ Session mit {author.display_name} gestartet!",
                    ephemeral=True
                )

            except Exception as e:
                log.error(f"Error creating coaching thread: {e}")
                await interaction.response.send_message(
                    f"❌ Fehler beim Starten der Session: {e}",
                    ephemeral=True
                )
        finally:
            _CLAIM_IN_PROGRESS.discard(self.request_id)


class CoachClaimView(discord.ui.View):
    def __init__(self, request_id: int, author_id: int):
        super().__init__(timeout=None)
        self.add_item(CoachClaimButton(request_id, author_id))


class CoachingRequestCog(commands.Cog):
    """Coaching Request - AI Analyse und Channel Management"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._analyze_loop: asyncio.Task | None = None

    async def cog_load(self):
        if self._analyze_loop is None or self._analyze_loop.done():
            self._analyze_loop = asyncio.create_task(self._analyze_pending_requests())

    async def cog_unload(self):
        if self._analyze_loop:
            self._analyze_loop.cancel()
            self._analyze_loop = None

    def _get_ai_connector(self):
        """Get AIConnector cog if available"""
        return self.bot.get_cog("AIConnector")

    async def _get_primary_guild(self) -> discord.Guild | None:
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild:
            return guild
        if settings.guild_id:
            try:
                return await self.bot.fetch_guild(settings.guild_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return None

    async def _get_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        member = guild.get_member(user_id)
        if member:
            return member
        try:
            return await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _assign_request_role(self, request_data: dict) -> None:
        guild = await self._get_primary_guild()
        if not guild:
            log.warning("Could not resolve guild for coaching role assignment")
            return

        member = await self._get_member(guild, request_data["discord_user_id"])
        if not member:
            log.warning("Could not resolve member %s for coaching role assignment", request_data["discord_user_id"])
            return

        now = int(time.time())
        expires_at = now + (settings.coaching_role_expiry_hours * 60 * 60)
        db.execute(
            """UPDATE coaching_requests
               SET role_assigned_at=COALESCE(role_assigned_at, ?),
                   role_expires_at=COALESCE(role_expires_at, ?),
                   updated_at=?
               WHERE id=?""",
            (now, expires_at, now, request_data["id"]),
        )

        role = guild.get_role(settings.coaching_active_role_id)
        if not role:
            log.warning("Coaching active role %s not found in guild %s", settings.coaching_active_role_id, guild.id)
            return
        if role in member.roles:
            return

        await member.add_roles(role, reason="Coaching-Anfrage analysiert")
        log.info("Assigned coaching role %s to user %s for request %s", role.id, member.id, request_data["id"])

    async def _analyze_with_ai(self, request_data: dict) -> str:
        """Use MiniMax to analyze the coaching request"""
        ai_connector = self._get_ai_connector()
        if not ai_connector:
            return f"**Analyse:**\n{request_data.get('current_problems', 'Keine Probleme beschrieben')}"

        prompt = f"""Analysiere diese Deadlock Coaching-Anfrage:

- Rang: {request_data.get('rank', 'N/A')} Subrank {request_data.get('subrank', 'N/A')}
- Hero: {request_data.get('hero', 'N/A')}
- Games: {request_data.get('games_played', 'N/A')}
- Stunden: {request_data.get('hours_played', 'N/A')}
- Verfügbarkeit: {request_data.get('availability', 'N/A')}
- Probleme: {request_data.get('current_problems', 'N/A')}

Erstelle eine präzise, hilfreiche Zusammenfassung für den Coach."""

        try:
            text, meta = await ai_connector.generate_text(
                provider="minimax",
                prompt=prompt,
                system_prompt=COACHING_ANALYSIS_SYSTEM,
                model="MiniMax-M2.7",
                max_output_tokens=500,
                temperature=0.7,
            )
            if text:
                return text
        except Exception as e:
            log.error(f"AI analysis failed: {e}")

        return f"**Probleme:** {request_data.get('current_problems', 'N/A')}"

    async def _get_coaching_channel(self) -> discord.TextChannel | None:
        """Get the coaching requests channel"""
        guild = await self._get_primary_guild()
        if not guild:
            return None
        channel = guild.get_channel(settings.coaching_request_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(settings.coaching_request_channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    async def _post_request_to_channel(self, request_data: dict, ai_summary: str):
        """Post formatted coaching request to channel"""
        channel = await self._get_coaching_channel()
        if not channel:
            log.error("Coaching channel not found")
            return None

        guild = channel.guild
        member = await self._get_member(guild, request_data["discord_user_id"]) if guild else None

        embed = discord.Embed(
            title="🎮 Neue Coaching-Anfrage",
            color=discord.Color.blue(),
        )

        username = request_data.get("discord_username", "Unknown")
        if member:
            embed.set_author(name=username, icon_url=member.display_avatar.url if member.display_avatar else None)

        rank = request_data.get("rank", "N/A")
        subrank = request_data.get("subrank", "1")
        hero = _normalize_inline_text(request_data.get("hero", "Nicht angegeben"), fallback="Nicht angegeben")
        games = _normalize_inline_text(request_data.get("games_played", "N/A"))
        hours = _normalize_inline_text(request_data.get("hours_played", "N/A"))
        availability = _normalize_inline_text(
            _get_availability_label(request_data.get("availability", "anytime")),
            fallback="Nicht angegeben",
            limit=256,
        )
        problems = _normalize_inline_text(
            request_data.get("current_problems", "Keine Beschreibung"),
            fallback="Keine Beschreibung",
            limit=DISCORD_EMBED_FIELD_LIMIT,
        )
        ai_summary_text = _format_ai_summary_for_embed(ai_summary)

        embed.add_field(name="Rang", value=f"{rank} (Subrank {subrank})", inline=True)
        embed.add_field(name="Hero", value=hero, inline=True)
        embed.add_field(name="Games", value=games, inline=True)
        embed.add_field(name="Stunden", value=hours, inline=True)
        embed.add_field(name="Verfügbar", value=availability, inline=True)
        embed.add_field(name="📝 Probleme", value=problems or "Keine", inline=False)
        embed.add_field(name="🤖 AI Analyse", value=ai_summary_text, inline=False)

        view = CoachClaimView(request_data["id"], request_data["discord_user_id"])

        try:
            user_mention = f"<@{request_data['discord_user_id']}>"
            content = f"📥 Anfrage von {user_mention}"
            message = await channel.send(content=content, embed=embed, view=view)

            # Update request with message info
            db.execute(
                "UPDATE coaching_requests SET message_id=?, channel_id=?, ai_summary=?, status='analyzed', updated_at=? WHERE id=?",
                (message.id, channel.id, ai_summary, int(time.time()), request_data["id"])
            )
            request_data["channel_id"] = channel.id
            request_data["message_id"] = message.id
            request_data["status"] = "analyzed"
            return message.id
        except Exception as e:
            log.error(f"Error posting to channel: {e}")
            return None

    async def _trigger_analysis_for_user(self, user_id: int) -> None:
        try:
            row = db.query_one(
                """SELECT * FROM coaching_requests
                   WHERE discord_user_id=? AND status='pending'
                   AND current_problems IS NOT NULL AND current_problems != ''
                   AND (ai_summary IS NULL OR ai_summary = '')
                   ORDER BY created_at DESC LIMIT 1""",
                (user_id,),
            )
            if not row:
                log.info("No pending coaching request ready for analysis for user %s", user_id)
                return

            request_data = dict(row)
            ai_summary = await self._analyze_with_ai(request_data)
            db.execute(
                "UPDATE coaching_requests SET ai_summary=?, updated_at=? WHERE id=?",
                (ai_summary, int(time.time()), request_data["id"]),
            )
            message_id = await self._post_request_to_channel(request_data, ai_summary)
            if message_id:
                await self._assign_request_role(request_data)
            else:
                log.warning("Coaching request %s could not be posted to the coaching channel", request_data["id"])
        except Exception:
            log.exception("Immediate coaching analysis failed for user %s", user_id)

    async def _analyze_pending_requests(self):
        """Background loop to analyze pending requests with AI"""
        await self.bot.wait_until_ready()
        while True:
            try:
                # Find requests that have problems filled but not yet analyzed
                rows = db.query_all(
                    """SELECT * FROM coaching_requests
                       WHERE status='pending'
                       AND current_problems IS NOT NULL
                       AND current_problems != ''
                       AND (ai_summary IS NULL OR ai_summary = '')
                       ORDER BY created_at ASC LIMIT 5"""
                )

                for row in rows:
                    request_data = dict(row)
                    ai_summary = await self._analyze_with_ai(request_data)

                    # Update with AI summary
                    db.execute(
                        "UPDATE coaching_requests SET ai_summary=?, updated_at=? WHERE id=?",
                        (ai_summary, int(time.time()), request_data["id"])
                    )

                    # Post to channel
                    message_id = await self._post_request_to_channel(request_data, ai_summary)
                    if message_id:
                        await self._assign_request_role(request_data)

                    # Small delay between requests
                    await asyncio.sleep(2)

            except Exception as e:
                log.error(f"Analyze loop error: {e}")

            await asyncio.sleep(30)  # Check every 30 seconds

    @app_commands.command(name="coaching-analysieren", description="Analysiere Request manuell (Admin)")
    @app_commands.describe(request_id="Request ID")
    async def analyze_request(self, interaction: discord.Interaction, request_id: str):
        """Admin command to manually analyze a request"""
        if not interaction.guild:
            await interaction.response.send_message("❌ Nur im Server nutzbar.", ephemeral=True)
            return

        # Check admin
        if not interaction.user.id == interaction.guild.owner_id:
            await interaction.response.send_message("❌ Nur Server-Owner.", ephemeral=True)
            return

        row = db.query_one("SELECT * FROM coaching_requests WHERE id=?", (int(request_id),))
        if not row:
            await interaction.response.send_message("❌ Request nicht gefunden.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        request_data = dict(row)
        ai_summary = await self._analyze_with_ai(request_data)

        db.execute(
            "UPDATE coaching_requests SET ai_summary=?, updated_at=? WHERE id=?",
            (ai_summary, int(time.time()), request_data["id"])
        )

        message_id = await self._post_request_to_channel(request_data, ai_summary)
        if message_id:
            await self._assign_request_role(request_data)

        if message_id:
            await interaction.followup.send(
                f"✅ Request analysiert und gepostet!\n\n**AI Summary:**\n{ai_summary[:500]}...",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ Konnte nicht posten. Channel ID prüfen.\n\n**AI Summary:**\n{ai_summary[:500]}...",
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(CoachingRequestCog(bot))
