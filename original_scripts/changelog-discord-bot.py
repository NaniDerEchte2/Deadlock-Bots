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

# KORRIGIERTES SYSTEM - Nur ein aktiver Thread zur Zeit
CURRENT_PATCH_THREAD = ""  # URL des aktuell überwachten Threads  
CURRENT_LAST_COMMENT_ID = ""  # Letzte verarbeitete Kommentar-ID
CURRENT_THREAD_FILE = CHANGELOG_DIR / "current_patch_thread.txt"

# Zeitbasierte Intervalle für Patch-Checks (adaptiv)
def get_comment_check_interval():
    """Dynamisches Intervall für Kommentar-Checks basierend auf Tageszeit"""
    from datetime import datetime
    import pytz
    
    try:
        german_tz = pytz.timezone('Europe/Berlin')
        now = datetime.now(german_tz)
        hour = now.hour
        
        # PRIME TIME - Sehr häufig (15 Sekunden)
        if (14 <= hour <= 17) or (18 <= hour <= 22):
            return 15
        # NORMALE ZEITEN - Mittel (45 Sekunden)  
        elif (6 <= hour <= 13) or (23 <= hour <= 23) or (0 <= hour <= 2):
            return 45
        # NACHT - Selten (2 Minuten)
        else:
            return 120
    except:
        return 30  # Fallback

def get_patch_check_interval():
    """Gibt zeitbasiertes Intervall für Patch-Checks zurück basierend auf aktueller Uhrzeit"""
    from datetime import datetime
    import pytz
    
    try:
        # Deutsche Zeit
        german_tz = pytz.timezone('Europe/Berlin')
        now = datetime.now(german_tz)
        hour = now.hour
        
        # PRIME TIME - SEHR HÄUFIG (5 Sekunden):
        # Valve postet oft zwischen 18:00-22:00 deutscher Zeit (Abends in USA)
        # Auch gerne nachmittags 14:00-17:00 (Morgens in USA)
        if (14 <= hour <= 17) or (18 <= hour <= 22):
            return 5  # Sehr aggressiv für große Updates
        
        # NORMALE ARBEITSZEITEN (30 Sekunden):
        # Früher Morgen, später Abend - mögliche Hotfixes
        elif (6 <= hour <= 13) or (23 <= hour <= 23) or (0 <= hour <= 2):
            return 30
        
        # GANZ UNTYPISCHE ZEITEN (3 Minuten):
        # Tiefe Nacht - nur für Notfall-Patches
        else:  # (3 <= hour <= 5)
            return 180
            
    except Exception as e:
        logger.error(f"Fehler bei Zeitberechnung: {e}")
        return 60  # Fallback: 1 Minute

# Bot-Konfiguration
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Globale Variablen für Background Tasks
patch_monitor_task = None
comment_monitor_task = None

