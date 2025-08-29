import discord
from discord.ext import commands, tasks
from discord.ui import Button, View, Modal, TextInput
import sys
import asyncio
import re
import datetime
import socket
import json
import threading
from pathlib import Path

# Event Loop Policy für Windows setzen
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Automatische Pfad-Erstellung
SCRIPT_DIR = Path(__file__).parent.absolute()
BASE_DIR = SCRIPT_DIR.parent

# Arbeitsverzeichnisse definieren
DATA_DIR = BASE_DIR / "coaching_data"
LOGS_DIR = BASE_DIR / "logs"

# Alle benötigten Verzeichnisse erstellen
def ensure_directories():
    directories = [DATA_DIR, LOGS_DIR]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        print(f"✅ Verzeichnis erstellt/überprüft: {directory}")

# Beim Start ausführen
ensure_directories()

# Standard-Dateien erstellen falls nicht vorhanden
def create_default_files():
    files_to_create = [
        (DATA_DIR / "user_data.json", "{}"),
        (DATA_DIR / "active_threads.json", "{}"),
        (LOGS_DIR / "coaching_logs.txt", "")
    ]
    
    for file_path, default_content in files_to_create:
        if not file_path.exists():
            file_path.write_text(default_content, encoding='utf-8')
            print(f"📄 {file_path} erstellt")

create_default_files()

# Bot Setup
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# WICHTIG: Diese IDs für die existierende Nachricht
CHANNEL_ID = 1357421075188813897
EXISTING_MESSAGE_ID = 1383883328385454210  # Die ID der existierenden Nachricht mit dem Button

ALLOWED_ROLES = [1234, 5678]  # Beispiel Rollen IDs
TIMEOUT_SECONDS = 600  # 10 Minuten Timeout

async def find_coaching_message_in_channel(channel):
    """Finde existierende Coaching-Message im Kanal automatisch"""
    try:
        # Durchsuche die letzten 50 Messages im Kanal
        async for message in channel.history(limit=50):
            if (message.author.id == bot.user.id and 
                message.embeds and 
                len(message.embeds) > 0 and
                "Deadlock Match-Coaching" in message.embeds[0].title):
                print(f"Coaching-Message gefunden: {message.id} in Channel {channel.name}")
                return message
        
        return None
        
    except Exception as e:
        print(f"Fehler beim Suchen der Coaching-Message: {e}")
        return None

# Socket-Kommunikation Setup
SOCKET_HOST = 'localhost'
SOCKET_PORT = 45678

# Deadlock-Ränge mit Emoji-IDs
RANKS = {
    "initiate": "<:initiate:1316457822518775869>",
    "seeker": "<:seeker:1316458138886475876>",
    "alchemist": "<:alchemist:1316455291629342750>",
    "arcanist": "<:arcanist:1316455305315352587>",
    "ritualist": "<:ritualist:1316458203298660533>",
    "emissary": "<:emissary:1316457650367496306>",
    "archon": "<:archon:1316457293801324594>",
    "oracle": "<:oracle:1316457885743579317>",
    "phantom": "<:phantom:1316457982363701278>",
    "ascendant": "<:ascendant:1316457367818338385>",
    "eternus": "<:eternus:1316457737621868574>"
}

# Subränge
SUBRANKS = ['i', 'ii', 'iii', 'iv', 'v', '✶']

# Helden mit Emoji-IDs - in zwei Gruppen aufgeteilt für Paginierung
HEROES_PAGE_1 = {
    "abrams": "<:abrams:1371194882483294280>",
    "bebot": "<:bebot:1371194884547023080>",
    "calico": "<:calico:1371194886845632582>",
    "dynamo": "<:dynamo:1371194889592766514>",
    "grey_talon": "<:grey_talon:1371194891362898002>",
    "haze": "<:haze:1371194893640142858>",
    "holiday": "<:holiday:1371194895686963304>",
    "infernus": "<:Infernus:1371194897939566663>",
    "ivy": "<:ivy:1371194899432476722>",
    "kelvin": "<:kelvin:1371194901391474860>",
    "lady_geist": "<:lady_geist:1371194903018733758>",
    "lash": "<:lash:1371194904545333428>",
    "mirage": "<:mirage:1371194910232809552>"
}

