"""
Standalone Deadlock Rank Bot - CLEAN VERSION
Separater Bot nur für Rank-Management mit Dropdown-Interface
Ohne Debug/Cleanup Funktionen - nur Core Features
"""

import discord
from discord.ext import commands, tasks
import sqlite3
import asyncio
from datetime import datetime, timedelta
import os
import logging
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))
from utils.deadlock_db import DB_PATH

# NEU (direkt nach den Imports einfügen oder vorhandenes load_dotenv ersetzen):
from dotenv import load_dotenv
load_dotenv(r"C:\Users\Nani-Admin\Documents\.env")  # lädt immer diese .env, unabhängig vom aktuellen User


# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('StandaloneRankBot')

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)

async def restore_persistent_views():
    """Restore persistent views after bot is ready"""
    logger.info("Restoring persistent views...")
    
    # Lade alle persistent views aus der Datenbank und re-registriere sie
    persistent_views = load_persistent_views()
    
    for message_id, channel_id, guild_id, view_type, user_id in persistent_views:
        try:
            guild = bot.get_guild(int(guild_id))
            if not guild:
                logger.warning(f"Guild {guild_id} not found for persistent view")
                continue
            
            if view_type == 'server_rank_select':
                view = ServerRankSelectView(guild)
                bot.add_view(view, message_id=int(message_id))
                logger.info(f"Re-registered ServerRankSelectView for message {message_id}")
            
            elif view_type == 'dm_rank_select':
                # DM Rank Select View wiederherstellen mit ID-based approach
                if user_id:
                    # Neue Views mit user_id
                    try:
                        view = RankSelectView(int(user_id), int(guild_id), persistent=True)
                        bot.add_view(view, message_id=int(message_id))
                        logger.info(f"Re-registered DM RankSelectView for user {user_id} (message {message_id})")
                    except Exception as e:
                        logger.warning(f"Failed to restore DM view for user {user_id}: {e}")
                else:
                    # Alte Views ohne user_id - versuche user_id aus channel_id zu extrahieren
                    try:
                        logger.info(f"Attempting to restore legacy DM view {message_id}, channel_id: {channel_id}")
                        channel = bot.get_channel(int(channel_id))
                        logger.info(f"Channel found: {channel}, type: {type(channel)}")
                        
                        if channel and hasattr(channel, 'recipient'):
                            # DM Kanal gefunden
                            logger.info(f"DM channel recipient: {channel.recipient.id}")
                            view = RankSelectView(channel.recipient.id, int(guild_id), persistent=True)
                            bot.add_view(view, message_id=int(message_id))
                            logger.info(f"Re-registered legacy DM RankSelectView for channel {channel_id} (message {message_id})")
                        else:
                            logger.warning(f"Could not restore legacy DM view {message_id} - channel not found or not DM")
                            # Versuche als Fallback eine generische View zu erstellen
                            logger.info(f"Attempting fallback restoration for message {message_id}")
                            try:
                                # Dummy user_id verwenden - View wird beim ersten Callback die echte user_id laden
                                view = RankSelectView(0, int(guild_id), persistent=True)
                                bot.add_view(view, message_id=int(message_id))
                                logger.info(f"Re-registered fallback DM RankSelectView for message {message_id}")
                            except Exception as fallback_e:
                                logger.error(f"Fallback restoration also failed for {message_id}: {fallback_e}")
                    except Exception as e:
                        logger.error(f"Failed to restore legacy DM view {message_id}: {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                
        except Exception as e:
            logger.error(f"Failed to restore persistent view {message_id}: {e}")
    
    logger.info("Persistent views restoration completed")

# Deadlock-Ränge (alle kleingeschrieben)
ranks = [
    "initiate", "seeker", "alchemist", "arcanist", "ritualist",
    "emissary", "archon", "oracle", "phantom", "ascendant", "eternus"
]

# Standard Rang-Intervalle (in Tagen)
RANK_INTERVALS = {
    "initiate": 45, "seeker": 45, "alchemist": 45, "arcanist": 45, "ritualist": 45,
    "emissary": 45, "archon": 45, "oracle": 45, "phantom": 60, "ascendant": 60, "eternus": 60
}

# Konfiguration - Diese IDs müssen angepasst werden
RANK_MESSAGE_ID = 1332790527145414657
ETHERNUS_RANK_ROLE_ID = 1331458087349129296
ENGLISH_ONLY_ROLE_ID = 1309741866098491479
NO_DEADLOCK_ROLE_ID = 1397676560231698502
NO_NOTIFICATION_ROLE_ID = 1397688110959165462
PHANTOM_NOTIFICATION_CHANNEL_ID = 1374364800817303632
RANK_SELECTION_CHANNEL_ID = 1398021105339334666  # Channel für automatische View-Wiederherstellung

# Test-User System - für normalen Betrieb leer lassen
test_users = []

# Deutsche Uhrzeiten (8-22 Uhr)
NOTIFICATION_START_HOUR = 8
NOTIFICATION_END_HOUR = 22

# Database setup
db_path = str(DB_PATH)

def init_database():
    """Initialisiert die SQLite Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_data (
                user_id TEXT PRIMARY KEY,
                custom_interval INTEGER,
                paused_until TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                rank TEXT NOT NULL,
                notification_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                count INTEGER DEFAULT 1
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                rank TEXT NOT NULL,
                queue_date TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed BOOLEAN DEFAULT FALSE
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS persistent_views (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                view_type TEXT NOT NULL,
                user_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Neue Tabelle für DM Response Tracking
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS dm_response_tracking (
                user_id TEXT PRIMARY KEY,
                last_dm_sent TIMESTAMP NOT NULL,
                response_count INTEGER DEFAULT 0,
                last_response TIMESTAMP,
                status TEXT DEFAULT 'pending'
            )
        ''')
        
        # Migration: Füge user_id Spalte hinzu falls sie nicht existiert
        try:
            cursor.execute('ALTER TABLE persistent_views ADD COLUMN user_id TEXT')
            logger.info("Added user_id column to persistent_views table")
        except sqlite3.OperationalError:
            # Spalte existiert bereits
            pass
        
        conn.commit()

# Database helper functions
def get_user_data(user_id: str) -> dict:
    """Lädt User-Daten aus der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT custom_interval, paused_until FROM user_data WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if result:
            custom_interval, paused_until = result
            return {
                'custom_interval': custom_interval,
                'paused_until': paused_until
            }
        return {}

def save_user_data(user_id: str, data: dict):
    """Speichert User-Daten in der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO user_data (user_id, custom_interval, paused_until, updated_at)
            VALUES (?, ?, ?, ?)
        ''', (user_id, data.get('custom_interval'), data.get('paused_until'), datetime.now().isoformat()))
        conn.commit()

def save_persistent_view(message_id: str, channel_id: str, guild_id: str, view_type: str, user_id: str = None):
    """Speichert persistent view in der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO persistent_views (message_id, channel_id, guild_id, view_type, user_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (message_id, channel_id, guild_id, view_type, user_id))
        conn.commit()

def remove_persistent_view(message_id: str):
    """Entfernt eine persistent view aus der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM persistent_views WHERE message_id = ?', (message_id,))
        conn.commit()

def load_persistent_views():
    """Lädt alle persistent views aus der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        # Prüfe zuerst ob user_id Spalte existiert
        cursor.execute("PRAGMA table_info(persistent_views)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'user_id' in columns:
            cursor.execute('SELECT message_id, channel_id, guild_id, view_type, user_id FROM persistent_views')
        else:
            # Fallback für alte DB ohne user_id Spalte
            cursor.execute('SELECT message_id, channel_id, guild_id, view_type FROM persistent_views')
            results = cursor.fetchall()
            # Füge None als user_id hinzu für Kompatibilität
            return [(*row, None) for row in results]
        
        return cursor.fetchall()

def cleanup_old_views(guild_id: str, view_type: str):
    """Entfernt alte Views für eine Guild/View-Type Kombination"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM persistent_views 
            WHERE guild_id = ? AND view_type = ?
        ''', (guild_id, view_type))
        deleted_count = cursor.rowcount
        conn.commit()
        return deleted_count

# Utility functions
def get_user_current_rank(user):
    """Ermittelt den aktuellen Rang eines Users basierend auf seinen Rollen"""
    for role in user.roles:
        role_name_lower = role.name.lower()
        if role_name_lower in ranks:
            return role_name_lower
    return None

async def remove_all_rank_roles(member, guild):
    """Entfernt alle Rang-Rollen von einem Member"""
    for role_name in ranks:
        role = discord.utils.get(guild.roles, name=role_name.capitalize())
        if role and role in member.roles:
            try:
                await member.remove_roles(role)
            except:
                pass

# View Classes
class RankSelectView(discord.ui.View):
    def __init__(self, user_id, guild_id, persistent=False):
        super().__init__(timeout=None if persistent else 900)  # Kein timeout für persistent views
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        self.persistent = persistent
        self.add_item(RankSelectDropdown(user_id, guild_id))
        self.add_item(IntervalSelectDropdown(user_id))
        self.add_item(NoNotificationButton(user_id, guild_id))
        self.add_item(NoDeadlockButton(user_id, guild_id))
        self.add_item(FinishedButton(user_id, guild_id))

class RankSelectViewWithCustomIDs(discord.ui.View):
    def __init__(self, user_id, guild_id, rank_select_id, interval_select_id, no_notification_id, no_deadlock_id, finished_id):
        super().__init__(timeout=None)  # Persistent view
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        self.add_item(RankSelectDropdownWithCustomID(user_id, guild_id, rank_select_id))
        self.add_item(IntervalSelectDropdownWithCustomID(user_id, interval_select_id))
        self.add_item(NoNotificationButtonWithCustomID(user_id, guild_id, no_notification_id))
        self.add_item(NoDeadlockButtonWithCustomID(user_id, guild_id, no_deadlock_id))
        self.add_item(FinishedButtonWithCustomID(user_id, guild_id, finished_id))

class RankSelectViewFromMessage(discord.ui.View):
    def __init__(self, user_id, guild_id, original_components):
        super().__init__(timeout=None)  # Persistent view
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        
        # Kopiere alle original Components mit ihren Eigenschaften
        for row in original_components:
            for component in row.children:
                if isinstance(component, discord.SelectMenu):
                    # Kopiere Select mit original Eigenschaften
                    if len(component.options) >= 6:  # Rank Select (hat viele Optionen)
                        dropdown = RankSelectDropdownFromOriginal(user_id, guild_id, component)
                    else:  # Interval Select (hat wenige Optionen)
                        dropdown = IntervalSelectDropdownFromOriginal(user_id, component)
                    self.add_item(dropdown)
                elif isinstance(component, discord.Button):
                    # Kopiere Button mit original Eigenschaften
                    if "Keine Benachrichtigung" in component.label:
                        button = NoNotificationButtonFromOriginal(user_id, guild_id, component)
                    elif "kein Deadlock" in component.label:
                        button = NoDeadlockButtonFromOriginal(user_id, guild_id, component)
                    elif "Fertig" in component.label:
                        button = FinishedButtonFromOriginal(user_id, guild_id, component)
                    else:
                        continue  # Unbekannter Button
                    self.add_item(button)

class RankSelectDropdown(discord.ui.Select):
    def __init__(self, user_id, guild_id):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        
        # Erstelle Optionen für jeden Rang (ohne Emojis da Guild erst zur Laufzeit geladen wird)
        options = []
        for rank in ranks:
            option = discord.SelectOption(
                label=rank.capitalize(),
                value=rank,
                description=f"Setze {rank.capitalize()} als deinen Rang"
            )
            options.append(option)
        
        super().__init__(
            placeholder="🎮 Wähle deinen aktuellen Deadlock-Rang...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="dm_rank_select"
        )
    
    async def callback(self, interaction):
        selected_rank = self.values[0]
        
        # Objekte zur Laufzeit laden
        guild = bot.get_guild(self.guild_id)
        member = guild.get_member(self.user_id) if guild else None
        
        if not guild or not member:
            await interaction.response.send_message("❌ Fehler: Server oder User nicht gefunden.", ephemeral=True)
            return
        
        # Alle Rang-Rollen entfernen
        await remove_all_rank_roles(member, guild)
        
        # Neue Rolle hinzufügen
        role = discord.utils.get(guild.roles, name=selected_rank.capitalize())
        if not role:
            role = await guild.create_role(name=selected_rank.capitalize())
        await member.add_roles(role)
        
        # Phantom+ Benachrichtigung senden
        if selected_rank in ["phantom", "ascendant", "eternus"]:
            notification_channel = self.guild.get_channel(PHANTOM_NOTIFICATION_CHANNEL_ID)
            if notification_channel:
                rank_emoji = discord.utils.get(self.guild.emojis, name=selected_rank)
                emoji_display = str(rank_emoji) if rank_emoji else ""
                
                notification_embed = discord.Embed(
                    title="🔥 Phantom+ Rang Update",
                    description=f"{emoji_display} **{member.display_name}** hat sich den Rang **{selected_rank.capitalize()}** gegeben!",
                    color=0xff6b35
                )
                notification_embed.add_field(
                    name="User", 
                    value=f"{member.mention} ({member.id})", 
                    inline=True
                )
                notification_embed.add_field(
                    name="Rang", 
                    value=f"{emoji_display} {selected_rank.capitalize()}", 
                    inline=True
                )
                notification_embed.timestamp = datetime.now()
                
                try:
                    await notification_channel.send(embed=notification_embed)
                except:
                    pass
        
        # Bestätigung senden ohne das View zu entfernen
        rank_emoji = discord.utils.get(guild.emojis, name=selected_rank)
        await interaction.response.send_message(
            f"✅ {rank_emoji} Rang erfolgreich auf **{selected_rank.capitalize()}** gesetzt!",
            ephemeral=True
        )

class IntervalSelectDropdown(discord.ui.Select):
    def __init__(self, user_id):
        self.user_id = int(user_id)
        
        options = [
            discord.SelectOption(
                label="30 Tage",
                value="30",
                description="Alle 30 Tage nach Rang fragen",
                emoji="📅"
            ),
            discord.SelectOption(
                label="45 Tage",
                value="45", 
                description="Alle 45 Tage nach Rang fragen",
                emoji="📆"
            ),
            discord.SelectOption(
                label="60 Tage",
                value="60",
                description="Alle 60 Tage nach Rang fragen", 
                emoji="🗓️"
            ),
            discord.SelectOption(
                label="90 Tage",
                value="90",
                description="Alle 90 Tage nach Rang fragen",
                emoji="📋"
            )
        ]
        
        super().__init__(
            placeholder="⏰ Wähle dein Benachrichtigungs-Intervall...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="interval_select"
        )

    async def callback(self, interaction):
        selected_interval = int(self.values[0])
        
        # User-spezifisches Intervall speichern
        user_id = str(self.user_id)
        user_data = get_user_data(user_id)
        user_data['custom_interval'] = selected_interval
        save_user_data(user_id, user_data)
        
        # Bestätigung senden ohne das View zu entfernen
        await interaction.response.send_message(
            f"⏰ Benachrichtigungs-Intervall auf **{selected_interval} Tage** gesetzt!",
            ephemeral=True
        )

class NoNotificationButton(discord.ui.Button):
    def __init__(self, user_id, guild_id):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Keine Benachrichtigungen mehr",
            emoji="⏸️",
            custom_id="no_notification_btn"
        )

    async def callback(self, interaction):
        try:
            # Objekte zur Laufzeit aus IDs laden
            guild = bot.get_guild(self.guild_id)
            member = guild.get_member(self.user_id) if guild else None
            
            if not guild or not member:
                await interaction.response.send_message("❌ Fehler: Server oder User nicht gefunden.", ephemeral=True)
                return
            
            # "Keine Benachrichtigung mehr" Rolle hinzufügen (Rang bleibt erhalten)
            no_notification_role = discord.utils.get(guild.roles, id=NO_NOTIFICATION_ROLE_ID)
            if no_notification_role:
                await member.add_roles(no_notification_role)
                logger.info(f"Added 'No Notification' role to {member.display_name}")
            else:
                logger.error(f"No Notification role not found! ID: {NO_NOTIFICATION_ROLE_ID}")
            
            embed = discord.Embed(
                title="⏸️ Benachrichtigungen deaktiviert",
                description="Du wirst nicht mehr nach deinem Rang gefragt.\n\nDein Rang bleibt erhalten. Du kannst ihn jederzeit im Rang-Kanal ändern!",
                color=0xffaa00
            )
            
            await interaction.response.edit_message(embed=embed, view=None)
            
            # Tracke dass User geantwortet hat
            track_dm_response(str(interaction.user.id))
            
        except Exception as e:
            logger.error(f"Error in NoNotificationButton callback: {e}")
            try:
                await interaction.response.send_message(
                    f"❌ Fehler beim Deaktivieren der Benachrichtigungen: {str(e)[:100]}", 
                    ephemeral=True
                )
            except:
                pass
        
        # DM View aus persistent views entfernen
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM persistent_views WHERE message_id = ? AND view_type = ?', 
                              (str(interaction.message.id), 'dm_rank_select'))
                conn.commit()
                logger.info(f"Removed DM view {interaction.message.id} from persistent views")
        except Exception as e:
            logger.error(f"Failed to remove DM view from database: {e}")
        
        # Nachricht nach 5 Minuten löschen
        await asyncio.sleep(300)
        try:
            await interaction.delete_original_response()
        except:
            pass

class NoDeadlockButton(discord.ui.Button):
    def __init__(self, user_id, guild_id):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Spiele kein Deadlock mehr",
            emoji="🚫",
            custom_id="no_deadlock_btn"
        )

    async def callback(self, interaction):
        try:
            # Objekte zur Laufzeit aus IDs laden
            guild = bot.get_guild(self.guild_id)
            member = guild.get_member(self.user_id) if guild else None
            
            if not guild or not member:
                await interaction.response.send_message("❌ Fehler: Server oder User nicht gefunden.", ephemeral=True)
                return
            
            # Alle Rang-Rollen entfernen
            await remove_all_rank_roles(member, guild)
            
            # "Spiele gar kein Deadlock mehr" Rolle hinzufügen
            no_deadlock_role = discord.utils.get(guild.roles, id=NO_DEADLOCK_ROLE_ID)
            if no_deadlock_role:
                await member.add_roles(no_deadlock_role)
            
            embed = discord.Embed(
                title="🚫 Kein Deadlock mehr",
                description="Du wirst nicht mehr nach deinem Rang gefragt.\n\nFalls du wieder anfängst zu spielen, kannst du deine Rolle jederzeit im Rang-Kanal ändern!",
                color=0xff0000
            )
            
            await interaction.response.edit_message(embed=embed, view=None)
            
            # Tracke dass User geantwortet hat
            track_dm_response(str(interaction.user.id))
            
        except Exception as e:
            logger.error(f"Error in NoDeadlockButton callback: {e}")
            try:
                await interaction.response.send_message(
                    f"❌ Fehler beim Deaktivieren: {str(e)[:100]}", 
                    ephemeral=True
                )
            except:
                pass
        
        # DM View aus persistent views entfernen
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM persistent_views WHERE message_id = ? AND view_type = ?', 
                              (str(interaction.message.id), 'dm_rank_select'))
                conn.commit()
                logger.info(f"Removed DM view {interaction.message.id} from persistent views")
        except Exception as e:
            logger.error(f"Failed to remove DM view from database: {e}")
        
        # Nachricht nach 5 Minuten löschen
        await asyncio.sleep(300)
        try:
            await interaction.delete_original_response()
        except:
            pass

class FinishedButton(discord.ui.Button):
    def __init__(self, user_id, guild_id):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Fertig",
            emoji="✅",
            custom_id="finished_btn"  
        )

    async def callback(self, interaction):
        embed = discord.Embed(
            title="✅ Einstellungen gespeichert!",
            description="Deine Rang- und Intervall-Einstellungen wurden erfolgreich gespeichert.",
            color=0x00ff00
        )
        
        await interaction.response.edit_message(embed=embed, view=None)
        
        # Tracke dass User geantwortet hat
        track_dm_response(str(interaction.user.id))
        
        # DM View aus persistent views entfernen
        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM persistent_views WHERE message_id = ? AND view_type = ?', 
                              (str(interaction.message.id), 'dm_rank_select'))
                conn.commit()
                logger.info(f"Removed DM view {interaction.message.id} from persistent views")
        except Exception as e:
            logger.error(f"Failed to remove DM view from database: {e}")
        
        # Nachricht nach 30 Sekunden löschen
        await asyncio.sleep(30)
        try:
            await interaction.delete_original_response()
        except:
            pass

# Server-Rang-Auswahl (Dropdown ohne Emojis)
class ServerRankSelectView(discord.ui.View):
    def __init__(self, guild):
        super().__init__(timeout=None)  # Persistent view
        self.guild = guild
        self.add_item(ServerRankSelectDropdown(guild))

class ServerRankSelectDropdown(discord.ui.Select):
    def __init__(self, guild):
        self.guild = guild
        
        # Erstelle Optionen für jeden Rang
        options = []
        for rank in ranks:
            emoji = discord.utils.get(guild.emojis, name=rank)
            
            option = discord.SelectOption(
                label=rank.capitalize(),
                value=rank,
                description=f"Setze {rank.capitalize()} als deinen Rang",
                emoji=emoji
            )
            options.append(option)
        
        super().__init__(
            placeholder="🎮 Wähle deinen Deadlock-Rang...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="server_rank_select"
        )

    async def callback(self, interaction):
        selected_rank = self.values[0]
        member = interaction.user
        
        # Alle Rang-Rollen entfernen
        await remove_all_rank_roles(member, self.guild)
        
        # Neue Rolle hinzufügen
        role = discord.utils.get(self.guild.roles, name=selected_rank.capitalize())
        if not role:
            role = await self.guild.create_role(name=selected_rank.capitalize())
        await member.add_roles(role)
        
        # Phantom+ Benachrichtigung senden
        if selected_rank in ["phantom", "ascendant", "eternus"]:
            notification_channel = self.guild.get_channel(PHANTOM_NOTIFICATION_CHANNEL_ID)
            if notification_channel:
                rank_emoji = discord.utils.get(self.guild.emojis, name=selected_rank)
                emoji_display = str(rank_emoji) if rank_emoji else ""
                
                notification_embed = discord.Embed(
                    title="🔥 Phantom+ Rang Update",
                    description=f"{emoji_display} **{member.display_name}** hat sich den Rang **{selected_rank.capitalize()}** gegeben!",
                    color=0xff6b35
                )
                notification_embed.add_field(
                    name="User", 
                    value=f"{member.mention} ({member.id})", 
                    inline=True
                )
                notification_embed.add_field(
                    name="Rang", 
                    value=f"{emoji_display} {selected_rank.capitalize()}", 
                    inline=True
                )
                notification_embed.timestamp = datetime.now()
                
                try:
                    await notification_channel.send(embed=notification_embed)
                except:
                    pass
        
        # Ephemeral Bestätigung (nur für den User sichtbar)
        rank_emoji = discord.utils.get(self.guild.emojis, name=selected_rank)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"✅ {rank_emoji} Dein Rang wurde erfolgreich auf **{selected_rank.capitalize()}** gesetzt!",
                    ephemeral=True
                )
        except (discord.NotFound, discord.HTTPException):
            pass
        
        # Tracke dass User geantwortet hat
        track_dm_response(str(interaction.user.id))

async def get_existing_dm_view(user_id: str):
    """Prüft ob User bereits eine aktive DM View hat und gibt Message-Info zurück"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT message_id, channel_id FROM persistent_views WHERE user_id = ? AND view_type = ? LIMIT 1', 
                      (user_id, 'dm_rank_select'))
        result = cursor.fetchone()
        
        if result:
            message_id, channel_id = result
            try:
                # Prüfe ob die Nachricht noch existiert und erreichbar ist
                channel = bot.get_channel(int(channel_id))
                if channel:
                    message = await channel.fetch_message(int(message_id))
                    if message:
                        logger.info(f"Found existing DM view for user {user_id}: message {message_id}")
                        return {'message': message, 'message_id': message_id, 'channel_id': channel_id}
            except Exception as e:
                logger.warning(f"Existing DM message {message_id} no longer accessible: {e}")
                # Entferne tote View aus DB
                cursor.execute('DELETE FROM persistent_views WHERE message_id = ?', (message_id,))
                conn.commit()
        
        return None

async def cleanup_old_dm_views(user_id: str):
    """Löscht alte DM Views für einen User"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT message_id, channel_id FROM persistent_views WHERE user_id = ? AND view_type = ?', 
                      (user_id, 'dm_rank_select'))
        old_views = cursor.fetchall()
        
        for message_id, channel_id in old_views:
            try:
                # Versuche alte Nachricht zu löschen
                channel = bot.get_channel(int(channel_id))
                if channel:
                    message = await channel.fetch_message(int(message_id))
                    if message:
                        await message.delete()
                        logger.info(f"Deleted old DM view message {message_id} for user {user_id}")
            except Exception as e:
                logger.warning(f"Could not delete old DM message {message_id}: {e}")
        
        # Entferne alle alten DM Views für diesen User aus der DB
        cursor.execute('DELETE FROM persistent_views WHERE user_id = ? AND view_type = ?', 
                      (user_id, 'dm_rank_select'))
        conn.commit()
        
        if old_views:
            logger.info(f"Cleaned up {len(old_views)} old DM views for user {user_id}")

def track_dm_sent(user_id: str):
    """Trackt dass eine DM an User gesendet wurde"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO dm_response_tracking 
            (user_id, last_dm_sent, response_count, status)
            VALUES (?, ?, COALESCE((SELECT response_count FROM dm_response_tracking WHERE user_id = ?), 0), 'pending')
        ''', (user_id, datetime.now().isoformat(), user_id))
        conn.commit()

def track_dm_response(user_id: str):
    """Trackt dass User auf DM geantwortet hat"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE dm_response_tracking 
            SET response_count = response_count + 1, 
                last_response = ?, 
                status = 'responded'
            WHERE user_id = ?
        ''', (datetime.now().isoformat(), user_id))
        conn.commit()

