import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime
import json
from openai import OpenAI
import os
import sys
import time
import logging
import traceback
from pathlib import Path
import hashlib

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
        print(f"Verzeichnis erstellt/ueberprueft: {directory}")

# Beim Start ausführen
ensure_directories()

# Konfiguration
BASE_URL = "https://forums.playdeadlock.com"
CHANGELOG_URL = f"{BASE_URL}/forums/changelog.10/"
LAST_PROCESSED_FILE = CHANGELOG_DIR / "last_processed_changelog.json"
PROCESSED_STEAM_LINKS_FILE = CHANGELOG_DIR / "processed_steam_links.json"
OUTPUT_FILE = CHANGELOG_DIR / "ausgabe.txt"
LOG_FILE = LOGS_DIR / "bot_logs.txt"
DISCORD_LOG_CHANNEL_ID = 1374364800817303632

# Test/Debug-Modus - umgeht Duplikat-Erkennung wenn True
DEBUG_MODE = "--debug" in sys.argv or "--test" in sys.argv

PERPLEXITY_API_KEY = "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"

# Standard-Dateien erstellen falls nicht vorhanden
def create_default_files():
    files_to_create = [
        (LAST_PROCESSED_FILE, "{}"),
        (PROCESSED_STEAM_LINKS_FILE, "[]"),
        (OUTPUT_FILE, ""),
        (LOG_FILE, "")
    ]
    
    for file_path, default_content in files_to_create:
        if not file_path.exists():
            file_path.write_text(default_content, encoding='utf-8')
            print(f"Datei {file_path} erstellt")

create_default_files()

# Logging konfigurieren
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('DeadlockChangelogBot')

# Wahrscheinlichkeiten der Tage in Prozent
prob_days = {
    'Monday': 9.5,
    'Tuesday': 6.8,
    'Wednesday': 9.5,
    'Thursday': 17.6,
    'Friday': 31.8,
    'Saturday': 18.2,
    'Sunday': 6.8
}

# Wahrscheinlichkeiten der Uhrzeiten in Prozent
prob_hours = {
    '00': 10.8, '01': 12.2, '02': 6.8, '03': 6.8, '04': 1.4, '05': 4.1, '06': 2.0, 
    '07': 0, '08': 0.7, '09': 2.7, '10': 0.7, '11': 0, '12': 0, '13': 0, '14': 0, 
    '15': 0, '16': 2.0, '17': 3.4, '18': 2.0, '19': 5.4, '20': 0, '21': 15.5, 
    '22': 8.1, '23': 12.2
}

def send_discord_log(message, is_error=False):
    """Sendet eine Lognachricht an den Discord-Log-Kanal"""
    try:
        log_level = "ERROR" if is_error else "INFO"
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{timestamp}] [{log_level}] {message}\n")
        
        if is_error:
            logger.error(message)
        else:
            logger.info(message)
    except Exception as e:
        logger.error(f"Fehler beim Senden des Discord-Logs: {e}")

def get_check_interval():
    """Bestimmt das Überprüfungsintervall basierend auf Tag und Uhrzeit"""
    now = datetime.now()
    current_day = now.strftime('%A')
    current_hour = now.strftime('%H')
    
    day_prob = prob_days.get(current_day, 0)
    hour_prob = prob_hours.get(current_hour, 0)
    
    combined_prob = (day_prob * 0.4) + (hour_prob * 0.6)
    
    if combined_prob > 15:
        return 1
    elif combined_prob > 10:
        return 5
    elif combined_prob > 7:
        return 30
    elif combined_prob > 5:
        return 60
    elif combined_prob > 3:
        return 5 * 60
    elif combined_prob > 1:
        return 15 * 60
    else:
        return 30 * 60

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
                        return clean_steam_content(item.get('contents', ''))
            
            if news_items:
                return clean_steam_content(news_items[0].get('contents', ''))
        
        return None
    except Exception as e:
        send_discord_log(f"Fehler beim Abrufen der Steam-News via API: {e}", is_error=True)
        return None

