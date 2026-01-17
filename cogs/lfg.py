"""
Steam Rank Checker - LFG System
Überwacht einen Discord-Textkanal und pingt Spieler basierend auf Rank und Steam-Online-Status
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Set, Tuple

import aiosqlite
import discord
from discord.ext import commands

from service.db import db_path

log = logging.getLogger("SteamRankChecker")

# Kanal-ID aus der Discord-URL: https://discord.com/channels/1289721245281292288/1374364800817303632
LFG_CHANNEL_ID = 1374364800817303632
GUILD_ID = 1289721245281292288

# Steam presence freshness (wie lange die Daten maximal alt sein dürfen)
PRESENCE_STALE_SECONDS = 300  # 5 Minuten

# Rank-Matching: wie viele Ränge Unterschied sind erlaubt?
RANK_TOLERANCE = 2  # +/- 2 Ränge (Grind-Modus)

# Voice Channel Kategorien (aus deadlock_voice_status.py und rank_voice_manager.py)
VOICE_CATEGORIES = {
    1412804540994162789: "grind",   # Grind Kategorie
    1289721245281292290: "casual",  # Casual/Spaß Kategorie
}
CASUAL_CATEGORY_ID = 1289721245281292290
VOICE_CATEGORY_TOLERANCE = {
    "grind": 2.5,
    "casual": 4.5,
}

# AI Configuration (Gemini)
GEMINI_MODEL = "gemini-3-pro-preview"
GEMINI_VOICE_MODEL = GEMINI_MODEL
USE_AI_DETECTION = True

# Zusatz-Rolle für neue Spieler ("Unbekannt")
UNKNOWN_ROLE_ID = 1397687886580547745  # feste Rolle für unbekannten Rang
UNKNOWN_RANK_NAME = "Unbekannt"
UNKNOWN_RANK_NAME_LOWER = UNKNOWN_RANK_NAME.lower()
# Bis zu welchem Rang sollen Unbekannte gematcht werden (Default: Emissary = 6)
UNKNOWN_MAX_MATCH_RANK = 6

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
    1397687886580547745: ("Unbekannt", 0),  # Standard-Rolle für Spieler ohne Rang
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

    async def _generate_gemini_text(
        self,
        *,
        model: str,
        contents: str,
        max_output_tokens: int,
        temperature: float,
    ) -> Optional[str]:
        """Routed Gemini-Aufruf über den zentralen AIConnector."""
        ai = getattr(self.bot, "get_cog", lambda name: None)("AIConnector")
        if not ai:
            return None
        text, _meta = await ai.generate_text(
            provider="gemini",
            prompt=contents,
            system_prompt=None,
            model=model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )
        return text

    def _get_user_rank_from_roles(self, member: discord.Member) -> Tuple[str, int]:
        """Ermittelt den höchsten Rang eines Users basierend auf Discord-Rollen"""
        highest_rank: Optional[Tuple[str, int]] = None

        for role in member.roles:
            if role.id in DISCORD_RANK_ROLES:
                rank_name, rank_value = DISCORD_RANK_ROLES[role.id]
                if highest_rank is None or rank_value > highest_rank[1]:
                    highest_rank = (rank_name, rank_value)

        return highest_rank or ("Obscurus", 0)

    async def _ai_check_lfg_intent(self, message_content: str) -> bool:
        """Nutzt nur AI um zu prüfen, ob jemand nach Mitspielern sucht."""
        if not USE_AI_DETECTION:
            log.debug("AI-Detection deaktiviert - kein LFG erkannt")
            return False

        ai = getattr(self.bot, "get_cog", lambda name: None)("AIConnector")
        if not ai:
            log.warning("AIConnector nicht geladen - AI-Only Modus, daher kein LFG erkannt")
            return False

        prompt = (
            "Antwort strikt nur mit 'ja' oder 'nein'. "
            "Sage 'ja' nur, wenn die Nachricht eindeutig Mitspieler oder ein Spiel JETZT sucht "
            "(LFG/LF Game, will spielen, suche Team usw.). "
            "Sage 'nein' bei Smalltalk, Witzen, Diskussionen, Meinungen, Fragen nach News/Leaks "
            "oder allem, was nicht klar eine Spielersuche ist. "
            "Im Zweifel immer 'nein'.\n\n"
            f"Nachricht: \"{message_content}\""
        )
        try:
            answer_text, _meta = await ai.generate_text(
                provider="gemini",
                prompt=prompt,
                system_prompt=None,
                model=GEMINI_MODEL,
                max_output_tokens=8,
                temperature=0,
            )
        except Exception as exc:
            log.warning("AI Intent-Check fehlgeschlagen (%s) - kein LFG erkannt", exc)
            return False

        if not answer_text:
            log.warning("AI gab keine Antwort zurück - kein LFG erkannt")
            return False

        normalized = str(answer_text).strip().lower()
        if normalized.startswith("ja") or normalized.startswith("yes"):
            log.debug("AI intent Antwort: %s -> %s", normalized, True)
            return True
        if normalized.startswith("nein") or normalized.startswith("no"):
            log.debug("AI intent Antwort: %s -> %s", normalized, False)
            return False

        log.debug("AI intent unklar (%s) -> kein LFG erkannt", normalized)
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
        author_id: int,
        *,
        min_rank: Optional[int] = None,
        max_rank: Optional[int] = None,
        include_unknown_until: Optional[int] = None
    ) -> List[Tuple[int, str, int, str, Optional[int]]]:
        """
        Findet Discord-User die:
        1. Steam-Account verknüpft haben
        2. Online in Deadlock sind (Lobby oder Match)
        3. Rang im Toleranzbereich haben

        Returns: [(discord_user_id, rank_name, rank_value, stage, minutes), ...]
        """
        if min_rank is None or max_rank is None:
            min_rank = max(1, author_rank_value - RANK_TOLERANCE)
            max_rank = min(11, author_rank_value + RANK_TOLERANCE)
        else:
            min_rank = max(1, min_rank)
            max_rank = min(11, max_rank)

        allow_unknown_matches = (
            include_unknown_until is not None
            and author_rank_value <= include_unknown_until
        )

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

            # Wer bereits in einem Voice Channel ist, soll nicht angepingt werden
            if member.voice and member.voice.channel:
                continue

            rank_name, rank_value = self._get_user_rank_from_roles(member)
            is_unknown_rank = rank_value == 0 and rank_name.lower() == UNKNOWN_RANK_NAME_LOWER

            # Rang-Matching
            if allow_unknown_matches and is_unknown_rank:
                matching_players.append((discord_id, rank_name, rank_value, user_stage, user_minutes))
            elif min_rank <= rank_value <= max_rank:
                matching_players.append((discord_id, rank_name, rank_value, user_stage, user_minutes))

        return matching_players

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

            is_casual_category = category_type == "casual" or category_id == CASUAL_CATEGORY_ID

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
                tolerance = VOICE_CATEGORY_TOLERANCE.get(category_type, 3.0)
                within_tolerance = rank_diff <= tolerance
                allow_high_gap_casual = is_casual_category and not within_tolerance  # Casual immer anzeigen, Warnung später

                # Nur Channels im Toleranzbereich vorschlagen
                if within_tolerance or allow_high_gap_casual:
                    suggestions.append((channel, category_type, member_ranks, rank_diff))

        # Sortiere nach Rank-Diff (beste Matches zuerst)
        suggestions.sort(key=lambda x: x[3])

        return suggestions[:3]  # Max 3 Vorschläge

    async def _generate_ai_voice_suggestion(
        self,
        author_name: str,
        author_rank: str,
        original_message: str,
        suggestions: List[Tuple[discord.VoiceChannel, str, List[Tuple[str, int]], float]],
        *,
        lobby_count: int,
        match_count: int,
        best_vc_url: Optional[str] = None,
    ) -> Optional[str]:
        """
        Generiert eine kurze, personalisierte Antwort (ähnlich AI-Onboarding)
        und spiegelt den Stil der Original-Nachricht.
        """
        tolerance_map = VOICE_CATEGORY_TOLERANCE

        voice_lines = []
        has_high_gap_casual = False
        for channel, cat_type, members, rank_diff in suggestions:
            tolerance = tolerance_map.get(cat_type, 3.0)
            if cat_type == "casual" and rank_diff > tolerance:
                has_high_gap_casual = True

            member_names = ", ".join(name for name, _ in members[:3]) or "Spieler"
            extra = len(members) - 3
            if extra > 0:
                member_names += f" +{extra}"

            fit = "nah dran" if rank_diff <= tolerance else f"weiter weg (Δ~{rank_diff:.1f})"
            cat_label = {"ranked": "Ranked", "grind": "Grind", "casual": "Casual"}.get(cat_type, cat_type)
            voice_lines.append(f"- {channel.name} ({cat_label}, {member_names}, {fit})")

        voice_context = "\n".join(voice_lines) if voice_lines else "Keine Voice Channels offen."
        counts_line = f"{lobby_count} in Lobby, {match_count} im Match"

        prompt = (
            "Du bist der LFG-Buddy der Deutschen Deadlock Community. Antworte immer auf Deutsch und wie ein Kumpel.\n"
            "Passe Tonfall an den Stil der Original-Nachricht an (Slang/kurz/Emojis nur wenn der User so schreibt).\n"
            "Schreibe 2-4 Sätze, keine Listen, kein Markdown, maximal 1 Emoji nur wenn es passt.\n"
            "Sag kurz, was du gefunden hast, empfiehl den besten Voice (Link wenn vorhanden) oder nenne die beste Option. "
            "Kling locker und einladend, kein Bot-Ton.\n"
            f"User: {author_name} (Rank: {author_rank})\n"
            f"Original: \"{original_message}\"\n"
            f"Gefundene Spieler: {counts_line}\n"
            f"Voice-Optionen:\n{voice_context}\n"
        )

        if has_high_gap_casual:
            prompt += "Weise freundlich darauf hin, dass Casual evtl. ein höheres Skill-Gap hat und rough sein kann, aber er willkommen ist.\n"
        else:
            prompt += "Kein Warnhinweis nötig.\n"

        if best_vc_url:
            prompt += f"Direkt-Link zum besten Voice: {best_vc_url}\n"

        suggestion = await self._generate_gemini_text(
            model=GEMINI_VOICE_MODEL,
            contents=prompt,
            max_output_tokens=220,
            temperature=0.4,
        )

        return suggestion or None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Überwacht LFG-Kanal nach LFG-Anfragen"""
        # Ignoriere Bot-Nachrichten
        if message.author.bot:
            return

        # Nur im konfigurierten Kanal reagieren
        if message.channel.id != LFG_CHANNEL_ID:
            return

        content_preview = message.content.replace("\n", " ")[:180]
        log.debug(
            "on_message LFG channel=%s author=%s content='%s'",
            message.channel.id,
            message.author.id,
            content_preview,
        )

        # AI entscheidet bei jeder Nachricht, ob es LFG ist (keine Keyword-Liste mehr)
        is_lfg = await self._ai_check_lfg_intent(message.content)
        log.debug("AI intent result for %s: %s", message.author.id, is_lfg)

        if not is_lfg:
            log.debug("Nachricht nicht als LFG erkannt (author=%s)", message.author.id)
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
            log.debug("Starte LFG-Handling für author=%s", message.author.id)
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
        is_unknown_rank = author_rank_value == 0 and author_rank_name.lower() == UNKNOWN_RANK_NAME_LOWER

        if author_rank_value == 0 and not is_unknown_rank:
            await message.reply(
                "❌ Du hast noch keine Rang-Rolle! Bitte verknüpfe deinen Account und erhalte eine Rang-Rolle. "
                f"Falls du neu bist, nutze die Rolle \"{UNKNOWN_RANK_NAME}\" für LFG.",
                delete_after=15
            )
            return

        # Suchbereich bestimmen
        search_min_rank = max(1, author_rank_value - RANK_TOLERANCE)
        search_max_rank = min(11, author_rank_value + RANK_TOLERANCE)
        if is_unknown_rank:
            search_min_rank = 1
            search_max_rank = UNKNOWN_MAX_MATCH_RANK
        rank_range_desc = f"{author_rank_name} ±{RANK_TOLERANCE}"
        if is_unknown_rank:
            rank_range_desc = f"{author_rank_name} bis Emissary"

        # Nachdenk-Nachricht schicken
        thinking_msg = await message.reply("🔍 Suche nach verfügbaren Spielern...")

        # Passende Spieler finden
        matching_players = await self._find_matching_players(
            author_rank_value,
            message.author.id,
            min_rank=search_min_rank,
            max_rank=search_max_rank,
            include_unknown_until=UNKNOWN_MAX_MATCH_RANK
        )

        # Voice Channel Vorschläge holen (nur wenn User nicht in VC ist)
        voice_suggestions = await self._get_voice_channel_suggestions(
            message.author,
            author_rank_value
        )

        # Antwort erstellen
        if not matching_players and not voice_suggestions:
            log.info(
                "Keine Spieler/VCs gefunden für %s (range=%s)",
                message.author.id,
                rank_range_desc,
            )
            await thinking_msg.edit(
                content=f"😔 Keine Spieler im Rang-Bereich **{rank_range_desc}** sind derzeit online in Deadlock und keine passenden Voice Channels gefunden."
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
            description=f"Spieler im Rang-Bereich **{rank_range_desc}**" if matching_players else f"Passende Voice Channels für **{author_rank_name}**",
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
            vc_lines = []
            for channel, cat_type, members, rank_diff in voice_suggestions:
                cat_emoji = {"ranked": "🏆", "grind": "💪", "casual": "🎉"}.get(cat_type, "🎮")
                member_count = len(members)
                tolerance = VOICE_CATEGORY_TOLERANCE.get(cat_type, 3.0)

                # Warnung bei Casual Lanes
                warning = ""
                if cat_type == "casual" and rank_diff > tolerance:
                    warning = " ⚠️ (hohes Skill-Gap, kann rough sein, bist trotzdem willkommen)"

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

        best_vc_url = None
        if voice_suggestions:
            best_vc_url = f"https://discord.com/channels/{GUILD_ID}/{voice_suggestions[0][0].id}"

        ai_suggestion_text = await self._generate_ai_voice_suggestion(
            message.author.display_name,
            author_rank_name,
            message.content,  # Original-Nachricht für Stil-Analyse
            voice_suggestions,
            lobby_count=len(in_lobby),
            match_count=len(in_match),
            best_vc_url=best_vc_url,
        )

        if ai_suggestion_text:
            ai_block = ai_suggestion_text
            if best_vc_url:
                ai_block += f"\n🔊 Direkt joinen: {best_vc_url}"
            response_parts.append(ai_block)

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
