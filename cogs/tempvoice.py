"""
TempVoice Cog - Rebuilt Version 3.0
Based on stable backup with working permissions + discussed changes

VERSION 3.0 FEATURES:
HINZUGEFUEGT:
   • Casual Lane Auto-Naming (Casual Lane 1, 2, 3...)
   • EU/DE Region Filter (EU/DE Buttons)
   • Zusaetzliche Buttons: UEBERNEHMEN, UEBERTRAGEN, INFO, RESET
   • Channel Status fuer alle User (set_voice_channel_status=True)
   • Maximum Bitrate bei Channel-Erstellung
   • Stabile Berechtigungslogik aus Backup-Version

ENTFERNT:
   • Warteraum-System komplett entfernt
   • Move Up Button
   • Add/Invite/Remove/Kick Buttons
   • Blockieren/Freigeben Buttons
   • Komplexe Queue-Logik

EINSTELLUNGEN:
   • Casual Create Channel: 8 User Limit
   • Ranked Create Channel: 6 User Limit
   • Default Region Filter: EU (alle koennen joinen)
   • DE Filter: English-Only Role wird blockiert
   • Bitrate: Immer auf Server-Maximum gesetzt

ERSTELLT: 2025-08-14
"""

import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import asyncio
import aiofiles
from datetime import datetime
from typing import Optional, Dict, List, Union
import logging
from collections import defaultdict, deque
import time

logger = logging.getLogger(__name__)

