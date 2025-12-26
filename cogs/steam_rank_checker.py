"""
Steam Rank Checker - LFG System
Überwacht einen Discord-Textkanal und pingt Spieler basierend auf Rank und Steam-Online-Status
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Dict, List, Optional, Set, Tuple

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

# Rank-Matching: wie viele Ränge Unterschied sind erlaubt?
RANK_TOLERANCE = int(os.getenv("LFG_RANK_TOLERANCE", "2"))  # +/- 2 Ränge (Grind-Modus)

# LFG Trigger-Wörter (case-insensitive)
LFG_TRIGGERS = [
    "lfg", "lf game", "looking for game", "suche mitspieler",
    "suche spieler", "wer will spielen", "jemand bock"
]

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
        if not self._is_lfg_message(message.content):
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

        # Antwort erstellen
        if not matching_players:
            await thinking_msg.edit(
                content=f"😔 Keine Spieler im Rang-Bereich **{author_rank_name} ±{RANK_TOLERANCE}** sind derzeit online in Deadlock."
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
        embed = discord.Embed(
            title="🎮 Verfügbare Spieler gefunden!",
            description=f"Spieler im Rang-Bereich **{author_rank_name} ±{RANK_TOLERANCE}**",
            color=discord.Color.green()
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

        embed.set_footer(text=f"Angefordert von {message.author.display_name}")
        embed.timestamp = message.created_at

        # Nachricht mit Pings
        mention_text = " ".join(mentions[:10])  # Max 10 Mentions um Discord-Limits zu beachten
        if len(mentions) > 10:
            mention_text += f"\n... und {len(mentions) - 10} weitere"

        await thinking_msg.edit(
            content=f"{message.author.mention} sucht Mitspieler!\n{mention_text}",
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
