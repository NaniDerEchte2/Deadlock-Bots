import os
import discord
from discord.ext import commands
from watchdog.observers import Observer
from bs4 import BeautifulSoup
from watchdog.events import FileSystemEventHandler
import asyncio
import logging
import hashlib
import time
import traceback
import threading
import re
import requests
from bs4 import BeautifulSoup
import sys
from pathlib import Path
from openai import OpenAI
from datetime import datetime
import pytz

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Automatische Pfad-Erstellung
SCRIPT_DIR = Path(__file__).parent.absolute()
BASE_DIR = SCRIPT_DIR.parent

# Arbeitsverzeichnisse definieren
CHANGELOG_DIR = BASE_DIR / "changelog_data"
LOGS_DIR = BASE_DIR / "logs"

# Alle benötigten Verzeichnisse erstellen
def ensure_directories():
    directories = [CHANGELOG_DIR, LOGS_DIR]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        print(f"✅ Verzeichnis erstellt/überprüft: {directory}")

# Beim Start ausführen
ensure_directories()

# Umfangreiches Logging konfigurieren
LOG_FILE = LOGS_DIR / "bot_logs.txt"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('ChangelogBot')
logging.getLogger('discord').setLevel(logging.WARNING)

# Konfiguration
TOKEN = 'MTMzMDY2MDg3NzA1MjkzNjM1NA.G1u5BT.-wNkdHTJrtk_MUZTnoW6Py1ABY1aGNacn7-U-0'
CHANNEL_ID = 1326973956825284628
LOG_CHANNEL_ID = 1374364800817303632
OUTPUT_FILE = CHANGELOG_DIR / "ausgabe.txt"
BOT_LOG_FILE = LOGS_DIR / "bot_logs.txt"
PROCESSED_FILE = CHANGELOG_DIR / "processed_changelogs.txt"
PERPLEXITY_API_KEY = "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"
MAX_MESSAGE_LENGTH = 1950
SEND_COOLDOWN = 1.5

# Thread monitoring configuration
MONITORED_THREADS = {}  # {thread_url: last_processed_comment_id}
THREADS_FILE = CHANGELOG_DIR / "monitored_threads.txt"
COMMENT_CHECK_INTERVAL = 30  # seconds

# Bot-Konfiguration
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Globale Variablen für Background Tasks
comment_monitor_task = None

# Erstelle Standard-Dateien falls nicht vorhanden
def create_default_files():
    files_to_create = [
        (OUTPUT_FILE, ""),
        (BOT_LOG_FILE, ""),
        (PROCESSED_FILE, ""),
        (THREADS_FILE, "")
    ]
    
    for file_path, default_content in files_to_create:
        if not file_path.exists():
            file_path.write_text(default_content, encoding='utf-8')
            print(f"📄 {file_path} erstellt")

create_default_files()

def clean_content(content):
    """Bereinigt den Inhalt, behält aber die Formatierung bei"""
    if not content:
        return content
    
    # Entferne Leerzeilen am Anfang und Ende
    content = content.strip()
    
    return content

def split_content_intelligently(content, max_length=1950):
    """Teilt den Inhalt intelligent auf, ohne Sätze oder Aufzählungen zu unterbrechen"""
    if len(content) <= max_length:
        return [content]
    
    chunks = []
    lines = content.split('\n')
    current_chunk = ""
    
    for line in lines:
        # Wenn die aktuelle Zeile zu groß ist, teile sie an Satzenden auf
        if len(line) > max_length:
            sentences = re.split(r'([.!?]\s)', line)
            i = 0
            while i < len(sentences):
                if i + 1 < len(sentences):
                    sentence = sentences[i] + sentences[i+1]
                    i += 2
                else:
                    sentence = sentences[i]
                    i += 1
                
                if len(current_chunk) + len(sentence) > max_length:
                    chunks.append(current_chunk)
                    current_chunk = sentence
                else:
                    current_chunk += sentence
        # Normale Zeile
        elif len(current_chunk) + len(line) + 1 > max_length:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            if current_chunk:
                current_chunk += "\n" + line
            else:
                current_chunk = line
    
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks

def ensure_mention_at_end(content):
    """Stellt sicher, dass die Mention am Ende der letzten Nachricht steht"""
    mention = "<@&1330994309524357140>"
    
    # Entferne existierende Mentions aus dem Content
    content = re.sub(r'<@&\d+>', '', content).strip()
    
    # Füge die Mention am Ende hinzu
    if not content.endswith(mention):
        content += f"\n\n{mention}"
    
    return content

