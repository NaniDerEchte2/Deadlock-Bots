"""
UNIFIED DEADLOCK PATCHNOTES BOT - COMPLETE VERSION
Kombiniert ALLE Funktionen von Forum KI Bot + Changelog Discord Bot
- Forum-Monitoring (Haupt-Patches + Kommentare)
- Steam-Content-Extraktion 
- Perplexity AI Übersetzung
- Discord-Ausgabe mit Markdown-Formatierung
- Vollständige Command-Suite
- Läuft als unabhängiger Bot mit eigenem Token
"""

import discord
from discord.ext import commands, tasks
import asyncio
import os
import sys
import logging
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime
import json
from openai import OpenAI
import time
import traceback
from pathlib import Path
import hashlib
import pytz

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Automatische Pfad-Erstellung
SCRIPT_DIR = Path(__file__).parent.absolute()
CHANGELOG_DIR = SCRIPT_DIR / "changelog_data"
LOGS_DIR = SCRIPT_DIR / "logs"

# Alle benötigten Verzeichnisse erstellen
def ensure_directories():
    directories = [CHANGELOG_DIR, LOGS_DIR]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

ensure_directories()

# Dateipfade
LAST_PROCESSED_FILE = CHANGELOG_DIR / "last_processed_changelog.json"
PROCESSED_STEAM_LINKS_FILE = CHANGELOG_DIR / "processed_steam_links.json"
CURRENT_THREAD_FILE = CHANGELOG_DIR / "current_patch_thread.txt"
LAST_COMMENT_ID_FILE = CHANGELOG_DIR / "last_comment_id.txt"
OUTPUT_FILE = CHANGELOG_DIR / "ausgabe.txt"
LOG_FILE = LOGS_DIR / "unified_patchnotes_bot.log"

# Bot-Konfiguration
BASE_URL = "https://forums.playdeadlock.com"
CHANGELOG_URL = f"{BASE_URL}/forums/changelog.10/"
PERPLEXITY_API_KEY = "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"

# Discord Konfiguration
PATCHNOTES_BOT_TOKEN = os.getenv('PATCHNOTES_BOT_TOKEN', 'MTMzMDY2MDg3NzA1MjkzNjM1NA.G1u5BT.-wNkdHTJrtk_MUZTnoW6Py1ABY1aGNacn7-U-0')
DISCORD_LOG_CHANNEL_ID = 1374364800817303632  # Log-Nachrichten
DISCORD_PATCHNOTES_CHANNEL_ID = 1326973956825284628  # Patchnotes

# Logging konfigurieren
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('UnifiedPatchnotesBot')

# ==================== HELPER FUNCTIONS ====================

def create_default_files():
    """Erstellt Standard-Dateien falls nicht vorhanden"""
    files_to_create = [
        (LAST_PROCESSED_FILE, "{}"),
        (PROCESSED_STEAM_LINKS_FILE, "[]"),
        (CURRENT_THREAD_FILE, ""),
        (LAST_COMMENT_ID_FILE, ""),
        (OUTPUT_FILE, "")
    ]
    
    for file_path, default_content in files_to_create:
        if not file_path.exists():
            file_path.write_text(default_content, encoding='utf-8')
            logger.info(f"Default file created: {file_path}")

create_default_files()

# ==================== STEAM DISCOVERY HELPERS (TOP-LEVEL) ====================