def clean_steam_content(content):
    """Bereinigt den Steam-Inhalt von doppelten Leerzeilen"""
    if not content:
        return content
    
    content = re.sub(r'\[img\].*?\[/img\]', '', content)
    content = re.sub(r'\[url=.*?\](.*?)\[/url\]', r'\1', content)
    content = re.sub(r'\[.*?\]', '', content)
    content = re.sub(r'@[^\s]+', '', content)
    content = re.sub(r'\n\s*\n', '\n', content)
    content = content.strip()
    
    return content

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
            
            return clean_steam_content(f"{title}\n\n{description}")
        
        return None
    except Exception as e:
        send_discord_log(f"Fehler beim Abrufen des Steam RSS-Feeds: {e}", is_error=True)
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
        send_discord_log(f"Fehler beim Extrahieren der Steam-URL: {e}", is_error=True)
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
                    return clean_steam_content(text)
        
        for id_pattern in ['news_content', 'announcement_content', 'newsPost', 'bodyContents']:
            main_content = soup.find('div', id=re.compile(f".*{id_pattern}.*"))
            if main_content:
                text = main_content.get_text(separator='\n', strip=True)
                if text and len(text) > 100:
                    return clean_steam_content(text)
        
        for div in soup.find_all('div'):
            if div.has_attr('data-panel') and 'news' in div.get('data-panel', ''):
                text = div.get_text(separator='\n', strip=True)
                if text and len(text) > 100:
                    return clean_steam_content(text)
        
        text_blocks = []
        for div in soup.find_all('div'):
            text = div.get_text(separator='\n', strip=True)
            if len(text) > 200:
                text_blocks.append((len(text), text))
        
        if text_blocks:
            text_blocks.sort(reverse=True)
            
            for _, text in text_blocks:
                if not re.search(r'(Home|Store|Community|About|Support).*(Home|Store|Community|About|Support)', text):
                    return clean_steam_content(text)
            
            return clean_steam_content(text_blocks[0][1])
        
        return None

    except Exception as e:
        send_discord_log(f"Fehler beim Extrahieren des Steam-Inhalts: {e}", is_error=True)
        return None

def get_latest_changelog_url():
    """Holt die URL des neuesten Changelog-Eintrags"""
    max_retries = 3
    retry_delay = 5
    
    for attempt in range(max_retries):
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
                entry_text = latest_entry.text.strip()
                
                full_url = BASE_URL + entry_link if not entry_link.startswith('http') else entry_link
                return full_url
            else:
                return None
                
        except requests.exceptions.RequestException as e:
            if "timeout" in str(e).lower():
                send_discord_log(f"Timeout beim Abrufen des Changelogs (Versuch {attempt+1}/{max_retries}): {e}", is_error=True)
            elif attempt == max_retries - 1:
                send_discord_log(f"Netzwerkfehler beim Abrufen des Changelogs nach {max_retries} Versuchen: {e}", is_error=True)
            
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                return None
        except Exception as e:
            send_discord_log(f"Unerwarteter Fehler beim Extrahieren des Changelogs: {e}", is_error=True)
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
                    # Prüfe ob Steam-Link bereits verarbeitet wurde
                    if is_steam_link_processed(steam_url):
                        send_discord_log(f"🔄 Steam-Link übersprungen (bereits verarbeitet): {steam_url}")
                        return clean_steam_content(content_text)
                    
                    if len(content_text.split()) < 50:
                        send_discord_log(f"Forum-Beitrag verweist auf Steam-Seite: {steam_url}")
                        steam_content = extract_steam_news_content(steam_url)
                        if steam_content and len(steam_content) > len(content_text):
                            # Speichere Steam-Link als verarbeitet
                            save_processed_steam_link(steam_url, url)
                            return steam_content
                    else:
                        steam_content = extract_steam_news_content(steam_url)
                        if steam_content and len(steam_content) > 200:
                            combined_content = f"{content_text}\n\n--- Steam-Inhalt ---\n\n{steam_content}"
                            # Speichere Steam-Link als verarbeitet
                            save_processed_steam_link(steam_url, url)
                            return clean_steam_content(combined_content)
                
                return clean_steam_content(content_text)
        
        send_discord_log(f"Kein Hauptinhalt mit bekannten Selektoren gefunden: {url}", is_error=True)
        
        steam_url = extract_steam_url_from_forum(url)
        if steam_url:
            # Prüfe ob Steam-Link bereits verarbeitet wurde
            if is_steam_link_processed(steam_url):
                send_discord_log(f"🔄 Steam-Link übersprungen (bereits verarbeitet): {steam_url}")
                return None
            
            send_discord_log(f"Versuche Steam-Inhalt zu extrahieren: {steam_url}")
            steam_content = extract_steam_news_content(steam_url)
            if steam_content:
                # Speichere Steam-Link als verarbeitet
                save_processed_steam_link(steam_url, url)
                return steam_content
        
        return None

    except Exception as e:
        send_discord_log(f"Fehler beim Extrahieren des Forum-Inhalts: {e}", is_error=True)
        return None