HEROES_PAGE_2 = {
    "mo": "<:mo:1371194912489472091>",
    "paradox": "<:paradox:1371194915551182858>",
    "pocket": "<:pocket:1371194917627494420>",
    "seven": "<:seven:1371209369177427989>",
    "mcginnis": "<:mcginnis:1371209373350629428>",
    "shiv": "<:shiv:1371209379692679249>",
    "vindicta": "<:vindicta:1371209387217125467>",
    "sinclair": "<:sinclair:1371209388878073927>",
    "viscous": "<:viscous:1371209390367047692>",
    "vyper": "<:vyper:1371209401519575192>",
    "warden": "<:warden:1371209405068214442>",
    "wraith": "<:wraith:1371209407781666826>",
    "yamato": "<:yamato:1371209416258359376>"
}

# Temporäre Speicherung der Nutzerdaten
user_data = {}
active_threads = {}
thread_last_activity = {}

# Funktion zum Senden von Daten an Bot 2 über Socket
def notify_claim_bot(thread_data):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.connect((SOCKET_HOST, SOCKET_PORT))
            # Konvertiere Dictionary zu JSON-String
            json_data = json.dumps(thread_data)
            # Sende die Länge der Nachricht zuerst (4 Bytes)
            s.sendall(len(json_data).to_bytes(4, byteorder='big'))
            # Sende die eigentliche Nachricht
            s.sendall(json_data.encode('utf-8'))
            print(f"Benachrichtigung an Claim-Bot gesendet: {thread_data}")
    except Exception as e:
        print(f"Fehler beim Senden der Benachrichtigung: {e}")

# Modal für Match ID Eingabe
class MatchIDModal(Modal):
    def __init__(self):
        super().__init__(title="Match ID Eingabe")
        self.match_id = TextInput(label="Bitte gib die Match ID ein", placeholder="Match ID hier eingeben...", max_length=20)
        self.add_item(self.match_id)

    async def on_submit(self, interaction):
        user_id = interaction.user.id
        if user_id not in user_data:
            user_data[user_id] = {}
        
        user_data[user_id]['match_id'] = self.match_id.value
        
        # Erstelle einen privaten Thread für die Analyse
        thread = await interaction.channel.create_thread(
            name=f"Match-Coaching: {interaction.user.display_name}",
            type=discord.ChannelType.private_thread
        )
        
        # Speichere den Thread für diesen Nutzer
        active_threads[user_id] = thread.id
        thread_last_activity[thread.id] = datetime.datetime.now()
        
        # Füge den Nutzer zum Thread hinzu
        await thread.add_user(interaction.user)
        
        # Sende eine Bestätigung an den Nutzer
        await interaction.response.send_message(f"Ein Thread für dein Match-Coaching wurde erstellt. Bitte gehe zu {thread.mention}.", ephemeral=True)
        
        # Starte den Analyse-Prozess im Thread
        await start_analysis_in_thread(thread, interaction.user, self.match_id.value)

# Funktion zum Starten der Analyse im Thread
async def start_analysis_in_thread(thread, user, match_id):
    # Speichere die Match-ID
    user_id = user.id
    user_data[user_id]['match_id'] = match_id
    user_data[user_id]['step'] = 'rank'  # Setze den aktuellen Schritt
    user_data[user_id]['thread_id'] = thread.id
    
    # Sende die erste Nachricht im Thread
    embed = discord.Embed(
        title="Deadlock Match-Coaching  \n\nHinweiß der Bot Braucht ein paar Sekunden bis er auf eingaben Reagiert!",
        description=f"Match-ID: {match_id}\n\nBitte reagiere mit deinem Rang auf diese Nachricht. Hinweiß der Bot Braucht ein paar Sekunden bis er auf eingaben Reagiert!",
        color=discord.Color.blue()
    )
    
    rank_message = await thread.send(embed=embed)
    
    # Speichere die Nachricht-ID für spätere Referenz
    user_data[user_id]['rank_message_id'] = rank_message.id
    
    # Füge Reaktionen für Ränge hinzu
    for rank, emoji_str in RANKS.items():
        # Extrahiere die Emoji-ID aus dem String
        match = re.search(r'<:([^:]+):(\d+)>', emoji_str)
        if match:
            emoji_name = match.group(1)
            emoji_id = int(match.group(2))
            emoji = discord.utils.get(thread.guild.emojis, id=emoji_id)
            if emoji:
                await rank_message.add_reaction(emoji)
    
    # Aktualisiere den Zeitstempel der letzten Aktivität
    thread_last_activity[thread.id] = datetime.datetime.now()

