import discord
from discord.ext import commands
import asyncio

TOKEN = 'MTMzMDY2MDg3NzA1MjkzNjM1NA.G1u5BT.-wNkdHTJrtk_MUZTnoW6Py1ABY1aGNacn7-U-0'

class TestBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        
        super().__init__(
            command_prefix='!patch',
            intents=intents,
            description='Test Bot'
        )
    
    async def on_ready(self):
        print(f'✅ Test Bot bereit als {self.user}')
        print(f'Command prefix: {self.command_prefix}')
        print(f'Registered commands: {[cmd.name for cmd in self.commands]}')
    
    async def on_message(self, message):
        if message.author == self.user:
            return
            
        print(f'Nachricht erhalten: "{message.content}" von {message.author}')
        
        # Process commands
        await self.process_commands(message)
    
    async def on_command(self, ctx):
        print(f'Command erkannt: {ctx.command.name} von {ctx.author}')
    
    async def on_command_error(self, ctx, error):
        print(f'Command Error: {error}')
        if isinstance(error, commands.CommandNotFound):
            print(f'Command nicht gefunden: {ctx.message.content}')
        await ctx.send(f"❌ Fehler: {str(error)}")

bot = TestBot()

@bot.command(name='test')
async def test_command(ctx):
    """Test command"""
    print(f'Test command ausgeführt von {ctx.author}')
    await ctx.send('✅ Test erfolgreich!')

@bot.command(name='lastpatch')
async def lastpatch_command(ctx):
    """Last patch command"""
    print(f'Lastpatch command ausgeführt von {ctx.author}')
    await ctx.send('✅ Lastpatch Test erfolgreich!')

if __name__ == '__main__':
    print('Starte Test Bot...')
    bot.run(TOKEN)