"""
Player Finder – Intelligente Spielersuche für Voice-Lobbys.

Erkennt wenn Spieler in einer Lane sind und nach Mitspielern suchen,
und schlägt passende Mitspieler basierend auf:
- Aktivität der letzten 14 Tage
- Typische aktive Uhrzeiten
- Steam-Präsenz (wer spielt gerade Deadlock?)
- Ob der Spieler bereits auf dem Server aktiv ist
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from service import db

log = logging.getLogger("PlayerFinder")

# --- Konfiguration ---

GUILD_ID = 1289721245281292288

# Kategorien die überwacht werden
NEW_PLAYER_CATEGORY_ID = 1465839366634209361
CASUAL_CATEGORY_ID = 1289721245281292290
RANKED_CATEGORY_ID = 1412804540994162789
STREET_BRAWL_CATEGORY_ID = 1357422957017698478

# Mindest-Spieler in Lane damit Suche getriggert wird
MIN_PLAYERS_FOR_SEARCH = 1
# Max Spieler - ab hier ist die Lane voll genug
MAX_PLAYERS_FOR_SEARCH = 4

# Wie oft der Finder-Loop läuft (Sekunden)
FINDER_INTERVAL_SECONDS = 300  # alle 5 Minuten

# Cooldown pro Lane: nicht öfter als alle 30 Minuten einen Vorschlag
LANE_SUGGESTION_COOLDOWN_SECONDS = 1800

# Aktivitäts-Lookback
ACTIVITY_LOOKBACK_DAYS = 14
MIN_SESSIONS_FOR_SUGGESTION = 2
MIN_ACTIVITY_SCORE = 3

# Steam Presence
PRESENCE_STALE_SECONDS = 120

# Maximale Vorschläge pro Nachricht
MAX_SUGGESTIONS = 5

# Rang-Toleranz für Vorschläge (±Ränge)
RANK_TOLERANCE_SUGGESTIONS = 3

# Output Channel für Vorschläge (LFG Channel)
SUGGESTION_CHANNEL_ID = 1376335502919335936

# Rank Definitionen (gleich wie in lfg.py)
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


class PlayerFinder(commands.Cog):
    """
    Findet passende Mitspieler für Leute die in Voice-Lanes sitzen.

    Basiert auf:
    - Voice-Session-History (letzte 14 Tage)
    - Typische aktive Uhrzeiten (user_activity_patterns)
    - Steam-Präsenz (live_player_state)
    - Rang-Kompatibilität
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Cooldowns: {channel_id: last_suggestion_timestamp}
        self._lane_cooldowns: dict[int, float] = {}

    async def cog_load(self) -> None:
        self.finder_loop.start()
        log.info("PlayerFinder geladen – Interval: %ss", FINDER_INTERVAL_SECONDS)

    # --- Steam Friend Status ---

    async def _get_steam_friend_ids(self) -> set[int]:
        """
        Gibt Discord-User-IDs zurück, die den Bot als Steam-Freund haben
        (steam_links.is_steam_friend = 1 AND verified = 1).
        """
        rows = await db.query_all_async(
            """
            SELECT DISTINCT user_id FROM steam_links
            WHERE verified = 1 AND is_steam_friend = 1 AND user_id > 0
            """
        )
        return {int(r["user_id"]) for r in rows}

    async def cog_unload(self) -> None:
        self.finder_loop.cancel()
        log.info("PlayerFinder entladen")

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

    # --- Steam Presence ---

    async def _get_steam_presence(self) -> dict[int, tuple[str, int | None]]:
        """
        Holt Steam-Präsenz für alle verlinkten Accounts.
        Returns: {discord_user_id: (stage, minutes)}
        """
        # Alle Steam-Links laden
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

        # Live-Status prüfen
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

        # Auf Discord-User mappen
        result: dict[int, tuple[str, int | None]] = {}
        for uid, sids in user_to_steam.items():
            for sid in sids:
                if sid in steam_online:
                    result[uid] = steam_online[sid]
                    break

        return result

    # --- Aktivitätsdaten ---

    async def _get_active_players(
        self,
        channel_ids: list[int],
        avg_rank: float,
        lane_member_ids: set[int],
    ) -> list[dict]:
        """
        Findet Spieler die basierend auf den letzten 14 Tagen und ihren
        typischen aktiven Uhrzeiten gut zu einer Lane passen.
        """
        cutoff = (datetime.utcnow() - timedelta(days=ACTIVITY_LOOKBACK_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        channel_json = json.dumps([int(c) for c in channel_ids])

        # Spieler die in den letzten 14 Tagen in diesen Channels aktiv waren
        rows = await db.query_all_async(
            """
            SELECT DISTINCT vsl.user_id, COUNT(*) as session_count,
                   SUM(vsl.duration_seconds) as total_seconds
            FROM voice_session_log vsl
            WHERE vsl.started_at >= ?
              AND vsl.channel_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
            GROUP BY vsl.user_id
            HAVING session_count >= ?
            ORDER BY session_count DESC
            LIMIT 50
            """,
            (cutoff, channel_json, MIN_SESSIONS_FOR_SUGGESTION),
        )

        candidate_ids = [int(r["user_id"]) for r in rows]
        session_counts = {int(r["user_id"]): int(r["session_count"]) for r in rows}

        if not candidate_ids:
            return []

        # Aktivitäts-Patterns laden
        ids_json = json.dumps(candidate_ids)
        pattern_rows = await db.query_all_async(
            """
            SELECT user_id, typical_hours, typical_days, activity_score_2w
            FROM user_activity_patterns
            WHERE user_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
            """,
            (ids_json,),
        )

        patterns: dict[int, tuple[list[int], list[int], int]] = {}
        for r in pattern_rows:
            uid = int(r["user_id"])
            hours = self._parse_json(r["typical_hours"])
            days = self._parse_json(r["typical_days"])
            score = int(r["activity_score_2w"] or 0)
            patterns[uid] = (hours, days, score)

        now = datetime.utcnow()
        current_hour = now.hour
        current_day = now.weekday()

        candidates = []
        for uid in candidate_ids:
            if uid in lane_member_ids:
                continue

            pat = patterns.get(uid)
            if not pat:
                continue

            hours, days, activity_score = pat
            if activity_score < MIN_ACTIVITY_SCORE:
                continue

            # Zeitlich passend? Spieler ist typischerweise jetzt oder bald aktiv
            hour_match = any(
                abs(current_hour - h) <= 2 or abs(current_hour - h) >= 22
                for h in hours
            ) if hours else False
            day_match = current_day in days if days else True

            if not hour_match:
                continue

            time_score = 1.0 if (hour_match and day_match) else 0.6
            sessions = session_counts.get(uid, 0)
            activity_norm = min(1.0, sessions / 10.0)

            candidates.append({
                "user_id": uid,
                "sessions": sessions,
                "activity_score": activity_score,
                "time_score": time_score,
                "activity_norm": activity_norm,
                "hour_match": hour_match,
                "day_match": day_match,
            })

        return candidates

    def _parse_json(self, raw) -> list[int]:
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [int(x) for x in parsed if str(x).isdigit() or isinstance(x, int)]
        except Exception:
            return []
        return []

    # --- Haupt-Loop ---

    @tasks.loop(seconds=FINDER_INTERVAL_SECONDS)
    async def finder_loop(self) -> None:
        """Prüft alle relevanten Voice-Lanes und macht Vorschläge."""
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        output_channel = guild.get_channel(SUGGESTION_CHANNEL_ID)
        if not output_channel or not isinstance(output_channel, discord.abc.Messageable):
            return

        now = time.time()

        # Steam-Präsenz und Freundesliste laden
        steam_presence = await self._get_steam_presence()
        steam_friend_ids = await self._get_steam_friend_ids()

        # Alle überwachten Kategorien scannen
        category_ids = [
            NEW_PLAYER_CATEGORY_ID,
            CASUAL_CATEGORY_ID,
            RANKED_CATEGORY_ID,
            STREET_BRAWL_CATEGORY_ID,
        ]

        for cat_id in category_ids:
            cat = guild.get_channel(cat_id)
            if not isinstance(cat, discord.CategoryChannel):
                continue

            for vc in cat.voice_channels:
                await self._check_lane(
                    guild, vc, cat_id, output_channel, steam_presence, steam_friend_ids, now,
                )

    @finder_loop.before_loop
    async def _before_finder_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _check_lane(
        self,
        guild: discord.Guild,
        vc: discord.VoiceChannel,
        category_id: int,
        output_channel: discord.abc.Messageable,
        steam_presence: dict[int, tuple[str, int | None]],
        steam_friend_ids: set[int],
        now: float,
    ) -> None:
        """Prüft eine einzelne Lane und macht ggf. Vorschläge."""
        members = [m for m in vc.members if not m.bot]
        member_count = len(members)

        # Nur Lanes mit 1-4 Spielern (suchen noch Leute)
        if member_count < MIN_PLAYERS_FOR_SEARCH or member_count > MAX_PLAYERS_FOR_SEARCH:
            return

        # Cooldown prüfen
        last = self._lane_cooldowns.get(vc.id, 0)
        if now - last < LANE_SUGGESTION_COOLDOWN_SECONDS:
            return

        lane_member_ids = {m.id for m in members}
        avg_rank = self._get_lane_avg_rank(members)
        lane_label = self._get_lane_label(category_id)

        # Alle Channel-IDs in derselben Kategorie (für Activity-Lookup)
        cat = guild.get_channel(category_id)
        channel_ids = []
        if isinstance(cat, discord.CategoryChannel):
            channel_ids = [ch.id for ch in cat.voice_channels]
        else:
            channel_ids = [vc.id]

        # Aktivitäts-basierte Kandidaten finden
        candidates = await self._get_active_players(
            channel_ids, avg_rank, lane_member_ids,
        )

        # Steam-Freunde ergänzen die noch nicht im Kandidatenpool sind
        activity_ids = {c["user_id"] for c in candidates}
        for friend_uid in steam_friend_ids:
            if friend_uid in activity_ids or friend_uid in lane_member_ids:
                continue
            member = guild.get_member(friend_uid)
            if not member or member.bot:
                continue
            # Steam-Freunde ohne Voice-History werden mit Minimal-Daten aufgenommen
            candidates.append({
                "user_id": friend_uid,
                "sessions": 0,
                "activity_score": 0,
                "time_score": 0.5,
                "activity_norm": 0.0,
                "hour_match": False,
                "day_match": False,
                "steam_friend_only": True,
            })

        if not candidates:
            return

        # Rang-Kompatibilität filtern und Scoring
        scored: list[tuple[dict, float]] = []
        for cand in candidates:
            uid = cand["user_id"]
            member = guild.get_member(uid)
            if not member or member.bot:
                continue

            # Bereits in einem Voice-Channel? Skip.
            if member.voice and member.voice.channel:
                continue

            # Rang prüfen
            _, rank_val = self._get_user_rank(member)
            if avg_rank > 0 and rank_val > 0:
                if abs(rank_val - avg_rank) > RANK_TOLERANCE_SUGGESTIONS:
                    continue

            # Score berechnen
            score = cand["time_score"] * 30 + cand["activity_norm"] * 20

            # Steam-Freund-Bonus: Bot-Freundschaft = verifizierte Verbindung
            if uid in steam_friend_ids:
                score += 25

            # Steam-Bonus: Wer gerade Deadlock spielt, bekommt mehr Gewicht
            steam = steam_presence.get(uid)
            if steam:
                stage, minutes = steam
                if stage == "lobby":
                    score += 50  # In der Lobby = bester Kandidat
                elif stage == "match":
                    score += 30  # Im Match = könnte bald frei sein

            # Discord-Status Bonus
            if member.status in (discord.Status.online, discord.Status.idle):
                score += 15
            elif member.status == discord.Status.dnd:
                score += 5

            # Steam-Freunde ohne Voice-History: nur vorschlagen wenn sie
            # gerade aktiv in Deadlock spielen (Lobby oder Match),
            # aber NICHT im Discord-Voice sind – sonst kein Signal.
            if cand.get("steam_friend_only"):
                steam = steam_presence.get(uid)
                if not steam:
                    continue
                stage, _ = steam
                if stage not in {"lobby", "match"}:
                    continue

            scored.append((cand, score))

        if not scored:
            return

        # Top-Kandidaten sortieren
        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:MAX_SUGGESTIONS]

        # Embed bauen
        embed = self._build_suggestion_embed(
            guild, vc, members, lane_label, avg_rank, top, steam_presence, steam_friend_ids,
        )
        if embed:
            await output_channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=False,  # Keine Pings - nur freundliche Vorschläge
                    roles=False,
                    everyone=False,
                ),
            )
            self._lane_cooldowns[vc.id] = now
            log.info(
                "Spielervorschlag gesendet für %s (%d Kandidaten)",
                vc.name, len(top),
            )

    def _build_suggestion_embed(
        self,
        guild: discord.Guild,
        vc: discord.VoiceChannel,
        lane_members: list[discord.Member],
        lane_label: str,
        avg_rank: float,
        scored_candidates: list[tuple[dict, float]],
        steam_presence: dict[int, tuple[str, int | None]],
        steam_friend_ids: set[int] | None = None,
    ) -> discord.Embed | None:
        """Baut ein freundliches Vorschlags-Embed."""
        if not scored_candidates:
            return None

        member_names = ", ".join(m.display_name for m in lane_members[:5])
        slots_free = (vc.user_limit or 6) - len(lane_members)
        if slots_free <= 0:
            slots_free = 2  # Fallback

        embed = discord.Embed(
            title="\U0001f50d Mitspieler-Vorschläge",
            description=(
                f"In **{vc.name}** ({lane_label}) "
                f"{'ist' if len(lane_members) == 1 else 'sind'} gerade "
                f"**{member_names}** unterwegs und "
                f"{'sucht' if len(lane_members) == 1 else 'suchen'} noch "
                f"**{slots_free}** Mitspieler!\n\n"
                "Vielleicht haben diese Spieler ja Lust mitzuspielen:"
            ),
            color=discord.Color.blue(),
        )

        suggestion_lines = []
        for cand, score in scored_candidates:
            uid = cand["user_id"]
            member = guild.get_member(uid)
            if not member:
                continue

            _, rank_val = self._get_user_rank(member)
            rank_name = ""
            for role_id, (rname, rval) in DISCORD_RANK_ROLES.items():
                if rval == rank_val and rval > 0:
                    rank_name = rname
                    break

            # Status-Indikator
            steam = steam_presence.get(uid)
            if steam:
                stage, minutes = steam
                if stage == "lobby":
                    status = "\U0001f7e2 In der Deadlock-Lobby"
                elif stage == "match" and minutes:
                    status = f"\U0001f3ae Im Match (~{minutes}min)"
                else:
                    status = "\U0001f3ae Im Spiel"
            elif member.status == discord.Status.online:
                status = "\U0001f535 Online auf Discord"
            elif member.status == discord.Status.idle:
                status = "\U0001f7e0 Abwesend"
            else:
                status = "\u26aa Typischerweise jetzt aktiv"

            rank_str = f" ({rank_name})" if rank_name else ""
            sessions = cand.get("sessions", 0)
            friend_badge = " 🤝" if (steam_friend_ids and uid in steam_friend_ids) else ""
            session_str = (
                f"{sessions} Sessions in den letzten 14 Tagen"
                if sessions > 0
                else "Steam-Freund des Bots"
            )
            suggestion_lines.append(
                f"**{member.display_name}**{rank_str}{friend_badge}\n"
                f"{status} · {session_str}"
            )

        if not suggestion_lines:
            return None

        embed.add_field(
            name="\U0001f465 Mögliche Mitspieler",
            value="\n\n".join(suggestion_lines),
            inline=False,
        )

        link = f"https://discord.com/channels/{guild.id}/{vc.id}"
        embed.add_field(
            name="\u27a1\ufe0f Direkt joinen",
            value=f"[Hier klicken um beizutreten]({link})",
            inline=False,
        )

        embed.set_footer(
            text="Basierend auf Spielaktivität und Steam-Status der letzten 14 Tage"
        )

        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlayerFinder(bot))