def load_monitored_threads():
    """Lädt die überwachten Threads und ihre letzten verarbeiteten Kommentar-IDs"""
    global MONITORED_THREADS
    try:
        if THREADS_FILE.exists():
            with open(THREADS_FILE, 'r', encoding='utf-8') as f:
                lines = f.read().strip().split('\n')
                for line in lines:
                    if '|' in line:
                        thread_url, last_comment_id = line.split('|', 1)
                        MONITORED_THREADS[thread_url] = last_comment_id
                        logger.info(f"Thread überwacht: {thread_url} (letzte ID: {last_comment_id})")
    except Exception as e:
        logger.error(f"Fehler beim Laden der überwachten Threads: {e}")
        MONITORED_THREADS = {}

def save_monitored_threads():
    """Speichert die überwachten Threads"""
    try:
        with open(THREADS_FILE, 'w', encoding='utf-8') as f:
            for thread_url, last_comment_id in MONITORED_THREADS.items():
                f.write(f"{thread_url}|{last_comment_id}\n")
    except Exception as e:
        logger.error(f"Fehler beim Speichern der überwachten Threads: {e}")

def add_thread_to_monitoring(thread_url, last_comment_id=""):
    """Fügt einen Thread zur Überwachung hinzu"""
    global MONITORED_THREADS
    MONITORED_THREADS[thread_url] = last_comment_id
    save_monitored_threads()
    logger.info(f"Thread zur Überwachung hinzugefügt: {thread_url}")

def extract_forum_thread_comments(url):
    """Extrahiert alle Kommentare aus einem Forum-Thread"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        
        message_containers = soup.find_all('article', class_='message')
        if not message_containers:
            message_containers = soup.find_all('div', class_='message')
        
        comments = []
        
        for container in message_containers:
            comment_id = None
            if container.has_attr('data-content'):
                comment_id = container.get('data-content')
            elif container.has_attr('id'):
                comment_id = container.get('id')
            
            if not comment_id:
                continue
            
            content_selectors = [
                ('div', 'message-body'),
                ('div', 'message-inner'), 
                ('div', 'message-content'),
                ('article', 'message-body')
            ]
            
            comment_content = None
            for tag, class_name in content_selectors:
                content_element = container.find(tag, class_=class_name)
                if content_element:
                    for script in content_element.find_all(['script', 'style']):
                        script.decompose()
                    
                    comment_text = content_element.get_text(strip=True, separator='\n')
                    if comment_text and len(comment_text.strip()) > 20:
                        comment_content = comment_text.strip()
                        break
            
            if comment_content:
                timestamp_elem = container.find('time')
                timestamp = timestamp_elem.get('datetime') if timestamp_elem else "Unbekannt"
                
                # Parse timestamp and format it nicely
                formatted_time = "Unbekannt"
                if timestamp != "Unbekannt":
                    try:
                        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                        # Convert to German timezone
                        german_tz = pytz.timezone('Europe/Berlin')
                        dt_german = dt.astimezone(german_tz)
                        formatted_time = dt_german.strftime('%d.%m.%Y um %H:%M Uhr')
                    except:
                        formatted_time = timestamp
                
                comments.append({
                    'id': comment_id,
                    'content': comment_content,
                    'timestamp': timestamp,
                    'formatted_time': formatted_time
                })
        
        comments.sort(key=lambda x: str(x['id']))
        return comments

    except Exception as e:
        logger.error(f"Fehler beim Extrahieren der Forum-Kommentare: {e}")
        return []

def get_latest_patch_thread():
    """Findet den neuesten Patch-Thread im Forum"""
    try:
        base_url = "https://forums.playdeadlock.com/forums/game-updates.4/"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(base_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Suche nach Threads mit Patch-Indikatoren
        patch_patterns = [
            r'\d{2}-\d{2}-\d{4}\s*update',
            r'patch\s*notes',
            r'update\s*\d+',
            r'\d{1,2}/\d{1,2}/\d{4}',
            r'hotfix'
        ]
        
        threads = soup.find_all('div', class_='structItem-title')
        
        for thread in threads:
            link = thread.find('a')
            if link:
                thread_title = link.get_text().strip().lower()
                thread_url = "https://forums.playdeadlock.com" + link.get('href', '')
                
                # Prüfe auf Patch-Pattern
                for pattern in patch_patterns:
                    if re.search(pattern, thread_title, re.IGNORECASE):
                        logger.info(f"Patch-Thread gefunden: {thread_title} -> {thread_url}")
                        return thread_url, thread_title
        
        return None, None
    
    except Exception as e:
        logger.error(f"Fehler beim Suchen des neuesten Patch-Threads: {e}")
        return None, None

async def translate_with_ai(content):
    """Übersetzt den Inhalt mit der Perplexity AI"""
    try:
        client = OpenAI(api_key=PERPLEXITY_API_KEY, base_url="https://api.perplexity.ai")
        
        messages = [
            {"role": "system", "content": "Du bist ein Experte für Deadlock und übersetzt Patchnotes präzise und spielgerecht ins Deutsche."},
            {"role": "user", "content": f"""Übersetze die folgenden Deadlock Patchnotes ins Deutsche und formatiere sie für Discord:

