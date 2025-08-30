"""
Discord Master Bot System V2 - CLEAN VERSION
Manages multiple cogs with hot-reloading, status monitoring, and German server compatibility.
Includes: dl_coaching, claim_system, changelog_discord_bot, forum_ki_bot, voice_activity_tracker, rank_voice_manager, tempvoice
Rank Bot functionality removed - now standalone only
"""

import discord
from discord.ext import commands
import asyncio
import logging
import logging.handlers
import os
import sys
import datetime
import pytz
import signal
from pathlib import Path
from typing import Dict, List, Tuple
import traceback

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

class MasterBot(commands.Bot):
    """
    Master Discord Bot for managing multiple cogs with hot-reloading capabilities.
    Supports German Discord server environment with comprehensive error handling.
    """
    
    def __init__(self):
        # Bot configuration
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        intents.guilds = True
        
        super().__init__(
            command_prefix='!',
            intents=intents,
            description='Master Bot System - Verwaltet alle Bot-Funktionen',
            owner_id=int(os.getenv('OWNER_ID', 0)),
            case_insensitive=True,
            chunk_guilds_at_startup=False,
            max_messages=1000,
            member_cache_flags=discord.MemberCacheFlags.from_intents(intents)
        )
        
        # Setup logging early so discovery logs are captured
        self.setup_logging()
        
        # Automatisches Cog-Loading System
        self.cogs_dir = Path(__file__).parent / 'cogs'
        self.cogs_list = []  # Wird automatisch gefüllt
        self.auto_discover_cogs()
        
        # Unified Patch Bot Management
        self.unified_patchbot_process = None
        self.unified_patchbot_script = Path(__file__).parent / "unified_patchnotes_bot.py"
        
        # Status tracking
        self.cog_status: Dict[str, str] = {}
        self.startup_time = datetime.datetime.now(pytz.timezone('Europe/Berlin'))
        
        # (logging already initialized above)
    
    def auto_discover_cogs(self):
        """Automatisches Entdecken aller Cogs im cogs/ Verzeichnis"""
        try:
            if not self.cogs_dir.exists():
                logging.warning(f"Cogs directory not found: {self.cogs_dir}")
                return
            
            discovered_cogs = []
            
            # Suche alle .py Dateien im cogs Verzeichnis
            for cog_file in self.cogs_dir.glob('*.py'):
                if cog_file.name.startswith('_'):  # Ignoriere __init__.py und _private.py
                    continue
                    
                cog_name = f"cogs.{cog_file.stem}"
                
                # Prüfe ob die Datei ein gültiges Cog ist (enthält setup Funktion oder Cog Klasse)
                try:
                    with open(cog_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if ('async def setup(' in content or 'def setup(' in content or 
                            'class ' in content and 'Cog' in content):
                            discovered_cogs.append(cog_name)
                            logging.info(f"🔍 Auto-discovered cog: {cog_name}")
                        else:
                            logging.info(f"⏭️ Skipped {cog_file.name} (no setup function or Cog class)")
                except Exception as e:
                    logging.warning(f"⚠️ Error checking {cog_file.name}: {e}")
            
            self.cogs_list = discovered_cogs
            logging.info(f"✅ Auto-discovery complete: {len(discovered_cogs)} cogs found")
            
        except Exception as e:
            logging.error(f"❌ Error during cog auto-discovery: {e}")
            logging.error(f"❌ CRITICAL: No cogs will be loaded! Check cogs/ directory")
            # Keine Fallback-Liste - wenn Discovery fehlschlägt, ist das ein kritischer Fehler
            self.cogs_list = []
        
    def setup_logging(self):
        """Setup comprehensive logging with rotation"""
        log_dir = Path(__file__).parent / 'logs'
        log_dir.mkdir(exist_ok=True)
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.handlers.RotatingFileHandler(
                    log_dir / 'master_bot.log',
                    maxBytes=5*1024*1024,
                    backupCount=5,
                    encoding='utf-8'
                ),
                logging.StreamHandler(sys.stdout)
            ]
        )
        
        logging.getLogger('discord').setLevel(logging.WARNING)
        logging.getLogger('discord.http').setLevel(logging.WARNING)
        
        logging.info("Master Bot logging initialized")
    
    async def setup_hook(self):
        """Setup hook called when bot is starting"""
        logging.info("Master Bot setup starting...")
        
        # Load all cogs
        await self.load_all_cogs()
        
        # Sync slash commands
        try:
            synced = await self.tree.sync()
            logging.info(f"Synced {len(synced)} slash commands")
        except Exception as e:
            logging.error(f"Failed to sync slash commands: {e}")
        
        logging.info("Master Bot setup completed")
    
    async def on_ready(self):
        """Event triggered when bot is ready"""
        logging.info(f"Bot logged in as {self.user} (ID: {self.user.id})")
        logging.info(f"Connected to {len(self.guilds)} guilds")
        
        # Set bot status
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{len(self.cogs_list)} Cogs | !help"
        )
        await self.change_presence(activity=activity)
        
        # Log cog status
        loaded_cogs = [name for name, status in self.cog_status.items() if status == 'loaded']
        logging.info(f"Loaded cogs: {len(loaded_cogs)}/{len(self.cogs_list)}")
        
        # Special logging for TempVoice
        tempvoice_cog = self.get_cog('TempVoiceCog')
        if tempvoice_cog:
            logging.info(f"TempVoice ready with {len(tempvoice_cog.create_channels)} create channels")
            
        # Start hourly health check
        self.loop.create_task(self.hourly_health_check())
    
    async def load_all_cogs(self):
        """Load all cogs with parallel loading for better performance"""
        logging.info("Loading all cogs in parallel...")
        
        async def load_single_cog(cog_name):
            """Load a single cog and return result"""
            try:
                await self.load_extension(cog_name)
                self.cog_status[cog_name] = 'loaded'
                logging.info(f"✅ Loaded cog: {cog_name}")
                return True, cog_name, None
            except Exception as e:
                self.cog_status[cog_name] = f'error: {str(e)[:100]}'
                logging.error(f"❌ Failed to load cog {cog_name}: {e}")
                return False, cog_name, e
        
        # Load all cogs in parallel
        tasks = [load_single_cog(cog_name) for cog_name in self.cogs_list]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        successful_cogs = 0
        for result in results:
            if isinstance(result, tuple) and result[0]:
                successful_cogs += 1
            elif isinstance(result, Exception):
                logging.error(f"❌ Unexpected error during cog loading: {result}")
        
        logging.info(f"Parallel cog loading completed: {successful_cogs}/{len(self.cogs_list)} successful")
    
    async def reload_all_cogs_with_discovery(self):
        """Reload all cogs AND re-discover new ones"""
        try:
            # 1. Entlade alle aktuell geladenen Cog-Extensions per Modulpfad (nicht Cog-Klassenname)
            unload_results = []
            loaded_extensions = [ext for ext in list(self.extensions.keys()) if ext.startswith('cogs.')]

            for ext_name in loaded_extensions:
                try:
                    await self.unload_extension(ext_name)
                    unload_results.append(f"✅ Unloaded: {ext_name}")
                    logging.info(f"Unloaded extension: {ext_name}")
                except Exception as e:
                    unload_results.append(f"❌ Error unloading {ext_name}: {str(e)[:50]}")
                    logging.error(f"Error unloading {ext_name}: {e}")
            
            # 2. Re-discover alle Cogs (findet auch neue)
            old_count = len(self.cogs_list)
            self.auto_discover_cogs()
            new_count = len(self.cogs_list)
            
            discovery_msg = f"🔍 Re-discovery: {old_count} -> {new_count} cogs"
            if new_count > old_count:
                discovery_msg += f" (🆕 {new_count - old_count} neue gefunden!)"
            logging.info(discovery_msg)
            
            # 3. Lade alle Cogs (alte + neue)
            #    Zurücksetzen des Status, damit keine veralteten Einträge gezählt werden
            self.cog_status = {}
            await self.load_all_cogs()
            
            # 4. Erstelle Zusammenfassung
            loaded_count = len([status for status in self.cog_status.values() if status == 'loaded'])
            
            summary = {
                'unloaded': len(unload_results),
                'discovered': new_count,
                'loaded': loaded_count,
                'new_cogs': new_count - old_count,
                'unload_details': unload_results
            }
            
            return True, summary
            
        except Exception as e:
            logging.error(f"Error during full cog reload: {e}")
            return False, f"Error: {str(e)}"
    
    async def reload_cog(self, cog_name: str) -> Tuple[bool, str]:
        """Reload a specific cog"""
        try:
            await self.reload_extension(cog_name)
            self.cog_status[cog_name] = 'loaded'
            message = f"✅ Successfully reloaded {cog_name}"
            logging.info(message)
            return True, message
        except commands.ExtensionNotLoaded:
            try:
                await self.load_extension(cog_name)
                self.cog_status[cog_name] = 'loaded'
                message = f"✅ Loaded {cog_name} (was not loaded before)"
                logging.info(message)
                return True, message
            except Exception as e:
                error_msg = f"❌ Failed to load {cog_name}: {str(e)[:200]}"
                self.cog_status[cog_name] = f'error: {str(e)[:100]}'
                logging.error(error_msg)
                return False, error_msg
        except Exception as e:
            error_msg = f"❌ Failed to reload {cog_name}: {str(e)[:200]}"
            self.cog_status[cog_name] = f'error: {str(e)[:100]}'
            logging.error(error_msg)
            return False, error_msg
    
    async def hourly_health_check(self):
        """Prioritized health check - critical processes checked more frequently"""
        critical_check_interval = 3600  # 1 hour for critical processes
        general_check_interval = 7200   # 2 hours for general health
        
        last_critical_check = 0
        last_general_check = 0
        
        while not self.is_closed():
            try:
                await asyncio.sleep(300)  # Check every 5 minutes
                current_time = asyncio.get_event_loop().time()
                
                # Critical cog health check every hour (TempVoice, Rank Bot, Voice Tracker)
                if current_time - last_critical_check >= critical_check_interval:
                    critical_issues = []
                    
                    # Check TempVoice
                    tempvoice_cog = self.get_cog('TempVoiceCog')
                    if not tempvoice_cog:
                        critical_issues.append("TempVoice not loaded")
                    elif not hasattr(tempvoice_cog, 'create_channels') or not tempvoice_cog.create_channels:
                        critical_issues.append("TempVoice create_channels empty")
                    
                    # Check Rank Voice Manager
                    rank_cog = self.get_cog('RolePermissionVoiceManager')
                    if not rank_cog:
                        critical_issues.append("RankVoiceManager not loaded")
                    
                    # Check Voice Activity Tracker
                    voice_tracker = self.get_cog('VoiceActivityTrackerCog')
                    if not voice_tracker:
                        critical_issues.append("VoiceActivityTracker not loaded")
                    elif hasattr(voice_tracker, 'db_manager') and not voice_tracker.db_manager.db:
                        critical_issues.append("VoiceTracker database disconnected")
                    
                    if critical_issues:
                        logging.warning(f"Critical Health Check: Issues found: {critical_issues}")
                    else:
                        logging.info("Critical Health Check: Core cogs operational")
                    
                    last_critical_check = current_time
                
                # General health check every 2 hours (includes unified patch bot monitoring)
                if current_time - last_general_check >= general_check_interval:
                    # Check Unified Patch Bot status
                    is_running, status_msg = await self.get_unified_patchbot_status()
                    if not is_running:
                        logging.warning(f"General Health Check: Unified Patch Bot not running - {status_msg}")
                    else:
                        logging.info(f"General Health Check: Unified Patch Bot operational - {status_msg}")
                    
                    last_general_check = current_time
                    
            except Exception as e:
                logging.error(f"Health check error: {e}")
    
    async def close(self):
        """Cleanup when bot shuts down"""
        logging.info("Master Bot shutting down...")
        
        # Close all cogs (über ihre Extension-Namen)
        for ext_name in [ext for ext in list(self.extensions.keys()) if ext.startswith('cogs.')]:
            try:
                await self.unload_extension(ext_name)
                logging.info(f"Unloaded extension: {ext_name}")
            except Exception as e:
                logging.error(f"Error unloading extension {ext_name}: {e}")
        
        await super().close()
        logging.info("Master Bot shutdown complete")