async def cleanup_old_dm_views_auto():
    """Automatisches Cleanup von DM Views älter als 7 Tage"""
    cutoff_date = (datetime.now() - timedelta(days=7)).isoformat()
    cleaned_count = 0
    
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        
        # Finde DM Views älter als 7 Tage
        cursor.execute('''
            SELECT message_id, channel_id, user_id 
            FROM persistent_views 
            WHERE view_type = 'dm_rank_select' 
            AND created_at < ?
        ''', (cutoff_date,))
        old_views = cursor.fetchall()
        
        for message_id, channel_id, user_id in old_views:
            try:
                # Versuche DM zu löschen
                channel = bot.get_channel(int(channel_id))
                if channel:
                    message = await channel.fetch_message(int(message_id))
                    if message:
                        await message.delete()
                        logger.info(f"Auto-deleted old DM view {message_id} for user {user_id}")
            except Exception as e:
                logger.warning(f"Could not delete old DM message {message_id}: {e}")
            
            cleaned_count += 1
        
        # Entferne aus DB
        cursor.execute('DELETE FROM persistent_views WHERE view_type = ? AND created_at < ?', 
                      ('dm_rank_select', cutoff_date))
        
        # Markiere User als "dropped" wenn sie nicht geantwortet haben
        cursor.execute('''
            UPDATE dm_response_tracking 
            SET status = 'dropped_no_response'
            WHERE last_dm_sent < ? AND status = 'pending'
        ''', (cutoff_date,))
        
        conn.commit()
    
    if cleaned_count > 0:
        logger.info(f"Auto-cleanup: Removed {cleaned_count} old DM views (7+ days old)")
    
    return cleaned_count

