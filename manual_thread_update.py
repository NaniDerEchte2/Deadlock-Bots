#!/usr/bin/env python3
"""
Manuelle Thread-Aktualisierung für das Deadlock Changelog System
"""

def update_to_latest_thread():
    # Der neue Steam Link vom 20. August 2025
    steam_url = "https://store.steampowered.com/news/app/1422450/view/524226158154219679"
    
    # Vermutlich gibt es einen neuen Forum Thread für August 2025
    # Da der Bot noch auf Juli Thread hängt, setze ihn manuell auf "suche nach neuestem"
    
    thread_file = r"C:\Users\Nani-Admin\Documents\Deadlock\changelog_data\current_patch_thread.txt"
    
    # Setze auf einen neueren Zeitraum - August 2025
    august_thread = "https://forums.playdeadlock.com/threads/08-20-2025-update.99999/"  # Placeholder
    
    # Aber da wir die genaue Thread URL nicht kennen, lösche die Datei
    # damit der Bot nach dem neuesten Thread sucht
    try:
        import os
        if os.path.exists(thread_file):
            os.remove(thread_file)
            print(f"Gelöscht: {thread_file}")
            print("Bot wird nach dem neuesten Thread suchen beim nächsten Start.")
        
        # Zusätzlich: Lösche auch die letzte comment ID
        comment_file = r"C:\Users\Nani-Admin\Documents\Deadlock\changelog_data\last_comment_id.txt"
        if os.path.exists(comment_file):
            os.remove(comment_file)
            print(f"Gelöscht: {comment_file}")
            
        print("System zurückgesetzt - Bot sucht automatisch nach neuestem Thread.")
        
        # Zeige Steam URL zur manuellen Verarbeitung
        print(f"\nNeue Steam URL zur manuellen Verarbeitung:")
        print(f"{steam_url}")
        
    except Exception as e:
        print(f"Fehler: {e}")

if __name__ == "__main__":
    update_to_latest_thread()