class TempVoiceCog(commands.Cog):
    """Rebuilt TempVoice System without Waiting Room + EU/DE Filter"""
    
    def __init__(self, bot):
        self.bot = bot
        
        # Core data storage
        self.temp_channels = {}  # channel_id -> data
        self.user_settings = {}  # user_id -> settings
        self.blocked_users = defaultdict(set)
        self.trusted_users = defaultdict(set)
        self.create_channels = set()
        
        # Enhanced Channel Management
        self.create_channel_limits = {
            1330278323145801758: 8,  # Casual: max 8 Personen
            1357422958544420944: 6   # Ranked: max 6 Personen
        }
        
        self.create_channel_bitraten = {}
        self.max_bitrate_cache = {}
        
        # Interface persistence
        self.interface_messages = {}
        self.interface_channel_id = 1371927143537315890
        
        # English-Only Role for EU/DE Filter
        self.english_only_role_id = 1309741866098491479
        
        # Special role that needs explicit Voice Channel Status permission
        self.voice_status_role_id = 1304216250649415771
        
        # PERFORMANCE OPTIMIZATIONS - Simplified
        self.voice_event_queue = deque(maxlen=50)
        self.last_event_time = 0
        self.min_event_interval = 0.05  # 50ms between events (faster response)
        self.event_processing_lock = asyncio.Lock()
        
        # Save management
        self.last_save = time.time()
        self.pending_saves = set()
        
        # Default settings (EU as default)
        self.default_user_settings = {
            'channel_name': '{user}\'s Channel',
            'user_limit': 0,
            'bitrate': 64000,
            'privacy_mode': 'public',
            'region_filter': 'EU'  # EU as default
        }
        
        # Pre-compiled patterns for performance
        self.name_patterns = {
            '{user}': lambda u: u.display_name,
            '{username}': lambda u: u.name,
            '{server}': lambda u: u.guild.name,
            '{time}': lambda u: datetime.now().strftime('%H:%M'),
            '{date}': lambda u: datetime.now().strftime('%d.%m')
        }
        
        self.data_file = './voice_data/tempvoice_data.json'
        os.makedirs('data', exist_ok=True)
        
        logger.info("TempVoice initialized (rebuilt version)")
    
    async def cog_load(self):
        """Load cog with staggered startup"""
        logger.info("Loading Rebuilt TempVoice Cog...")
        
        await self.load_data_async()
        self.bot.add_view(VoiceControlView(self))
        
        # Staggered background tasks
        await asyncio.sleep(2)
        self.bot.loop.create_task(self.periodic_save())
        
        await asyncio.sleep(1)
        self.bot.loop.create_task(self.cleanup_task())
        
        await asyncio.sleep(1)
        self.bot.loop.create_task(self.cache_guild_bitraten())
        
        await asyncio.sleep(3)
        self.bot.loop.create_task(self.auto_deploy_interface())
        self.bot.loop.create_task(self.validate_existing_channels())
        
        logger.info("Rebuilt TempVoice Cog loaded successfully")
    
    async def cog_unload(self):
        """Clean unload with data save"""
        if self.pending_saves:
            await self.save_data_async()
        logger.info("TempVoice Cog unloaded")
    
    async def cache_guild_bitraten(self):
        """Cache maximum bitraten for all guilds"""
        await self.bot.wait_until_ready()
        
        for guild in self.bot.guilds:
            max_bitrate = self.get_max_bitrate_for_guild(guild)
            self.max_bitrate_cache[guild.id] = max_bitrate
            logger.info(f"Cached bitrate limit for {guild.name}: {max_bitrate}")
    
    def get_max_bitrate_for_guild(self, guild: discord.Guild) -> int:
        """Get maximum bitrate based on guild's boost level"""
        if guild.premium_tier >= 3:
            return 384000  # Level 3: 384kbps
        elif guild.premium_tier >= 2:
            return 256000  # Level 2: 256kbps  
        elif guild.premium_tier >= 1:
            return 128000  # Level 1: 128kbps
        else:
            return 96000   # No boost: 96kbps
    
    async def periodic_save(self):
        """Simple periodic save without spam"""
        while True:
            try:
                await asyncio.sleep(60)  # Save every minute
                if self.pending_saves and time.time() - self.last_save > 30:
                    await self.save_data_async()
            except Exception as e:
                logger.error(f"Periodic save error: {e}")
                await asyncio.sleep(60)
    
    async def cleanup_task(self):
        """Background cleanup every 2 minutes"""
        while True:
            try:
                await asyncio.sleep(120)  # Every 2 minutes
                await self.cleanup_invalid_channels()
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
                await asyncio.sleep(60)
    
    async def cleanup_invalid_channels(self):
        """Remove invalid channels from cache and delete empty channels"""
        try:
            invalid_channels = []
            empty_channels = []
            
            for channel_id in list(self.temp_channels.keys()):
                channel = self.bot.get_channel(int(channel_id))
                if not channel:
                    # Channel doesn't exist anymore
                    invalid_channels.append(channel_id)
                elif len(channel.members) == 0:
                    # Channel exists but is empty
                    empty_channels.append(channel)
            
            # Remove invalid channels from tracking
            for channel_id in invalid_channels:
                self.temp_channels.pop(channel_id, None)
            
            # Delete empty channels
            for channel in empty_channels:
                try:
                    await self.delete_temp_channel(channel)
                except Exception as e:
                    logger.error(f"Failed to delete empty channel {channel.name}: {e}")
            
            total_cleaned = len(invalid_channels) + len(empty_channels)
            if total_cleaned > 0:
                self.mark_for_save()
                logger.info(f"Cleaned up {len(invalid_channels)} orphaned channels and {len(empty_channels)} empty channels")
                
        except Exception as e:
            logger.error(f"Cleanup invalid channels error: {e}")
    
    def mark_for_save(self):
        """Mark data for saving"""
        self.pending_saves.add('data')
    
    async def get_auto_casual_name(self, category):
        """Generate auto-numbered Casual Lane names"""
        existing_names = set()
        for channel in category.voice_channels:
            if channel.name.startswith("Casual Lane "):
                existing_names.add(channel.name)
        
        # Find next available number
        counter = 1
        while f"Casual Lane {counter}" in existing_names:
            counter += 1
        
        return f"Casual Lane {counter}"
    
    # DATA MANAGEMENT
    async def save_data_async(self):
        """Optimized async data save"""
        try:
            save_data = {
                'temp_channels': {str(k): v for k, v in self.temp_channels.items()},
                'user_settings': {str(k): v for k, v in self.user_settings.items()},
                'blocked_users': {str(k): list(v) for k, v in self.blocked_users.items()},
                'trusted_users': {str(k): list(v) for k, v in self.trusted_users.items()},
                'create_channels': list(self.create_channels),
                'create_channel_limits': {str(k): v for k, v in self.create_channel_limits.items()},
                'create_channel_bitraten': {str(k): v for k, v in self.create_channel_bitraten.items()},
                'interface_messages': {str(k): v for k, v in self.interface_messages.items()},
                'last_updated': datetime.now().isoformat()
            }
            
            async with aiofiles.open(self.data_file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(save_data, indent=2, ensure_ascii=False))
            
            self.last_save = time.time()
            self.pending_saves.clear()
            
        except Exception as e:
            logger.error(f"Save error: {e}")

    async def load_data_async(self):
        """Async load with validation"""
        try:
            if os.path.exists(self.data_file):
                async with aiofiles.open(self.data_file, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    data = json.loads(content)
                
                # Load data with validation
                self.temp_channels = {int(k): v for k, v in data.get('temp_channels', {}).items() if k.isdigit()}
                self.user_settings = {int(k): v for k, v in data.get('user_settings', {}).items() if k.isdigit()}
                
                # Load sets and dictionaries
                for user_id_str, blocked_list in data.get('blocked_users', {}).items():
                    if user_id_str.isdigit():
                        self.blocked_users[user_id_str] = set(blocked_list)
                
                for user_id_str, trusted_list in data.get('trusted_users', {}).items():
                    if user_id_str.isdigit():
                        self.trusted_users[user_id_str] = set(trusted_list)
                
                self.create_channels = set(data.get('create_channels', []))
                
                # Load channel settings
                for channel_id_str, limit in data.get('create_channel_limits', {}).items():
                    if channel_id_str.isdigit():
                        self.create_channel_limits[int(channel_id_str)] = limit
                
                for channel_id_str, bitrate in data.get('create_channel_bitraten', {}).items():
                    if channel_id_str.isdigit():
                        self.create_channel_bitraten[int(channel_id_str)] = bitrate
                
                for channel_id_str, message_id in data.get('interface_messages', {}).items():
                    if channel_id_str.isdigit():
                        self.interface_messages[int(channel_id_str)] = message_id
                
                logger.info(f"Loaded TempVoice data: {len(self.temp_channels)} temp channels")
                
        except Exception as e:
            logger.error(f"Load error: {e}")

    # VOICE EVENT HANDLING - SIMPLIFIED
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Optimized voice state handling for immediate deletion"""
        # Check for immediate channel deletion first (highest priority)
        if (before.channel and 
            before.channel.id in self.temp_channels and 
            len(before.channel.members) == 0):
            # Immediate deletion without rate limiting
            try:
                await self.delete_temp_channel(before.channel)
                logger.info(f"Immediately deleted empty channel: {before.channel.name}")
            except Exception as e:
                logger.error(f"Immediate deletion error: {e}")
        
        # Rate limiting for other events
        current_time = time.time()
        if current_time - self.last_event_time < self.min_event_interval:
            self.voice_event_queue.append((member, before, after))
            return
        
        self.last_event_time = current_time
        
        async with self.event_processing_lock:
            try:
                await self._process_voice_event(member, before, after)
                
                # Process queued events if any
                while self.voice_event_queue:
                    queued_member, queued_before, queued_after = self.voice_event_queue.popleft()
                    await self._process_voice_event(queued_member, queued_before, queued_after)
                    
            except Exception as e:
                logger.error(f"Voice event error: {e}")

    async def _process_voice_event(self, member, before, after):
        """Process individual voice event"""
        # Create temp channel
        if (after.channel and 
            after.channel.id in self.create_channels and 
            before.channel != after.channel):
            await self.create_temp_channel(member, after.channel)
        
        # Note: Channel deletion is now handled immediately in on_voice_state_update
        # to avoid rate limiting delays

    async def create_temp_channel(self, member: discord.Member, create_channel: discord.VoiceChannel):
        """Fast channel creation with proper permissions"""
        try:
            user_settings = self.user_settings.get(member.id, self.default_user_settings.copy())
            
            # Generate channel name with auto-numbering for Casual channels
            if create_channel.id == 1330278323145801758:  # Casual Create Channel
                channel_name = await self.get_auto_casual_name(create_channel.category)
                # Use channel limit for Casual channels
                user_limit = self.create_channel_limits.get(create_channel.id, 8)
            else:
                channel_name = user_settings.get('channel_name', '{user}\'s Channel')
                for pattern, func in self.name_patterns.items():
                    if pattern in channel_name:
                        channel_name = channel_name.replace(pattern, func(member))
                # Use user preference limited by channel max
                user_limit = min(user_settings.get('user_limit', 0), 
                               self.create_channel_limits.get(create_channel.id, 99))
            
            # Set bitrate to maximum available
            bitrate = self.max_bitrate_cache.get(member.guild.id, 96000)
            
            # Get category permissions to inherit properly
            category_overwrites = create_channel.category.overwrites if create_channel.category else {}
            
            # Create channel with inherited category permissions
            new_channel = await create_channel.category.create_voice_channel(
                name=channel_name,
                user_limit=user_limit if user_limit > 0 else None,
                bitrate=bitrate,
                overwrites=category_overwrites,  # Inherit category permissions
                reason=f"Temp channel for {member.display_name}"
            )
            
            # Store data
            self.temp_channels[new_channel.id] = {
                'owner_id': member.id,
                'channel_name': channel_name,
                'created_at': datetime.now().isoformat(),
                'privacy_mode': user_settings.get('privacy_mode', 'public'),
                'original_name': channel_name,
                'region_filter': user_settings.get('region_filter', 'EU'),
                'create_channel_id': create_channel.id,
                'configured_limit': user_limit,
                'configured_bitrate': bitrate
            }
            
            # Category permissions are already inherited, just ensure @everyone has voice channel status
            try:
                # Try to explicitly set voice channel status for @everyone
                await new_channel.set_permissions(
                    member.guild.default_role, 
                    set_voice_channel_status=True,
                    reason="Ensure voice channel status permission"
                )
                logger.info(f"Successfully set voice channel status permission for @everyone in {channel_name}")
            except Exception as e:
                logger.warning(f"Could not set voice channel status permission for @everyone: {e}")
                # This is expected if the permission doesn't exist in this discord.py version
            
            # Set explicit permissions for special voice status role  
            voice_status_role = member.guild.get_role(self.voice_status_role_id)
            if voice_status_role:
                try:
                    await new_channel.set_permissions(
                        voice_status_role,
                        set_voice_channel_status=True,
                        reason="Explicit voice status permission for special role"
                    )
                    logger.info(f"Successfully set voice channel status permission for role {voice_status_role.name}")
                except Exception as e:
                    logger.warning(f"Could not set voice channel status permission for role: {e}")
            
            # Move user FIRST (before setting permissions to avoid conflicts)
            await member.move_to(new_channel, reason="Temp channel created")
            
            # Apply saved region filter setting after move
            region_filter = user_settings.get('region_filter', 'EU')
            if region_filter == 'DE':  # Only block for DE mode
                english_role = member.guild.get_role(self.english_only_role_id)
                if english_role:
                    await new_channel.set_permissions(english_role, view_channel=False, connect=False, 
                                                    reason=f"DE-only mode from saved settings")
            
            self.pending_saves.add('temp_channels')
            logger.info(f"Created temp channel: {channel_name} for {member.display_name}")
            
        except Exception as e:
            logger.error(f"Create channel error: {e}")

    async def delete_temp_channel(self, channel: discord.VoiceChannel):
        """Fast channel deletion"""
        try:
            # Remove from data
            self.temp_channels.pop(channel.id, None)
            self.pending_saves.add('temp_channels')
            
            # Delete channel
            await channel.delete(reason="Empty temp channel")
            logger.info(f"Deleted temp channel: {channel.name}")
            
        except Exception as e:
            logger.error(f"Delete channel error: {e}")

    async def auto_deploy_interface(self):
        """Auto deploy interface in main channel"""
        try:
            await self.bot.wait_until_ready()
            await asyncio.sleep(2)  # Wait for channel cache
            
            main_channel = self.bot.get_channel(self.interface_channel_id)
            if main_channel and self.interface_channel_id not in self.interface_messages:
                # Look for existing interface
                async for message in main_channel.history(limit=50):
                    if (message.author == self.bot.user and 
                        message.embeds and 
                        any("TempVoice" in str(embed.title) or "Voice Channel Control" in str(embed.title) 
                            for embed in message.embeds)):
                        
                        # Update existing interface
                        view = VoiceControlView(self)
                        await message.edit(view=view)
                        self.interface_messages[main_channel.id] = message.id
                        self.pending_saves.add('interface_messages')
                        logger.info("Updated main interface")
                        return
                
                logger.info("No existing interface found in main channel")
                        
        except Exception as e:
            logger.error(f"Auto deploy interface error: {e}")

    async def validate_existing_channels(self):
        """Validate and restore temp channels after restart"""
        try:
            await asyncio.sleep(3)
            
            channels_to_delete = []
            
            for channel_id in list(self.temp_channels.keys()):
                channel = self.bot.get_channel(channel_id)
                if channel and len(channel.members) > 0:
                    # Channel exists and has members - keep it
                    continue
                elif channel and len(channel.members) == 0:
                    # Channel exists but is empty - mark for deletion
                    channels_to_delete.append(channel)
                else:
                    # Channel doesn't exist - remove from tracking
                    self.temp_channels.pop(channel_id, None)
                    self.pending_saves.add('temp_channels')
            
            # Delete empty channels
            for channel in channels_to_delete:
                try:
                    await self.delete_temp_channel(channel)
                    logger.info(f"Deleted empty channel during validation: {channel.name}")
                except Exception as e:
                    logger.error(f"Failed to delete empty channel {channel.name} during validation: {e}")
            
            logger.info(f"Validated temp channels: {len(self.temp_channels)} active, {len(channels_to_delete)} deleted")
            
        except Exception as e:
            logger.error(f"Validation error: {e}")

    # SLASH COMMANDS
    @app_commands.command(name="tempvoice", description="TempVoice management commands")
    @app_commands.describe(action="Action to perform")
    async def tempvoice_cmd(self, interaction: discord.Interaction, action: str):
        """TempVoice management command"""
        if action.lower() == "reload":
            try:
                await interaction.response.send_message("🔄 Reloading TempVoice...", ephemeral=True)
                await self.load_data_async()
                await interaction.edit_original_response(content="✅ TempVoice reloaded successfully!")
            except Exception as e:
                await interaction.edit_original_response(content=f"❌ Reload failed: {e}")
        
        elif action.lower() == "status":
            embed = discord.Embed(title="TempVoice Status", color=0x2b2d31)
            embed.add_field(name="Active Channels", value=len(self.temp_channels), inline=True)
            embed.add_field(name="User Settings", value=len(self.user_settings), inline=True)
            embed.add_field(name="Create Channels", value=len(self.create_channels), inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        
        elif action.lower() == "cleanup":
            try:
                await interaction.response.send_message("🧹 Running cleanup...", ephemeral=True)
                await self.cleanup_invalid_channels()
                await interaction.edit_original_response(content="✅ Cleanup completed successfully!")
            except Exception as e:
                await interaction.edit_original_response(content=f"❌ Cleanup failed: {e}")
        
        else:
            await interaction.response.send_message("❌ Unknown action. Use: reload, status, cleanup", ephemeral=True)


# VOICE CONTROL VIEW WITH ALL DISCUSSED BUTTONS
class VoiceControlView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog
    
    def is_channel_owner_or_trusted(self, user_id: int, channel_id: int) -> bool:
        """Check if user is owner or trusted for channel"""
        channel_data = self.cog.temp_channels.get(channel_id)
        if not channel_data:
            return False
        
        if channel_data['owner_id'] == user_id:
            return True
        
        return user_id in self.cog.trusted_users.get(str(channel_data['owner_id']), set())

    async def _safe_respond(self, interaction: discord.Interaction, content: str, ephemeral: bool = True):
        """Safe response with error handling"""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(content, ephemeral=ephemeral)
        except Exception as e:
            logger.error(f"Response error: {e}")

    @discord.ui.button(label="🏷️ NAME", style=discord.ButtonStyle.secondary, custom_id="tv_rename", row=0)
    async def rename_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.user.voice.channel if interaction.user.voice else None
        
        if not channel or channel.id not in self.cog.temp_channels:
            await self._safe_respond(interaction, "Du bist nicht in einem temporären Channel", True)
            return
        
        if not self.is_channel_owner_or_trusted(interaction.user.id, channel.id):
            await self._safe_respond(interaction, "Nur der Channel-Besitzer kann das", True)
            return

        await interaction.response.send_modal(RenameModal(self.cog, channel))

    @discord.ui.button(label="👥 LIMIT", style=discord.ButtonStyle.secondary, custom_id="tv_limit", row=0)
    async def limit_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.user.voice.channel if interaction.user.voice else None
        
        if not channel or channel.id not in self.cog.temp_channels:
            await self._safe_respond(interaction, "Du bist nicht in einem temporären Channel", True)
            return
        
        if not self.is_channel_owner_or_trusted(interaction.user.id, channel.id):
            await self._safe_respond(interaction, "Nur der Channel-Besitzer kann das", True)
            return

        await interaction.response.send_modal(LimitModal(self.cog, channel))

    @discord.ui.button(label="🔒 PRIVAT", style=discord.ButtonStyle.secondary, custom_id="tv_lock", row=0)
    async def lock_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.user.voice.channel if interaction.user.voice else None
        
        if not channel or channel.id not in self.cog.temp_channels:
            await self._safe_respond(interaction, "Du bist nicht in einem temporären Channel", True)
            return
        
        if not self.is_channel_owner_or_trusted(interaction.user.id, channel.id):
            await self._safe_respond(interaction, "Nur der Channel-Besitzer kann das", True)
            return

        try:
            # Toggle privacy mode
            current_mode = self.cog.temp_channels[channel.id].get('privacy_mode', 'public')
            new_mode = 'private' if current_mode == 'public' else 'public'
            
            if new_mode == 'private':
                # Lock channel - only current members can join
                await channel.set_permissions(channel.guild.default_role, connect=False)
                response = "Channel ist jetzt privat (nur aktuelle Mitglieder können beitreten)"
            else:
                # Unlock channel - everyone can join again
                await channel.set_permissions(channel.guild.default_role, connect=True)
                response = "Channel ist jetzt öffentlich"
            
            # Update data
            self.cog.temp_channels[channel.id]['privacy_mode'] = new_mode
            self.cog.pending_saves.add('temp_channels')
            
            await self._safe_respond(interaction, response, True)
            
        except Exception as e:
            logger.error(f"Lock button error: {e}")
            await self._safe_respond(interaction, "Fehler beim Sperren", True)

    @discord.ui.button(label="🇪🇺 EU", style=discord.ButtonStyle.primary, custom_id="tv_region_eu", row=1)
    async def region_filter_eu(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Set region filter to EU (allow all)"""
        channel = interaction.user.voice.channel if interaction.user.voice else None
        
        if not channel or channel.id not in self.cog.temp_channels:
            await self._safe_respond(interaction, "Du bist nicht in einem temporären Channel", True)
            return
        
        if not self.is_channel_owner_or_trusted(interaction.user.id, channel.id):
            await self._safe_respond(interaction, "Nur der Channel-Besitzer kann das", True)
            return

        try:
            english_role = interaction.guild.get_role(self.cog.english_only_role_id)
            if not english_role:
                await self._safe_respond(interaction, "Rolle nicht gefunden", True)
                return
            
            # Set to EU mode - remove English-Only restrictions
            await channel.set_permissions(english_role, view_channel=None, connect=None, 
                                        reason=f"EU mode enabled by {interaction.user}")
            
            # Update channel data
            self.cog.temp_channels[channel.id]['region_filter'] = 'EU'
            self.cog.pending_saves.add('temp_channels')
            
            # Update user settings
            if interaction.user.id not in self.cog.user_settings:
                self.cog.user_settings[interaction.user.id] = {}
            self.cog.user_settings[interaction.user.id]['region_filter'] = 'EU'
            self.cog.pending_saves.add('user_settings')
            
            await self._safe_respond(interaction, "EU Modus aktiviert (Deutsche und Englische können beitreten)", True)
            
        except Exception as e:
            logger.error(f"EU region filter error: {e}")
            await self._safe_respond(interaction, "Fehler beim Umschalten", True)

    @discord.ui.button(label="🇩🇪 DE", style=discord.ButtonStyle.success, custom_id="tv_region_de", row=1)
    async def region_filter_de(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Set region filter to DE only"""
        channel = interaction.user.voice.channel if interaction.user.voice else None
        
        if not channel or channel.id not in self.cog.temp_channels:
            await self._safe_respond(interaction, "Du bist nicht in einem temporären Channel", True)
            return
        
        if not self.is_channel_owner_or_trusted(interaction.user.id, channel.id):
            await self._safe_respond(interaction, "Nur der Channel-Besitzer kann das", True)
            return

        try:
            english_role = interaction.guild.get_role(self.cog.english_only_role_id)
            if not english_role:
                await self._safe_respond(interaction, "Rolle nicht gefunden", True)
                return
            
            # Set to DE mode - block English-Only role
            await channel.set_permissions(english_role, view_channel=False, connect=False, 
                                        reason=f"DE-only mode enabled by {interaction.user}")
            
            # Update channel data
            self.cog.temp_channels[channel.id]['region_filter'] = 'DE'
            self.cog.pending_saves.add('temp_channels')
            
            # Update user settings
            if interaction.user.id not in self.cog.user_settings:
                self.cog.user_settings[interaction.user.id] = {}
            self.cog.user_settings[interaction.user.id]['region_filter'] = 'DE'
            self.cog.pending_saves.add('user_settings')
            
            await self._safe_respond(interaction, "Deutschland-Only Modus aktiviert (Nur Deutsche können beitreten)", True)
            
        except Exception as e:
            logger.error(f"DE region filter error: {e}")
            await self._safe_respond(interaction, "Fehler beim Umschalten", True)

    @discord.ui.button(label="👑 ÜBERNEHMEN", style=discord.ButtonStyle.secondary, custom_id="tv_claim", row=2)
    async def claim_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.user.voice.channel if interaction.user.voice else None
        
        if not channel or channel.id not in self.cog.temp_channels:
            await self._safe_respond(interaction, "Du bist nicht in einem temporären Channel", True)
            return
        
        channel_data = self.cog.temp_channels.get(channel.id, {})
        current_owner = self.cog.bot.get_user(channel_data.get('owner_id'))
        
        # Check if current owner is still in the channel
        if current_owner and current_owner in channel.members:
            await self._safe_respond(interaction, "Der Besitzer ist noch im Channel", True)
            return
        
        try:
            # Transfer ownership
            self.cog.temp_channels[channel.id]['owner_id'] = interaction.user.id
            self.cog.pending_saves.add('temp_channels')
            
            await self._safe_respond(interaction, f"Du bist jetzt Besitzer von **{channel.name}**", True)
            logger.info(f"Channel claimed by {interaction.user.display_name}: {channel.name}")
            
        except Exception as e:
            logger.error(f"Claim error: {e}")
            await self._safe_respond(interaction, "Fehler beim Übernehmen", True)

    @discord.ui.button(label="🔄 ÜBERTRAGEN", style=discord.ButtonStyle.secondary, custom_id="tv_transfer", row=2)
    async def transfer_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.user.voice.channel if interaction.user.voice else None
        
        if not channel or channel.id not in self.cog.temp_channels:
            await self._safe_respond(interaction, "Du bist nicht in einem temporären Channel", True)
            return
        
        if not self.is_channel_owner_or_trusted(interaction.user.id, channel.id):
            await self._safe_respond(interaction, "Nur der Channel-Besitzer kann das", True)
            return

        await interaction.response.send_modal(TransferModal(self.cog, channel))

    @discord.ui.button(label="📊 INFO", style=discord.ButtonStyle.secondary, custom_id="tv_info", row=2)
    async def show_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.user.voice.channel if interaction.user.voice else None
        
        if not channel or channel.id not in self.cog.temp_channels:
            await self._safe_respond(interaction, "Du bist nicht in einem temporären Channel", True)
            return

        try:
            embed = await self.create_info_embed(channel)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Info error: {e}")
            await self._safe_respond(interaction, "Fehler beim Laden der Infos", True)

    @discord.ui.button(label="🔧 RESET", style=discord.ButtonStyle.secondary, custom_id="tv_reset", row=2)
    async def reset_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.user.voice.channel if interaction.user.voice else None
        
        if not channel or channel.id not in self.cog.temp_channels:
            await self._safe_respond(interaction, "Du bist nicht in einem temporären Channel", True)
            return
        
        if not self.is_channel_owner_or_trusted(interaction.user.id, channel.id):
            await self._safe_respond(interaction, "Nur der Channel-Besitzer kann das", True)
            return

        try:
            # Reset to defaults
            user_settings = self.cog.user_settings.get(interaction.user.id, {})
            default_name = user_settings.get('channel_name', f"{interaction.user.display_name}'s Channel")
            
            # Apply name patterns
            for pattern, func in self.cog.name_patterns.items():
                if pattern in default_name:
                    default_name = default_name.replace(pattern, func(interaction.user))
            
            await channel.edit(
                name=default_name,
                user_limit=user_settings.get('user_limit', 0) or None,
                bitrate=user_settings.get('bitrate', 64000)
            )
            
            # Reset channel data
            self.cog.temp_channels[channel.id].update({
                'privacy_mode': 'public',
                'region_filter': 'EU'
            })
            
            # Remove region restrictions
            english_role = channel.guild.get_role(self.cog.english_only_role_id)
            if english_role:
                await channel.set_permissions(english_role, overwrite=None)
            
            # Reset to category permissions by inheriting them
            if channel.category:
                category_overwrites = channel.category.overwrites
                await channel.edit(overwrites=category_overwrites, reason="Reset to category permissions")
            
            # Ensure voice channel status for @everyone
            try:
                await channel.set_permissions(
                    channel.guild.default_role,
                    set_voice_channel_status=True,
                    reason="Restore voice channel status permission"
                )
            except Exception as e:
                logger.warning(f"Could not restore voice channel status permission for @everyone: {e}")
            
            # Restore explicit permissions for special voice status role
            voice_status_role = channel.guild.get_role(self.cog.voice_status_role_id)
            if voice_status_role:
                try:
                    await channel.set_permissions(
                        voice_status_role,
                        set_voice_channel_status=True,
                        reason="Restore explicit voice status permission for special role"
                    )
                except Exception as e:
                    logger.warning(f"Could not restore voice channel status permission for role: {e}")
            
            self.cog.pending_saves.add('temp_channels')
            await self._safe_respond(interaction, "Channel auf Standard zurückgesetzt", True)
            
        except Exception as e:
            logger.error(f"Reset error: {e}")
            await self._safe_respond(interaction, "Fehler beim Zurücksetzen", True)

    @discord.ui.button(label="❌ DELETE", style=discord.ButtonStyle.danger, custom_id="tv_delete", row=2)
    async def delete_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.user.voice.channel if interaction.user.voice else None
        
        if not channel or channel.id not in self.cog.temp_channels:
            await self._safe_respond(interaction, "Du bist nicht in einem temporären Channel", True)
            return
        
        if not self.is_channel_owner_or_trusted(interaction.user.id, channel.id):
            await self._safe_respond(interaction, "Nur der Channel-Besitzer kann das", True)
            return

        try:
            await self._safe_respond(interaction, "Channel wird gelöscht...", True)
            await self.cog.delete_temp_channel(channel)
        except Exception as e:
            logger.error(f"Delete button error: {e}")
            await self._safe_respond(interaction, "Fehler beim Löschen", True)

    async def create_info_embed(self, channel: discord.VoiceChannel) -> discord.Embed:
        """Create info embed for channel"""
        try:
            embed = discord.Embed(
                title=f"🎙️ {channel.name}",
                color=0x2b2d31
            )
            
            channel_data = self.cog.temp_channels.get(channel.id, {})
            owner_id = channel_data.get('owner_id')
            owner = self.cog.bot.get_user(owner_id) if owner_id else None
            
            embed.add_field(name="👑 Besitzer", value=owner.mention if owner else "Unbekannt", inline=True)
            embed.add_field(name="👥 Mitglieder", value=f"{len(channel.members)}/{channel.user_limit or '∞'}", inline=True)
            embed.add_field(name="🎵 Bitrate", value=f"{channel.bitrate//1000} kbps", inline=True)
            
            privacy = channel_data.get('privacy_mode', 'public')
            region = channel_data.get('region_filter', 'EU')
            embed.add_field(name="🔒 Privat", value="Ja" if privacy == 'private' else "Nein", inline=True)
            embed.add_field(name="🌍 Region", value=region, inline=True)
            
            created_at = channel_data.get('created_at')
            if created_at:
                embed.add_field(name="⏰ Erstellt", value=f"<t:{int(datetime.fromisoformat(created_at).timestamp())}:R>", inline=True)
            
            return embed
            
        except Exception as e:
            logger.error(f"Info embed error: {e}")
            return discord.Embed(title="❌ Fehler", description="Konnte Channel-Infos nicht laden", color=0xff0000)


# MODALS
class RenameModal(discord.ui.Modal, title="Channel umbenennen"):
    def __init__(self, cog, channel):
        super().__init__()
        self.cog = cog
        self.channel = channel

    name_input = discord.ui.TextInput(
        label="Neuer Channel Name",
        placeholder="Gib den neuen Namen ein...",
        style=discord.TextStyle.short,
        max_length=100,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            new_name = self.name_input.value.strip()
            await self.channel.edit(name=new_name)
            
            # Update data
            self.cog.temp_channels[self.channel.id]['channel_name'] = new_name
            self.cog.pending_saves.add('temp_channels')
            
            await interaction.response.send_message(f"Channel umbenannt zu: **{new_name}**", ephemeral=True)
            
        except Exception as e:
            logger.error(f"Rename modal error: {e}")
            await interaction.response.send_message("Fehler beim Umbenennen", ephemeral=True)


class LimitModal(discord.ui.Modal, title="Benutzerlimit setzen"):
    def __init__(self, cog, channel):
        super().__init__()
        self.cog = cog
        self.channel = channel

    limit_input = discord.ui.TextInput(
        label="Benutzerlimit (0 = unbegrenzt)",
        placeholder="0-99",
        style=discord.TextStyle.short,
        max_length=2,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            limit = int(self.limit_input.value)
            if limit < 0 or limit > 99:
                await interaction.response.send_message("Limit muss zwischen 0 und 99 liegen", ephemeral=True)
                return
            
            await self.channel.edit(user_limit=limit if limit > 0 else None)
            
            # Update data
            self.cog.temp_channels[self.channel.id]['configured_limit'] = limit
            self.cog.pending_saves.add('temp_channels')
            
            limit_text = str(limit) if limit > 0 else "unbegrenzt"
            await interaction.response.send_message(f"Limit gesetzt auf: **{limit_text}**", ephemeral=True)
            
        except ValueError:
            await interaction.response.send_message("Bitte gib eine gültige Zahl ein", ephemeral=True)
        except Exception as e:
            logger.error(f"Limit modal error: {e}")
            await interaction.response.send_message("Fehler beim Setzen des Limits", ephemeral=True)


class TransferModal(discord.ui.Modal, title="Channel übertragen"):
    def __init__(self, cog, channel):
        super().__init__()
        self.cog = cog
        self.channel = channel

    user_input = discord.ui.TextInput(
        label="Neuer Besitzer (Benutzername oder ID)",
        placeholder="Benutzername#0000 oder 123456789",
        style=discord.TextStyle.short,
        max_length=50,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target_user = None
            user_input = self.user_input.value.strip()
            
            # Try to find user by ID first
            if user_input.isdigit():
                target_user = self.channel.guild.get_member(int(user_input))
            
            # Try to find by username
            if not target_user:
                for member in self.channel.guild.members:
                    if (member.name.lower() == user_input.lower() or 
                        member.display_name.lower() == user_input.lower() or
                        str(member).lower() == user_input.lower()):
                        target_user = member
                        break
            
            if not target_user:
                await interaction.response.send_message("Benutzer nicht gefunden", ephemeral=True)
                return
            
            if target_user.id == interaction.user.id:
                await interaction.response.send_message("Du kannst den Channel nicht an dich selbst übertragen", ephemeral=True)
                return
            
            # Transfer ownership
            self.cog.temp_channels[self.channel.id]['owner_id'] = target_user.id
            self.cog.pending_saves.add('temp_channels')
            
            await interaction.response.send_message(
                f"Channel **{self.channel.name}** wurde an **{target_user.display_name}** übertragen", 
                ephemeral=True
            )
            
            logger.info(f"Channel transferred from {interaction.user.display_name} to {target_user.display_name}: {self.channel.name}")
            
        except Exception as e:
            logger.error(f"Transfer modal error: {e}")
            await interaction.response.send_message("Fehler beim Übertragen", ephemeral=True)


def get_channel_from_user(member: discord.Member, cog):
    """Get temp channel from user"""
    if not member.voice or not member.voice.channel:
        return None
    
    channel = member.voice.channel
    if channel.id not in cog.temp_channels:
        return None
    
    return channel

async def setup(bot):
    """Setup function"""
    try:
        logger.info("Setting up Rebuilt TempVoice Cog...")
        await bot.add_cog(TempVoiceCog(bot))
        logger.info("✅ Rebuilt TempVoice Cog loaded!")
        logger.info("🚀 Features:")
        logger.info("   • No Waiting Room System")  
        logger.info("   • Casual Lane Auto-Naming")
        logger.info("   • EU/DE Region Filter")
        logger.info("   • Additional Control Buttons")
        logger.info("   • Channel Status for All Users")
        logger.info("   • Maximum Bitrate Creation")
        logger.info("   • Proper Permission Handling")
        
    except Exception as e:
        logger.error(f"Failed to load Rebuilt TempVoice Cog: {e}")
        raise