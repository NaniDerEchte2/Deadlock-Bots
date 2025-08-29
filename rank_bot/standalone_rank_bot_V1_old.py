"""
Standalone Deadlock Rank Bot
Separater Bot nur für Rank-Management mit Dropdown-Interface
"""

import discord
from discord.ext import commands, tasks
import sqlite3
import asyncio
from datetime import datetime, timedelta
import os
import logging

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

@bot.event
async def setup_hook():
    """Setup hook für persistent views"""
    logger.info("Setting up persistent views...")
    
    # Lade alle persistent views aus der Datenbank und re-registriere sie
    persistent_views = load_persistent_views()
    
    for message_id, channel_id, guild_id, view_type in persistent_views:
        try:
            guild = bot.get_guild(int(guild_id))
            if not guild:
                logger.warning(f"Guild {guild_id} not found for persistent view")
                continue
            
            if view_type == 'server_rank_select':
                view = ServerRankSelectView(guild)
                bot.add_view(view, message_id=int(message_id))
                logger.info(f"Re-registered ServerRankSelectView for message {message_id}")
                
        except Exception as e:
            logger.error(f"Failed to restore persistent view {message_id}: {e}")
    
    logger.info("Persistent views setup completed")

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
db_path = os.path.join(os.path.dirname(__file__), 'rank_data', 'standalone_rank_bot.db')
os.makedirs(os.path.dirname(db_path), exist_ok=True)

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
        
        # Tabelle für persistent views
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS persistent_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                view_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
    logger.info("Database initialized successfully")