def is_new_changelog(url):
    """Prüft, ob ein Changelog neu ist"""
    try:
        with open(LAST_PROCESSED_FILE, 'r') as f:
            content = f.read().strip()
            if content:
                last_processed = json.loads(content)
                return url != last_processed.get('url', '')
            return True
    except (FileNotFoundError, json.JSONDecodeError):
        return True

def save_processed_changelog(url):
    """Speichert einen verarbeiteten Changelog SOFORT"""
    with open(LAST_PROCESSED_FILE, 'w') as f:
        json.dump({'url': url, 'date': datetime.now().isoformat()}, f)

def generate_steam_url_hash(url):
    """Erstellt einen eindeutigen Hash für eine Steam-URL"""
    # Normalisiere die URL (entferne Tracking-Parameter etc.)
    normalized_url = re.sub(r'[?&]utm_.*?(&|$)', '', url)
    normalized_url = re.sub(r'[?&]source=.*?(&|$)', '', normalized_url)
    normalized_url = normalized_url.rstrip('/')
    
    return hashlib.md5(normalized_url.encode('utf-8')).hexdigest()

def is_steam_link_processed(steam_url):
    """Prüft ob eine Steam-URL bereits verarbeitet wurde"""
    if DEBUG_MODE:
        send_discord_log("🧪 DEBUG-MODUS: Übspringe Steam-Link-Duplikat-Prüfung")
        return False
    
    try:
        if not PROCESSED_STEAM_LINKS_FILE.exists():
            return False
            
        with open(PROCESSED_STEAM_LINKS_FILE, 'r', encoding='utf-8') as f:
            processed_links = json.load(f)
        
        url_hash = generate_steam_url_hash(steam_url)
        
        for entry in processed_links:
            if entry.get('hash') == url_hash:
                send_discord_log(f"🔄 Steam-Link bereits verarbeitet: {steam_url}")
                return True
        
        return False
    except (FileNotFoundError, json.JSONDecodeError):
        return False