async def ask_rank_update(member, current_rank, guild):
    """Sendet oder refresht Rang-Update-Nachricht mit Dropdown-Interface"""
    try:
        # Prüfe ob User bereits eine aktive DM hat
        existing_dm_info = await get_existing_dm_view(str(member.id))
        
        # User-spezifisches Intervall laden
        user_data = get_user_data(str(member.id))
        custom_interval = user_data.get('custom_interval')
        interval_days = custom_interval if custom_interval else RANK_INTERVALS.get(current_rank, 30)
        
        # Unterschiedliche Messages für User mit/ohne Rang
        if current_rank == "unranked":
            # Neuer User ohne Rang
            embed = discord.Embed(
                title="🎯 Willkommen zum Deadlock Rank Bot!",
                description=f"Hey {member.display_name} :)\n\nIch bin der **Deadlock Rank Bot** und möchte mal nett nachfragen, welchen Rang du aktuell hast :)\n\n🆕 **Du hast noch keinen Rang im Server!**\n\nWähle unten aus den Dropdown-Menüs deinen aktuellen Deadlock-Rang und dein gewünschtes Benachrichtigungs-Intervall aus!",
                color=0x7289DA
            )
        else:
            # User mit bestehendem Rang
            current_emoji = discord.utils.get(guild.emojis, name=current_rank)
            emoji_display = str(current_emoji) if current_emoji else ""
            
            embed = discord.Embed(
                title="🎯 Deadlock Rang-Update",
                description=f"Hey {member.display_name} :)\n\nIch bin der **Deadlock Rank Bot** und möchte mal nett nachfragen, welchen Rang du aktuell hast :)\n\n{emoji_display} **Dein aktueller Rang: {current_rank.capitalize()}**\n\nWähle unten aus den Dropdown-Menüs deinen aktuellen Rang und dein gewünschtes Benachrichtigungs-Intervall aus!",
                color=0x7289DA
            )
        
        embed.add_field(
            name="⏰ Aktuelles Intervall", 
            value=f"{interval_days} Tage", 
            inline=True
        )
        
        embed.add_field(
            name="📋 Bitte ehrlich sein", 
            value="Gib deinen **tatsächlichen** Rang an! Bei schwankenden Rängen wähle den, in dem du die meiste Zeit verbringst.", 
            inline=False
        )
        
        # Link zum Rang-Kanal hinzufügen
        embed.add_field(
            name="🔗 Alternative", 
            value="Du kannst deinen Rang auch direkt im Server ändern:\nhttps://discord.com/channels/1289721245281292288/1398021105339334666/1398062470244995267", 
            inline=False
        )
        
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        
        # Dropdown-View erstellen (persistent für DMs)
        view = RankSelectView(member.id, guild.id, persistent=True)
        
        if existing_dm_info:
            # Bestehende DM bearbeiten (refresh)
            existing_message = existing_dm_info['message']
            await existing_message.edit(embed=embed, view=view)
            logger.info(f"Refreshed existing DM view for {member.display_name}")
            
            # View für die bearbeitete Nachricht re-registrieren
            bot.add_view(view, message_id=int(existing_dm_info['message_id']))
        else:
            # Neue DM senden
            message = await member.send(embed=embed, view=view)
            logger.info(f"Sent new DM view to {member.display_name}")
            
            # Speichere DM View in Datenbank für Auto-Restore
            save_persistent_view(str(message.id), str(message.channel.id), str(guild.id), 'dm_rank_select', str(member.id))
        
        # Tracke dass DM gesendet wurde
        track_dm_sent(str(member.id))
        
    except discord.Forbidden:
        # Falls DM nicht möglich ist, ignorieren
        logger.warning(f"Could not send DM to {member.display_name} ({member.id})")

# Auto-restore function for views
async def auto_restore_rank_channel_view():
    """Automatische Wiederherstellung des Rank-Selection Views im Rang-Kanal"""
    try:
        # Hole den Rang-Kanal
        channel = bot.get_channel(RANK_SELECTION_CHANNEL_ID)
        if not channel:
            logger.error(f"Rank selection channel {RANK_SELECTION_CHANNEL_ID} not found")
            return
            
        guild = channel.guild
        logger.info(f"[AUTO RESTORE] Checking rank channel: #{channel.name}")
        
        # Suche nach Bot-Nachrichten in den letzten 50 Nachrichten
        bot_messages = []
        async for message in channel.history(limit=50):
            if message.author == bot.user:
                bot_messages.append(message)
        
        logger.info(f"[AUTO RESTORE] Found {len(bot_messages)} bot messages in rank channel")
        
        if not bot_messages:
            logger.info("[AUTO RESTORE] No bot messages found - creating new rank selection message")
            await create_rank_selection_message(channel, guild)
            return
        
        # Nimm die neueste Bot-Nachricht
        latest_message = bot_messages[0]
        logger.info(f"[AUTO RESTORE] Latest bot message ID: {latest_message.id}")
        
        # Prüfe ob die Nachricht ein Rang-Selection Embed ist
        if latest_message.embeds:
            embed = latest_message.embeds[0]
            if "Rang-Auswahl" in embed.title or "Deadlock-Rang" in str(embed.description):
                logger.info(f"[AUTO RESTORE] Found rank selection message - attaching view")
                
                # Erstelle neuen View und hänge ihn an
                view = ServerRankSelectView(guild)
                
                try:
                    await latest_message.edit(embed=embed, view=view)
                    save_persistent_view(str(latest_message.id), str(channel.id), str(guild.id), 'server_rank_select')
                    logger.info(f"[AUTO RESTORE] Successfully restored view to message {latest_message.id}")
                    
                except discord.NotFound:
                    logger.warning(f"[AUTO RESTORE] Message {latest_message.id} not found - creating new one")
                    await create_rank_selection_message(channel, guild)
                except Exception as e:
                    logger.error(f"[AUTO RESTORE] Error attaching view: {e}")
                    await create_rank_selection_message(channel, guild)
            else:
                logger.info("[AUTO RESTORE] Latest message is not a rank selection - creating new one")
                await create_rank_selection_message(channel, guild)
        else:
            logger.info("[AUTO RESTORE] Latest message has no embeds - creating new rank selection")
            await create_rank_selection_message(channel, guild)
            
    except Exception as e:
        logger.error(f"[AUTO RESTORE] Error in auto restore: {e}")