def save_persistent_view(message_id: str, channel_id: str, guild_id: str, view_type: str):
    """Speichert eine persistent view in der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO persistent_views (message_id, channel_id, guild_id, view_type)
            VALUES (?, ?, ?, ?)
        ''', (message_id, channel_id, guild_id, view_type))
        conn.commit()

def cleanup_old_views(guild_id: str, view_type: str):
    """Entfernt alte Views des gleichen Typs aus der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM persistent_views 
            WHERE guild_id = ? AND view_type = ?
        ''', (guild_id, view_type))
        conn.commit()
        return cursor.rowcount

def load_persistent_views():
    """Lädt alle persistent views aus der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT message_id, channel_id, guild_id, view_type FROM persistent_views')
        return cursor.fetchall()

def get_user_data(user_id: str) -> dict:
    """Lädt User-Daten aus der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT custom_interval, paused_until FROM user_data WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if result:
            return {
                'custom_interval': result[0],
                'paused_until': result[1]
            }
        return {}

def save_user_data(user_id: str, data: dict):
    """Speichert User-Daten in der Datenbank"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO user_data (user_id, custom_interval, paused_until, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, data.get('custom_interval'), data.get('paused_until')))
        conn.commit()

def get_user_current_rank(member):
    """Ermittelt aktuellen Rang basierend auf Discord-Rollen"""
    for role in member.roles:
        if role.name.lower() in ranks:
            return role.name.lower()
    return None

async def remove_all_rank_roles(member, guild):
    """Entfernt alle Rang-Rollen von einem User"""
    for role in member.roles:
        if role.name.lower() in ranks:
            await member.remove_roles(role)

# UI Components
class RankSelectView(discord.ui.View):
    def __init__(self, member, current_rank, guild):
        super().__init__(timeout=300)
        self.member = member
        self.current_rank = current_rank
        self.guild = guild
        
        # Rang-Dropdown hinzufügen
        self.add_item(RankSelectDropdown(member, current_rank, guild))
        
        # Intervall-Dropdown hinzufügen
        self.add_item(IntervalSelectDropdown(member))
        
        # Spezial-Buttons hinzufügen
        self.add_item(NoNotificationButton())
        self.add_item(NoDeadlockButton())
        self.add_item(FinishedButton())

class RankSelectDropdown(discord.ui.Select):
    def __init__(self, member, current_rank, guild):
        self.member = member
        self.current_rank = current_rank
        self.guild = guild
        
        # Erstelle Optionen für jeden Rang
        options = []
        for rank in ranks:
            emoji = discord.utils.get(guild.emojis, name=rank)
            is_current = rank == current_rank
            
            option = discord.SelectOption(
                label=rank.capitalize(),
                value=rank,
                description=f"Wähle {rank.capitalize()} als deinen Rang",
                emoji=emoji,
                default=is_current
            )
            options.append(option)
        
        super().__init__(
            placeholder="🎮 Wähle deinen Deadlock-Rang...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction):
        selected_rank = self.values[0]
        
        # Alle Rang-Rollen entfernen
        await remove_all_rank_roles(self.member, self.guild)
        
        # Neue Rolle hinzufügen
        role = discord.utils.get(self.guild.roles, name=selected_rank.capitalize())
        if not role:
            role = await self.guild.create_role(name=selected_rank.capitalize())
        await self.member.add_roles(role)
        
        # Phantom+ Benachrichtigung senden
        if selected_rank in ["phantom", "ascendant", "eternus"]:
            notification_channel = self.guild.get_channel(PHANTOM_NOTIFICATION_CHANNEL_ID)
            if notification_channel:
                rank_emoji = discord.utils.get(self.guild.emojis, name=selected_rank)
                emoji_display = str(rank_emoji) if rank_emoji else ""
                
                notification_embed = discord.Embed(
                    title="🔥 Phantom+ Rang Update",
                    description=f"{emoji_display} **{self.member.display_name}** hat sich den Rang **{selected_rank.capitalize()}** gegeben!",
                    color=0xff6b35
                )
                notification_embed.add_field(
                    name="User", 
                    value=f"{self.member.mention} ({self.member.id})", 
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
        rank_emoji = discord.utils.get(self.guild.emojis, name=selected_rank)
        await interaction.response.send_message(
            f"✅ {rank_emoji} Rang erfolgreich auf **{selected_rank.capitalize()}** gesetzt!",
            ephemeral=True
        )

class IntervalSelectDropdown(discord.ui.Select):
    def __init__(self, member):
        self.member = member
        
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
            options=options
        )

    async def callback(self, interaction):
        selected_interval = int(self.values[0])
        
        # User-spezifisches Intervall speichern
        user_id = str(self.member.id)
        user_data = get_user_data(user_id)
        user_data['custom_interval'] = selected_interval
        save_user_data(user_id, user_data)
        
        # Bestätigung senden ohne das View zu entfernen
        await interaction.response.send_message(
            f"⏰ Benachrichtigungs-Intervall auf **{selected_interval} Tage** gesetzt!",
            ephemeral=True
        )

class NoNotificationButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Keine Benachrichtigungen mehr",
            emoji="⏸️"
        )

    async def callback(self, interaction):
        member = interaction.user
        guild = interaction.guild
        
        # "Keine Benachrichtigung mehr" Rolle hinzufügen (Rang bleibt erhalten)
        no_notification_role = discord.utils.get(guild.roles, id=NO_NOTIFICATION_ROLE_ID)
        if no_notification_role:
            await member.add_roles(no_notification_role)
        
        embed = discord.Embed(
            title="⏸️ Keine Benachrichtigungen mehr",
            description="Du wirst nicht mehr nach deinem Rang gefragt.\n\nDein aktueller Rang bleibt erhalten! Du kannst deinen Rang weiterhin jederzeit manuell im Rang-Kanal ändern.",
            color=0xff9900
        )
        
        await interaction.response.edit_message(embed=embed, view=None)
        
        # Nachricht nach 5 Minuten löschen
        await asyncio.sleep(300)
        try:
            await interaction.delete_original_response()
        except:
            pass

class NoDeadlockButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Spiele kein Deadlock mehr",
            emoji="🚫"
        )

    async def callback(self, interaction):
        member = interaction.user
        guild = interaction.guild
        
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
        
        # Nachricht nach 5 Minuten löschen
        await asyncio.sleep(300)
        try:
            await interaction.delete_original_response()
        except:
            pass

class FinishedButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Fertig",
            emoji="✅"
        )

    async def callback(self, interaction):
        embed = discord.Embed(
            title="✅ Einstellungen gespeichert!",
            description="Deine Rang- und Intervall-Einstellungen wurden erfolgreich gespeichert.",
            color=0x00ff00
        )
        
        await interaction.response.edit_message(embed=embed, view=None)
        
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
        await interaction.response.send_message(
            f"✅ {rank_emoji} Dein Rang wurde erfolgreich auf **{selected_rank.capitalize()}** gesetzt!",
            ephemeral=True
        )

async def ask_rank_update(member, current_rank, guild):
    """Sendet Rang-Update-Nachricht mit Dropdown-Interface"""
    try:
        # Emoji für aktuellen Rang finden
        current_emoji = discord.utils.get(guild.emojis, name=current_rank)
        emoji_display = str(current_emoji) if current_emoji else ""
        
        # User-spezifisches Intervall laden
        user_data = get_user_data(str(member.id))
        custom_interval = user_data.get('custom_interval')
        interval_days = custom_interval if custom_interval else RANK_INTERVALS.get(current_rank, 30)
        
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
        
        # Dropdown-View erstellen
        view = RankSelectView(member, current_rank, guild)
        
        message = await member.send(embed=embed, view=view)
        
    except discord.Forbidden:
        # Falls DM nicht möglich ist, ignorieren
        logger.warning(f"Could not send DM to {member.display_name} ({member.id})")

# Commands
@bot.command(name='rsetup')
@commands.has_permissions(administrator=True)
async def setup_rank_roles(ctx):
    """Erstellt Rang-Auswahl-Nachricht mit Dropdown (nur für User sichtbar)"""
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
    """Test-Command: Sendet Rang-Update-Message an User"""
    guild = ctx.guild
    
    # Wenn kein User angegeben, verwende ersten Test-User
    if user is None:
        if not test_users:
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
    """Setzt die Test-User für Daily-Check"""
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
    
    embed = discord.Embed(title="✅ Test-User gesetzt", color=0x00ff00)
    user_list = []
    
    for user in test_users:
        current_rank = get_user_current_rank(user)
        user_list.append(f"{user.mention} - Rang: {current_rank or 'Kein Rang'}")
    
    embed.add_field(name="👥 Test-User", value="\n".join(user_list), inline=False)
    embed.add_field(name="ℹ️ Info", value="Diese User werden beim Daily-Check benachrichtigt", inline=False)
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    
    await ctx.send(embed=embed)

@bot.command(name='rstart')
@commands.has_permissions(administrator=True)
async def start_rank_notifications(ctx):
    """Startet das automatische Benachrichtigungs-System"""
    logger.info(f"rstart command called by {ctx.author} from Standalone Rank Bot")
    
    # Tasks starten
    if not daily_queue_preparation.is_running():
        daily_queue_preparation.start()
    
    if not process_notification_queue.is_running():
        process_notification_queue.start()
    
    mode = "Test-Modus" if test_users else "Live-Betrieb"
    interval = "alle 30 Sekunden" if test_users else "alle 3 Minuten"
    
    embed = discord.Embed(
        title="✅ Standalone Rank Bot gestartet!",
        description=f"**Modus:** {mode}\n**Intervall:** {interval}",
        color=0x00ff00
    )
    embed.add_field(name="📅 Queue-Erstellung", value="Täglich um 7:00 Uhr", inline=True)
    embed.add_field(name="⏰ Aktive Zeiten", value="8-22 Uhr deutsche Zeit", inline=True)
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    
    await ctx.send(embed=embed)

@bot.command(name='rstop')
@commands.has_permissions(administrator=True)
async def stop_rank_notifications(ctx):
    """Stoppt das automatische Benachrichtigungs-System"""
    if daily_queue_preparation.is_running():
        daily_queue_preparation.stop()
    
    if process_notification_queue.is_running():
        process_notification_queue.stop()
    
    embed = discord.Embed(
        title="🛑 Rank Bot gestoppt!",
        description="Automatische Benachrichtigungen wurden gestoppt.",
        color=0xff6600
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)

@bot.command(name='rdb')
@commands.has_permissions(administrator=True)
async def view_database(ctx, table: str = None):
    """Zeigt Datenbank-Inhalte an"""
    if not table:
        # Übersicht aller Tabellen
        embed = discord.Embed(
            title="📊 Rank Bot Datenbank",
            description="Verfügbare Tabellen:",
            color=0x0099ff
        )
        
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            
            # User Data Count
            cursor.execute('SELECT COUNT(*) FROM user_data')
            user_count = cursor.fetchone()[0]
            
            # Notification Log Count
            cursor.execute('SELECT COUNT(*) FROM notification_log')
            notification_count = cursor.fetchone()[0]
            
            # Queue Count (today)
            today = datetime.now().strftime('%Y-%m-%d')
            cursor.execute('SELECT COUNT(*) FROM notification_queue WHERE queue_date = ?', (today,))
            queue_count = cursor.fetchone()[0]
            
            # Persistent Views Count
            cursor.execute('SELECT COUNT(*) FROM persistent_views')
            views_count = cursor.fetchone()[0]
        
        embed.add_field(name="📋 Verfügbare Tabellen", value="`users` - User-Einstellungen\n`notifications` - Benachrichtigungs-Log\n`queue` - Heutige Queue\n`views` - Persistent Views", inline=False)
        embed.add_field(name="📊 Statistiken", value=f"👥 User: {user_count}\n📧 Notifications: {notification_count}\n📋 Queue heute: {queue_count}\n🖼️ Views: {views_count}", inline=False)
        embed.add_field(name="🔧 Verwendung", value="`!rdb users` - User-Daten\n`!rdb notifications` - Letzte Benachrichtigungen\n`!rdb queue` - Heutige Queue\n`!rdb views` - Persistent Views", inline=False)
        
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
    
    elif table.lower() == 'users':
        # User Data
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_id, custom_interval, paused_until FROM user_data ORDER BY updated_at DESC LIMIT 10')
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
        # Queue Data
        today = datetime.now().strftime('%Y-%m-%d')
        queue_data = load_queue_data(today)
        
        embed = discord.Embed(title=f"📋 Queue ({today})", color=0x0099ff)
        embed.add_field(name="📊 Anzahl", value=str(len(queue_data)), inline=True)
        
        if queue_data:
            user_list = []
            for i, item in enumerate(queue_data[:10]):  # Zeige erste 10
                try:
                    guild = bot.get_guild(int(item['guild_id']))
                    if guild:
                        member = guild.get_member(int(item['user_id']))
                        if member:
                            user_list.append(f"{i+1}. {member.display_name} ({item['rank']})")
                        else:
                            user_list.append(f"{i+1}. User {item['user_id']} ({item['rank']})")
                except:
                    user_list.append(f"{i+1}. Error loading user {item['user_id']}")
            
            if len(queue_data) > 10:
                user_list.append(f"... und {len(queue_data) - 10} weitere")
            
            embed.add_field(name="👥 Queue", value="\n".join(user_list), inline=False)
        else:
            embed.add_field(name="👥 Queue", value="Leer", inline=False)
        
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
    
    elif table.lower() == 'views':
        # Persistent Views
        persistent_views = load_persistent_views()
        
        embed = discord.Embed(title="🖼️ Persistent Views", color=0x0099ff)
        embed.add_field(name="📊 Anzahl", value=str(len(persistent_views)), inline=True)
        
        if persistent_views:
            view_list = []
            for message_id, channel_id, guild_id, view_type in persistent_views:
                try:
                    guild = bot.get_guild(int(guild_id))
                    guild_name = guild.name if guild else f"Guild {guild_id}"
                    channel = bot.get_channel(int(channel_id))
                    channel_name = channel.name if channel else f"Channel {channel_id}"
                    view_list.append(f"**{view_type}**: {guild_name} #{channel_name}\nMsg: {message_id}")
                except:
                    view_list.append(f"**{view_type}**: Error loading")
            
            embed.add_field(name="🖼️ Views", value="\n".join(view_list), inline=False)
        else:
            embed.add_field(name="🖼️ Views", value="Keine Views gespeichert", inline=False)
        
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

@bot.command(name='rcleanup')
@commands.has_permissions(administrator=True)
async def cleanup_user_dms(ctx, user: discord.Member = None, confirm: str = None):
    """Löscht alle Bot-DMs eines Users (für Testing/Cleanup)"""
    if not user:
        embed = discord.Embed(
            title="❌ Kein User angegeben",
            description="Verwendung: `!rcleanup @user confirm`\n\n⚠️ **WARNUNG**: Dies löscht ALLE DM-Nachrichten zwischen dem Bot und dem User!",
            color=0xff0000
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return
    
    # Sicherheitsabfrage
    if confirm != 'confirm':
        embed = discord.Embed(
            title="⚠️ DM Cleanup Bestätigung",
            description=f"**WARNUNG**: Du bist dabei, ALLE DM-Nachrichten zwischen dem Bot und {user.mention} zu löschen!\n\nDies umfasst:\n• Alle Rang-Update-Nachrichten\n• Alle Dropdown-Menüs\n• Alle Bot-Antworten\n\n**Dies kann NICHT rückgängig gemacht werden!**",
            color=0xff6600
        )
        embed.add_field(
            name="🔧 Bestätigung erforderlich",
            value=f"Führe aus: `!rcleanup {user.mention} confirm`",
            inline=False
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return
    
    # Cleanup durchführen
    try:
        embed = discord.Embed(
            title="🧹 DM Cleanup gestartet",
            description=f"Lösche alle Bot-DMs mit {user.mention}...",
            color=0xffaa00
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        status_msg = await ctx.send(embed=embed)
        
        # DM Channel mit User abrufen
        dm_channel = user.dm_channel
        if not dm_channel:
            dm_channel = await user.create_dm()
        
        deleted_count = 0
        bot_messages = []
        
        # Alle Nachrichten im DM Channel durchgehen
        async for message in dm_channel.history(limit=None):
            if message.author == bot.user:
                bot_messages.append(message)
        
        # Bot-Nachrichten löschen (in Batches wegen Rate Limiting)
        for message in bot_messages:
            try:
                await message.delete()
                deleted_count += 1
                
                # Rate limiting - kurz warten zwischen Löschungen
                if deleted_count % 5 == 0:
                    await asyncio.sleep(1)
                    
            except discord.NotFound:
                # Nachricht bereits gelöscht
                pass
            except discord.Forbidden:
                # Keine Berechtigung (sollte nicht passieren bei eigenen Nachrichten)
                pass
            except Exception as e:
                logger.warning(f"Error deleting message {message.id}: {e}")
        
        # Erfolgs-Nachricht
        embed = discord.Embed(
            title="✅ DM Cleanup abgeschlossen",
            description=f"Erfolgreich **{deleted_count}** Bot-Nachrichten mit {user.mention} gelöscht!",
            color=0x00ff00
        )
        embed.add_field(
            name="🧹 Gelöscht",
            value=f"• {deleted_count} Bot-Nachrichten\n• Alle Rang-Update-DMs\n• Alle Dropdown-Menüs",
            inline=False
        )
        embed.add_field(
            name="ℹ️ Info",
            value="Der User kann jetzt wieder 'frische' DMs erhalten",
            inline=False
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        
        await status_msg.edit(embed=embed)
        
    except discord.Forbidden:
        embed = discord.Embed(
            title="❌ Keine Berechtigung",
            description="Bot kann nicht auf DMs mit diesem User zugreifen.",
            color=0xff0000
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Error during DM cleanup: {e}")
        embed = discord.Embed(
            title="❌ Fehler beim Cleanup",
            description=f"Ein Fehler ist aufgetreten: {str(e)[:200]}",
            color=0xff0000
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)

@bot.command(name='rcleanup_all')
@commands.has_permissions(administrator=True)
async def cleanup_all_test_users(ctx, confirm: str = None):
    """Löscht alle Bot-DMs aller Test-User (für Testing)"""
    if not test_users:
        embed = discord.Embed(
            title="❌ Keine Test-User",
            description="Keine Test-User gesetzt! Verwende `!rtest_users @user1 @user2` zuerst.",
            color=0xff0000
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return
    
    # Sicherheitsabfrage
    if confirm != 'confirm':
        user_list = [user.mention for user in test_users]
        embed = discord.Embed(
            title="⚠️ Massen-DM Cleanup Bestätigung",
            description=f"**WARNUNG**: Du bist dabei, ALLE DM-Nachrichten zwischen dem Bot und **{len(test_users)} Test-Usern** zu löschen!",
            color=0xff6600
        )
        embed.add_field(
            name="👥 Betroffene User",
            value="\n".join(user_list),
            inline=False
        )
        embed.add_field(
            name="🔧 Bestätigung erforderlich",
            value="`!rcleanup_all confirm`",
            inline=False
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return
    
    # Cleanup für alle Test-User
    embed = discord.Embed(
        title="🧹 Massen-DM Cleanup gestartet",
        description=f"Lösche alle Bot-DMs mit {len(test_users)} Test-Usern...",
        color=0xffaa00
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    status_msg = await ctx.send(embed=embed)
    
    total_deleted = 0
    processed_users = 0
    
    for user in test_users:
        try:
            # DM Channel mit User abrufen
            dm_channel = user.dm_channel
            if not dm_channel:
                dm_channel = await user.create_dm()
            
            user_deleted = 0
            bot_messages = []
            
            # Alle Bot-Nachrichten sammeln
            async for message in dm_channel.history(limit=None):
                if message.author == bot.user:
                    bot_messages.append(message)
            
            # Bot-Nachrichten löschen
            for message in bot_messages:
                try:
                    await message.delete()
                    user_deleted += 1
                    total_deleted += 1
                    
                    # Rate limiting
                    if user_deleted % 3 == 0:
                        await asyncio.sleep(0.5)
                        
                except:
                    pass
            
            processed_users += 1
            logger.info(f"Cleaned {user_deleted} messages for {user.display_name}")
            
            # Zwischen Usern warten
            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(f"Error cleaning DMs for {user.display_name}: {e}")
    
    # Erfolgs-Nachricht
    embed = discord.Embed(
        title="✅ Massen-DM Cleanup abgeschlossen",
        description=f"Erfolgreich **{total_deleted}** Bot-Nachrichten von **{processed_users}** Test-Usern gelöscht!",
        color=0x00ff00
    )
    embed.add_field(
        name="📊 Statistiken",
        value=f"• {processed_users}/{len(test_users)} User verarbeitet\n• {total_deleted} Nachrichten gelöscht\n• Alle Test-User haben jetzt 'saubere' DMs",
        inline=False
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    
    await status_msg.edit(embed=embed)

@bot.command(name='rfind_dms')
@commands.has_permissions(administrator=True)
async def find_all_dm_users(ctx, deep_search: str = None):
    """Findet alle User mit denen der Bot DMs hat"""
    is_deep_search = deep_search == 'deep'
    
    # Timeout für Deep Search (10 Minuten max)
    if is_deep_search:
        try:
            return await asyncio.wait_for(_find_dms_internal(ctx, is_deep_search), timeout=600.0)
        except asyncio.TimeoutError:
            timeout_embed = discord.Embed(
                title="⏰ Deep Search Timeout",
                description="🚨 **Deep Search abgebrochen** nach 10 Minuten!\n\nWahrscheinlich ist ein User-DM hängen geblieben.",
                color=0xff0000
            )
            timeout_embed.add_field(
                name="🔧 Lösungen",
                value="• `!rfind_dms` - Quick Search versuchen\n• `!rreload` - Bot neu starten\n• Später nochmal versuchen",
                inline=False
            )
            timeout_embed.set_footer(text="🎮 Deadlock Rank Bot")
            await ctx.send(embed=timeout_embed)
            return
    else:
        return await _find_dms_internal(ctx, is_deep_search)

async def _find_dms_internal(ctx, is_deep_search):
    start_time = datetime.now()
    logger.info(f"[DEEP SEARCH] Starting _find_dms_internal at {start_time.strftime('%H:%M:%S')}")
    
    # Problem-User die geskippt werden sollen (falls bekannt)
    PROBLEM_USERS = {
        "josua / mathegenie",  # User der immer hängt
        # Weitere können hier hinzugefügt werden
    }
    
    embed = discord.Embed(
        title="🔍 Suche nach Bot-DMs...",
        description="Durchsuche alle DM-Channels nach Bot-Nachrichten...\n\n" + 
                   ("🔬 **Deep Search**: Versuche aktiv DM-Channels zu öffnen" if is_deep_search else 
                    "⚡ **Quick Search**: Nur bereits geladene DM-Channels\n💡 Tipp: `!rfind_dms deep` für vollständige Suche"),
        color=0xffaa00
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    status_msg = await ctx.send(embed=embed)
    
    dm_users = []
    total_messages = 0
    checked_users = 0
    failed_users = 0
    skipped_users = 0
    
    # Durchgehe alle User die der Bot kennt
    logger.info(f"[DEEP SEARCH] Starting to process guilds and members")
    for guild in bot.guilds:
        logger.debug(f"[DEEP SEARCH] Processing guild: {guild.name} with {len(guild.members)} members")
        for member in guild.members:
            if member.bot or member == bot.user:
                continue
            
            # Skip bekannte Problem-User bei Deep Search
            if is_deep_search and member.display_name.lower() in PROBLEM_USERS:
                logger.info(f"[DEEP SEARCH] Skipping known problem user: {member.display_name}")
                skipped_users += 1
                continue
            
            checked_users += 1
            
            # Progress Update alle 50 User bei Deep Search
            if is_deep_search and checked_users % 50 == 0:
                progress_embed = discord.Embed(
                    title="🔍 Deep Search läuft...",
                    description=f"Fortschritt: **{checked_users}** User gecheckt\n\nAktuell: {member.display_name}",
                    color=0xffaa00
                )
                progress_embed.add_field(
                    name="📊 Zwischenstand",
                    value=f"• Gefunden: {len(dm_users)} User mit DMs\n• Nachrichten: {total_messages}\n• Fehler: {failed_users}",
                    inline=False
                )
                progress_embed.set_footer(text="🎮 Deadlock Rank Bot")
                await status_msg.edit(embed=progress_embed)
            
            try:
                dm_channel = None
                
                if is_deep_search:
                    # Deep Search: Versuche aktiv DM Channel zu erstellen/abrufen (mit Timeout)
                    try:
                        dm_channel = await asyncio.wait_for(member.create_dm(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning(f"[DEEP SEARCH] Timeout creating DM with {member.display_name}")
                        failed_users += 1
                        continue
                    except discord.Forbidden:
                        # User erlaubt keine DMs von Servermitgliedern
                        continue
                    except Exception as e:
                        failed_users += 1
                        continue
                else:
                    # Quick Search: Nur bereits geladene DM Channels
                    dm_channel = member.dm_channel
                    if not dm_channel:
                        continue
                
                if not dm_channel:
                    continue
                
                # Zähle Bot-Nachrichten in diesem DM
                message_count = 0
                last_message = None
                
                try:
                    # Message History mit Timeout (max 10 Sekunden pro User)
                    async def check_messages():
                        count = 0
                        last_msg = None
                        async for message in dm_channel.history(limit=50):  # Nur erste 50 checken für Performance
                            if message.author == bot.user:
                                count += 1
                                if not last_msg:
                                    last_msg = message.created_at
                            # Extra safety: max 50 messages
                            if count >= 50:
                                break
                        return count, last_msg
                    
                    message_count, last_message = await asyncio.wait_for(check_messages(), timeout=10.0)
                    
                except asyncio.TimeoutError:
                    logger.warning(f"[DEEP SEARCH] Timeout checking message history for {member.display_name}")
                    failed_users += 1
                    continue
                except discord.Forbidden:
                    # Kein Zugriff auf DM-Verlauf
                    continue
                except Exception as e:
                    logger.warning(f"[DEEP SEARCH] Error checking messages for {member.display_name}: {e}")
                    failed_users += 1
                    continue
                
                if message_count > 0:
                    dm_users.append({
                        'user': member,
                        'messages': message_count,
                        'last_message': last_message,
                        'guild': guild.name
                    })
                    total_messages += message_count
                
                # Bei Deep Search: kurze Pause für Rate Limiting
                if is_deep_search and checked_users % 10 == 0:
                    await asyncio.sleep(0.5)
                
            except Exception as e:
                failed_users += 1
                logger.debug(f"Error checking DMs for {member.display_name}: {e}")
    
    # Sortiere nach letzter Nachricht (neueste zuerst)
    logger.info(f"[DEEP SEARCH] Sorting {len(dm_users)} found users by last message date")
    dm_users.sort(key=lambda x: x['last_message'] if x['last_message'] else datetime.min, reverse=True)
    
    # Erstelle Ergebnis-Embed mit Timeout-Schutz
    logger.info(f"[DEEP SEARCH] Creating final results embed")
    try:
        search_type = "🔬 Deep Search" if is_deep_search else "⚡ Quick Search"
        embed = discord.Embed(
            title="📋 Bot-DM Übersicht",
            description=f"{search_type}\n\nGefunden: **{len(dm_users)} User** mit **{total_messages} Bot-Nachrichten**",
            color=0x0099ff
        )
        logger.info(f"[DEEP SEARCH] Basic embed created successfully")
    except Exception as e:
        logger.error(f"[DEEP SEARCH] Error creating basic embed: {e}")
        # Fallback: send simple message and return
        try:
            await ctx.send(f"🔍 Deep Search Ergebnis: {len(dm_users)} User mit {total_messages} Bot-Nachrichten gefunden. Fehler beim Erstellen der Übersicht.")
        except:
            pass
        return
    
    # Search-Statistiken hinzufügen
    logger.info(f"[DEEP SEARCH] Building statistics field")
    try:
        stats_text = f"• Geprüfte User: {checked_users}\n• Gefundene DMs: {len(dm_users)}\n• Fehler: {failed_users}"
        if skipped_users > 0:
            stats_text += f"\n• Geskippt: {skipped_users} (Problem-User)"
        stats_text += f"\n• Erfolgsrate: {((checked_users - failed_users) / max(checked_users, 1) * 100):.1f}%"
        
        embed.add_field(
            name="📊 Search-Statistiken",
            value=stats_text,
            inline=True
        )
        logger.info(f"[DEEP SEARCH] Statistics field added successfully")
    except Exception as e:
        logger.error(f"[DEEP SEARCH] Error adding statistics field: {e}")
        # Continue without statistics
    
    if dm_users:
        logger.info(f"[DEEP SEARCH] Building user list for {len(dm_users)} users")
        try:
            # Top 15 User anzeigen
            user_list = []
            for i, data in enumerate(dm_users[:15]):
                try:
                    user = data['user']
                    messages = data['messages']
                    last_msg = data['last_message']
                    guild = data['guild']
                    
                    if last_msg:
                        time_str = last_msg.strftime('%d.%m %H:%M')
                    else:
                        time_str = "Unbekannt"
                    
                    user_list.append(f"{i+1}. **{user.display_name}** ({guild})\n   📧 {messages} Nachrichten | 🕐 {time_str}")
                except Exception as e:
                    logger.warning(f"[DEEP SEARCH] Error formatting user {i}: {e}")
                    user_list.append(f"{i+1}. Fehler beim Laden des Users")
            
            if len(dm_users) > 15:
                user_list.append(f"\n... und **{len(dm_users) - 15}** weitere User")
            
            logger.info(f"[DEEP SEARCH] Adding user list field with {len(user_list)} entries")
            embed.add_field(
                name="👥 User mit Bot-DMs",
                value="\n".join(user_list),
                inline=False
            )
            
            logger.info(f"[DEEP SEARCH] Adding cleanup options field")
            embed.add_field(
                name="🧹 Cleanup Optionen",
                value="`!rcleanup_found confirm` - Alle gefundenen User cleanen\n"
                      "`!rcleanup @user confirm` - Einzelnen User cleanen",
                inline=False
            )
            logger.info(f"[DEEP SEARCH] User list and cleanup fields added successfully")
        except Exception as e:
            logger.error(f"[DEEP SEARCH] Error building user list: {e}")
            # Add simplified user count instead
            embed.add_field(
                name="👥 User mit Bot-DMs",
                value=f"**{len(dm_users)} User** gefunden, aber Fehler beim Anzeigen der Details",
                inline=False
            )
    else:
        embed.add_field(
            name="✅ Keine DMs gefunden",
            value="Der Bot hat aktuell keine DMs mit Usern.",
            inline=False
        )
    
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    
    # Final result update with error handling
    try:
        logger.info(f"[DEEP SEARCH] Completing search - found {len(dm_users)} users with {total_messages} messages")
        await status_msg.edit(embed=embed)
        logger.info(f"[DEEP SEARCH] Successfully updated final results embed")
    except discord.NotFound:
        logger.error(f"[DEEP SEARCH] Status message not found when trying to update final results")
        # Try to send new message instead
        try:
            await ctx.send(embed=embed)
            logger.info(f"[DEEP SEARCH] Sent new message with final results after status message was lost")
        except Exception as e:
            logger.error(f"[DEEP SEARCH] Failed to send final results as new message: {e}")
    except discord.HTTPException as e:
        logger.error(f"[DEEP SEARCH] HTTP error updating final results: {e}")
        # If embed is too large, try a simplified version
        if "too large" in str(e).lower() or "2000" in str(e):
            simple_embed = discord.Embed(
                title="📋 Bot-DM Übersicht",
                description=f"Gefunden: **{len(dm_users)} User** mit **{total_messages} Bot-Nachrichten**\n\n⚠️ Vollständige Liste zu groß - verwende `!rcleanup_found confirm` für Cleanup",
                color=0x0099ff
            )
            simple_embed.set_footer(text="🎮 Deadlock Rank Bot")
            try:
                await status_msg.edit(embed=simple_embed)
                logger.info(f"[DEEP SEARCH] Updated with simplified embed due to size limit")
            except Exception as e2:
                logger.error(f"[DEEP SEARCH] Failed to update even simplified embed: {e2}")
    except Exception as e:
        logger.error(f"[DEEP SEARCH] Unexpected error updating final results: {e}")
        # Last resort - try to send a simple text message
        try:
            await ctx.send(f"🔍 Deep Search abgeschlossen: {len(dm_users)} User mit {total_messages} Bot-Nachrichten gefunden")
            logger.info(f"[DEEP SEARCH] Sent simple text message as final fallback")
        except Exception as e2:
            logger.error(f"[DEEP SEARCH] Even text message fallback failed: {e2}")
    
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    logger.info(f"[DEEP SEARCH] Function _find_dms_internal completed successfully in {duration:.2f} seconds")
    return

@bot.command(name='rcleanup_found')
@commands.has_permissions(administrator=True)
async def cleanup_all_dm_users(ctx, confirm: str = None):
    """Löscht Bot-DMs mit ALLEN gefundenen Usern"""
    # Sicherheitsabfrage
    if confirm != 'confirm':
        embed = discord.Embed(
            title="⚠️ GLOBALER DM Cleanup Bestätigung",
            description="**WARNUNG**: Du bist dabei, ALLE Bot-DMs mit ALLEN Usern zu löschen!\n\n🔥 **DIES BETRIFFT ALLE USER IM SERVER, NICHT NUR TEST-USER!**\n\nDies kann NICHT rückgängig gemacht werden!",
            color=0xff0000
        )
        embed.add_field(
            name="🚨 Was passiert",
            value="• Alle Bot-Nachrichten in allen DM-Channels werden gelöscht\n• Alle Rang-Update-Nachrichten verschwinden\n• Alle Dropdown-Menüs werden gelöscht\n• User-Nachrichten bleiben erhalten",
            inline=False
        )
        embed.add_field(
            name="🔧 Bestätigung erforderlich",
            value="`!rcleanup_found confirm`\n\n⚠️ **Verwende zuerst `!rfind_dms` um zu sehen welche User betroffen sind!**",
            inline=False
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await ctx.send(embed=embed)
        return
    
    # Finde alle User mit Bot-DMs (mit Deep Search)
    embed = discord.Embed(
        title="🔍 Suche nach allen Bot-DMs...",
        description="🔬 Deep Search: Sammle alle User mit Bot-Nachrichten...",
        color=0xffaa00
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    status_msg = await ctx.send(embed=embed)
    
    dm_users = []
    checked_users = 0
    
    # Durchgehe alle User (mit Deep Search)
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot or member == bot.user:
                continue
            
            checked_users += 1
            
            # Progress Update alle 50 User
            if checked_users % 50 == 0:
                progress_embed = discord.Embed(
                    title="🔍 Deep Search für Cleanup...",
                    description=f"Fortschritt: **{checked_users}** User gecheckt\n\nAktuell: {member.display_name}\n\nGefunden: {len(dm_users)} User mit Bot-DMs",
                    color=0xffaa00
                )
                progress_embed.set_footer(text="🎮 Deadlock Rank Bot")
                await status_msg.edit(embed=progress_embed)
            
            try:
                # Deep Search: Versuche aktiv DM Channel zu bekommen
                try:
                    dm_channel = await member.create_dm()
                except discord.Forbidden:
                    continue
                except Exception:
                    continue
                
                if not dm_channel:
                    continue
                
                # Check ob Bot-Nachrichten existieren
                has_bot_messages = False
                try:
                    async for message in dm_channel.history(limit=10):
                        if message.author == bot.user:
                            has_bot_messages = True
                            break
                except discord.Forbidden:
                    continue
                except Exception:
                    continue
                
                if has_bot_messages:
                    dm_users.append(member)
                
                # Rate limiting
                if checked_users % 10 == 0:
                    await asyncio.sleep(0.3)
                    
            except Exception:
                pass
    
    if not dm_users:
        embed = discord.Embed(
            title="✅ Keine DMs gefunden",
            description="Der Bot hat keine DMs mit Usern zu löschen.",
            color=0x00ff00
        )
        embed.set_footer(text="🎮 Deadlock Rank Bot")
        await status_msg.edit(embed=embed)
        return
    
    # Cleanup für alle gefundenen User
    embed = discord.Embed(
        title="🧹 GLOBALER DM Cleanup gestartet",
        description=f"Lösche alle Bot-DMs mit **{len(dm_users)} Usern**...\n\n⏱️ Dies kann einige Minuten dauern!",
        color=0xff6600
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await status_msg.edit(embed=embed)
    
    total_deleted = 0
    processed_users = 0
    failed_users = 0
    
    for i, user in enumerate(dm_users):
        try:
            # Update Progress bei jedem User (nicht nur alle 5)
            progress_embed = discord.Embed(
                title="🧹 GLOBALER DM Cleanup läuft...",
                description=f"Fortschritt: **{i+1}/{len(dm_users)}** User verarbeitet\n\n👤 Aktuell: **{user.display_name}**\n⏱️ Verarbeitungszeit: ~{(i+1)*2}s",
                color=0xffaa00
            )
            progress_embed.add_field(
                name="📊 Live-Statistiken",
                value=f"✅ Verarbeitet: {processed_users}\n🗑️ Nachrichten gelöscht: {total_deleted}\n❌ Fehler: {failed_users}\n📈 Erfolgsrate: {((processed_users) / max(i+1, 1) * 100):.1f}%",
                inline=False
            )
            progress_embed.add_field(
                name="⏱️ Zeitschätzung",
                value=f"Verbleibende User: {len(dm_users) - i - 1}\nETA: ~{(len(dm_users) - i - 1) * 2} Sekunden",
                inline=False
            )
            progress_embed.set_footer(text="🎮 Deadlock Rank Bot")
            await status_msg.edit(embed=progress_embed)
            
            # DM Channel abrufen (mit Timeout)
            try:
                dm_channel = user.dm_channel
                if not dm_channel:
                    # Versuche DM Channel zu erstellen falls nicht vorhanden
                    dm_channel = await asyncio.wait_for(user.create_dm(), timeout=10.0)
                
                if not dm_channel:
                    failed_users += 1
                    continue
                
                user_deleted = 0
                bot_messages = []
                
                # Alle Bot-Nachrichten sammeln (mit Timeout)
                try:
                    message_count = 0
                    async for message in dm_channel.history(limit=None):
                        if message.author == bot.user:
                            bot_messages.append(message)
                            message_count += 1
                            
                        # Prevent infinite loops
                        if message_count > 200:  # Max 200 messages per user
                            logger.warning(f"User {user.display_name} has >200 messages, limiting...")
                            break
                            
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout loading message history for {user.display_name}")
                    failed_users += 1
                    continue
                except Exception as e:
                    logger.warning(f"Error loading messages for {user.display_name}: {e}")
                    failed_users += 1
                    continue
                
                # Bot-Nachrichten löschen (mit besserem Rate Limiting)
                for j, message in enumerate(bot_messages):
                    try:
                        await asyncio.wait_for(message.delete(), timeout=5.0)
                        user_deleted += 1
                        total_deleted += 1
                        
                        # Aggressiveres Rate limiting
                        if j % 2 == 0:
                            await asyncio.sleep(0.5)
                            
                    except asyncio.TimeoutError:
                        logger.warning(f"Timeout deleting message {message.id}")
                        continue
                    except discord.NotFound:
                        # Message already deleted
                        continue
                    except Exception as e:
                        logger.warning(f"Error deleting message: {e}")
                        continue
                
                processed_users += 1
                logger.info(f"[GLOBAL CLEANUP] Cleaned {user_deleted} messages for {user.display_name}")
                
                # Längere Pause zwischen Usern für Rate Limiting
                await asyncio.sleep(2.0)
                
            except asyncio.TimeoutError:
                logger.error(f"[GLOBAL CLEANUP] Timeout processing user {user.display_name}")
                failed_users += 1
                continue
            except Exception as e:
                logger.error(f"[GLOBAL CLEANUP] Error processing user {user.display_name}: {e}")
                failed_users += 1
                continue
            
        except Exception as e:
            failed_users += 1
            logger.error(f"[GLOBAL CLEANUP] Error cleaning DMs for {user.display_name}: {e}")
    
    # Finale Erfolgs-Nachricht
    embed = discord.Embed(
        title="✅ GLOBALER DM Cleanup abgeschlossen",
        description=f"🎯 **ALLE Bot-DMs wurden erfolgreich gelöscht!**",
        color=0x00ff00
    )
    embed.add_field(
        name="📊 Finale Statistiken",
        value=f"• **{processed_users}** User verarbeitet\n• **{total_deleted}** Nachrichten gelöscht\n• **{failed_users}** Fehler\n• **{len(dm_users)}** User insgesamt gefunden",
        inline=False
    )
    embed.add_field(
        name="🎉 Ergebnis",
        value="Alle User haben jetzt komplett 'saubere' DMs und können wieder frische Benachrichtigungen erhalten!",
        inline=False
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    
    await status_msg.edit(embed=embed)

@bot.command(name='rstop_cleanup')
@commands.has_permissions(administrator=True)
async def emergency_stop_cleanup(ctx):
    """Emergency Stop für hängende Cleanup-Operationen"""
    embed = discord.Embed(
        title="🛑 Emergency Stop",
        description="**WARNUNG**: Dieser Command kann nur den Bot neu starten um hängende Operationen zu stoppen.\n\nDer Cleanup-Fortschritt geht verloren!",
        color=0xff0000
    )
    embed.add_field(
        name="🔧 Lösung",
        value="Verwende `!rreload` über den Main Bot um den Rank Bot neu zu starten:\n\n`!rreload` (im Main Bot Channel)",
        inline=False
    )
    embed.add_field(
        name="ℹ️ Info",
        value="Ein Bot-Neustart stoppt alle laufenden Operationen sofort.",
        inline=False
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)

@bot.command(name='rclean_direct')
@commands.has_permissions(administrator=True)
async def clean_direct_all_dms(ctx):
    """DIREKTE DM-Löschung: Löscht SOFORT alle Bot-DMs ohne Suche oder Bestätigung"""
    embed = discord.Embed(
        title="🗑️ DIREKTE DM-Löschung gestartet",
        description="🚀 **Sofortige Löschung aller Bot-DMs**\n\n⚡ Kein Confirm, keine Suche - direkte Löschung!",
        color=0xff3300
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    status_msg = await ctx.send(embed=embed)
    
    total_deleted = 0
    processed_users = 0
    failed_users = 0
    
    # Gehe durch alle Guilds/Members und versuche direkt DMs zu löschen
    for guild in bot.guilds:
        for member in guild.members:
            if member.bot or member == bot.user:
                continue
            
            # Skip bekannte Problem-User
            if member.display_name.lower() in ["josua / mathegenie", "meisteradrian"]:
                logger.info(f"[DIRECT CLEAN] Skipping problem user: {member.display_name}")
                continue
                
            processed_users += 1
            
            # Progress Update alle 25 User
            if processed_users % 25 == 0:
                try:
                    progress_embed = discord.Embed(
                        title="🗑️ DIREKTE Löschung läuft...",
                        description=f"⚡ **{processed_users}** User verarbeitet\n\n👤 Aktuell: **{member.display_name}**",
                        color=0xff6600
                    )
                    progress_embed.add_field(
                        name="📊 Live-Stats",
                        value=f"🗑️ Gelöscht: {total_deleted}\n❌ Fehler: {failed_users}\n⚡ Ohne Suche - nur direkte Löschung!",
                        inline=False
                    )
                    progress_embed.set_footer(text="🎮 Deadlock Rank Bot")
                    await status_msg.edit(embed=progress_embed)
                except:
                    pass  # Ignore update errors
            
            try:
                # Versuche DM Channel zu bekommen (mit sehr kurzem Timeout)
                try:
                    dm_channel = await asyncio.wait_for(member.create_dm(), timeout=2.0)
                except:
                    continue
                
                if not dm_channel:
                    continue
                
                # Lösche direkt alle Bot-Nachrichten (max 20 pro User für Performance)
                user_deleted = 0
                try:
                    async for message in dm_channel.history(limit=20):
                        if message.author == bot.user:
                            try:
                                await asyncio.wait_for(message.delete(), timeout=3.0)
                                user_deleted += 1
                                total_deleted += 1
                                
                                # Mini-pause zwischen deletes
                                await asyncio.sleep(0.3)
                                
                            except:
                                break  # Bei Fehler -> nächster User
                        
                        # Max 20 messages pro User
                        if user_deleted >= 20:
                            break
                            
                except:
                    failed_users += 1
                
                # Rate limiting zwischen Usern
                await asyncio.sleep(0.5)
                
            except Exception as e:
                failed_users += 1
                logger.debug(f"[DIRECT CLEAN] Error with {member.display_name}: {e}")
    
    # Finale Erfolgsmeldung
    embed = discord.Embed(
        title="✅ DIREKTE DM-Löschung abgeschlossen!",
        description=f"🎯 **Alle verfügbaren Bot-DMs wurden gelöscht!**",
        color=0x00ff00
    )
    embed.add_field(
        name="📊 Endergebnis",
        value=f"• **{processed_users}** User verarbeitet\n• **{total_deleted}** Nachrichten gelöscht\n• **{failed_users}** Fehler\n• ⚡ **DIREKT** ohne Suche!",
        inline=False
    )
    embed.add_field(
        name="🎉 Fertig",
        value="Alle verfügbaren DMs wurden direkt gelöscht!\nKeine hängenden Suchvorgänge mehr!",
        inline=False
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    
    await status_msg.edit(embed=embed)

@bot.command(name='rrestore')
@commands.has_permissions(administrator=True)
async def manual_restore_rank_channel(ctx):
    """Manueller Test der automatischen Rang-Kanal Wiederherstellung"""
    embed = discord.Embed(
        title="🔄 Manuelle Wiederherstellung gestartet",
        description=f"Prüfe Rang-Kanal <#{RANK_SELECTION_CHANNEL_ID}> und stelle Views wieder her...",
        color=0x0099ff
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    status_msg = await ctx.send(embed=embed)
    
    try:
        await auto_restore_rank_channel_view()
        
        success_embed = discord.Embed(
            title="✅ Wiederherstellung abgeschlossen",
            description=f"Rang-Kanal <#{RANK_SELECTION_CHANNEL_ID}> wurde erfolgreich überprüft und Views wiederhergestellt!",
            color=0x00ff00
        )
        success_embed.add_field(
            name="📋 Was passiert ist",
            value="• Kanal nach Bot-Nachrichten durchsucht\n• Rang-Selection View automatisch angehängt\n• Falls nötig: Neue Nachricht erstellt",
            inline=False
        )
        success_embed.set_footer(text="🎮 Deadlock Rank Bot")
        await status_msg.edit(embed=success_embed)
        
    except Exception as e:
        error_embed = discord.Embed(
            title="❌ Fehler bei Wiederherstellung",
            description=f"Fehler beim Wiederherstellen: {str(e)[:200]}",
            color=0xff0000
        )
        error_embed.set_footer(text="🎮 Deadlock Rank Bot")
        await status_msg.edit(embed=error_embed)

@bot.command(name='rcancel')
@commands.has_permissions(administrator=True)
async def cancel_operation(ctx):
    """Versucht laufende Operationen zu canceln"""
    embed = discord.Embed(
        title="🛑 Operation Cancel",
        description="⚠️ **Problem erkannt**: Eine Operation läuft noch im Hintergrund!\n\n**Wahrscheinliche Ursache**: Deep Search oder Cleanup noch nicht abgeschlossen.",
        color=0xff6600
    )
    embed.add_field(
        name="🔧 Sofortige Lösung", 
        value="**Rank Bot neu laden:**\n`!rreload` (im Main Bot Channel)\n\n✅ Stoppt alle laufenden Operationen sofort",
        inline=False
    )
    embed.add_field(
        name="💡 Tipp",
        value="Warte immer bis eine Operation abgeschlossen ist bevor du die nächste startest!",
        inline=False
    )
    embed.set_footer(text="🎮 Deadlock Rank Bot")
    await ctx.send(embed=embed)

# DM Scheduling Tasks
def log_notification(user_id: str, rank: str):
    """Loggt eine gesendete Benachrichtigung"""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        
        # Prüfe ob bereits ein Eintrag für diesen User existiert
        cursor.execute('SELECT count FROM notification_log WHERE user_id = ? ORDER BY notification_time DESC LIMIT 1', (user_id,))
        result = cursor.fetchone()
        count = (result[0] + 1) if result else 1
        
        # Neuen Eintrag hinzufügen
        cursor.execute('''
            INSERT INTO notification_log (user_id, rank, count)
            VALUES (?, ?, ?)
        ''', (user_id, rank, count))
        conn.commit()

def should_notify_user(user_id: int, current_rank: str) -> bool:
    """Prüft ob User benachrichtigt werden soll basierend auf letzter Benachrichtigung"""
    user_id_str = str(user_id)
    
    # Für Test-User: Immer benachrichtigen (ignoriere Intervalle)
    if test_users and any(user.id == user_id for user in test_users):
        return True
    
    # Letzte Benachrichtigung laden
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT notification_time 
            FROM notification_log 
            WHERE user_id = ? 
            ORDER BY notification_time DESC 
            LIMIT 1
        ''', (user_id_str,))
        result = cursor.fetchone()
        
        if not result:
            return True  # Noch nie benachrichtigt
        
        last_date = datetime.fromisoformat(result[0])
        now = datetime.now()
        
        # Prüfe ob User ein custom interval gesetzt hat
        user_data = get_user_data(user_id_str)
        custom_interval = user_data.get('custom_interval')
        interval_days = custom_interval if custom_interval else RANK_INTERVALS.get(current_rank, 45)
        
        return (now - last_date).days >= interval_days

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