def is_bot_owner():
    """Decorator to check if user is bot owner"""
    async def predicate(ctx):
        return ctx.author.id == ctx.bot.owner_id
    return commands.check(predicate)

class MasterControlCog(commands.Cog):
    """Master control commands for bot management"""
    
    def __init__(self, bot: MasterBot):
        self.bot = bot
    
    @commands.group(name='master', invoke_without_command=True, aliases=['m'])
    @is_bot_owner()
    async def master_control(self, ctx):
        """Master bot control panel"""
        embed = discord.Embed(
            title="🤖 Master Bot Kontrolle",
            description="Verwalte alle Bot-Cogs und Systeme",
            color=0x0099ff
        )
        
        embed.add_field(
            name="📋 Master Commands",
            value="`!master status` - Bot Status\n"
                  "`!master reload [cog]` - Cog neu laden\n"
                  "`!master reloadall` - Alle Cogs neu laden + Auto-Discovery\n"
                  "`!master discover` - Neue Cogs entdecken (ohne laden)\n"
                  "`!master shutdown` - Bot beenden",
            inline=False
        )
        
        embed.add_field(
            name="🎮 Patch Bot Commands",
            value="`!patchbot start` - Unified Patch Bot starten\n"
                  "`!patchbot stop` - Unified Patch Bot stoppen\n"
                  "`!patchbot restart` - Unified Patch Bot neustarten\n"
                  "`!patchbot status` - Unified Patch Bot Status",
            inline=False
        )
        
        await ctx.send(embed=embed)
    
    @master_control.command(name='status', aliases=['s'])
    async def master_status(self, ctx):
        """Bot status and health check"""
        embed = discord.Embed(
            title="📊 Master Bot Status",
            description=f"Bot läuft seit: {self.bot.startup_time.strftime('%d.%m.%Y %H:%M:%S')}",
            color=0x00ff00
        )
        
        embed.add_field(
            name="🔧 System",
            value=f"Guilds: {len(self.bot.guilds)}\n"
                  f"Users: {len(set(self.bot.get_all_members()))}\n"
                  f"Commands: {len(self.bot.commands)}",
            inline=True
        )
        
        # Add cog status
        loaded_cogs = []
        error_cogs = []
        
        for cog_name, status in self.bot.cog_status.items():
            short_name = cog_name.split('.')[-1]
            if status == 'loaded':
                loaded_cogs.append(f"✅ {short_name}")
            else:
                error_cogs.append(f"❌ {short_name}")
        
        if loaded_cogs:
            embed.add_field(
                name=f"📦 Loaded Cogs ({len(loaded_cogs)})",
                value="\n".join(loaded_cogs),
                inline=True
            )
        
        if error_cogs:
            embed.add_field(
                name=f"⚠️ Error Cogs ({len(error_cogs)})",
                value="\n".join(error_cogs),
                inline=True
            )
        
        await ctx.send(embed=embed)
    
    @master_control.command(name='reload', aliases=['rl'])
    async def master_reload(self, ctx, cog_name: str = None):
        """Reload specific cog or all cogs"""
        if cog_name:
            # Find matching cog
            matching_cogs = [c for c in self.bot.cogs_list if cog_name.lower() in c.lower()]
            
            if not matching_cogs:
                await ctx.send(f"❌ Cog '{cog_name}' nicht gefunden!")
                return
            
            target_cog = matching_cogs[0]
            success, message = await self.bot.reload_cog(target_cog)
            
            embed = discord.Embed(
                title="🔄 Cog Reload",
                description=message,
                color=0x00ff00 if success else 0xff0000
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send("❌ Bitte Cog-Namen angeben! Verfügbar:\n" + "\n".join([c.split('.')[-1] for c in self.bot.cogs_list]))
    
    @master_control.command(name='reloadall', aliases=['rla'])
    async def master_reload_all(self, ctx):
        """Reload all cogs with automatic discovery"""
        embed = discord.Embed(
            title="🔄 Alle Cogs neu laden (Auto-Discovery)",
            description="Entdecke neue Cogs und lade alle neu...",
            color=0xffaa00
        )
        message = await ctx.send(embed=embed)
        
        # Verwende die neue Auto-Discovery Reload Funktion
        success, result = await self.bot.reload_all_cogs_with_discovery()
        
        if success:
            summary = result
            
            final_embed = discord.Embed(
                title="🔄 Auto-Reload Abgeschlossen",
                description=f"**{summary['loaded']}/{summary['discovered']}** Cogs erfolgreich geladen",
                color=0x00ff00 if summary['loaded'] == summary['discovered'] else 0xffaa00
            )
            
            if summary['new_cogs'] > 0:
                final_embed.add_field(
                    name="🆕 Neue Cogs",
                    value=f"{summary['new_cogs']} neue Cogs automatisch entdeckt!",
                    inline=False
                )
            
            final_embed.add_field(
                name="📊 Summary",
                value=f"Entladen: {summary['unloaded']}\n"
                      f"Entdeckt: {summary['discovered']}\n"
                      f"Geladen: {summary['loaded']}\n"
                      f"Neu: {summary['new_cogs']}",
                inline=True
            )
            
            # Zeige aktuell geladene Cogs
            loaded_cogs = [name.split('.')[-1] for name, status in self.bot.cog_status.items() if status == 'loaded']
            if loaded_cogs:
                final_embed.add_field(
                    name="✅ Aktive Cogs",
                    value="\n".join([f"• {cog}" for cog in loaded_cogs]),
                    inline=True
                )
        else:
            final_embed = discord.Embed(
                title="❌ Auto-Reload Fehlgeschlagen", 
                description=str(result),
                color=0xff0000
            )
        
        await message.edit(embed=final_embed)
    
    @master_control.command(name='discover', aliases=['disc'])
    async def master_discover(self, ctx):
        """Discover new cogs without reloading"""
        old_count = len(self.bot.cogs_list)
        old_cogs = self.bot.cogs_list.copy()
        
        # Re-discover
        self.bot.auto_discover_cogs()
        new_count = len(self.bot.cogs_list)
        new_cogs = [cog for cog in self.bot.cogs_list if cog not in old_cogs]
        
        embed = discord.Embed(
            title="🔍 Cog Discovery",
            color=0x00ffff
        )
        
        embed.add_field(
            name="📊 Ergebnis",
            value=f"Vorher: {old_count} Cogs\n"
                  f"Jetzt: {new_count} Cogs\n"
                  f"Neue: {len(new_cogs)} Cogs",
            inline=True
        )
        
        if new_cogs:
            embed.add_field(
                name="🆕 Neue Cogs gefunden",
                value="\n".join([f"• {cog.split('.')[-1]}" for cog in new_cogs]),
                inline=True
            )
            embed.color = 0x00ff00
        else:
            embed.add_field(
                name="ℹ️ Status", 
                value="Keine neuen Cogs gefunden",
                inline=True
            )
        
        embed.add_field(
            name="📋 Alle entdeckten Cogs",
            value="\n".join([f"• {cog.split('.')[-1]}" for cog in self.bot.cogs_list]),
            inline=False
        )
        
        await ctx.send(embed=embed)
    
    @master_control.command(name='shutdown', aliases=['stop', 'quit'])
    async def master_shutdown(self, ctx):
        """Shutdown the master bot"""
        embed = discord.Embed(
            title="🛑 Master Bot wird beendet",
            description="Bot fährt herunter...",
            color=0xff0000
        )
        await ctx.send(embed=embed)
        
        logging.info(f"Shutdown initiated by {ctx.author}")
        
        # Stoppe Unified Patch Bot vor Master Bot
        await self.bot.stop_unified_patchbot()
        await self.bot.close()
    
    @commands.group(name='patchbot', invoke_without_command=True, aliases=['patch'])
    @is_bot_owner()
    async def patchbot_control(self, ctx):
        """Unified Patch Bot Steuerung"""
        embed = discord.Embed(
            title="🎮 Unified Patch Bot Kontrolle",
            description="Steuere den einheitlichen Patchnotes Bot",
            color=0x00ff99
        )
        
        is_running, status_msg = await self.bot.get_unified_patchbot_status()
        status_emoji = "🟢" if is_running else "🔴"
        
        embed.add_field(
            name=f"{status_emoji} Status",
            value=status_msg,
            inline=False
        )
        
        embed.add_field(
            name="📋 Verfügbare Commands",
            value="`!patchbot start` - Bot starten\n"
                  "`!patchbot stop` - Bot stoppen\n"
                  "`!patchbot restart` - Bot neustarten\n"
                  "`!patchbot status` - Detaillierter Status",
            inline=False
        )
        
        await ctx.send(embed=embed)
    
    @patchbot_control.command(name='start')
    async def patchbot_start(self, ctx):
        """Startet den Unified Patch Bot"""
        success, message = await self.bot.start_unified_patchbot()
        
        embed = discord.Embed(
            title="🚀 Unified Patch Bot Start",
            description=message,
            color=0x00ff00 if success else 0xff0000
        )
        await ctx.send(embed=embed)
    
    @patchbot_control.command(name='stop')
    async def patchbot_stop(self, ctx):
        """Stoppt den Unified Patch Bot"""
        success, message = await self.bot.stop_unified_patchbot()
        
        embed = discord.Embed(
            title="🛑 Unified Patch Bot Stop",
            description=message,
            color=0x00ff00 if success else 0xff0000
        )
        await ctx.send(embed=embed)
    
    @patchbot_control.command(name='restart')
    async def patchbot_restart(self, ctx):
        """Startet den Unified Patch Bot neu"""
        # Erst stoppen
        stop_success, stop_msg = await self.bot.stop_unified_patchbot()
        
        # Kurz warten
        await asyncio.sleep(2)
        
        # Dann starten
        start_success, start_msg = await self.bot.start_unified_patchbot()
        
        embed = discord.Embed(
            title="🔄 Unified Patch Bot Restart",
            description=f"**Stop:** {stop_msg}\n**Start:** {start_msg}",
            color=0x00ff00 if start_success else 0xff0000
        )
        await ctx.send(embed=embed)
    
    @patchbot_control.command(name='status')
    async def patchbot_status(self, ctx):
        """Zeigt detaillierten Status des Unified Patch Bot"""
        is_running, status_msg = await self.bot.get_unified_patchbot_status()
        
        embed = discord.Embed(
            title="📊 Unified Patch Bot Status",
            color=0x00ff00 if is_running else 0xff0000
        )
        
        status_emoji = "🟢" if is_running else "🔴"
        embed.add_field(
            name=f"{status_emoji} Prozess Status",
            value=status_msg,
            inline=False
        )
        
        # Zeige Script-Pfad
        embed.add_field(
            name="📁 Script",
            value=f"`{self.bot.unified_patchbot_script}`",
            inline=False
        )
        
        # Zeige Features
        embed.add_field(
            name="⚡ Features",
            value="• Automatisches Haupt-Patch-Monitoring\n"
                  "• Automatisches Kommentar-Monitoring\n"
                  "• Steam-Content-Extraktion\n"
                  "• Discord-Markdown-Formatierung\n"
                  "• Intelligente Bild-Unterstützung",
            inline=False
        )
        
        embed.add_field(
            name="🎯 Überwacht",
            value="`https://forums.playdeadlock.com/forums/changelog.10/`",
            inline=False
        )
        
        await ctx.send(embed=embed)

async def main():
    """Main function to run the bot"""
    bot = MasterBot()
    
    # Add master control cog
    await bot.add_cog(MasterControlCog(bot))
    
    # Graceful shutdown handling
    def signal_handler(signum, frame):
        logging.info(f"Received signal {signum}, shutting down gracefully...")
        # Dann stoppe Master Bot
        asyncio.create_task(bot.close())
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        await bot.start(os.getenv('DISCORD_TOKEN'))
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received, shutting down...")
    except Exception as e:
        logging.error(f"Bot crashed: {e}")
        logging.error(traceback.format_exc())
    finally:
        if not bot.is_closed():
            await bot.close()

if __name__ == "__main__":
    asyncio.run(main())