async def create_rank_selection_message(channel, guild):
    """Erstellt eine neue Rang-Auswahl-Nachricht im Kanal"""
    try:
        embed = discord.Embed(
            title="🎯 Deadlock Rang-Auswahl",
            description="Wähle deinen aktuellen Deadlock-Rang aus dem Dropdown-Menü.\n\nDie Auswahl ist nur für dich sichtbar und wird automatisch als Rolle zugewiesen.",
            color=0x7289DA
        )
        
        embed.add_field(
            name="📋 Hinweise",
            value="• Wähle deinen **tatsächlichen** Rang\n• Bei schwankenden Rängen: Den wo du die meiste Zeit verbringst\n• Die Auswahl ist **nur für dich sichtbar**",
            inline=False
        )
        
        embed.set_footer(text="🎮 Deadlock Rank Bot - Auto-Wiederhergestellt")
        
        view = ServerRankSelectView(guild)
        message = await channel.send(embed=embed, view=view)
        
        # Speichere in persistent views
        save_persistent_view(str(message.id), str(channel.id), str(guild.id), 'server_rank_select')
        
        logger.info(f"[AUTO RESTORE] Created new rank selection message {message.id}")
        return message
        
    except Exception as e:
        logger.error(f"[AUTO RESTORE] Error creating new rank selection message: {e}")

# Commands
@bot.command(name='rsetup')
@commands.has_permissions(administrator=True)
async def setup_rank_roles(ctx):
    """Erstellt Rang-Auswahl-Nachricht mit Dropdown"""
    # Cleanup alte Views für diese Guild
    removed_count = cleanup_old_views(str(ctx.guild.id), 'server_rank_select')
    
    embed = discord.Embed(
        title="🎯 Deadlock Rang-Auswahl",
        description="Wähle deinen aktuellen Deadlock-Rang aus dem Dropdown-Menü.\n\nDie Auswahl ist nur für dich sichtbar und wird automatisch als Rolle zugewiesen.",
        color=0x7289DA
    )
    
    embed.add_field(
        name="📋 Hinweise",
        value="• Wähle deinen **tatsächlichen** Rang\n• Bei schwankenden Rängen: Den wo du die meiste Zeit verbringst\n• Die Auswahl ist **nur für dich sichtbar**",
        inline=False
    )
    
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    
    view = ServerRankSelectView(ctx.guild)
    message = await ctx.send(embed=embed, view=view)
    
    # Speichere neue persistent view in Datenbank
    save_persistent_view(str(message.id), str(ctx.channel.id), str(ctx.guild.id), 'server_rank_select')
    
    global RANK_MESSAGE_ID
    RANK_MESSAGE_ID = message.id  # Update message ID
    
    confirm_embed = discord.Embed(
        title="✅ Rang-Auswahl erstellt!",
        description=f"Dropdown-Menü wurde erfolgreich erstellt.\nMessage-ID: {message.id}",
        color=0x00ff00
    )
    
    if removed_count > 0:
        confirm_embed.add_field(
            name="🧹 Cleanup", 
            value=f"{removed_count} alte View(s) automatisch entfernt", 
            inline=False
        )
    
    confirm_embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=confirm_embed)