# Klasse für den Start-Button
class StartView(View):
    def __init__(self):
        super().__init__(timeout=None)  # Kein Timeout für den Start-Button
        
        # Füge einen Start-Button hinzu
        start_button = Button(
            label="Match-Coaching starten", 
            style=discord.ButtonStyle.primary, 
            custom_id="start_analysis"
        )
        start_button.callback = self.start_analysis
        self.add_item(start_button)
    
    async def start_analysis(self, interaction):
        # Zeige das Modal für die Match-ID-Eingabe
        modal = MatchIDModal()
        await interaction.response.send_modal(modal)

# Periodische Überprüfung der Reaktionen und Thread-Timeouts
@tasks.loop(seconds=1)
async def check_reactions_and_timeouts():
    # Überprüfe Timeouts
    current_time = datetime.datetime.now()
    threads_to_close = []
    
    for thread_id, last_activity in thread_last_activity.items():
        if (current_time - last_activity).total_seconds() > TIMEOUT_SECONDS:
            threads_to_close.append(thread_id)
    
    for thread_id in threads_to_close:
        try:
            # Finde den Thread
            channel = bot.get_channel(thread_id)
            if channel and isinstance(channel, discord.Thread):
                await channel.send("Timeout erreicht. Thread wird geschlossen.")
                await channel.edit(archived=True, locked=True)
            
            # Entferne den Thread aus den Daten
            del thread_last_activity[thread_id]
            
            # Finde und entferne zugehörige Benutzerdaten
            for user_id, data in list(user_data.items()):
                if data.get('thread_id') == thread_id:
                    del user_data[user_id]
                    if user_id in active_threads:
                        del active_threads[user_id]
        except Exception as e:
            print(f"Fehler beim Schließen des Threads {thread_id}: {e}")
    
    # Überprüfe Reaktionen für jeden aktiven Benutzer
    for user_id, data in list(user_data.items()):
        if 'step' not in data:
            continue
        
        try:
            step = data['step']
            thread_id = data.get('thread_id')
            
            if not thread_id:
                continue
            
            channel = bot.get_channel(thread_id)
            if not channel:
                continue
            
            # Überprüfe je nach aktuellem Schritt
            if step == 'rank' and 'rank_message_id' in data:
                message = await channel.fetch_message(data['rank_message_id'])
                await check_rank_reactions(message, user_id)
            
            elif step == 'subrank' and 'subrank_message_id' in data:
                message = await channel.fetch_message(data['subrank_message_id'])
                await check_subrank_reactions(message, user_id)
            
            elif step == 'hero' and 'hero_message_id' in data:
                message = await channel.fetch_message(data['hero_message_id'])
                await check_hero_reactions(message, user_id)
            
            elif step == 'finish' and 'summary_message_id' in data:
                message = await channel.fetch_message(data['summary_message_id'])
                await check_finish_reactions(message, user_id)
        
        except discord.NotFound:
            # Nachricht wurde gelöscht
            continue
        except discord.HTTPException:
            # API-Fehler, überspringen und beim nächsten Durchlauf erneut versuchen
            continue
        except Exception as e:
            print(f"Fehler bei der Überprüfung der Reaktionen für Benutzer {user_id}: {e}")

