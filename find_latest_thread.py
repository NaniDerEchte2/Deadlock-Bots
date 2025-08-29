import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

def find_latest_thread():
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Lade die Game Updates Seite
        url = "https://forums.playdeadlock.com/forums/game-updates.4/"
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Finde alle Thread-Links
        thread_links = soup.find_all('a', href=re.compile(r'/threads/.*update.*'))
        
        print("Gefundene Update-Threads:")
        for i, link in enumerate(thread_links[:10]):  # Top 10
            href = link.get('href', '')
            title = link.get_text(strip=True)
            full_url = f"https://forums.playdeadlock.com{href}"
            print(f"{i+1}. {title} - {full_url}")
            
        if thread_links:
            latest_thread = thread_links[0]
            latest_url = f"https://forums.playdeadlock.com{latest_thread.get('href', '')}"
            latest_title = latest_thread.get_text(strip=True)
            
            print(f"\nNEUESTER THREAD:")
            print(f"Title: {latest_title}")
            print(f"URL: {latest_url}")
            
            # Aktualisiere current_patch_thread.txt
            thread_file = r"C:\Users\Nani-Admin\Documents\Deadlock\changelog_data\current_patch_thread.txt"
            with open(thread_file, 'w', encoding='utf-8') as f:
                f.write(f"{latest_url}|")
            
            print(f"current_patch_thread.txt aktualisiert!")
            return latest_url
        else:
            print("Keine Update-Threads gefunden!")
            return None
            
    except Exception as e:
        print(f"Fehler: {e}")
        return None

if __name__ == "__main__":
    find_latest_thread()