@bot.command(name='rtest', aliases=['test_rank_message'])
@commands.has_permissions(administrator=True)
async def test_rank_message(ctx, user: discord.Member = None):
    """Testet eine Rang-Update-Nachricht"""
    guild = ctx.guild
    
    # Bestimme Test-User
    if not user:
        if test_users:
            test_user = test_users[0]
        else:
            embed = discord.Embed(
                title="❌ Keine Test-User",
                description="Keine Test-User gesetzt! Verwende `!rtest_users @user1 @user2 @user3`",
                color=0xff0000
            )
            embed.set_footer(text="🎮 Deadlock Rank Bot")
            await ctx.send(embed=embed)
            return
        test_user = test_users[0]
    else:
        test_user = user
    
    # Aktuellen Rang des Users ermitteln
    current_rank = get_user_current_rank(test_user)
    
    if not current_rank:
        embed = discord.Embed(
            title="❌ Kein Rang gefunden",
            description=f"User {test_user.mention} hat keinen Rang!\nVerfügbare Rollen: {[role.name for role in test_user.roles]}",
            color=0xff0000
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return
    
    await ask_rank_update(test_user, current_rank, guild)
    
    embed = discord.Embed(
        title="✅ Test-Nachricht gesendet!",
        description=f"Rang-Update-Nachricht an {test_user.mention} gesendet.",
        color=0x00ff00
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)

@bot.command(name='rtest_users')
@commands.has_permissions(administrator=True)
async def set_test_users(ctx, *users: discord.Member):
    """Setzt Test-User für das System"""
    global test_users
    
    if not users:
        embed = discord.Embed(
            title="❌ Keine User angegeben",
            description="Verwende: `!rtest_users @user1 @user2 @user3`",
            color=0xff0000
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return
    
    test_users = list(users)
    
    embed = discord.Embed(
        title="✅ Test-User gesetzt!",
        description=f"**{len(test_users)}** Test-User wurden gesetzt:",
        color=0x00ff00
    )
    
    user_list = []
    for user in test_users:
        current_rank = get_user_current_rank(user)
        user_list.append(f"{user.mention} - Rang: {current_rank or 'Kein Rang'}")
    
    embed.add_field(name="👥 Test-User", value="\n".join(user_list), inline=False)
    embed.add_field(name="ℹ️ Info", value="Diese User werden beim Daily-Check benachrichtigt", inline=False)
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    
    await ctx.send(embed=embed)

@bot.command(name='rqueue')
@commands.has_permissions(administrator=True)
async def create_queue_manually(ctx):
    """Erstellt die Benachrichtigungs-Queue manuell"""
    embed = discord.Embed(
        title="🔄 Queue wird erstellt...",
        description="Erstelle Benachrichtigungs-Queue für heute",
        color=0xffaa00
    )
    message = await ctx.send(embed=embed)
    
    # Queue erstellen
    await create_daily_queue()
    
    # Anzahl der Queue-Einträge prüfen
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM notification_queue WHERE queue_date = ?', (datetime.now().strftime('%Y-%m-%d'),))
        queue_count = cursor.fetchone()[0]
    
    final_embed = discord.Embed(
        title="✅ Queue erstellt!",
        description=f"**{queue_count}** User in der heutigen Queue",
        color=0x00ff00
    )
    final_embed.add_field(name="📋 Anzeigen", value="Verwende `!rdb queue` um die Queue anzuzeigen", inline=False)
    final_embed.set_footer(text="🎮 Deadlock Rank Bot")
    
    await message.edit(embed=final_embed)

@bot.command(name='rqueue_remaining', aliases=['rqr'])
@commands.has_permissions(administrator=True)
async def create_remaining_queue(ctx):
    """Erstellt neue Queue nur mit noch nicht verarbeiteten Usern"""
    embed = discord.Embed(
        title="🔄 Remaining Queue wird erstellt...",
        description="Erstelle Queue nur mit noch nicht verarbeiteten Usern",
        color=0xffaa00
    )
    message = await ctx.send(embed=embed)
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    # Lade noch nicht verarbeitete User aus heutiger Queue
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, guild_id, rank FROM notification_queue WHERE queue_date = ? AND processed = FALSE', (today,))
        remaining_users = cursor.fetchall()
    
    if not remaining_users:
        final_embed = discord.Embed(
            title="ℹ️ Keine verbleibenden User",
            description="Alle User aus der heutigen Queue wurden bereits verarbeitet!",
            color=0x0099ff
        )
        final_embed.set_footer(text="🎮 Deadlock Rank Bot")
        await message.edit(embed=final_embed)
        return
    
    # Erstelle neue Queue mit verbleibenden Usern
    new_queue_data = []
    for user_id, guild_id, rank in remaining_users:
        new_queue_data.append((user_id, guild_id, rank, today, False))
    
    # Lösche alte Queue und erstelle neue
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM notification_queue WHERE queue_date = ?', (today,))
        cursor.executemany('''
            INSERT INTO notification_queue (user_id, guild_id, rank, queue_date, processed)
            VALUES (?, ?, ?, ?, ?)
        ''', new_queue_data)
        conn.commit()
    
    logger.info(f"Remaining queue created with {len(new_queue_data)} users for {today}")
    
    final_embed = discord.Embed(
        title="✅ Remaining Queue erstellt!",
        description=f"**{len(new_queue_data)}** noch nicht verarbeitete User in der neuen Queue",
        color=0x00ff00
    )
    final_embed.add_field(name="📋 Anzeigen", value="Verwende `!rdb queue` um die Queue anzuzeigen", inline=False)
    final_embed.set_footer(text="🎮 Deadlock Rank Bot")
    
    await message.edit(embed=final_embed)

@bot.command(name='rqueue_never_contacted')
@commands.has_permissions(administrator=True)
async def create_never_contacted_queue(ctx):
    """Erstellt Queue nur mit Usern die noch NIE kontaktiert wurden (mit 30 Tage Wartezeit)"""
    embed = discord.Embed(
        title="🔍 Suche nach noch nie kontaktierten Usern...",
        description="Erstelle Queue mit Usern die noch keine DM erhalten haben (30 Tage Wartezeit)",
        color=0xffaa00
    )
    message = await ctx.send(embed=embed)
    
    guild = ctx.guild
    today = datetime.now().strftime('%Y-%m-%d')
    
    # Alle Server-Member laden (außer Bots)
    all_members = [member for member in guild.members if not member.bot]
    
    # Bereits kontaktierte User aus notification_log laden
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT user_id FROM notification_log')
        contacted_users = {row[0] for row in cursor.fetchall()}
    
    # User filtern die noch nie kontaktiert wurden
    never_contacted = []
    for member in all_members:
        if str(member.id) not in contacted_users:
            # Aktuellen Rang des Users ermitteln (falls vorhanden)
            user_rank = "unranked"  # Standard für User ohne Rang
            for rank in ranks:
                role = discord.utils.get(guild.roles, name=rank.capitalize())
                if role and role in member.roles:
                    user_rank = rank
                    break
            
            # Never contacted User sollen HEUTE kontaktiert werden
            never_contacted.append((str(member.id), str(guild.id), user_rank, today, False))
    
    if not never_contacted:
        final_embed = discord.Embed(
            title="ℹ️ Alle User bereits kontaktiert",
            description="Alle User mit Rang-Rollen haben bereits mindestens eine DM erhalten!",
            color=0x0099ff
        )
        final_embed.set_footer(text="🎮 Deadlock Rank Bot")
        await message.edit(embed=final_embed)
        return
    
    # Queue erstellen
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        # Alte Queue für heute löschen
        cursor.execute('DELETE FROM notification_queue WHERE queue_date = ?', (today,))
        # Neue Queue mit never contacted users für HEUTE erstellen
        cursor.executemany('''
            INSERT INTO notification_queue (user_id, guild_id, rank, queue_date, processed)
            VALUES (?, ?, ?, ?, ?)
        ''', never_contacted)
        conn.commit()
    
    logger.info(f"Never contacted queue created with {len(never_contacted)} users for {today}")
    
    final_embed = discord.Embed(
        title="✅ Queue für noch nie kontaktierte User erstellt!",
        description=f"**{len(never_contacted)}** User werden heute kontaktiert",
        color=0x00ff00
    )
    final_embed.add_field(
        name="📋 Nächste Schritte", 
        value="• `!rdb queue` - Queue anzeigen\n• `!rstart` - Benachrichtigungen starten", 
        inline=False
    )
    final_embed.set_footer(text="🎮 Deadlock Rank Bot")
    
    await message.edit(embed=final_embed)

@bot.command(name='rcheck_never_contacted')
@commands.has_permissions(administrator=True)
async def check_never_contacted(ctx):
    """Zeigt detaillierte Info über noch nie kontaktierte User"""
    guild = ctx.guild
    
    # Alle Server-Member sammeln (außer Bots)
    all_server_users = set()
    user_mapping = {}
    
    for member in guild.members:
        if not member.bot:
            all_server_users.add(str(member.id))
            
            # Rang ermitteln
            user_rank = "unranked"
            for rank in ranks:
                role = discord.utils.get(guild.roles, name=rank.capitalize())
                if role and role in member.roles:
                    user_rank = rank
                    break
            
            user_mapping[str(member.id)] = (member, user_rank)
    
    # Bereits kontaktierte User aus notification_log
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT user_id FROM notification_log')
        contacted_users = {row[0] for row in cursor.fetchall()}
    
    # Noch nie kontaktierte User
    never_contacted = all_server_users - contacted_users
    
    embed = discord.Embed(
        title="🔍 User-Kontakt Analyse",
        description=f"Analyse aller Server-User (außer Bots)",
        color=0x0099ff
    )
    
    embed.add_field(
        name="📊 Statistiken",
        value=f"**Gesamt Server-User**: {len(all_server_users)}\n"
              f"**Bereits kontaktiert**: {len(contacted_users)}\n"
              f"**Noch nie kontaktiert**: {len(never_contacted)}",
        inline=False
    )
    
    if never_contacted:
        # Zeige einige nie kontaktierte User
        sample_users = []
        for user_id in list(never_contacted)[:10]:
            if user_id in user_mapping:
                member, rank = user_mapping[user_id]
                rank_display = rank.capitalize() if rank != "unranked" else "Kein Rang"
                sample_users.append(f"• **{member.display_name}** ({rank_display})")
        
        embed.add_field(
            name="👥 Beispiele nie kontaktierter User",
            value="\n".join(sample_users) + (f"\n... und {len(never_contacted)-10} weitere" if len(never_contacted) > 10 else ""),
            inline=False
        )
    
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)

@bot.command(name='rstart')
@commands.has_permissions(administrator=True)
async def start_notification_system(ctx, mode: str = "normal", interval: int = 30):
    """Startet das Benachrichtigungs-System"""
    if not daily_notification_check.is_running():
        daily_notification_check.start()
    
    if not daily_cleanup_check.is_running():
        daily_cleanup_check.start()
    
    embed = discord.Embed(
        title="✅ Rank Bot gestartet!",
        description=f"**Modus:** {mode}\n**Intervall:** {interval}",
        color=0x00ff00
    )
    embed.add_field(name="📋 Queue erstellen", value="Verwende `!rqueue` um Queue manuell zu erstellen", inline=True)
    embed.add_field(name="⏰ Aktive Zeiten", value="8-22 Uhr deutsche Zeit", inline=True)
    embed.add_field(name="🧹 Auto-Cleanup", value="Alte DM Views (7+ Tage) werden täglich entfernt", inline=True)
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    
    await ctx.send(embed=embed)

@bot.command(name='rstop')
@commands.has_permissions(administrator=True)
async def stop_notification_system(ctx):
    """Stoppt das Benachrichtigungs-System"""
    if daily_notification_check.is_running():
        daily_notification_check.stop()
    
    embed = discord.Embed(
        title="🛑 Rank Bot gestoppt!",
        description="Automatische Benachrichtigungen wurden gestoppt.",
        color=0xff6600
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)

@bot.command(name='radd')
@commands.has_permissions(administrator=True)
async def add_user_to_queue(ctx, user: discord.Member):
    """Fügt einen User zur heutigen Queue hinzu"""
    today = datetime.now().strftime('%Y-%m-%d')
    current_rank = get_user_current_rank(user)
    
    if not current_rank:
        embed = discord.Embed(
            title="❌ Kein Rang gefunden",
            description=f"User {user.mention} hat keinen Rang!",
            color=0xff0000
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return
    
    # User zur Queue hinzufügen
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO notification_queue (user_id, guild_id, rank, queue_date)
            VALUES (?, ?, ?, ?)
        ''', (str(user.id), str(ctx.guild.id), current_rank, today))
        conn.commit()
    
    embed = discord.Embed(
        title="✅ User zur Queue hinzugefügt!",
        description=f"{user.mention} wurde zur heutigen Queue hinzugefügt.\n**Rang:** {current_rank.capitalize()}",
        color=0x00ff00
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)

@bot.command(name='rdb')
@commands.has_permissions(administrator=True)
async def view_database(ctx, table: str = None):
    """Zeigt Datenbank-Inhalte an"""
    if not table:
        # Übersicht aller Tabellen
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            
            # Count entries in each table
            cursor.execute('SELECT COUNT(*) FROM user_data')
            user_count = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM notification_log')
            notification_count = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM notification_queue WHERE queue_date = ?', (datetime.now().strftime('%Y-%m-%d'),))
            queue_count = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM persistent_views')
            views_count = cursor.fetchone()[0]
        
        embed = discord.Embed(title="📊 Datenbank Übersicht", color=0x0099ff)
        
        embed.add_field(name="📋 Tabellen", value="`users`, `notifications`, `queue`, `views`", inline=True)
        embed.add_field(name="📊 Statistiken", value=f"👥 {user_count} User\n📧 {notification_count} Logs\n📋 {queue_count} Queue\n🖼️ {views_count} Views", inline=True)
        embed.add_field(name="🔧 Commands", value="`!rdb [table]`", inline=True)
        
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
    
    elif table.lower() == 'users':
        # User Data
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, custom_interval, paused_until FROM user_data LIMIT 10')
            results = cursor.fetchall()
        
        embed = discord.Embed(title="👥 User-Daten", color=0x0099ff)
        
        if results:
            user_list = []
            for user_id, custom_interval, paused_until in results:
                try:
                    user = bot.get_user(int(user_id))
                    name = user.display_name if user else f"User {user_id}"
                    interval_text = f"{custom_interval}d" if custom_interval else "Standard"
                    pause_text = "Pausiert" if paused_until else "Aktiv"
                    user_list.append(f"**{name}**: {interval_text}, {pause_text}")
                except:
                    user_list.append(f"**User {user_id}**: Fehler beim Laden")
            
            embed.add_field(name="📋 User (Top 10)", value="\n".join(user_list), inline=False)
        else:
            embed.add_field(name="📋 User", value="Keine Daten vorhanden", inline=False)
        
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
    
    elif table.lower() == 'notifications':
        # Notification Log
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, rank, notification_time, count FROM notification_log ORDER BY notification_time DESC LIMIT 10')
            results = cursor.fetchall()
        
        embed = discord.Embed(title="📧 Benachrichtigungs-Log", color=0x0099ff)
        
        if results:
            notif_list = []
            for user_id, rank, notif_time, count in results:
                try:
                    user = bot.get_user(int(user_id))
                    name = user.display_name if user else f"User {user_id}"
                    time_str = datetime.fromisoformat(notif_time).strftime('%d.%m %H:%M')
                    notif_list.append(f"**{name}**: {rank.capitalize()} ({time_str}) #{count}")
                except:
                    notif_list.append(f"**User {user_id}**: {rank} - Fehler")
            
            embed.add_field(name="📋 Letzte Benachrichtigungen", value="\n".join(notif_list), inline=False)
        else:
            embed.add_field(name="📋 Benachrichtigungen", value="Keine Daten vorhanden", inline=False)
        
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
    
    elif table.lower() == 'queue':
        # Notification Queue
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, rank, queue_date FROM notification_queue WHERE queue_date = ? ORDER BY user_id', (datetime.now().strftime('%Y-%m-%d'),))
            results = cursor.fetchall()
        
        embed = discord.Embed(title="📋 Heutige Benachrichtigungs-Queue", color=0x0099ff)
        
        if results:
            queue_list = []
            for user_id, rank, queue_date in results:
                try:
                    user = bot.get_user(int(user_id))
                    name = user.display_name if user else f"User {user_id}"
                    queue_list.append(f"**{name}**: {rank.capitalize()}")
                except:
                    queue_list.append(f"**User {user_id}**: {rank}")
            
            # Kürze die Liste wenn zu lang (Discord Limit: 1024 Zeichen)
            queue_text = "\n".join(queue_list)
            if len(queue_text) > 1000:
                queue_text = "\n".join(queue_list[:15]) + f"\n... und {len(queue_list)-15} weitere"
            embed.add_field(name=f"📅 Queue für {datetime.now().strftime('%d.%m.%Y')} ({len(queue_list)} User)", value=queue_text, inline=False)
        else:
            embed.add_field(name="📋 Queue", value="Keine Einträge für heute", inline=False)
        
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
    
    elif table.lower() == 'views':
        # Persistent Views
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT message_id, channel_id, guild_id, view_type, user_id FROM persistent_views')
            results = cursor.fetchall()
        
        embed = discord.Embed(title="🖼️ Persistent Views", color=0x0099ff)
        
        if results:
            views_list = []
            dm_count = 0
            server_count = 0
            
            for message_id, channel_id, guild_id, view_type, user_id in results:
                try:
                    if view_type == 'dm_rank_select' and user_id:
                        dm_count += 1
                        # Nur erste 10 DM Views anzeigen
                        if dm_count <= 10:
                            user = bot.get_user(int(user_id))
                            user_name = user.display_name if user else f"User {user_id}"
                            views_list.append(f"**{view_type}**: DM to {user_name}")
                    else:
                        server_count += 1
                        channel = bot.get_channel(int(channel_id))
                        channel_name = channel.name if channel else f"Channel {channel_id}"
                        views_list.append(f"**{view_type}**: #{channel_name}")
                except:
                    views_list.append(f"**{view_type}**: Unknown")
            
            # Zusammenfassung hinzufügen
            summary = f"📊 **Gesamt:** {len(results)} Views ({server_count} Server, {dm_count} DMs)"
            if dm_count > 10:
                summary += f"\n*(Zeige nur erste 10 von {dm_count} DM Views)*"
            
            embed.add_field(name="📋 Aktive Views", value=summary + "\n\n" + "\n".join(views_list[:20]), inline=False)
        else:
            embed.add_field(name="📋 Views", value="Keine persistent Views vorhanden", inline=False)
        
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
    
    else:
        embed = discord.Embed(
            title="❌ Unbekannte Tabelle",
            description="Verfügbare Tabellen: `users`, `notifications`, `queue`, `views`",
            color=0xff0000
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)

@bot.command(name='rtest_view')
@commands.has_permissions(administrator=True)
async def test_view_registration(ctx, message_id: str):
    """Testet ob eine View korrekt registriert ist"""
    try:
        # Prüfe ob View in Bot registriert ist
        view_found = False
        view_count = 0
        for view in bot.persistent_views:
            view_count += 1
            # Views werden mit message_id registriert, nicht über .message Attribut
            
        # Prüfe direkt über bot._view_store
        if hasattr(bot, '_view_store'):
            for stored_message_id in bot._view_store._views:
                if str(stored_message_id) == message_id:
                    view_found = True
                    break
        
        # Prüfe Datenbank
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM persistent_views WHERE message_id = ?', (message_id,))
            db_result = cursor.fetchone()
        
        embed = discord.Embed(
            title="🔍 View Registration Test",
            description=f"Test für Message ID: `{message_id}`",
            color=0x0099ff
        )
        
        embed.add_field(
            name="🤖 Bot Registration",
            value=f"✅ Registriert" if view_found else f"❌ Nicht registriert ({view_count} Views total)",
            inline=True
        )
        
        embed.add_field(
            name="💾 Datenbank",
            value="✅ Vorhanden" if db_result else "❌ Nicht vorhanden",
            inline=True
        )
        
        if db_result:
            embed.add_field(
                name="📋 DB Details (Roh)",
                value=f"0: {db_result[0]}\n1: {db_result[1]}\n2: {db_result[2]}\n3: {db_result[3]}\n4: {db_result[4]}",
                inline=False
            )
        
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"❌ Fehler beim Testen: {str(e)[:100]}")

@bot.command(name='rtest_register')
@commands.has_permissions(administrator=True)
async def test_register_view(ctx, user_id: str, guild_id: str):
    """Testet direkte View-Registrierung"""
    try:
        # Erstelle neue View
        view = RankSelectView(int(user_id), int(guild_id), persistent=True)
        
        # Registriere sie
        bot.add_view(view)
        
        await ctx.send(f"✅ View für User {user_id} registriert. Custom IDs: {[item.custom_id for item in view.children]}")
        
    except Exception as e:
        await ctx.send(f"❌ Fehler: {str(e)[:200]}")

@bot.command(name='rclean_duplicates')
@commands.has_permissions(administrator=True)
async def clean_duplicate_views(ctx):
    """Bereinigt doppelte Server Views aus der Datenbank"""
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            
            # Finde doppelte Server Views
            cursor.execute('''
                SELECT message_id, COUNT(*) as count 
                FROM persistent_views 
                WHERE view_type = 'server_rank_select' 
                GROUP BY message_id 
                HAVING count > 1
            ''')
            duplicates = cursor.fetchall()
            
            if not duplicates:
                await ctx.send("✅ Keine doppelten Server Views gefunden!")
                return
            
            cleaned_count = 0
            for message_id, count in duplicates:
                # Behalte nur einen Eintrag pro message_id
                cursor.execute('''
                    DELETE FROM persistent_views 
                    WHERE message_id = ? AND view_type = 'server_rank_select' 
                    AND rowid NOT IN (
                        SELECT MIN(rowid) 
                        FROM persistent_views 
                        WHERE message_id = ? AND view_type = 'server_rank_select'
                    )
                ''', (message_id, message_id))
                
                cleaned_count += count - 1
            
            conn.commit()
            
            embed = discord.Embed(
                title="🧹 Duplikate bereinigt",
                description=f"**{cleaned_count}** doppelte Server Views entfernt",
                color=0x00ff00
            )
            await ctx.send(embed=embed)
            
    except Exception as e:
        await ctx.send(f"❌ Fehler beim Bereinigen: {str(e)[:100]}")

@bot.command(name='rclean_views')
@commands.has_permissions(administrator=True)
async def clean_dm_views(ctx):
    """Entfernt alle DM Views aus der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM persistent_views WHERE view_type = ?', ('dm_rank_select',))
        dm_count = cursor.fetchone()[0]
        
        if dm_count == 0:
            embed = discord.Embed(
                title="ℹ️ Keine DM Views",
                description="Keine DM Views in der Datenbank gefunden.",
                color=0x0099ff
            )
            embed.set_footer(text="🎮 Deadlock Rank Bot")
            await ctx.send(embed=embed)
            return
        
        cursor.execute('DELETE FROM persistent_views WHERE view_type = ?', ('dm_rank_select',))
        conn.commit()
    
    embed = discord.Embed(
        title="🧹 DM Views bereinigt!",
        description=f"**{dm_count}** DM Views aus der Datenbank entfernt.",
        color=0x00ff00
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)
    
    logger.info(f"Cleaned {dm_count} DM views from database")

@bot.command(name='rfix_existing_views')
@commands.has_permissions(administrator=True)
async def fix_existing_views(ctx):
    """Scannt existierende DM Messages aus der Datenbank und erstellt Views mit den echten Custom IDs"""
    embed = discord.Embed(
        title="🔍 Scanne existierende DM Messages...",
        description="Fetche DMs aus der Datenbank und extrahiere echte Custom IDs",
        color=0xffaa00
    )
    message = await ctx.send(embed=embed)
    
    # Alle existierenden DM Views aus der Datenbank laden
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT message_id, channel_id, user_id FROM persistent_views WHERE view_type = ?', ('dm_rank_select',))
        existing_views = cursor.fetchall()
    
    if not existing_views:
        error_embed = discord.Embed(
            title="❌ Keine DM Views gefunden",
            description="Keine existierenden DM Views in der Datenbank.",
            color=0xff0000
        )
        await message.edit(embed=error_embed)
        return
    
    success_count = 0
    failed_count = 0
    custom_ids_map = {}
    
    # Update embed für Progress
    progress_embed = discord.Embed(
        title="🔄 Verarbeite existierende DMs...",
        description=f"Scanne {len(existing_views)} DM Messages aus Datenbank...",
        color=0xffaa00
    )
    await message.edit(embed=progress_embed)
    
    for message_id, channel_id, user_id in existing_views:
        try:
            # User und DM Channel fetchen
            user = await bot.fetch_user(int(user_id))
            dm_channel = await user.create_dm()
            
            # Original Message fetchen
            original_message = await dm_channel.fetch_message(int(message_id))
            
            # Custom IDs aus den Components extrahieren
            if original_message.components:
                extracted_ids = []
                for row in original_message.components:
                    for comp in row.children:
                        if hasattr(comp, 'custom_id'):
                            extracted_ids.append(comp.custom_id)
                
                if len(extracted_ids) >= 5:  # Erwarten 5 Components: 2 Dropdowns + 3 Buttons
                    custom_ids_map[message_id] = {
                        'rank_select': extracted_ids[0],
                        'interval_select': extracted_ids[1], 
                        'no_notification': extracted_ids[2],
                        'no_deadlock': extracted_ids[3],
                        'finished': extracted_ids[4],
                        'user_id': user_id,
                        'channel_id': channel_id
                    }
                    success_count += 1
                    
                    # Erstelle View mit original Components und echten Custom IDs
                    view = RankSelectViewFromMessage(
                        int(user_id), 
                        int(ctx.guild.id),
                        original_message.components  # Original Components mit Emojis
                    )
                    
                    # View registrieren
                    bot.add_view(view, message_id=int(message_id))
                    
                else:
                    logger.warning(f"Message {message_id} hat nicht genug Components: {len(extracted_ids)}")
                    failed_count += 1
            else:
                logger.warning(f"Message {message_id} hat keine Components")
                failed_count += 1
                
        except Exception as e:
            logger.error(f"Failed to process message {message_id}: {e}")
            failed_count += 1
    
    # Ergebnis anzeigen
    final_embed = discord.Embed(
        title="✅ Existierende Views gefixed (DB)!",
        description=f"Custom IDs aus Datenbank-Messages extrahiert",
        color=0x00ff00
    )
    final_embed.add_field(
        name="📊 Ergebnis",
        value=f"✅ Erfolgreich: {success_count}\n"
              f"❌ Fehlgeschlagen: {failed_count}\n"
              f"📧 Gesamt: {len(existing_views)}",
        inline=False
    )
    final_embed.add_field(
        name="🔧 Was wurde gemacht",
        value="• DM Messages aus Datenbank gefetched\n"
              "• Echte Custom IDs extrahiert\n" 
              "• Views mit korrekten IDs erstellt\n"
              "• Views für Bot-Restart registriert",
        inline=False
    )
    final_embed.set_footer(text="🎮 Deadlock Rank Bot • DB Views gefixt!")
    
    await message.edit(embed=final_embed)

@bot.command(name='rfix_all_dm_messages')
@commands.has_permissions(administrator=True)
async def fix_all_dm_messages(ctx):
    """Scannt ALLE DM Channels nach Bot Messages und erstellt Views mit echten Custom IDs"""
    embed = discord.Embed(
        title="🔍 Vollscan: Suche ALLE Bot DM Messages...",
        description="Scanne ALLE Server-User und suche Bot Messages in ihren DMs",
        color=0xffaa00
    )
    message = await ctx.send(embed=embed)
    
    guild = ctx.guild
    total_users = 0
    scanned_users = 0
    found_messages = 0
    fixed_messages = 0
    failed_messages = 0
    new_messages = 0  # Messages die nicht in der DB waren
    
    # ALLE User sammeln (nicht nur die mit Rang-Rollen)
    users_to_scan = []
    for member in guild.members:
        if member.bot:
            continue
            
        total_users += 1
        users_to_scan.append(member)  # Alle User hinzufügen
    
    if not users_to_scan:
        error_embed = discord.Embed(
            title="❌ Keine User gefunden",
            description="Keine User im Server gefunden.",
            color=0xff0000
        )
        await message.edit(embed=error_embed)
        return
    
    # Progress Update
    progress_embed = discord.Embed(
        title="🔄 Scanne DM Channels...",
        description=f"Scanne {len(users_to_scan)} User nach Bot Messages...",
        color=0xffaa00
    )
    await message.edit(embed=progress_embed)
    
    # Existierende Messages aus DB laden für Vergleich
    existing_message_ids = set()
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT message_id FROM persistent_views WHERE view_type = ?', ('dm_rank_select',))
        existing_message_ids = {row[0] for row in cursor.fetchall()}
    
    for i, member in enumerate(users_to_scan):
        try:
            scanned_users += 1
            
            # Progress Update alle 20 User
            if i % 20 == 0:
                progress_embed.description = f"Scanne User {i+1}/{len(users_to_scan)}... (Gefunden: {found_messages}, Gefixt: {fixed_messages})"
                await message.edit(embed=progress_embed)
            
            # DM Channel erstellen/fetchen
            dm_channel = await member.create_dm()
            
            # Letzte 50 Messages im DM Channel durchsuchen
            async for dm_message in dm_channel.history(limit=50):
                if dm_message.author == bot.user and dm_message.components:
                    found_messages += 1
                    
                    # Prüfen ob Message Components hat die wie Rank Views aussehen
                    has_rank_components = False
                    extracted_ids = []
                    
                    for row in dm_message.components:
                        for comp in row.children:
                            if hasattr(comp, 'custom_id'):
                                extracted_ids.append(comp.custom_id)
                    
                    # Wenn genug Components vorhanden sind, als Rank View behandeln
                    if len(extracted_ids) >= 5:
                        has_rank_components = True
                        
                        # Prüfen ob Message neu ist (nicht in DB)
                        if str(dm_message.id) not in existing_message_ids:
                            new_messages += 1
                            
                            # Message zur DB hinzufügen
                            with sqlite3.connect(db_path) as conn:
                                cursor = conn.cursor()
                                cursor.execute('''
                                    INSERT OR IGNORE INTO persistent_views 
                                    (message_id, channel_id, guild_id, view_type, user_id) 
                                    VALUES (?, ?, ?, ?, ?)
                                ''', (str(dm_message.id), str(dm_channel.id), str(guild.id), 'dm_rank_select', str(member.id)))
                                conn.commit()
                        
                        try:
                            # View mit original Components und echten Custom IDs erstellen
                            view = RankSelectViewFromMessage(
                                member.id,
                                guild.id,
                                dm_message.components  # Original Components mit Emojis
                            )
                            
                            # View registrieren
                            bot.add_view(view, message_id=dm_message.id)
                            fixed_messages += 1
                            
                        except Exception as e:
                            logger.error(f"Failed to create view for message {dm_message.id}: {e}")
                            failed_messages += 1
            
            # Rate limiting - kurze Pause alle 10 User
            if i % 10 == 0:
                await asyncio.sleep(0.5)
                
        except Exception as e:
            logger.error(f"Failed to scan DM for user {member.display_name}: {e}")
    
    # Ergebnis anzeigen
    final_embed = discord.Embed(
        title="✅ Vollscan abgeschlossen!",
        description=f"Alle DM Channels nach Bot Messages gescannt",
        color=0x00ff00
    )
    final_embed.add_field(
        name="📊 Scan Ergebnis",
        value=f"👥 User gescannt: {scanned_users}/{total_users}\n"
              f"💬 Bot Messages gefunden: {found_messages}\n"
              f"🆕 Neue Messages (nicht in DB): {new_messages}",
        inline=False
    )
    final_embed.add_field(
        name="🔧 Fix Ergebnis",
        value=f"✅ Views erstellt: {fixed_messages}\n"
              f"❌ Fehlgeschlagen: {failed_messages}",
        inline=False
    )
    final_embed.add_field(
        name="💾 Datenbank",
        value=f"Neue Messages zur DB hinzugefügt: {new_messages}",
        inline=False
    )
    final_embed.set_footer(text="🎮 Deadlock Rank Bot • Vollscan komplett!")
    
    await message.edit(embed=final_embed)

@bot.command(name='rdm_stats')
@commands.has_permissions(administrator=True)
async def dm_response_stats(ctx):
    """Zeigt DM Response Statistiken"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        
        # Gesamtstatistiken
        cursor.execute('SELECT COUNT(*) FROM dm_response_tracking')
        total_users = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM dm_response_tracking WHERE status = "responded"')
        responded_users = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM dm_response_tracking WHERE status = "pending"')
        pending_users = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM dm_response_tracking WHERE status = "dropped_no_response"')
        dropped_users = cursor.fetchone()[0]
        
        # Durchschnittliche Response-Zeit (für User die geantwortet haben)
        cursor.execute('''
            SELECT AVG(julianday(last_response) - julianday(last_dm_sent)) as avg_response_days
            FROM dm_response_tracking 
            WHERE status = "responded" AND last_response IS NOT NULL
        ''')
        avg_response_days = cursor.fetchone()[0]
        
        if total_users == 0:
            embed = discord.Embed(
                title="📊 DM Response Statistiken",
                description="Noch keine DM-Daten vorhanden",
                color=0x0099ff
            )
        else:
            response_rate = (responded_users / total_users) * 100
            
            embed = discord.Embed(
                title="📊 DM Response Statistiken",
                description=f"Antwortrate: **{response_rate:.1f}%**",
                color=0x0099ff
            )
            
            embed.add_field(
                name="👥 User Status",
                value=f"✅ Geantwortet: {responded_users}\n⏳ Ausstehend: {pending_users}\n❌ Dropped: {dropped_users}\n📊 Gesamt: {total_users}",
                inline=True
            )
            
            if avg_response_days:
                avg_hours = avg_response_days * 24
                embed.add_field(
                    name="⏱️ Ø Response-Zeit",
                    value=f"{avg_hours:.1f} Stunden",
                    inline=True
                )
    
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)

@bot.event
async def on_ready():
    logger.info(f'Deadlock Rank Bot ist online! ({bot.user})')
    logger.info("Standalone Rank Bot bereit für Commands:")
    logger.info("   !rsetup - Rang-Auswahl erstellen")
    logger.info("   !rqueue - Queue manuell erstellen")
    logger.info("   !rqueue_remaining - Queue mit verbleibenden Usern")
    logger.info("   !rqueue_never_contacted - Queue mit neuen Usern (30 Tage Wartezeit)")
    logger.info("   !rcheck_never_contacted - Analyse der noch nie kontaktierten User")
    logger.info("   !radd @user - User zur Queue hinzufügen")
    logger.info("   !rtest - Test-Nachricht senden")
    logger.info("   !rtest_users @user1 @user2 - Test-User setzen")
    logger.info("   !rstart - System starten")
    logger.info("   !rdb - Datenbank anzeigen")
    logger.info("   !rtest_view [message_id] - View Registration testen")
    logger.info("   !rclean_duplicates - Doppelte Views bereinigen")
    logger.info("   !rclean_views - DM Views bereinigen")
    logger.info("   !rqueue_all_users - Alle User mit Rang-Rollen neu in Queue")
    logger.info("   !rfix_existing_views - Alte DMs mit echten Custom IDs fixen")
    logger.info("   !rfix_all_dm_messages - VOLLSCAN: Alle DM Channels nach Bot Messages durchsuchen")
    
    # Restore persistent views now that bot is ready and connected
    await restore_persistent_views()
    
    # Automatische Wiederherstellung des Rang-Kanals
    await auto_restore_rank_channel_view()

# DM Scheduling Tasks
def log_notification(user_id: str, rank: str):
    """Loggt eine gesendete Benachrichtigung"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO notification_log (user_id, rank) VALUES (?, ?)
        ''', (user_id, rank))
        conn.commit()

def is_notification_time() -> bool:
    """Prüft ob es aktuell eine gute Zeit für Benachrichtigungen ist (8-22 Uhr deutsche Zeit)"""
    now = datetime.now()
    current_hour = now.hour
    
    # Für Test-User: Immer erlaubt
    if test_users:
        return True
    
    # Für alle anderen: normale Zeiten
    return NOTIFICATION_START_HOUR <= current_hour < NOTIFICATION_END_HOUR

def save_queue_data(queue_data: list, date: str):
    """Speichert Queue-Daten in der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        
        # Alte Queue für das Datum löschen
        cursor.execute('DELETE FROM notification_queue WHERE queue_date = ?', (date,))
        
        # Neue Queue einfügen
        for item in queue_data:
            cursor.execute('''
                INSERT INTO notification_queue (user_id, guild_id, rank, queue_date)
                VALUES (?, ?, ?, ?)
            ''', (item['user_id'], item['guild_id'], item['rank'], date))
        
        conn.commit()

def load_queue_data(date: str) -> list:
    """Lädt Queue-Daten aus der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, guild_id, rank 
            FROM notification_queue 
            WHERE queue_date = ? AND processed = FALSE
            ORDER BY added_at
        ''', (date,))
        
        results = cursor.fetchall()
        return [{'user_id': row[0], 'guild_id': row[1], 'rank': row[2]} for row in results]

def mark_queue_item_processed(user_id: str, guild_id: str, date: str):
    """Markiert Queue-Item als verarbeitet"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE notification_queue 
            SET processed = TRUE 
            WHERE user_id = ? AND guild_id = ? AND queue_date = ?
        ''', (user_id, guild_id, date))
        conn.commit()