# Funktion zur Überprüfung der Rang-Reaktionen
async def check_rank_reactions(message, user_id):
    if user_id not in user_data:
        return
    
    data = user_data[user_id]
    
    # Aktualisiere die Reaktionen
    message = await message.channel.fetch_message(message.id)
    
    for reaction in message.reactions:
        # Überprüfe nur Reaktionen, die nicht vom Bot sind
        async for user in reaction.users():
            if user.id == user_id:  # Der Benutzer hat reagiert
                emoji = reaction.emoji
                
                # Finde den Rang basierend auf der Emoji-ID
                selected_rank = None
                for rank, emoji_str in RANKS.items():
                    if isinstance(emoji, discord.Emoji) and str(emoji.id) in emoji_str:
                        selected_rank = rank
                        break
                
                if selected_rank:
                    # Speichere den Rang
                    data['rank'] = selected_rank
                    data['step'] = 'subrank'  # Aktualisiere den Schritt
                    
                    # Sende die nächste Nachricht für den Subrang
                    embed = discord.Embed(
                        title="Deadlock Match-Coaching",
                        description=f"Match-ID: {data['match_id']}\nRang: {selected_rank} {RANKS[selected_rank]}\n\nBitte reagiere mit deinem Subrang auf diese Nachricht.",
                        color=discord.Color.blue()
                    )
                    
                    subrank_message = await message.channel.send(embed=embed)
                    
                    # Speichere die Nachricht-ID für spätere Referenz
                    data['subrank_message_id'] = subrank_message.id
                    
                    # Füge Reaktionen für Subränge hinzu
                    for subrank in SUBRANKS:
                        if subrank == '✶':
                            await subrank_message.add_reaction('⭐')  # Unicode-Stern als Ersatz
                        else:
                            # Verwende Zahlen-Emojis für die Subränge
                            number_emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣']
                            if SUBRANKS.index(subrank) < len(number_emojis):
                                await subrank_message.add_reaction(number_emojis[SUBRANKS.index(subrank)])
                    
                    # Aktualisiere den Zeitstempel der letzten Aktivität
                    thread_last_activity[message.channel.id] = datetime.datetime.now()
                    return

# Funktion zur Überprüfung der Subrang-Reaktionen
async def check_subrank_reactions(message, user_id):
    if user_id not in user_data:
        return
    
    data = user_data[user_id]
    
    # Aktualisiere die Reaktionen
    message = await message.channel.fetch_message(message.id)
    
    for reaction in message.reactions:
        # Überprüfe nur Reaktionen, die nicht vom Bot sind
        async for user in reaction.users():
            if user.id == user_id:  # Der Benutzer hat reagiert
                emoji = reaction.emoji
                
                # Prüfe auf Zahlen-Emojis für Subränge
                number_emojis = ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣']
                selected_subrank = None
                
                if str(emoji) in number_emojis:
                    subrank_index = number_emojis.index(str(emoji))
                    selected_subrank = SUBRANKS[subrank_index]
                elif str(emoji) == '⭐':
                    selected_subrank = '✶'
                
                if selected_subrank:
                    # Speichere den Subrang
                    data['subrank'] = selected_subrank
                    data['step'] = 'hero'  # Aktualisiere den Schritt
                    data['hero_page'] = 1  # Starte mit Seite 1 für Helden
                    
                    # Sende die nächste Nachricht für die Helden (Seite 1)
                    embed = discord.Embed(
                        title="Deadlock Match-Coaching",
                        description=f"Match-ID: {data['match_id']}\nRang: {data['rank']} {RANKS[data['rank']]}\nSubrang: {selected_subrank}\n\nBitte reagiere mit deinem Helden auf diese Nachricht (Seite 1/2).",
                        color=discord.Color.blue()
                    )
                    
                    hero_message = await message.channel.send(embed=embed)
                    
                    # Speichere die Nachricht-ID für spätere Referenz
                    data['hero_message_id'] = hero_message.id
                    
                    # Füge Reaktionen für Helden (Seite 1) hinzu
                    for hero, emoji_str in HEROES_PAGE_1.items():
                        match = re.search(r'<:([^:]+):(\d+)>', emoji_str)
                        if match:
                            emoji_name = match.group(1)
                            emoji_id = int(match.group(2))
                            emoji = discord.utils.get(message.guild.emojis, id=emoji_id)
                            if emoji:
                                await hero_message.add_reaction(emoji)
                    
                    # Füge Navigationsreaktionen hinzu
                    await hero_message.add_reaction('➡️')  # Pfeil nach rechts für nächste Seite
                    
                    # Aktualisiere den Zeitstempel der letzten Aktivität
                    thread_last_activity[message.channel.id] = datetime.datetime.now()
                    return