1. Struktur:
   - Beginne mit '### Deadlock Patch Notes' als Hauptüberschrift
   - Verwende '##' für Kategorien/Abschnitte
   - Verwende '**Überschrift**' für Unterabschnitte
   - Verwende '-' für Aufzählungspunkte

2. Inhalt:
   - Behalte die exakte Reihenfolge der Änderungen bei
   - Übersetze alle Texte ins Deutsche, AUSSER Eigennamen und Item-Bezeichnungen
   - Verwende nur die gegebenen Informationen, keine externen Quellen
   - Ignoriere Bilder oder Links im Originaltext

3. Formatierung:
   - Halte dich an Discord-Formatierungsrichtlinien
   - Füge am Ende eine **Kurzzusammenfassung** hinzu, getrennt durch eine _____ Linie
   - Beende die Nachricht zwingend mit <@&1330994309524357140>

Hier sind die Patchnotes: {content}"""}
        ]
        
        response = client.chat.completions.create(
            model="sonar-pro",
            messages=messages,
        )
        
        return response.choices[0].message.content
        
    except Exception as e:
        logger.error(f"Fehler bei der KI-Übersetzung: {e}")
        return None

async def process_new_comments():
    """Überprüft alle überwachten Threads auf neue Kommentare"""
    global MONITORED_THREADS
    
    for thread_url, last_processed_id in MONITORED_THREADS.items():
        try:
            logger.info(f"Überprüfe Thread: {thread_url} (letzte ID: {last_processed_id})")
            
            # Extrahiere alle Kommentare
            comments = extract_forum_thread_comments(thread_url)
            
            if not comments:
                continue
            
            # Finde neue Kommentare
            new_comments = []
            for comment in comments:
                if not last_processed_id or comment['id'] > last_processed_id:
                    new_comments.append(comment)
            
            if new_comments:
                logger.info(f"Neue Kommentare gefunden: {len(new_comments)}")
                
                # Verarbeite nur den neuesten Kommentar
                latest_comment = new_comments[-1]
                
                # Erstelle Changelog für den neuen Kommentar
                changelog_content = f"### Deadlock Patch Notes Update\n\n"
                changelog_content += f"**Neuer Kommentar** - Veröffentlicht am {latest_comment['formatted_time']}\n\n"
                
                # Bereinige den Content
                content = latest_comment['content']
                content = re.sub(r'^Yoshi\nValve Developer\n.*?#\d+\n', '', content, flags=re.MULTILINE | re.DOTALL)
                content = re.sub(r'\nReactions:.*$', '', content, flags=re.MULTILINE | re.DOTALL)
                
                changelog_content += f"{content.strip()}\n\n"
                
                # KI-Übersetzung
                logger.info("Übersetze neuen Kommentar mit KI...")
                translated_content = await translate_with_ai(changelog_content)
                
                if translated_content:
                    changelog_content = translated_content
                    logger.info("KI-Übersetzung erfolgreich!")
                else:
                    logger.warning("KI-Übersetzung fehlgeschlagen, verwende Original")
                
                # Stelle sicher, dass die Mention am Ende steht
                changelog_content = ensure_mention_at_end(changelog_content)
                
                # Schreibe in ausgabe.txt für automatisches Posting
                with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
                    f.write(changelog_content)
                
                # Update last processed ID
                MONITORED_THREADS[thread_url] = latest_comment['id']
                save_monitored_threads()
                
                logger.info(f"Neuer Kommentar verarbeitet: {latest_comment['id']}")
                
                # Kurz warten vor dem nächsten Thread
                await asyncio.sleep(2)
        
        except Exception as e:
            logger.error(f"Fehler beim Verarbeiten von Thread {thread_url}: {e}")

async def comment_monitoring_loop():
    """Kontinuierliche Überwachung von Kommentaren"""
    global comment_monitor_task
    
    logger.info(f"Kommentar-Überwachung gestartet (alle {COMMENT_CHECK_INTERVAL} Sekunden)")
    
    while True:
        try:
            await process_new_comments()
            await asyncio.sleep(COMMENT_CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"Fehler in der Kommentar-Überwachung: {e}")
            await asyncio.sleep(60)  # Bei Fehlern länger warten

# Alles andere bleibt gleich...
def extract_steam_ids_from_url(url):
    """Extrahiert App-ID und News-ID aus einer Steam-URL"""
    app_id_match = re.search(r'app/(\d+)', url)
    news_id_match = re.search(r'view/(\d+)', url)
    
    app_id = app_id_match.group(1) if app_id_match else None
    news_id = news_id_match.group(1) if news_id_match else None
    
    return app_id, news_id

def get_steam_news_via_api(app_id, news_id=None):
    """Verbesserte Funktion zum Abrufen von Steam-News über die API"""
    try:
        api_url = f"http://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/?appid={app_id}&count=3&maxlength=0&format=json&feeds=steam_community_announcements"
        
        response = requests.get(api_url, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        if 'appnews' in data and 'newsitems' in data['appnews']:
            news_items = data['appnews']['newsitems']
            
            if news_id:
                for item in news_items:
                    if str(news_id) in item.get('url', ''):
                        return clean_content(item.get('contents', ''))
            
            if news_items:
                return clean_content(news_items[0].get('contents', ''))
        
        return None
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der Steam-News via API: {e}")
        return None

def get_steam_news_via_rss(app_id):
    """Holt News über den Steam RSS-Feed"""
    try:
        rss_url = f"https://store.steampowered.com/feeds/news/app/{app_id}/"
        response = requests.get(rss_url, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'xml')
        items = soup.find_all('item')
        
        if items:
            latest_item = items[0]
            title = latest_item.title.text if latest_item.title else ""
            description = latest_item.description.text if latest_item.description else ""
            
            return clean_content(f"{title}\n\n{description}")
        
        return None
    except Exception as e:
        logger.error(f"Fehler beim Abrufen des Steam RSS-Feeds: {e}")
        return None

def extract_steam_url_from_forum(url):
    """Extrahiert Steam-URLs aus einem Forum-Beitrag"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        
        steam_links = []
        
        for link in soup.find_all('a', href=re.compile(r'store\.steampowered\.com|steamcommunity\.com')):
            steam_links.append(link.get('href'))
        
        for embed in soup.find_all(class_=re.compile(r'bbCodeBlock-(steam|media)')):
            links = embed.find_all('a')
            for link in links:
                if 'steampowered.com' in link.get('href', '') or 'steamcommunity.com' in link.get('href', ''):
                    steam_links.append(link.get('href'))
        
        if steam_links:
            for link in steam_links:
                if 'news' in link:
                    return link
            return steam_links[0]
        
        return None

    except Exception as e:
        logger.error(f"Fehler beim Extrahieren der Steam-URL: {e}")
        return None

