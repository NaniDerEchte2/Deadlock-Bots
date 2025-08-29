import discord
from discord.ext import commands
import random
import logging
from typing import Dict, List, Tuple, Optional
import sqlite3
import os
import asyncio
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class PlayerSelectionView(discord.ui.View):
    """Interactive View für Spielerauswahl bei zu vielen Teilnehmern"""
    
    def __init__(self, players: List[Tuple[discord.Member, str, int]], max_players: int = 12):
        super().__init__(timeout=60.0)
        self.players = players
        self.max_players = max_players
        self.selected_players = set()
        self.original_interaction = None
        
        # Erstelle Buttons für jeden Spieler
        for i, (member, rank_name, rank_value) in enumerate(players):
            if i >= 25:  # Discord limit für Buttons
                break
            button = PlayerButton(member, rank_name, rank_value, i)
            self.add_item(button)
        
        # Confirm Button
        confirm_button = discord.ui.Button(
            label=f"Match starten (0/{max_players})",
            style=discord.ButtonStyle.success,
            emoji="🎮",
            disabled=True,
            row=4
        )
        confirm_button.callback = self.confirm_selection
        self.add_item(confirm_button)
        
        # Random Button
        random_button = discord.ui.Button(
            label="Zufällige Auswahl",
            style=discord.ButtonStyle.secondary,
            emoji="🎲",
            row=4
        )
        random_button.callback = self.random_selection
        self.add_item(random_button)
        
    def update_confirm_button(self):
        """Aktualisiert den Confirm Button"""
        confirm_button = self.children[-2]  # Vorletztes Item
        selected_count = len(self.selected_players)
        confirm_button.label = f"Match starten ({selected_count}/{self.max_players})"
        confirm_button.disabled = selected_count < 4 or selected_count > self.max_players
        
    async def confirm_selection(self, interaction: discord.Interaction):
        """Startet Match mit ausgewählten Spielern"""
        selected_members = []
        for i in self.selected_players:
            if i < len(self.players):
                selected_members.append(self.players[i])
        
        self.stop()
        
        # Starte Team-Balancing mit ausgewählten Spielern
        cog = interaction.client.get_cog("DeadlockTeamBalancer")
        if cog:
            await interaction.response.edit_message(
                content=f"🎮 Starte Match mit {len(selected_members)} ausgewählten Spielern...",
                view=None,
                embed=None
            )
            await cog.process_team_balance(
                interaction, selected_members, 
                f"Ausgewählte Spieler Match", 
                create_channels=True
            )
        
    async def random_selection(self, interaction: discord.Interaction):
        """Wählt zufällig Spieler aus"""
        # Lösche aktuelle Auswahl
        self.selected_players.clear()
        
        # Wähle zufällig max_players Spieler
        random_indices = random.sample(range(len(self.players)), min(self.max_players, len(self.players)))
        self.selected_players.update(random_indices)
        
        # Update alle Buttons
        for item in self.children:
            if isinstance(item, PlayerButton):
                item.update_style(item.player_index in self.selected_players)
        
        self.update_confirm_button()
        
        await interaction.response.edit_message(
            content=f"🎲 {len(self.selected_players)} Spieler zufällig ausgewählt!",
            view=self
        )

class PlayerButton(discord.ui.Button):
    """Button für einzelne Spieler"""
    
    def __init__(self, member: discord.Member, rank_name: str, rank_value: int, player_index: int):
        self.member = member
        self.rank_name = rank_name
        self.rank_value = rank_value
        self.player_index = player_index
        
        # Button Style
        super().__init__(
            label=f"{member.display_name} ({rank_name})",
            style=discord.ButtonStyle.secondary,
            row=player_index // 5  # 5 Buttons pro Reihe
        )
    
    def update_style(self, selected: bool):
        """Update Button Style basierend auf Auswahl"""
        self.style = discord.ButtonStyle.success if selected else discord.ButtonStyle.secondary
    
    async def callback(self, interaction: discord.Interaction):
        """Toggle Spieler-Auswahl"""
        view = self.view
        
        if self.player_index in view.selected_players:
            # Spieler deselektieren
            view.selected_players.remove(self.player_index)
            self.update_style(False)
        else:
            # Spieler selektieren (wenn noch Platz)
            if len(view.selected_players) < view.max_players:
                view.selected_players.add(self.player_index)
                self.update_style(True)
            else:
                await interaction.response.send_message(
                    f"❌ Maximale Spielerzahl ({view.max_players}) bereits erreicht!",
                    ephemeral=True
                )
                return
        
        # Update Confirm Button
        view.update_confirm_button()
        
        selected_count = len(view.selected_players)
        await interaction.response.edit_message(
            content=f"👥 Spieler auswählen für Match ({selected_count}/{view.max_players} ausgewählt):",
            view=view
        )

