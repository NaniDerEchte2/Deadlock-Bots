"""
Steam Rank Checker - LFG System
Überwacht einen Discord-Textkanal und pingt Spieler basierend auf Rank und Steam-Online-Status
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional, Set, Tuple

import aiohttp
import aiosqlite
import discord
from discord.ext import commands

from service.db import db_path

log = logging.getLogger("SteamRankChecker")

# Kanal-ID aus der Discord-URL: https://discord.com/channels/1289721245281292288/1376335502919335936
LFG_CHANNEL_ID = int(os.getenv("LFG_CHANNEL_ID", "1376335502919335936"))
GUILD_ID = int(os.getenv("LFG_GUILD_ID", "1289721245281292288"))

# Steam presence freshness (wie lange die Daten maximal alt sein dürfen)
PRESENCE_STALE_SECONDS = 300  # 5 Minuten

# Activity Pattern Analysis
ACTIVITY_ANALYSIS_DAYS = 14  # Analysiere letzte 2 Wochen
ACTIVITY_MIN_SESSIONS = 3    # Mindestens 3 Sessions für valide Muster

# Rank-Matching: wie viele Ränge Unterschied sind erlaubt?
RANK_TOLERANCE = int(os.getenv("LFG_RANK_TOLERANCE", "2"))  # +/- 2 Ränge (Grind-Modus)

# LFG Trigger-Wörter (case-insensitive)
LFG_TRIGGERS = [
    "lfg", "lf game", "looking for game", "suche mitspieler",
    "suche spieler", "wer will spielen", "jemand bock", "jmd bock",
    "zu zocken", "suche noch", "wer hat lust", "wer zockt"
]

# Voice Channel Kategorien (aus deadlock_voice_status.py und rank_voice_manager.py)
VOICE_CATEGORIES = {
    1357422957017698478: "ranked",  # Ranked Kategorie
    1412804540994162789: "grind",   # Grind Kategorie
    1289721245281292290: "casual",  # Casual/Spaß Kategorie
}

# AI Configuration (Anthropic Claude)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
USE_AI_DETECTION = os.getenv("LFG_USE_AI", "true").lower() in ("true", "1", "yes")

# Deadlock Rank-System (muss mit rank_voice_manager.py übereinstimmen)
DISCORD_RANK_ROLES = {
    1331457571118387210: ("Initiate", 1),
    1331457652877955072: ("Seeker", 2),
    1331457699992436829: ("Alchemist", 3),
    1331457724848017539: ("Arcanist", 4),
    1331457879345070110: ("Ritualist", 5),
    1331457898781474836: ("Emissary", 6),
    1331457949654319114: ("Archon", 7),
    1316966867033653338: ("Oracle", 8),
    1331458016356208680: ("Phantom", 9),
    1331458049637875785: ("Ascendant", 10),
    1331458087349129296: ("Eternus", 11),
}


class SteamRankChecker(commands.Cog):
    """
    Bot der Steam durchgeht basierend auf dem Rank,
    checkt wer online ist und diese dann pingt wenn jemand nach Spielern sucht.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: Optional[aiosqlite.Connection] = None

        # Rate limiting: User -> letzter LFG-Timestamp
        self.last_lfg_per_user: Dict[int, float] = {}
        self.lfg_cooldown_seconds = 300  # 5 Minuten Cooldown pro User

    async def cog_load(self) -> None:
        await self._ensure_db()
        log.info("SteamRankChecker geladen - überwacht Kanal %s", LFG_CHANNEL_ID)

    async def cog_unload(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None
        log.info("SteamRankChecker entladen")

    async def _ensure_db(self) -> None:
        if self.db:
            return
        self.db = await aiosqlite.connect(str(db_path()))
        self.db.row_factory = aiosqlite.Row

    def _get_user_rank_from_roles(self, member: discord.Member) -> Tuple[str, int]:
        """Ermittelt den höchsten Rang eines Users basierend auf Discord-Rollen"""
        highest_rank = ("Obscurus", 0)
        highest_rank_value = 0

        for role in member.roles:
            if role.id in DISCORD_RANK_ROLES:
                rank_name, rank_value = DISCORD_RANK_ROLES[role.id]
                if rank_value > highest_rank_value:
                    highest_rank = (rank_name, rank_value)
                    highest_rank_value = rank_value

        return highest_rank

    def _is_lfg_message(self, content: str) -> bool:
        """Prüft ob eine Nachricht ein LFG-Trigger enthält"""
        content_lower = content.lower()
        return any(trigger in content_lower for trigger in LFG_TRIGGERS)

    async def _ai_check_lfg_intent(self, message_content: str) -> bool:
        """
        Nutzt AI um zu prüfen ob jemand nach Mitspielern sucht.
        Nur wenn kein Keyword-Match gefunden wurde.
        """
        if not ANTHROPIC_API_KEY or not USE_AI_DETECTION:
            return False

        try:
            async with aiohttp.ClientSession() as session:
                headers = {
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }

                # Sehr sparsames Prompt für Token-Kosten
                payload = {
                    "model": "claude-3-haiku-20240307",  # Günstigstes Modell
                    "max_tokens": 10,
                    "messages": [{
                        "role": "user",
                        "content": f"Sucht diese Person nach Mitspielern für ein Spiel? Antworte nur 'ja' oder 'nein':\n\n\"{message_content}\""
                    }]
                }

                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status != 200:
                        log.warning("AI API Fehler: %s", resp.status)
                        return False

                    data = await resp.json()
                    answer = data.get("content", [{}])[0].get("text", "").lower().strip()
                    return "ja" in answer or "yes" in answer

        except Exception as exc:
            log.debug("AI-Check fehlgeschlagen: %s", exc)
            return False

    async def _get_all_steam_links(self) -> Dict[int, List[str]]:
        """
        Holt alle Discord User -> Steam ID Mappings.
        Returns: {discord_user_id: [steam_id1, steam_id2, ...]}
        """
        await self._ensure_db()
        if not self.db:
            return {}

        query = """
            SELECT user_id, steam_id
            FROM steam_links
            WHERE steam_id IS NOT NULL AND steam_id != ''
            AND verified = 1
            ORDER BY primary_account DESC, updated_at DESC
        """
        cursor = await self.db.execute(query)
        rows = await cursor.fetchall()
        await cursor.close()

        mapping: Dict[int, List[str]] = {}
        for row in rows:
            uid = int(row["user_id"])
            sid = str(row["steam_id"])
            mapping.setdefault(uid, []).append(sid)

        return mapping

    async def _get_online_steam_users(self, steam_ids: Set[str]) -> Dict[str, Tuple[str, Optional[int]]]:
        """
        Filtert Steam-IDs nach Online-Status (in Deadlock).
        Returns: {steam_id: (stage, minutes)}
        stage: 'lobby' oder 'match'
        minutes: Spielminuten bei 'match', None bei 'lobby'
        """
        await self._ensure_db()
        if not self.db or not steam_ids:
            return {}

        now = int(time.time())
        placeholders = ",".join("?" for _ in steam_ids)

        query = f"""
            SELECT steam_id, deadlock_stage, deadlock_minutes, deadlock_updated_at, last_seen_ts
            FROM live_player_state
            WHERE steam_id IN ({placeholders})
            AND (in_deadlock_now = 1 OR deadlock_stage IS NOT NULL)
        """

        cursor = await self.db.execute(query, tuple(steam_ids))
        rows = await cursor.fetchall()
        await cursor.close()

        online_map: Dict[str, Tuple[str, Optional[int]]] = {}

        for row in rows:
            updated_at = row["deadlock_updated_at"] or row["last_seen_ts"]
            if not updated_at:
                continue

            # Zu alte Daten überspringen
            if now - int(updated_at) > PRESENCE_STALE_SECONDS:
                continue

            stage = row["deadlock_stage"]
            if stage not in {"lobby", "match"}:
                continue

            minutes = row["deadlock_minutes"]
            online_map[str(row["steam_id"])] = (stage, minutes)

        return online_map

    async def _find_matching_players(
        self,
        author_rank_value: int,
        author_id: int
    ) -> List[Tuple[int, str, int, str, Optional[int]]]:
        """
        Findet Discord-User die:
        1. Steam-Account verknüpft haben
        2. Online in Deadlock sind (Lobby oder Match)
        3. Rang im Toleranzbereich haben

        Returns: [(discord_user_id, rank_name, rank_value, stage, minutes), ...]
        """
        # Alle Steam-Links holen
        steam_links = await self._get_all_steam_links()

        # Alle Steam-IDs sammeln
        all_steam_ids = {sid for sids in steam_links.values() for sid in sids}

        # Online-Status filtern
        online_users = await self._get_online_steam_users(all_steam_ids)

        if not online_users:
            return []

        # Discord-User zu Steam-IDs mappen und Rang checken
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            log.warning("Guild %s nicht gefunden", GUILD_ID)
            return []

        matching_players: List[Tuple[int, str, int, str, Optional[int]]] = []
        min_rank = max(1, author_rank_value - RANK_TOLERANCE)
        max_rank = min(11, author_rank_value + RANK_TOLERANCE)

        for discord_id, steam_ids in steam_links.items():
            # Autor überspringen
            if discord_id == author_id:
                continue

            # Prüfen ob einer der Steam-Accounts online ist
            user_online = False
            user_stage = None
            user_minutes = None
            for sid in steam_ids:
                if sid in online_users:
                    user_online = True
                    user_stage, user_minutes = online_users[sid]
                    break

            if not user_online:
                continue

            # Discord Member holen und Rang prüfen
            member = guild.get_member(discord_id)
            if not member or member.bot:
                continue

            rank_name, rank_value = self._get_user_rank_from_roles(member)

            # Rang-Matching
            if min_rank <= rank_value <= max_rank:
                matching_players.append((discord_id, rank_name, rank_value, user_stage, user_minutes))

        return matching_players

    async def _analyze_user_activity_patterns(
        self,
        user_id: int
    ) -> Optional[Dict[str, any]]:
        """
        Analysiert Aktivitätsmuster eines Users:
        - Zu welchen Uhrzeiten ist er normalerweise online?
        - Wie aktiv war er in den letzten 2 Wochen?

        Returns: {
            'active_hours': [14, 15, 16, 20, 21, 22],  # Uhrzeiten (0-23)
            'total_sessions': 15,
            'total_minutes': 450,
            'avg_session_length': 30,
            'is_currently_typical_time': True/False
        }
        """
        await self._ensure_db()
        if not self.db:
            return None

        # Letzte 2 Wochen analysieren
        cutoff_date = time.time() - (ACTIVITY_ANALYSIS_DAYS * 24 * 3600)

        query = """
            SELECT
                started_at,
                ended_at,
                duration_seconds
            FROM voice_session_log
            WHERE user_id = ?
            AND guild_id = ?
            AND started_at >= datetime(?, 'unixepoch')
            ORDER BY started_at DESC
        """

        cursor = await self.db.execute(query, (user_id, GUILD_ID, cutoff_date))
        rows = await cursor.fetchall()
        await cursor.close()

        if len(rows) < ACTIVITY_MIN_SESSIONS:
            return None

        # Uhrzeiten sammeln (jede Stunde einer Session zählt)
        hour_counts = {}
        total_minutes = 0

        for row in rows:
            try:
                # Parse datetime strings
                start_str = row["started_at"]
                end_str = row["ended_at"]

                # Simple parsing (assumes format: YYYY-MM-DD HH:MM:SS)
                start_hour = int(start_str[11:13]) if len(start_str) > 13 else 12
                end_hour = int(end_str[11:13]) if len(end_str) > 13 else 12

                # Zähle alle Stunden zwischen Start und Ende
                duration_minutes = row["duration_seconds"] // 60
                total_minutes += duration_minutes

                # Einfache Variante: Start-Stunde zählt
                hour_counts[start_hour] = hour_counts.get(start_hour, 0) + 1

            except Exception:
                continue

        if not hour_counts:
            return None

        # Top Uhrzeiten (mindestens 20% der max Häufigkeit)
        max_count = max(hour_counts.values())
        threshold = max_count * 0.2
        active_hours = sorted([h for h, c in hour_counts.items() if c >= threshold])

        # Ist jetzt eine typische Zeit?
        import datetime
        current_hour = datetime.datetime.now().hour
        is_typical_time = current_hour in active_hours

        return {
            'active_hours': active_hours,
            'total_sessions': len(rows),
            'total_minutes': total_minutes,
            'avg_session_length': total_minutes // len(rows) if rows else 0,
            'is_currently_typical_time': is_typical_time
        }

    async def _find_coplay_partners(
        self,
        user_id: int,
        min_rank: int,
        max_rank: int
    ) -> List[Tuple[int, int]]:
        """
        Findet User die oft mit dem angegebenen User zusammen spielen.

        Returns: [(other_user_id, co_play_count), ...]
        """
        await self._ensure_db()
        if not self.db:
            return []

        cutoff_date = time.time() - (ACTIVITY_ANALYSIS_DAYS * 24 * 3600)

        # Hole alle Sessions des Users
        query = """
            SELECT channel_id, started_at, ended_at
            FROM voice_session_log
            WHERE user_id = ?
            AND guild_id = ?
            AND started_at >= datetime(?, 'unixepoch')
        """

        cursor = await self.db.execute(query, (user_id, GUILD_ID, cutoff_date))
        user_sessions = await cursor.fetchall()
        await cursor.close()

        if not user_sessions:
            return []

        # Finde überlappende Sessions von anderen Usern
        coplay_counts = {}

        for session in user_sessions:
            channel_id = session["channel_id"]
            start = session["started_at"]
            end = session["ended_at"]

            # Finde andere User die zur gleichen Zeit im gleichen Channel waren
            overlap_query = """
                SELECT DISTINCT user_id
                FROM voice_session_log
                WHERE channel_id = ?
                AND user_id != ?
                AND guild_id = ?
                AND (
                    (started_at <= ? AND ended_at >= ?)
                    OR (started_at >= ? AND started_at <= ?)
                )
            """

            cursor = await self.db.execute(
                overlap_query,
                (channel_id, user_id, GUILD_ID, start, start, start, end)
            )
            coplay_users = await cursor.fetchall()
            await cursor.close()

            for row in coplay_users:
                other_id = row["user_id"]
                coplay_counts[other_id] = coplay_counts.get(other_id, 0) + 1

        # Filtere nach Rank
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return []

        filtered_partners = []
        for other_id, count in coplay_counts.items():
            member = guild.get_member(other_id)
            if not member or member.bot:
                continue

            rank_name, rank_value = self._get_user_rank_from_roles(member)
            if min_rank <= rank_value <= max_rank and rank_value > 0:
                filtered_partners.append((other_id, count))

        # Sortiere nach Häufigkeit
        filtered_partners.sort(key=lambda x: x[1], reverse=True)

        return filtered_partners[:5]  # Top 5 Co-Player

    async def _get_suggested_offline_users(
        self,
        author_id: int,
        author_rank_value: int
    ) -> List[Tuple[int, str, int, Dict[str, any]]]:
        """
        Schlägt OFFLINE User vor die:
        1. Normalerweise zu dieser Uhrzeit online sind
        2. Im Rank-Bereich sind
        3. Oft mit dem Author oder ähnlichen Leuten spielen

        Returns: [(discord_id, rank_name, rank_value, activity_pattern), ...]
        """
        steam_links = await self._get_all_steam_links()
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return []

        suggestions = []
        min_rank = max(1, author_rank_value - RANK_TOLERANCE)
        max_rank = min(11, author_rank_value + RANK_TOLERANCE)

        # Finde Co-Play Partners des Authors
        coplay_partners = await self._find_coplay_partners(author_id, min_rank, max_rank)
        coplay_ids = {uid for uid, _ in coplay_partners}

        # Alle online Steam-User (um sie zu excluden)
        all_steam_ids = {sid for sids in steam_links.values() for sid in sids}
        online_users = await self._get_online_steam_users(all_steam_ids)
        online_discord_ids = {
            discord_id
            for discord_id, steam_ids in steam_links.items()
            for sid in steam_ids
            if sid in online_users
        }

        # Analysiere Offline-User
        for discord_id in steam_links.keys():
            # Skip Author, Online-User und Bots
            if discord_id == author_id or discord_id in online_discord_ids:
                continue

            member = guild.get_member(discord_id)
            if not member or member.bot:
                continue

            # Rank check
            rank_name, rank_value = self._get_user_rank_from_roles(member)
            if not (min_rank <= rank_value <= max_rank and rank_value > 0):
                continue

            # Aktivitäts-Muster analysieren
            activity_pattern = await self._analyze_user_activity_patterns(discord_id)
            if not activity_pattern:
                continue

            # Nur vorschlagen wenn es eine typische Zeit ist
            if not activity_pattern['is_currently_typical_time']:
                continue

            # Bonus wenn Co-Play Partner
            activity_pattern['is_coplay_partner'] = discord_id in coplay_ids
            activity_pattern['coplay_count'] = next(
                (count for uid, count in coplay_partners if uid == discord_id),
                0
            )

            suggestions.append((discord_id, rank_name, rank_value, activity_pattern))

        # Sortiere: Co-Play Partner zuerst, dann nach Aktivität
        suggestions.sort(
            key=lambda x: (x[3]['is_coplay_partner'], x[3]['coplay_count'], x[3]['total_sessions']),
            reverse=True
        )

        return suggestions[:3]  # Max 3 Vorschläge

    async def _get_voice_channel_suggestions(
        self,
        author: discord.Member,
        author_rank_value: int
    ) -> List[Tuple[discord.VoiceChannel, str, List[Tuple[str, int]], float]]:
        """
        Findet passende Voice Channels basierend auf Rang.
        Returns: [(channel, category_type, [(member_name, rank)], avg_rank_diff), ...]
        """
        if author.voice and author.voice.channel:
            # User ist bereits in einem Voice Channel
            return []

        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return []

        suggestions = []

        for category_id, category_type in VOICE_CATEGORIES.items():
            category = guild.get_channel(category_id)
            if not category or not isinstance(category, discord.CategoryChannel):
                continue

            for channel in category.voice_channels:
                # Channel muss Mitglieder haben
                if not channel.members:
                    continue

                # Mitglieder und deren Ränge sammeln
                member_ranks = []
                rank_values = []

                for member in channel.members:
                    if member.bot:
                        continue

                    rank_name, rank_value = self._get_user_rank_from_roles(member)
                    if rank_value > 0:
                        member_ranks.append((member.display_name, rank_value))
                        rank_values.append(rank_value)

                if not rank_values:
                    continue

                # Durchschnitts-Rang berechnen
                avg_rank = sum(rank_values) / len(rank_values)
                rank_diff = abs(avg_rank - author_rank_value)

                # Ranked: ±1, Grind: ±2, Casual: ±4
                tolerance = {
                    "ranked": 1.5,
                    "grind": 2.5,
                    "casual": 4.5
                }.get(category_type, 3.0)

                # Nur Channels im Toleranzbereich vorschlagen
                if rank_diff <= tolerance:
                    suggestions.append((channel, category_type, member_ranks, rank_diff))

        # Sortiere nach Rank-Diff (beste Matches zuerst)
        suggestions.sort(key=lambda x: x[3])

        return suggestions[:3]  # Max 3 Vorschläge

    async def _generate_ai_voice_suggestion(
        self,
        author_name: str,
        author_rank: str,
        original_message: str,
        suggestions: List[Tuple[discord.VoiceChannel, str, List[Tuple[str, int]], float]]
    ) -> Optional[str]:
        """
        Generiert eine freundliche, menschliche Antwort für Voice Channel Vorschläge.
        Analysiert den Stil der originalen Nachricht und antwortet auf gleicher Augenhöhe.
        """
        if not ANTHROPIC_API_KEY or not suggestions:
            return None

        try:
            # Context für AI vorbereiten
            context_lines = []
            for channel, cat_type, members, rank_diff in suggestions:
                member_count = len(members)
                cat_emoji = {"ranked": "🏆", "grind": "💪", "casual": "🎉"}.get(cat_type, "🎮")
                context_lines.append(
                    f"{cat_emoji} {channel.name} ({cat_type}) - {member_count} Spieler"
                )

            context = "\n".join(context_lines)

            async with aiohttp.ClientSession() as session:
                headers = {
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }

                # Stil-angepasstes Prompt
                payload = {
                    "model": "claude-3-haiku-20240307",
                    "max_tokens": 150,
                    "messages": [{
                        "role": "user",
                        "content": (
                            f"Analysiere den Schreibstil (Jugendsprache/Slang/Umgangssprache/Standard) und antworte "
                            f"im EXAKT gleichen Stil. Nutze die GLEICHEN Formulierungen, gleiche Abkürzungen, "
                            f"gleiche Wörter wenn möglich. Schreibe wie ein Kumpel auf Augenhöhe.\n\n"
                            f"WICHTIG: Niemals 'Sie', immer 'du'. Keine Emojis. Max 2 Sätze.\n\n"
                            f"Original: \"{original_message}\"\n\n"
                            f"Schlage diese Voice Channels vor:\n{context}"
                        )
                    }]
                }

                async with session.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status != 200:
                        return None

                    data = await resp.json()
                    return data.get("content", [{}])[0].get("text", "").strip()

        except Exception as exc:
            log.debug("AI-Response-Generation fehlgeschlagen: %s", exc)
            return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Überwacht LFG-Kanal nach LFG-Anfragen"""
        # Ignoriere Bot-Nachrichten
        if message.author.bot:
            return

        # Nur im konfigurierten Kanal reagieren
        if message.channel.id != LFG_CHANNEL_ID:
            return

        # Prüfe ob Nachricht LFG-Trigger enthält
        is_lfg = self._is_lfg_message(message.content)

        # Falls kein Keyword-Match: AI-basierte Erkennung
        if not is_lfg and USE_AI_DETECTION:
            is_lfg = await self._ai_check_lfg_intent(message.content)
            if is_lfg:
                log.info("AI erkannte LFG-Intent von %s: %s", message.author.display_name, message.content[:50])

        if not is_lfg:
            return

        # Rate limiting
        now = time.time()
        last_lfg = self.last_lfg_per_user.get(message.author.id, 0)
        if now - last_lfg < self.lfg_cooldown_seconds:
            remaining = int(self.lfg_cooldown_seconds - (now - last_lfg))
            await message.reply(
                f"⏳ Bitte warte noch {remaining} Sekunden bevor du erneut nach Spielern suchst.",
                delete_after=10
            )
            return

        self.last_lfg_per_user[message.author.id] = now

        try:
            await self._handle_lfg_request(message)
        except Exception as exc:
            log.exception("Fehler beim Verarbeiten der LFG-Anfrage: %s", exc)
            await message.reply(
                "❌ Fehler beim Suchen nach Spielern. Bitte versuche es später erneut.",
                delete_after=10
            )

    async def _handle_lfg_request(self, message: discord.Message) -> None:
        """Verarbeitet eine LFG-Anfrage"""
        # Rang des Autors ermitteln
        if not isinstance(message.author, discord.Member):
            return

        author_rank_name, author_rank_value = self._get_user_rank_from_roles(message.author)

        if author_rank_value == 0:
            await message.reply(
                "❌ Du hast noch keine Rang-Rolle! Bitte verknüpfe deinen Account und erhalte eine Rang-Rolle.",
                delete_after=15
            )
            return

        # Nachdenk-Nachricht schicken
        thinking_msg = await message.reply("🔍 Suche nach verfügbaren Spielern...")

        # Passende Spieler finden
        matching_players = await self._find_matching_players(
            author_rank_value,
            message.author.id
        )

        # Voice Channel Vorschläge holen (nur wenn User nicht in VC ist)
        voice_suggestions = await self._get_voice_channel_suggestions(
            message.author,
            author_rank_value
        )

        # Intelligente Offline-User Vorschläge (basierend auf Aktivitätsmustern)
        offline_suggestions = await self._get_suggested_offline_users(
            message.author.id,
            author_rank_value
        )

        # Antwort erstellen
        if not matching_players and not voice_suggestions and not offline_suggestions:
            await thinking_msg.edit(
                content=f"😔 Keine Spieler im Rang-Bereich **{author_rank_name} ±{RANK_TOLERANCE}** sind derzeit online in Deadlock und keine passenden Voice Channels gefunden."
            )
            return

        # Spieler nach Status gruppieren
        in_lobby = []
        in_match = []

        for discord_id, rank_name, rank_value, stage, minutes in matching_players:
            member = message.guild.get_member(discord_id)
            if not member:
                continue

            if stage == "lobby":
                in_lobby.append((member, rank_name, rank_value))
            elif stage == "match":
                in_match.append((member, rank_name, rank_value, minutes))

        # Embed erstellen
        embed_color = discord.Color.green() if matching_players else discord.Color.blue()
        embed = discord.Embed(
            title="🎮 Verfügbare Spieler gefunden!" if matching_players else "🎮 Voice Channel Vorschläge",
            description=f"Spieler im Rang-Bereich **{author_rank_name} ±{RANK_TOLERANCE}**" if matching_players else f"Passende Voice Channels für **{author_rank_name}**",
            color=embed_color
        )

        mentions = []

        if in_lobby:
            lobby_lines = []
            for member, rank_name, rank_value in sorted(in_lobby, key=lambda x: x[2], reverse=True):
                lobby_lines.append(f"{member.mention} - **{rank_name}**")
                mentions.append(member.mention)

            embed.add_field(
                name=f"🟢 In der Lobby ({len(in_lobby)})",
                value="\n".join(lobby_lines),
                inline=False
            )

        if in_match:
            match_lines = []
            for member, rank_name, rank_value, minutes in sorted(in_match, key=lambda x: x[2], reverse=True):
                time_str = f" (Min {minutes})" if minutes is not None else ""
                match_lines.append(f"{member.mention} - **{rank_name}**{time_str}")
                mentions.append(member.mention)

            embed.add_field(
                name=f"🎯 Im Match ({len(in_match)})",
                value="\n".join(match_lines),
                inline=False
            )

        # Voice Channel Vorschläge hinzufügen
        if voice_suggestions:
            # AI-generierte Nachricht für Voice Channels (im Stil der Original-Nachricht)
            ai_suggestion_text = await self._generate_ai_voice_suggestion(
                message.author.display_name,
                author_rank_name,
                message.content,  # Original-Nachricht für Stil-Analyse
                voice_suggestions
            )

            vc_lines = []
            for channel, cat_type, members, rank_diff in voice_suggestions:
                cat_emoji = {"ranked": "🏆", "grind": "💪", "casual": "🎉"}.get(cat_type, "🎮")
                member_count = len(members)

                # Warnung bei Casual Lanes
                warning = ""
                if cat_type == "casual" and rank_diff > 2:
                    warning = " ⚠️ (größere Rank-Diff)"

                # Klickbarer Voice Channel Link (Discord URL) - Discord zeigt das automatisch richtig an
                vc_url = f"https://discord.com/channels/{GUILD_ID}/{channel.id}"
                vc_lines.append(
                    f"{cat_emoji} [{channel.name}]({vc_url}) - {member_count} Spieler{warning}"
                )

            embed.add_field(
                name="🔊 Passende Voice Channels (klick zum Beitreten)",
                value="\n".join(vc_lines),
                inline=False
            )

            if ai_suggestion_text:
                embed.add_field(
                    name="💬 Tipp",
                    value=ai_suggestion_text,
                    inline=False
                )

        # Intelligente Offline-User Vorschläge (mit Vorsicht!)
        if offline_suggestions:
            offline_lines = []
            for discord_id, rank_name, rank_value, pattern in offline_suggestions:
                member = message.guild.get_member(discord_id)
                if not member:
                    continue

                # Info zusammenbauen
                parts = [member.mention, f"**{rank_name}**"]

                # Co-Play Partner Hinweis
                if pattern.get('is_coplay_partner'):
                    coplay_count = pattern.get('coplay_count', 0)
                    parts.append(f"🤝 {coplay_count}x zusammen gespielt")

                # Typische Zeit
                hours = pattern.get('active_hours', [])
                if hours:
                    hour_range = f"{min(hours)}-{max(hours)} Uhr"
                    parts.append(f"⏰ meist {hour_range}")

                offline_lines.append(" • ".join(parts))

            if offline_lines:
                embed.add_field(
                    name="💡 Könnte passen (offline, aber meist jetzt online)",
                    value="\n".join(offline_lines),
                    inline=False
                )

        embed.set_footer(text=f"Angefordert von {message.author.display_name}")
        embed.timestamp = message.created_at

        # Nachricht mit Pings
        response_parts = []
        if matching_players:
            mention_text = " ".join(mentions[:10])  # Max 10 Mentions um Discord-Limits zu beachten
            if len(mentions) > 10:
                mention_text += f"\n... und {len(mentions) - 10} weitere"
            response_parts.append(f"{message.author.mention} sucht Mitspieler!\n{mention_text}")
        else:
            response_parts.append(f"{message.author.mention}")

        await thinking_msg.edit(
            content="\n".join(response_parts),
            embed=embed
        )

        log.info(
            "LFG: %s (%s) -> %d Spieler gefunden (%d Lobby, %d Match)",
            message.author.display_name,
            author_rank_name,
            len(matching_players),
            len(in_lobby),
            len(in_match)
        )

    @commands.command(name="lfg")
    async def lfg_command(self, ctx: commands.Context) -> None:
        """Manueller LFG-Befehl (funktioniert auch außerhalb des LFG-Kanals)"""
        # Simuliere eine Nachricht im LFG-Kanal
        if ctx.channel.id != LFG_CHANNEL_ID:
            await ctx.send(
                f"💡 Bitte verwende diesen Befehl im <#{LFG_CHANNEL_ID}> Kanal oder schreibe dort einfach eine Nachricht mit 'LFG'.",
                delete_after=10
            )
            return

        # Nutze die normale on_message Logik
        await self._handle_lfg_request(ctx.message)

    @commands.command(name="lfgstatus")
    @commands.has_permissions(manage_guild=True)
    async def lfg_status(self, ctx: commands.Context) -> None:
        """Zeigt den Status des LFG-Systems"""
        embed = discord.Embed(
            title="📊 LFG System Status",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="⚙️ Konfiguration",
            value=(
                f"Kanal: <#{LFG_CHANNEL_ID}>\n"
                f"Rang-Toleranz: ±{RANK_TOLERANCE}\n"
                f"Cooldown: {self.lfg_cooldown_seconds}s\n"
                f"Max Presence-Alter: {PRESENCE_STALE_SECONDS}s"
            ),
            inline=False
        )

        # Steam-Links zählen
        steam_links = await self._get_all_steam_links()
        all_steam_ids = {sid for sids in steam_links.values() for sid in sids}
        online_users = await self._get_online_steam_users(all_steam_ids)

        embed.add_field(
            name="📈 Statistiken",
            value=(
                f"Verknüpfte Accounts: {len(steam_links)}\n"
                f"Steam-Accounts: {len(all_steam_ids)}\n"
                f"Derzeit online: {len(online_users)}"
            ),
            inline=False
        )

        # Online-Spieler nach Rank gruppieren
        if online_users:
            guild = self.bot.get_guild(GUILD_ID)
            rank_distribution: Dict[str, int] = {}

            for discord_id, steam_ids in steam_links.items():
                for sid in steam_ids:
                    if sid in online_users:
                        member = guild.get_member(discord_id) if guild else None
                        if member and not member.bot:
                            rank_name, _ = self._get_user_rank_from_roles(member)
                            rank_distribution[rank_name] = rank_distribution.get(rank_name, 0) + 1
                        break

            if rank_distribution:
                rank_lines = [f"**{rn}**: {count}" for rn, count in sorted(rank_distribution.items())]
                embed.add_field(
                    name="🎭 Online nach Rang",
                    value="\n".join(rank_lines),
                    inline=False
                )

        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SteamRankChecker(bot))
    log.info("SteamRankChecker cog hinzugefügt")