def extract_steam_news_content(url):
    """Extrahiert den Inhalt einer Steam-News-Seite mit verbesserten Selektoren"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

        app_id, news_id = extract_steam_ids_from_url(url)
        if app_id:
            api_content = get_steam_news_via_api(app_id, news_id)
            if api_content and len(api_content) > 200:
                return api_content
            
            rss_content = get_steam_news_via_rss(app_id)
            if rss_content and len(rss_content) > 200:
                return rss_content

        soup = BeautifulSoup(response.text, 'html.parser')
        
        steam_selectors = [
            ('div', 'eventtext'),
            ('div', 'body'),
            ('div', 'news_body'),
            ('div', 'announcementBody'),
            ('div', 'announcement_body'),
            ('div', 'bodytext'),
            ('div', 'body_text'),
            ('div', 'newsPostBlock'),
            ('div', 'bb_content')
        ]
        
        for tag, class_name in steam_selectors:
            content = soup.find(tag, class_=class_name)
            if content:
                for script in content.find_all(['script', 'style']):
                    script.decompose()
                
                text = content.get_text(separator='\n', strip=True)
                if text and len(text) > 100:
                    return clean_content(text)
        
        # Wenn kein spezifischer Inhalt gefunden wurde, versuche allgemeiner
        for id_pattern in ['news_content', 'announcement_content', 'newsPost', 'bodyContents']:
            main_content = soup.find('div', id=re.compile(f".*{id_pattern}.*"))
            if main_content:
                text = main_content.get_text(separator='\n', strip=True)
                if text and len(text) > 100:
                    return clean_content(text)
        
        # Versuche, den Inhalt anhand von Attributen zu finden
        for div in soup.find_all('div'):
            if div.has_attr('data-panel') and 'news' in div.get('data-panel', ''):
                text = div.get_text(separator='\n', strip=True)
                if text and len(text) > 100:
                    return clean_content(text)
        
        # Letzter Versuch: Suche nach dem größten Text-Block auf der Seite
        text_blocks = []
        for div in soup.find_all('div'):
            text = div.get_text(separator='\n', strip=True)
            if len(text) > 200:
                text_blocks.append((len(text), text))
        
        if text_blocks:
            text_blocks.sort(reverse=True)
            
            for _, text in text_blocks:
                if not re.search(r'(Home|Store|Community|About|Support).*(Home|Store|Community|About|Support)', text):
                    return clean_content(text)
            
            return clean_content(text_blocks[0][1])
        
        return None

    except Exception as e:
        logger.error(f"Fehler beim Extrahieren des Steam-Inhalts: {e}")
        return None

def extract_forum_content(url):
    """Extrahiert den Inhalt eines Forum-Beitrags"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        
        content_selectors = [
            ('div', 'message-inner'),
            ('div', 'message-body'),
            ('article', 'message-body'),
            ('div', 'message-content')
        ]
        
        for tag, class_name in content_selectors:
            main_content = soup.find(tag, class_=class_name)
            if main_content:
                for script in main_content.find_all(['script', 'style']):
                    script.decompose()
                
                content_text = main_content.get_text(strip=True, separator='\n')
                
                steam_url = extract_steam_url_from_forum(url)
                if steam_url:
                    if len(content_text.split()) < 50:
                        logger.info(f"Forum-Beitrag verweist auf Steam-Seite: {steam_url}")
                        steam_content = extract_steam_news_content(steam_url)
                        if steam_content and len(steam_content) > len(content_text):
                            return steam_content
                    else:
                        steam_content = extract_steam_news_content(steam_url)
                        if steam_content and len(steam_content) > 200:
                            combined_content = f"{content_text}\n\n--- Steam-Inhalt ---\n\n{steam_content}"
                            return clean_content(combined_content)
                
                return clean_content(content_text)
        
        logger.error(f"Kein Hauptinhalt mit bekannten Selektoren gefunden: {url}")
        
        steam_url = extract_steam_url_from_forum(url)
        if steam_url:
            logger.info(f"Versuche Steam-Inhalt zu extrahieren: {steam_url}")
            steam_content = extract_steam_news_content(steam_url)
            if steam_content:
                return steam_content
        
        return None

    except Exception as e:
        logger.error(f"Fehler beim Extrahieren des Forum-Inhalts: {e}")
        return None