class DeadlockTeamBalancer(commands.Cog):
    """Team-Balancing für Deadlock Custom Matches basierend auf Ranks"""

    def __init__(self, bot):
        self.bot = bot
        
        # Match-Verwaltung
        self.active_matches = {}  # {match_id: {"channels": [], "players": [], "start_time": datetime}}
        self.match_counter = 0
        
        # Kategorie für Match-Channels
        self.match_category_id = 1289721245281292290
        
        # TempVoice Casual Lane für Nachbesprechung
        self.casual_lane_id = 1330278323145801758
        
        # Deadlock Rank-System (wie im rank_voice_manager)
        self.deadlock_ranks = {
            "Obscurus": 0,
            "Initiate": 1,
            "Seeker": 2,
            "Alchemist": 3,
            "Arcanist": 4,
            "Ritualist": 5,
            "Emissary": 6,
            "Archon": 7,
            "Oracle": 8,
            "Phantom": 9,
            "Ascendant": 10,
            "Eternus": 11
        }
        
        # Discord Rollen-IDs zu Rang-Mapping (gleich wie im rank_voice_manager)
        self.discord_rank_roles = {
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
            1331458087349129296: ("Eternus", 11)
        }

    async def cog_load(self):
        """Wird beim Laden des Cogs aufgerufen"""
        logger.info("DeadlockTeamBalancer Cog erfolgreich geladen")
        print("✅ DeadlockTeamBalancer Cog geladen")

    def get_user_rank_from_roles(self, member: discord.Member) -> Tuple[str, int]:
        """Ermittelt Benutzer-Rang basierend auf Discord-Rollen (analog zum rank_voice_manager)"""
        highest_rank = ("Obscurus", 0)
        highest_rank_value = 0

        for role in member.roles:
            if role.id in self.discord_rank_roles:
                rank_name, rank_value = self.discord_rank_roles[role.id]
                if rank_value > highest_rank_value:
                    highest_rank = (rank_name, rank_value)
                    highest_rank_value = rank_value

        return highest_rank

    def get_rank_from_db(self, user_id: int) -> Tuple[str, int]:
        """Holt Rang aus der standalone_rank_bot Datenbank als Fallback"""
        try:
            db_path = os.path.join(os.path.dirname(__file__), '..', 'rank_bot', 'rank_data', 'standalone_rank_bot.db')
            if os.path.exists(db_path):
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT rank FROM user_ranks WHERE user_id = ?", (user_id,))
                    result = cursor.fetchone()
                    if result:
                        rank_name = result[0].title()
                        rank_value = self.deadlock_ranks.get(rank_name, 0)
                        return rank_name, rank_value
        except Exception as e:
            logger.warning(f"Fehler beim Abrufen des DB-Ranks für User {user_id}: {e}")
        
        return "Obscurus", 0

    def get_user_rank(self, member: discord.Member) -> Tuple[str, int]:
        """Kombiniert Discord-Rollen und DB-Rank für besten Rank"""
        # Zuerst Discord-Rollen prüfen
        role_rank_name, role_rank_value = self.get_user_rank_from_roles(member)
        
        # Falls keine Rolle gefunden, DB prüfen
        if role_rank_value == 0:
            db_rank_name, db_rank_value = self.get_rank_from_db(member.id)
            if db_rank_value > 0:
                return db_rank_name, db_rank_value
        
        return role_rank_name, role_rank_value

    def calculate_team_balance_score(self, team: List[Tuple[discord.Member, int]]) -> float:
        """Berechnet Balance-Score für ein Team (niedrigerer Score = bessere Balance)"""
        if not team:
            return float('inf')
        
        ranks = [rank_value for _, rank_value in team]
        avg_rank = sum(ranks) / len(ranks)
        variance = sum((rank - avg_rank) ** 2 for rank in ranks) / len(ranks)
        
        # Score kombiniert Durchschnitt und Varianz
        return avg_rank + (variance * 0.5)

    def generate_team_combinations(self, players: List[Tuple[discord.Member, str, int]], team_size: int) -> List[Tuple[List[Tuple[discord.Member, int]], List[Tuple[discord.Member, int]]]]:
        """Generiert alle möglichen Team-Kombinationen und findet die beste Balance"""
        from itertools import combinations
        
        if len(players) < 4:  # Minimum 4 Spieler (2v2)
            return []
        
        player_data = [(member, rank_value) for member, _, rank_value in players]
        
        best_combinations = []
        best_score_diff = float('inf')
        
        # Generiere alle möglichen Team-Kombinationen
        for team1_indices in combinations(range(len(player_data)), team_size):
            team1 = [player_data[i] for i in team1_indices]
            team2 = [player_data[i] for i in range(len(player_data)) if i not in team1_indices]
            
            if len(team2) < team_size:
                continue
            
            # Nimm die ersten team_size Spieler von team2
            team2 = team2[:team_size]
            
            # Berechne Balance-Scores
            score1 = self.calculate_team_balance_score(team1)
            score2 = self.calculate_team_balance_score(team2)
            score_diff = abs(score1 - score2)
            
            # Speichere bessere Kombinationen
            if score_diff < best_score_diff:
                best_score_diff = score_diff
                best_combinations = [(team1, team2)]
            elif score_diff == best_score_diff:
                best_combinations.append((team1, team2))
        
        return best_combinations[:5]  # Top 5 Kombinationen

    def create_team_embed(self, team1: List[Tuple[discord.Member, int]], team2: List[Tuple[discord.Member, int]], title: str = "🎯 Deadlock Team Balance") -> discord.Embed:
        """Erstellt ein Embed für die Team-Aufteilung"""
        embed = discord.Embed(title=title, color=0x00ff00)
        
        # Team 1
        team1_info = []
        team1_ranks = []
        for member, rank_value in team1:
            rank_name = self.get_rank_name_from_value(rank_value)
            team1_info.append(f"**{member.display_name}** - {rank_name} ({rank_value})")
            team1_ranks.append(rank_value)
        
        team1_avg = sum(team1_ranks) / len(team1_ranks) if team1_ranks else 0
        
        embed.add_field(
            name=f"🟠 Team Amber (Ø {team1_avg:.1f})",
            value="\n".join(team1_info),
            inline=True
        )
        
        # Team 2
        team2_info = []
        team2_ranks = []
        for member, rank_value in team2:
            rank_name = self.get_rank_name_from_value(rank_value)
            team2_info.append(f"**{member.display_name}** - {rank_name} ({rank_value})")
            team2_ranks.append(rank_value)
        
        team2_avg = sum(team2_ranks) / len(team2_ranks) if team2_ranks else 0
        
        embed.add_field(
            name=f"🔵 Team Sapphire (Ø {team2_avg:.1f})",
            value="\n".join(team2_info),
            inline=True
        )
        
        # Balance-Info
        balance_diff = abs(team1_avg - team2_avg)
        balance_emoji = "✅" if balance_diff < 1.0 else "⚠️" if balance_diff < 2.0 else "❌"
        
        embed.add_field(
            name="📊 Balance-Analyse",
            value=f"{balance_emoji} Rank-Unterschied: {balance_diff:.2f}\n"
                  f"Team 1 Varianz: {self.calculate_variance(team1_ranks):.2f}\n"
                  f"Team 2 Varianz: {self.calculate_variance(team2_ranks):.2f}",
            inline=False
        )
        
        return embed

    def calculate_variance(self, ranks: List[int]) -> float:
        """Berechnet Varianz der Ranks"""
        if not ranks:
            return 0
        avg = sum(ranks) / len(ranks)
        return sum((rank - avg) ** 2 for rank in ranks) / len(ranks)

    def get_rank_name_from_value(self, rank_value: int) -> str:
        """Konvertiert Rang-Wert zu Rang-Name"""
        for rank_name, value in self.deadlock_ranks.items():
            if value == rank_value:
                return rank_name
        return "Obscurus"

    @commands.group(name="balance", aliases=["bal", "teams"], invoke_without_command=True)
    async def balance_command(self, ctx):
        """Deadlock Team-Balancing Befehle"""
        embed = discord.Embed(
            title="⚖️ Deadlock Team Balancer",
            description="Erstellt ausbalancierte Teams basierend auf Deadlock-Rängen",
            color=0x0099ff
        )
        
        embed.add_field(
            name="📋 Verfügbare Befehle",
            value="`auto` - Automatisches Balancing (nur Anzeige)\n"
                  "`start` - Balancing mit automatischen Channels\n"
                  "`manual @user1 @user2 ...` - Manuelles Balancing\n"
                  "`voice` - Balancing aller User im Voice Channel\n"
                  "`status` - Zeigt User-Rank\n"
                  "`matches` - Zeigt aktive Matches\n"
                  "`end <match_id>` - Beendet Match + TempVoice Nachbesprechung",
            inline=False
        )
        
        embed.add_field(
            name="🎮 Verwendung",
            value="**Nur anzeigen**: `!balance auto` (4+ Spieler)\n**Mit Channels**: `!balance start` (4-12 Spieler)\n**Bei >12 Spielern**: Interaktive Auswahl mit Buttons",
            inline=False
        )
        
        await ctx.send(embed=embed)

    @balance_command.command(name="auto")
    async def auto_balance(self, ctx):
        """Automatisches Team-Balancing für alle User im Voice Channel"""
        # Prüfe ob User in Voice Channel ist
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("❌ Du musst in einem Voice Channel sein für Auto-Balance!")
            return
        
        channel = ctx.author.voice.channel
        members = [m for m in channel.members if not m.bot]
        
        if len(members) < 4:
            await ctx.send(f"❌ Mindestens 4 Spieler benötigt (aktuell: {len(members)})")
            return
        
        # Hole Ranks für alle Spieler
        players = []
        for member in members:
            rank_name, rank_value = self.get_user_rank(member)
            players.append((member, rank_name, rank_value))
        
        await self.process_team_balance(ctx, players, f"Auto-Balance für {channel.name}", create_channels=False)

    @balance_command.command(name="start")
    async def start_match(self, ctx):
        """Startet ein Match mit automatischen Voice Channels und User-Movement"""
        # Prüfe ob User in Voice Channel ist
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.send("❌ Du musst in einem Voice Channel sein für Match-Start!")
            return
        
        channel = ctx.author.voice.channel
        members = [m for m in channel.members if not m.bot]
        
        if len(members) < 4:
            await ctx.send(f"❌ Mindestens 4 Spieler benötigt (aktuell: {len(members)})")
            return
        
        # Hole Ranks für alle Spieler
        players = []
        for member in members:
            rank_name, rank_value = self.get_user_rank(member)
            players.append((member, rank_name, rank_value))
        
        # Wenn mehr als 12 Spieler, interaktive Auswahl
        if len(players) > 12:
            embed = discord.Embed(
                title="👥 Zu viele Spieler im Channel!",
                description=f"**{len(players)} Spieler** gefunden, aber Maximum ist **12**.\nWähle die Spieler für das Match aus:",
                color=discord.Color.orange()
            )
            
            # Zeige alle Spieler mit Ranks
            player_list = []
            for i, (member, rank_name, rank_value) in enumerate(players[:20], 1):  # Erste 20 zeigen
                player_list.append(f"{i}. **{member.display_name}** - {rank_name} ({rank_value})")
            
            if len(players) > 20:
                player_list.append(f"... und {len(players) - 20} weitere")
            
            embed.add_field(
                name="📋 Verfügbare Spieler",
                value="\n".join(player_list) if player_list else "Keine Spieler",
                inline=False
            )
            
            embed.add_field(
                name="🎯 Anleitung",
                value="• Klicke auf Spieler um sie auszuwählen\n• 🎲 für zufällige Auswahl\n• 🎮 um Match zu starten",
                inline=False
            )
            
            view = PlayerSelectionView(players, max_players=12)
            await ctx.send(embed=embed, view=view)
            return
        
        # Normal starten wenn 4-12 Spieler
        await self.process_team_balance(ctx, players, f"Match-Start für {channel.name}", create_channels=True)

    @balance_command.command(name="voice")
    async def voice_balance(self, ctx):
        """Team-Balancing für aktuellen Voice Channel (Alias für auto)"""
        await self.auto_balance(ctx)

    @balance_command.command(name="manual")
    async def manual_balance(self, ctx, *members: discord.Member):
        """Manuelles Team-Balancing für spezifische User"""
        if len(members) < 4:
            await ctx.send(f"❌ Mindestens 4 Spieler benötigt (angegeben: {len(members)})")
            return
        
        if len(members) > 12:
            await ctx.send(f"❌ Maximal 12 Spieler unterstützt (angegeben: {len(members)})")
            return
        
        # Hole Ranks für alle angegebenen Spieler
        players = []
        for member in members:
            rank_name, rank_value = self.get_user_rank(member)
            players.append((member, rank_name, rank_value))
        
        await self.process_team_balance(ctx, players, "Manuelles Team-Balance")

    async def create_match_channels(self, ctx, match_id: str) -> Tuple[Optional[discord.VoiceChannel], Optional[discord.VoiceChannel]]:
        """Erstellt zwei Voice Channels für das Match"""
        try:
            category = ctx.guild.get_channel(self.match_category_id)
            if not category:
                logger.error(f"Match-Kategorie {self.match_category_id} nicht gefunden")
                return None, None
            
            # Erstelle Team Channels
            team1_channel = await ctx.guild.create_voice_channel(
                name=f"🟠 Team Amber - Match {match_id}",
                category=category,
                reason=f"Deadlock Match {match_id} - Team Amber"
            )
            
            await asyncio.sleep(0.5)  # Rate limiting
            
            team2_channel = await ctx.guild.create_voice_channel(
                name=f"🔵 Team Sapphire - Match {match_id}",
                category=category,
                reason=f"Deadlock Match {match_id} - Team Sapphire"
            )
            
            logger.info(f"Match Channels erstellt: {team1_channel.name}, {team2_channel.name}")
            return team1_channel, team2_channel
            
        except discord.Forbidden:
            logger.error("Keine Berechtigung zum Erstellen von Voice Channels")
            return None, None
        except Exception as e:
            logger.error(f"Fehler beim Erstellen der Match Channels: {e}")
            return None, None

    async def move_teams_to_channels(self, team1: List[Tuple[discord.Member, int]], team2: List[Tuple[discord.Member, int]], 
                                   team1_channel: discord.VoiceChannel, team2_channel: discord.VoiceChannel) -> Tuple[int, int]:
        """Bewegt Spieler in ihre Team-Channels"""
        moved_team1 = 0
        moved_team2 = 0
        
        try:
            # Team 1 bewegen
            for member, _ in team1:
                if member.voice and member.voice.channel:
                    try:
                        await member.move_to(team1_channel, reason="Deadlock Team Balance - Team Amber")
                        moved_team1 += 1
                        await asyncio.sleep(0.5)  # Rate limiting
                    except discord.Forbidden:
                        logger.warning(f"Keine Berechtigung um {member.display_name} zu bewegen")
                    except discord.HTTPException as e:
                        logger.warning(f"Fehler beim Bewegen von {member.display_name}: {e}")
            
            # Team 2 bewegen
            for member, _ in team2:
                if member.voice and member.voice.channel:
                    try:
                        await member.move_to(team2_channel, reason="Deadlock Team Balance - Team Sapphire")
                        moved_team2 += 1
                        await asyncio.sleep(0.5)  # Rate limiting
                    except discord.Forbidden:
                        logger.warning(f"Keine Berechtigung um {member.display_name} zu bewegen")
                    except discord.HTTPException as e:
                        logger.warning(f"Fehler beim Bewegen von {member.display_name}: {e}")
        
        except Exception as e:
            logger.error(f"Allgemeiner Fehler beim Bewegen der Teams: {e}")
        
        return moved_team1, moved_team2

    async def process_team_balance(self, ctx, players: List[Tuple[discord.Member, str, int]], title: str, create_channels: bool = False):
        """Verarbeitet Team-Balancing und sendet Ergebnis"""
        # Sortiere Spieler nach Rank (für bessere Balance)
        players_sorted = sorted(players, key=lambda x: x[2], reverse=True)
        
        # Berechne optimale Team-Größe (2-6 pro Team)
        total_players = len(players)
        if total_players >= 12:
            team_size = 6  # 6v6
        elif total_players >= 10:
            team_size = 5  # 5v5  
        elif total_players >= 8:
            team_size = 4  # 4v4
        elif total_players >= 6:
            team_size = 3  # 3v3
        else:
            team_size = total_players // 2  # 2v2 oder 2v1
        
        # Generiere Team-Kombinationen
        combinations = self.generate_team_combinations(players_sorted, team_size)
        
        if not combinations:
            await ctx.send("❌ Keine ausbalancierten Teams gefunden!")
            return
        
        # Nimm beste Kombination
        best_team1, best_team2 = combinations[0]
        
        # Erstelle Match ID
        self.match_counter += 1
        match_id = f"{self.match_counter:03d}"
        
        # Erstelle und sende Embed
        embed = self.create_team_embed(best_team1, best_team2, f"{title} - Match {match_id}")
        
        # Zusätzliche Info wenn mehr Spieler vorhanden
        remaining_players = len(players) - (len(best_team1) + len(best_team2))
        if remaining_players > 0:
            embed.add_field(
                name="ℹ️ Info",
                value=f"{remaining_players} Spieler nicht in Teams eingeteilt (Reserve)",
                inline=False
            )
        
        # Channels erstellen wenn gewünscht
        if create_channels:
            team1_channel, team2_channel = await self.create_match_channels(ctx, match_id)
            
            if team1_channel and team2_channel:
                # Spieler bewegen
                moved_team1, moved_team2 = await self.move_teams_to_channels(
                    best_team1, best_team2, team1_channel, team2_channel
                )
                
                # Match verwalten
                self.active_matches[match_id] = {
                    "channels": [team1_channel.id, team2_channel.id],
                    "players": [m[0].id for m in best_team1 + best_team2],
                    "start_time": datetime.now(),
                    "guild_id": ctx.guild.id,
                    "original_channel": ctx.author.voice.channel.id if ctx.author.voice else None
                }
                
                embed.add_field(
                    name="🎮 Match Channels",
                    value=f"**{team1_channel.name}**: {moved_team1}/{len(best_team1)} Spieler bewegt\n"
                          f"**{team2_channel.name}**: {moved_team2}/{len(best_team2)} Spieler bewegt\n"
                          f"Match ID: `{match_id}` - Verwende `!balance end {match_id}` zum Beenden",
                    inline=False
                )
                
                if moved_team1 < len(best_team1) or moved_team2 < len(best_team2):
                    embed.add_field(
                        name="⚠️ Hinweis",
                        value="Nicht alle Spieler konnten bewegt werden (nicht in Voice oder keine Berechtigung)",
                        inline=False
                    )
                
                logger.info(f"Match {match_id} gestartet: {moved_team1 + moved_team2} Spieler bewegt")
            else:
                embed.add_field(
                    name="❌ Channel-Erstellung fehlgeschlagen",
                    value="Channels konnten nicht erstellt werden (Berechtigungen prüfen)",
                    inline=False
                )
        
        await ctx.send(embed=embed)

    @balance_command.command(name="status")
    async def user_status(self, ctx, member: discord.Member = None):
        """Zeigt Rank-Status eines Users"""
        if member is None:
            member = ctx.author
        
        rank_name, rank_value = self.get_user_rank(member)
        role_rank_name, role_rank_value = self.get_user_rank_from_roles(member)
        db_rank_name, db_rank_value = self.get_rank_from_db(member.id)
        
        embed = discord.Embed(
            title=f"📊 Rank-Status: {member.display_name}",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="🎯 Aktueller Rank",
            value=f"**{rank_name}** (Wert: {rank_value})",
            inline=True
        )
        
        embed.add_field(
            name="🎭 Discord-Rollen-Rank",
            value=f"{role_rank_name} ({role_rank_value})" if role_rank_value > 0 else "Keine Rolle",
            inline=True
        )
        
        embed.add_field(
            name="🗃️ Datenbank-Rank",
            value=f"{db_rank_name} ({db_rank_value})" if db_rank_value > 0 else "Nicht in DB",
            inline=True
        )
        
        await ctx.send(embed=embed)

    @balance_command.command(name="matches")
    async def show_active_matches(self, ctx):
        """Zeigt alle aktiven Matches"""
        if not self.active_matches:
            await ctx.send("📭 Keine aktiven Matches")
            return
        
        embed = discord.Embed(
            title="🎮 Aktive Deadlock Matches",
            color=discord.Color.green()
        )
        
        for match_id, match_data in self.active_matches.items():
            guild = self.bot.get_guild(match_data["guild_id"])
            if not guild:
                continue
            
            channels_info = []
            for channel_id in match_data["channels"]:
                channel = guild.get_channel(channel_id)
                if channel:
                    member_count = len([m for m in channel.members if not m.bot])
                    channels_info.append(f"{channel.name}: {member_count} Spieler")
                else:
                    channels_info.append(f"Channel {channel_id}: Gelöscht")
            
            start_time = match_data["start_time"]
            duration = datetime.now() - start_time
            duration_str = f"{duration.seconds // 60}min {duration.seconds % 60}s"
            
            embed.add_field(
                name=f"Match {match_id}",
                value=f"**Dauer**: {duration_str}\n" + "\n".join(channels_info),
                inline=True
            )
        
        await ctx.send(embed=embed)

    async def trigger_tempvoice_creation(self, ctx, match_id: str) -> Optional[discord.VoiceChannel]:
        """Triggert TempVoice Channel-Erstellung über Casual Lane"""
        try:
            casual_lane = ctx.guild.get_channel(self.casual_lane_id)
            if not casual_lane:
                logger.error(f"Casual Lane {self.casual_lane_id} nicht gefunden")
                return None
            
            # Bot User temporär in Casual Lane bewegen um TempVoice zu triggern
            bot_member = ctx.guild.me
            if not bot_member:
                logger.error("Bot Member nicht gefunden")
                return None
            
            # Bot in Casual Lane bewegen
            try:
                await bot_member.move_to(casual_lane, reason=f"TempVoice für Match {match_id} triggern")
                logger.info(f"Bot in Casual Lane bewegt um TempVoice zu triggern")
                
                # Kurz warten damit TempVoice reagieren kann
                await asyncio.sleep(2)
                
                # Prüfe ob neuer Channel erstellt wurde (TempVoice sollte reagiert haben)
                # Neue Channels in der Kategorie suchen
                category = casual_lane.category
                if category:
                    for channel in category.voice_channels:
                        # Wenn Bot der einzige im Channel ist, ist es wahrscheinlich der neue TempVoice Channel
                        if (len(channel.members) == 1 and 
                            bot_member in channel.members and 
                            channel.id != self.casual_lane_id):
                            logger.info(f"TempVoice Channel gefunden: {channel.name}")
                            return channel
                
                # Fallback: Bot wieder aus Casual Lane bewegen falls kein Channel gefunden
                try:
                    await bot_member.move_to(None, reason="TempVoice nicht erfolgreich - Bot disconnecten")
                except:
                    pass
                
                logger.warning("Kein neuer TempVoice Channel gefunden")
                return None
                
            except discord.Forbidden:
                logger.error("Keine Berechtigung um Bot zu bewegen")
                return None
            except Exception as e:
                logger.error(f"Fehler beim Bewegen des Bots: {e}")
                return None
            
        except Exception as e:
            logger.error(f"Fehler beim TempVoice triggern: {e}")
            return None

    async def move_players_to_tempvoice(self, match_data: dict, tempvoice_channel: discord.VoiceChannel, ctx) -> Tuple[int, List[str]]:
        """Bewegt alle Match-Spieler in den TempVoice Channel"""
        moved_players = 0
        failed_moves = []
        
        try:
            for player_id in match_data["players"]:
                try:
                    member = ctx.guild.get_member(player_id)
                    if member and member.voice and member.voice.channel:
                        # Prüfe ob User noch in einem der Match-Channels ist
                        current_channel_id = member.voice.channel.id
                        if current_channel_id in match_data["channels"]:
                            await member.move_to(tempvoice_channel, reason=f"Match {match_data.get('match_id', 'unknown')} - Nachbesprechung")
                            moved_players += 1
                            await asyncio.sleep(0.3)  # Rate limiting
                        else:
                            # Spieler ist bereits woanders - nicht bewegen
                            logger.debug(f"Spieler {member.display_name} nicht in Match-Channel - überspringe")
                    else:
                        failed_moves.append(f"{member.display_name if member else f'ID {player_id}'} (nicht in Voice)")
                        
                except discord.Forbidden:
                    failed_moves.append(f"{member.display_name if member else f'ID {player_id}'} (keine Berechtigung)")
                except discord.HTTPException as e:
                    failed_moves.append(f"{member.display_name if member else f'ID {player_id}'} (Fehler: {e})")
                except Exception as e:
                    failed_moves.append(f"ID {player_id} (Unbekannter Fehler: {e})")
        
        except Exception as e:
            logger.error(f"Allgemeiner Fehler beim Bewegen zur TempVoice: {e}")
        
        return moved_players, failed_moves

    async def create_nachbesprechung_lane(self, ctx, match_id: str) -> Optional[discord.VoiceChannel]:
        """Erstellt eine Nachbesprechungs-Lane in der gleichen Kategorie"""
        try:
            category = ctx.guild.get_channel(self.match_category_id)
            if not category:
                logger.error(f"Match-Kategorie {self.match_category_id} nicht gefunden")
                return None
            
            # Erstelle Nachbesprechungs-Lane
            nachbesprechung_channel = await ctx.guild.create_voice_channel(
                name=f"💬 Nachbesprechung - Match {match_id}",
                category=category,
                reason=f"Nachbesprechung für Match {match_id}"
            )
            
            logger.info(f"Nachbesprechungs-Lane erstellt: {nachbesprechung_channel.name}")
            return nachbesprechung_channel
            
        except discord.Forbidden:
            logger.error("Keine Berechtigung zum Erstellen von Voice Channels")
            return None
        except Exception as e:
            logger.error(f"Fehler beim Erstellen der Nachbesprechungs-Lane: {e}")
            return None

    async def move_players_to_lane(self, match_data: dict, nachbesprechung_channel: discord.VoiceChannel, ctx) -> Tuple[int, List[str]]:
        """Bewegt alle Match-Spieler in die Nachbesprechungs-Lane"""
        moved_players = 0
        failed_moves = []
        
        try:
            for player_id in match_data["players"]:
                try:
                    member = ctx.guild.get_member(player_id)
                    if member and member.voice and member.voice.channel:
                        # Prüfe ob User noch in einem der Match-Channels ist
                        current_channel_id = member.voice.channel.id
                        if current_channel_id in match_data["channels"]:
                            await member.move_to(nachbesprechung_channel, reason=f"Match {match_data.get('match_id', 'unknown')} - Nachbesprechung")
                            moved_players += 1
                            await asyncio.sleep(0.3)  # Rate limiting
                        else:
                            # Spieler ist bereits woanders - nicht bewegen
                            logger.debug(f"Spieler {member.display_name} nicht in Match-Channel - überspringe")
                    else:
                        failed_moves.append(f"{member.display_name if member else f'ID {player_id}'} (nicht in Voice)")
                        
                except discord.Forbidden:
                    failed_moves.append(f"{member.display_name if member else f'ID {player_id}'} (keine Berechtigung)")
                except discord.HTTPException as e:
                    failed_moves.append(f"{member.display_name if member else f'ID {player_id}'} (Fehler: {e})")
                except Exception as e:
                    failed_moves.append(f"ID {player_id} (Unbekannter Fehler: {e})")
        
        except Exception as e:
            logger.error(f"Allgemeiner Fehler beim Bewegen zur Nachbesprechungs-Lane: {e}")
        
        return moved_players, failed_moves

    @balance_command.command(name="end")
    async def end_match(self, ctx, match_id: str = None, skip_debrief: bool = False):
        """Beendet ein Match und sammelt alle Spieler in einer Lane für Nachbesprechung"""
        if not match_id:
            await ctx.send("❌ Bitte Match ID angeben: `!balance end <match_id>`")
            return
        
        if match_id not in self.active_matches:
            await ctx.send(f"❌ Match `{match_id}` nicht gefunden")
            return
        
        match_data = self.active_matches[match_id]
        match_data["match_id"] = match_id  # Für Logging
        
        # 1. Erstelle Lane für Nachbesprechung in der gleichen Kategorie
        nachbesprechung_channel = None
        moved_to_lane = 0
        failed_lane_moves = []
        
        if not skip_debrief:
            nachbesprechung_channel = await self.create_nachbesprechung_lane(ctx, match_id)
            
            if nachbesprechung_channel:
                # 2. Bewege alle Spieler zur Nachbesprechungs-Lane
                moved_to_lane, failed_lane_moves = await self.move_players_to_lane(
                    match_data, nachbesprechung_channel, ctx
                )
                
                # Kurz warten damit alle Spieler Zeit haben anzukommen
                await asyncio.sleep(2)
        
        # 3. Lösche Match-Channels
        deleted_channels = []
        failed_channels = []
        
        for channel_id in match_data["channels"]:
            try:
                channel = ctx.guild.get_channel(channel_id)
                if channel:
                    await channel.delete(reason=f"Match {match_id} beendet")
                    deleted_channels.append(channel.name)
                    await asyncio.sleep(0.5)  # Rate limiting
                else:
                    failed_channels.append(f"Channel {channel_id} (bereits gelöscht)")
            except discord.Forbidden:
                failed_channels.append(f"Channel {channel_id} (keine Berechtigung)")
            except Exception as e:
                failed_channels.append(f"Channel {channel_id} (Fehler: {e})")
        
        # 4. Match aus Liste entfernen
        del self.active_matches[match_id]
        
        # 5. Bestätigung senden
        embed = discord.Embed(
            title=f"🏁 Match {match_id} beendet",
            color=discord.Color.green() if nachbesprechung_channel else discord.Color.red()
        )
        
        if nachbesprechung_channel:
            embed.add_field(
                name="💬 Nachbesprechungs-Lane",
                value=f"**Channel**: {nachbesprechung_channel.mention}\n"
                      f"**Spieler bewegt**: {moved_to_lane}/{len(match_data['players'])}\n"
                      f"*Lane wurde in derselben Kategorie erstellt für die Nachbesprechung*",
                inline=False
            )
            
            if failed_lane_moves:
                embed.add_field(
                    name="⚠️ Nicht zur Nachbesprechung bewegt",
                    value="\n".join(failed_lane_moves[:5]) + (f"\n... und {len(failed_lane_moves)-5} weitere" if len(failed_lane_moves) > 5 else ""),
                    inline=False
                )
        else:
            embed.add_field(
                name="❌ Nachbesprechungs-Lane",
                value="Nachbesprechungs-Lane konnte nicht erstellt werden",
                inline=False
            )
        
        if deleted_channels:
            embed.add_field(
                name="🗑️ Gelöschte Match-Channels",
                value="\n".join(deleted_channels),
                inline=False
            )
        
        if failed_channels:
            embed.add_field(
                name="⚠️ Nicht gelöscht",
                value="\n".join(failed_channels),
                inline=False
            )
        
        start_time = match_data["start_time"]
        duration = datetime.now() - start_time
        duration_str = f"{duration.seconds // 60}min {duration.seconds % 60}s"
        
        embed.add_field(
            name="📊 Match-Statistiken",
            value=f"**Dauer**: {duration_str}\n**Spieler**: {len(match_data['players'])}",
            inline=False
        )
        
        await ctx.send(embed=embed)
        logger.info(f"Match {match_id} beendet - {len(deleted_channels)} Channels gelöscht, {moved_to_lane} Spieler zur Lane bewegt")

    @balance_command.command(name="cleanup")
    @commands.has_permissions(manage_channels=True)
    async def cleanup_old_matches(self, ctx, hours: int = 2):
        """Löscht automatisch alte Match-Channels (Admin only)"""
        if hours < 1 or hours > 24:
            await ctx.send("❌ Stunden müssen zwischen 1-24 liegen")
            return
        
        cutoff_time = datetime.now() - timedelta(hours=hours)
        old_matches = []
        
        for match_id, match_data in list(self.active_matches.items()):
            if match_data["start_time"] < cutoff_time:
                old_matches.append((match_id, match_data))
        
        if not old_matches:
            await ctx.send(f"🧹 Keine Matches älter als {hours} Stunden gefunden")
            return
        
        cleaned_count = 0
        for match_id, match_data in old_matches:
            # Channels löschen
            for channel_id in match_data["channels"]:
                try:
                    channel = ctx.guild.get_channel(channel_id)
                    if channel:
                        await channel.delete(reason=f"Automatische Bereinigung - Match {match_id} > {hours}h alt")
                        cleaned_count += 1
                        await asyncio.sleep(0.5)
                except:
                    pass  # Ignoriere Fehler bei Cleanup
            
            # Match entfernen
            del self.active_matches[match_id]
        
        await ctx.send(f"🧹 {len(old_matches)} alte Matches bereinigt ({cleaned_count} Channels gelöscht)")
        logger.info(f"Cleanup: {len(old_matches)} Matches bereinigt")

    async def cog_command_error(self, ctx, error):
        """Behandelt Befehl-Fehler innerhalb des Cogs"""
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Unzureichende Berechtigungen für diesen Befehl.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("❌ Ungültige Argumente. Verwende `!balance help` für Hilfe.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("❌ Ein oder mehrere Benutzer wurden nicht gefunden.")
        else:
            logger.error(f"Unerwarteter Fehler in {ctx.command}: {error}")
            await ctx.send("❌ Ein unerwarteter Fehler ist aufgetreten.")

async def setup(bot):
    """Setup-Funktion für das Cog"""
    await bot.add_cog(DeadlockTeamBalancer(bot))
    logger.info("DeadlockTeamBalancer Cog hinzugefügt")

async def teardown(bot):
    """Teardown-Funktion für das Cog"""
    try:
        cog = bot.get_cog("DeadlockTeamBalancer")
        if cog:
            await bot.remove_cog("DeadlockTeamBalancer")
        logger.info("DeadlockTeamBalancer Cog entfernt")
    except Exception as e:
        logger.error(f"Fehler beim Entfernen des DeadlockTeamBalancer Cogs: {e}")