"""
User Activity Analyzer & Smart Pinging System

Features:
- Analysiert User-Aktivität der letzten 2 Wochen
- Erkennt typische Online-Zeiten (Uhrzeiten & Wochentage)
- Trackt wer mit wem zusammen spielt
- Smart Pinging mit KI-generierten, menschlich klingenden Nachrichten
- Rate-Limiting zum Schutz vor Spam
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

import discord
from discord.ext import commands, tasks

from service import db as central_db

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

logger = logging.getLogger(__name__)


class UserActivityAnalyzer(commands.Cog):
    """
    Analysiert User-Aktivität und bietet Smart-Pinging mit personalisierten Nachrichten.
    """

    def __init__(self, bot):
        self.bot = bot

        # OpenAI Client für menschliche Nachrichten
        self.openai_client = None
        if OpenAI:
            api_key = os.getenv("OPENAI_API_KEY") or os.getenv("DEADLOCK_OPENAI_KEY")
            if api_key:
                self.openai_client = OpenAI(api_key=api_key)
                logger.info("OpenAI Client initialized for Activity Analyzer")
            else:
                logger.warning("OpenAI API Key not found - AI messages disabled")

        # Cache für Co-Spieler-Daten (wird alle 30 Min refreshed)
        self._co_player_cache: Dict[int, List[Tuple[int, int]]] = {}
        self._cache_timestamp = datetime.utcnow()

        logger.info("User Activity Analyzer initializing")

    async def cog_load(self):
        """Startet Background-Tasks für Analyse und Tracking."""
        # Starte Background-Tasks
        self.analyze_user_activity.start()
        self.track_co_players_realtime.start()
        self.cleanup_old_pings.start()

        logger.info("User Activity Analyzer loaded - Background tasks started")

    async def cog_unload(self):
        """Stoppt Background-Tasks sauber."""
        tasks_to_cancel = [
            self.analyze_user_activity,
            self.track_co_players_realtime,
            self.cleanup_old_pings,
        ]
        for task in tasks_to_cancel:
            if task.is_running():
                task.cancel()

        await asyncio.gather(*[
            task.wait_for_cancel() if hasattr(task, 'wait_for_cancel') else asyncio.sleep(0)
            for task in tasks_to_cancel if task.is_running()
        ], return_exceptions=True)

        logger.info("User Activity Analyzer unloaded")

    # ========== ACTIVITY ANALYSIS ==========

    @tasks.loop(hours=6)
    async def analyze_user_activity(self):
        """
        Analysiert alle User-Aktivitäten der letzten 2 Wochen.
        Läuft alle 6 Stunden.
        """
        try:
            logger.info("Starting activity analysis for last 2 weeks...")

            # Cutoff: 2 Wochen zurück
            cutoff = datetime.utcnow() - timedelta(days=14)
            cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

            # Hole alle Sessions der letzten 2 Wochen
            sessions = central_db.query_all(
                """
                SELECT user_id, started_at, ended_at, duration_seconds, channel_id
                FROM voice_session_log
                WHERE started_at >= ?
                ORDER BY user_id, started_at
                """,
                (cutoff_str,)
            )

            if not sessions:
                logger.info("No sessions found in last 2 weeks")
                return

            # Gruppiere Sessions nach User
            user_sessions = defaultdict(list)
            for row in sessions:
                user_sessions[row[0]].append({
                    'user_id': row[0],
                    'started_at': datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S") if row[1] else None,
                    'ended_at': datetime.strptime(row[2], "%Y-%m-%d %H:%M:%S") if row[2] else None,
                    'duration_seconds': row[3] or 0,
                    'channel_id': row[4],
                })

            # Analysiere jeden User
            analyzed_count = 0
            for user_id, user_session_list in user_sessions.items():
                await self._analyze_single_user(user_id, user_session_list)
                analyzed_count += 1

            logger.info(f"Activity analysis completed: {analyzed_count} users analyzed")

        except Exception as e:
            logger.error(f"Error in activity analysis: {e}", exc_info=True)

    @analyze_user_activity.before_loop
    async def before_analyze(self):
        await self.bot.wait_until_ready()

    async def _analyze_single_user(self, user_id: int, sessions: List[Dict]):
        """Analysiert einen einzelnen User und speichert die Patterns."""
        try:
            if not sessions:
                return

            # === Zeitfenster-Analyse ===
            hour_counts = defaultdict(int)  # Stunde -> Anzahl Sessions
            day_counts = defaultdict(int)   # Wochentag (0=Mo, 6=So) -> Anzahl Sessions

            total_minutes = 0
            last_active = None

            # === Co-Spieler-Tracking aus Session-Logs ===
            co_player_minutes = defaultdict(int)  # co_player_id -> total minutes together

            for session in sessions:
                started_at = session.get('started_at')
                duration_seconds = session.get('duration_seconds') or 0
                duration_minutes = duration_seconds // 60

                if started_at:
                    # Stunde (0-23)
                    hour = started_at.hour
                    hour_counts[hour] += 1

                    # Wochentag (0=Mo, 6=So)
                    weekday = started_at.weekday()
                    day_counts[weekday] += 1

                    # Last Active
                    if last_active is None or started_at > last_active:
                        last_active = started_at

                # Total Minutes
                total_minutes += duration_minutes

                # Co-Spieler aus dieser Session (falls vorhanden)
                # Wird vom voice_activity_tracker als JSON-Liste gespeichert
                # TODO: Diese Daten werden erst nach dem nächsten Reload verfügbar sein

            # === Top 3 häufigste Stunden ===
            top_hours = sorted(hour_counts.items(), key=lambda x: x[1], reverse=True)[:3]
            typical_hours = [h for h, _ in top_hours]

            # === Top 3 häufigste Wochentage ===
            top_days = sorted(day_counts.items(), key=lambda x: x[1], reverse=True)[:3]
            typical_days = [d for d, _ in top_days]

            # === Activity Score (Anzahl Sessions in 2W) ===
            activity_score = len(sessions)

            # === Speichere in DB ===
            central_db.execute(
                """
                INSERT INTO user_activity_patterns(
                    user_id, typical_hours, typical_days,
                    activity_score_2w, sessions_count_2w, total_minutes_2w,
                    last_active_at, last_analyzed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    typical_hours = excluded.typical_hours,
                    typical_days = excluded.typical_days,
                    activity_score_2w = excluded.activity_score_2w,
                    sessions_count_2w = excluded.sessions_count_2w,
                    total_minutes_2w = excluded.total_minutes_2w,
                    last_active_at = excluded.last_active_at,
                    last_analyzed_at = CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    json.dumps(typical_hours),
                    json.dumps(typical_days),
                    activity_score,
                    len(sessions),
                    total_minutes,
                    last_active.strftime("%Y-%m-%d %H:%M:%S") if last_active else None,
                )
            )

            # === Co-Spieler aus Session-Logs analysieren (falls vorhanden) ===
            await self._analyze_co_players_from_sessions(user_id)

        except Exception as e:
            logger.error(f"Error analyzing user {user_id}: {e}", exc_info=True)

    async def _analyze_co_players_from_sessions(self, user_id: int):
        """
        Analysiert Co-Spieler aus voice_session_log.co_player_ids
        und aktualisiert die user_co_players Tabelle.
        """
        try:
            cutoff = datetime.utcnow() - timedelta(days=14)
            cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

            # Hole alle Sessions mit co_player_ids
            rows = central_db.query_all(
                """
                SELECT co_player_ids, duration_seconds
                FROM voice_session_log
                WHERE user_id = ? AND started_at >= ? AND co_player_ids IS NOT NULL
                """,
                (user_id, cutoff_str)
            )

            if not rows:
                return

            # Aggregiere Co-Spieler-Daten
            co_player_stats = defaultdict(lambda: {'sessions': 0, 'minutes': 0})

            for row in rows:
                co_player_ids_json = row[0]
                duration_seconds = row[1] or 0
                duration_minutes = duration_seconds // 60

                if not co_player_ids_json:
                    continue

                try:
                    co_player_ids = json.loads(co_player_ids_json)
                    for co_id in co_player_ids:
                        co_player_stats[co_id]['sessions'] += 1
                        co_player_stats[co_id]['minutes'] += duration_minutes
                except json.JSONDecodeError:
                    continue

            # Speichere/Update in DB
            for co_id, stats in co_player_stats.items():
                central_db.execute(
                    """
                    INSERT INTO user_co_players(
                        user_id, co_player_id, sessions_together,
                        total_minutes_together, last_played_together
                    )
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, co_player_id) DO UPDATE SET
                        sessions_together = sessions_together + excluded.sessions_together,
                        total_minutes_together = total_minutes_together + excluded.total_minutes_together,
                        last_played_together = CURRENT_TIMESTAMP
                    """,
                    (user_id, co_id, stats['sessions'], stats['minutes'])
                )

        except Exception as e:
            logger.error(f"Error analyzing co-players for {user_id}: {e}", exc_info=True)

    # ========== CO-PLAYER TRACKING ==========

    @tasks.loop(minutes=10)
    async def track_co_players_realtime(self):
        """
        Trackt wer aktuell mit wem in Voice-Channels ist.
        Läuft alle 10 Minuten.
        """
        try:
            logger.debug("Tracking co-players in voice channels...")

            # Durchlaufe alle Guilds
            for guild in self.bot.guilds:
                await self._track_guild_co_players(guild)

        except Exception as e:
            logger.error(f"Error tracking co-players: {e}", exc_info=True)

    @track_co_players_realtime.before_loop
    async def before_track_co_players(self):
        await self.bot.wait_until_ready()

    async def _track_guild_co_players(self, guild: discord.Guild):
        """Trackt Co-Spieler für eine einzelne Guild."""
        try:
            # Durchlaufe alle Voice-Channels
            for channel in guild.voice_channels:
                if not channel.members or len(channel.members) < 2:
                    continue

                # Filter: Nur echte User (keine Bots)
                real_members = [m for m in channel.members if not m.bot]

                if len(real_members) < 2:
                    continue

                # Alle Paarungen speichern
                for i, member1 in enumerate(real_members):
                    for member2 in real_members[i+1:]:
                        await self._record_co_player_session(
                            member1.id,
                            member2.id,
                            duration_minutes=10  # 10 Min pro Loop-Interval
                        )

        except Exception as e:
            logger.error(f"Error tracking co-players in guild {guild.id}: {e}", exc_info=True)

    async def _record_co_player_session(self, user_id: int, co_player_id: int, duration_minutes: int = 10):
        """Speichert eine Co-Player-Session (bidirektional)."""
        try:
            # Beide Richtungen speichern (A->B und B->A)
            for uid, co_uid in [(user_id, co_player_id), (co_player_id, user_id)]:
                central_db.execute(
                    """
                    INSERT INTO user_co_players(
                        user_id, co_player_id, sessions_together,
                        total_minutes_together, last_played_together
                    )
                    VALUES (?, ?, 1, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id, co_player_id) DO UPDATE SET
                        sessions_together = sessions_together + 1,
                        total_minutes_together = total_minutes_together + excluded.total_minutes_together,
                        last_played_together = CURRENT_TIMESTAMP
                    """,
                    (uid, co_uid, duration_minutes)
                )

            # Invalidiere Cache
            if user_id in self._co_player_cache:
                del self._co_player_cache[user_id]
            if co_player_id in self._co_player_cache:
                del self._co_player_cache[co_player_id]

        except Exception as e:
            logger.error(f"Error recording co-player session: {e}", exc_info=True)

    async def get_top_co_players(self, user_id: int, limit: int = 5) -> List[Tuple[int, int]]:
        """
        Gibt die Top Co-Spieler eines Users zurück.
        Returns: List of (co_player_id, sessions_together)
        """
        # Cache-Check
        now = datetime.utcnow()
        if user_id in self._co_player_cache and (now - self._cache_timestamp).total_seconds() < 1800:
            return self._co_player_cache[user_id][:limit]

        try:
            rows = central_db.query_all(
                """
                SELECT co_player_id, sessions_together
                FROM user_co_players
                WHERE user_id = ?
                ORDER BY sessions_together DESC, total_minutes_together DESC
                LIMIT ?
                """,
                (user_id, limit)
            )

            result = [(row[0], row[1]) for row in rows]
            self._co_player_cache[user_id] = result
            return result

        except Exception as e:
            logger.error(f"Error getting co-players for {user_id}: {e}", exc_info=True)
            return []

    # ========== CLEANUP ==========

    @tasks.loop(hours=24)
    async def cleanup_old_pings(self):
        """
        Resettet ping_count_30d für User die länger als 30 Tage nicht gepingt wurden.
        Läuft täglich.
        """
        try:
            cutoff = datetime.utcnow() - timedelta(days=30)
            cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")

            central_db.execute(
                """
                UPDATE user_activity_patterns
                SET ping_count_30d = 0
                WHERE last_pinged_at < ? OR last_pinged_at IS NULL
                """,
                (cutoff_str,)
            )

            logger.info("Cleaned up old ping counts")

        except Exception as e:
            logger.error(f"Error cleaning up pings: {e}", exc_info=True)

    @cleanup_old_pings.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    # ========== SMART PINGING ==========

    async def should_ping_user(self, user_id: int, max_pings_30d: int = 3) -> Tuple[bool, str]:
        """
        Prüft ob ein User gepingt werden sollte.

        Returns:
            (can_ping: bool, reason: str)
        """
        try:
            # Hole Pattern-Daten
            row = central_db.query_one(
                """
                SELECT typical_hours, typical_days, activity_score_2w,
                       last_pinged_at, ping_count_30d
                FROM user_activity_patterns
                WHERE user_id = ?
                """,
                (user_id,)
            )

            if not row:
                return False, "Keine Aktivitätsdaten vorhanden"

            typical_hours_json = row[0]
            typical_days_json = row[1]
            activity_score = row[2] or 0
            last_pinged_str = row[3]
            ping_count = row[4] or 0

            # Check 1: Rate-Limit (max 3 Pings in 30 Tagen)
            if ping_count >= max_pings_30d:
                return False, f"Rate-Limit erreicht ({ping_count}/{max_pings_30d} in 30d)"

            # Check 2: Mindestens 1 Tag seit letztem Ping
            if last_pinged_str:
                last_pinged = datetime.strptime(last_pinged_str, "%Y-%m-%d %H:%M:%S")
                time_since_ping = (datetime.utcnow() - last_pinged).total_seconds()
                if time_since_ping < 86400:  # 24h
                    hours_remaining = (86400 - time_since_ping) / 3600
                    return False, f"Zu früh (noch {hours_remaining:.1f}h bis nächster Ping)"

            # Check 3: User sollte aktiv sein (min 5 Sessions in 2W)
            if activity_score < 5:
                return False, f"User zu inaktiv (nur {activity_score} Sessions in 2W)"

            # Check 4: Typische Online-Zeit?
            now = datetime.utcnow()
            current_hour = now.hour
            current_day = now.weekday()

            typical_hours = json.loads(typical_hours_json) if typical_hours_json else []
            typical_days = json.loads(typical_days_json) if typical_days_json else []

            # Flexibles Zeitfenster: ±2 Stunden von typischen Zeiten
            hour_match = False
            for typ_hour in typical_hours:
                if abs(current_hour - typ_hour) <= 2 or abs(current_hour - typ_hour) >= 22:  # wrap around
                    hour_match = True
                    break

            if not hour_match:
                return False, f"Außerhalb typischer Online-Zeiten (typisch: {typical_hours}h)"

            # Wochentag ist optional (falls verfügbar, prüfen wir es)
            if typical_days and current_day not in typical_days:
                day_names = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
                return False, f"Unpassender Wochentag (typisch: {[day_names[d] for d in typical_days]})"

            return True, "OK - User kann gepingt werden"

        except Exception as e:
            logger.error(f"Error checking ping eligibility for {user_id}: {e}", exc_info=True)
            return False, f"Fehler: {e}"

    async def generate_ping_message(
        self,
        user: discord.Member,
        reason: str = "join",
        co_players: Optional[List[discord.Member]] = None
    ) -> str:
        """
        Generiert eine menschlich klingende Ping-Nachricht mit KI.

        Args:
            user: Der zu pingende User
            reason: Grund für den Ping ("join", "game_ready", "friends_online", etc.)
            co_players: Optional - Liste von Mitspielern die bereits online sind
        """
        # Fallback ohne KI
        if not self.openai_client:
            return await self._generate_fallback_message(user, reason, co_players)

        try:
            # Kontext für KI aufbauen
            context_parts = []

            # Co-Spieler erwähnen
            if co_players:
                names = ", ".join([m.display_name for m in co_players[:3]])
                if len(co_players) > 3:
                    names += f" und {len(co_players) - 3} weitere"
                context_parts.append(f"Deine Mitspieler {names} sind gerade online")

            # Grund
            reason_texts = {
                "join": "zum Spielen einladen",
                "game_ready": "dass ein Game startet",
                "friends_online": "dass Freunde online sind",
                "ranked_session": "zu einer Ranked Session einladen",
            }
            reason_text = reason_texts.get(reason, "zum Spielen einladen")

            # Prompt für GPT
            system_prompt = """Du bist ein freundlicher Discord-Bot der Spieler zum Gaming einlädt.

Schreibe eine kurze, lockere Nachricht auf Deutsch die:
- Natürlich und menschlich klingt (keine AI-Sprache!)
- Freundlich und einladend ist
- Nicht nach Bot klingt
- Umgangssprache nutzt (z.B. "Bock auf ne Runde?", "Hey, Zeit für Deadlock?")
- Maximal 1-2 Sätze lang ist
- Keine Emojis enthält (außer höchstens 1-2 am Anfang)

Schreibe NUR die Nachricht, nichts anderes."""

            user_prompt = f"""Schreib eine Ping-Nachricht um {user.display_name} {reason_text}.

Kontext:
{chr(10).join(context_parts) if context_parts else 'Keine besonderen Infos'}

Wichtig: Die Nachricht soll locker und wie von einem Freund klingen, nicht wie von einem Bot!"""

            # API Call
            response = self.openai_client.chat.completions.create(
                model="gpt-4o-mini",  # Schnell & günstig
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=100,
                temperature=0.9,  # Kreativ für Varianz
            )

            message = response.choices[0].message.content.strip()

            # Ping hinzufügen
            return f"{user.mention} {message}"

        except Exception as e:
            logger.error(f"Error generating AI message: {e}", exc_info=True)
            return await self._generate_fallback_message(user, reason, co_players)

    async def _generate_fallback_message(
        self,
        user: discord.Member,
        reason: str,
        co_players: Optional[List[discord.Member]] = None
    ) -> str:
        """Fallback-Nachrichten ohne KI."""
        templates = {
            "join": [
                f"{user.mention} Hey, Bock auf ne Runde Deadlock?",
                f"{user.mention} Wir sind am zocken, hast du Zeit?",
                f"{user.mention} Lust auf ein Match?",
            ],
            "game_ready": [
                f"{user.mention} Game ist ready, bist du dabei?",
                f"{user.mention} Wir starten gleich, kommst du?",
            ],
            "friends_online": [
                f"{user.mention} Deine Crew ist online!",
                f"{user.mention} Deine Mitspieler warten schon",
            ],
        }

        import random
        options = templates.get(reason, templates["join"])
        base_msg = random.choice(options)

        # Co-Spieler erwähnen
        if co_players:
            names = ", ".join([m.display_name for m in co_players[:2]])
            base_msg += f" ({names} sind schon da)"

        return base_msg

    async def record_ping(self, user_id: int):
        """Speichert dass ein User gepingt wurde (für Rate-Limiting)."""
        try:
            central_db.execute(
                """
                UPDATE user_activity_patterns
                SET last_pinged_at = CURRENT_TIMESTAMP,
                    ping_count_30d = ping_count_30d + 1
                WHERE user_id = ?
                """,
                (user_id,)
            )
        except Exception as e:
            logger.error(f"Error recording ping for {user_id}: {e}", exc_info=True)

    # ========== COMMANDS ==========

    @commands.command(name="myactivity")
    async def my_activity_command(self, ctx, user: Optional[discord.Member] = None):
        """Zeigt deine Aktivitätsmuster der letzten 2 Wochen."""
        target = user or ctx.author

        try:
            row = central_db.query_one(
                """
                SELECT typical_hours, typical_days, activity_score_2w,
                       sessions_count_2w, total_minutes_2w, last_active_at,
                       ping_count_30d, last_pinged_at
                FROM user_activity_patterns
                WHERE user_id = ?
                """,
                (target.id,)
            )

            if not row:
                await ctx.send(f"❌ Keine Aktivitätsdaten für {target.display_name} vorhanden.")
                return

            typical_hours = json.loads(row[0]) if row[0] else []
            typical_days = json.loads(row[1]) if row[1] else []
            activity_score = row[2] or 0
            sessions_count = row[3] or 0
            total_minutes = row[4] or 0
            last_active_str = row[5]
            ping_count = row[6] or 0
            last_pinged_str = row[7]

            embed = discord.Embed(
                title=f"📊 Aktivitätsmuster - {target.display_name}",
                color=discord.Color.blue()
            )

            # Typische Online-Zeiten
            if typical_hours:
                hours_str = ", ".join([f"{h}:00" for h in typical_hours])
                embed.add_field(name="🕐 Typische Online-Zeiten", value=hours_str, inline=False)

            # Typische Wochentage
            if typical_days:
                day_names = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
                days_str = ", ".join([day_names[d] for d in typical_days])
                embed.add_field(name="📅 Typische Wochentage", value=days_str, inline=False)

            # Activity Score
            embed.add_field(name="⭐ Activity Score (2W)", value=f"{activity_score} Sessions", inline=True)
            embed.add_field(name="⏱️ Gesamtzeit (2W)", value=f"{total_minutes // 60}h {total_minutes % 60}m", inline=True)

            # Last Active
            if last_active_str:
                last_active = datetime.strptime(last_active_str, "%Y-%m-%d %H:%M:%S")
                time_ago = datetime.utcnow() - last_active
                days_ago = time_ago.days
                hours_ago = time_ago.seconds // 3600
                embed.add_field(
                    name="🔴 Zuletzt aktiv",
                    value=f"vor {days_ago}d {hours_ago}h",
                    inline=True
                )

            # Ping Stats
            embed.add_field(name="📬 Pings (30d)", value=f"{ping_count}/3", inline=True)

            # Co-Spieler
            co_players = await self.get_top_co_players(target.id, limit=5)
            if co_players:
                co_player_lines = []
                for co_id, sessions_together in co_players[:3]:
                    co_member = ctx.guild.get_member(co_id)
                    name = co_member.display_name if co_member else f"User {co_id}"
                    co_player_lines.append(f"**{name}** ({sessions_together}x zusammen)")

                embed.add_field(
                    name="👥 Top Mitspieler",
                    value="\n".join(co_player_lines),
                    inline=False
                )

            embed.set_thumbnail(url=target.display_avatar.url)
            embed.set_footer(text="Daten der letzten 2 Wochen")
            await ctx.send(embed=embed)

        except Exception as e:
            logger.error(f"Error in myactivity command: {e}", exc_info=True)
            await ctx.send(f"❌ Fehler beim Abrufen der Daten: {e}")

    @commands.command(name="smartping")
    @commands.has_permissions(manage_messages=True)
    async def smart_ping_command(self, ctx, user: discord.Member, reason: str = "join"):
        """
        Pingt einen User mit einer personalisierten Nachricht (nur mit Permission).

        Usage: !smartping @User [reason]
        Reasons: join, game_ready, friends_online, ranked_session
        """
        # Check ob Ping erlaubt
        can_ping, check_reason = await self.should_ping_user(user.id)

        if not can_ping:
            await ctx.send(f"❌ Kann {user.display_name} nicht pingen: {check_reason}")
            return

        # Hole Co-Spieler die online sind
        co_players_data = await self.get_top_co_players(user.id, limit=10)
        online_co_players = []

        for co_id, _ in co_players_data:
            member = ctx.guild.get_member(co_id)
            if member and member.voice and member.voice.channel:
                online_co_players.append(member)

        # Generiere Nachricht
        message = await self.generate_ping_message(user, reason, online_co_players)

        # Sende Nachricht
        await ctx.send(message)

        # Record Ping
        await self.record_ping(user.id)

        logger.info(f"Smart ping sent to {user.display_name} by {ctx.author.display_name}")

    @commands.command(name="checkping")
    async def check_ping_command(self, ctx, user: Optional[discord.Member] = None):
        """Prüft ob ein User gepingt werden kann."""
        target = user or ctx.author

        can_ping, reason = await self.should_ping_user(target.id)

        embed = discord.Embed(
            title=f"🔔 Ping-Check - {target.display_name}",
            color=discord.Color.green() if can_ping else discord.Color.red()
        )

        embed.add_field(
            name="Status",
            value="✅ Kann gepingt werden" if can_ping else "❌ Kann nicht gepingt werden",
            inline=False
        )

        embed.add_field(name="Grund", value=reason, inline=False)

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(UserActivityAnalyzer(bot))