# Erstelle Standard-Dateien falls nicht vorhanden
def create_default_files():
    files_to_create = [
        (OUTPUT_FILE, ""),
        (BOT_LOG_FILE, ""),
        (PROCESSED_FILE, ""),
        (CURRENT_THREAD_FILE, "")
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

def load_current_patch_thread():
    """Lädt den aktuell überwachten Thread"""
    global CURRENT_PATCH_THREAD, CURRENT_LAST_COMMENT_ID
    try:
        if CURRENT_THREAD_FILE.exists():
            with open(CURRENT_THREAD_FILE, 'r', encoding='utf-8') as f:
                line = f.read().strip()
                if '|' in line:
                    CURRENT_PATCH_THREAD, CURRENT_LAST_COMMENT_ID = line.split('|', 1)
                    logger.info(f"Aktueller überwachter Thread: {CURRENT_PATCH_THREAD} (letzte ID: {CURRENT_LAST_COMMENT_ID})")
                elif line:
                    CURRENT_PATCH_THREAD = line
                    CURRENT_LAST_COMMENT_ID = ""
                    logger.info(f"Aktueller überwachter Thread: {CURRENT_PATCH_THREAD}")
    except Exception as e:
        logger.error(f"Fehler beim Laden des aktuellen Threads: {e}")
        CURRENT_PATCH_THREAD = ""
        CURRENT_LAST_COMMENT_ID = ""

def save_current_patch_thread():
    """Speichert den aktuell überwachten Thread"""
    try:
        with open(CURRENT_THREAD_FILE, 'w', encoding='utf-8') as f:
            if CURRENT_PATCH_THREAD:
                f.write(f"{CURRENT_PATCH_THREAD}|{CURRENT_LAST_COMMENT_ID}")
    except Exception as e:
        logger.error(f"Fehler beim Speichern des aktuellen Threads: {e}")

def set_current_patch_thread(thread_url, last_comment_id=""):
    """Setzt einen neuen aktuellen Patch-Thread (überschreibt den alten)"""
    global CURRENT_PATCH_THREAD, CURRENT_LAST_COMMENT_ID
    CURRENT_PATCH_THREAD = thread_url
    CURRENT_LAST_COMMENT_ID = last_comment_id
    save_current_patch_thread()
    logger.info(f"Neuer aktueller Thread gesetzt: {thread_url}")

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
    """Findet den neuesten Patch-Thread im Forum (WAHRSCHEINLICHKEITS-PRINZIP)"""
    try:
        base_url = "https://forums.playdeadlock.com/forums/changelog.10/"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(base_url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Erweiterte Patch-Pattern mit Wahrscheinlichkeits-Gewichtung
        patch_patterns = [
            (r'\d{2}-\d{2}-\d{4}\s*update', 10),        # "07-29-2025 update" - höchste Priorität
            (r'\d{1,2}/\d{1,2}/\d{4}\s*update', 9),     # "7/29/2024 update"
            (r'patch\s*notes', 8),                       # "patch notes"
            (r'update\s*\d+', 7),                        # "update 123"
            (r'hotfix', 6),                              # "hotfix"
            (r'\d{4}-\d{2}-\d{2}', 5),                  # "2024-07-29"
            (r'balance\s*changes', 4),                   # "balance changes"
            (r'game\s*update', 3),                       # "game update"
        ]
        
        threads = soup.find_all('div', class_='structItem-title')
        candidates = []
        
        for thread in threads:
            link = thread.find('a')
            if link:
                thread_title = link.get_text().strip().lower()
                thread_url = "https://forums.playdeadlock.com" + link.get('href', '')
                
                # Berechne Wahrscheinlichkeits-Score
                total_score = 0
                for pattern, weight in patch_patterns:
                    if re.search(pattern, thread_title, re.IGNORECASE):
                        total_score += weight
                
                if total_score > 0:
                    candidates.append((total_score, thread_url, thread_title))
        
        if candidates:
            # Sortiere nach Score (höchster zuerst)
            candidates.sort(key=lambda x: x[0], reverse=True)
            best_score, best_url, best_title = candidates[0]
            logger.info(f"Bester Patch-Kandidat gefunden (Score: {best_score}): {best_title} -> {best_url}")
            return best_url, best_title
        
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

async def check_for_new_main_patches():
    """Sucht nach neuen HAUPTPATCHES (zeitbasiert adaptiv)"""
    global CURRENT_PATCH_THREAD
    
    latest_thread_url, latest_thread_title = get_latest_patch_thread()
    
    if latest_thread_url and latest_thread_url != CURRENT_PATCH_THREAD:
        logger.info(f"🆕 NEUER HAUPTPATCH GEFUNDEN: {latest_thread_title}")
        
        # Extrahiere Hauptpost
        comments = extract_forum_thread_comments(latest_thread_url)
        if comments:
            main_post = comments[0]  # Erster Post = Hauptpatch
            
            # NEUE INTELLIGENTE STRATEGIE: Analysiere Forum-Content zuerst
            logger.info("🔍 Analysiere Forum-Post Struktur...")
            
            # Extrahiere rohen Forum-Content
            raw_forum_content = main_post['content']
            raw_forum_content = re.sub(r'^Yoshi\nValve Developer\n.*?#\d+\n', '', raw_forum_content, flags=re.MULTILINE | re.DOTALL)
            raw_forum_content = re.sub(r'\nReactions:.*$', '', raw_forum_content, flags=re.MULTILINE | re.DOTALL)
            clean_forum_content = raw_forum_content.strip()
            
            # SICHERE LINK-POST ERKENNUNG (nur für Deadlock Steam-Links!)
            word_count = len(clean_forum_content.split())
            
            # Erkenne verschiedene Link-Typen
            has_steam_deadlock_link = bool(re.search(r'store\.steampowered\.com/news/app/1422450', raw_forum_content, re.IGNORECASE))
            has_steam_general_link = bool(re.search(r'steampowered\.com', raw_forum_content, re.IGNORECASE))
            has_other_links = bool(re.search(r'https?://(?!.*steampowered\.com)', raw_forum_content, re.IGNORECASE))
            
            # Sehr restriktive Bedingungen für Link-Only Erkennung
            is_safe_link_post = (
                word_count < 25 and  # Sehr wenige Wörter
                has_steam_deadlock_link and  # NUR Deadlock Steam-Links
                not has_other_links  # KEINE anderen Links
            )
            
            steam_url = extract_steam_url_from_forum(latest_thread_url)
            final_content = None
            content_source = "Forum"
            
            # PRIORITÄT 1: NUR bei sicheren Deadlock Steam-Link-Posts
            if is_safe_link_post and steam_url and "1422450" in steam_url:
                logger.info(f"🔗 SICHERER DEADLOCK-LINK-POST erkannt: {word_count} Wörter, nur Deadlock Steam-Link")
                logger.info(f"🎮 Extrahiere von verifiziertem Deadlock Steam-Link: {steam_url}")
                steam_content = extract_steam_news_content(steam_url)
                if steam_content and len(steam_content) > 100:
                    final_content = steam_content
                    content_source = "Steam"
                    logger.info("✅ Steam-Inhalt für sicheren Link-Post erfolgreich extrahiert!")
                else:
                    logger.warning("⚠️ Steam-Content fehlgeschlagen, verwende minimalen Forum-Text")
                    final_content = clean_forum_content
            
            # PRIORITÄT 2: Normale Posts mit Steam-Links (nur Deadlock!)
            elif steam_url and "1422450" in steam_url and word_count >= 25:
                logger.info(f"📝 NORMALER POST mit Deadlock Steam-Link - Vergleiche Content-Qualität")
                logger.info(f"🔍 Weitere Sicherheitsprüfung: Deadlock App-ID in URL verifiziert")
                steam_content = extract_steam_news_content(steam_url)
                
                if steam_content and len(steam_content) > len(clean_forum_content) * 2:
                    logger.info(f"🎮 Steam-Content deutlich umfangreicher ({len(steam_content)} vs {len(clean_forum_content)} Zeichen)")
                    final_content = steam_content
                    content_source = "Steam"
                else:
                    logger.info(f"📋 Forum-Content ausreichend - verwende Forum")
                    final_content = clean_forum_content
            
            # PRIORITÄT 2b: Steam-Links für andere Apps → Ignorieren
            elif steam_url and "1422450" not in steam_url:
                logger.warning(f"⚠️ Steam-Link gefunden, aber NICHT für Deadlock - ignoriere: {steam_url}")
                logger.info("📋 Verwende Forum-Content für Nicht-Deadlock Steam-Link")
                final_content = clean_forum_content
            
            # PRIORITÄT 3: Nur Forum-Content
            else:
                logger.info("📋 Nur Forum-Content verfügbar")
                final_content = clean_forum_content
            
            # SICHERHEIT: Fallback bei leerem Content
            if not final_content or len(final_content.strip()) < 10:
                logger.warning("⚠️ Content zu kurz - verwende Titel als Fallback")
                final_content = f"Update verfügbar: {latest_thread_title}\n\nDetails siehe Forum oder Steam."
            
            # Erstelle Changelog mit dem besten verfügbaren Inhalt
            changelog_content = f"### Deadlock Patch Notes\n\n"
            changelog_content += f"**{latest_thread_title}**\n"
            if content_source == "Steam":
                changelog_content += f"*(Übersetzt von der Steam-Seite)*\n\n"
            else:
                changelog_content += f"\n"
            
            changelog_content += f"{final_content}\n\n"
            
            # KI-Übersetzung
            logger.info(f"🤖 Übersetze {content_source}-Inhalt mit KI...")
            translated_content = await translate_with_ai(changelog_content)
            
            if translated_content:
                changelog_content = translated_content
                logger.info("✅ KI-Übersetzung erfolgreich!")
            else:
                logger.warning("⚠️ KI-Übersetzung fehlgeschlagen, verwende Original")
            
            # Stelle sicher, dass die Mention am Ende steht
            changelog_content = ensure_mention_at_end(changelog_content)
            
            # Schreibe in ausgabe.txt für automatisches Posting
            with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
                f.write(changelog_content)
            
            # SETZE NEUEN AKTUELLEN THREAD (überschreibt alten!)
            set_current_patch_thread(latest_thread_url, main_post['id'])
            
            logger.info(f"🎉 Neuer Hauptpatch von {content_source} verarbeitet und Thread-Überwachung umgestellt!")

async def check_current_thread_comments():
    """Überprüft den aktuellen Thread auf neue Kommentare (alle 30 Sekunden)"""
    global CURRENT_PATCH_THREAD, CURRENT_LAST_COMMENT_ID
    
    if not CURRENT_PATCH_THREAD:
        return
    
    try:
        
        # Extrahiere alle Kommentare
        comments = extract_forum_thread_comments(CURRENT_PATCH_THREAD)
        
        if not comments:
            return
        
        # Finde neue Kommentare
        new_comments = []
        for comment in comments:
            if not CURRENT_LAST_COMMENT_ID or comment['id'] > CURRENT_LAST_COMMENT_ID:
                new_comments.append(comment)
        
        if new_comments:
            logger.info(f"🆕 {len(new_comments)} neue Kommentare gefunden!")
            
            # Verarbeite nur den NEUESTEN Kommentar
            latest_comment = new_comments[-1]
            
            # Priorität 1: Versuche Steam-Inhalt zu finden
            logger.info("🔍 Suche nach Steam-Link im neuen Kommentar...")
            steam_url = extract_steam_url_from_forum(CURRENT_PATCH_THREAD)
            
            final_content = None
            content_source = "Forum"
            
            if steam_url:
                logger.info(f"🎮 Steam-Link gefunden: {steam_url}")
                steam_content = extract_steam_news_content(steam_url)
                if steam_content and len(steam_content) > 200:
                    final_content = steam_content
                    content_source = "Steam"
                    logger.info("✅ Steam-Inhalt erfolgreich extrahiert - wird für Kommentar verwendet!")
                else:
                    logger.warning("⚠️ Steam-Inhalt zu kurz oder nicht gefunden, verwende Forum-Kommentar")
            
            # Fallback: Verwende Forum-Kommentar
            if not final_content:
                logger.info("📋 Verwende Forum-Kommentar als Fallback")
                content = latest_comment['content']
                content = re.sub(r'^Yoshi\nValve Developer\n.*?#\d+\n', '', content, flags=re.MULTILINE | re.DOTALL)
                content = re.sub(r'\nReactions:.*$', '', content, flags=re.MULTILINE | re.DOTALL)
                final_content = content.strip()
            
            # Erstelle Changelog für den neuen Kommentar
            changelog_content = f"### Deadlock Patch Notes Update\n\n"
            changelog_content += f"**Neuer Kommentar** - Veröffentlicht am {latest_comment['formatted_time']}\n"
            if content_source == "Steam":
                changelog_content += f"*(Übersetzt von der Steam-Seite)*\n\n"
            else:
                changelog_content += f"\n"
            
            changelog_content += f"{final_content}\n\n"
            
            # KI-Übersetzung
            logger.info(f"🤖 Übersetze neuen {content_source}-Kommentar mit KI...")
            translated_content = await translate_with_ai(changelog_content)
            
            if translated_content:
                changelog_content = translated_content
                logger.info("✅ KI-Übersetzung erfolgreich!")
            else:
                logger.warning("⚠️ KI-Übersetzung fehlgeschlagen, verwende Original")
            
            # Stelle sicher, dass die Mention am Ende steht
            changelog_content = ensure_mention_at_end(changelog_content)
            
            # Schreibe in ausgabe.txt für automatisches Posting
            with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
                f.write(changelog_content)
            
            # Update last processed ID
            CURRENT_LAST_COMMENT_ID = latest_comment['id']
            save_current_patch_thread()
            
            logger.info(f"🎉 Neuer {content_source}-Kommentar verarbeitet: {latest_comment['id']}")
        
    except Exception as e:
        logger.error(f"Fehler beim Verarbeiten von Kommentaren: {e}")

async def patch_monitoring_loop():
    """Kontinuierliche Suche nach neuen HAUPTPATCHES (zeitbasiert adaptiv)"""
    logger.info("📡 Hauptpatch-Überwachung gestartet (zeitbasiert adaptiv)")
    
    while True:
        try:
            # Berechne aktuelles Intervall basierend auf Tageszeit
            current_interval = get_patch_check_interval()
            
            await check_for_new_main_patches()
            await asyncio.sleep(current_interval)
        except Exception as e:
            logger.error(f"Fehler in der Hauptpatch-Überwachung: {e}")
            await asyncio.sleep(60)  # Bei Fehlern kürzer warten

async def comment_monitoring_loop():
    """Kontinuierliche Überwachung von Kommentaren im aktuellen Thread (dynamisches Intervall)"""
    logger.info("💬 Kommentar-Überwachung gestartet (dynamisches Intervall basierend auf Tageszeit)")
    
    while True:
        try:
            current_interval = get_comment_check_interval()
            
            await check_current_thread_comments()
            await asyncio.sleep(current_interval)
        except Exception as e:
            logger.error(f"Fehler in der Kommentar-Überwachung: {e}")
            await asyncio.sleep(30)  # Bei Fehlern festes Intervall

# Alles andere bleibt gleich...
def extract_steam_ids_from_url(url):
    """Extrahiert App-ID und News-ID aus einer Steam-URL"""
    app_id_match = re.search(r'app/(\d+)', url)
    news_id_match = re.search(r'view/(\d+)', url)
    
    app_id = app_id_match.group(1) if app_id_match else None
    news_id = news_id_match.group(1) if news_id_match else None
    
    return app_id, news_id

def get_steam_news_via_api(app_id, news_id=None):
    """Verbesserte Funktion zum Abrufen von Steam-News über die API für Deadlock"""
    try:
        api_url = f"http://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/?appid={app_id}&count=5&maxlength=0&format=json&feeds=steam_community_announcements"
        
        response = requests.get(api_url, timeout=10)
        response.raise_for_status()
        
        data = response.json()
        
        if 'appnews' in data and 'newsitems' in data['appnews']:
            news_items = data['appnews']['newsitems']
            
            if news_id:
                for item in news_items:
                    if str(news_id) in item.get('url', ''):
                        return clean_content(item.get('contents', ''))
            
            # Suche nach dem neuesten Patch/Update-Post
            for item in news_items:
                title = item.get('title', '').lower()
                contents = item.get('contents', '')
                
                # Priorisiere Patch Notes und Updates
                if any(keyword in title for keyword in ['update', 'patch', 'notes', 'changelog', 'balance']):
                    if len(contents) > 200:  # Stelle sicher, dass genug Inhalt vorhanden ist
                        logger.info(f"📋 Steam-News gefunden: {item.get('title', 'Unbekannt')}")
                        return clean_content(contents)
            
            # Fallback: Nimm den ersten verfügbaren Inhalt
            if news_items and len(news_items[0].get('contents', '')) > 200:
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
        
        # Suche nach Steam-Links in normalen <a> Tags
        for link in soup.find_all('a', href=re.compile(r'store\.steampowered\.com|steamcommunity\.com')):
            steam_links.append(link.get('href'))
        
        # Suche nach Steam-Links in BBCode-Embeddings
        for embed in soup.find_all(class_=re.compile(r'bbCodeBlock-(steam|media)')):
            links = embed.find_all('a')
            for link in links:
                if 'steampowered.com' in link.get('href', '') or 'steamcommunity.com' in link.get('href', ''):
                    steam_links.append(link.get('href'))
        
        # Hardcoded Deadlock Steam-Seite als Fallback - immer verfügbar!
        if not steam_links:
            deadlock_app_id = "1422450"  # Deadlock App ID
            logger.info("🔧 Kein Steam-Link im Forum gefunden, verwende Deadlock Steam-News direkt")
            return f"https://store.steampowered.com/news/app/{deadlock_app_id}/"
        
        # Priorisiere News-Links
        for link in steam_links:
            if 'news' in link.lower() or 'announcement' in link.lower():
                return link
        
        return steam_links[0]

    except Exception as e:
        logger.error(f"Fehler beim Extrahieren der Steam-URL: {e}")
        # Fallback: Immer die Deadlock Steam-News-Seite verwenden
        return "https://store.steampowered.com/news/app/1422450/"

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

@bot.command(name="set_current")
async def set_current(ctx, url: str, *, last_comment_id: str = ""):
    """Setzt einen neuen aktuellen Thread zur Überwachung"""
    if ctx.channel.id != LOG_CHANNEL_ID:
        await ctx.send("Bitte nutze diesen Befehl nur im Log-/Testkanal.")
        return
    
    if not url.startswith(("http://", "https://")):
        await ctx.send("❌ Ungültiger URL-Format. Bitte gib eine vollständige URL an.")
        return
    
    if "playdeadlock.com" not in url:
        await ctx.send("❌ URL muss von playdeadlock.com sein.")
        return
    
    set_current_patch_thread(url, last_comment_id)
    await ctx.send(f"✅ Neuer aktueller Thread gesetzt: {url}")
    if last_comment_id:
        await ctx.send(f"Letzte verarbeitete ID: {last_comment_id}")

@bot.command(name="status")
async def show_status(ctx):
    """Zeigt den aktuellen Überwachungsstatus"""
    if ctx.channel.id != LOG_CHANNEL_ID:
        await ctx.send("Bitte nutze diesen Befehl nur im Log-/Testkanal.")
        return
    
    # Aktuelle Intervalle berechnen
    current_patch_interval = get_patch_check_interval()
    current_comment_interval = get_comment_check_interval()
    
    await ctx.send(f"**🚀 Intelligenter Überwachungsstatus:**")
    if CURRENT_PATCH_THREAD:
        await ctx.send(f"🔗 Thread: {CURRENT_PATCH_THREAD}")
        await ctx.send(f"🆔 Letzte ID: {CURRENT_LAST_COMMENT_ID}")
        
        # Zeige dynamische Intervalle
        if current_patch_interval == 5:
            await ctx.send(f"🔥 Patch-Check: alle **5 Sekunden** (PRIME TIME!)")
        elif current_patch_interval == 30:
            await ctx.send(f"⏰ Patch-Check: alle **30 Sekunden** (normale Zeit)")
        else:
            await ctx.send(f"💤 Patch-Check: alle **3 Minuten** (tiefe Nacht)")
            
        if current_comment_interval == 15:
            await ctx.send(f"💬 Kommentar-Check: alle **15 Sekunden** (AKTIV)")
        elif current_comment_interval == 45:
            await ctx.send(f"💬 Kommentar-Check: alle **45 Sekunden** (normal)")
        else:
            await ctx.send(f"💬 Kommentar-Check: alle **2 Minuten** (nacht)")
            
        await ctx.send(f"🤖 **Intelligente Priorität:** Steam für große Updates, Forum für Hotfixes")
    else:
        await ctx.send("❌ Kein Thread wird aktuell überwacht")

@bot.command(name="check_patches")
async def manual_patch_check(ctx):
    """Manueller Check nach neuen Patches"""
    if ctx.channel.id != LOG_CHANNEL_ID:
        await ctx.send("Bitte nutze diesen Befehl nur im Log-/Testkanal.")
        return
    
    await ctx.send("🔍 Suche nach neuen Patch-Threads...")
    await check_for_new_main_patches()
    await ctx.send("✅ Patch-Check abgeschlossen!")

@bot.command(name="check_comments")
async def manual_comment_check(ctx):
    """Manueller Check nach neuen Kommentaren im aktuellen Thread"""
    if ctx.channel.id != LOG_CHANNEL_ID:
        await ctx.send("Bitte nutze diesen Befehl nur im Log-/Testkanal.")
        return
    
    if not CURRENT_PATCH_THREAD:
        await ctx.send("❌ Kein aktueller Thread zur Überwachung!")
        return
    
    await ctx.send(f"🔍 Überprüfe Kommentare in aktuellem Thread...")
    await ctx.send(f"Thread: {CURRENT_PATCH_THREAD}")
    await ctx.send(f"Letzte ID: {CURRENT_LAST_COMMENT_ID}")
    
    await check_current_thread_comments()
    await ctx.send("✅ Kommentar-Check abgeschlossen!")

@bot.command(name="test_steam")
async def test_steam_extraction(ctx):
    """Testet die Steam-Extraktion für Deadlock direkt"""
    if ctx.channel.id != LOG_CHANNEL_ID:
        await ctx.send("Bitte nutze diesen Befehl nur im Log-/Testkanal.")
        return
    
    await ctx.send("🎮 Teste Steam-Extraktion für Deadlock...")
    
    # Teste direkte Steam-News-Extraktion
    deadlock_app_id = "1422450"
    steam_url = f"https://store.steampowered.com/news/app/{deadlock_app_id}/"
    
    await ctx.send(f"📋 Extrahiere von: {steam_url}")
    
    steam_content = extract_steam_news_content(steam_url)
    
    if steam_content and len(steam_content) > 200:
        await ctx.send(f"✅ Steam-Inhalt erfolgreich extrahiert ({len(steam_content)} Zeichen)")
        await ctx.send("🤖 Sende zur KI-Übersetzung...")
        
        # Erstelle Changelog-Format
        changelog_content = f"### Deadlock Patch Notes\n\n"
        changelog_content += f"**Steam Update** - *(Direkt von Steam extrahiert)*\n\n"
        changelog_content += f"{steam_content}\n\n"
        
        # KI-Übersetzung
        translated_content = await translate_with_ai(changelog_content)
        
        if translated_content:
            translated_content = ensure_mention_at_end(translated_content)
            chunks = split_content_intelligently(translated_content, MAX_MESSAGE_LENGTH)
            
            await ctx.send("✅ Steam-Übersetzung abgeschlossen:")
            for chunk in chunks:
                await ctx.send(chunk)
                await asyncio.sleep(0.7)
        else:
            await ctx.send("❌ KI-Übersetzung fehlgeschlagen")
    else:
        await ctx.send("❌ Konnte keinen Steam-Inhalt extrahieren oder Inhalt zu kurz")

@bot.command(name="test_link_detection")
async def test_link_detection(ctx, url: str = None):
    """Testet die neue Link-Post-Erkennung mit der gestrigen URL"""
    if ctx.channel.id != LOG_CHANNEL_ID:
        await ctx.send("Bitte nutze diesen Befehl nur im Log-/Testkanal.")
        return
    
    # Verwende gestrige URL als Standard
    test_url = url or "https://forums.playdeadlock.com/threads/08-18-2025-update.75046/"
    
    await ctx.send(f"🔍 Teste Link-Post-Erkennung für: {test_url}")
    
    try:
        # Simuliere die neue Logik
        comments = extract_forum_thread_comments(test_url)
        if not comments:
            await ctx.send("❌ Keine Kommentare gefunden")
            return
            
        main_post = comments[0]
        
        # Analysiere Content wie im Bot
        raw_content = main_post['content']
        raw_content = re.sub(r'^Yoshi\nValve Developer\n.*?#\d+\n', '', raw_content, flags=re.MULTILINE | re.DOTALL)
        raw_content = re.sub(r'\nReactions:.*$', '', raw_content, flags=re.MULTILINE | re.DOTALL)
        clean_content = raw_content.strip()
        
        word_count = len(clean_content.split())
        
        # Angewendete Sicherheitschecks
        has_steam_deadlock_link = bool(re.search(r'store\.steampowered\.com/news/app/1422450', raw_content, re.IGNORECASE))
        has_steam_general_link = bool(re.search(r'steampowered\.com', raw_content, re.IGNORECASE))
        has_other_links = bool(re.search(r'https?://(?!.*steampowered\.com)', raw_content, re.IGNORECASE))
        
        is_safe_link_post = (
            word_count < 25 and
            has_steam_deadlock_link and
            not has_other_links
        )
        
        await ctx.send(f"📊 **SICHERE ANALYSE:**")
        await ctx.send(f"- Wort-Anzahl: {word_count}")
        await ctx.send(f"- Deadlock Steam-Link: {'✅ JA' if has_steam_deadlock_link else '❌ NEIN'}")
        await ctx.send(f"- Andere Steam-Links: {'⚠️ JA' if has_steam_general_link and not has_steam_deadlock_link else '❌ NEIN'}")
        await ctx.send(f"- Andere Links: {'⚠️ JA' if has_other_links else '✅ NEIN'}")
        await ctx.send(f"- **SICHERE Link-Post Erkennung**: {'✅ JA' if is_safe_link_post else '❌ NEIN'}")
        await ctx.send(f"- Forum-Content: `{clean_content[:100]}...`")
        
        if is_safe_link_post:
            await ctx.send("🎯 **ERGEBNIS: SICHERER Link-Post - würde Steam-Content verwenden!**")
            
            steam_url = extract_steam_url_from_forum(test_url)
            if steam_url:
                await ctx.send(f"🔗 Steam-URL: {steam_url}")
                steam_content = extract_steam_news_content(steam_url)
                if steam_content:
                    await ctx.send(f"✅ Steam-Content verfügbar: {len(steam_content)} Zeichen")
                else:
                    await ctx.send("❌ Steam-Content nicht extrahierbar")
        else:
            await ctx.send("📝 **ERGEBNIS: Normaler Post - würde Forum-Content verwenden**")
            
    except Exception as e:
        await ctx.send(f"❌ Fehler beim Testen: {e}")

@bot.event
async def on_ready():
    global patch_monitor_task, comment_monitor_task
    
    logger.info(f'{bot.user} hat sich bei Discord angemeldet!')
    
    # Lade aktuellen überwachten Thread
    load_current_patch_thread()
    
    # Falls kein Thread gesetzt ist, setze 07-29 als Standard (temporär)
    if not CURRENT_PATCH_THREAD:
        july_29_thread = "https://forums.playdeadlock.com/threads/07-29-2025-update.72760/"
        set_current_patch_thread(july_29_thread, "post-141214")  # Setze als aktuell
        logger.info("🔄 07-29 Thread als Standard gesetzt (bis neuer Patch gefunden wird)")
    
    # Starte ZWEI separate Monitoring-Loops
    patch_monitor_task = bot.loop.create_task(patch_monitoring_loop())      # Hauptpatches (5 Min)
    comment_monitor_task = bot.loop.create_task(comment_monitoring_loop())   # Kommentare (30s)
    
    await check_bot_logs()
    
    # Datei-Überwachung für ausgabe.txt
    event_handler = ThreadSafeUpdateHandler(bot, CHANNEL_ID, OUTPUT_FILE)
    observer = Observer()
    observer.schedule(event_handler, path=str(CHANGELOG_DIR), recursive=False)
    observer.start()
    logger.info(f"Überwache Verzeichnis: {CHANGELOG_DIR}")

bot.run(TOKEN)