class ThreadSafeUpdateHandler(FileSystemEventHandler):
    _instance_lock = threading.Lock()
    
    def __init__(self, bot, channel_id, output_file):
        self.bot = bot
        self.channel_id = channel_id
        self.output_file = output_file
        self.processed_hashes = set()
        self.last_send_time = 0
        self._processing = False
        
        self.load_processed_hashes()
        logger.info(f"UpdateHandler initialisiert für {output_file}")

    def load_processed_hashes(self):
        try:
            with open(PROCESSED_FILE, 'r') as f:
                self.processed_hashes = set(f.read().splitlines())
        except FileNotFoundError:
            self.processed_hashes = set()

    def save_processed_hash(self, content_hash):
        with open(PROCESSED_FILE, 'a') as f:
            f.write(f"{content_hash}\n")
        self.processed_hashes.add(content_hash)

    def get_content_hash(self, content):
        return hashlib.md5(content.encode('utf-8')).hexdigest()

    def on_created(self, event):
        with self._instance_lock:
            if (os.path.normpath(event.src_path) == os.path.normpath(self.output_file) 
                and not self._processing):
                self._processing = True
                asyncio.run_coroutine_threadsafe(self.send_discord_message(), self.bot.loop)

    def on_modified(self, event):
        with self._instance_lock:
            if (os.path.normpath(event.src_path) == os.path.normpath(self.output_file) 
                and not self._processing):
                self._processing = True
                asyncio.run_coroutine_threadsafe(self.send_discord_message(), self.bot.loop)

    async def send_discord_message(self):
        try:
            current_time = time.time()
            
            if current_time - self.last_send_time < SEND_COOLDOWN:
                logger.warning("Nachrichtenversand zu früh - abgebrochen")
                self._processing = False
                return

            await self.bot.wait_until_ready()
            channel = self.bot.get_channel(self.channel_id)
            
            if not channel:
                logger.error(f"Kanal mit ID {self.channel_id} nicht gefunden!")
                self._processing = False
                return
            
            await asyncio.sleep(2)
            
            with open(self.output_file, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            
            content_hash = self.get_content_hash(content)
            
            if not content or content_hash in self.processed_hashes:
                logger.info("Inhalt bereits verarbeitet oder leer.")
                self._processing = False
                return
            
            # Stelle sicher, dass die Mention am Ende steht
            content = ensure_mention_at_end(content)
            
            await self.send_with_error_handling(channel, content)
            
            self.save_processed_hash(content_hash)
            self.last_send_time = current_time
            
        except Exception as e:
            logger.error(f"Kritischer Fehler: {e}")
            logger.error(traceback.format_exc())
        
        finally:
            self._processing = False

    async def send_with_error_handling(self, channel, content):
        message_parts = split_content_intelligently(content)
        sent_parts = set()
        
        for part in message_parts:
            part_hash = hashlib.md5(part.encode()).hexdigest()
            
            if part_hash not in sent_parts:
                try:
                    await channel.send(part)
                    sent_parts.add(part_hash)
                    await asyncio.sleep(0.7)
                except discord.HTTPException as e:
                    logger.error(f"Fehler beim Senden: {e}")
                except Exception as e:
                    logger.error(f"Unerwarteter Fehler beim Senden: {e}")

async def check_bot_logs():
    """Überprüft die Bot-Logs-Datei auf neue Einträge"""
    try:
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if not log_channel:
            logger.error(f"Log-Kanal mit ID {LOG_CHANNEL_ID} nicht gefunden!")
            return
            
        bot.loop.create_task(monitor_bot_logs(log_channel))
        
    except Exception as e:
        logger.error(f"Fehler beim Überprüfen der Bot-Logs: {e}")

async def monitor_bot_logs(log_channel):
    """Überwacht kontinuierlich die Bot-Logs-Datei"""
    last_position = 0
    
    while True:
        try:
            if os.path.exists(BOT_LOG_FILE):
                with open(BOT_LOG_FILE, 'r', encoding='utf-8') as f:
                    f.seek(last_position)
                    new_logs = f.read()
                    last_position = f.tell()
                
                if new_logs.strip():
                    log_lines = new_logs.strip().split('\n')
                    
                    important_logs = []
                    for line in log_lines:
                        if '[ERROR]' in line or 'Neuer Changelog gefunden' in line or 'Changelog verarbeitet' in line:
                            important_logs.append(line)
                    
                    if important_logs:
                        log_message = '\n'.join(important_logs)
                        if len(log_message) > 1950:
                            log_message = log_message[:1950] + "..."
                        
                        await log_channel.send(log_message)
        
        except Exception as e:
            logger.error(f"Fehler beim Überwachen der Bot-Logs: {e}")
        
        await asyncio.sleep(10)

async def send_ki_translation(ctx, url):
    """Sendet eine KI-Übersetzung des Inhalts einer URL"""
    forum_content = None
    if "steampowered.com" in url:
        forum_content = extract_steam_news_content(url)
    else:
        forum_content = extract_forum_content(url)
        if not forum_content or len(forum_content.split()) < 50:
            steam_url = extract_steam_url_from_forum(url)
            if steam_url:
                await ctx.send(f"🔍 Forum-Beitrag verweist auf Steam-Seite: {steam_url}")
                forum_content = extract_steam_news_content(steam_url)
    
    if not forum_content:
        await ctx.send("❌ Konnte keinen Inhalt aus der URL extrahieren.")
        return
    
    await ctx.send(f"✅ Inhalt extrahiert ({len(forum_content)} Zeichen). Sende zur KI...")
    
    translated_notes = await translate_with_ai(forum_content)
    
    if not translated_notes:
        await ctx.send("❌ Fehler bei der KI-Übersetzung")
        return
    
    translated_notes = ensure_mention_at_end(translated_notes)
    
    chunks = split_content_intelligently(translated_notes, MAX_MESSAGE_LENGTH)
    
    await ctx.send("✅ KI-Übersetzung abgeschlossen:")
    
    for chunk in chunks:
        await ctx.send(chunk)
        await asyncio.sleep(0.7)

@bot.command(name="url")
async def url_test(ctx, url: str):
    """Testet eine Forum- oder Steam-URL und gibt die KI-Übersetzung im Log-Channel aus."""
    if ctx.channel.id != LOG_CHANNEL_ID:
        await ctx.send("Bitte nutze diesen Befehl nur im Log-/Testkanal.")
        return
    
    if not url.startswith(("http://", "https://")):
        await ctx.send("❌ Ungültiger URL-Format. Bitte gib eine vollständige URL an.")
        return
    
    if "playdeadlock.com" not in url and "steampowered.com" not in url:
        await ctx.send("❌ URL muss von playdeadlock.com oder steampowered.com sein.")
        return
    
    await ctx.send(f"🔍 Teste URL: {url}")
    await send_ki_translation(ctx, url)

@bot.command(name="monitor")
async def add_monitor(ctx, url: str, *, last_comment_id: str = ""):
    """Fügt einen Thread zur Kommentar-Überwachung hinzu"""
    if ctx.channel.id != LOG_CHANNEL_ID:
        await ctx.send("Bitte nutze diesen Befehl nur im Log-/Testkanal.")
        return
    
    if not url.startswith(("http://", "https://")):
        await ctx.send("❌ Ungültiger URL-Format. Bitte gib eine vollständige URL an.")
        return
    
    if "playdeadlock.com" not in url:
        await ctx.send("❌ URL muss von playdeadlock.com sein.")
        return
    
    add_thread_to_monitoring(url, last_comment_id)
    await ctx.send(f"✅ Thread zur Überwachung hinzugefügt: {url}")
    if last_comment_id:
        await ctx.send(f"Letzte verarbeitete ID: {last_comment_id}")

@bot.command(name="check_patches")
async def manual_patch_check(ctx):
    """Manueller Check nach neuen Patches"""
    if ctx.channel.id != LOG_CHANNEL_ID:
        await ctx.send("Bitte nutze diesen Befehl nur im Log-/Testkanal.")
        return
    
    await ctx.send("🔍 Suche nach neuen Patch-Threads...")
    
    thread_url, thread_title = get_latest_patch_thread()
    
    if thread_url:
        await ctx.send(f"✅ Neuester Thread gefunden: {thread_title}")
        await ctx.send(f"URL: {thread_url}")
        
        # Füge zur Überwachung hinzu wenn noch nicht vorhanden
        if thread_url not in MONITORED_THREADS:
            add_thread_to_monitoring(thread_url, "")
            await ctx.send("➕ Thread zur automatischen Überwachung hinzugefügt.")
        else:
            await ctx.send("ℹ️ Thread wird bereits überwacht.")
    else:
        await ctx.send("❌ Kein neuer Patch-Thread gefunden.")

@bot.event
async def on_ready():
    global comment_monitor_task
    
    logger.info(f'{bot.user} hat sich bei Discord angemeldet!')
    
    # Lade überwachte Threads
    load_monitored_threads()
    
    # Füge 07-29 Thread zur Überwachung hinzu (falls noch nicht vorhanden)
    july_29_thread = "https://forums.playdeadlock.com/threads/07-29-2025-update.72760/"
    if july_29_thread not in MONITORED_THREADS:
        add_thread_to_monitoring(july_29_thread, "post-141214")  # Letzte verarbeitete ID
    
    # Starte Kommentar-Überwachung
    comment_monitor_task = bot.loop.create_task(comment_monitoring_loop())
    
    await check_bot_logs()
    
    event_handler = ThreadSafeUpdateHandler(bot, CHANNEL_ID, OUTPUT_FILE)
    observer = Observer()
    observer.schedule(event_handler, path=str(CHANGELOG_DIR), recursive=False)
    observer.start()
    logger.info(f"Überwache Verzeichnis: {CHANGELOG_DIR}")

bot.run(TOKEN)