@tasks.loop(minutes=1)
async def daily_notification_check():
    """Minütliche Benachrichtigungen senden"""
    # Nur Benachrichtigungen senden (wenn es Zeit dafür ist)
    if is_notification_time():
        await process_notification_queue()

@tasks.loop(hours=24)
async def daily_cleanup_check():
    """Tägliches Cleanup von alten DM Views"""
    logger.info("Starting daily DM cleanup...")
    cleaned_count = await cleanup_old_dm_views_auto()
    
    if cleaned_count > 0:
        logger.info(f"Daily cleanup completed: {cleaned_count} old DM views removed")
    else:
        logger.info("Daily cleanup completed: No old DM views to remove")

async def create_daily_queue():
    """Erstellt die tägliche Benachrichtigungs-Queue"""
    today = datetime.now().strftime('%Y-%m-%d')
    queue_data = []
    
    # Durchgehe alle Guilds und Member
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot:
                continue
            
            # Skip User mit "Keine Benachrichtigung" oder "Kein Deadlock" Rolle
            if any(role.id in [NO_NOTIFICATION_ROLE_ID, NO_DEADLOCK_ROLE_ID] for role in member.roles):
                continue
            
            # Aktueller Rang
            current_rank = get_user_current_rank(member)
            if not current_rank:
                continue
            
            # User-Daten laden
            user_data = get_user_data(str(member.id))
            
            # Pausiert?
            if user_data.get('paused_until'):
                pause_until = datetime.fromisoformat(user_data['paused_until'])
                if datetime.now() < pause_until:
                    continue
            
            # Intervall bestimmen
            custom_interval = user_data.get('custom_interval')
            interval_days = custom_interval if custom_interval else RANK_INTERVALS.get(current_rank, 30)
            
            # Letzte Benachrichtigung prüfen
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT notification_time FROM notification_log 
                    WHERE user_id = ? 
                    ORDER BY notification_time DESC LIMIT 1
                ''', (str(member.id),))
                result = cursor.fetchone()
                
                if result:
                    last_notification = datetime.fromisoformat(result[0])
                    days_since = (datetime.now() - last_notification).days
                    
                    if days_since < interval_days:
                        continue  # Noch nicht Zeit für neue Benachrichtigung
            
            # Test-User oder normaler User
            if test_users and member not in test_users:
                continue  # Nur Test-User wenn Test-Modus aktiv
            
            # Zur Queue hinzufügen
            queue_data.append({
                'user_id': str(member.id),
                'guild_id': str(guild.id),
                'rank': current_rank
            })
    
    # Queue in Datenbank speichern
    save_queue_data(queue_data, today)
    logger.info(f"Daily queue created with {len(queue_data)} users for {today}")

async def process_notification_queue():
    """Verarbeitet die Benachrichtigungs-Queue"""
    today = datetime.now().strftime('%Y-%m-%d')
    queue_data = load_queue_data(today)
    
    if not queue_data:
        return
    
    # Maximal 1 User pro Minute verarbeiten
    user_to_notify = queue_data[0]
    
    try:
        guild = bot.get_guild(int(user_to_notify["guild_id"]))
        if not guild:
            mark_queue_item_processed(user_to_notify["user_id"], user_to_notify["guild_id"], today)
            return
        
        member = guild.get_member(int(user_to_notify["user_id"]))
        if not member:
            mark_queue_item_processed(user_to_notify["user_id"], user_to_notify["guild_id"], today)
            return
        
        # Rang-Update-Nachricht senden
        await ask_rank_update(member, user_to_notify["rank"], guild)
        
        # Benachrichtigung loggen
        log_notification(user_to_notify["user_id"], user_to_notify["rank"])
        
        # Queue-Item als verarbeitet markieren
        mark_queue_item_processed(user_to_notify["user_id"], user_to_notify["guild_id"], today)
        
        logger.info(f"Sent notification to {member.display_name} ({user_to_notify['rank']})")
        
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        mark_queue_item_processed(user_to_notify["user_id"], user_to_notify["guild_id"], today)

# Bot starten
# Custom ID Components für existierende Messages
class RankSelectDropdownWithCustomID(discord.ui.Select):
    def __init__(self, user_id, guild_id, custom_id):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        
        options = []
        for rank in ranks:
            option = discord.SelectOption(
                label=rank.capitalize(),
                value=rank,
                description=f"Setze {rank.capitalize()} als deinen Rang"
            )
            options.append(option)
        
        super().__init__(
            placeholder="🎮 Wähle deinen aktuellen Deadlock-Rang...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=custom_id
        )
    
    async def callback(self, interaction):
        selected_rank = self.values[0]
        guild = bot.get_guild(self.guild_id)
        member = guild.get_member(self.user_id) if guild else None
        
        if not guild or not member:
            await interaction.response.send_message("❌ Fehler: Server oder User nicht gefunden.", ephemeral=True)
            return
        
        await remove_all_rank_roles(member, guild)
        role = discord.utils.get(guild.roles, name=selected_rank.capitalize())
        if not role:
            role = await guild.create_role(name=selected_rank.capitalize())
        await member.add_roles(role)
        
        log_notification(str(self.user_id), selected_rank)
        await interaction.response.send_message(f"✅ Dein Rang wurde auf **{selected_rank.capitalize()}** gesetzt!", ephemeral=True)

class IntervalSelectDropdownWithCustomID(discord.ui.Select):
    def __init__(self, user_id, custom_id):
        self.user_id = int(user_id)
        
        options = [
            discord.SelectOption(label="1 Tag", value="1", description="Täglich benachrichtigen"),
            discord.SelectOption(label="3 Tage", value="3", description="Alle 3 Tage benachrichtigen"),
            discord.SelectOption(label="7 Tage", value="7", description="Wöchentlich benachrichtigen"),
            discord.SelectOption(label="14 Tage", value="14", description="Alle 2 Wochen benachrichtigen"),
            discord.SelectOption(label="30 Tage", value="30", description="Monatlich benachrichtigen")
        ]
        
        super().__init__(
            placeholder="⏰ Wähle dein Benachrichtigungs-Intervall...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=custom_id
        )

    async def callback(self, interaction):
        selected_interval = int(self.values[0])
        user_id = str(self.user_id)
        user_data = get_user_data(user_id)
        user_data['custom_interval'] = selected_interval
        save_user_data(user_id, user_data)
        
        await interaction.response.send_message(
            f"⏰ Benachrichtigungs-Intervall auf **{selected_interval} Tage** gesetzt!",
            ephemeral=True
        )

class NoNotificationButtonWithCustomID(discord.ui.Button):
    def __init__(self, user_id, guild_id, custom_id):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Keine Benachrichtigungen mehr",
            emoji="⏸️",
            custom_id=custom_id
        )

    async def callback(self, interaction):
        try:
            guild = bot.get_guild(self.guild_id)
            member = guild.get_member(self.user_id) if guild else None
            
            if not guild or not member:
                await interaction.response.send_message("❌ Fehler: Server oder User nicht gefunden.", ephemeral=True)
                return
            
            no_notification_role = discord.utils.get(guild.roles, id=NO_NOTIFICATION_ROLE_ID)
            if no_notification_role:
                await member.add_roles(no_notification_role)
            
            await interaction.response.send_message("⏸️ Du erhältst keine Benachrichtigungen mehr!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in NoNotificationButton callback: {e}")
            await interaction.response.send_message("❌ Ein Fehler ist aufgetreten.", ephemeral=True)

class NoDeadlockButtonWithCustomID(discord.ui.Button):
    def __init__(self, user_id, guild_id, custom_id):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Spiele kein Deadlock mehr",
            emoji="🚫",
            custom_id=custom_id
        )

    async def callback(self, interaction):
        try:
            guild = bot.get_guild(self.guild_id)
            member = guild.get_member(self.user_id) if guild else None
            
            if not guild or not member:
                await interaction.response.send_message("❌ Fehler: Server oder User nicht gefunden.", ephemeral=True)
                return
            
            await remove_all_rank_roles(member, guild)
            no_deadlock_role = discord.utils.get(guild.roles, id=NO_DEADLOCK_ROLE_ID)
            if no_deadlock_role:
                await member.add_roles(no_deadlock_role)
            
            await interaction.response.send_message("🚫 Du wurdest als 'Spielt kein Deadlock mehr' markiert!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in NoDeadlockButton callback: {e}")
            await interaction.response.send_message("❌ Ein Fehler ist aufgetreten.", ephemeral=True)

class FinishedButtonWithCustomID(discord.ui.Button):
    def __init__(self, user_id, guild_id, custom_id):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Fertig",
            emoji="✅",
            custom_id=custom_id
        )

    async def callback(self, interaction):
        embed = discord.Embed(
            title="✅ Einstellungen gespeichert!",
            description="Deine Rang- und Intervall-Einstellungen wurden erfolgreich gespeichert.",
            color=0x00ff00
        )
        
        await interaction.response.edit_message(embed=embed, view=None)
        track_dm_response(str(interaction.user.id))
        await asyncio.sleep(0.1)
        
        try:
            remove_persistent_view(str(interaction.message.id))
        except Exception as e:
            logger.error(f"Failed to remove persistent view: {e}")

# Original Component Copiers (behalten Emojis und Design)
class RankSelectDropdownFromOriginal(discord.ui.Select):
    def __init__(self, user_id, guild_id, original_component):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        
        # Kopiere alle Eigenschaften vom Original
        super().__init__(
            placeholder=original_component.placeholder,
            min_values=original_component.min_values,
            max_values=original_component.max_values,
            options=original_component.options,  # Behält Server-Emojis!
            custom_id=original_component.custom_id,  # Echte Custom ID!
            disabled=original_component.disabled
        )
    
    async def callback(self, interaction):
        selected_rank = self.values[0]
        guild = bot.get_guild(self.guild_id)
        member = guild.get_member(self.user_id) if guild else None
        
        if not guild or not member:
            await interaction.response.send_message("❌ Fehler: Server oder User nicht gefunden.", ephemeral=True)
            return
        
        await remove_all_rank_roles(member, guild)
        role = discord.utils.get(guild.roles, name=selected_rank.capitalize())
        if not role:
            role = await guild.create_role(name=selected_rank.capitalize())
        await member.add_roles(role)
        
        log_notification(str(self.user_id), selected_rank)
        await interaction.response.send_message(f"✅ Dein Rang wurde auf **{selected_rank.capitalize()}** gesetzt!", ephemeral=True)

class IntervalSelectDropdownFromOriginal(discord.ui.Select):
    def __init__(self, user_id, original_component):
        self.user_id = int(user_id)
        
        # Kopiere alle Eigenschaften vom Original
        super().__init__(
            placeholder=original_component.placeholder,
            min_values=original_component.min_values,
            max_values=original_component.max_values,
            options=original_component.options,  # Behält Server-Emojis!
            custom_id=original_component.custom_id,  # Echte Custom ID!
            disabled=original_component.disabled
        )

    async def callback(self, interaction):
        selected_interval = int(self.values[0])
        user_id = str(self.user_id)
        user_data = get_user_data(user_id)
        user_data['custom_interval'] = selected_interval
        save_user_data(user_id, user_data)
        
        await interaction.response.send_message(
            f"⏰ Benachrichtigungs-Intervall auf **{selected_interval} Tage** gesetzt!",
            ephemeral=True
        )

class NoNotificationButtonFromOriginal(discord.ui.Button):
    def __init__(self, user_id, guild_id, original_component):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        
        # Kopiere alle Eigenschaften vom Original
        super().__init__(
            style=original_component.style,
            label=original_component.label,
            emoji=original_component.emoji,  # Behält Server-Emoji!
            custom_id=original_component.custom_id,  # Echte Custom ID!
            disabled=original_component.disabled
        )

    async def callback(self, interaction):
        try:
            guild = bot.get_guild(self.guild_id)
            member = guild.get_member(self.user_id) if guild else None
            
            if not guild or not member:
                await interaction.response.send_message("❌ Fehler: Server oder User nicht gefunden.", ephemeral=True)
                return
            
            no_notification_role = discord.utils.get(guild.roles, id=NO_NOTIFICATION_ROLE_ID)
            if no_notification_role:
                await member.add_roles(no_notification_role)
            
            await interaction.response.send_message("⏸️ Du erhältst keine Benachrichtigungen mehr!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in NoNotificationButton callback: {e}")
            await interaction.response.send_message("❌ Ein Fehler ist aufgetreten.", ephemeral=True)

class NoDeadlockButtonFromOriginal(discord.ui.Button):
    def __init__(self, user_id, guild_id, original_component):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        
        # Kopiere alle Eigenschaften vom Original
        super().__init__(
            style=original_component.style,
            label=original_component.label,
            emoji=original_component.emoji,  # Behält Server-Emoji!
            custom_id=original_component.custom_id,  # Echte Custom ID!
            disabled=original_component.disabled
        )

    async def callback(self, interaction):
        try:
            guild = bot.get_guild(self.guild_id)
            member = guild.get_member(self.user_id) if guild else None
            
            if not guild or not member:
                await interaction.response.send_message("❌ Fehler: Server oder User nicht gefunden.", ephemeral=True)
                return
            
            await remove_all_rank_roles(member, guild)
            no_deadlock_role = discord.utils.get(guild.roles, id=NO_DEADLOCK_ROLE_ID)
            if no_deadlock_role:
                await member.add_roles(no_deadlock_role)
            
            await interaction.response.send_message("🚫 Du wurdest als 'Spielt kein Deadlock mehr' markiert!", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in NoDeadlockButton callback: {e}")
            await interaction.response.send_message("❌ Ein Fehler ist aufgetreten.", ephemeral=True)

class FinishedButtonFromOriginal(discord.ui.Button):
    def __init__(self, user_id, guild_id, original_component):
        self.user_id = int(user_id)
        self.guild_id = int(guild_id)
        
        # Kopiere alle Eigenschaften vom Original
        super().__init__(
            style=original_component.style,
            label=original_component.label,
            emoji=original_component.emoji,  # Behält Server-Emoji!
            custom_id=original_component.custom_id,  # Echte Custom ID!
            disabled=original_component.disabled
        )

    async def callback(self, interaction):
        embed = discord.Embed(
            title="✅ Einstellungen gespeichert!",
            description="Deine Rang- und Intervall-Einstellungen wurden erfolgreich gespeichert.",
            color=0x00ff00
        )
        
        await interaction.response.edit_message(embed=embed, view=None)
        track_dm_response(str(interaction.user.id))
        await asyncio.sleep(0.1)
        
        try:
            remove_persistent_view(str(interaction.message.id))
        except Exception as e:
            logger.error(f"Failed to remove persistent view: {e}")

if __name__ == "__main__":
    init_database()
    token = os.getenv("DISCORD_TOKEN_RANKED")  # genau dieses ENV wird geladen
    if not token:
        print("❌ FEHLER: Kein Ranked Discord Token gefunden!")
        print("Bitte in C:\\Users\\Nani-Admin\\Documents\\.env den Key DISCORD_TOKEN_RANKED= setzen")
        exit(1)
    bot.run(token)