def steam_load_processed_links() -> list:
    try:
        if PROCESSED_STEAM_LINKS_FILE.exists():
            with open(PROCESSED_STEAM_LINKS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return []

def steam_save_processed_links(items: list):
    try:
        with open(PROCESSED_STEAM_LINKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.getLogger('UnifiedPatchnotesBot').error(f"Fehler beim Speichern PROCESSED_STEAM_LINKS_FILE: {e}")

def steam_is_processed(url: str) -> bool:
    return url in steam_load_processed_links()

def steam_mark_processed(url: str):
    items = steam_load_processed_links()
    if url not in items:
        items.append(url)
        if len(items) > 200:
            items = items[-200:]
        steam_save_processed_links(items)

def get_latest_steam_news_url_from_store(app_id: str) -> str or None:
    """Reads https://store.steampowered.com/news/app/<app_id> and returns latest news URL."""
    try:
        url = f"https://store.steampowered.com/news/app/{app_id}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        # Prefer relative / absolute news links
        link = soup.find('a', href=re.compile(r'/news/app/\d+/view/\d+'))
        if not link:
            link = soup.find('a', href=re.compile(r'https://store\.steampowered\.com/news/app/\d+/view/\d+'))
        if link:
            href = link.get('href')
            if href.startswith('http'):
                return href
            return f"https://store.steampowered.com{href}"
        return None
    except Exception as e:
        logging.getLogger('UnifiedPatchnotesBot').error(f"Fehler beim Lesen der Steam-News-Seite: {e}")
        return None

def fetch_steam_rss_items(app_id: str) -> list:
    """Liest den RSS-Feed der App-News und gibt eine Liste von Items (neueste zuerst) zurück.
       Jedes Item: { 'title': str, 'link': str, 'pubDate': str, 'description': str }
    """
    url = f"https://store.steampowered.com/feeds/news/app/{app_id}/"
    headers = {'User-Agent': 'Mozilla/5.0'}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp.text)
        channel = root.find('channel') or root.find('.//channel')
        items = []
        if channel is None:
            return items
        for it in channel.findall('item'):
            title_el = it.find('title')
            link_el = it.find('link')
            date_el = it.find('pubDate')
            desc_el = it.find('description')
            title = (title_el.text or '').strip() if title_el is not None else ''
            link = (link_el.text or '').strip() if link_el is not None else ''
            pub = (date_el.text or '').strip() if date_el is not None else ''
            desc = (desc_el.text or '').strip() if desc_el is not None else ''
            if title and link:
                items.append({'title': title, 'link': link, 'pubDate': pub, 'description': desc})
        return items
    except Exception:
        # Simple regex fallback
        items = []
        for m in re.finditer(r'<item>\s*<title>(.*?)</title>[\s\S]*?<link>(.*?)</link>[\s\S]*?<pubDate>(.*?)</pubDate>[\s\S]*?<description><!\[CDATA\[([\s\S]*?)\]\]></description>', resp.text):
            items.append({'title': m.group(1), 'link': m.group(2), 'pubDate': m.group(3), 'description': m.group(4)})
        return items

async def steam_background_worker(bot):
    """Background worker: discovers latest Steam news and posts to Discord."""
    await bot.wait_until_ready()
    app_id = "1422450"
    while True:
        try:
            interval = get_patch_check_interval()
            urls_to_check = []
            # 1) RSS (zuverlässig, serverseitig)
            try:
                rss_items = fetch_steam_rss_items(app_id)
                for it in rss_items[:5]:
                    link = it.get('link')
                    if link:
                        urls_to_check.append(link)
            except Exception as _e:
                await bot.send_discord_log(f"Steam RSS Fehler: {_e}")
            # 2) Fallback: Heuristik
            if not urls_to_check:
                alt = get_latest_steam_news_url_from_store(app_id)
                if alt:
                    urls_to_check.append(alt)

            # Verarbeite neue URLs (älteste zuerst, damit Reihenfolge stimmt)
            for news_url in reversed(urls_to_check):
                if news_url and not steam_is_processed(news_url):
                    await bot.send_discord_log(f"Neue Steam-News gefunden: {news_url}")
                    content = bot.extract_steam_news_content(news_url, with_images=True)
                    if not content:
                        # Harte Fallback-Stufe: direkt aus RSS-Beschreibung extrahieren
                        try:
                            items = fetch_steam_rss_items(app_id)
                            for it in items:
                                if it.get('link') == news_url:
                                    desc_html = it.get('description') or ''
                                    if desc_html:
                                        content, _imgs = bot.clean_steam_content(desc_html, preserve_images=True)
                                        break
                        except Exception as _e:
                            await bot.send_discord_log(f"RSS-Notfallfallback fehlgeschlagen: {_e}")
                    if content:
                        translated = bot.send_to_perplexity_api(content, with_images=True)
                        if translated:
                            channel = bot.get_channel(DISCORD_PATCHNOTES_CHANNEL_ID)
                            if channel is None:
                                try:
                                    channel = await bot.fetch_channel(DISCORD_PATCHNOTES_CHANNEL_ID)
                                except Exception:
                                    channel = None
                            if channel is None:
                                channel = bot.get_channel(DISCORD_LOG_CHANNEL_ID)
                                if channel is None:
                                    try:
                                        channel = await bot.fetch_channel(DISCORD_LOG_CHANNEL_ID)
                                    except Exception:
                                        channel = None
                            if channel:
                                await channel.send(f"✅ **NEUE DEADLOCK STEAM-NEWS**\n🔗 {news_url}")
                                for chunk in bot.split_message_intelligently(translated):
                                    await channel.send(chunk)
                                    await asyncio.sleep(1)
                                steam_mark_processed(news_url)
                                await bot.send_discord_log("Steam-News gepostet und markiert")
                            else:
                                await bot.send_discord_log("Kein Discord-Channel gefunden (Patchnotes/Log)", is_error=True)
                        else:
                            await bot.send_discord_log("Übersetzung der Steam-News fehlgeschlagen", is_error=True)
                    else:
                        await bot.send_discord_log("Konnte Steam-Content nicht extrahieren", is_error=True)
            await asyncio.sleep(interval)
        except Exception as e:
            await bot.send_discord_log(f"Steam-Monitor Fehler: {e}", is_error=True)
            await asyncio.sleep(60)

def get_comment_check_interval():
    """Bestimmt das Überprüfungsintervall für Kommentare basierend auf der Tageszeit"""
    now = datetime.now()
    current_hour = now.hour
    
    if 8 <= current_hour <= 23:  # Tagsüber
        return 180  # 3 Minuten
    else:  # Nachts
        return 900  # 15 Minuten

def get_patch_check_interval():
    """Bestimmt das Überprüfungsintervall für Haupt-Patches basierend auf der Tageszeit"""
    now = datetime.now()
    current_hour = now.hour
    
    if 8 <= current_hour <= 23:  # Tagsüber
        return 300  # 5 Minuten  
    else:  # Nachts
        return 1800  # 30 Minuten

def clean_content(content):
    """Bereinigt den Content von unnötigen Zeichen"""
    if not content:
        return content
    
    # Entferne doppelte Leerzeichen und Zeilenschaltungen
    content = re.sub(r'\n\s*\n', '\n', content)
    content = re.sub(r' +', ' ', content)
    content = content.strip()
    
    return content

def load_current_patch_thread():
    """Lädt den aktuellen Patch-Thread"""
    try:
        if CURRENT_THREAD_FILE.exists():
            content = CURRENT_THREAD_FILE.read_text(encoding='utf-8').strip()
            if content and '|' in content:
                url, last_comment_id = content.split('|', 1)
                return url, last_comment_id
            elif content:
                return content, ""
        return None, ""
    except Exception as e:
        logger.error(f"Fehler beim Laden des aktuellen Threads: {e}")
        return None, ""

def save_current_patch_thread():
    """Speichert den aktuellen Patch-Thread"""
    try:
        global current_thread_url, last_processed_comment_id
        if current_thread_url:
            content = f"{current_thread_url}|{last_processed_comment_id}"
            CURRENT_THREAD_FILE.write_text(content, encoding='utf-8')
    except Exception as e:
        logger.error(f"Fehler beim Speichern des aktuellen Threads: {e}")

def set_current_patch_thread(thread_url, last_comment_id=""):
    """Setzt einen neuen aktuellen Patch-Thread"""
    global current_thread_url, last_processed_comment_id
    current_thread_url = thread_url
    last_processed_comment_id = last_comment_id
    save_current_patch_thread()
    logger.info(f"Neuer aktueller Thread gesetzt: {thread_url}")

def ensure_mention_at_end(content):
    """Stellt sicher, dass die Mention am Ende steht"""
    mention = "<@&1330994309524357140>"
    
    # Entferne existierende Mentions
    content = content.replace(mention, "").strip()
    
    # Füge Mention am Ende hinzu
    content = f"{content}\n\n{mention}"
    
    return content

# ==================== GLOBAL VARIABLES ====================

current_thread_url, last_processed_comment_id = load_current_patch_thread()

class UnifiedPatchnotesBot(commands.Bot):
    """Unified Deadlock Patchnotes Bot - Alle Funktionen in einem Bot"""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        
        super().__init__(
            command_prefix='!',
            intents=intents,
            description='Unified Deadlock Patchnotes Bot - Complete Edition',
            case_insensitive=True,
            help_command=None
        )
        
        # Bot-Status
        self.monitoring_enabled = True
        self.main_patch_task = None
        self.comment_task = None
        
    async def setup_hook(self):
        """Setup beim Bot-Start"""
        logger.info("🎮 Unified Patchnotes Bot (Complete) wird gestartet...")
        
    async def on_ready(self):
        """Bot ist bereit"""
        logger.info(f"✅ Unified Patchnotes Bot ist bereit als {self.user}")
        logger.info(f"Bot ID: {self.user.id}")
        
        # Setze Bot-Status
        await self.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, 
            name="Deadlock Forums & Steam 🎮"
        ))
        
        # Starte alle Monitoring-Tasks
        if self.monitoring_enabled:
            self.main_patch_task = asyncio.create_task(self.main_patch_monitoring_loop())
            self.comment_task = asyncio.create_task(self.comment_monitoring_loop())
            logger.info("🚀 Alle Monitoring-Tasks gestartet")
        
        # Zeige aktuellen Thread
        global current_thread_url, last_processed_comment_id
        if current_thread_url:
            logger.info(f"Aktueller überwachter Thread: {current_thread_url} (letzte ID: {last_processed_comment_id})")
        
    async def on_message(self, message):
        """Verarbeite alle Nachrichten und Commands"""
        if message.author == self.user:
            return
            
        # Logge NUR Patchnotes Commands
        if message.content.startswith('!patch') and not message.author.bot:
            logger.info(f"Command erhalten: '{message.content}' von {message.author}")
        
        # Verarbeite Commands für alle User (nicht nur nicht-Bots)
        await self.process_commands(message)
    
    async def on_command_error(self, ctx, error):
        """Fehlerbehandlung"""
        if isinstance(error, commands.CommandNotFound):
            if ctx.message.content.startswith('!patch'):
                logger.warning(f"Command nicht gefunden: '{ctx.message.content}' von {ctx.author}")
                available_commands = [
                    "`!patchtest <URL>` - URL testen",
                    "`!patchtestbilder <URL>` - URL mit Bildern testen", 
                    "`!patchlastpatch` - Letzten Patch testen",
                    "`!patchstatus` - Bot-Status anzeigen",
                    "`!patchhelp` - Hilfe anzeigen",
                    "`!patchthread <URL>` - Thread manuell setzen",
                    "`!patchcomment` - Kommentar-Monitoring testen"
                ]
                await ctx.send(f"❌ **Command nicht gefunden!** Verfügbare Commands:\n" + "\n• ".join(available_commands))
            return
        
        logger.error(f"Command Error: {error}")
        await ctx.send(f"❌ **Fehler:** {str(error)}")
    
    # ==================== FORUM MONITORING ====================
    
    async def send_discord_log(self, message, is_error=False):
        """Sendet Logs an Discord Channel"""
        try:
            channel = self.get_channel(DISCORD_LOG_CHANNEL_ID)
            if channel:
                prefix = "❌ **ERROR:**" if is_error else "ℹ️"
                await channel.send(f"{prefix} {message}")
        except Exception as e:
            logger.error(f"Discord log send failed: {e}")
    
    def get_latest_changelog_url(self):
        """Holt die URL des neuesten Changelog-Eintrags"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = requests.get(CHANGELOG_URL, headers=headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            changelog_entries = soup.find_all('a', string=re.compile(r'Update'))
            
            if changelog_entries:
                latest_entry = changelog_entries[0]
                entry_link = latest_entry.get('href')
                
                full_url = BASE_URL + entry_link if not entry_link.startswith('http') else entry_link
                return full_url
            
            return None
            
        except Exception as e:
            logger.error(f"Fehler bei Changelog-URL-Suche: {e}")
            return None
    
    def is_new_changelog(self, url):
        """Prüft ob es ein neuer Changelog ist"""
        try:
            if not LAST_PROCESSED_FILE.exists():
                return True
                
            with open(LAST_PROCESSED_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            last_url = data.get('url', '')
            return url != last_url
            
        except Exception as e:
            logger.error(f"Fehler beim Prüfen der letzten Changelog: {e}")
            return True
    
    def save_processed_changelog(self, url):
        """Speichert die verarbeitete Changelog URL"""
        try:
            data = {
                'url': url,
                'date': datetime.now().isoformat()
            }
            
            with open(LAST_PROCESSED_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            logger.error(f"Fehler beim Speichern der Changelog: {e}")
    
    async def main_patch_monitoring_loop(self):
        """Hauptschleife für Haupt-Patch Monitoring"""
        await self.send_discord_log("🚀 Haupt-Patch Monitoring gestartet")
        
        while self.monitoring_enabled:
            try:
                interval = get_patch_check_interval()
                
                now = datetime.now()
                current_day = now.strftime('%A')
                current_hour = now.strftime('%H:%M')
                
                if interval >= 1800 or now.minute % 30 == 0:
                    logger.info(f"[{current_day} {current_hour}] Überprüfe nach neuen Haupt-Patches... (Intervall: {interval} Sekunden)")
                
                latest_url = self.get_latest_changelog_url()
                
                if latest_url and self.is_new_changelog(latest_url):
                    await self.send_discord_log(f"📰 Neuer Haupt-Patch gefunden: {latest_url}")
                    
                    # WICHTIG: Speichere die URL SOFORT
                    self.save_processed_changelog(latest_url)
                    
                    # Setze als aktuellen Thread für Kommentar-Monitoring
                    set_current_patch_thread(latest_url, "")
                    
                    # Prozessiere den Changelog
                    await self.process_new_main_patch(latest_url)
                    await self.send_discord_log(f"✅ Haupt-Patch verarbeitet und gepostet: {latest_url}")
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                error_msg = f"Unerwarteter Fehler in der Haupt-Patch Monitoring Loop: {e}"
                await self.send_discord_log(error_msg, is_error=True)
                await self.send_discord_log(traceback.format_exc(), is_error=True)
                await asyncio.sleep(60)
    
    async def comment_monitoring_loop(self):
        """Überwacht Kommentare im aktuellen Thread"""
        await self.send_discord_log("💬 Kommentar-Monitoring gestartet")
        
        while self.monitoring_enabled:
            try:
                interval = get_comment_check_interval()
                
                global current_thread_url
                if current_thread_url:
                    new_comments = await self.check_for_new_comments(current_thread_url)
                    if new_comments:
                        await self.send_discord_log(f"💬 {len(new_comments)} neue Kommentare im aktuellen Thread")
                        for comment in new_comments:
                            await self.process_comment(comment)
                
                await asyncio.sleep(interval)
                
            except Exception as e:
                error_msg = f"Unerwarteter Fehler in der Kommentar Monitoring Loop: {e}"
                await self.send_discord_log(error_msg, is_error=True)
                await asyncio.sleep(60)
    
    async def check_for_new_comments(self, thread_url):
        """Prüft auf neue Kommentare in einem Thread"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            response = requests.get(thread_url, headers=headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            comments = soup.find_all('article', class_=re.compile(r'message'))
            
            new_comments = []
            global last_processed_comment_id
            
            for comment in comments:
                comment_id = comment.get('data-content', '')
                if comment_id and (not last_processed_comment_id or comment_id > last_processed_comment_id):
                    content_elem = comment.find('div', class_='message-content')
                    if content_elem:
                        content = content_elem.get_text(strip=True)
                        if content and len(content) > 50:  # Nur substantielle Kommentare
                            new_comments.append({
                                'id': comment_id,
                                'content': content,
                                'url': f"{thread_url}#post-{comment_id}"
                            })
            
            # Aktualisiere letzte Kommentar-ID
            if new_comments:
                last_processed_comment_id = max(comment['id'] for comment in new_comments)
                save_current_patch_thread()
            
            return new_comments
            
        except Exception as e:
            logger.error(f"Fehler beim Prüfen von Kommentaren: {e}")
            return []
    
    async def process_new_main_patch(self, url):
        """Verarbeitet einen neuen Haupt-Patch automatisch"""
        try:
            # Extrahiere Content (Forum + Steam)
            content = self.extract_forum_content(url, with_images=True)
            if not content:
                await self.send_discord_log(f"❌ Konnte keinen Content von {url} extrahieren", is_error=True)
                return
            
            # Übersetze mit Perplexity
            translated = self.send_to_perplexity_api(content, with_images=True)
            if not translated:
                await self.send_discord_log(f"❌ Übersetzung fehlgeschlagen für {url}", is_error=True)
                return
            
            # Poste in Discord Channel
            channel = self.get_channel(DISCORD_LOG_CHANNEL_ID)
            if channel:
                await channel.send(f"🚀 **NEUER DEADLOCK HAUPT-PATCH AUTOMATISCH ERKANNT**\n📋 URL: {url}")
                
                # Sende übersetzten Content in Chunks
                chunks = self.split_message_intelligently(translated)
                for chunk in chunks:
                    await channel.send(chunk)
                    await asyncio.sleep(1)  # Rate limiting
                
                logger.info(f"✅ Automatischer Haupt-Patch gepostet: {url}")
            else:
                logger.error(f"❌ Discord Channel {DISCORD_LOG_CHANNEL_ID} nicht gefunden")
            
        except Exception as e:
            logger.error(f"Fehler beim Verarbeiten des Haupt-Patch: {e}")
            await self.send_discord_log(f"❌ Fehler beim automatischen Posten: {str(e)}", is_error=True)
    
    async def process_comment(self, comment):
        """Verarbeitet einen neuen Kommentar"""
        try:
            # Prüfe ob der Kommentar wichtig genug ist
            content = comment['content']
            if len(content) < 100:  # Zu kurz
                return
            
            # Übersetze Kommentar
            translated = self.send_to_perplexity_api(content, with_images=False, is_comment=True)
            if not translated:
                return
            
            # Poste in Discord Channel
            channel = self.get_channel(DISCORD_LOG_CHANNEL_ID)
            if channel:
                await channel.send(f"💬 **NEUER PATCH-KOMMENTAR**\n🔗 {comment['url']}\n\n{translated}")
                logger.info(f"✅ Kommentar gepostet: {comment['id']}")
            
        except Exception as e:
            logger.error(f"Fehler beim Verarbeiten des Kommentars: {e}")
    
    # ==================== CONTENT EXTRACTION ====================
    
    def extract_steam_ids_from_url(self, url):
        """Extrahiert App-ID und News-ID aus einer Steam-URL"""
        app_id_match = re.search(r'app/(\d+)', url)
        news_id_match = re.search(r'view/(\d+)', url)
        
        app_id = app_id_match.group(1) if app_id_match else None
        news_id = news_id_match.group(1) if news_id_match else None
        
        return app_id, news_id
    