@tasks.loop(hours=24)
async def daily_queue_preparation():
    """Täglich um 7:00 Uhr: Queue für den Tag erstellen"""
    now = datetime.now()
    # Für Test-User: Immer ausführen, sonst nur um 7:00 Uhr
    if not test_users and now.hour != 7:
        return
        
    logger.info(f"Creating notification queue for today...")
    
    today = now.strftime('%Y-%m-%d')
    queue_data = []
    
    for guild in bot.guilds:
        logger.debug(f"Checking guild: {guild.name}")
        
        # Sammle alle zu benachrichtigenden Member
        for member in guild.members:
            if member.bot:
                continue
            
            logger.debug(f"Checking member: {member.display_name} ({member.id})")
            
            # Wenn TEST_USERS gesetzt sind, nur diese berücksichtigen (für Tests)
            if test_users and member.id not in [user.id for user in test_users]:
                logger.debug(f"{member.display_name} not in test users (test mode)")
                continue
            
            # User mit speziellen Rollen ignorieren
            special_roles = [role.id for role in member.roles if role.id in [
                ETHERNUS_RANK_ROLE_ID, ENGLISH_ONLY_ROLE_ID, 
                NO_DEADLOCK_ROLE_ID, NO_NOTIFICATION_ROLE_ID
            ]]
            if special_roles:
                logger.debug(f"{member.display_name} has special roles: {special_roles}")
                continue
                
            user_id = str(member.id)
            
            # Prüfen ob User pausiert ist
            user_data = get_user_data(user_id)
            if user_data.get('paused_until'):
                pause_until = datetime.fromisoformat(user_data['paused_until'])
                if now < pause_until:
                    logger.debug(f"{member.display_name} is paused until {pause_until}")
                    continue  # User ist noch pausiert
                else:
                    # Pause ist abgelaufen, entfernen
                    user_data['paused_until'] = None
                    save_user_data(user_id, user_data)
            
            current_rank = get_user_current_rank(member)
            logger.debug(f"{member.display_name} rank: {current_rank}")
            if not current_rank:
                logger.debug(f"{member.display_name} has no rank")
                continue
            
            # Prüfen ob User benachrichtigt werden soll
            should_notify = should_notify_user(member.id, current_rank)
            logger.debug(f"{member.display_name} should be notified: {should_notify}")
            if should_notify:
                queue_data.append({
                    "user_id": str(member.id),
                    "guild_id": str(guild.id),
                    "rank": current_rank
                })
                logger.debug(f"{member.display_name} added to queue")
    
    save_queue_data(queue_data, today)
    logger.info(f"Queue created: {len(queue_data)} users to notify")

