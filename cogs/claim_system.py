import discord
from discord.ext import commands, tasks
import re
import asyncio
import sys
import json
import socket
import threading
import queue
import sqlite3
from pathlib import Path

# Event Loop Policy für Windows setzen
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Automatische Pfad-Erstellung
SCRIPT_DIR = Path(__file__).parent.absolute()
BASE_DIR = SCRIPT_DIR.parent

# Verzeichnisse definieren (Claim-Daten jetzt in DB, Logs bleiben in Datei)
CLAIM_DATA_DIR = BASE_DIR / "claim_data"
LOGS_DIR = BASE_DIR / "logs"

# Verzeichnisse erstellen
def ensure_directories():
    for directory in [CLAIM_DATA_DIR, LOGS_DIR]:
        directory.mkdir(parents=True, exist_ok=True)
        print(f"✅ Verzeichnis erstellt/überprüft: {directory}")

ensure_directories()

# Datenbank initialisieren (zentral)
SHARED_DIR = BASE_DIR / "shared"
SHARED_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = SHARED_DIR / "deadlock.db"

def init_database():
    """Initialisiert die zentrale Datenbank-Tabelle für Claims."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS claimed_threads (
            thread_id         BIGINT PRIMARY KEY,
            assigned_user_id  BIGINT,
            claimed_by_id     BIGINT,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_database()

# Claim-Daten in Memory laden (bereits bearbeitete Threads)
def load_claimed_threads():
    """Lädt alle Einträge aus claimed_threads in ein Dictionary."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT thread_id, claimed_by_id FROM claimed_threads")
    rows = cursor.fetchall()
    conn.close()
    # Dictionary: thread_id (als str) -> claimed_by (None falls noch nicht übernommen)
    data = {}
    for thread_id, claimed_by in rows:
        data[str(thread_id)] = None if claimed_by is None else int(claimed_by)
    return data

CLAIMED_THREADS = load_claimed_threads()

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
CLAIMED_THREADS = CLAIMED_THREADS  # (globale Dict bleibt erhalten)

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

# Queue für Kommunikation zwischen Socket-Thread und Bot-Thread
notification_queue = queue.Queue()

# --- Funktionen zum Persistieren über die DB (statt JSON) ---
def mark_thread_processed(thread_id: int, assigned_user_id: int):
    """Markiert einen neuen Thread als bearbeitet (noch nicht übernommen)."""
    # Eintrag in DB anlegen mit assigned_user und noch keinem Claimer
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO claimed_threads (thread_id, assigned_user_id, claimed_by_id) VALUES (?, ?, NULL)",
        (thread_id, assigned_user_id)
    )
    conn.commit()
    conn.close()
    # Im Cache-Dict vermerken (None = noch nicht übernommen)
    CLAIMED_THREADS[str(thread_id)] = None

def mark_thread_claimed(thread_id: int, claimer_id: int):
    """Markiert einen Thread als übernommen durch claimer_id."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE claimed_threads SET claimed_by_id = ? WHERE thread_id = ?",
        (claimer_id, thread_id)
    )
    conn.commit()
    conn.close()
    CLAIMED_THREADS[str(thread_id)] = claimer_id

def release_thread(thread_id: int):
    """Markiert einen Thread als freigegeben (assigned_user_id = 0)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE claimed_threads SET assigned_user_id = 0 WHERE thread_id = ?",
        (thread_id,)
    )
    conn.commit()
    conn.close()
    # Im Dictionary belassen wir den gleichen Status (None oder user), da Freigabe nur zur Laufzeit wirkt.

# --- ClaimView Klasse ---
class ClaimView(discord.ui.View):
    def __init__(self, thread_id, assigned_user_id):
        super().__init__(timeout=None)
        self.thread_id = thread_id
        self.assigned_user_id = assigned_user_id

    @discord.ui.button(label="Coaching übernehmen", style=discord.ButtonStyle.primary, custom_id="claim_button")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Prüfe Berechtigung (Staff-Rolle)
        has_staff_role = any(role.id == STAFF_ROLE_ID for role in interaction.user.roles)
        if not has_staff_role:
            await interaction.response.send_message("Du hast nicht die Berechtigung, das Coaching zu übernehmen.", ephemeral=True)
            return

        # Prüfe, ob der Thread bereits übernommen wurde
        if str(self.thread_id) in CLAIMED_THREADS and CLAIMED_THREADS[str(self.thread_id)] is not None:
            await interaction.response.send_message(f"Dieses Coaching wurde bereits von <@{CLAIMED_THREADS[str(self.thread_id)]}> übernommen.", ephemeral=True)
            return

        # Prüfe zugewiesenen Benutzer (nur der zugewiesene oder freigegeben=0 kann übernehmen)
        if interaction.user.id != self.assigned_user_id and self.assigned_user_id != 0:
            await interaction.response.send_message("Dieses Coaching ist einem anderen Benutzer zugewiesen.", ephemeral=True)
            return

        # Thread als übernommen markieren
        mark_thread_claimed(self.thread_id, interaction.user.id)

        # Button deaktivieren
        self.children[0].disabled = True
        await interaction.message.edit(view=self)

        # Bestätigung senden
        thread = interaction.channel
        await thread.send(f"<@{interaction.user.id}> hat dieses Coaching übernommen.")
        await interaction.response.send_message("Du hast dieses Coaching erfolgreich übernommen.", ephemeral=True)

    @discord.ui.button(label="Freigeben", style=discord.ButtonStyle.secondary, custom_id="release_button")
    async def release_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Prüfe Berechtigung (Staff-Rolle)
        has_staff_role = any(role.id == STAFF_ROLE_ID for role in interaction.user.roles)
        if not has_staff_role:
            await interaction.response.send_message("Du hast nicht die Berechtigung, dieses Coaching freizugeben.", ephemeral=True)
            return

        # Thread für alle freigeben
        self.assigned_user_id = 0
        release_thread(self.thread_id)  # in DB vermerken (zugewiesen=0)

        # Claim-Button aktivieren (für alle)
        self.children[0].disabled = False
        await interaction.message.edit(view=self)

        # Benachrichtigung an Staff-Rolle
        thread = interaction.channel
        await thread.send(f"<@&{STAFF_ROLE_ID}> Dieses Coaching wurde freigegeben und kann von jedem übernommen werden.")
        await interaction.response.send_message("Du hast dieses Coaching erfolgreich freigegeben.", ephemeral=True)

