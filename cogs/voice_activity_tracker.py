import discord
from discord.ext import commands, tasks
import logging
import json
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Optional
from functools import lru_cache
from collections import defaultdict, deque
from dataclasses import dataclass

# zentrale DB-API (synchron, mit internem Lock), KEINE eigenen Tabellen-Anlagen hier!
from service import db as central_db

logger = logging.getLogger(__name__)

# ========= Zentrale Einzel-DB Pfad (nur zur Anzeige/Diagnose) =========
def central_db_path() -> str:
    # reine Anzeige – tatsächlicher Zugriff läuft über shared.db
    try:
        return central_db._db_file()
    except Exception:
        # Fallback NUR für Anzeige, nicht für Zugriff
        return os.path.expandvars(r"%USERPROFILE%\Documents\Deadlock\service\deadlock.sqlite3")

# ========= Defaults (werden pro Guild via kv_store überschrieben) =========
@dataclass
class VoiceTrackerConfig:
    min_users_for_tracking: int = 2
    grace_period_duration: int = 180  # 3 Minuten
    session_timeout: int = 300        # 5 Minuten
    afk_timeout: int = 1800           # aktuell ungenutzt
    special_role_id: int = 1313624729466441769
    max_sessions_per_user: int = 100

# ========= Rate Limiter =========
class RateLimiter:
    def __init__(self, max_requests: int = 10, time_window: int = 60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = defaultdict(deque)

    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        user_requests = self.requests[user_id]
        while user_requests and user_requests[0] < now - self.time_window:
            user_requests.popleft()
        if len(user_requests) < self.max_requests:
            user_requests.append(now)
            return True
        return False

    def get_remaining_time(self, user_id: int) -> int:
        now = time.time()
        user_requests = self.requests[user_id]
        if not user_requests:
            return 0
        oldest_request = user_requests[0]
        return max(0, int(self.time_window - (now - oldest_request)))

# ========= Config Manager (per Guild in kv_store) =========
class ConfigManager:
    """
    Persistenz ausschließlich über zentrale DB (kv_store).
    ns='voice_cfg', k=str(guild_id), v=json mit Config.
    """
    NS = "voice_cfg"

    def __init__(self, defaults: VoiceTrackerConfig):
        self.defaults = defaults
        self._cache: Dict[int, VoiceTrackerConfig] = {}

    def _load_from_db(self, guild_id: int) -> Optional[VoiceTrackerConfig]:
        raw = central_db.get_kv(self.NS, str(guild_id))
        if not raw:
            return None
        try:
            d = json.loads(raw)
            return VoiceTrackerConfig(
                min_users_for_tracking=int(d.get("min_users_for_tracking", self.defaults.min_users_for_tracking)),
                grace_period_duration=int(d.get("grace_period_duration", self.defaults.grace_period_duration)),
                session_timeout=int(d.get("session_timeout", self.defaults.session_timeout)),
                afk_timeout=int(d.get("afk_timeout", self.defaults.afk_timeout)),
                special_role_id=int(d.get("special_role_id", self.defaults.special_role_id)),
                max_sessions_per_user=int(d.get("max_sessions_per_user", self.defaults.max_sessions_per_user)),
            )
        except Exception:
            return None

    def _save_to_db(self, guild_id: int, cfg: VoiceTrackerConfig) -> None:
        payload = {
            "min_users_for_tracking": cfg.min_users_for_tracking,
            "grace_period_duration": cfg.grace_period_duration,
            "session_timeout": cfg.session_timeout,
            "afk_timeout": cfg.afk_timeout,
            "special_role_id": cfg.special_role_id,
            "max_sessions_per_user": cfg.max_sessions_per_user,
        }
        central_db.set_kv(self.NS, str(guild_id), json.dumps(payload, separators=(",", ":")))

    async def get(self, guild_id: int) -> VoiceTrackerConfig:
        if guild_id in self._cache:
            return self._cache[guild_id]
        cfg = self._load_from_db(guild_id) or self.defaults
        # falls nicht vorhanden, sofort persistieren (ohne neue Tabellen zu erstellen)
        if self._load_from_db(guild_id) is None:
            self._save_to_db(guild_id, cfg)
        self._cache[guild_id] = cfg
        return cfg

    async def set(self, guild_id: int, field: str, value):
        cfg = await self.get(guild_id)
        if not hasattr(cfg, field):
            raise ValueError(f"Unknown field: {field}")
        setattr(cfg, field, value)
        self._save_to_db(guild_id, cfg)
        self._cache[guild_id] = cfg

# ========= Voice Cog (nur zentrale DB-Tabellen: voice_stats, kv_store) =========
class VoiceActivityTrackerCog(commands.Cog):
    """
    Voice-Tracking auf EINER zentralen DB:
      - aggregiert pro User in voice_stats (user_id, total_seconds, total_points, last_update)
      - Config pro Guild in kv_store(ns='voice_cfg')
    Keine Migration, keine Table-Erzeugung in diesem Cog.
    """

    def __init__(self, bot):
        self.bot = bot
        self.defaults = VoiceTrackerConfig()
        self.config_manager = ConfigManager(self.defaults)

        # runtime state
        self.voice_sessions: Dict[str, Dict] = {}   # key=f"{user_id}:{guild_id}" → session dict
        self.grace_period_users: Dict[str, Dict] = {}
        self.rate_limiter = RateLimiter(max_requests=5, time_window=30)

        self.session_stats = {
            'total_sessions_created': 0,
            'total_grace_periods': 0,
            'uptime_start': datetime.utcnow()
        }

        logger.info("Enhanced Voice Activity Tracker initializing (DB-centralized)")

    async def cog_load(self):
        # shared.db initialisiert Schema beim connect() selbst – hier nur Smoke-Test:
        try:
            _ = central_db.query_one("SELECT 1")
        except Exception as e:
            logger.error(f"Central DB not available: {e}")
            raise

        # Background tasks (keine Backups/Migrationen hier)
        self.cleanup_sessions.start()
        self.update_sessions.start()
        self.grace_period_monitor.start()
        self.health_check.start()

        logger.info("Voice Activity Tracker initialized (DB-centralized)")

    def cog_unload(self):
        tasks_to_cancel = [
            self.cleanup_sessions, self.update_sessions,
            self.grace_period_monitor, self.health_check
        ]
        for task in tasks_to_cancel:
            if task.is_running():
                task.cancel()
        logger.info("Voice Activity Tracker unloaded")

    # ===== Helpers =====
    async def cfg(self, guild_id: int) -> VoiceTrackerConfig:
        return await self.config_manager.get(guild_id)

    async def has_grace_period_role(self, member: discord.Member) -> bool:
        cfg = await self.cfg(member.guild.id)
        return any(role.id == cfg.special_role_id for role in member.roles)

    def calculate_points(self, seconds: int, peak_users: int) -> int:
        """Berechnet Punkte für eine abgeschlossene Session."""
        if seconds <= 0:
            return 0
        base_points = seconds // 60  # 1 Punkt pro Minute
        # kleiner Bonus bei hoher Aktivität im Channel
        if base_points > 0:
            if peak_users >= 5:
                base_points += max(1, base_points // 10)
            elif peak_users >= 3:
                base_points += max(1, base_points // 20)
        return max(0, base_points)

    def _finalize_session(self, session: Dict, end_time: datetime):
        seconds = max(0, int((end_time - session['start_time']).total_seconds()))
        if seconds <= 0:
            return 0, 0
        points = self.calculate_points(seconds, session.get('peak_users') or 1)
        try:
            central_db.execute(
                """
                INSERT INTO voice_stats(user_id, total_seconds, total_points, last_update)
                VALUES(?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                  total_seconds = total_seconds + excluded.total_seconds,
                  total_points  = total_points  + excluded.total_points,
                  last_update   = CURRENT_TIMESTAMP
                """,
                (session['user_id'], seconds, points)
            )
        except Exception as e:
            logger.error(f"DB write failed on session finalize: {e}")
        return seconds, points

    def is_user_active_basic(self, voice_state: discord.VoiceState) -> bool:
        if not voice_state or not voice_state.channel:
            return False
        if getattr(voice_state, "afk", False):
            return False
        is_muted_or_deaf = (voice_state.mute or voice_state.deaf or
                            voice_state.self_mute or voice_state.self_deaf)
        return not is_muted_or_deaf

    async def is_user_active(self, member: discord.Member) -> bool:
        vs = member.voice
        if not vs or not vs.channel:
            return False
        if self.is_user_active_basic(vs):
            return True
        if await self.has_grace_period_role(member):
            grace_key = f"{member.id}:{member.guild.id}"
            if grace_key in self.grace_period_users:
                cfg = await self.cfg(member.guild.id)
                time_in_grace = (datetime.utcnow() - self.grace_period_users[grace_key]['start_time']).total_seconds()
                if time_in_grace <= cfg.grace_period_duration:
                    return True
        return False

    async def start_grace_period(self, member: discord.Member):
        if not await self.has_grace_period_role(member):
            return
        grace_key = f"{member.id}:{member.guild.id}"
        if grace_key in self.grace_period_users:
            return
        self.grace_period_users[grace_key] = {
            'user_id': member.id,
            'guild_id': member.guild.id,
            'channel_id': member.voice.channel.id if member.voice else None,
            'start_time': datetime.utcnow(),
        }
        self.session_stats['total_grace_periods'] += 1
        logger.info(f"Grace period started for {member.display_name} ({member.id})")

    async def end_grace_period(self, member_id: int, guild_id: int, reason: str = "timeout"):
        grace_key = f"{member_id}:{guild_id}"
        if grace_key in self.grace_period_users:
            del self.grace_period_users[grace_key]
            logger.info(f"Grace period ended for {member_id} ({reason})")

    async def _resolve_display_name(self, guild: discord.Guild, user_id: int) -> str:
        """Resolve a stable display name for leaderboard rows."""
        member = guild.get_member(user_id)
        if member:
            return member.display_name

        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            member = None
        except discord.HTTPException as e:
            logger.debug(f"Failed to fetch guild member {user_id}: {e}")
            member = None

        if member:
            return member.display_name

        user = self.bot.get_user(user_id)
        if user:
            return user.display_name

        try:
            user = await self.bot.fetch_user(user_id)
        except discord.NotFound:
            user = None
        except discord.HTTPException as e:
            logger.debug(f"Failed to fetch user {user_id}: {e}")
            user = None

        return user.display_name if user else f"User {user_id}"

    async def start_voice_session(self, member: discord.Member, channel: discord.VoiceChannel):
        key = f"{member.id}:{channel.guild.id}"
        if key not in self.voice_sessions:
            self.voice_sessions[key] = {
                'user_id': member.id,
                'guild_id': channel.guild.id,
                'channel_id': channel.id,
                'channel_name': channel.name,
                'start_time': datetime.utcnow(),
                'last_update': datetime.utcnow(),
                'total_time': 0,  # Sekunden seit Start
                'peak_users': 1,
                'user_counts': [],
            }
            self.session_stats['total_sessions_created'] += 1
            logger.info(f"Started voice session: {member.display_name} in {channel.name}")

    async def end_voice_session(self, member: discord.Member, guild_id: int):
        key = f"{member.id}:{guild_id}"
        session = self.voice_sessions.pop(key, None)
        if not session:
            await self.end_grace_period(member.id, guild_id, "voice_leave")
            return

        # finalisieren & persistieren
        end_time = datetime.utcnow()
        seconds, points = self._finalize_session(session, end_time)
        if seconds > 0:
            logger.info(
                f"Ended voice session: {member.display_name}, {seconds}s, {points}pts"
            )

        await self.end_grace_period(member.id, guild_id, "voice_leave")

    # ===== Discord Events =====
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        try:
            if member.bot:
                return

            # Logik für Grace-Start/-Ende bei (Un)Mute
            if (before.channel and after.channel and before.channel == after.channel):
                was_muted = before.mute or before.self_mute or before.deaf or before.self_deaf
                is_muted = after.mute or after.self_mute or after.deaf or after.self_deaf
                if not was_muted and is_muted and await self.has_grace_period_role(member):
                    await self.start_grace_period(member)
                elif was_muted and not is_muted:
                    await self.end_grace_period(member.id, member.guild.id, "unmuted")

            # Channel-Wechsel
            if before.channel != after.channel:
                if before.channel:
                    await self.handle_voice_leave(member, before.channel)
                if after.channel:
                    await self.handle_voice_join(member, after.channel)
            elif after.channel:
                await self.update_channel_sessions(after.channel)

        except Exception as e:
            logger.error(f"Error in voice state update: {e}")

    async def handle_voice_join(self, member: discord.Member, channel: discord.VoiceChannel):
        await self.update_channel_sessions(channel)

    async def handle_voice_leave(self, member: discord.Member, channel: discord.VoiceChannel):
        await self.end_voice_session(member, channel.guild.id)
        await self.update_channel_sessions(channel)

    async def update_channel_sessions(self, channel: discord.VoiceChannel):
        if not channel.members:
            return

        # aktive User (ungemutet oder in Grace)
        active_users = []
        for m in channel.members:
            if m.bot:
                continue
            if await self.is_user_active(m):
                active_users.append(m)

        user_count = len(active_users)
        cfg = await self.cfg(channel.guild.id)

        # peak/avg-Statistik für laufende Sessions
        for member in active_users:
            k = f"{member.id}:{channel.guild.id}"
            if k in self.voice_sessions:
                s = self.voice_sessions[k]
                s['user_counts'].append(user_count)
                s['peak_users'] = max(s['peak_users'], user_count)

        # Sessions starten/aktualisieren/enden
        if user_count >= cfg.min_users_for_tracking and active_users:
            for member in active_users:
                k = f"{member.id}:{channel.guild.id}"
                if k not in self.voice_sessions:
                    await self.start_voice_session(member, channel)
                if k in self.voice_sessions:
                    self.voice_sessions[k]['last_update'] = datetime.utcnow()

        # alle, die NICHT aktiv sind → Session ggf. beenden
        for member in channel.members:
            if member.bot:
                continue
            k = f"{member.id}:{channel.guild.id}"
            if k in self.voice_sessions and member not in active_users:
                await self.end_voice_session(member, channel.guild.id)

    # ===== BACKGROUND TASKS =====
    @tasks.loop(minutes=2)
    async def update_sessions(self):
        if not self.voice_sessions:
            return
        now = datetime.utcnow()
        # lediglich Keep-Alive/Peak-Update – keine Punkteberechnung in DB
        for k, s in list(self.voice_sessions.items()):
            s['last_update'] = now

    @update_sessions.before_loop
    async def before_update_sessions(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=60)
    async def grace_period_monitor(self):
        if not self.grace_period_users:
            return
        now = datetime.utcnow()
        expired = []
        for k, g in self.grace_period_users.items():
            guild_id = g['guild_id']
            cfg = await self.cfg(guild_id)
            if (now - g['start_time']).total_seconds() >= cfg.grace_period_duration:
                expired.append((g['user_id'], guild_id))
        for uid, gid in expired:
            await self.end_grace_period(uid, gid, "timeout_3min")

    @grace_period_monitor.before_loop
    async def before_grace_period_monitor(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def cleanup_sessions(self):
        if not self.voice_sessions:
            return
        now = datetime.utcnow()
        to_close = []
        for k, s in self.voice_sessions.items():
            cfg = await self.cfg(s['guild_id'])
            cutoff = now - timedelta(seconds=cfg.session_timeout)
            if s['last_update'] < cutoff:
                to_close.append(k)
        for k in to_close:
            s = self.voice_sessions.get(k)
            if not s:
                continue
            end_time = s['last_update']
            seconds, points = self._finalize_session(s, end_time)
            user = self.bot.get_user(s['user_id'])
            if seconds > 0:
                display_name = user.display_name if user else s['user_id']
                logger.info(
                    f"Cleaned up inactive session: {display_name} ({seconds}s, {points}pts)"
                )
            self.voice_sessions.pop(k, None)

    @cleanup_sessions.before_loop
    async def before_cleanup_sessions(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=2)
    async def health_check(self):
        try:
            _ = central_db.query_one("SELECT 1")
            active_sessions = len(self.voice_sessions)
            grace_periods = len(self.grace_period_users)
            uptime = datetime.utcnow() - self.session_stats['uptime_start']
            logger.info(f"Health check: {active_sessions} sessions, {grace_periods} grace periods, uptime: {uptime}")
        except Exception as e:
            logger.error(f"Health check failed: {e}")

    @health_check.before_loop
    async def before_health_check(self):
        await self.bot.wait_until_ready()

    # ===== COMMANDS =====
    @commands.command(name="vstats")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def voice_stats_command(self, ctx, user: Optional[discord.Member] = None):
        if not self.rate_limiter.is_allowed(ctx.author.id):
            remaining = self.rate_limiter.get_remaining_time(ctx.author.id)
            await ctx.send(f"⏰ Rate limit reached. Try again in {remaining} seconds.")
            return

        target_user = user or ctx.author
        try:
            # Gesamtzeit (global über voice_stats)
            row = central_db.query_one(
                "SELECT total_seconds, total_points FROM voice_stats WHERE user_id=?",
                (target_user.id,)
            )
            total_seconds = int(row[0]) if row and row[0] else 0
            total_points = int(row[1]) if row and len(row) > 1 and row[1] is not None else 0

            # Live-Session addieren (nur Anzeige)
            session_key = f"{target_user.id}:{ctx.guild.id}"
            live_add = 0
            live_info = ""
            live_points = 0
            if session_key in self.voice_sessions:
                s = self.voice_sessions[session_key]
                live_add = int((datetime.utcnow() - s['start_time']).total_seconds())
                live_points = self.calculate_points(live_add, s.get('peak_users') or 1)
                live_info = f"🔴 Live: +{live_add//60}m / +{live_points}pts"

            total = total_seconds + live_add
            total_hours = total // 3600
            total_minutes = (total % 3600) // 60
            total_points_display = total_points + live_points

            embed = discord.Embed(
                title=f"📊 Voice-Statistiken - {target_user.display_name}",
                color=discord.Color.blue()
            )
            embed.add_field(name="⏱️ Gesamtzeit", value=f"{total_hours}h {total_minutes}m", inline=True)
            embed.add_field(name="⭐ Punkte", value=str(total_points_display), inline=True)
            if live_info:
                embed.add_field(name="Status", value=live_info, inline=True)

            # Grace-Info
            has_role = await self.has_grace_period_role(target_user)
            if has_role:
                embed.add_field(name="🎖️ Spezielle Rolle", value="Grace Period berechtigt (3 Min Schutz)", inline=False)

            embed.set_thumbnail(url=target_user.display_avatar.url)
            embed.set_footer(text=f"Angefragt von {ctx.author.display_name}")
            await ctx.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in vstats: {e}")
            await ctx.send(f"❌ Fehler beim Abrufen der Statistiken: {e}")

    @commands.command(name="vleaderboard", aliases=["vlb", "voicetop"])
    @commands.cooldown(1, 15, commands.BucketType.guild)
    async def voice_leaderboard_command(self, ctx):
        if not self.rate_limiter.is_allowed(ctx.author.id):
            remaining = self.rate_limiter.get_remaining_time(ctx.author.id)
            await ctx.send(f"⏰ Rate limit reached. Try again in {remaining} seconds.")
            return

        limit = 10

        try:
            rows = central_db.query_all(
                """
                SELECT user_id, total_seconds, total_points
                FROM voice_stats
                ORDER BY total_points DESC, total_seconds DESC
                LIMIT ?
                """,
                (limit,)
            )
            if not rows:
                await ctx.send("📊 Noch keine Voice-Aktivität aufgezeichnet.")
                return

            embed = discord.Embed(
                title=f"🏆 Voice-Leaderboard - {ctx.guild.name}",
                color=discord.Color.gold()
            )

            desc = ""
            for i, (uid, secs, pts) in enumerate(rows, 1):
                name = await self._resolve_display_name(ctx.guild, uid)
                hours = (secs or 0) // 3600
                minutes = ((secs or 0) % 3600) // 60
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                points_display = int(pts or 0)
                desc += f"{medal} **{name}** — {hours}h {minutes}m · {points_display} Punkte\n"
            embed.description = desc
            await ctx.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in vleaderboard: {e}")
            await ctx.send(f"❌ Fehler beim Abrufen des Leaderboards: {e}")

    @commands.command(name="vtest")
    async def voice_test_command(self, ctx):
        embed = discord.Embed(title="🔧 Voice System Test (Central DB)", color=0x00ff99)
        try:
            _ = central_db.query_one("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False
        embed.add_field(name="🗄️ Database", value="✅ Verbunden" if db_ok else "❌ Fehler", inline=True)
        embed.add_field(name="🔴 Live Sessions", value=len(self.voice_sessions), inline=True)

        cfg = await self.cfg(ctx.guild.id)
        embed.add_field(name="⏱️ Grace Duration", value=f"{cfg.grace_period_duration}s", inline=True)
        embed.add_field(name="🎖️ Special Role", value=f"<@&{cfg.special_role_id}>", inline=True)
        embed.add_field(name="👥 Min Users", value=cfg.min_users_for_tracking, inline=True)

        session_key = f"{ctx.author.id}:{ctx.guild.id}"
        if session_key in self.voice_sessions:
            s = self.voice_sessions[session_key]
            duration = int((datetime.utcnow() - s['start_time']).total_seconds())
            session_info = f"🔴 **Aktive Session**\n⏱️ {duration//60}m"
        else:
            session_info = "⭕ Keine aktive Session"
        embed.add_field(name="📊 Deine Session", value=session_info, inline=False)

        if ctx.author.voice:
            active_users = []
            for m in ctx.author.voice.channel.members:
                if not m.bot and await self.is_user_active(m):
                    active_users.append(m)
            voice_info = (f"🎵 **{ctx.author.voice.channel.name}**\n"
                          f"👥 {len(ctx.author.voice.channel.members)} User "
                          f"({len(active_users)} aktiv)\n"
                          f"✅ Du bist aktiv: {await self.is_user_active(ctx.author)}")
        else:
            voice_info = "❌ Nicht in Voice"
        embed.add_field(name="🎧 Voice Status", value=voice_info, inline=False)

        uptime = datetime.utcnow() - self.session_stats['uptime_start']
        stats_info = (f"🕐 Uptime: {uptime.days}d {uptime.seconds//3600}h\n"
                      f"📈 Sessions erstellt: {self.session_stats['total_sessions_created']}\n"
                      f"🛡️ Grace Periods: {self.session_stats['total_grace_periods']}")
        embed.add_field(name="📊 System Stats", value=stats_info, inline=True)

        embed.add_field(name="📁 DB Path", value=f"...{central_db_path()[-40:]}", inline=False)
        embed.set_footer(text="Enhanced Voice Activity Tracker (Central DB)")
        await ctx.send(embed=embed)

    # ===== ADMIN COMMANDS (Konfig nur über kv_store) =====
    @commands.command(name="voice_status")
    @commands.has_permissions(administrator=True)
    async def voice_status_command(self, ctx):
        try:
            cfg = await self.cfg(ctx.guild.id)
            embed = discord.Embed(title="🔧 Voice System Admin Status (Central DB)", color=0x00ff99)
            embed.add_field(name="🔴 Live Sessions", value=len(self.voice_sessions), inline=True)
            embed.add_field(name="🛡️ Grace Periods", value=len(self.grace_period_users), inline=True)
            try:
                _ = central_db.query_one("SELECT 1")
                db_state = "Connected"
            except Exception:
                db_state = "Disconnected"
            embed.add_field(name="🗄️ Database", value=db_state, inline=True)

            embed.add_field(name="👥 Min Users", value=cfg.min_users_for_tracking, inline=True)
            embed.add_field(name="⏱️ Grace Duration", value=f"{cfg.grace_period_duration}s", inline=True)
            embed.add_field(name="🎖️ Role ID", value=cfg.special_role_id, inline=True)

            uptime = datetime.utcnow() - self.session_stats['uptime_start']
            embed.add_field(name="🕐 Uptime", value=f"{uptime.days}d {uptime.seconds//3600}h", inline=True)
            embed.add_field(name="📁 DB Path", value=f"...{central_db_path()[-40:]}", inline=True)
            await ctx.send(embed=embed)
        except Exception as e:
            await ctx.send(f"❌ Fehler beim Abrufen des Status: {e}")

    @commands.command(name="voice_config")
    @commands.has_permissions(administrator=True)
    async def voice_config_command(self, ctx, setting=None, value=None):
        cfg = await self.cfg(ctx.guild.id)
        if not setting:
            embed = discord.Embed(title="⚙️ Voice Tracker Config (Central DB)", color=0x0099ff)
            embed.add_field(name="👥 Min Users", value=cfg.min_users_for_tracking, inline=True)
            embed.add_field(name="⏱️ Grace Duration", value=f"{cfg.grace_period_duration}s", inline=True)
            embed.add_field(name="🎖️ Special Role", value=cfg.special_role_id, inline=True)
            embed.add_field(name="🔄 Session Timeout", value=f"{cfg.session_timeout}s", inline=True)
            embed.add_field(name="📊 Max Sessions", value=cfg.max_sessions_per_user, inline=True)
            embed.add_field(
                name="Available Settings",
                value="```\n!voice_config grace_duration <seconds>\n!voice_config grace_role <role_id>\n!voice_config min_users <2-10>\n!voice_config session_timeout <seconds>\n!voice_config max_sessions <number>\n```",
                inline=False
            )
            await ctx.send(embed=embed)
            return

        try:
            s = setting.lower().strip()
            if s == "grace_duration":
                duration = int(value)
                if 60 <= duration <= 600:
                    await self.config_manager.set(ctx.guild.id, 'grace_period_duration', duration)
                    await ctx.send(f"✅ Grace period duration set to {duration} seconds (zentral gespeichert)")
                else:
                    await ctx.send("❌ Grace duration must be between 60 and 600 seconds")
            elif s == "grace_role":
                role_id = int(value)
                await self.config_manager.set(ctx.guild.id, 'special_role_id', role_id)
                await ctx.send(f"✅ Grace period role set to <@&{role_id}> (zentral gespeichert)")
            elif s == "min_users":
                min_users = int(value)
                if 2 <= min_users <= 10:
                    await self.config_manager.set(ctx.guild.id, 'min_users_for_tracking', min_users)
                    await ctx.send(f"✅ Minimum users set to {min_users} (zentral gespeichert)")
                else:
                    await ctx.send("❌ Minimum users must be between 2 and 10")
            elif s == "session_timeout":
                to = int(value)
                if 60 <= to <= 3600:
                    await self.config_manager.set(ctx.guild.id, 'session_timeout', to)
                    await ctx.send(f"✅ Session timeout set to {to}s (zentral gespeichert)")
                else:
                    await ctx.send("❌ Session timeout must be between 60 and 3600 seconds")
            elif s == "max_sessions":
                mx = int(value)
                if 10 <= mx <= 10000:
                    await self.config_manager.set(ctx.guild.id, 'max_sessions_per_user', mx)
                    await ctx.send(f"✅ Max sessions set to {mx} (zentral gespeichert)")
                else:
                    await ctx.send("❌ Max sessions must be between 10 and 10000")
            else:
                await ctx.send(f"❌ Unknown setting: {setting}")
        except ValueError:
            await ctx.send("❌ Invalid value provided")
        except Exception as e:
            await ctx.send(f"❌ Error updating config: {e}")

async def setup(bot):
    await bot.add_cog(VoiceActivityTrackerCog(bot))