def get_steam_news_via_api(self, app_id, news_id=None, with_images=False, strict_id_match=False):
    """Abrufen von Steam-News über die API.

    - strict_id_match=True: Liefert nur Treffer, wenn die angegebene news_id eindeutig
      im Item (URL/GID) vorkommt. Kein heuristisches Fallback.
    - strict_id_match=False: Wenn keine ID angegeben ist, liefert das neueste Item.
    """
    try:
        api_url = f"https://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/?appid={app_id}&count=100&maxlength=0&format=json"
        response = requests.get(api_url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if 'appnews' in data and 'newsitems' in data['appnews']:
            news_items = data['appnews']['newsitems']

            # Striktes ID-Matching (wenn news_id vorhanden)
            if news_id:
                for item in news_items:
                    item_url = item.get('url', '')
                    item_gid = str(item.get('gid', ''))
                    # exakte Übereinstimmung: URL enthält /view/<news_id> oder GID == news_id
                    if (f"/view/{news_id}" in item_url) or (item_gid == str(news_id)):
                        content, images = self.clean_steam_content(item.get('contents', ''), preserve_images=with_images)
                        return content
                if strict_id_match:
                    return None

            # Locker: nimm das neueste Item
            if news_items and not strict_id_match:
                content, images = self.clean_steam_content(news_items[0].get('contents', ''), preserve_images=with_images)
                return content

        return None
    except Exception as e:
        logger.error(f"Fehler beim Abrufen der Steam-News via API: {e}")
        return None
    
    def extract_image_urls_from_content(self, content):
        """Extrahiert Bild-URLs aus dem Inhalt"""
        image_urls = []
        
        # Finde alle [img]URL[/img] Tags
        img_matches = re.findall(r'\[img\](.*?)\[/img\]', content, re.IGNORECASE)
        for img_url in img_matches:
            if img_url.strip():
                image_urls.append(img_url.strip())
        
        # Finde ALLE HTML img Tags von Steam-Seiten (egal welche Domain)
        img_html_matches = re.findall(r'<img[^>]*src=["\']([^"\']+)["\'][^>]*>', content, re.IGNORECASE)
        for img_url in img_html_matches:
            if img_url.strip() and ('http' in img_url or img_url.startswith('//')):
                image_urls.append(img_url.strip())
        
        return image_urls
    
    def clean_steam_content(self, content, preserve_images=False):
        """Bereinigt den Steam-Inhalt - mit optionaler Bild-Erhaltung"""
        if not content:
            return content, []
        
        # Extrahiere Bilder BEVOR sie gelöscht werden
        image_urls = self.extract_image_urls_from_content(content) if preserve_images else []
        
        if preserve_images and image_urls:
            # Ersetze [img] Tags mit Discord-freundlicher Darstellung
            for img_url in image_urls:
                # Konvertiere Steam-Platzhalter zu echten URLs und normalisiere alle URLs
                if img_url.startswith('{STEAM_CLAN_IMAGE}'):
                    # IMMER fastly CDN verwenden - das ist die aktuelle Steam CDN
                    full_url = img_url.replace('{STEAM_CLAN_IMAGE}', 'https://clan.fastly.steamstatic.com/images/')
                    logger.info(f"Verwende fastly CDN (Standard): {full_url}")
                elif 'STEAM_CLAN_IMAGE' in img_url:
                    full_url = img_url.replace('STEAM_CLAN_IMAGE', 'https://clan.fastly.steamstatic.com/images/')
                elif img_url.startswith('//'):
                    full_url = 'https:' + img_url  # Relative Protocol URLs
                elif not img_url.startswith('http'):
                    full_url = 'https://' + img_url  # Falls Protocol fehlt
                else:
                    full_url = img_url  # Bereits vollständige URL
                
                logger.info(f"Bild gefunden: {full_url}")
                content = content.replace(f'[img]{img_url}[/img]', f'\n{full_url}\n')
                # Ersetze auch HTML <img> Tags mit der URL in eigener Zeile
                try:
                    pattern = r'<img[^>]*src=["\']' + re.escape(img_url) + r'["\'][^>]*>'
                    content = re.sub(pattern, '\n' + full_url + '\n', content, flags=re.IGNORECASE)
                except Exception:
                    pass
            logger.info(f"{len(image_urls)} Bilder gefunden und konvertiert")
        else:
            # Alte Methode: Bilder entfernen
            content = re.sub(r'\[img\].*?\[/img\]', '', content)
        
        content = re.sub(r'\[url=.*?\](.*?)\[/url\]', r'\1', content)
        content = re.sub(r'\[.*?\]', '', content)
        content = re.sub(r'@[^\s]+', '', content)
        content = re.sub(r'\n\s*\n', '\n', content)
        content = content.strip()
        
        return content, image_urls
    
    def extract_steam_url_from_forum(self, url):
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
    
def extract_steam_news_content(self, url, with_images=False, prefer_scrape_for_view=True):
    """Extrahiert den Inhalt einer Steam-News-Seite.

    Wenn die URL eine konkrete News-ID ("/view/<id>") enthält, wird zuerst die Seite direkt
    gescraped, um ID-Mapping-Probleme der API zu vermeiden. Nur wenn Scraping fehlschlägt,
    wird die API mit strikt-ID-basiertem Matching versucht.
    """
    try:
        app_id, news_id = self.extract_steam_ids_from_url(url)

        # 1) Bei konkreter News-URL erst scrapen (verhindert falsche Items)
        if news_id and prefer_scrape_for_view:
            content = self.scrape_steam_news_page(url, with_images=with_images)
            if content and len(content) > 100:
                return content
            # 2) Falls Scraping fehlschlägt: API nur mit strengem ID-Match
            if app_id:
                api_content = self.get_steam_news_via_api(app_id, news_id, with_images=with_images, strict_id_match=True)
                if api_content and len(api_content) > 200:
                    return api_content
            # Kein early return hier – weiter mit generellen Fallbacks (API locker, Scrape, RSS)

        # 3) Allgemeiner Fall (kein /view/<id>): zuerst API (locker)
        if app_id:
            api_content = self.get_steam_news_via_api(app_id, news_id, with_images=with_images, strict_id_match=False)
            if api_content and len(api_content) > 200:
                return api_content
        # Danach Scraping versuchen
        content = self.scrape_steam_news_page(url, with_images=with_images)
        if content and len(content) > 100:
            return content
        # Letzter Fallback: RSS Beschreibung für exakte Link-Übereinstimmung
        if app_id:
            try:
                rss_items = fetch_steam_rss_items(app_id)
                for it in rss_items:
                    if it.get('link') == url:
                        desc_html = it.get('description') or ''
                        if desc_html:
                            clean_text, _imgs = self.clean_steam_content(desc_html, preserve_images=with_images)
                            if clean_text and len(clean_text) > 50:
                                logger.info(f"Steam RSS extraction successful. Length: {len(clean_text)}")
                                return clean_text
            except Exception as _e:
                logger.warning(f"Steam RSS fallback failed: {_e}")
        return None

    except Exception as e:
        logger.error(f"Fehler beim Extrahieren des Steam-Inhalts: {e}")
        return None
    
    def scrape_steam_news_page(self, url, with_images=False):
        """Scraped direkt eine Steam News Seite. Fällt auf eingebettete JSON-Daten zurück."""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9,de;q=0.8',
                'Referer': 'https://store.steampowered.com/news/app/1422450'
            }
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Finde den News Content Bereich - verschiedene Selektoren versuchen
            content_selectors = [
                ('div', {'id': 'news_body'}),('div', {'class': 'apphub_AppArticleContent'}),('div', {'class': 'apphub_CardTextContent'}),('div', {'class': 'apphub_CardContentMain'}),
                ('div', {'class': 'apphub_AppNews_Body'}),
                ('div', {'class': 'content'}),
                ('div', {'class': 'newsPostBody'}),
                ('main', {}),
            ]
            
            content_area = None
            for tag, attrs in content_selectors:
                if attrs:
                    content_area = soup.find(tag, attrs)
                else:
                    content_area = soup.find(tag)
                    
                if content_area:
                    logger.info(f"Content gefunden mit: {tag} {attrs}")
                    break
            
            if not content_area:
                logger.warning("Kein News Content Bereich gefunden - versuche eingebettete JSON-Daten")
                # Versuche eingebettete JSON-Daten (data-announcejson oder data-partnerevent)
                container = soup.find(attrs={'data-announcejson': True}) or soup.find(attrs={'data-partnerevent': True})
                if container:
                    from html import unescape
                    try:
                        raw = container.get('data-announcejson') or container.get('data-partnerevent')
                        data = json.loads(unescape(raw))
                        # data kann Liste oder Dict sein
                        items = []
                        if isinstance(data, list):
                            for d in data:
                                if isinstance(d, dict):
                                    if 'announcements' in d:
                                        items.extend(d.get('announcements') or [])
                                    else:
                                        items.append(d)
                        elif isinstance(data, dict):
                            items = data.get('announcements') or data.get('events') or []
                        # Wähle das passende Item (falls view/id in URL vorhanden)
                        app_id, news_id = self.extract_steam_ids_from_url(url)
                        chosen = None
                        if news_id:
                            for it in items:
                                try:
                                    gid = str(it.get('gid') or it.get('announcementGID') or it.get('event_gid') or '')
                                except Exception:
                                    gid = ''
                                if gid == str(news_id):
                                    chosen = it
                                    break
                        if not chosen and items:
                            chosen = items[0]
                        if chosen:
                            body = chosen.get('announcement_body') or chosen.get('body') or ''
                            if body and with_images:
                                # Stelle sicher, dass [img] Platzhalter unterstützt werden
                                content = body
                                # Kein HTML-Strip nötig da BBCode; Übergib an clean_steam_content
                                clean_text, _imgs = self.clean_steam_content(content, preserve_images=True)
                                logger.info(f"Steam JSON extraction successful. Length: {len(clean_text)}")
                                return clean_text
                            elif body:
                                clean_text, _imgs = self.clean_steam_content(body, preserve_images=False)
                                logger.info(f"Steam JSON extraction successful (no images). Length: {len(clean_text)}")
                                return clean_text
                    except Exception as e:
                        logger.warning(f"Steam JSON extraction failed: {e}")
                # Zusätzlicher Fallback: Roh-HTML nach announcement_body durchsuchen
                try:
                    raw_html = response.text
                    m = re.search(r'"announcement_body"\s*:\s*"([\s\S]*?)"', raw_html)
                    if not m:
                        m = re.search(r'"body"\s*:\s*"([\s\S]*?)"', raw_html)
                    if m:
                        enc = m.group(1)
                        try:
                            # JSON‑String sicher decodieren
                            decoded = json.loads(f'"{enc}"')
                        except Exception:
                            from html import unescape as _unesc
                            decoded = _unesc(enc)
                            decoded = decoded.replace('\\n', '\n').replace('\/', '/')
                        clean_text, _imgs = self.clean_steam_content(decoded, preserve_images=with_images)
                        if clean_text and len(clean_text) > 50:
                            logger.info(f"Steam regex JSON extraction successful. Length: {len(clean_text)}")
                            return clean_text
                except Exception as e:
                    logger.warning(f"Steam regex JSON fallback failed: {e}")
                # Fallback: versuche Titelbasiertes API-Matching
                try:
                    # Titel aus <title> oder og:title
                    page_title = None
                    title_tag = soup.find('title')
                    if title_tag and title_tag.text:
                        page_title = title_tag.text.strip()
                    if not page_title:
                        ogt = soup.find('meta', attrs={'property': 'og:title'})
                        if ogt and ogt.get('content'):
                            page_title = ogt.get('content').strip()
                    app_id, news_id = self.extract_steam_ids_from_url(url)
                    if page_title and app_id:
                        api_url = f"https://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/?appid={app_id}&count=100&maxlength=0&format=json"
                        resp = requests.get(api_url, timeout=10)
                        resp.raise_for_status()
                        data = resp.json()
                        items = (data.get('appnews') or {}).get('newsitems') or []
                        norm = lambda s: re.sub(r'\s+', ' ', (s or '').strip()).lower()
                        want = norm(page_title)
                        found = None
                        # 1) Präzise Titel-Übereinstimmung
                        for it in items:
                            if norm(it.get('title','')) == want:
                                found = it
                                break
                        # 2) URL enthält view/<id>
                        if not found and news_id:
                            for it in items:
                                if f"/view/{news_id}" in (it.get('url','')):
                                    found = it
                                    break
                        if found:
                            content, _imgs = self.clean_steam_content(found.get('contents',''), preserve_images=with_images)
                            if content and len(content) > 100:
                                logger.info(f"Steam API title-match extraction successful. Length: {len(content)}")
                                return content
                except Exception as e:
                    logger.warning(f"Steam API title-match fallback failed: {e}")
                # Letzter Fallback: komplette Seite als Textbereich
                content_area = soup
                
            # Extrahiere Text und Bilder
            raw_html = str(content_area)
            
            # Konvertiere zu Steam-ähnlichem Format
            # Finde img Tags und konvertiere zu [img] Format
            img_pattern = r'<img[^>]*src=["\']([^"\']*clan\.[^"\']*)["\'][^>]*>'
            
            def img_replacer(match):
                img_url = match.group(1)
                if with_images:
                    return f'[img]{img_url}[/img]'
                else:
                    return ''  # Entferne Bilder wenn nicht gewünscht
            
            # Ersetze img tags
            content = re.sub(img_pattern, img_replacer, raw_html, flags=re.IGNORECASE)
            
            # Entferne HTML Tags
            soup_content = BeautifulSoup(content, 'html.parser')
            text_content = soup_content.get_text(separator='\n', strip=True)
            
            # Bereinige den Content
            clean_content, images = self.clean_steam_content(text_content, preserve_images=with_images)
            
            logger.info(f"Steam page scraping successful. Content length: {len(clean_content)}")
            return clean_content
                
        except Exception as e:
            logger.error(f"Fehler beim direkten Steam News Scraping: {e}")
            return None
    
    def extract_forum_content(self, url, debug_mode=False, with_images=False):
        """Extrahiert den Inhalt eines Forum-Beitrags oder Steam-News - mit optionaler Bild-Extraktion"""
        try:
            # Falls es eine direkte Steam URL ist, verwende Steam-API direkt
            if 'store.steampowered.com/news/app/' in url:
                return self.extract_steam_news_content(url, with_images=with_images)
                
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Cache-Control': 'max-age=0'
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
                    
                    # Steam-URL-Extraktion
                    steam_url = self.extract_steam_url_from_forum(url)
                    if steam_url:
                        steam_content = self.extract_steam_news_content(steam_url, with_images=with_images)
                        if steam_content and len(steam_content) > 200:
                            combined_content = f"{content_text}\n\n--- Steam-Inhalt ---\n\n{steam_content}"
                            if with_images:
                                # Verarbeite auch das Forum-Content für Bilder
                                content_with_images, forum_images = self.clean_steam_content(combined_content, preserve_images=True)
                                if forum_images:
                                    logger.info(f"{len(forum_images)} Bilder aus Forum-Content gefunden")
                                return content_with_images
                            return combined_content
                    
                    if with_images:
                        # Verarbeite Forum-Content für Bilder auch ohne Steam-Link

                        # Verarbeite Forum-Content für Bilder auch ohne Steam-Link
                        content_with_images, forum_images = self.clean_steam_content(content_text, preserve_images=True)
                        if forum_images:
                            logger.info(f"{len(forum_images)} Bilder aus Forum gefunden")
                        return content_with_images
                    
                    return content_text
            
            return None
            
        except Exception as e:
            logger.error(f"Fehler bei Forum-Content-Extraktion: {e}")
            return None
    
    # ==================== TRANSLATION ====================
    
    def send_to_perplexity_api(self, content, with_images=False, is_comment=False):
        """Sendet den Inhalt an die Perplexity API zur Übersetzung - mit optionalem Bild-Support"""
        try:
            client = OpenAI(api_key=PERPLEXITY_API_KEY, base_url="https://api.perplexity.ai")
            
            if is_comment:
                # Spezielle Behandlung für Kommentare
                system_prompt = "Du bist ein Experte für Deadlock und übersetzt Kommentare zu Patchnotes präzise ins Deutsche."
                user_prompt = f"""Übersetze den folgenden Deadlock Patch-Kommentar ins Deutsche:

WICHTIG:
- Kurz und prägnant übersetzen
- Verwende normalen Text (KEIN Markdown)
- Behalte den Sinn und Kontext bei
- Füge am Ende <@&1330994309524357140> hinzu

Kommentar: {content}"""
            
            elif with_images:
                system_prompt = "Du bist ein Experte für Deadlock und übersetzt Patchnotes präzise und spielgerecht ins Deutsche. Du behältst Bild-URLs und formatierst sie Discord-freundlich."
                user_prompt = f"""Übersetze die folgenden Deadlock Patchnotes ins Deutsche und formatiere sie übersichtlich mit Discord-Markdown:

KRITISCH WICHTIG FÜR BILDER:
- Alle URLs die mit "https://clan.fastly.steamstatic.com/images/" oder "https://clan.akamai.steamstatic.com/images/" beginnen MÜSSEN 1:1 übernommen werden
- NIEMALS diese Bild-URLs übersetzen oder verändern
- NIEMALS Bild-URLs entfernen
- Behalte die Bild-URLs an ihrer ursprünglichen Position im Text

FORMATIERUNG VERWENDEN:
- Verwende **# Überschriften** für Hauptbereiche (# Deadlock Patch Notes)
- Verwende **## Unterüberschriften** für Charaktere, Items, etc.
- Verwende **### Kleine Überschriften** für Unterkategorien
- Verwende **fetten Text** für wichtige Änderungen
- Verwende *kursiven Text* für Erklärungen
- Verwende **- Listen** für übersichtliche Aufzählung von Änderungen
- Verwende **```Code-Blöcke```** für Zahlen/Statistiken falls nötig

STRUKTUR:
- Beginne mit "# Deadlock Patch Notes" als Hauptüberschrift
- Strukturiere nach Kategorien (## Helden, ## Items, ## Gameplay, etc.)
- Füge am Ende eine **## Zusammenfassung** hinzu
- Beende mit <@&1330994309524357140>

BEISPIEL von Bild-URLs die EXAKT übernommen werden müssen:
https://clan.fastly.steamstatic.com/images//45164767/6155ec51cb83504f4649748ee9be6cce27920329.png
https://clan.akamai.steamstatic.com/images//45164767/f67ecaff28204a3d8d9ab86f495a3e4465df3135.png

Hier sind die Patchnotes: {content}"""
            else:
                system_prompt = "Du bist ein Experte für Deadlock und übersetzt Patchnotes präzise und spielgerecht ins Deutsche."
                user_prompt = f"""Übersetze die folgenden Deadlock Patchnotes ins Deutsche und formatiere sie übersichtlich mit Discord-Markdown:

FORMATIERUNG VERWENDEN:
- Verwende **# Überschriften** für Hauptbereiche (# Deadlock Patch Notes)
- Verwende **## Unterüberschriften** für Charaktere, Items, etc.
- Verwende **### Kleine Überschriften** für Unterkategorien
- Verwende **fetten Text** für wichtige Änderungen
- Verwende *kursiven Text* für Erklärungen
- Verwende **- Listen** für übersichtliche Aufzählung von Änderungen
- Verwende **```Code-Blöcke```** für Zahlen/Statistiken falls nötig

STRUKTUR:
- Beginne mit "# Deadlock Patch Notes" als Hauptüberschrift
- Strukturiere nach Kategorien (## Helden, ## Items, ## Gameplay, etc.)
- Füge am Ende eine **## Zusammenfassung** hinzu
- Beende mit <@&1330994309524357140>

Hier sind die Patchnotes: {content}"""
            
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
            
            response = client.chat.completions.create(
                model="sonar-pro",
                messages=messages,
            )
            
            result = response.choices[0].message.content
            
            # Stelle sicher dass die Mention am Ende steht
            result = ensure_mention_at_end(result)
            
            return result
            
        except Exception as e:
            logger.error(f"Fehler bei Perplexity-Anfrage: {e}")
            return None

    # ==================== STEAM CONTENT (REDEFINED, ROBUST) ====================

    def scrape_steam_news_page(self, url, with_images=False):
        """Robuste Extraktion für Steam-View-Links über RSS-Beschreibung.
        Vermeidet dynamisches DOM; nutzt serverseitigen Feed-Inhalt (stabil)."""
        try:
            app_id, _ = self.extract_steam_ids_from_url(url) if hasattr(self, 'extract_steam_ids_from_url') else ("1422450", None)
            if not app_id:
                app_id = "1422450"
            items = fetch_steam_rss_items(app_id)
            for it in items:
                if it.get('link') == url:
                    desc_html = it.get('description') or ''
                    if desc_html:
                        clean_text, _imgs = self.clean_steam_content(desc_html, preserve_images=with_images)
                        return clean_text
            return None
        except Exception:
            return None

    def extract_steam_news_content(self, url, with_images=False, prefer_scrape_for_view=True):
        """Extrahiert Steam-News-Inhalt. Priorisiert RSS (stabil), dann API (strict), dann Scrape."""
        try:
            app_id, news_id = self.extract_steam_ids_from_url(url)

            # Konkrete View-URL: erst RSS (stabil)
            if news_id and prefer_scrape_for_view:
                content = self.scrape_steam_news_page(url, with_images=with_images)
                if content and len(content) > 50:
                    return content
                # Striktes API-Match als Fallback
                if app_id:
                    api_content = self.get_steam_news_via_api(app_id, news_id, with_images=with_images, strict_id_match=True)
                    if api_content and len(api_content) > 50:
                        return api_content
                # Letzter Versuch: generische API
                if app_id:
                    api_content = self.get_steam_news_via_api(app_id, None, with_images=with_images, strict_id_match=False)
                    if api_content and len(api_content) > 50:
                        return api_content
                return None

            # Allgemeiner Fall: API (locker), dann RSS/Scrape
            if app_id:
                api_content = self.get_steam_news_via_api(app_id, news_id, with_images=with_images, strict_id_match=False)
                if api_content and len(api_content) > 50:
                    return api_content

            content = self.scrape_steam_news_page(url, with_images=with_images)
            if content and len(content) > 50:
                return content
            return None
        except Exception as e:
            logger.error(f"Fehler beim Extrahieren des Steam-Inhalts: {e}")
            return None

    def split_message_intelligently(self, content, max_length=1950):
        """Teilt Nachrichten intelligent auf - mit Bild-Trennung"""
        has_steam_images = ("https://clan.akamai.steamstatic.com/images/" in content or 
                           "https://clan.fastly.steamstatic.com/images/" in content)
        if len(content) <= max_length and not has_steam_images:
            return [content]
        
        chunks = []
        lines = content.split('\n')
        current_chunk = ""
        
        for line in lines:
            # Wenn eine Bild-URL Zeile gefunden wird (beginnt mit https://)
            if (line.strip().startswith("https://clan.akamai.steamstatic.com/images/") or 
                line.strip().startswith("https://clan.fastly.steamstatic.com/images/")):
                # Schließe aktuelle Nachricht ab
                if current_chunk.strip():
                    chunks.append(current_chunk.strip())
                
                # Bild als eigene Nachricht
                chunks.append(line.strip())
                current_chunk = ""
                continue
            
            # Normale Zeilen-Verarbeitung
            if len(current_chunk) + len(line) + 1 > max_length:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = line
                else:
                    # Zeile ist zu lang, teile sie auf
                    words = line.split(' ')
                    for word in words:
                        if len(current_chunk) + len(word) + 1 > max_length:
                            chunks.append(current_chunk.strip())
                            current_chunk = word
                        else:
                            if current_chunk:
                                current_chunk += " " + word
                            else:
                                current_chunk = word
            else:
                if current_chunk:
                    current_chunk += "\n" + line
                else:
                    current_chunk = line
        
        if current_chunk.strip():
            chunks.append(current_chunk.strip())
        
        # Entferne leere Chunks
        chunks = [chunk for chunk in chunks if chunk.strip()]
        
        return chunks

