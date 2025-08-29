"""
TEMPLATE für neue unabhängige Cogs
Kopiere diese Datei und benenne sie um (z.B. mein_neuer_cog.py)
Das automatische Cog-Loading System wird sie automatisch finden und laden.

WICHTIG:
- Diese Datei beginnt mit _ und wird daher ignoriert
- Dein neues Cog sollte NICHT mit _ beginnen
- Es muss eine setup() Funktion oder eine Cog-Klasse enthalten
"""

import discord
from discord.ext import commands
import logging
import asyncio
from datetime import datetime
from typing import Optional

# Logger für dieses Cog (wird automatisch erstellt)
logger = logging.getLogger(__name__)

class TemplateExampleCog(commands.Cog):
    """
    Template für ein neues Cog - komplett unabhängig und selbst-konfigurierend
    
    Features die dein Cog haben kann:
    - Commands (! und slash commands)
    - Event Listeners
    - Background Tasks
    - Database Integration (falls nötig)
    - Eigene Konfiguration
    """
    
    def __init__(self, bot):
        self.bot = bot
        self.start_time = datetime.now()
        
        # Cog-spezifische Konfiguration (unabhängig vom Main Bot)
        self.config = {
            'enabled': True,
            'debug_mode': False,
            # Füge deine eigenen Config-Optionen hier hinzu
        }
        
        # Optional: Starte Background Tasks
        self.background_task.start()
        
        logger.info(f"✅ {self.__class__.__name__} initialized")
    
    def cog_unload(self):
        """Aufräumen beim Entladen des Cogs"""
        self.background_task.cancel()
        logger.info(f"🛑 {self.__class__.__name__} unloaded")
    
    # ==================== COMMANDS ====================
    
    @commands.group(name='template', invoke_without_command=True)
    async def template_command(self, ctx):
        """Basis Command für dein Cog"""
        embed = discord.Embed(
            title="🛠️ Template Cog",
            description="Dies ist ein Template für neue Cogs",
            color=0x00ffff
        )
        
        embed.add_field(
            name="ℹ️ Status",
            value=f"Läuft seit: {self.start_time.strftime('%H:%M:%S')}\n"
                  f"Enabled: {'✅' if self.config['enabled'] else '❌'}",
            inline=False
        )
        
        await ctx.send(embed=embed)
    
    @template_command.command(name='test')
    async def template_test(self, ctx):
        """Test Command"""
        if not self.config['enabled']:
            await ctx.send("❌ Template Cog ist deaktiviert")
            return
            
        await ctx.send("✅ Template Cog Test erfolgreich!")
    
    @template_command.command(name='toggle')
    async def template_toggle(self, ctx):
        """Enable/Disable das Cog"""
        self.config['enabled'] = not self.config['enabled']
        status = "aktiviert" if self.config['enabled'] else "deaktiviert"
        await ctx.send(f"🔄 Template Cog wurde **{status}**")
    
    # ==================== SLASH COMMANDS ====================
    
    @discord.app_commands.command(name='template_info', description='Template Cog Info')
    async def template_slash_info(self, interaction: discord.Interaction):
        """Beispiel Slash Command"""
        embed = discord.Embed(
            title="🛠️ Template Info (Slash)",
            description="Dies ist ein Slash Command Beispiel",
            color=0x00ffff
        )
        await interaction.response.send_message(embed=embed)
    
    # ==================== EVENT LISTENERS ====================
    
    @commands.Cog.listener()
    async def on_member_join(self, member):
        """Beispiel Event Listener"""
        if not self.config['enabled']:
            return
            
        if self.config['debug_mode']:
            logger.info(f"Template Cog: Member joined: {member}")
    
    @commands.Cog.listener()
    async def on_message(self, message):
        """Beispiel Message Listener"""
        if message.author.bot or not self.config['enabled']:
            return
        
        # Deine Message-Verarbeitung hier
        pass
    
    # ==================== BACKGROUND TASKS ====================
    
    @commands.loop(minutes=30)
    async def background_task(self):
        """Beispiel Background Task"""
        if not self.config['enabled']:
            return
            
        if self.config['debug_mode']:
            logger.info("Template Cog: Background task running")
        
        # Deine regelmäßige Aufgabe hier
        pass
    
    @background_task.before_loop
    async def before_background_task(self):
        """Warte bis Bot ready ist"""
        await self.bot.wait_until_ready()
    
    # ==================== HELPER METHODS ====================
    
    async def get_cog_status(self):
        """Gibt Status-Info für dieses Cog zurück"""
        return {
            'name': self.__class__.__name__,
            'enabled': self.config['enabled'],
            'start_time': self.start_time,
            'commands_count': len(self.get_commands()),
            'listeners_count': len(self.get_listeners())
        }

# ==================== SETUP FUNCTION ====================
# DIESE FUNKTION IST KRITISCH - ohne sie wird das Cog nicht geladen!

async def setup(bot):
    """
    Setup-Funktion - wird automatisch vom Bot aufgerufen
    
    WICHTIG: 
    - Diese Funktion MUSS existieren
    - Sie MUSS 'async def setup(bot):' heißen
    - Sie MUSS 'await bot.add_cog(DeinCogName(bot))' aufrufen
    """
    await bot.add_cog(TemplateExampleCog(bot))
    logger.info("✅ Template Example Cog added successfully")

# ==================== OPTIONAL TEARDOWN ====================

async def teardown(bot):
    """
    Optional: Teardown-Funktion beim Entladen
    Wird automatisch aufgerufen wenn das Cog entladen wird
    """
    logger.info("🛑 Template Example Cog teardown")