"""
Smart LFG Agent - Deadlock LFG System
Analysiert Anfragen mit KI und routet Spieler basierend auf Skill, Modus und Verfügbarkeit.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

import discord
from discord.ext import commands

from service import db

log = logging.getLogger("SmartLFG")

# --- Konfiguration ---

# LFG Eingangskanal (User schreibt hier)
LFG_CHANNEL_ID = 1376335502919335936

# Output-Kanal für Bot-Antworten
OUTPUT_CHANNEL_ID = 1374364800817303632

GUILD_ID = 1289721245281292288

# AI Config (ChatGPT/OpenAI)
OPENAI_MODEL = "gpt-5.3"
NO_LFG_TOKEN = "NO_LFG"
LFG_INTENT_MAX_TOKENS = 8
USE_AI_LFG_DETECTION = True

# Steam presence freshness (wie lange die Daten maximal alt sein dürfen)
PRESENCE_STALE_SECONDS = 120  # 2 Minuten (reduziert für genauere Erkennung)

# Rank-Matching: wie viele Ränge Unterschied sind erlaubt?
RANK_TOLERANCE = 2  # +/- 2 Ränge
MAX_MENTION_PINGS = 10
ACTIVITY_LOOKBACK_DAYS = 14
MIN_ACTIVITY_SCORE_SESSIONS = 3
MIN_TIME_MATCH_SCORE = 0.5

# Scoring Weights (Rank ist bewusst dominant)
WEIGHT_RANK = 70
WEIGHT_TIME = 10
WEIGHT_LANE = 7
WEIGHT_COPLAY = 7
WEIGHT_PRESENCE = 4
WEIGHT_ACTIVITY = 2

# Spezielle Channel / Kategorien
NEW_PLAYER_LANE_ID = 1465839460485697556
STREET_BRAWL_LANE_ID = 1357422958544420944
CASUAL_CATEGORY_ID = 1289721245281292290
RANKED_CATEGORY_ID = 1412804540994162789  # "Grind" Category

# Rollen & Ranks
UNKNOWN_ROLE_ID = 1397687886580547745
UNKNOWN_RANK_NAME = "Unbekannt"
UNKNOWN_RANK_NAME_LOWER = UNKNOWN_RANK_NAME.lower()
# Bis zu welchem Rang sollen Unbekannte gematcht werden (Default: Emissary = 6)
UNKNOWN_MAX_MATCH_RANK = 6

# Rank Definitionen
# 1-5: New Player Friendly
# 6-7: Mid Elo
# 8+: High Elo
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
    1397687886580547745: ("Unbekannt", 0),
}


class SmartLFGAgent(commands.Cog):
    """
    KI-gesteuerter LFG Bot, der Nutzer basierend auf Rang und Anfrage intelligent zuweist.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.lfg_cooldowns: Dict[int, float] = {}
        self.cooldown_seconds = 60  # Kurzer Cooldown gegen Spam

    async def cog_load(self) -> None:
        log.info(
            "SmartLFGAgent geladen - LFG Channel: %s | Output Channel: %s",
            LFG_CHANNEL_ID,
            OUTPUT_CHANNEL_ID,
        )

    async def cog_unload(self) -> None:
        log.info("SmartLFGAgent entladen")

    def _get_user_rank(self, member: discord.Member) -> Tuple[str, int]:
        """Ermittelt den höchsten Rang eines Users."""
        highest = ("Unbekannt", 0)
        for role in member.roles:
            if role.id in DISCORD_RANK_ROLES:
                r_name, r_val = DISCORD_RANK_ROLES[role.id]
                if r_val > highest[1]:
                    highest = (r_name, r_val)

        # Fallback: Wenn User 'Unbekannt' Rolle explizit hat oder gar keine Rank Rolle
        if highest[1] == 0:
            return (UNKNOWN_RANK_NAME, 0)
        return highest

    def _keyword_lfg_intent(self, message_content: str) -> bool:
        """Fallback-Heuristik für LFG-Erkennung."""
        text = (message_content or "").lower()
        if not text:
            return False

        if "lfg" in text or "lfm" in text:
            return True

        if ("suche" in text or "suchen" in text or "gesucht" in text) and (
            "mitspieler" in text
            or "team" in text
            or "gruppe" in text
            or "party" in text
        ):
            return True

        if ("spielen" in text or "zocken" in text or "grinden" in text) and (
            "wer" in text or "jemand" in text or "bock" in text
        ):
            return True

        if "duo" in text or "trio" in text or "squad" in text or "stack" in text:
            return True

        return False

    async def _ai_check_lfg_intent(self, message_content: str) -> bool:
        """AI-Check ob die Nachricht wirklich LFG ist (strikt)."""
        if not message_content or not message_content.strip():
            return False
        if self._keyword_lfg_intent(message_content):
            return True

        if not USE_AI_LFG_DETECTION:
            return False

        ai = getattr(self.bot, "get_cog", lambda name: None)("AIConnector")
        if not ai:
            log.warning("AIConnector nicht geladen - LFG-Detection deaktiviert")
            return False

        prompt = (
            "Antworte strikt nur mit 'ja' oder 'nein'. "
            "Sage 'ja' nur, wenn die Nachricht eindeutig Mitspieler für Deadlock JETZT/zeitnah sucht "
            "(LFG/LFM, 'suche Leute', 'wer bock', 'jemand Lust zu zocken', duo/trio/stack). "
            "Sage 'nein' bei Smalltalk, Diskussionen, Memes, News/Leaks, Meinungen oder allem, "
            "was keine klare Spielersuche ist. Im Zweifel immer 'nein'.\n\n"
            f'Nachricht: "{message_content}"'
        )
        try:
            answer_text, _meta = await ai.generate_text(
                provider="openai",
                prompt=prompt,
                system_prompt=None,
                model=OPENAI_MODEL,
                max_output_tokens=LFG_INTENT_MAX_TOKENS,
                temperature=0,
            )
        except Exception as exc:
            log.warning("AI Intent-Check fehlgeschlagen (%s) - Fallback Keywords", exc)
            return self._keyword_lfg_intent(message_content)

        if not answer_text:
            log.warning("AI gab keine Antwort zurück - Fallback auf Keywords")
            return self._keyword_lfg_intent(message_content)

        normalized = str(answer_text).strip().lower()
        if normalized.startswith("ja") or normalized.startswith("yes"):
            return True
        if normalized.startswith("nein") or normalized.startswith("no"):
            return False

        return False

    async def _get_all_steam_links(self) -> Dict[int, List[str]]:
        """
        Holt alle Discord User -> Steam ID Mappings.
        Returns: {discord_user_id: [steam_id1, steam_id2, ...]}
        """
        query = """
            SELECT user_id, steam_id
            FROM steam_links
            WHERE steam_id IS NOT NULL AND steam_id != ''
            AND verified = 1
            ORDER BY primary_account DESC, updated_at DESC
        """
        rows = await db.query_all_async(query)

        mapping: Dict[int, List[str]] = {}
        for row in rows:
            uid = int(row["user_id"])
            sid = str(row["steam_id"])
            mapping.setdefault(uid, []).append(sid)

        return mapping

    async def _get_online_steam_users(
        self, steam_ids: Set[str]
    ) -> Dict[str, Tuple[str, Optional[int]]]:
        """
        Filtert Steam-IDs nach Online-Status (in Deadlock).
        Returns: {steam_id: (stage, minutes)}
        stage: 'lobby' oder 'match'
        minutes: Spielminuten bei 'match', None bei 'lobby'
        """
        if not steam_ids:
            return {}

        now = int(time.time())
        steam_ids_json = json.dumps(sorted(steam_ids))
        rows = await db.query_all_async(
            """
            SELECT steam_id, deadlock_stage, deadlock_minutes, deadlock_updated_at, last_seen_ts
            FROM live_player_state
            WHERE steam_id IN (SELECT value FROM json_each(?))
            AND (in_deadlock_now = 1 OR deadlock_stage IS NOT NULL)
            """,
            (steam_ids_json,),
        )

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

    def _chunked(self, items: Iterable[int], size: int = 400) -> Iterable[List[int]]:
        chunk: List[int] = []
        for item in items:
            chunk.append(int(item))
            if len(chunk) >= size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk

    def _parse_json_list(self, raw: Optional[str]) -> List[int]:
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [
                    int(x) for x in parsed if str(x).isdigit() or isinstance(x, int)
                ]
        except Exception:
            return []
        return []

    def _rank_score(self, diff: int) -> float:
        if diff <= 0:
            return 1.0
        if diff == 1:
            return 0.7
        if diff == 2:
            return 0.4
        return 0.0

    def _time_match_score(
        self, typical_hours: List[int], typical_days: List[int], now: datetime
    ) -> float:
        if not typical_hours and not typical_days:
            return 0.0

        hour_match = False
        for typ_hour in typical_hours:
            diff = abs(now.hour - int(typ_hour))
            if diff <= 2 or diff >= 22:
                hour_match = True
                break

        day_match = not typical_days or now.weekday() in typical_days

        if hour_match and day_match:
            return 1.0
        if hour_match:
            return 0.7
        if day_match:
            return 0.4
        return 0.0

    def _get_target_lane_channel_ids(
        self,
        guild: Optional[discord.Guild],
        content_lower: str,
        author_rank_value: int,
    ) -> List[int]:
        if not guild:
            return []

        if "street brawl" in content_lower:
            return [STREET_BRAWL_LANE_ID]

        if "ranked" in content_lower or "grind" in content_lower:
            ranked_cat = guild.get_channel(RANKED_CATEGORY_ID)
            if isinstance(ranked_cat, discord.CategoryChannel):
                return [vc.id for vc in ranked_cat.voice_channels]
            return []

        if author_rank_value <= 5:
            return [NEW_PLAYER_LANE_ID]

        casual_cat = guild.get_channel(CASUAL_CATEGORY_ID)
        if isinstance(casual_cat, discord.CategoryChannel):
            return [vc.id for vc in casual_cat.voice_channels]

        return []

    async def _fetch_activity_patterns(
        self,
        user_ids: List[int],
    ) -> Dict[int, Tuple[List[int], List[int], int]]:
        if not user_ids:
            return {}

        patterns: Dict[int, Tuple[List[int], List[int], int]] = {}
        for chunk in self._chunked(user_ids):
            chunk_json = json.dumps([int(uid) for uid in chunk])
            rows = await db.query_all_async(
                """
                SELECT user_id, typical_hours, typical_days, activity_score_2w
                FROM user_activity_patterns
                WHERE user_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
                """,
                (chunk_json,),
            )
            for row in rows:
                uid = int(row[0])
                hours = self._parse_json_list(row[1])
                days = self._parse_json_list(row[2])
                score = int(row[3] or 0)
                patterns[uid] = (hours, days, score)
        return patterns

    async def _fetch_co_player_stats(self, user_id: int) -> Dict[int, Tuple[int, int]]:
        rows = await db.query_all_async(
            """
            SELECT co_player_id, sessions_together, total_minutes_together
            FROM user_co_players
            WHERE user_id = ?
            """,
            (user_id,),
        )
        stats: Dict[int, Tuple[int, int]] = {}
        for row in rows:
            stats[int(row[0])] = (int(row[1] or 0), int(row[2] or 0))
        return stats

    async def _fetch_lane_activity_users(
        self,
        user_ids: List[int],
        channel_ids: List[int],
        cutoff_str: str,
    ) -> Set[int]:
        if not user_ids or not channel_ids:
            return set()

        result: Set[int] = set()
        channel_ids_json = json.dumps([int(cid) for cid in channel_ids])
        for chunk in self._chunked(user_ids):
            chunk_json = json.dumps([int(uid) for uid in chunk])
            rows = await db.query_all_async(
                """
                SELECT DISTINCT user_id
                FROM voice_session_log
                WHERE started_at >= ?
                  AND user_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
                  AND channel_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
                """,
                (cutoff_str, chunk_json, channel_ids_json),
            )
            for row in rows:
                result.add(int(row[0]))
        return result

    def _infer_target_rank_from_coplayers(
        self,
        guild: Optional[discord.Guild],
        co_player_ids: List[int],
    ) -> Optional[int]:
        if not guild or not co_player_ids:
            return None
        ranks: List[int] = []
        for co_id in co_player_ids:
            member = guild.get_member(co_id)
            if not member:
                continue
            _, r_val = self._get_user_rank(member)
            if r_val > 0:
                ranks.append(r_val)
        if not ranks:
            return None
        avg = sum(ranks) / len(ranks)
        return int(round(avg))

    async def _find_matching_players(
        self,
        author: discord.Member,
        message_content: str,
        author_rank_value: int,
    ) -> List[Dict[str, object]]:
        guild = author.guild
        if not guild:
            return []

        steam_links = await self._get_all_steam_links()
        if not steam_links:
            return []

        co_player_stats = await self._fetch_co_player_stats(author.id)

        target_rank = author_rank_value
        rank_strict = True
        if target_rank == 0:
            inferred = self._infer_target_rank_from_coplayers(
                guild, list(co_player_stats.keys())
            )
            if inferred:
                target_rank = inferred
            else:
                rank_strict = False

        candidate_ids = list(steam_links.keys())
        patterns = await self._fetch_activity_patterns(candidate_ids)

        content_lower = (message_content or "").lower()
        lane_channel_ids = self._get_target_lane_channel_ids(
            guild, content_lower, author_rank_value
        )
        cutoff_str = (
            datetime.utcnow() - timedelta(days=ACTIVITY_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        lane_active_users = await self._fetch_lane_activity_users(
            candidate_ids, lane_channel_ids, cutoff_str
        )

        all_steam_ids = {sid for sids in steam_links.values() for sid in sids}
        online_users = await self._get_online_steam_users(all_steam_ids)

        user_presence: Dict[int, Tuple[str, Optional[int]]] = {}
        for discord_id, steam_ids in steam_links.items():
            for sid in steam_ids:
                if sid in online_users:
                    user_presence[discord_id] = online_users[sid]
                    break

        now = datetime.utcnow()
        candidates: List[Dict[str, object]] = []

        for discord_id in candidate_ids:
            if discord_id == author.id:
                continue

            member = guild.get_member(discord_id)
            if not member or member.bot:
                continue

            if member.voice and member.voice.channel:
                continue

            rank_name, rank_value = self._get_user_rank(member)

            if rank_strict and target_rank > 0 and rank_value > 0:
                diff = abs(rank_value - target_rank)
                if diff > RANK_TOLERANCE:
                    continue
                rank_score = self._rank_score(diff)
            elif rank_strict and target_rank > 0 and rank_value == 0:
                continue
            else:
                rank_score = 0.5

            pattern = patterns.get(discord_id)
            typical_hours = pattern[0] if pattern else []
            typical_days = pattern[1] if pattern else []
            activity_sessions = pattern[2] if pattern else 0

            time_score = self._time_match_score(typical_hours, typical_days, now)
            activity_score = (
                min(1.0, activity_sessions / 10.0) if activity_sessions > 0 else 0.0
            )

            lane_score = 1.0 if discord_id in lane_active_users else 0.0

            coplay_score = 0.0
            if discord_id in co_player_stats:
                sessions, minutes = co_player_stats[discord_id]
                coplay_score = min(1.0, (sessions / 5.0) + (minutes / 300.0))

            stage = None
            minutes = None
            presence_score = 0.0
            presence = user_presence.get(discord_id)
            if presence:
                stage, minutes = presence
                if stage == "lobby":
                    presence_score = 1.0
                elif stage == "match":
                    presence_score = 0.7

            if stage is None:
                if (
                    activity_sessions < MIN_ACTIVITY_SCORE_SESSIONS
                    and time_score < MIN_TIME_MATCH_SCORE
                ):
                    continue

            score = (
                rank_score * WEIGHT_RANK
                + time_score * WEIGHT_TIME
                + lane_score * WEIGHT_LANE
                + coplay_score * WEIGHT_COPLAY
                + presence_score * WEIGHT_PRESENCE
                + activity_score * WEIGHT_ACTIVITY
            )

            candidates.append(
                {
                    "user_id": discord_id,
                    "rank_name": rank_name,
                    "rank_value": rank_value,
                    "stage": stage,
                    "minutes": minutes,
                    "score": score,
                    "time_score": time_score,
                    "activity_sessions": activity_sessions,
                }
            )

        candidates.sort(key=lambda c: float(c.get("score", 0.0)), reverse=True)

        online = [c for c in candidates if c.get("stage") in ("lobby", "match")]
        offline = [c for c in candidates if c.get("stage") not in ("lobby", "match")]

        selected = online
        if len(selected) < MAX_MENTION_PINGS:
            selected += offline[: max(0, MAX_MENTION_PINGS - len(selected))]

        return selected

    def _build_player_lines(
        self,
        guild: discord.Guild,
        candidates: List[Dict[str, object]],
    ) -> Tuple[List[str], int, int]:
        in_lobby: List[str] = []
        in_match: List[str] = []
        in_active: List[str] = []

        for cand in candidates:
            discord_id = int(cand.get("user_id", 0) or 0)
            if not discord_id:
                continue
            member = guild.get_member(discord_id)
            if not member:
                continue
            stage = cand.get("stage")
            if stage == "lobby":
                in_lobby.append(member.mention)
            elif stage == "match":
                minutes = cand.get("minutes")
                if minutes is not None:
                    in_match.append(f"{member.mention} ({minutes}m)")
                else:
                    in_match.append(member.mention)
            else:
                in_active.append(member.mention)

        lobby_count = len(in_lobby)
        match_count = len(in_match)

        lines: List[str] = []
        remaining = MAX_MENTION_PINGS

        if in_lobby:
            shown = in_lobby[:remaining]
            remaining -= len(shown)
            extra = len(in_lobby) - len(shown)
            extra_txt = f" (+{extra})" if extra > 0 else ""
            if shown:
                lines.append(f"🟢 Lobby: {' '.join(shown)}{extra_txt}")

        if in_match:
            if remaining > 0:
                shown = in_match[:remaining]
                remaining -= len(shown)
                extra = len(in_match) - len(shown)
                extra_txt = f" (+{extra})" if extra > 0 else ""
                if shown:
                    lines.append(f"🎯 Match: {' '.join(shown)}{extra_txt}")
            else:
                lines.append(f"🎯 Match: (+{len(in_match)})")

        if in_active and remaining > 0:
            shown = in_active[:remaining]
            remaining -= len(shown)
            extra = len(in_active) - len(shown)
            extra_txt = f" (+{extra})" if extra > 0 else ""
            if shown:
                lines.append(f"🕒 Aktiv: {' '.join(shown)}{extra_txt}")

        return lines, lobby_count, match_count

    def _get_voice_state_context(self, guild: discord.Guild) -> str:
        """
        Scannt relevante Voice-Kanäle und baut einen Kontext-String für die KI.
        Zeigt: Name, ID, Anzahl User, Avg Rank (textuell).
        """
        lines = []

        # Helper um Channel-Info zu bauen
        def analyze_channel(channel: discord.VoiceChannel, label: str):
            members = [m for m in channel.members if not m.bot]
            count = len(members)

            ranks = []
            for m in members:
                _, r_val = self._get_user_rank(m)
                if r_val > 0:
                    ranks.append(r_val)

            avg_rank_str = "Leer"
            if ranks:
                avg_val = sum(ranks) / len(ranks)
                # Mapping back roughly to name
                # 1-5 Low, 6-7 Mid, 8+ High
                if avg_val < 5.5:
                    avg_rank_str = f"Low (~{avg_val:.1f})"
                elif avg_val < 7.5:
                    avg_rank_str = f"Mid (~{avg_val:.1f})"
                else:
                    avg_rank_str = f"High (~{avg_val:.1f})"

            link = f"https://discord.com/channels/{guild.id}/{channel.id}"
            return f"- {label}: {channel.name} - {link} (User: {count}, Skill: {avg_rank_str}, ID: {channel.id})"

        # 1. New Player Lane
        np_chan = guild.get_channel(NEW_PLAYER_LANE_ID)
        if np_chan and isinstance(np_chan, discord.VoiceChannel):
            lines.append(analyze_channel(np_chan, "New Player Lane"))

        # 2. Street Brawl
        sb_chan = guild.get_channel(STREET_BRAWL_LANE_ID)
        if sb_chan and isinstance(sb_chan, discord.VoiceChannel):
            lines.append(analyze_channel(sb_chan, "Street Brawl Lane"))

        # 3. Casual Category
        cat_casual = guild.get_channel(CASUAL_CATEGORY_ID)
        if cat_casual and isinstance(cat_casual, discord.CategoryChannel):
            # Zeige nur Channels mit Usern ODER die ersten 2 leeren
            empty_shown = 0
            for vc in cat_casual.voice_channels:
                if vc.id in [NEW_PLAYER_LANE_ID, STREET_BRAWL_LANE_ID]:
                    continue  # Skip duplicates
                if len(vc.members) > 0:
                    lines.append(analyze_channel(vc, "Casual Lane"))
                elif empty_shown < 2:
                    lines.append(analyze_channel(vc, "Casual Lane (Leer)"))
                    empty_shown += 1

        # 4. Ranked Category
        cat_ranked = guild.get_channel(RANKED_CATEGORY_ID)
        if cat_ranked and isinstance(cat_ranked, discord.CategoryChannel):
            empty_shown = 0
            for vc in cat_ranked.voice_channels:
                if len(vc.members) > 0:
                    lines.append(analyze_channel(vc, "Ranked/Grind Lane"))
                elif empty_shown < 1:  # Zeige nur 1 leeren Ranked Channel
                    lines.append(analyze_channel(vc, "Ranked/Grind Lane (Leer)"))
                    empty_shown += 1

        return "\n".join(lines)

    async def _handle_lfg_request(self, message: discord.Message):
        """
        Verarbeitet die Anfrage via OpenAI (ChatGPT).
        """
        output_channel = message.guild.get_channel(OUTPUT_CHANNEL_ID)
        if not output_channel or not isinstance(
            output_channel, discord.abc.Messageable
        ):
            log.warning(
                "Output-Channel %s nicht gefunden oder nicht messageable. Fallback auf LFG-Channel.",
                OUTPUT_CHANNEL_ID,
            )
            output_channel = message.channel
        prefix = ""
        if output_channel.id != message.channel.id:
            prefix = f"{message.author.mention} (LFG: {message.channel.mention}) "

        # 1. User Info
        rank_name, rank_val = self._get_user_rank(message.author)
        is_new_player = rank_val <= 5  # Unbekannt (0) bis Ritualist (5)

        player_lines: List[str] = []
        try:
            matching_players = await self._find_matching_players(
                message.author,
                message.content,
                rank_val,
            )
            if message.guild:
                player_lines, _lobby_count, _match_count = self._build_player_lines(
                    message.guild,
                    matching_players,
                )
        except Exception as exc:
            log.warning("Spielersuche fehlgeschlagen (%s)", exc)

        # 2. Voice Context holen
        voice_context = self._get_voice_state_context(message.guild)

        # 3. Prompt bauen
        system_prompt = (
            "Du hilfst Spielern den richtigen Voice-Channel zu finden. "
            "Schreib entspannt und natürlich, wie ein Community-Member der anderen hilft.\n\n"
            "**DEIN STIL (basierend auf echten Community-Nachrichten):**\n"
            "- Freundlich und ehrlich, kein Bullshit\n"
            "- `:)` am Satzende ist völlig ok, aber nicht übertreiben\n"
            "- Wenn Channel leer ist: sag's ehrlich, aber motiviere trotzdem aufzumachen\n"
            "- Bei schlechten Zeiten: sei ehrlich ('die Uhrzeit ist ganz böse')\n"
            "- Mach konkrete Angebote ('mach dir ne lane auf ich komm dazu')\n"
            "- Keine Floskeln, komm direkt zum Punkt\n\n"
            "**ECHTE BEISPIELE AUS DER COMMUNITY (orientier dich daran):**\n"
            '- "Schau mal hier ist zwar gerade keiner da aber wenn du joinst kommen bestimmt paar dazu :) https://discord.com/channels/..."\n'
            '- "Das ist der sinn dahinter :) ansonsten kannst du hier https://discord.com/channels/... dazu stoßen ist halt so Archon Oracle"\n'
            '- "Uhh die Uhrzeit ist ganz böse eigentlich ist zu so einer Uhrzeit fast niemand online :( https://discord.com/channels/... Vielleicht kommt jemand dazu"\n'
            '- "Mach dir ne lane auf ich komm und coache dich :)"\n'
            '- "Mach dir sonst einfach einen Kanal auf und schau ob wer dazu kommt"\n'
            '- "Schnupper einfach wo rein ;)"\n\n'
            "**CHANNEL AUSWAHL:**\n"
            "1. Street Brawl erwähnt? -> Street Brawl Lane\n"
            "2. User ist Rank 0-5 UND will nicht Ranked? -> New Player Lane (erwähne dass es für Einsteiger ist)\n"
            "3. User will Ranked/Grind? -> Ranked Category Channel (ähnlicher Rank +/- 2)\n"
            "4. Sonst -> Casual Lanes (erwähne ungefähren Rank wenn relevant)\n\n"
            "**WICHTIG:**\n"
            "- Schreib die Discord URL direkt hin, KEIN Markdown [Text](URL)\n"
            "- 1-3 Sätze reichen meistens\n"
            "- Sei ehrlich über Uhrzeit/Aktivität wenn relevant\n"
            "- Wenn verfügbare Spieler gezeigt werden, bezieh dich kurz darauf\n\n"
            f"Wenn es KEIN LFG ist, antworte mit `{NO_LFG_TOKEN}` (ohne Zusatz)."
        )

        user_input = (
            f"User: {message.author.display_name}\n"
            f"Rang: {rank_name} (Wert: {rank_val})\n"
            f"Ist New Player: {'Ja' if is_new_player else 'Nein'}\n"
            f'Nachricht: "{message.content}"\n\n'
            f"VERFÜGBARE VOICE CHANNELS (Status):\n{voice_context}\n\n"
            f"Empfiehl den besten Channel und antworte im Persona-Style. "
            f"Wenn es KEIN LFG ist oder eine Diskussion, antworte exakt mit {NO_LFG_TOKEN}."
        )

        # 4. AI Request
        ai = getattr(self.bot, "get_cog", lambda name: None)("AIConnector")
        if not ai:
            log.error("AIConnector nicht gefunden!")
            await output_channel.send(
                f"{prefix}⚠️ AI Modul nicht geladen. Kann gerade nicht helfen."
            )
            return

        async with output_channel.typing():
            response_text, _ = await ai.generate_text(
                provider="openai",
                prompt=user_input,
                system_prompt=system_prompt,
                model=OPENAI_MODEL,
                max_output_tokens=250,
                temperature=0.7,
            )

        clean_text: Optional[str] = None
        if response_text:
            # Clean up potential markdown code blocks provided by AI
            cleaned = (
                response_text.replace("```markdown", "").replace("```", "").strip()
            )
            if cleaned.upper() != NO_LFG_TOKEN:
                clean_text = cleaned

        response_parts: List[str] = []

        if player_lines:
            header = (
                "sucht Mitspieler!"
                if prefix
                else f"{message.author.mention} sucht Mitspieler!"
            )
            response_parts.append(header + "\n" + "\n".join(player_lines))

        if clean_text:
            response_parts.append(clean_text)

        if response_parts:
            final_text = "\n\n".join(response_parts)
            if prefix:
                final_text = prefix + final_text
            await output_channel.send(final_text)
            return

        await output_channel.send(
            f"{prefix}🤔 Puh, gerade hakt's bei mir. Versuch's gleich nochmal."
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        # Nur im LFG-Channel lauschen
        if message.channel.id != LFG_CHANNEL_ID:
            return

        is_lfg = await self._ai_check_lfg_intent(message.content)
        if not is_lfg:
            return

        # Cooldown Check
        now = time.time()
        if now - self.lfg_cooldowns.get(message.author.id, 0) < self.cooldown_seconds:
            # Silent ignore bei Spam oder kurze Reaction
            return

        self.lfg_cooldowns[message.author.id] = now

        # Nur echte LFG-Anfragen werden weiterverarbeitet
        await self._handle_lfg_request(message)

    @commands.command(name="lfgtest")
    @commands.has_permissions(administrator=True)
    async def lfg_debug(self, ctx):
        """Zeigt den aktuellen Voice-Kontext für Debugging."""
        ctx_str = self._get_voice_state_context(ctx.guild)
        await ctx.send(f"**Voice Context Snapshot:**\n{ctx_str}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SmartLFGAgent(bot))