# ==================== BOT CREATION ====================

def create_unified_bot():
    """Erstellt Bot-Instanz und registriert ALLE Commands"""
    bot = UnifiedPatchnotesBot()
    
    # Fallback: Sollte nach Änderungen eine Methode fehlen, stelle minimalen Ersatz bereit
    # damit Steam-Tests weiterhin funktionieren.
    if not hasattr(bot, 'extract_forum_content'):
        def _fallback_extract_forum_content(url, debug_mode=False, with_images=False):
            # Unterstütze zumindest Steam-Links für Tests (verwende Top-Level Funktion)
            if 'store.steampowered.com/news/app/' in url:
                return extract_steam_news_content(bot, url, with_images=with_images)
            return None
        bot.extract_forum_content = _fallback_extract_forum_content

    # Sicherstellen, dass ein Aufruf self.extract_steam_news_content verfügbar ist
    if not hasattr(bot, 'extract_steam_news_content'):
        def _extract_steam_news_content(url, with_images=False, prefer_scrape_for_view=True):
            return extract_steam_news_content(bot, url, with_images=with_images, prefer_scrape_for_view=prefer_scrape_for_view)
        bot.extract_steam_news_content = _extract_steam_news_content
    
    # Zusätzlicher on_ready Listener, der den Steam-Worker startet,
    # ohne die bestehende on_ready-Implementierung der Klasse zu überschreiben.
    async def _start_steam_worker():
        if not getattr(bot, "_steam_worker_started", False):
            bot._steam_worker_started = True
            bot.loop.create_task(steam_background_worker(bot))
    bot.add_listener(_start_steam_worker, 'on_ready')
    
    @bot.command(name='patchtest')
    async def patchtest(ctx, *, url: str = None):
        """Testet die Übersetzung einer spezifischen URL"""
        if not url:
            await ctx.send("❌ Bitte gib eine URL an: !patchtest <URL>")
            return
        
        if not url.startswith(('http://', 'https://')):
            await ctx.send("❌ URL muss mit http:// oder https:// beginnen!")
            return
            
        await ctx.send("🧪 Starte Test-Übersetzung...")
        
        try:
            content = bot.extract_forum_content(url, debug_mode=True, with_images=True)
            if not content:
                await ctx.send("❌ Konnte keinen Inhalt von der URL extrahieren.")
                return
            
            translated = bot.send_to_perplexity_api(content, with_images=True)
            if not translated:
                await ctx.send("❌ Übersetzung fehlgeschlagen.")
                return
            
            await ctx.send("✅ Test-Übersetzung abgeschlossen:")
            
            # Sende als normale Nachricht in Teilen
            chunks = bot.split_message_intelligently(translated)
            for chunk in chunks:
                await ctx.send(chunk)
            
        except Exception as e:
            await ctx.send(f"❌ Fehler: {str(e)}")
            logger.error(f"Fehler bei Test-Übersetzung: {e}")
    
    @bot.command(name='patchtestbilder')
    async def patchtest_with_images(ctx, *, url: str = None):
        """Testet die Übersetzung einer spezifischen URL MIT Bildern"""
        if not url:
            await ctx.send("❌ Bitte gib eine URL an: !patchtestbilder <URL>")
            return
        
        if not url.startswith(('http://', 'https://')):
            await ctx.send("❌ URL muss mit http:// oder https:// beginnen!")
            return
            
        await ctx.send("🖼️ Starte Test-Übersetzung MIT BILDERN...")
        
        try:
            content = bot.extract_forum_content(url, debug_mode=True, with_images=True)
            if not content:
                await ctx.send("❌ Konnte keinen Inhalt von der URL extrahieren.")
                return
            
            translated = bot.send_to_perplexity_api(content, with_images=True)
            if not translated:
                await ctx.send("❌ Übersetzung fehlgeschlagen.")
                return
            
            await ctx.send("✅ Test-Übersetzung MIT BILDERN abgeschlossen:")
            
            # Sende als normale Nachricht in Teilen
            chunks = bot.split_message_intelligently(translated)
            for chunk in chunks:
                await ctx.send(chunk)
            
        except Exception as e:
            await ctx.send(f"❌ Fehler: {str(e)}")
            logger.error(f"Fehler bei Test-Übersetzung mit Bildern: {e}")
    
    @bot.command(name='patchlastpatch')
    async def patchlastpatch(ctx):
        """Testet den letzten Patch"""
        await ctx.send("🔍 Suche nach letztem Changelog...")
        
        try:
            latest_url = bot.get_latest_changelog_url()
            if not latest_url:
                await ctx.send("❌ Kein Changelog gefunden.")
                return
            
            await ctx.send(f"🧪 Teste letzten Patch: {latest_url}")
            
            content = bot.extract_forum_content(latest_url, debug_mode=True, with_images=True)
            if content:
                translated = bot.send_to_perplexity_api(content, with_images=True)
                if translated:
                    await ctx.send("✅ Letzter Patch übersetzt:")
                    
                    # Sende als normale Nachricht in Teilen
                    chunks = bot.split_message_intelligently(translated)
                    for chunk in chunks:
                        await ctx.send(chunk)
                else:
                    await ctx.send("❌ Übersetzung fehlgeschlagen.")
            else:
                await ctx.send("❌ Konnte Inhalt nicht extrahieren.")
                
        except Exception as e:
            await ctx.send(f"❌ Fehler: {str(e)}")
            logger.error(f"Fehler bei Last-Patch-Test: {e}")
    
    @bot.command(name='patchthread')
    async def patchthread(ctx, *, url: str = None):
        """Setzt den aktuellen Thread für Kommentar-Monitoring"""
        if not url:
            global current_thread_url
            if current_thread_url:
                await ctx.send(f"🔗 **Aktueller Thread:** {current_thread_url}")
            else:
                await ctx.send("🟡 **Kein Thread gesetzt** - Verwende: `!patchthread <URL>`")
            return
        
        if not url.startswith(('http://', 'https://')):
            await ctx.send("❌ URL muss mit http:// oder https:// beginnen!")
            return
        
        set_current_patch_thread(url, "")
        await ctx.send(f"✅ **Thread gesetzt:** {url}\n💬 Kommentar-Monitoring für diesen Thread aktiv!")
    
    @bot.command(name='patchcomment')
    async def patchcomment(ctx):
        """Testet das Kommentar-Monitoring"""
        global current_thread_url
        if not current_thread_url:
            await ctx.send("❌ Kein Thread gesetzt! Verwende `!patchthread <URL>` zuerst.")
            return
        
        await ctx.send(f"🔍 Prüfe Kommentare in: {current_thread_url}")
        
        try:
            new_comments = await bot.check_for_new_comments(current_thread_url)
            if new_comments:
                await ctx.send(f"💬 **{len(new_comments)} neue Kommentare gefunden:**")
                for comment in new_comments[:3]:  # Nur erste 3 zeigen
                    await ctx.send(f"🔗 {comment['url']}\n```{comment['content'][:200]}...```")
            else:
                await ctx.send("🟡 Keine neuen Kommentare gefunden.")
                
        except Exception as e:
            await ctx.send(f"❌ Fehler: {str(e)}")
            logger.error(f"Fehler bei Kommentar-Test: {e}")
    
    @bot.command(name='patchstatus')
    async def patchstatus(ctx):
        """Zeigt den Status des Unified Patchnotes Bots"""
        try:
            # Bot-Info
            bot_info = f"**Bot:** {bot.user.name}#{bot.user.discriminator}\n**ID:** {bot.user.id}"
            
            # Monitoring Status
            main_patch_status = "🟢 Läuft" if bot.monitoring_enabled and bot.main_patch_task and not bot.main_patch_task.done() else "🔴 Gestoppt"
            comment_status = "🟢 Läuft" if bot.monitoring_enabled and bot.comment_task and not bot.comment_task.done() else "🔴 Gestoppt"
            
            # Steam-Links zählen
            steam_links_count = 0
            if PROCESSED_STEAM_LINKS_FILE.exists():
                try:
                    with open(PROCESSED_STEAM_LINKS_FILE, 'r', encoding='utf-8') as f:
                        steam_links = json.load(f)
                        steam_links_count = len(steam_links)
                except:
                    steam_links_count = "Fehler"
            
            # Letzte Verarbeitung
            last_processed = "Keine"
            if LAST_PROCESSED_FILE.exists():
                try:
                    with open(LAST_PROCESSED_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        last_processed = f"{data.get('url', 'Unbekannt')}\nZeit: {data.get('date', 'Unbekannt')}"
                except:
                    last_processed = "Fehler beim Lesen"
            
            # Aktueller Thread
            global current_thread_url, last_processed_comment_id
            current_thread_info = f"{current_thread_url}" if current_thread_url else "Keiner"
            
            status_text = f"""📊 **Unified Deadlock Patchnotes Bot Status (Complete)**
            
{bot_info}

🔄 **Haupt-Patch Monitoring:** {main_patch_status}
💬 **Kommentar-Monitoring:** {comment_status}
📋 **Überwacht:** `/forums/changelog.10/`

🔗 **Aktueller Thread:** {current_thread_info}
📝 **Letzte Kommentar-ID:** {last_processed_comment_id or "Keine"}

🔗 **Verarbeitete Steam-Links:** {steam_links_count}

📝 **Letzte Haupt-Patch Verarbeitung:**
{last_processed}

🧪 **Verfügbare Commands:**
• `!patchtest <URL>` - URL testen (ohne Bilder)
• `!patchtestbilder <URL>` - URL testen MIT Bildern
• `!patchlastpatch` - Letzten Patch testen
• `!patchthread <URL>` - Thread für Kommentar-Monitoring setzen
• `!patchcomment` - Kommentar-Monitoring testen
• `!patchstatus` - Diesen Status anzeigen
• `!patchhelp` - Hilfe anzeigen

⚡ **Features:**
• Automatisches Haupt-Patch-Monitoring
• Automatisches Kommentar-Monitoring
• Steam-Content-Extraktion
• Discord-Markdown-Formatierung
• Intelligente Bild-Unterstützung
• Vollständige Command-Suite"""

            await ctx.send(status_text)
            
        except Exception as e:
            await ctx.send(f"❌ Fehler beim Abrufen des Status: {str(e)}")
            logger.error(f"Fehler bei Status-Abfrage: {e}")
    
    @bot.command(name='patchhelp')
    async def patchhelp(ctx):
        """Zeigt die Hilfe für den Unified Patchnotes Bot"""
        help_text = """🎮 **Unified Deadlock Patchnotes Bot - Hilfe (Complete Edition)**

**Automatische Funktionen:**
• **Haupt-Patch-Monitoring:** Überwacht `/forums/changelog.10/` automatisch
• **Kommentar-Monitoring:** Überwacht Kommentare im aktuellen Thread
• **Steam-Integration:** Extrahiert Steam-Content automatisch
• **Discord-Ausgabe:** Postet neue Patches + Kommentare automatisch

**Test Commands:**
• `!patchtest <URL>` - Übersetzt eine spezifische Forum-URL (ohne Bilder)
• `!patchtestbilder <URL>` - Übersetzt eine Forum-URL MIT Bildern
• `!patchlastpatch` - Findet und übersetzt den neuesten Patch

**Thread Management:**
• `!patchthread <URL>` - Setzt Thread für Kommentar-Monitoring
• `!patchthread` - Zeigt aktuellen Thread
• `!patchcomment` - Testet Kommentar-Monitoring

**Status Commands:**
• `!patchstatus` - Zeigt Bot-Status und Statistiken
• `!patchhelp` - Diese Hilfe anzeigen

**Features:**
• **Vollautomatisch:** Haupt-Patches + Kommentare
• **Dual-Monitoring:** Forum + Steam Content-Extraktion
• **AI-Übersetzung:** Perplexity AI mit Discord-Markdown
• **Intelligente Bild-Unterstützung:** Automatische Steam-Bild-Konvertierung
• **Thread-Tracking:** Kommentar-Monitoring für aktuelle Patches

**Überwacht:** `https://forums.playdeadlock.com/forums/changelog.10/`
**Engine:** Kombiniert alle Funktionen von Forum KI Bot + Changelog Discord Bot"""

        await ctx.send(help_text)
    
    return bot

if __name__ == "__main__":
    # Prüfe Bot-Token
    if not PATCHNOTES_BOT_TOKEN:
        logger.error("❌ PATCHNOTES_BOT_TOKEN nicht in .env gefunden!")
        sys.exit(1)
    
    # Erstelle Bot-Instanz mit registrierten Commands
    bot = create_unified_bot()
    
    try:
        logger.info("🚀 Starte Unified Deadlock Patchnotes Bot (Complete Edition)...")
        bot.run(PATCHNOTES_BOT_TOKEN)
    except discord.LoginFailure:
        logger.error("❌ Bot-Token ungültig!")
    except Exception as e:
        logger.error(f"❌ Unerwarteter Fehler: {e}")