@tasks.loop(seconds=30)
async def process_notification_queue():
    """Verarbeitet die Benachrichtigungs-Queue"""
    # Nur zu deutschen Uhrzeiten (8-22 Uhr)
    if not is_notification_time():
        return
    
    # Für Live-Betrieb: Rate limiting - nur alle 3 Minuten
    if not test_users:
        # Einfache Rate-Limiting Logik: Nur ausführen wenn Minute durch 3 teilbar ist
        current_minute = datetime.now().minute
        if current_minute % 3 != 0:
            return
    
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    
    # Queue für heute laden
    queue_data = load_queue_data(today)
    if not queue_data:
        return
    
    # Ersten User aus Queue nehmen
    user_to_notify = queue_data[0]
    
    # User benachrichtigen
    try:
        guild = bot.get_guild(int(user_to_notify["guild_id"]))
        if not guild:
            logger.warning(f"Guild not found: {user_to_notify['guild_id']}")
            mark_queue_item_processed(user_to_notify["user_id"], user_to_notify["guild_id"], today)
            return
            
        member = guild.get_member(int(user_to_notify["user_id"]))
        if not member:
            logger.warning(f"Member not found: {user_to_notify['user_id']}")
            mark_queue_item_processed(user_to_notify["user_id"], user_to_notify["guild_id"], today)
            return
        
        await ask_rank_update(member, user_to_notify["rank"], guild)
        log_notification(user_to_notify["user_id"], user_to_notify["rank"])
        mark_queue_item_processed(user_to_notify["user_id"], user_to_notify["guild_id"], today)
        
        remaining = len(queue_data) - 1
        logger.info(f"Notification sent to {member.display_name} ({member.id}) - Rank: {user_to_notify['rank']} | {remaining} remaining in queue")
        
    except Exception as e:
        logger.error(f"Error sending notification: {e}")
        mark_queue_item_processed(user_to_notify["user_id"], user_to_notify["guild_id"], today)

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
        logger.info(f"[AUTO RESTORE] Latest bot message ID: {latest_message.id} from {latest_message.created_at}")
        
        # Prüfe ob die Nachricht ein Rang-Selection Embed ist
        if latest_message.embeds:
            embed = latest_message.embeds[0]
            if "Rang-Auswahl" in embed.title or "Deadlock-Rang" in str(embed.description):
                logger.info(f"[AUTO RESTORE] Found rank selection message - attaching view")
                
                # Erstelle neuen View und hänge ihn an
                view = ServerRankSelectView(guild)
                
                try:
                    # Versuche die Nachricht zu bearbeiten und View anzuhängen
                    await latest_message.edit(embed=embed, view=view)
                    
                    # Speichere in persistent views
                    save_persistent_view(str(latest_message.id), str(channel.id), str(guild.id), 'server_rank_select')
                    
                    logger.info(f"[AUTO RESTORE] Successfully restored view to message {latest_message.id}")
                    
                except discord.NotFound:
                    logger.warning(f"[AUTO RESTORE] Message {latest_message.id} not found - creating new one")
                    await create_rank_selection_message(channel, guild)
                except Exception as e:
                    logger.error(f"[AUTO RESTORE] Error attaching view to existing message: {e}")
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

