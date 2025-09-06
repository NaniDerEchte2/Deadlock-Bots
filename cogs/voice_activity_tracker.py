# ===================================================================
# ENHANCED VERSION (DB-CENTRALIZED): voice_activity_tracker.py
# Pfad: C:\Users\Nani-Admin\Documents\Deadlock\cogs\voice_activity_tracker.py
# Änderung: Keine lokalen JSONs/Einzel-DBs mehr. Zentrale DB via utils.deadlock_db.DB_PATH
# ===================================================================

import discord
from discord.ext import commands, tasks
import aiosqlite
import logging
import json
import os
import shutil
import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, Set, Optional, List
from functools import lru_cache
from collections import defaultdict, deque
from dataclasses import dataclass, asdict

from utils.deadlock_db import DB_PATH

# Setup enhanced logging
logger = logging.getLogger(__name__)

# ========= Defaults (werden pro Guild aus DB überschrieben) =========

@dataclass
class VoiceTrackerConfig:
    """Configuration class for Voice Tracker (Defaults; Persistenz: zentrale DB)"""
    min_users_for_tracking: int = 2
    grace_period_duration: int = 180  # 3 minutes
    session_timeout: int = 300        # 5 minutes
    afk_timeout: int = 1800           # 30 minutes (reserviert)
    special_role_id: int = 1313624729466441769
    backup_interval_hours: int = 24
    max_sessions_per_user: int = 100
    point_multipliers: Dict[int, float] = None

    def __post_init__(self):
        if self.point_multipliers is None:
            self.point_multipliers = {
                2: 1.0, 3: 1.1, 4: 1.2, 5: 1.3, 6: 1.4,
                7: 1.5, 8: 1.6, 9: 1.7, 10: 1.8
            }

# ========= Rate Limiter =========

class RateLimiter:
    """Rate limiting class for command usage"""
    def __init__(self, max_requests: int = 10, time_window: int = 60):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = defaultdict(deque)

    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        user_requests = self.requests[user_id]

        # Remove old requests
        while user_requests and user_requests[0] < now - self.time_window:
            user_requests.popleft()

        # Check if under limit
        if len(user_requests) < self.max_requests:
            user_requests.append(now)
            return True

        return False

    def get_remaining_time(self, user_id: int) -> int:
        """Get remaining cooldown time in seconds"""
        now = time.time()
        user_requests = self.requests[user_id]

        if not user_requests:
            return 0

        oldest_request = user_requests[0]
        return max(0, int(self.time_window - (now - oldest_request)))

# ========= DB Manager =========