# Funktion zur Überprüfung der Helden-Reaktionen
async def check_hero_reactions(message, user_id):
    if user_id not in user_data:
        return
    
    data = user_data[user_id]
    
    # Aktualisiere die Reaktionen
    message = await message.channel.fetch_message(message.id)
    
    for reaction in message.reactions:
        # Überprüfe nur Reaktionen, die nicht vom Bot sind
        async for user in reaction.users():
            if user.id == user_id:  # Der Benutzer hat reagiert
                emoji = reaction.emoji
                
                # Prüfe auf Navigationsreaktionen
                if str(emoji) == '➡️' and data['hero_page'] == 1:
                    # Wechsle zur Seite 2
                    await message.clear_reactions()
                    
                    embed = discord.Embed(
                        title="Deadlock Match-Coaching",
                        description=f"Match-ID: {data['match_id']}\nRang: {data['rank']} {RANKS[data['rank']]}\nSubrang: {data['subrank']}\n\nBitte reagiere mit deinem Helden auf diese Nachricht (Seite 2/2).",
                        color=discord.Color.blue()
                    )
                    
                    await message.edit(embed=embed)
                    data['hero_page'] = 2
                    
                    # Füge Reaktionen für Helden (Seite 2) hinzu
                    for hero, emoji_str in HEROES_PAGE_2.items():
                        match = re.search(r'<:([^:]+):(\d+)>', emoji_str)
                        if match:
                            emoji_name = match.group(1)
                            emoji_id = int(match.group(2))
                            emoji = discord.utils.get(message.guild.emojis, id=emoji_id)
                            if emoji:
                                await message.add_reaction(emoji)
                    
                    # Füge Navigationsreaktionen hinzu
                    await message.add_reaction('⬅️')  # Pfeil nach links für vorherige Seite
                    
                    # Entferne die Reaktion des Benutzers
                    await message.remove_reaction('➡️', user)
                    
                    # Aktualisiere den Zeitstempel der letzten Aktivität
                    thread_last_activity[message.channel.id] = datetime.datetime.now()
                    return
                
                elif str(emoji) == '⬅️' and data['hero_page'] == 2:
                    # Wechsle zur Seite 1
                    await message.clear_reactions()
                    
                    embed = discord.Embed(
                        title="Deadlock Match-Coaching",
                        description=f"Match-ID: {data['match_id']}\nRang: {data['rank']} {RANKS[data['rank']]}\nSubrang: {data['subrank']}\n\nBitte reagiere mit deinem Helden auf diese Nachricht (Seite 1/2).",
                        color=discord.Color.blue()
                    )
                    
                    await message.edit(embed=embed)
                    data['hero_page'] = 1
                    
                    # Füge Reaktionen für Helden (Seite 1) hinzu
                    for hero, emoji_str in HEROES_PAGE_1.items():
                        match = re.search(r'<:([^:]+):(\d+)>', emoji_str)
                        if match:
                            emoji_name = match.group(1)
                            emoji_id = int(match.group(2))
                            emoji = discord.utils.get(message.guild.emojis, id=emoji_id)
                            if emoji:
                                await message.add_reaction(emoji)
                    
                    # Füge Navigationsreaktionen hinzu
                    await message.add_reaction('➡️')  # Pfeil nach rechts für nächste Seite
                    
                    # Entferne die Reaktion des Benutzers
                    await message.remove_reaction('⬅️', user)
                    
                    # Aktualisiere den Zeitstempel der letzten Aktivität
                    thread_last_activity[message.channel.id] = datetime.datetime.now()
                    return
                
                else:
                    # Prüfe, ob ein Held ausgewählt wurde
                    selected_hero = None
                    heroes = HEROES_PAGE_1 if data['hero_page'] == 1 else HEROES_PAGE_2
                    
                    for hero, emoji_str in heroes.items():
                        if isinstance(emoji, discord.Emoji) and str(emoji.id) in emoji_str:
                            selected_hero = hero
                            break
                    
                    if selected_hero:
                        # Speichere den Helden
                        data['hero'] = selected_hero
                        data['step'] = 'comment'  # Aktualisiere den Schritt
                        
                        # Sende die nächste Nachricht für den Kommentar
                        embed = discord.Embed(
                            title="Deadlock Match-Coaching",
                            description=f"Match-ID: {data['match_id']}\nRang: {data['rank']} {RANKS[data['rank']]}\nSubrang: {data['subrank']}\nHeld: {selected_hero} {heroes[selected_hero]}\n\nBitte gib einen Kommentar zur Spielsituation ein (**antworte auf diese Nachricht**).",
                            color=discord.Color.blue()
                        )
                        
                        comment_message = await message.channel.send(embed=embed)
                        
                        # Speichere die Nachricht-ID für spätere Referenz
                        data['comment_message_id'] = comment_message.id
                        
                        # Aktualisiere den Zeitstempel der letzten Aktivität
                        thread_last_activity[message.channel.id] = datetime.datetime.now()
                        return