def save_processed_steam_link(steam_url, forum_url):
    """Speichert eine verarbeitete Steam-URL"""
    if DEBUG_MODE:
        send_discord_log("🧪 DEBUG-MODUS: Steam-Link wird nicht als verarbeitet gespeichert")
        return
    
    try:
        if PROCESSED_STEAM_LINKS_FILE.exists():
            with open(PROCESSED_STEAM_LINKS_FILE, 'r', encoding='utf-8') as f:
                processed_links = json.load(f)
        else:
            processed_links = []
        
        url_hash = generate_steam_url_hash(steam_url)
        
        # Prüfe ob bereits vorhanden
        for entry in processed_links:
            if entry.get('hash') == url_hash:
                return
        
        new_entry = {
            'hash': url_hash,
            'steam_url': steam_url,
            'forum_url': forum_url,
            'date': datetime.now().isoformat()
        }
        
        processed_links.append(new_entry)
        
        with open(PROCESSED_STEAM_LINKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(processed_links, f, indent=2, ensure_ascii=False)
        
        send_discord_log(f"✅ Steam-Link als verarbeitet gespeichert: {steam_url}")
        
    except Exception as e:
        send_discord_log(f"❌ Fehler beim Speichern des Steam-Links: {e}", is_error=True)

def split_content_intelligently(content, max_length=1950):
    """Teilt den Inhalt intelligent auf, ohne Sätze oder Aufzählungen zu unterbrechen"""
    if len(content) <= max_length:
        return [content]
    
    chunks = []
    lines = content.split('\n')
    current_chunk = ""
    
    for line in lines:
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

def send_to_perplexity_api(url):
    """Sendet den Inhalt an die Perplexity API zur Übersetzung"""
    forum_content = extract_forum_content(url)
    
    if not forum_content:
        send_discord_log("Keine Inhalte zum Verarbeiten gefunden", is_error=True)
        return

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

Hier sind die Patchnotes: {forum_content}"""}
    ]
    
    try:
        response = client.chat.completions.create(
            model="sonar-pro",
            messages=messages,
        )
        
        translated_notes = response.choices[0].message.content
        
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            f.write(translated_notes)
        
        send_discord_log(f"Übersetzte Patchnotes wurden in {OUTPUT_FILE} gespeichert.")
    
    except Exception as e:
        error_msg = f"Fehler beim Senden an die Perplexity API: {e}"
        send_discord_log(error_msg, is_error=True)
        send_discord_log(traceback.format_exc(), is_error=True)

def main():
    """Hauptfunktion"""
    if DEBUG_MODE:
        send_discord_log("🧪 DEADLOCK CHANGELOG BOT GESTARTET IM DEBUG-MODUS 🧪")
        send_discord_log("Steam-Link-Duplikat-Erkennung ist DEAKTIVIERT für Tests")
    else:
        send_discord_log("Deadlock Changelog Bot gestartet mit Steam-Link-Duplikat-Schutz.")
    
    while True:
        try:
            interval = get_check_interval()
            
            now = datetime.now()
            current_day = now.strftime('%A')
            current_hour = now.strftime('%H:%M')
            
            if interval >= 1800 or now.minute % 30 == 0:
                logger.info(f"[{current_day} {current_hour}] Überprüfe nach Updates... (Intervall: {interval} Sekunden)")
            
            latest_url = get_latest_changelog_url()
            
            if latest_url and is_new_changelog(latest_url):
                send_discord_log(f"Neuer Changelog gefunden: {latest_url}")
                
                # WICHTIG: Speichere die URL SOFORT, bevor die Verarbeitung beginnt
                save_processed_changelog(latest_url)
                
                send_to_perplexity_api(latest_url)
                send_discord_log(f"Changelog verarbeitet: {latest_url}")
            
            time.sleep(interval)
            
        except Exception as e:
            error_msg = f"Unerwarteter Fehler in der Hauptschleife: {e}"
            send_discord_log(error_msg, is_error=True)
            send_discord_log(traceback.format_exc(), is_error=True)
            time.sleep(60)

def test_translate_url(url):
    """Test-Funktion zum manuellen Übersetzen einer URL"""
    send_discord_log(f"🧪 TEST-MODUS: Übersetze URL: {url}")
    
    try:
        send_to_perplexity_api(url)
        send_discord_log(f"✅ Test-Übersetzung abgeschlossen für: {url}")
        print(f"\n📄 Übersetzung wurde gespeichert in: {OUTPUT_FILE}")
        
        # Zeige die Übersetzung auch in der Konsole an
        if OUTPUT_FILE.exists():
            with open(OUTPUT_FILE, 'r', encoding='utf-8') as f:
                content = f.read()
                print("\n" + "="*50)
                print("ÜBERSETZUNGS-ERGEBNIS:")
                print("="*50)
                print(content)
                print("="*50)
                
    except Exception as e:
        send_discord_log(f"❌ Fehler bei Test-Übersetzung: {e}", is_error=True)

if __name__ == "__main__":
    # Prüfe Kommandozeilen-Argumente
    if len(sys.argv) > 1:
        if sys.argv[1] == "--translate" and len(sys.argv) > 2:
            # Test-Übersetzung einer spezifischen URL
            test_url = sys.argv[2]
            print(f"🧪 Starte Test-Übersetzung für: {test_url}")
            test_translate_url(test_url)
        elif sys.argv[1] == "--help":
            print("Deadlock Changelog Bot - Verwendung:")
            print("  python forum-ki-bot.py                    # Normaler Bot-Modus")
            print("  python forum-ki-bot.py --debug            # Debug-Modus (keine Duplikat-Prüfung)")
            print("  python forum-ki-bot.py --test             # Test-Modus (keine Duplikat-Prüfung)")
            print("  python forum-ki-bot.py --translate <URL>  # Übersetze spezifische URL")
            print("  python forum-ki-bot.py --help             # Diese Hilfe anzeigen")
        else:
            print("❌ Unbekannter Parameter. Verwende --help für Hilfe.")
    else:
        # Normaler Bot-Modus
        main()
