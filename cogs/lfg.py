"""
Smart LFG Agent - Deadlock LFG System
Analysiert Anfragen mit KI und routet Spieler basierend auf Skill, Modus und Verfügbarkeit.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Set, Tuple

import aiosqlite
import discord
from discord.ext import commands

from service.db import db_path

log = logging.getLogger("SmartLFG")

# --- Konfiguration ---

# LFG Eingangskanal (User schreibt hier)
LFG_CHANNEL_ID = 1376335502919335936

# Output-Kanal für Bot-Antworten
OUTPUT_CHANNEL_ID = 1374364800817303632

GUILD_ID = 1289721245281292288

# AI Config (ChatGPT/OpenAI)
OPENAI_MODEL = "gpt-5.2"
NO_LFG_TOKEN = "NO_LFG"
LFG_INTENT_MAX_TOKENS = 8
USE_AI_LFG_DETECTION = True

# Steam presence freshness (wie lange die Daten maximal alt sein dürfen)
PRESENCE_STALE_SECONDS = 300  # 5 Minuten

# Rank-Matching: wie viele Ränge Unterschied sind erlaubt?
RANK_TOLERANCE = 2  # +/- 2 Ränge
MAX_MENTION_PINGS = 10

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
        self.db: Optional[aiosqlite.Connection] = None
        self.lfg_cooldowns: Dict[int, float] = {}
        self.cooldown_seconds = 60  # Kurzer Cooldown gegen Spam

    async def cog_load(self) -> None:
        await self._ensure_db()
        log.info(
            "SmartLFGAgent geladen - LFG Channel: %s | Output Channel: %s",
            LFG_CHANNEL_ID,
            OUTPUT_CHANNEL_ID,
        )

    async def cog_unload(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None
        log.info("SmartLFGAgent entladen")

    async def _ensure_db(self) -> None:
        if self.db:
            return
        self.db = await aiosqlite.connect(str(db_path()))
        self.db.row_factory = aiosqlite.Row

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
            "mitspieler" in text or "team" in text or "gruppe" in text or "party" in text
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
            f"Nachricht: \"{message_content}\""
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
        include_unknown_until: Optional[int] = None,
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

        steam_links = await self._get_all_steam_links()
        all_steam_ids = {sid for sids in steam_links.values() for sid in sids}
        online_users = await self._get_online_steam_users(all_steam_ids)

        if not online_users:
            return []

        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            log.warning("Guild %s nicht gefunden", GUILD_ID)
            return []

        matching_players: List[Tuple[int, str, int, str, Optional[int]]] = []

        for discord_id, steam_ids in steam_links.items():
            if discord_id == author_id:
                continue

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

            member = guild.get_member(discord_id)
            if not member or member.bot:
                continue

            if member.voice and member.voice.channel:
                continue

            rank_name, rank_value = self._get_user_rank(member)
            is_unknown_rank = rank_value == 0 and rank_name.lower() == UNKNOWN_RANK_NAME_LOWER

            if allow_unknown_matches and is_unknown_rank:
                matching_players.append((discord_id, rank_name, rank_value, user_stage, user_minutes))
            elif min_rank <= rank_value <= max_rank:
                matching_players.append((discord_id, rank_name, rank_value, user_stage, user_minutes))

        return matching_players

    def _build_player_lines(
        self,
        guild: discord.Guild,
        matching_players: List[Tuple[int, str, int, str, Optional[int]]],
    ) -> Tuple[List[str], int, int]:
        in_lobby: List[str] = []
        in_match: List[str] = []

        for discord_id, _rank_name, _rank_value, stage, minutes in matching_players:
            member = guild.get_member(discord_id)
            if not member:
                continue
            if stage == "lobby":
                in_lobby.append(member.mention)
            elif stage == "match":
                if minutes is not None:
                    in_match.append(f"{member.mention} ({minutes}m)")
                else:
                    in_match.append(member.mention)

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
                if avg_val < 5.5: avg_rank_str = f"Low (~{avg_val:.1f})"
                elif avg_val < 7.5: avg_rank_str = f"Mid (~{avg_val:.1f})"
                else: avg_rank_str = f"High (~{avg_val:.1f})"

            link = f"https://discord.com/channels/{guild.id}/{channel.id}"
            return f"- {label}: [{channel.name}]({link}) (User: {count}, Skill: {avg_rank_str}, ID: {channel.id})"

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
                if vc.id in [NEW_PLAYER_LANE_ID, STREET_BRAWL_LANE_ID]: continue # Skip duplicates
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
                elif empty_shown < 1: # Zeige nur 1 leeren Ranked Channel
                    lines.append(analyze_channel(vc, "Ranked/Grind Lane (Leer)"))
                    empty_shown += 1

        return "\n".join(lines)

    async def _handle_lfg_request(self, message: discord.Message):
        """
        Verarbeitet die Anfrage via OpenAI (ChatGPT).
        """
        output_channel = message.guild.get_channel(OUTPUT_CHANNEL_ID)
        if not output_channel or not isinstance(output_channel, discord.abc.Messageable):
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
            is_unknown_rank = rank_val == 0 and rank_name.lower() == UNKNOWN_RANK_NAME_LOWER
            search_min_rank = max(1, rank_val - RANK_TOLERANCE)
            search_max_rank = min(11, rank_val + RANK_TOLERANCE)
            if is_unknown_rank:
                search_min_rank = 1
                search_max_rank = UNKNOWN_MAX_MATCH_RANK

            matching_players = await self._find_matching_players(
                rank_val,
                message.author.id,
                min_rank=search_min_rank,
                max_rank=search_max_rank,
                include_unknown_until=UNKNOWN_MAX_MATCH_RANK,
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
            "Du bist der LFG-Buddy der deutschen Deadlock Community. "
            "Dein Ziel ist es, Spieler basierend auf ihrem Rang und Wunschmodus in den richtigen Voice-Channel zu lotsen.\n\n"
            
            "**DEINE PERSÖNLICHKEIT (Authentisch):**\n"
            "- Du bist freundlich, direkt und nutzt lockere Umgangssprache (Duzen).\n"
            "- Du nutzt Smileys wie `:)` `;)` oder auch mal `❤️`, aber spamst sie nicht.\n"
            "- Du schreibst eher kurze Sätze. Keine Romane.\n"
            "- **KERN-PHILOSOPHIE:** Wenn ein empfohlener Channel LEER ist, motivierst du IMMER dazu, ihn aufzumachen. "
            "Dein Mantra: 'Mach dir ne Lane auf, andere kommen dann schon dazu.'\n"
            "- Du antwortest DIREKT mit dem Link zum Channel.\n\n"

            "**BEISPIELE FÜR DEINEN STIL (Nutze diese Art zu sprechen aber nicht exakt immer alles so Picken!):**\n"
            "- \"Mach dir ne Lane auf, die meisten kommen so 17/18h.\"\n"
            "- \"Komm einfach in Lane 2 :)\"\n"
            "- \"Schau mal hier ist zwar gerade keiner da aber wenn du joinst kommen bestimmt paar dazu :)\"\n"
            "- \"Du kannst hier [Link] dazu stoßen.\"\n"
            "- \"Möglichkeiten gibt's hier genug musst dich nur blicken lassen ❤️\"\n"
            "- \"Hard core ge carryt werden da gibts nur eine die Juicer Lane [Link]\"\n"
            "- \"Ansonsten gibts ne humane Lane hier [Link]\"\n"
            "- \"joa mach ne lane auf ich komme dazu\"\n\n"
            
            "**ROUTING REGELN (STRIKTE PRIORITÄT):**\n"
            "1. **Street Brawl:** Wenn der User 'Street Brawl' erwähnt -> Street Brawl Lane.\n"
            "2. **Neue Spieler:** Wenn User Rank 'Unbekannt' bis 'Ritualist' (Rank 0-5) ist UND NICHT explizit nach Ranked fragt -> New Player Lane.\n"
            "   - Schicke neue Spieler NICHT in Lanes mit High Elo Spielern (Oracle+).\n"
            "   - Sag ihnen ruhig, dass das die Lane für Einsteiger ist.\n"
            "3. **Ranked/Grind:** Wenn User explizit 'Ranked' oder 'Grind' will:\n"
            "   - Suche Channel in Ranked Kategorie.\n"
            "   - Toleranz: +/- 2 Ränge (User Rang vs Avg Channel Rank).\n"
            "   - Beispiel: User Emissary (6) passt zu Arcanist(4) bis Oracle(8).\n"
            "4. **Casual/Default:** Wenn nichts anderes passt -> Casual Lanes.\n"
            "   - Hier ist der Rang fast egal, aber vermeide extreme Unterschiede wenn möglich.\n\n"
            
            "**OUTPUT FORMAT:**\n"
            "Antworte kurz (2-3 Sätze). Verlinke den Voice Channel im Format `[ChannelName](URL)`. "
            "Erkläre kurz warum du diesen Channel empfiehlst (z.B. 'passender Rang', 'perfekt für Einsteiger'). "
            "Keine 'Hallo' Floskeln am Anfang, steig direkt ein wie in den Beispielen.\n"
            f"WICHTIG: Wenn es KEIN LFG ist oder eine Diskussion, antworte exakt mit `{NO_LFG_TOKEN}` (ohne Zusatz)."
        )

        user_input = (
            f"User: {message.author.display_name}\n"
            f"Rang: {rank_name} (Wert: {rank_val})\n"
            f"Ist New Player: {'Ja' if is_new_player else 'Nein'}\n"
            f"Nachricht: \"{message.content}\"\n\n"
            f"VERFÜGBARE VOICE CHANNELS (Status):\n{voice_context}\n\n"
            f"Empfiehl den besten Channel und antworte im Persona-Style. "
            f"Wenn es KEIN LFG ist oder eine Diskussion, antworte exakt mit {NO_LFG_TOKEN}."
            
        )

        # 4. AI Request
        ai = getattr(self.bot, "get_cog", lambda name: None)("AIConnector")
        if not ai:
            log.error("AIConnector nicht gefunden!")
            await output_channel.send(f"{prefix}⚠️ AI Modul nicht geladen. Kann gerade nicht helfen.")
            return

        async with output_channel.typing():
            response_text, _ = await ai.generate_text(
                provider="openai",
                prompt=user_input,
                system_prompt=system_prompt,
                model=OPENAI_MODEL,
                max_output_tokens=250,
                temperature=0.7
            )

        clean_text: Optional[str] = None
        if response_text:
            # Clean up potential markdown code blocks provided by AI
            cleaned = response_text.replace("```markdown", "").replace("```", "").strip()
            if cleaned.upper() != NO_LFG_TOKEN:
                clean_text = cleaned

        response_parts: List[str] = []

        if player_lines:
            header = "sucht Mitspieler!" if prefix else f"{message.author.mention} sucht Mitspieler!"
            response_parts.append(header + "\n" + "\n".join(player_lines))

        if clean_text:
            response_parts.append(clean_text)

        if response_parts:
            final_text = "\n\n".join(response_parts)
            if prefix:
                final_text = prefix + final_text
            await output_channel.send(final_text)
            return

        await output_channel.send(f"{prefix}🤔 Puh, gerade hakt's bei mir. Versuch's gleich nochmal.")

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