# Funktion zur Überprüfung der Fertig-Reaktion
async def check_finish_reactions(message, user_id):
    if user_id not in user_data:
        return
    
    data = user_data[user_id]
    
    # Aktualisiere die Reaktionen
    message = await message.channel.fetch_message(message.id)
    
    for reaction in message.reactions:
        # Überprüfe nur Reaktionen, die nicht vom Bot sind
        async for user in reaction.users():
            if user.id == user_id and str(reaction.emoji) == '✅':  # Der Benutzer hat mit ✅ reagiert
                # Erstelle einen Thread mit den gesammelten Daten
                channel = message.channel
                
                # Lösche alle vorherigen Nachrichten im Thread
                async for old_message in channel.history(limit=100):
                    try:
                        await old_message.delete()
                    except discord.errors.HTTPException:
                        # Ignoriere Fehler beim Löschen (z.B. wenn Nachricht bereits gelöscht wurde)
                        pass
                
                # Erstelle eine Nachricht mit den Daten
                content = (
                    f"**Match-Coaching**\n\n"
                    f"**Match ID:** {data.get('match_id')}\n"
                    f"**Rang:** {data.get('rank')} {RANKS.get(data.get('rank', ''), '')}\n"
                    f"**Subrang:** {data.get('subrank')}\n"
                )
                
                hero = data.get('hero', '')
                hero_emoji = ""
                if hero in HEROES_PAGE_1:
                    hero_emoji = HEROES_PAGE_1[hero]
                elif hero in HEROES_PAGE_2:
                    hero_emoji = HEROES_PAGE_2[hero]
                
                content += f"**Held:** {hero} {hero_emoji}\n"
                content += f"**Kommentar:** {data.get('comment', 'Kein Kommentar angegeben.')}\n\n"
                content += f"_______________________________\n"
                content += f"Analysiert von: <@{user_id}>\n"
                content += f"Coaching abgeschlossen! Danke für deine Eingaben."
                
                await channel.send(content)
                
                # Sende eine Benachrichtigung an den Claim-Bot über Socket
                notification_data = {
                    "thread_id": channel.id,
                    "match_id": data.get('match_id'),
                    "rank": data.get('rank'),
                    "subrank": data.get('subrank'),
                    "hero": data.get('hero'),
                    "user_id": user_id
                }
                
                # Sende die Daten über Socket
                notify_claim_bot(notification_data)
                
                # Lösche die Daten des Benutzers
                del user_data[user_id]
                if user_id in active_threads:
                    del active_threads[user_id]
                
                # Entferne den Thread aus der Timeout-Überwachung
                if message.channel.id in thread_last_activity:
                    del thread_last_activity[message.channel.id]
                
                return