@bot.event
async def on_ready():
    print(f'🎮 Deadlock Rank Bot ist online! ({bot.user})')
    print("✅ Standalone Rank Bot bereit für Commands:")
    print("   !rsetup - Rang-Auswahl erstellen")
    print("   !rtest - Test-Nachricht senden")
    print("   !rtest_users @user1 @user2 - Test-User setzen")
    print("   !rstart - System starten")
    print("   !rdb - Datenbank anzeigen")
    print("   !rfind_dms - Alle User mit Bot-DMs finden (Quick Search)")
    print("   !rfind_dms deep - Alle User mit Bot-DMs finden (Deep Search)")
    print("   !rcleanup @user confirm - Alle Bot-DMs eines Users löschen")
    
    # Automatische Wiederherstellung des Rang-Kanals
    await auto_restore_rank_channel_view()
    print("   !rcleanup_all confirm - Alle Bot-DMs aller Test-User löschen")
    print("   !rcleanup_found confirm - ALLE Bot-DMs aller gefundenen User löschen")

# Bot starten
if __name__ == "__main__":
    init_database()
    
    # Bot-Token aus .env laden oder direkt setzen
    TOKEN = "MTMzMTQ1NDk1Njk4ODkyMzk2OA.GxU66N.ufe9iMqU5HHgqk59jwiXd3wsR0FmlsWENX2Ia8"
    
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Bot konnte nicht gestartet werden: {e}")
        input("Drücke Enter zum Beenden...")