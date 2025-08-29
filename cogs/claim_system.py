import discord
from discord.ext import commands
import asyncio
import os
import sys
import subprocess

class ClaimSystemCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.process = None
        
    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Claim System geladen")
        # Original-Skript als Subprocess starten
        self.process = subprocess.Popen([
            sys.executable, 
            r'C:\Users\Nani-Admin\Documents\Deadlock\original_scripts\Claim-System.py'
        ])
    
    def cog_unload(self):
        if self.process:
            self.process.terminate()
            print("❌ Claim System beendet")

async def setup(bot):
    await bot.add_cog(ClaimSystemCog(bot))
