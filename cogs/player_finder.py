"""
Player Finder – Intelligente Spielersuche für Voice-Lobbys.

Reagiert auf Nachrichten im LFG-Channel und findet passende Mitspieler
basierend auf einfachen Y/N Filtern:
- Passende Zeit (typical_hours enthält aktuelle Stunde)
- Passender Tag (typical_days enthält aktuellen Wochentag)
- Voice-aktiv in den letzten 14 Tagen
- Passender Rang (±3)

Steam-Status priorisiert: Lobby → Match → Discord online
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta

import discord
from discord.ext import commands

from service import db

log = logging.getLogger("PlayerFinder")

# --- Konfiguration ---

GUILD_ID = 1289721245281292288

# Channel-IDs
LFG_CHANNEL_ID = 1376335502919335936
SUGGESTION_CHANNEL_ID = 1376335502919335936

# Kategorien die überwacht werden
NEW_PLAYER_CATEGORY_ID = 1465839366634209361
CASUAL_CATEGORY_ID = 1289721245281292290
RANKED_CATEGORY_ID = 1412804540994162789
STREET_BRAWL_CATEGORY_ID = 1357422957017698478

# Mindest-Spieler in Lane damit Suche getriggert wird
MIN_PLAYERS_FOR_SEARCH = 1
MAX_PLAYERS_FOR_SEARCH = 4

# Aktivitäts-Lookback
ACTIVITY_LOOKBACK_DAYS = 14

# Rang-Toleranz (±Ränge)
RANK_TOLERANCE_SUGGESTIONS = 3

# Rolle für Spieler die aktiv gepingt werden wollen
LFG_NOTIFY_ROLE_ID = 1411798947936342097

# Maximale Pings pro Nachricht
MAX_LFG_PINGS = 5

# Steam Presence freshness
PRESENCE_STALE_SECONDS = 120

# Cooldown pro User (Sekunden)
COOLDOWN_SECONDS = 60

# Rank Definitionen
DISCORD_RANK_ROLES: dict[int, tuple[str, int]] = {
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

NEW_PLAYER_MAX_RANK = 4

# --- Regex Pattern für LFG-Erkennung ---

SHORT_LFG_COUNT_RE = re.compile(
    r"^\s*(?:(?:suche|suchen|lfm|lfg)\s*\+?\s*[1-6]|\+\s*[1-6])\s*$",
    re.IGNORECASE,
)
PLUS_PLAYER_RE = re.compile(r"\+\s*[1-6](?:\D|$)")
RANK_NAME_TO_VALUE = {name.lower(): val for name, val in DISCORD_RANK_ROLES.values()}
MESSAGE_RANK_ALIASES = {
    "ini": "Initiate",
    "seek": "Seeker",
    "alch": "Alchemist",
    "arc": "Arcanist",
    "rit": "Ritualist",
    "emi": "Emissary",
    "emiss": "Emissary",
    "arch": "Archon",
    "asc": "Ascendant",
    "et": "Eternus",
    "arkanist": "Arcanist",
    "ascendent": "Ascendant",
    "ethernus": "Eternus",
}


class PlayerFinder(commands.Cog):
    """
    Findet passende Mitspieler für Leute die in Voice-Lanes sitzen.

    Reagiert auf Nachrichten im LFG-Channel.
    Nutzt Y/N Filter:
    - Passende Zeit (typical_hours)
    - Passender Tag (typical_days)
    - Voice-aktiv in letzten 14 Tagen
    - Passender Rang (±3)
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.lfg_cooldowns: dict[int, float] = {}

    async def cog_load(self) -> None:
        log.info("PlayerFinder geladen (reaktiv) – Cooldown: %ss", COOLDOWN_SECONDS)

    async def cog_unload(self) -> None:
        log.info("PlayerFinder entladen")

    # --- LFG Intent Erkennung ---

    def _keyword_lfg_intent(self, message_content: str) -> bool:
        """
        Erkennt LFG-Intention per Keyword-Heuristik.
        """
        text = (message_content or "").lower()
        if not text:
            return False

        rank_tokens = tuple(RANK_NAME_TO_VALUE.keys()) + tuple(MESSAGE_RANK_ALIASES.keys())

        # Rang-Kontext + Spielwunsch
        if (
            any(token in text for token in rank_tokens)
            and ("bock" in text or "lust" in text)
            and any(
                token in text
                for token in (
                    "runde",
                    "runden",
                    "ründchen",
                    "rundchen",
                    "game",
                    "games",
                    "match",
                    "matches",
                    "spielen",
                    "zocken",
                    "grinden",
                    "gamen",
                )
            )
        ):
            return True

        # "suchen +3", "suche+2", "lfm +1" oder nur "+3"
        if SHORT_LFG_COUNT_RE.match(text):
            return True

        # Direkte LFG/LFM Keywords
        if "lfg" in text or "lfm" in text:
            return True

        # Gruppen-Keywords
        if "duo" in text or "trio" in text or "squad" in text or "stack" in text:
            return True

        # "bock" Patterns
        if "bock" in text and any(
            w in text
            for w in (
                "jemand",
                "jmd",
                "wer",
                "iwer",
                "irgendwer",
                "noch",
                "hat",
                "hätte",
                "hättest",
            )
        ):
            return True

        # "lust" Patterns
        if "lust" in text and any(
            w in text
            for w in (
                "jemand",
                "jmd",
                "wer",
                "iwer",
                "irgendwer",
                "hat",
                "noch",
            )
        ):
            return True

        # "suche" breit
        if "suche" in text or "suchen" in text or "gesucht" in text:
            if PLUS_PLAYER_RE.search(text):
                return True
            if any(
                w in text
                for w in (
                    "leute",
                    "spieler",
                    "mitspieler",
                    "team",
                    "gruppe",
                    "party",
                    "wen",
                    "anschluss",
                    "jemand",
                    "noch",
                    "nach",
                    "mates",
                    "mate",
                )
            ):
                return True

        # "sucht noch jemand"
        if "sucht" in text and "jemand" in text:
            return True

        # Spielen/Zocken + Frage-Kontext
        if ("spielen" in text or "zocken" in text or "grinden" in text or "gamen" in text) and (
            "wer" in text
            or "jemand" in text
            or "bock" in text
            or "jmd" in text
            or "iwer" in text
            or "irgendwer" in text
        ):
            return True

        # "paar runden/games" standalone
        if "paar runden" in text or "paar games" in text or "paar rounds" in text:
            return True

        # "jemand down" / "wer down"
        if "down" in text and any(w in text for w in ("jemand", "wer", "iwer", "irgendwer")):
            return True

        # "mag wer"
        if "mag wer" in text:
            return True

        # "möchte jemand"
        if "möchte" in text and ("jemand" in text or "wer" in text):
            return True

        # "Interesse" Patterns
        if (
            "interesse" in text
            and any(
                w in text
                for w in (
                    "jemand",
                    "wer",
                    "jmd",
                    "iwer",
                    "irgendwer",
                    "anderer",
                    "andere",
                    "noch",
                    "hat",
                    "hätte",
                )
            )
            and any(
                w in text
                for w in (
                    "spielen",
                    "zocken",
                    "grinden",
                    "gamen",
                    "runde",
                    "runden",
                    "game",
                    "games",
                    "match",
                    "matches",
                    "anfänger",
                    "anfanger",
                    "neuling",
                    "neu",
                )
            )
        ):
            return True

        # English LFG patterns
        if "hmu" in text:
            return True
        if "anyone" in text and ("wanna" in text or "down" in text or "game" in text):
            return True

        # "auf der suche"
        if "auf der suche" in text:
            return True

        return False

    # --- Rang-Erkennung ---

    def _get_user_rank(self, member: discord.Member) -> tuple[str, int]:
        """Ermittelt den höchsten Rang eines Users."""
        best_name = "Unbekannt"
        best_val = 0
        for role in member.roles:
            if role.id in DISCORD_RANK_ROLES:
                name, val = DISCORD_RANK_ROLES[role.id]
                if val > best_val:
                    best_name, best_val = name, val
        return best_name, best_val

    def _get_lane_avg_rank(self, members: list[discord.Member]) -> float:
        """Berechnet den Durchschnitts-Rang einer Lane."""
        ranks = []
        for m in members:
            _, val = self._get_user_rank(m)
            if val > 0:
                ranks.append(val)
        return sum(ranks) / len(ranks) if ranks else 0.0

    def _get_lane_label(self, category_id: int) -> str:
        """Gibt das Label für eine Kategorie zurück."""
        labels = {
            NEW_PLAYER_CATEGORY_ID: "Neue Spieler",
            CASUAL_CATEGORY_ID: "Casual",
            RANKED_CATEGORY_ID: "Ranked",
            STREET_BRAWL_CATEGORY_ID: "Street Brawl",
        }
        return labels.get(category_id, "Unbekannt")

    def _rank_matches(self, member: discord.Member, avg_rank: float) -> bool:
        """Prüft ob Rang des Users ±3 vom avg_rank liegt."""
        if avg_rank <= 0:
            return True  # Unbekannt passt immer
        _, rank_val = self._get_user_rank(member)
        if rank_val <= 0:
            return True  # Unbekannt passt immer
        return abs(rank_val - avg_rank) <= RANK_TOLERANCE_SUGGESTIONS

    # --- Steam Presence ---

    async def _get_steam_presence(self) -> dict[int, tuple[str, int | None]]:
        """Holt Steam-Präsenz für alle verlinkten Accounts."""
        link_rows = await db.query_all_async(
            """
            SELECT user_id, steam_id FROM steam_links
            WHERE steam_id IS NOT NULL AND steam_id != '' AND verified = 1
            ORDER BY primary_account DESC
            """
        )
        if not link_rows:
            return {}

        user_to_steam: dict[int, list[str]] = {}
        all_steam_ids: set[str] = set()
        for row in link_rows:
            uid = int(row["user_id"])
            sid = str(row["steam_id"])
            user_to_steam.setdefault(uid, []).append(sid)
            all_steam_ids.add(sid)

        if not all_steam_ids:
            return {}

        now = int(time.time())
        steam_json = json.dumps(sorted(all_steam_ids))
        state_rows = await db.query_all_async(
            """
            SELECT steam_id, deadlock_stage, deadlock_minutes,
                   deadlock_updated_at, last_seen_ts
            FROM live_player_state
            WHERE steam_id IN (SELECT value FROM json_each(?))
            AND (in_deadlock_now = 1 OR deadlock_stage IS NOT NULL)
            """,
            (steam_json,),
        )

        steam_online: dict[str, tuple[str, int | None]] = {}
        for row in state_rows:
            updated = row["deadlock_updated_at"] or row["last_seen_ts"]
            if not updated or now - int(updated) > PRESENCE_STALE_SECONDS:
                continue
            stage = row["deadlock_stage"]
            if stage not in {"lobby", "match"}:
                continue
            minutes = row["deadlock_minutes"]
            if stage == "match" and minutes is not None:
                minutes = int(minutes) + ((now - int(updated)) // 60)
            steam_online[str(row["steam_id"])] = (stage, minutes)

        result: dict[int, tuple[str, int | None]] = {}
        for uid, sids in user_to_steam.items():
            for sid in sids:
                if sid in steam_online:
                    result[uid] = steam_online[sid]
                    break

        return result

    async def _get_steam_friend_ids(self) -> set[int]:
        """Gibt Discord-User-IDs zurück, die den Bot als Steam-Freund haben."""
        rows = await db.query_all_async(
            """
            SELECT DISTINCT user_id FROM steam_links
            WHERE verified = 1 AND is_steam_friend = 1 AND user_id > 0
            """
        )
        return {int(r["user_id"]) for r in rows}

    # --- Y/N Filter für Kandidaten ---

    def _parse_json_list(self, raw) -> list[int]:
        """Parst JSON-Liste zu int-Liste."""
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [int(x) for x in parsed if str(x).isdigit() or isinstance(x, int)]
        except Exception:
            return []
        return []

    def _passes_time_filter(self, typical_hours: list[int], current_hour: int) -> bool:
        """Prüft ob typical_hours die aktuelle Stunde enthält (±2h oder wrap)."""
        if not typical_hours:
            return True  # Keine Daten = keine Einschränkung
        return any(abs(current_hour - h) <= 2 or abs(current_hour - h) >= 22 for h in typical_hours)

    def _passes_day_filter(self, typical_days: list[int], current_day: int) -> bool:
        """Prüft ob typical_days den aktuellen Wochentag enthält."""
        if not typical_days:
            return True  # Keine Daten = keine Einschränkung
        return current_day in typical_days

    async def _has_voice_activity_14d(
        self,
        user_id: int,
        channel_ids: list[int],
    ) -> bool:
        """Prüft ob user in letzten 14 Tagen in diesen Voice-Channels aktiv war."""
        if not channel_ids:
            return False

        cutoff = (datetime.utcnow() - timedelta(days=ACTIVITY_LOOKBACK_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        channel_json = json.dumps([int(c) for c in channel_ids])
        uid_json = json.dumps([int(user_id)])

        rows = await db.query_all_async(
            """
            SELECT COUNT(*) as cnt FROM voice_session_log
            WHERE started_at >= ?
              AND user_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
              AND channel_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
            """,
            (cutoff, uid_json, channel_json),
        )
        return bool(rows and rows[0]["cnt"] > 0)

    async def _get_candidates_for_lane(
        self,
        guild: discord.Guild,
        lane_members: list[discord.Member],
        category_id: int,
        steam_presence: dict[int, tuple[str, int | None]],
        steam_friend_ids: set[int],
    ) -> list[tuple[discord.Member, str]]:
        """
        Findet passende Kandidaten für eine Lane.

        Filter: Zeit ✓ AND Tag ✓ AND Voice-aktiv (14d) ✓ AND Rang ±3 ✓

        Returns: [(member, status_label), ...] sortiert nach Steam-Status
        """
        lane_member_ids = {m.id for m in lane_members}
        avg_rank = self._get_lane_avg_rank(lane_members)

        # Channel-IDs in derselben Kategorie für Activity-Check
        cat = guild.get_channel(category_id)
        channel_ids = []
        if isinstance(cat, discord.CategoryChannel):
            channel_ids = [ch.id for ch in cat.voice_channels]

        now = datetime.utcnow()
        current_hour = now.hour
        current_day = now.weekday()

        # Steam-Links laden für alle verifizierten User
        link_rows = await db.query_all_async(
            """
            SELECT user_id, steam_id FROM steam_links
            WHERE steam_id IS NOT NULL AND steam_id != '' AND verified = 1
            """
        )

        candidates: list[tuple[discord.Member, str]] = []

        for row in link_rows:
            uid = int(row["user_id"])
            if uid in lane_member_ids:
                continue

            member = guild.get_member(uid)
            if not member or member.bot:
                continue

            # Bereits in einem Voice-Channel? Skip.
            if member.voice and member.voice.channel:
                continue

            # Rang-Filter
            if not self._rank_matches(member, avg_rank):
                continue

            # Activity Patterns laden
            pattern_rows = await db.query_all_async(
                """
                SELECT typical_hours, typical_days FROM user_activity_patterns
                WHERE user_id = ?
                """,
                (uid,),
            )

            if pattern_rows:
                pat = pattern_rows[0]
                hours = self._parse_json_list(pat["typical_hours"])
                days = self._parse_json_list(pat["typical_days"])

                # Zeit-Filter
                if not self._passes_time_filter(hours, current_hour):
                    continue

                # Tag-Filter
                if not self._passes_day_filter(days, current_day):
                    continue

            # Voice-Activity (14d) in dieser Kategorie
            if channel_ids:
                has_activity = await self._has_voice_activity_14d(uid, channel_ids)
                if not has_activity:
                    continue

            # Status bestimmen
            steam = steam_presence.get(uid)
            if steam:
                stage, minutes = steam
                if stage == "lobby":
                    status = "🟢 In der Deadlock-Lobby"
                elif stage == "match":
                    suffix = f" (~{minutes}min)" if minutes else ""
                    status = f"🎮 Im Match{suffix}"
                else:
                    status = "🟡 Im Spiel"
            elif member.status == discord.Status.online:
                status = "💬 Auf Discord online"
            elif member.status == discord.Status.idle:
                status = "🟠 Abwesend"
            else:
                status = "⚪ Offline"

            candidates.append((member, status))

        # Sortieren: Lobby → Match → Discord online → idle → offline
        def sort_key(item: tuple[discord.Member, str]) -> int:
            _, status = item
            if status.startswith("🟢"):
                return 0
            if status.startswith("🎮"):
                return 1
            if status.startswith("💬"):
                return 2
            if status.startswith("🟠"):
                return 3
            return 4

        candidates.sort(key=sort_key)
        return candidates[:MAX_LFG_PINGS]

    # --- Lane Scanning ---

    def _scan_category_lanes(
        self,
        guild: discord.Guild,
        category_id: int,
    ) -> list[tuple[discord.VoiceChannel, list[discord.Member], int]]:
        """Scannt alle Voice-Channels einer Kategorie. Gibt [(vc, members, category_id), ...] zurück."""
        cat = guild.get_channel(category_id)
        if not isinstance(cat, discord.CategoryChannel):
            return []

        result = []
        for vc in cat.voice_channels:
            members = [m for m in vc.members if not m.bot]
            if members:
                result.append((vc, members, category_id))
        return result

    # --- Embed Bauen ---

    def _build_embed(
        self,
        guild: discord.Guild,
        lane_name: str,
        lane_label: str,
        members: list[discord.Member],
        candidates: list[tuple[discord.Member, str]],
        channel_id: int,
    ) -> discord.Embed:
        """Baut das Vorschlags-Embed."""
        member_names = ", ".join(m.display_name for m in members[:5])
        slots_free = (6 - len(members)) if len(members) < 6 else 2

        embed = discord.Embed(
            title="\U0001f50d Mitspieler-Vorschläge",
            description=(
                f"In **{lane_name}** ({lane_label}) "
                f"{'ist' if len(members) == 1 else 'sind'} gerade "
                f"**{member_names}** unterwegs und "
                f"{'sucht' if len(members) == 1 else 'suchen'} noch "
                f"**{slots_free}** Mitspieler!"
            ),
            color=discord.Color.blue(),
        )

        if not candidates:
            embed.add_field(
                name="\U0001f465 Keine passenden Mitspieler gefunden",
                value="Versuch es später nochmal oder schreib direkt jemanden an.",
                inline=False,
            )
            return embed

        lines = []
        for member, status in candidates:
            friend_badge = " 🤝" if member.id in self._steam_friend_cache else ""
            lines.append(f"**{member.display_name}**{friend_badge}\n{status}")

        embed.add_field(
            name="\U0001f465 Mögliche Mitspieler",
            value="\n\n".join(lines),
            inline=False,
        )

        lane_link = f"https://discord.com/channels/{guild.id}/{channel_id}"
        embed.add_field(
            name="\u27a1\ufe0f Direkt joinen",
            value=f"[Hier klicken um beizutreten]({lane_link})",
            inline=False,
        )

        embed.set_footer(text="Basierend auf Aktivität und Steam-Status")

        return embed

    # --- Cooldowns ---

    def _check_cooldown(self, user_id: int) -> bool:
        """Prüft Cooldown. Returns True wenn Request erlaubt ist."""
        now = time.time()
        last = self.lfg_cooldowns.get(user_id, 0)
        if now - last < COOLDOWN_SECONDS:
            return False
        self.lfg_cooldowns[user_id] = now
        return True

    # --- Main Handler ---

    async def _handle_lfg_request(self, message: discord.Message) -> None:
        """Verarbeitet eine LFG-Anfrage."""
        guild = message.guild
        if not guild:
            return

        output_channel = guild.get_channel(SUGGESTION_CHANNEL_ID)
        if not output_channel or not isinstance(output_channel, discord.abc.Messageable):
            return

        # Steam-Daten laden
        steam_presence = await self._get_steam_presence()
        self._steam_friend_cache = await self._get_steam_friend_ids()

        # Wenn der User IN einer Voice-Lane ist → suche gezielt für diese Lane
        if message.author.voice and message.author.voice.channel:
            target_channel = message.author.voice.channel
            # Lane finden
            for cat_id in [
                NEW_PLAYER_CATEGORY_ID,
                CASUAL_CATEGORY_ID,
                RANKED_CATEGORY_ID,
                STREET_BRAWL_CATEGORY_ID,
            ]:
                cat = guild.get_channel(cat_id)
                if not isinstance(cat, discord.CategoryChannel):
                    continue
                if target_channel.category_id != cat_id:
                    continue

                members = [m for m in target_channel.members if not m.bot]
                if len(members) < MIN_PLAYERS_FOR_SEARCH or len(members) > MAX_PLAYERS_FOR_SEARCH:
                    return

                lane_label = self._get_lane_label(cat_id)
                candidates = await self._get_candidates_for_lane(
                    guild,
                    members,
                    cat_id,
                    steam_presence,
                    self._steam_friend_cache,
                )

                embed = self._build_embed(
                    guild, target_channel.name, lane_label, members, candidates, target_channel.id
                )
                await output_channel.send(embed=embed)
                return

            return  # Lane nicht in einer der überwachten Kategorien

        # User ist NICHT in Voice → generische LFG Suche (ähnlich lfg.py)
        # Scanne alle Kategorien nach aktiven Lanes mit Platz
        for cat_id in [
            NEW_PLAYER_CATEGORY_ID,
            CASUAL_CATEGORY_ID,
            RANKED_CATEGORY_ID,
            STREET_BRAWL_CATEGORY_ID,
        ]:
            lanes = self._scan_category_lanes(guild, cat_id)
            for vc, members, _ in lanes:
                if len(members) < MIN_PLAYERS_FOR_SEARCH or len(members) > MAX_PLAYERS_FOR_SEARCH:
                    continue

                lane_label = self._get_lane_label(cat_id)
                candidates = await self._get_candidates_for_lane(
                    guild,
                    members,
                    cat_id,
                    steam_presence,
                    self._steam_friend_cache,
                )

                if candidates:
                    embed = self._build_embed(
                        guild, vc.name, lane_label, members, candidates, vc.id
                    )
                    await output_channel.send(embed=embed)
                    return

        # Keine aktive Lane mit Platz gefunden
        embed = discord.Embed(
            title="\U0001f50d Mitspieler-Vorschläge",
            description=(
                f"**{message.author.display_name}** sucht Mitspieler!\n\n"
                "Aktuell ist keine Lane mit Platz verfügbar. "
                "Mach einfach eine auf — es kommen erfahrungsgemäß schnell Leute dazu."
            ),
            color=discord.Color.blue(),
        )
        await output_channel.send(embed=embed)

    # --- Event Listener ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        # Nur im LFG-Channel lauschen
        if message.channel.id != LFG_CHANNEL_ID:
            return

        # Intent-Check
        if not self._keyword_lfg_intent(message.content):
            return

        # Cooldown-Check (per User)
        if not self._check_cooldown(message.author.id):
            return

        await self._handle_lfg_request(message)

    # Cache für steam friend ids (wird pro request aktualisiert)
    _steam_friend_cache: set[int] = set()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlayerFinder(bot))