class DatabaseManager:
    """Enhanced database management with retry logic and backups (zentrale DB)"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None
        self.last_backup: Optional[datetime] = None

    async def connect(self):
        """Connect to database with retry logic and WAL mode for better concurrency"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.db = await aiosqlite.connect(self.db_path)
                self.db.row_factory = aiosqlite.Row

                # Enable WAL mode for better concurrency
                await self.db.execute('PRAGMA journal_mode=WAL')
                await self.db.execute('PRAGMA synchronous=NORMAL')
                await self.db.execute('PRAGMA cache_size=10000')
                await self.db.execute('PRAGMA temp_store=MEMORY')

                await self.create_tables()
                logger.info("Database connected successfully with WAL mode")
                return True
            except Exception as e:
                logger.error(f"Database connection failed (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
        return False

    async def safe_execute(self, query: str, params: tuple = None, fetch_one: bool = False, fetch_all: bool = False):
        """Safe database execution with retry logic"""
        if not self.db:
            logger.error("Database not connected")
            return None

        max_retries = 3
        for attempt in range(max_retries):
            try:
                cursor = await self.db.execute(query, params or ())

                if fetch_one:
                    result = await cursor.fetchone()
                elif fetch_all:
                    result = await cursor.fetchall()
                else:
                    result = cursor

                await self.db.commit()
                return result

            except Exception as e:
                logger.error(f"Database error (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(1)
        return None

    async def batch_execute(self, query: str, params_list: list):
        """Execute multiple operations in a batch for better performance"""
        if not self.db or not params_list:
            return None

        max_retries = 3
        for attempt in range(max_retries):
            try:
                cursor = await self.db.executemany(query, params_list)
                await self.db.commit()
                return cursor
            except Exception as e:
                logger.error(f"Batch database error (attempt {attempt + 1}): {e}")
                if attempt == max_retries - 1:
                    raise e
                await asyncio.sleep(1)
        return None

    async def create_tables(self):
        """Create database tables with enhanced structure"""
        # Users table with additional fields
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS voice_users (
                id INTEGER PRIMARY KEY,
                discord_id BIGINT UNIQUE NOT NULL,
                username TEXT NOT NULL,
                total_voice_time INTEGER DEFAULT 0,
                total_points REAL DEFAULT 0.0,
                grace_period_used INTEGER DEFAULT 0,
                session_count INTEGER DEFAULT 0,
                longest_session INTEGER DEFAULT 0,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Enhanced voice sessions table
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS voice_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id BIGINT NOT NULL,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                channel_name TEXT DEFAULT '',
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP NOT NULL,
                duration_seconds INTEGER NOT NULL,
                points_earned REAL NOT NULL,
                grace_period_used INTEGER DEFAULT 0,
                peak_users INTEGER DEFAULT 0,
                avg_users REAL DEFAULT 0.0,
                co_participants TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Grace period events table
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS grace_period_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id BIGINT NOT NULL,
                guild_id BIGINT NOT NULL,
                channel_id BIGINT,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                duration_used INTEGER DEFAULT 0,
                points_earned_during REAL DEFAULT 0.0,
                reason TEXT DEFAULT 'self_mute',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # System events table for logging
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                guild_id BIGINT,
                user_id BIGINT,
                data TEXT DEFAULT '{}',
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Server statistics table
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS server_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id BIGINT NOT NULL,
                date DATE NOT NULL,
                total_sessions INTEGER DEFAULT 0,
                total_voice_time INTEGER DEFAULT 0,
                total_points REAL DEFAULT 0.0,
                unique_users INTEGER DEFAULT 0,
                peak_concurrent INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, date)
            )
        ''')

        # NEW: Voice config table (per guild) – zentrale Persistenz statt JSON-Datei
        await self.db.execute('''
            CREATE TABLE IF NOT EXISTS voice_config (
                guild_id BIGINT PRIMARY KEY,
                min_users_for_tracking INTEGER DEFAULT 2,
                grace_period_duration INTEGER DEFAULT 180,
                session_timeout INTEGER DEFAULT 300,
                afk_timeout INTEGER DEFAULT 1800,
                special_role_id BIGINT DEFAULT 1313624729466441769,
                backup_interval_hours INTEGER DEFAULT 24,
                max_sessions_per_user INTEGER DEFAULT 100,
                point_multipliers TEXT DEFAULT '{"2":1.0,"3":1.1,"4":1.2,"5":1.3,"6":1.4,"7":1.5,"8":1.6,"9":1.7,"10":1.8}',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Perform migrations for existing tables
        await self.migrate_tables()

        # Create indexes for better performance
        await self.create_indexes()

        await self.db.commit()
        logger.info("Database tables created/verified")

    async def migrate_tables(self):
        """Migrate existing tables to new structure"""
        try:
            # Add missing columns to voice_users
            user_columns_to_add = [
                ('session_count', 'INTEGER DEFAULT 0'),
                ('longest_session', 'INTEGER DEFAULT 0'),
                ('first_seen', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
                ('created_at', 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
            ]

            for column_name, column_def in user_columns_to_add:
                try:
                    await self.db.execute(f'ALTER TABLE voice_users ADD COLUMN {column_name} {column_def}')
                    logger.info(f"Added column {column_name} to voice_users")
                except Exception:
                    pass  # Column already exists

            # Add missing columns to voice_sessions
            session_columns_to_add = [
                ('channel_name', 'TEXT DEFAULT ""'),
                ('peak_users', 'INTEGER DEFAULT 0'),
                ('avg_users', 'REAL DEFAULT 0.0'),
                ('grace_period_used', 'INTEGER DEFAULT 0')
            ]

            for column_name, column_def in session_columns_to_add:
                try:
                    await self.db.execute(f'ALTER TABLE voice_sessions ADD COLUMN {column_name} {column_def}')
                    logger.info(f"Added column {column_name} to voice_sessions")
                except Exception:
                    pass  # Column already exists

        except Exception as e:
            logger.warning(f"Migration warning: {e}")

    async def create_indexes(self):
        """Create database indexes for better performance"""
        indexes = [
            'CREATE INDEX IF NOT EXISTS idx_voice_sessions_user_id ON voice_sessions (user_id)',
            'CREATE INDEX IF NOT EXISTS idx_voice_sessions_guild_id ON voice_sessions (guild_id)',
            'CREATE INDEX IF NOT EXISTS idx_voice_sessions_start_time ON voice_sessions (start_time)',
            'CREATE INDEX IF NOT EXISTS idx_grace_period_user_id ON grace_period_events (user_id)',
            'CREATE INDEX IF NOT EXISTS idx_grace_period_guild_id ON grace_period_events (guild_id)',
            'CREATE INDEX IF NOT EXISTS idx_voice_users_discord_id ON voice_users (discord_id)',
            'CREATE INDEX IF NOT EXISTS idx_server_stats_guild_date ON server_stats (guild_id, date)',
            'CREATE INDEX IF NOT EXISTS idx_system_events_type ON system_events (event_type, timestamp)'
        ]

        for index_query in indexes:
            try:
                await self.db.execute(index_query)
            except Exception as e:
                logger.warning(f"Index creation warning: {e}")

    async def backup_database(self):
        """Create database backup (konsistent via VACUUM INTO)"""
        try:
            backup_dir = os.path.dirname(self.db_path)
            backup_filename = f"shared_db_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            backup_path = os.path.join(backup_dir, backup_filename)

            # VACUUM INTO erzeugt eine konsistente Kopie (auch bei WAL)
            await self.db.execute(f"VACUUM INTO ?", (backup_path,))
            await self.db.commit()

            self.last_backup = datetime.now()

            # Keep only last 7 backups
            await self.cleanup_old_backups(backup_dir, 7)

            logger.info(f"Database backup created: {backup_path}")
            return backup_path
        except Exception as e:
            logger.error(f"Backup creation failed: {e}")
            return None

    async def cleanup_old_backups(self, backup_dir: str, keep_count: int):
        """Remove old backup files"""
        try:
            backup_files = [f for f in os.listdir(backup_dir) if f.startswith('shared_db_backup_') and f.endswith('.db')]
            backup_files.sort(reverse=True)  # Newest first

            for old_backup in backup_files[keep_count:]:
                old_backup_path = os.path.join(backup_dir, old_backup)
                os.remove(old_backup_path)
                logger.info(f"Removed old backup: {old_backup}")
        except Exception as e:
            logger.warning(f"Backup cleanup warning: {e}")

    async def close(self):
        """Close database connection"""
        if self.db:
            try:
                await self.db.execute("VACUUM")
            except Exception:
                pass
            await self.db.close()
            logger.info("Database connection closed")

# ========= Config Manager (zentrale DB statt JSON-Datei) =========

class ConfigManager:
    """Per-Guild Konfigurationsverwaltung in der zentralen DB"""
    def __init__(self, dbm: DatabaseManager, defaults: VoiceTrackerConfig):
        self.dbm = dbm
        self.defaults = defaults
        self._cache: Dict[int, VoiceTrackerConfig] = {}

    async def get(self, guild_id: int) -> VoiceTrackerConfig:
        if guild_id in self._cache:
            return self._cache[guild_id]

        row = await self.dbm.safe_execute('''
            SELECT * FROM voice_config WHERE guild_id = ?
        ''', (guild_id,), fetch_one=True)

        cfg = VoiceTrackerConfig()  # start with defaults
        if row:
            try:
                pm_raw = row['point_multipliers']
                pm = json.loads(pm_raw) if isinstance(pm_raw, str) else (pm_raw or {})
                pm = {int(k): float(v) for k, v in pm.items()} if pm else None
            except Exception:
                pm = None

            cfg = VoiceTrackerConfig(
                min_users_for_tracking=row['min_users_for_tracking'],
                grace_period_duration=row['grace_period_duration'],
                session_timeout=row['session_timeout'],
                afk_timeout=row['afk_timeout'],
                special_role_id=row['special_role_id'],
                backup_interval_hours=row['backup_interval_hours'],
                max_sessions_per_user=row['max_sessions_per_user'],
                point_multipliers=pm or self.defaults.point_multipliers.copy()
            )
        else:
            # Insert defaults for this guild
            await self.set_all(guild_id, cfg)

        self._cache[guild_id] = cfg
        return cfg

    async def set(self, guild_id: int, field: str, value):
        """Set a single config field and update cache"""
        if field == 'point_multipliers' and isinstance(value, dict):
            value = json.dumps({str(k): float(v) for k, v in value.items()})

        await self.dbm.safe_execute(f'''
            INSERT INTO voice_config (guild_id, {field}, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET {field}=excluded.{field}, updated_at=CURRENT_TIMESTAMP
        ''', (guild_id, value))

        # refresh cache
        if guild_id in self._cache:
            cfg = self._cache[guild_id]
            if field == 'point_multipliers' and isinstance(value, str):
                cfg.point_multipliers = {int(k): float(v) for k, v in json.loads(value).items()}
            else:
                setattr(cfg, field, value)

    async def set_all(self, guild_id: int, cfg: VoiceTrackerConfig):
        pm = json.dumps({str(k): float(v) for k, v in cfg.point_multipliers.items()})
        await self.dbm.safe_execute('''
            INSERT INTO voice_config (
                guild_id, min_users_for_tracking, grace_period_duration, session_timeout,
                afk_timeout, special_role_id, backup_interval_hours, max_sessions_per_user,
                point_multipliers, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id) DO UPDATE SET
                min_users_for_tracking=excluded.min_users_for_tracking,
                grace_period_duration=excluded.grace_period_duration,
                session_timeout=excluded.session_timeout,
                afk_timeout=excluded.afk_timeout,
                special_role_id=excluded.special_role_id,
                backup_interval_hours=excluded.backup_interval_hours,
                max_sessions_per_user=excluded.max_sessions_per_user,
                point_multipliers=excluded.point_multipliers,
                updated_at=CURRENT_TIMESTAMP
        ''', (guild_id, cfg.min_users_for_tracking, cfg.grace_period_duration, cfg.session_timeout,
              cfg.afk_timeout, cfg.special_role_id, cfg.backup_interval_hours, cfg.max_sessions_per_user, pm))
        self._cache[guild_id] = cfg

# ========= Voice Cog =========

class VoiceActivityTrackerCog(commands.Cog):
    """Enhanced Voice Activity Tracking System (DB-zentral, keine JSONs)"""

    def __init__(self, bot):
        self.bot = bot

        # Zentrale DB
        self.db_path = str(DB_PATH)
        self.db_manager = DatabaseManager(self.db_path)

        # Defaults + Config-Manager
        self.defaults = VoiceTrackerConfig()
        self.config_manager = ConfigManager(self.db_manager, self.defaults)

        # Voice session tracking (in-memory)
        self.voice_sessions: Dict[str, Dict] = {}
        self.grace_period_users: Dict[str, Dict] = {}
        self.tracked_guilds: Set[int] = set()

        # Rate limiting
        self.rate_limiter = RateLimiter(max_requests=5, time_window=30)

        # Statistics
        self.session_stats = {
            'total_sessions_created': 0,
            'total_grace_periods': 0,
            'uptime_start': datetime.utcnow()
        }

        # Batch update system placeholder (bewusst passiv; Sessions werden am Ende gespeichert)
        self.pending_session_updates = []
        self.batch_update_size = 10

        logger.info("Enhanced Voice Activity Tracker initializing (DB-centralized)")

    async def cog_load(self):
        """Called when cog is loaded"""
        try:
            await self.db_manager.connect()
            await self.log_system_event("system_start", data={"version": "enhanced_db_central"})

            # Start background tasks
            self.cleanup_sessions.start()
            self.update_sessions.start()
            self.grace_period_monitor.start()
            self.health_check.start()
            self.daily_backup.start()

            logger.info("Voice Activity Tracker initialized (DB-centralized)")

        except Exception as e:
            logger.error(f"Failed to load cog: {e}")

    def cog_unload(self):
        """Called when cog is unloaded"""
        # Cancel all tasks
        tasks_to_cancel = [
            self.cleanup_sessions, self.update_sessions,
            self.grace_period_monitor, self.health_check, self.daily_backup
        ]

        for task in tasks_to_cancel:
            if task.is_running():
                task.cancel()

        # Close database
        if self.db_manager:
            asyncio.create_task(self.db_manager.close())

        logger.info("Voice Activity Tracker unloaded")

    # ===== Helpers =====

    async def cfg(self, guild_id: int) -> VoiceTrackerConfig:
        return await self.config_manager.get(guild_id)

    @lru_cache(maxsize=1024)
    def _multiplier_from_map(self, key: int, serialized: str) -> float:
        """Cache-Layer: wandelt JSON-Map in Dict und liefert Faktor; nur intern genutzt"""
        try:
            m = json.loads(serialized)
            return float(m.get(str(key), 2.0))
        except Exception:
            return 2.0

    async def calculate_point_multiplier_cached(self, guild_id: int, user_count: int) -> float:
        cfg = await self.cfg(guild_id)
        if user_count < cfg.min_users_for_tracking:
            return 0.0
        # serialize map for cache key stability
        ser = json.dumps({str(k): v for k, v in cfg.point_multipliers.items()}, sort_keys=True)
        return self._multiplier_from_map(user_count, ser)

    async def has_grace_period_role(self, member: discord.Member) -> bool:
        cfg = await self.cfg(member.guild.id)
        return any(role.id == cfg.special_role_id for role in member.roles)

    def is_user_active_basic(self, voice_state: discord.VoiceState) -> bool:
        if not voice_state or not voice_state.channel:
            return False
        if getattr(voice_state, "afk", False):
            return False
        is_muted_or_deaf = (voice_state.mute or voice_state.deaf or
                            voice_state.self_mute or voice_state.self_deaf)
        return not is_muted_or_deaf

    async def is_user_active(self, member: discord.Member) -> bool:
        """Check if user is actively participating in voice (inkl. Grace-Role-Ausnahme)"""
        vs = member.voice
        if not vs or not vs.channel:
            return False

        if self.is_user_active_basic(vs):
            return True

        # Grace Ausnahmen
        if await self.has_grace_period_role(member):
            grace_key = f"{member.id}:{member.guild.id}"
            if grace_key in self.grace_period_users:
                grace_data = self.grace_period_users[grace_key]
                time_in_grace = (datetime.utcnow() - grace_data['start_time']).total_seconds()
                cfg = await self.cfg(member.guild.id)
                if time_in_grace <= cfg.grace_period_duration:
                    return True

        return False

    async def log_system_event(self, event_type: str, guild_id: int = None, user_id: int = None, data: dict = None):
        """Log system events to database"""
        try:
            await self.db_manager.safe_execute('''
                INSERT INTO system_events (event_type, guild_id, user_id, data)
                VALUES (?, ?, ?, ?)
            ''', (event_type, guild_id, user_id, json.dumps(data or {})))
        except Exception as e:
            logger.warning(f"Failed to log system event: {e}")

    async def start_grace_period(self, member: discord.Member):
        """Start grace period for a user with the special role"""
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
            'points_earned': 0.0
        }

        self.session_stats['total_grace_periods'] += 1

        await self.db_manager.safe_execute('''
            INSERT INTO grace_period_events (user_id, guild_id, channel_id, start_time)
            VALUES (?, ?, ?, ?)
        ''', (member.id, member.guild.id, member.voice.channel.id if member.voice else None, datetime.utcnow()))

        await self.log_system_event("grace_period_start", member.guild.id, member.id)

        logger.info(f"Grace period started for {member.display_name} ({member.id})")

    async def end_grace_period(self, member_id: int, guild_id: int, reason: str = "timeout"):
        """End grace period for a user"""
        grace_key = f"{member_id}:{guild_id}"

        if grace_key not in self.grace_period_users:
            return

        grace_data = self.grace_period_users[grace_key]
        duration_used = (datetime.utcnow() - grace_data['start_time']).total_seconds()

        await self.db_manager.safe_execute('''
            UPDATE grace_period_events 
            SET end_time = ?, duration_used = ?, points_earned_during = ?, reason = ?
            WHERE user_id = ? AND guild_id = ? AND end_time IS NULL
        ''', (datetime.utcnow(), int(duration_used), grace_data['points_earned'], reason, member_id, guild_id))

        await self.log_system_event("grace_period_end", guild_id, member_id, {
            "reason": reason,
            "duration": duration_used,
            "points": grace_data['points_earned']
        })

        del self.grace_period_users[grace_key]

    async def start_voice_session(self, member: discord.Member, channel: discord.VoiceChannel):
        """Start a new voice session for a user (nur in-memory; Persistenz beim Ende)"""
        session_key = f"{member.id}:{channel.guild.id}"

        if session_key not in self.voice_sessions:
            self.voice_sessions[session_key] = {
                'user_id': member.id,
                'guild_id': channel.guild.id,
                'channel_id': channel.id,
                'channel_name': channel.name,
                'start_time': datetime.utcnow(),
                'last_update': datetime.utcnow(),
                'total_time': 0,
                'total_points': 0.0,
                'grace_period_used': 0,
                'peak_users': 0,
                'user_counts': [],
                'co_participants': set()
            }

            self.session_stats['total_sessions_created'] += 1
            await self.log_system_event("session_start", channel.guild.id, member.id, {"channel": channel.name})

            logger.info(f"Started voice session: {member.display_name} in {channel.name}")

    async def end_voice_session(self, member: discord.Member, guild_id: int):
        """End a voice session and save to database"""
        session_key = f"{member.id}:{guild_id}"

        if session_key in self.voice_sessions:
            session = self.voice_sessions[session_key]
            session_duration = (datetime.utcnow() - session['start_time']).total_seconds()

            # Calculate average users
            avg_users = sum(session['user_counts']) / len(session['user_counts']) if session['user_counts'] else 0

            # Save to database
            await self.save_voice_session(
                user_id=session['user_id'],
                guild_id=session['guild_id'],
                channel_id=session['channel_id'],
                channel_name=session['channel_name'],
                start_time=session['start_time'],
                end_time=datetime.utcnow(),
                duration_seconds=int(session_duration),
                points_earned=session['total_points'],
                grace_period_used=session['grace_period_used'],
                peak_users=session['peak_users'],
                avg_users=avg_users,
                co_participants=list(session['co_participants'])
            )

            await self.log_system_event("session_end", guild_id, member.id, {
                "duration": session_duration,
                "points": session['total_points']
            })

            del self.voice_sessions[session_key]
            logger.info(f"Ended voice session: {member.display_name}, duration: {session_duration:.1f}s, points: {session['total_points']:.1f}")

        # End grace period if active
        await self.end_grace_period(member.id, guild_id, "voice_leave")

    async def save_voice_session(self, user_id: int, guild_id: int, channel_id: int, channel_name: str,
                                 start_time: datetime, end_time: datetime, duration_seconds: int,
                                 points_earned: float, grace_period_used: int = 0, peak_users: int = 0,
                                 avg_users: float = 0.0, co_participants: List[int] = None):
        """Save a completed voice session with enhanced data"""
        try:
            co_participants_json = json.dumps(co_participants or [])

            # Save session
            await self.db_manager.safe_execute('''
                INSERT INTO voice_sessions 
                (user_id, guild_id, channel_id, channel_name, start_time, end_time, duration_seconds, 
                 points_earned, grace_period_used, peak_users, avg_users, co_participants)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, guild_id, channel_id, channel_name, start_time, end_time, duration_seconds,
                  points_earned, grace_period_used, peak_users, avg_users, co_participants_json))

            # Update user totals with enhanced statistics
            await self.db_manager.safe_execute('''
                INSERT OR REPLACE INTO voice_users 
                (discord_id, username, total_voice_time, total_points, grace_period_used, 
                 session_count, longest_session, last_seen)
                VALUES (?, ?, 
                    COALESCE((SELECT total_voice_time FROM voice_users WHERE discord_id = ?), 0) + ?,
                    COALESCE((SELECT total_points FROM voice_users WHERE discord_id = ?), 0) + ?,
                    COALESCE((SELECT grace_period_used FROM voice_users WHERE discord_id = ?), 0) + ?,
                    COALESCE((SELECT session_count FROM voice_users WHERE discord_id = ?), 0) + 1,
                    MAX(COALESCE((SELECT longest_session FROM voice_users WHERE discord_id = ?), 0), ?),
                    CURRENT_TIMESTAMP)
            ''', (user_id, "Unknown", user_id, duration_seconds, user_id, points_earned,
                  user_id, grace_period_used, user_id, user_id, duration_seconds))

            logger.info(f"Saved voice session: User {user_id}, {duration_seconds}s, {points_earned:.1f} points")

        except Exception as e:
            logger.error(f"Error saving voice session: {e}")

    async def get_live_user_stats(self, user_id: int, guild_id: int):
        """Get comprehensive user stats including live sessions"""
        db_stats = {
            'total_time': 0, 'total_points': 0.0, 'session_count': 0,
            'grace_period_used': 0, 'longest_session': 0
        }

        try:
            result = await self.db_manager.safe_execute('''
                SELECT total_voice_time, total_points, grace_period_used, session_count, longest_session,
                       (SELECT COUNT(*) FROM voice_sessions WHERE user_id = ? AND guild_id = ?) as guild_sessions
                FROM voice_users WHERE discord_id = ?
            ''', (user_id, guild_id, user_id), fetch_one=True)

            if result:
                db_stats = {
                    'total_time': result['total_voice_time'] or 0,
                    'total_points': result['total_points'] or 0.0,
                    'session_count': result['guild_sessions'] or 0,
                    'total_session_count': result['session_count'] or 0,
                    'grace_period_used': result['grace_period_used'] or 0,
                    'longest_session': result['longest_session'] or 0
                }

        except Exception as e:
            logger.error(f"Database error in get_live_user_stats: {e}")

        # Add live session data
        session_key = f"{user_id}:{guild_id}"
        if session_key in self.voice_sessions:
            live_session = self.voice_sessions[session_key]
            live_duration = (datetime.utcnow() - live_session['start_time']).total_seconds()

            db_stats['total_time'] += int(live_duration)
            db_stats['total_points'] += live_session['total_points']
            db_stats['has_live_session'] = True
            db_stats['live_duration'] = int(live_duration)
            db_stats['live_points'] = live_session['total_points']
        else:
            db_stats['has_live_session'] = False

        # Add grace period info
        grace_key = f"{user_id}:{guild_id}"
        if grace_key in self.grace_period_users:
            cfg = await self.cfg(guild_id)
            grace_data = self.grace_period_users[grace_key]
            grace_duration = (datetime.utcnow() - grace_data['start_time']).total_seconds()
            remaining_time = max(0, cfg.grace_period_duration - grace_duration)

            db_stats['has_active_grace'] = True
            db_stats['grace_remaining'] = int(remaining_time)
            db_stats['grace_points'] = grace_data['points_earned']
        else:
            db_stats['has_active_grace'] = False

        return db_stats

    async def process_batch_updates(self):
        """Kein Live-DB-Update – Sessions werden beim Ende persistiert (Mechanik unverändert).
        Placeholder gelassen, falls später benötigt."""
        if not self.pending_session_updates:
            return
        # absichtlich leer (keine Änderung der Laufzeitlogik)

    # ===== Discord Events =====

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Handle voice state updates with enhanced logging"""
        try:
            if member.bot:
                return

            # Log significant voice state changes
            if before.channel != after.channel:
                if before.channel:
                    await self.log_system_event("voice_leave", before.channel.guild.id, member.id,
                                               {"channel": before.channel.name})
                if after.channel:
                    await self.log_system_event("voice_join", after.channel.guild.id, member.id,
                                               {"channel": after.channel.name})

            # Handle mute state changes for grace period
            if (before.channel and after.channel and before.channel == after.channel):
                was_muted = before.mute or before.self_mute or before.deaf or before.self_deaf
                is_muted = after.mute or after.self_mute or after.deaf or after.self_deaf

                if not was_muted and is_muted and await self.has_grace_period_role(member):
                    await self.start_grace_period(member)
                elif was_muted and not is_muted:
                    await self.end_grace_period(member.id, member.guild.id, "unmuted")

            # Handle channel changes
            if before.channel != after.channel:
                if before.channel:
                    await self.handle_voice_leave(member, before.channel)
                if after.channel:
                    await self.handle_voice_join(member, after.channel)
            elif before.channel and after.channel:
                await self.update_channel_sessions(after.channel)

        except Exception as e:
            logger.error(f"Error in voice state update: {e}")

    async def handle_voice_join(self, member: discord.Member, channel: discord.VoiceChannel):
        """Handle user joining a voice channel"""
        await self.update_channel_sessions(channel)

    async def handle_voice_leave(self, member: discord.Member, channel: discord.VoiceChannel):
        """Handle user leaving a voice channel"""
        await self.end_voice_session(member, channel.guild.id)
        await self.update_channel_sessions(channel)

    async def update_channel_sessions(self, channel: discord.VoiceChannel):
        """Update voice sessions for all users in a channel"""
        if not channel.members:
            return

        # aktive User (mit Grace-Ausnahme)
        active_users = []
        for m in channel.members:
            if m.bot:
                continue
            if await self.is_user_active(m):
                active_users.append(m)

        user_count = len(active_users)

        # Track user count für Sessions
        for member in active_users:
            session_key = f"{member.id}:{channel.guild.id}"
            if session_key in self.voice_sessions:
                session = self.voice_sessions[session_key]
                session['user_counts'].append(user_count)
                session['peak_users'] = max(session['peak_users'], user_count)

        cfg = await self.cfg(channel.guild.id)

        # Start or continue sessions for active users
        if user_count >= cfg.min_users_for_tracking and active_users:
            for member in active_users:
                session_key = f"{member.id}:{channel.guild.id}"

                if session_key not in self.voice_sessions:
                    await self.start_voice_session(member, channel)

                if session_key in self.voice_sessions:
                    session = self.voice_sessions[session_key]
                    session['last_update'] = datetime.utcnow()
                    session['co_participants'].update(
                        user.id for user in active_users if user.id != member.id
                    )

        # End sessions for users who are no longer active
        for member in channel.members:
            if not member.bot:
                session_key = f"{member.id}:{channel.guild.id}"
                if (session_key in self.voice_sessions and member not in active_users):
                    await self.end_voice_session(member, channel.guild.id)

    # ===== BACKGROUND TASKS =====

    @tasks.loop(minutes=2)
    async def update_sessions(self):
        """Update active voice sessions with enhanced tracking"""
        if not self.voice_sessions:
            return

        current_time = datetime.utcnow()

        for session_key, session in list(self.voice_sessions.items()):
            try:
                time_elapsed = (current_time - session['last_update']).total_seconds()

                guild = self.bot.get_guild(session['guild_id'])
                if not guild:
                    continue

                channel = guild.get_channel(session['channel_id'])
                if not channel:
                    continue

                # aktive User erneut prüfen
                active_users = []
                for m in channel.members:
                    if m.bot:
                        continue
                    if await self.is_user_active(m):
                        active_users.append(m)

                user_count = len(active_users)
                cfg = await self.cfg(session['guild_id'])

                if user_count >= cfg.min_users_for_tracking:
                    point_multiplier = await self.calculate_point_multiplier_cached(session['guild_id'], user_count)
                    points_earned = time_elapsed * point_multiplier / 60

                    session['total_time'] += int(time_elapsed)
                    session['total_points'] += points_earned
                    session['last_update'] = current_time

                    # Track grace period usage
                    grace_key = f"{session['user_id']}:{session['guild_id']}"
                    if grace_key in self.grace_period_users:
                        session['grace_period_used'] += int(time_elapsed)
                        self.grace_period_users[grace_key]['points_earned'] += points_earned

            except Exception as e:
                logger.error(f"Error updating session {session_key}: {e}")

        # Keine Live-DB-Updates – Mechanik bleibt (Persistenz am Ende)
        await self.process_batch_updates()

    @update_sessions.before_loop
    async def before_update_sessions(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=60)
    async def grace_period_monitor(self):
        """Monitor and cleanup expired grace periods"""
        if not self.grace_period_users:
            return

        current_time = datetime.utcnow()
        expired_users = []

        for grace_key, grace_data in self.grace_period_users.items():
            time_in_grace = (current_time - grace_data['start_time']).total_seconds()
            cfg = await self.cfg(grace_data['guild_id'])
            if time_in_grace >= cfg.grace_period_duration:
                expired_users.append((grace_data['user_id'], grace_data['guild_id']))

        for user_id, guild_id in expired_users:
            await self.end_grace_period(user_id, guild_id, "timeout_3min")

    @grace_period_monitor.before_loop
    async def before_grace_period_monitor(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=5)
    async def cleanup_sessions(self):
        """Clean up inactive voice sessions"""
        if not self.voice_sessions:
            return

        # session_timeout pro Guild
        now = datetime.utcnow()
        sessions_to_remove = []

        for session_key, session in self.voice_sessions.items():
            cfg = await self.cfg(session['guild_id'])
            cutoff_time = now - timedelta(seconds=cfg.session_timeout)
            if session['last_update'] < cutoff_time:
                sessions_to_remove.append(session_key)

        for session_key in sessions_to_remove:
            session = self.voice_sessions[session_key]

            # Calculate final stats
            avg_users = sum(session['user_counts']) / len(session['user_counts']) if session['user_counts'] else 0

            await self.save_voice_session(
                user_id=session['user_id'],
                guild_id=session['guild_id'],
                channel_id=session['channel_id'],
                channel_name=session['channel_name'],
                start_time=session['start_time'],
                end_time=session['last_update'],
                duration_seconds=session['total_time'],
                points_earned=session['total_points'],
                grace_period_used=session['grace_period_used'],
                peak_users=session['peak_users'],
                avg_users=avg_users,
                co_participants=list(session['co_participants'])
            )

            user = self.bot.get_user(session['user_id'])
            logger.info(f"Cleaned up inactive session: {user.display_name if user else 'Unknown'}")

            del self.voice_sessions[session_key]

    @cleanup_sessions.before_loop
    async def before_cleanup_sessions(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=2)
    async def health_check(self):
        """System health monitoring"""
        try:
            # Database health check
            if self.db_manager.db:
                await self.db_manager.safe_execute('SELECT 1')

            # Memory usage check
            active_sessions = len(self.voice_sessions)
            grace_periods = len(self.grace_period_users)

            # Warnung, wenn viele Sessions (globaler Wert)
            # (Traditionell: konservativ bleiben.)
            if active_sessions > self.defaults.max_sessions_per_user:
                logger.warning(f"High session count detected: {active_sessions}")
                await self.log_system_event("high_session_count", data={"count": active_sessions})

            # Clean up stuck sessions (>12 hours)
            stuck_cutoff = datetime.utcnow() - timedelta(hours=12)
            stuck_sessions = [
                key for key, session in self.voice_sessions.items()
                if session['start_time'] < stuck_cutoff
            ]

            for session_key in stuck_sessions:
                logger.warning(f"Cleaning up stuck session: {session_key}")
                session = self.voice_sessions[session_key]
                await self.save_voice_session(
                    user_id=session['user_id'],
                    guild_id=session['guild_id'],
                    channel_id=session['channel_id'],
                    channel_name=session['channel_name'],
                    start_time=session['start_time'],
                    end_time=datetime.utcnow(),
                    duration_seconds=session['total_time'],
                    points_earned=session['total_points'],
                    grace_period_used=session['grace_period_used'],
                    peak_users=session['peak_users'],
                    avg_users=0.0,
                    co_participants=list(session['co_participants'])
                )
                del self.voice_sessions[session_key]

            uptime = datetime.utcnow() - self.session_stats['uptime_start']
            await self.log_system_event("health_check", data={
                "active_sessions": active_sessions,
                "grace_periods": grace_periods,
                "uptime_hours": uptime.total_seconds() / 3600,
                "total_sessions_created": self.session_stats['total_sessions_created'],
                "total_grace_periods": self.session_stats['total_grace_periods']
            })

            logger.info(f"Health check: {active_sessions} sessions, {grace_periods} grace periods, uptime: {uptime}")

        except Exception as e:
            logger.error(f"Health check failed: {e}")

    @health_check.before_loop
    async def before_health_check(self):
        await self.bot.wait_until_ready()

    @tasks.loop(hours=24)
    async def daily_backup(self):
        """Create daily database backups"""
        try:
            backup_path = await self.db_manager.backup_database()
            if backup_path:
                await self.log_system_event("database_backup", data={"backup_path": backup_path})
                logger.info("Daily backup completed successfully")
            else:
                logger.error("Daily backup failed")
        except Exception as e:
            logger.error(f"Daily backup error: {e}")

    @daily_backup.before_loop
    async def before_daily_backup(self):
        await self.bot.wait_until_ready()

    # ===== COMMANDS =====

    @commands.command(name="vstats")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def voice_stats_command(self, ctx, user: Optional[discord.Member] = None):
        """Enhanced voice statistics with comprehensive data"""
        if not self.rate_limiter.is_allowed(ctx.author.id):
            remaining = self.rate_limiter.get_remaining_time(ctx.author.id)
            await ctx.send(f"⏰ Rate limit reached. Try again in {remaining} seconds.")
            return

        target_user = user or ctx.author

        try:
            stats = await self.get_live_user_stats(target_user.id, ctx.guild.id)

            if stats['total_time'] == 0 and not stats['has_live_session']:
                embed = discord.Embed(
                    title="Keine Voice-Aktivität",
                    description=f"{target_user.mention} hat noch keine Voice-Aktivität aufgezeichnet.\n\n**So funktioniert es:**\n• Gehe mit 2+ Personen in Voice\n• Bleibt beide nicht muted\n• Zeit wird automatisch getrackt!\n• Mit spezieller Rolle: 3 Min Grace Period",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return

            total_hours = stats['total_time'] // 3600
            total_minutes = (stats['total_time'] % 3600) // 60
            longest_hours = stats.get('longest_session', 0) // 3600
            longest_minutes = (stats.get('longest_session', 0) % 3600) // 60

            embed = discord.Embed(
                title=f"📊 Voice-Statistiken - {target_user.display_name}",
                color=discord.Color.blue()
            )

            # Basic stats
            embed.add_field(name="⏱️ Gesamtzeit", value=f"{total_hours}h {total_minutes}m", inline=True)
            embed.add_field(name="⭐ Punkte", value=f"{stats['total_points']:.1f}", inline=True)
            embed.add_field(name="🎯 Sessions", value=f"{stats['session_count']}", inline=True)

            # Additional stats
            if stats.get('longest_session', 0) > 0:
                embed.add_field(name="🏆 Längste Session", value=f"{longest_hours}h {longest_minutes}m", inline=True)

            if stats.get('grace_period_used', 0) > 0:
                grace_minutes = stats['grace_period_used'] // 60
                embed.add_field(name="🛡️ Grace Period", value=f"{grace_minutes} Min genutzt", inline=True)

            # Live session info
            if stats['has_live_session']:
                live_hours = stats['live_duration'] // 3600
                live_minutes = (stats['live_duration'] % 3600) // 60
                session_info = f"🔴 {live_hours}h {live_minutes}m (+{stats['live_points']:.1f} pts)"

                if stats['has_active_grace']:
                    remaining_sec = stats['grace_remaining']
                    remaining_min = remaining_sec // 60
                    remaining_sec = remaining_sec % 60
                    session_info += f"\n🛡️ Grace: {remaining_min}:{remaining_sec:02d} verbleibend"

                embed.add_field(name="Live Session", value=session_info, inline=False)

            # Role and permissions
            if await self.has_grace_period_role(target_user):
                embed.add_field(name="🎖️ Spezielle Rolle", value="Grace Period berechtigt (3 Min Schutz)", inline=False)

            embed.set_thumbnail(url=target_user.display_avatar.url)
            embed.set_footer(text=f"Angefragt von {ctx.author.display_name} • !vleaderboard für Rangliste")

            await ctx.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in voice_stats: {e}")
            await ctx.send(f"❌ Fehler beim Abrufen der Statistiken: {e}")

    @commands.command(name="vleaderboard", aliases=["vlb", "voicetop"])
    @commands.cooldown(1, 15, commands.BucketType.guild)
    async def voice_leaderboard_command(self, ctx, limit: Optional[int] = 10):
        """Enhanced voice leaderboard with advanced statistics"""
        if not self.rate_limiter.is_allowed(ctx.author.id):
            remaining = self.rate_limiter.get_remaining_time(ctx.author.id)
            await ctx.send(f"⏰ Rate limit reached. Try again in {remaining} seconds.")
            return

        if limit < 1 or limit > 25:
            limit = 10

        try:
            all_users = {}

            # Get database users with enhanced stats
            result = await self.db_manager.safe_execute('''
                SELECT u.discord_id, u.username, 
                       SUM(vs.duration_seconds) as total_time,
                       SUM(vs.points_earned) as total_points,
                       COUNT(vs.id) as session_count,
                       SUM(COALESCE(vs.grace_period_used, 0)) as grace_used,
                       MAX(vs.duration_seconds) as longest_session,
                       AVG(vs.avg_users) as avg_co_users
                FROM voice_users u
                JOIN voice_sessions vs ON u.discord_id = vs.user_id
                WHERE vs.guild_id = ?
                GROUP BY u.discord_id
                ORDER BY total_points DESC
            ''', (ctx.guild.id,), fetch_all=True)

            if result:
                for row in result:
                    all_users[row['discord_id']] = {
                        'username': row['username'],
                        'total_time': row['total_time'] or 0,
                        'total_points': row['total_points'] or 0.0,
                        'session_count': row['session_count'] or 0,
                        'grace_used': row['grace_used'] or 0,
                        'longest_session': row['longest_session'] or 0,
                        'avg_co_users': row['avg_co_users'] or 0.0
                    }

            # Add live session data
            for session_key, session in self.voice_sessions.items():
                if session['guild_id'] == ctx.guild.id:
                    user_id = session['user_id']
                    user = self.bot.get_user(user_id)

                    if user_id not in all_users:
                        all_users[user_id] = {
                            'username': user.display_name if user else 'Unknown',
                            'total_time': 0,
                            'total_points': 0.0,
                            'session_count': 0,
                            'grace_used': 0,
                            'longest_session': 0,
                            'avg_co_users': 0.0
                        }

                    live_duration = (datetime.utcnow() - session['start_time']).total_seconds()
                    all_users[user_id]['total_time'] += int(live_duration)
                    all_users[user_id]['total_points'] += session['total_points']
                    all_users[user_id]['has_live'] = True

                    grace_key = f"{user_id}:{ctx.guild.id}"
                    if grace_key in self.grace_period_users:
                        all_users[user_id]['has_grace'] = True

            if not all_users:
                embed = discord.Embed(
                    title="📊 Voice-Leaderboard",
                    description="Noch keine Voice-Aktivität auf diesem Server aufgezeichnet.\n\n**So funktioniert es:**\n• Gehe mit 2+ Personen in Voice\n• Bleibt beide nicht muted\n• Zeit wird automatisch getrackt!\n• Spezielle Rolle: 3 Min Grace Period bei Mute",
                    color=discord.Color.red()
                )
                await ctx.send(embed=embed)
                return

            sorted_users = sorted(all_users.items(), key=lambda x: x[1]['total_points'], reverse=True)[:limit]

            embed = discord.Embed(
                title=f"🏆 Voice-Aktivitäts-Leaderboard - {ctx.guild.name}",
                color=discord.Color.gold()
            )

            description = ""
            user_rank = None

            for i, (user_id, data) in enumerate(sorted_users, 1):
                user = self.bot.get_user(user_id)
                username = user.display_name if user else data['username']

                if user_id == ctx.author.id:
                    user_rank = i

                hours = data['total_time'] // 3600
                minutes = (data['total_time'] % 3600) // 60

                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."

                indicators = ""
                if data.get('has_live', False):
                    indicators += " 🔴"
                if data.get('has_grace', False):
                    indicators += " 🛡️"

                description += f"{medal} **{username}**{indicators}\n"
                description += f"   ⏱️ {hours}h {minutes}m | ⭐ {data['total_points']:.1f} pts"

                if data['session_count'] > 0:
                    avg_session = data['total_time'] // data['session_count'] // 60
                    description += f" | 📊 {avg_session}m avg"

                description += "\n\n"

            embed.description = description

            # Statistics
            if user_rank:
                embed.add_field(name="🎯 Dein Rang", value=f"#{user_rank}", inline=True)

            embed.add_field(name="🔴 Live Sessions", value=f"{len(self.voice_sessions)} aktiv", inline=True)
            embed.add_field(name="🛡️ Grace Periods", value=f"{len(self.grace_period_users)} aktiv", inline=True)

            # Total server stats
            total_time = sum(user['total_time'] for user in all_users.values()) // 3600
            total_points = sum(user['total_points'] for user in all_users.values())
            embed.add_field(name="📈 Server Total", value=f"{total_time}h | {total_points:.0f} pts", inline=False)

            embed.set_footer(text=f"Top {len(sorted_users)} User • 🔴 = Live • 🛡️ = Grace Period • !vstats für Details")

            await ctx.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in voice_leaderboard: {e}")
            await ctx.send(f"❌ Fehler beim Abrufen des Leaderboards: {e}")

    @commands.command(name="vtest")
    async def voice_test_command(self, ctx):
        """System test and diagnostics"""
        embed = discord.Embed(title="🔧 Enhanced Voice System Test", color=0x00ff99)

        # System Status
        embed.add_field(name="💾 System Status", value="✅ Geladen und aktiv", inline=True)
        embed.add_field(name="🗄️ Database", value="✅ Verbunden" if self.db_manager.db else "❌ Fehler", inline=True)
        embed.add_field(name="🔴 Live Sessions", value=len(self.voice_sessions), inline=True)

        # Grace Period Status
        cfg = await self.cfg(ctx.guild.id)
        embed.add_field(name="🛡️ Grace Periods", value=len(self.grace_period_users), inline=True)
        embed.add_field(name="⏱️ Grace Duration", value=f"{cfg.grace_period_duration}s", inline=True)
        embed.add_field(name="🎖️ Special Role", value=f"<@&{cfg.special_role_id}>", inline=True)

        # User Status
        has_role = await self.has_grace_period_role(ctx.author)
        role_status = "✅ Berechtigt" if has_role else "❌ Nicht berechtigt"
        embed.add_field(name="👤 Deine Berechtigung", value=role_status, inline=True)

        # Current Session
        session_key = f"{ctx.author.id}:{ctx.guild.id}"
        grace_key = f"{ctx.author.id}:{ctx.guild.id}"

        if session_key in self.voice_sessions:
            session = self.voice_sessions[session_key]
            duration = (datetime.utcnow() - session['start_time']).total_seconds() / 60
            session_info = f"🔴 **Aktive Session**\n⏱️ {duration:.1f} Minuten\n⭐ {session['total_points']:.1f} Punkte"

            if grace_key in self.grace_period_users:
                grace_data = self.grace_period_users[grace_key]
                grace_duration = (datetime.utcnow() - grace_data['start_time']).total_seconds()
                remaining = max(0, cfg.grace_period_duration - grace_duration)
                session_info += f"\n🛡️ Grace: {remaining:.0f}s verbleibend"
        else:
            session_info = "⭕ Keine aktive Session"

        embed.add_field(name="📊 Deine Session", value=session_info, inline=False)

        # Voice State
        if ctx.author.voice:
            active_users = []
            for m in ctx.author.voice.channel.members:
                if not m.bot and await self.is_user_active(m):
                    active_users.append(m)

            voice_info = f"🎵 **{ctx.author.voice.channel.name}**\n"
            voice_info += f"👥 {len(ctx.author.voice.channel.members)} User ({len(active_users)} aktiv)\n"
            voice_info += f"🔇 Muted: {ctx.author.voice.mute or ctx.author.voice.self_mute}\n"
            voice_info += f"✅ Du bist aktiv: {await self.is_user_active(ctx.author)}"
        else:
            voice_info = "❌ Nicht in Voice"

        embed.add_field(name="🎧 Voice Status", value=voice_info, inline=False)

        # System Statistics
        uptime = datetime.utcnow() - self.session_stats['uptime_start']
        stats_info = f"🕐 Uptime: {uptime.days}d {uptime.seconds//3600}h\n"
        stats_info += f"📈 Sessions erstellt: {self.session_stats['total_sessions_created']}\n"
        stats_info += f"🛡️ Grace Periods: {self.session_stats['total_grace_periods']}"

        embed.add_field(name="📊 System Stats", value=stats_info, inline=True)

        # Rate Limiting / Pfade
        rate_info = f"⏰ Cooldown: {self.rate_limiter.get_remaining_time(ctx.author.id)}s\n"
        rate_info += f"🗄️ Zentrale DB: ...{self.db_path[-40:]}\n"
        rate_info += f"⚙️ Config: zentral in DB (voice_config)"
        embed.add_field(name="⚙️ Technical", value=rate_info, inline=True)

        embed.set_footer(text="Enhanced Voice Activity Tracker | Use !vstats and !vleaderboard")

        await ctx.send(embed=embed)

    # ===== ADMIN COMMANDS =====

    @commands.command(name="voice_status")
    @commands.has_permissions(administrator=True)
    async def voice_status_command(self, ctx):
        """Detailed system status for administrators"""
        try:
            cfg = await self.cfg(ctx.guild.id)

            embed = discord.Embed(title="🔧 Voice System Admin Status", color=0x00ff99)

            # System Overview
            embed.add_field(name="🔴 Live Sessions", value=len(self.voice_sessions), inline=True)
            embed.add_field(name="🛡️ Grace Periods", value=len(self.grace_period_users), inline=True)
            embed.add_field(name="🗄️ Database", value="Connected" if self.db_manager.db else "Disconnected", inline=True)

            # Configuration
            embed.add_field(name="⚙️ Min Users", value=cfg.min_users_for_tracking, inline=True)
            embed.add_field(name="⏱️ Grace Duration", value=f"{cfg.grace_period_duration}s", inline=True)
            embed.add_field(name="🎖️ Role ID", value=cfg.special_role_id, inline=True)

            # Performance Stats
            uptime = datetime.utcnow() - self.session_stats['uptime_start']
            embed.add_field(name="🕐 Uptime", value=f"{uptime.days}d {uptime.seconds//3600}h", inline=True)
            embed.add_field(name="📈 Total Sessions", value=self.session_stats['total_sessions_created'], inline=True)
            embed.add_field(name="🛡️ Total Grace", value=self.session_stats['total_grace_periods'], inline=True)

            # Database Info
            backup_status = "✅ Available" if self.db_manager.last_backup else "⚠️ No recent backup"
            embed.add_field(name="💾 Last Backup", value=backup_status, inline=True)
            embed.add_field(name="📁 DB Path", value=f"...{self.db_path[-30:]}", inline=True)
            embed.add_field(name="⚙️ Config Store", value="voice_config (DB-zentral)", inline=True)

            # Live Session Details
            if self.voice_sessions:
                session_details = []
                for session_key, session in list(self.voice_sessions.items())[:3]:
                    user = self.bot.get_user(session['user_id'])
                    if user:
                        duration = (datetime.utcnow() - session['start_time']).total_seconds() / 60
                        grace_indicator = ""

                        grace_key = f"{session['user_id']}:{session['guild_id']}"
                        if grace_key in self.grace_period_users:
                            grace_time = (datetime.utcnow() - self.grace_period_users[grace_key]['start_time']).total_seconds()
                            remaining = max(0, cfg.grace_period_duration - grace_time)
                            grace_indicator = f" 🛡️({remaining:.0f}s)"

                        session_details.append(f"{user.display_name}: {duration:.1f}m ({session['total_points']:.1f}pts){grace_indicator}")

                if session_details:
                    embed.add_field(name="🔴 Live Sessions (Sample)", value="\n".join(session_details), inline=False)

            embed.set_footer(text="Enhanced Voice Activity Tracker - Admin Panel")
            await ctx.send(embed=embed)

        except Exception as e:
            await ctx.send(f"❌ Fehler beim Abrufen des Status: {e}")

    @commands.command(name="voice_config")
    @commands.has_permissions(administrator=True)
    async def voice_config_command(self, ctx, setting=None, value=None):
        """Configure voice tracker settings (zentral in DB; keine JSON-Datei)"""
        cfg = await self.cfg(ctx.guild.id)

        if not setting:
            embed = discord.Embed(title="⚙️ Voice Tracker Configuration (DB-zentral)", color=0x0099ff)

            embed.add_field(name="👥 Min Users", value=cfg.min_users_for_tracking, inline=True)
            embed.add_field(name="⏱️ Grace Duration", value=f"{cfg.grace_period_duration}s", inline=True)
            embed.add_field(name="🎖️ Special Role", value=cfg.special_role_id, inline=True)
            embed.add_field(name="🔄 Session Timeout", value=f"{cfg.session_timeout}s", inline=True)
            embed.add_field(name="💾 Backup Interval", value=f"{cfg.backup_interval_hours}h", inline=True)
            embed.add_field(name="📊 Max Sessions", value=cfg.max_sessions_per_user, inline=True)

            embed.add_field(
                name="Available Settings",
                value="```\n!voice_config grace_duration <seconds>\n!voice_config grace_role <role_id>\n!voice_config min_users <2-10>\n!voice_config session_timeout <seconds>\n!voice_config max_sessions <number>\n```",
                inline=False
            )

            await ctx.send(embed=embed)
            return

        try:
            setting = setting.lower().strip()

            if setting == "grace_duration":
                duration = int(value)
                if 60 <= duration <= 600:  # 1-10 minutes
                    await self.config_manager.set(ctx.guild.id, 'grace_period_duration', duration)
                    await ctx.send(f"✅ Grace period duration set to {duration} seconds (zentral gespeichert)")
                else:
                    await ctx.send("❌ Grace duration must be between 60 and 600 seconds")

            elif setting == "grace_role":
                role_id = int(value)
                await self.config_manager.set(ctx.guild.id, 'special_role_id', role_id)
                await ctx.send(f"✅ Grace period role set to <@&{role_id}> (zentral gespeichert)")

            elif setting == "min_users":
                min_users = int(value)
                if 2 <= min_users <= 10:
                    await self.config_manager.set(ctx.guild.id, 'min_users_for_tracking', min_users)
                    await ctx.send(f"✅ Minimum users set to {min_users} (zentral gespeichert)")
                else:
                    await ctx.send("❌ Minimum users must be between 2 and 10")

            elif setting == "session_timeout":
                to = int(value)
                if 60 <= to <= 3600:
                    await self.config_manager.set(ctx.guild.id, 'session_timeout', to)
                    await ctx.send(f"✅ Session timeout set to {to}s (zentral gespeichert)")
                else:
                    await ctx.send("❌ Session timeout must be between 60 and 3600 seconds")

            elif setting == "max_sessions":
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

    @commands.command(name="voice_backup")
    @commands.has_permissions(administrator=True)
    async def voice_backup_command(self, ctx):
        """Create manual database backup (zentral)"""
        try:
            backup_path = await self.db_manager.backup_database()
            if backup_path:
                await ctx.send(f"✅ Database backup created: `{os.path.basename(backup_path)}`")
            else:
                await ctx.send("❌ Backup creation failed")
        except Exception as e:
            await ctx.send(f"❌ Backup error: {e}")

    @commands.command(name="voice_analytics")
    @commands.has_permissions(administrator=True)
    async def voice_analytics_command(self, ctx, days: int = 7):
        """Get server analytics for the past N days"""
        if days < 1 or days > 30:
            days = 7

        try:
            # Get daily statistics
            result = await self.db_manager.safe_execute(f'''
                SELECT DATE(start_time) as date,
                       COUNT(*) as sessions,
                       SUM(duration_seconds) as total_time,
                       SUM(points_earned) as total_points,
                       COUNT(DISTINCT user_id) as unique_users,
                       MAX(peak_users) as peak_users
                FROM voice_sessions 
                WHERE guild_id = ? 
                AND start_time >= datetime('now', '-{days} days')
                GROUP BY DATE(start_time)
                ORDER BY date DESC
            ''', (ctx.guild.id,), fetch_all=True)

            if not result:
                await ctx.send(f"📊 No voice activity data found for the past {days} days.")
                return

            embed = discord.Embed(
                title=f"📊 Voice Analytics - Last {days} Days",
                color=discord.Color.blue()
            )

            total_sessions = sum(row['sessions'] for row in result)
            total_hours = (sum(row['total_time'] for row in result) or 0) // 3600
            total_points = sum(row['total_points'] for row in result) or 0.0
            # Hinweis: unique_users hier pro Tag aggregiert, kein globaler Distinct – traditionell einfach halten.

            embed.add_field(name="📈 Total Sessions", value=total_sessions, inline=True)
            embed.add_field(name="⏱️ Total Hours", value=f"{total_hours}h", inline=True)
            embed.add_field(name="⭐ Total Points", value=f"{total_points:.0f}", inline=True)
            embed.add_field(name="📊 Avg Session/Day", value=f"{total_sessions/max(1,len(result)):.1f}", inline=True)
            embed.add_field(name="🎯 Avg Hours/Day", value=f"{total_hours/max(1,len(result)):.1f}h", inline=True)

            # Recent activity
            recent_data = []
            for row in result[:5]:
                hours = (row['total_time'] or 0) // 3600
                recent_data.append(f"**{row['date']}**: {row['sessions']} sessions, {hours}h, {row['unique_users']} users")

            if recent_data:
                embed.add_field(name="📅 Recent Activity", value="\n".join(recent_data), inline=False)

            embed.set_footer(text=f"Analytics for {ctx.guild.name} • Use !voice_status for live data")
            await ctx.send(embed=embed)

        except Exception as e:
            logger.error(f"Analytics error: {e}")
            await ctx.send(f"❌ Error generating analytics: {e}")

async def setup(bot):
    await bot.add_cog(VoiceActivityTrackerCog(bot))