@bot.event
async def on_message(message):
    # Ignoriere Nachrichten von Bots
    if message.author.bot:
        return
    
    # Prüfe, ob es sich um eine Antwort auf eine Kommentar-Nachricht handelt
    if message.reference and message.reference.message_id:
        user_id = message.author.id
        
        # Prüfe, ob der Nutzer eine aktive Analyse hat
        if user_id in user_data:
            data = user_data[user_id]
            
            # Prüfe, ob die Antwort auf die Kommentar-Nachricht ist
            if 'comment_message_id' in data and message.reference.message_id == data['comment_message_id']:
                # Speichere den Kommentar
                data['comment'] = message.content
                data['step'] = 'finish'  # Aktualisiere den Schritt
                
                # Sende die abschließende Nachricht
                channel = message.channel
                
                embed = discord.Embed(
                    title="Deadlock Match-Coaching - Zusammenfassung",
                    description="Hier ist eine Zusammenfassung deiner Eingaben. Reagiere mit ✅, um das Coaching abzuschließen.",
                    color=discord.Color.green()
                )
                
                embed.add_field(name="Match ID", value=data.get('match_id'), inline=False)
                embed.add_field(name="Rang", value=f"{data.get('rank')} {RANKS.get(data.get('rank', ''), '')}", inline=False)
                embed.add_field(name="Subrang", value=data.get('subrank'), inline=False)
                
                hero = data.get('hero', '')
                hero_emoji = ""
                if hero in HEROES_PAGE_1:
                    hero_emoji = HEROES_PAGE_1[hero]
                elif hero in HEROES_PAGE_2:
                    hero_emoji = HEROES_PAGE_2[hero]
                
                embed.add_field(name="Held", value=f"{hero} {hero_emoji}", inline=False)
                embed.add_field(name="Kommentar", value=data.get('comment', 'Kein Kommentar angegeben.'), inline=False)
                
                summary_message = await channel.send(embed=embed)
                
                # Speichere die Nachricht-ID für spätere Referenz
                data['summary_message_id'] = summary_message.id
                
                # Füge die Fertig-Reaktion hinzu
                await summary_message.add_reaction('✅')
                
                # Lösche die Kommentarnachricht für Übersichtlichkeit
                await message.delete()
                
                # Aktualisiere den Zeitstempel der letzten Aktivität
                thread_last_activity[channel.id] = datetime.datetime.now()
    
    await bot.process_commands(message)

@bot.event
async def on_ready():
    print(f'{bot.user} ist online!')
    # Starte die periodische Überprüfung
    check_reactions_and_timeouts.start()
    
    # Versuche die existierende Coaching-Nachricht automatisch zu finden
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        try:
            # Suche nach der Coaching-Message im Kanal
            coaching_message = await find_coaching_message_in_channel(channel)
            
            if coaching_message:
                # Erstelle eine neue View und hänge sie an die gefundene Nachricht
                view = StartView()
                await coaching_message.edit(view=view)
                
                print(f"✅ View erfolgreich an existierende Nachricht {coaching_message.id} angehängt!")
            else:
                print("❌ Keine Coaching-Nachricht im Kanal gefunden!")
            
        except Exception as e:
            print(f"❌ Fehler beim Suchen der Coaching-Nachricht: {e}")
            # Optional: Erstelle eine neue Nachricht falls die alte nicht gefunden wird
            # embed = discord.Embed(
            #     title="Deadlock Match-Coaching",
            #     description="Klicke auf den Button, um ein neues Match-Coaching zu starten.",
            #     color=discord.Color.blue()
            # )
            # view = StartView()
            # await channel.send(embed=embed, view=view)
        except Exception as e:
            print(f"❌ Fehler beim Anhängen der View: {e}")
    else:
        print(f"❌ Kanal mit ID {CHANNEL_ID} konnte nicht gefunden werden!")
    
    # AUSKOMMENTIERT: Erstellt keinen neuen Button mehr
    # # Sende eine Startnachricht im angegebenen Kanal
    # channel = bot.get_channel(CHANNEL_ID)
    # if channel:
    #     embed = discord.Embed(
    #         title="Deadlock Match-Coaching",
    #         description="Klicke auf den Button, um ein neues Match-Coaching zu starten.",
    #         color=discord.Color.blue()
    #     )
    #     
    #     view = StartView()
    #     await channel.send(embed=embed, view=view)
    # else:
    #     print(f"Kanal mit ID {CHANNEL_ID} konnte nicht gefunden werden!")

# WICHTIG: Ersetze 'DEIN_BOT_TOKEN' mit deinem echten Bot Token
# und speichere ihn niemals im Code! Nutze Umgebungsvariablen!
bot.run('MTM1NTA3ODE4OTg5NDA3ODU5Nw.GgkZvF.W6pmBEBMYCj9wYhOVHchNFQk6Q0Cod94Y0deAo')