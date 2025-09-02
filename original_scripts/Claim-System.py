import discord
from discord.ext import commands, tasks
import re
import asyncio
import sys
import json
import socket
import threading
import queue
from pathlib import Path
import os

# Event Loop Policy für Windows setzen
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Automatische Pfad-Erstellung
SCRIPT_DIR = Path(__file__).parent.absolute()
BASE_DIR = SCRIPT_DIR.parent

# Arbeitsverzeichnisse definieren
CLAIM_DATA_DIR = BASE_DIR / "claim_data"
LOGS_DIR = BASE_DIR / "logs"

# Alle benötigten Verzeichnisse erstellen
def ensure_directories():
    directories = [CLAIM_DATA_DIR, LOGS_DIR]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        print(f"✅ Verzeichnis erstellt/überprüft: {directory}")

# Beim Start ausführen
ensure_directories()

# Standard-Dateien erstellen falls nicht vorhanden
def create_default_files():
    files_to_create = [
        (CLAIM_DATA_DIR / "claimed_threads.json", "{}"),
        (CLAIM_DATA_DIR / "user_assignments.json", "{}"),
        (LOGS_DIR / "claim_logs.txt", "")
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
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Socket-Kommunikation Setup
SOCKET_HOST = 'localhost'
SOCKET_PORT = 45680

# Konfiguration
STAFF_ROLE_ID = 1371929762913587292  # Rolle, die claimen kann
CLAIMED_THREADS = {}  # Speichert Thread-IDs und wer sie beansprucht hat

# Benutzer-IDs und Helden für die Zuweisung
USERS = {
    "ashty": {
        "id": 219209674019438592,
        "heroes": ["kelvin", "ivy", "lash", "sinclair", "mcginnis", "viscous", "abrams", "paradox", "mo", "holiday"]
    },
    "yourmomgosky": {
        "id": 678968588203327539,
        "heroes": ["lady_geist", "warden", "wraith", "kelvin", "ivy"]
    },
    "naniderechte": {
        "id": 662995601738170389,
        "heroes": []  # Fallback für alles unter Emissary 5
    },
    "hucci1789": {
        "id": 410856005551915008,
        "heroes": ["abrams", "paradox", "ivy", "dynamo", "pocket"]
    }
}

# Queue für die Kommunikation zwischen Socket-Thread und Bot-Thread
notification_queue = queue.Queue()

# Funktion zum Laden der gespeicherten Daten
def load_claimed_threads():
    try:
        with open(CLAIM_DATA_DIR / "claimed_threads.json", 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

# Funktion zum Speichern der Daten
def save_claimed_threads():
    with open(CLAIM_DATA_DIR / "claimed_threads.json", 'w', encoding='utf-8') as f:
        json.dump(CLAIMED_THREADS, f, indent=2)

# Lade gespeicherte Daten beim Start
CLAIMED_THREADS = load_claimed_threads()

# Funktion zur Bestimmung des zugewiesenen Benutzers
def get_assigned_user(rank, subrank, hero):
    # Konvertiere Rang und Subrang in numerische Werte für einfachere Vergleiche
    rank_values = {
        "initiate": 1, "seeker": 2, "alchemist": 3, "arcanist": 4, "ritualist": 5,
        "emissary": 6, "archon": 7, "oracle": 8, "phantom": 9, "ascendant": 10, "eternus": 11
    }
    
    subrank_values = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "✶": 6}
    
    rank_value = rank_values.get(rank.lower(), 0)
    subrank_value = subrank_values.get(subrank.lower(), 0)
    
    # Hucci1789: Alles bis Oracle 1 mit bestimmten Helden
    if rank_value <= 8 and subrank_value <= 1 and hero.lower() in USERS["hucci1789"]["heroes"]:
        return USERS["hucci1789"]["id"]
    
    # Ashty: Oracle 4+ mit bestimmten Helden ODER Phantom 1+ unabhängig vom Helden
    if (rank_value >= 8 and subrank_value >= 4 and hero.lower() in USERS["ashty"]["heroes"]) or \
       (rank_value >= 9 and subrank_value >= 1):
        return USERS["ashty"]["id"]
    
    # Yourmomgosky: Emissary 5+ bis Oracle 4 mit bestimmten Helden
    if (rank_value >= 6 and subrank_value >= 5 and rank_value <= 8 and subrank_value <= 4):
        if hero.lower() in USERS["yourmomgosky"]["heroes"]:
            return USERS["yourmomgosky"]["id"]
        
        # Prüfen, ob ein anderer Benutzer diesen Helden hat
        if hero.lower() not in USERS["ashty"]["heroes"] and hero.lower() not in USERS["hucci1789"]["heroes"]:
            return USERS["yourmomgosky"]["id"]
    
    # Naniderechte: Alles unter Emissary 5
    if rank_value < 6 or (rank_value == 6 and subrank_value < 5):
        return USERS["naniderechte"]["id"]
    
    # Standardmäßig an Naniderechte zuweisen
    return USERS["naniderechte"]["id"]

# Erstelle Claim-Button für Threads
class ClaimView(discord.ui.View):
    def __init__(self, thread_id, assigned_user_id):
        super().__init__(timeout=None)
        self.thread_id = thread_id
        self.assigned_user_id = assigned_user_id
    
    @discord.ui.button(label="Coaching übernehmen", style=discord.ButtonStyle.primary, custom_id="claim_button")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Prüfe, ob der Benutzer die richtige Rolle hat
        has_staff_role = False
        for role in interaction.user.roles:
            if role.id == STAFF_ROLE_ID:
                has_staff_role = True
                break
        
        if not has_staff_role:
            await interaction.response.send_message("Du hast nicht die Berechtigung, das Coaching zu übernehmen.", ephemeral=True)
            return
        
        # Prüfe, ob der Thread bereits beansprucht wurde
        if str(self.thread_id) in CLAIMED_THREADS and CLAIMED_THREADS[str(self.thread_id)] is not None:
            await interaction.response.send_message(f"Dieses Coaching wurde bereits von <@{CLAIMED_THREADS[str(self.thread_id)]}> übernommen.", ephemeral=True)
            return
        
        # Prüfe, ob der Benutzer der zugewiesene Benutzer ist oder ob das Coaching freigegeben wurde
        if interaction.user.id != self.assigned_user_id and self.assigned_user_id != 0:
            await interaction.response.send_message("Dieses Coaching ist einem anderen Benutzer zugewiesen.", ephemeral=True)
            return
        
        # Beanspruche den Thread
        CLAIMED_THREADS[str(self.thread_id)] = interaction.user.id
        save_claimed_threads()
        
        # Deaktiviere den Button
        self.children[0].disabled = True
        await interaction.message.edit(view=self)
        
        # Sende eine Bestätigung
        thread = interaction.channel
        await thread.send(f"<@{interaction.user.id}> hat dieses Coaching übernommen.")
        await interaction.response.send_message("Du hast dieses Coaching erfolgreich übernommen.", ephemeral=True)

    @discord.ui.button(label="Freigeben", style=discord.ButtonStyle.secondary, custom_id="release_button")
    async def release_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Prüfe, ob der Benutzer die richtige Rolle hat
        has_staff_role = False
        for role in interaction.user.roles:
            if role.id == STAFF_ROLE_ID:
                has_staff_role = True
                break
        
        if not has_staff_role:
            await interaction.response.send_message("Du hast nicht die Berechtigung, dieses Coaching freizugeben.", ephemeral=True)
            return
        
        # Setze den zugewiesenen Benutzer auf 0 (freigegeben)
        self.assigned_user_id = 0
        
        # Aktiviere den Claim-Button für alle
        self.children[0].disabled = False
        await interaction.message.edit(view=self)
        
        # Sende eine Benachrichtigung an die Rolle
        thread = interaction.channel
        await thread.send(f"<@&{STAFF_ROLE_ID}> Dieses Coaching wurde freigegeben und kann von jedem übernommen werden.")
        await interaction.response.send_message("Du hast dieses Coaching erfolgreich freigegeben.", ephemeral=True)

# Socket-Server zum Empfangen von Benachrichtigungen vom Hauptbot
def start_socket_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((SOCKET_HOST, SOCKET_PORT))
        s.listen()
        print("Socket-Server gestartet, warte auf Verbindungen...")
        
        while True:
            conn, addr = s.accept()
            with conn:
                print(f"Verbindung von {addr} hergestellt")
                
                # Empfange die Länge der Nachricht (4 Bytes)
                data_length_bytes = conn.recv(4)
                if not data_length_bytes:
                    continue
                    
                data_length = int.from_bytes(data_length_bytes, byteorder='big')
                
                # Empfange die eigentliche Nachricht
                chunks = []
                bytes_received = 0
                
                while bytes_received < data_length:
                    chunk = conn.recv(min(data_length - bytes_received, 4096))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    bytes_received += len(chunk)
                
                if bytes_received < data_length:
                    print("Unvollständige Daten empfangen")
                    continue
                
                data_json = b''.join(chunks).decode('utf-8')
                data = json.loads(data_json)
                
                print(f"Daten empfangen: {data}")
                
                # Füge die Daten zur Queue hinzu, damit sie vom Bot-Thread verarbeitet werden können
                notification_queue.put(data)

# Starte den Socket-Server in einem separaten Thread
def initialize_socket_server():
    thread = threading.Thread(target=start_socket_server, daemon=True)
    thread.start()
    print("Socket-Server-Thread gestartet")

# Funktion zur Verarbeitung der Benachrichtigung
async def process_notification_data(data):
    try:
        thread_id = data.get('thread_id')
        if not thread_id:
            print("Keine Thread-ID in den Daten gefunden")
            return
            
        match_id = data.get('match_id', "Unbekannt")
        rank = data.get('rank', "Unbekannt")
        subrank = data.get('subrank', "Unbekannt")
        hero = data.get('hero', "Unbekannt")
        user_id = data.get('user_id', 0)
        
        print(f"Verarbeite Thread: ID={thread_id}, Match ID={match_id}, Rang={rank}, Subrang={subrank}, Held={hero}, User ID={user_id}")
        
        # Prüfe, ob der Thread bereits bearbeitet wurde
        if str(thread_id) in CLAIMED_THREADS:
            print(f"Thread {thread_id} wurde bereits bearbeitet.")
            return
        
        # Hole den Thread
        thread = bot.get_channel(thread_id)
        if not thread:
            print(f"Thread {thread_id} nicht gefunden.")
            return
        
        print(f"Thread gefunden: {thread.name}")
        
        # Bestimme den zugewiesenen Benutzer
        assigned_user_id = get_assigned_user(rank, subrank, hero)
        print(f"Zugewiesener Benutzer: {assigned_user_id}")
        
        # Füge den zugewiesenen Benutzer zum Thread hinzu
        try:
            assigned_user = await bot.fetch_user(assigned_user_id)
            await thread.add_user(assigned_user)
            print(f"Benutzer {assigned_user_id} zum Thread hinzugefügt.")
        except Exception as e:
            print(f"Fehler beim Hinzufügen des Benutzers {assigned_user_id} zum Thread: {e}")
        
        # Sende eine Nachricht mit dem Claim-Button
        view = ClaimView(thread_id, assigned_user_id)
        await thread.send(
            f"Dieses Coaching wurde <@{assigned_user_id}> zugewiesen. Bitte übernimm das Coaching, um es zu bearbeiten.",
            view=view
        )
        print(f"Claim-Button für Thread {thread_id} gesendet.")
        
        # Markiere den Thread als bearbeitet
        CLAIMED_THREADS[str(thread_id)] = None  # Noch nicht beansprucht, aber bearbeitet
        save_claimed_threads()
        print(f"Thread {thread_id} als bearbeitet markiert.")
        
    except Exception as e:
        print(f"Fehler bei der Verarbeitung der Benachrichtigung: {e}")

# Task zum Überprüfen der Queue und Verarbeiten der Benachrichtigungen
@tasks.loop(seconds=1)
async def check_notification_queue():
    try:
        # Verarbeite alle Benachrichtigungen in der Queue
        while not notification_queue.empty():
            data = notification_queue.get_nowait()
            await process_notification_data(data)
    except Exception as e:
        print(f"Fehler beim Überprüfen der Benachrichtigungsqueue: {e}")

@bot.event
async def on_ready():
    print(f"{bot.user} ist online!")
    # Initialisiere den Socket-Server
    initialize_socket_server()
    # Starte die Task zum Überprüfen der Queue
    check_notification_queue.start()

# Manueller Befehl zum Testen
@bot.command()
async def process_notification(ctx, *, notification_text):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("Du benötigst Administratorrechte, um diesen Befehl zu verwenden.")
        return
    
    print(f"Manuell verarbeite: {notification_text}")
    
    try:
        # Versuche, die Daten als JSON zu parsen
        if notification_text.startswith("{") and notification_text.endswith("}"):
            data = json.loads(notification_text)
        else:
            # Verwende reguläre Ausdrücke für die Extraktion
            thread_id_match = re.search(r"'thread_id':\s*(\d+)", notification_text)
            match_id_match = re.search(r"'match_id':\s*'([^']+)'", notification_text)
            rank_match = re.search(r"'rank':\s*'([^']+)'", notification_text)
            subrank_match = re.search(r"'subrank':\s*'([^']+)'", notification_text)
            hero_match = re.search(r"'hero':\s*'([^']+)'", notification_text)
            user_id_match = re.search(r"'user_id':\s*(\d+)", notification_text)
            
            if not thread_id_match:
                await ctx.send("Thread ID nicht gefunden!")
                return
                
            data = {
                "thread_id": int(thread_id_match.group(1)),
                "match_id": match_id_match.group(1) if match_id_match else "Unbekannt",
                "rank": rank_match.group(1) if rank_match else "Unbekannt",
                "subrank": subrank_match.group(1) if subrank_match else "Unbekannt",
                "hero": hero_match.group(1) if hero_match else "Unbekannt",
                "user_id": int(user_id_match.group(1)) if user_id_match else 0
            }
        
        await process_notification_data(data)
        await ctx.send("Benachrichtigung erfolgreich verarbeitet.")
        
    except Exception as e:
        print(f"Fehler bei der manuellen Verarbeitung: {e}")
        await ctx.send(f"Fehler: {e}")

if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
    except Exception:
        load_dotenv = None

    if load_dotenv:
        # 1) Standard .env im aktuellen Arbeitsverzeichnis
        load_dotenv()
        # 2) Projektwurzel (eine Ebene höher): Deadlock/.env
        try:
            load_dotenv(Path(__file__).resolve().parent.parent / '.env')
        except Exception:
            pass
        # 3) Zentrale .env unter C:\Users\<User>\Documents\.env
        try:
            from pathlib import Path as _P
            load_dotenv(_P.home() / 'Documents' / '.env')
        except Exception:
            pass

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN nicht gesetzt. Bitte .env konfigurieren.")
    bot.run(token)