# Socket-Server (empfängt Benachrichtigungen vom Hauptbot über neue Coaching-Threads)
def start_socket_server():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((SOCKET_HOST, SOCKET_PORT))
        s.listen()
        print("Socket-Server gestartet, warte auf Verbindungen...")

        while True:
            conn, addr = s.accept()
            with conn:
                print(f"Verbindung von {addr} hergestellt")

                # Empfang der Nachrichtenlänge (4 Bytes)
                data_length_bytes = conn.recv(4)
                if not data_length_bytes:
                    continue
                data_length = int.from_bytes(data_length_bytes, byteorder='big')

                # Empfang der Nachricht
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

                # In Queue für den Bot-Thread legen
                notification_queue.put(data)

# Socket-Server in Thread starten
def initialize_socket_server():
    thread = threading.Thread(target=start_socket_server, daemon=True)
    thread.start()
    print("Socket-Server-Thread gestartet")

# Verarbeitung der empfangenen Benachrichtigung
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

        # Falls dieser Thread bereits bearbeitet wurde, abbrechen
        if str(thread_id) in CLAIMED_THREADS:
            print(f"Thread {thread_id} wurde bereits bearbeitet.")
            return

        # Thread-Channel holen
        thread = bot.get_channel(thread_id)
        if not thread:
            print(f"Thread {thread_id} nicht gefunden.")
            return

        print(f"Thread gefunden: {thread.name}")

        # Zuständigen Benutzer bestimmen (zuweisen oder freigeben)
        assigned_user_id = USERS["hucci1789"]["id"]  # Standard-Fallback
        try:
            assigned_user_id = int(data.get('assigned_user_id', 0))
        except:
            # Falls 'assigned_user_id' nicht in data, anhand von Rang/Subrang/Held bestimmen
            assigned_user_id = get_assigned_user(rank, subrank, hero)
        print(f"Zugewiesener Benutzer: {assigned_user_id}")

        # Benutzer zum Thread hinzufügen (falls nicht der Bot selbst)
        try:
            assigned_user = await bot.fetch_user(assigned_user_id)
            await thread.add_user(assigned_user)
            print(f"Benutzer {assigned_user_id} zum Thread hinzugefügt.")
        except Exception as e:
            print(f"Fehler beim Hinzufügen des Benutzers {assigned_user_id} zum Thread: {e}")

        # Nachricht mit Claim-Button im Thread posten
        view = ClaimView(thread_id, assigned_user_id)
        await thread.send(
            f"Dieses Coaching wurde <@{assigned_user_id}> zugewiesen. Bitte übernimm das Coaching, um es zu bearbeiten.",
            view=view
        )
        print(f"Claim-Button für Thread {thread_id} gesendet.")

        # Thread als bearbeitet markieren (noch nicht übernommen)
        mark_thread_processed(thread_id, assigned_user_id)
        print(f"Thread {thread_id} als bearbeitet markiert.")
    except Exception as e:
        print(f"Fehler bei der Verarbeitung der Benachrichtigung: {e}")

# Loop zur Verarbeitung der Queue
@tasks.loop(seconds=1)
async def check_notification_queue():
    try:
        while not notification_queue.empty():
            data = notification_queue.get()
            await process_notification_data(data)
    except Exception as e:
        print(f"Fehler in check_notification_queue: {e}")

@check_notification_queue.before_loop
async def before_check_notification():
    await bot.wait_until_ready()

# Starte Socket-Server und Task Loop
initialize_socket_server()
check_notification_queue.start()

# Bot-Start (Token aus .env laden empfohlen, hier fest im Code)
bot.run('<BOT_TOKEN_HERE>')
