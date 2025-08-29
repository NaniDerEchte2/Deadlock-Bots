import discord
from discord.ext import commands
import asyncio
import os
import sys
import subprocess

class DlCoachingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.process = None
        
    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ DL Coaching System geladen")
        # Original-Skript als Subprocess starten
        self.process = subprocess.Popen([
            sys.executable, 
            r'C:\Users\Nani-Admin\Documents\Deadlock\original_scripts\dl_coaching.py'
        ])
    
    def cog_unload(self):
        if self.process:
            self.process.terminate()
            print("❌ DL Coaching System beendet")

async def setup(bot):
    await bot.add_cog(DlCoachingCog(bot))
