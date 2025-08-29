"""
Windows Auto-Start Setup für Master Bot

Führe dieses Script als Administrator aus um:
1. Windows Task zu erstellen der beim Boot startet
2. Master Bot automatisch zu starten
"""

import subprocess
import sys
import os
from pathlib import Path

def create_startup_task():
    """Erstellt Windows Task Scheduler Eintrag"""
    
    script_dir = Path(__file__).parent.absolute()
    bat_file = script_dir / "start_master_bot.bat"
    
    # Task Scheduler Command
    task_cmd = [
        'schtasks', '/create',
        '/tn', 'DeadlockMasterBot',
        '/tr', str(bat_file),
        '/sc', 'onstart',
        '/ru', 'SYSTEM',
        '/rl', 'HIGHEST',
        '/f'  # Force overwrite if exists
    ]
    
    try:
        result = subprocess.run(task_cmd, capture_output=True, text=True, check=True)
        print("✅ Windows Task erfolgreich erstellt!")
        print("Der Master Bot startet jetzt automatisch beim Windows-Start.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Fehler beim Erstellen des Tasks: {e}")
        print(f"Ausgabe: {e.output}")
        return False

def create_registry_entry():
    """Alternative: Registry Auto-Start Eintrag"""
    
    script_dir = Path(__file__).parent.absolute()
    bat_file = script_dir / "start_master_bot.bat"
    
    reg_cmd = [
        'reg', 'add',
        'HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run',
        '/v', 'DeadlockMasterBot',
        '/t', 'REG_SZ', 
        '/d', str(bat_file),
        '/f'
    ]
    
    try:
        subprocess.run(reg_cmd, check=True)
        print("✅ Registry-Eintrag erfolgreich erstellt!")
        print("Der Master Bot startet automatisch bei der Benutzeranmeldung.")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Fehler beim Registry-Eintrag: {e}")
        return False

def remove_startup():
    """Entfernt Auto-Start"""
    
    # Task Scheduler entfernen
    try:
        subprocess.run(['schtasks', '/delete', '/tn', 'DeadlockMasterBot', '/f'], 
                      capture_output=True, check=True)
        print("✅ Windows Task entfernt")
    except:
        pass
    
    # Registry entfernen  
    try:
        subprocess.run(['reg', 'delete', 
                       'HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run',
                       '/v', 'DeadlockMasterBot', '/f'], 
                      capture_output=True, check=True)
        print("✅ Registry-Eintrag entfernt")
    except:
        pass

if __name__ == "__main__":
    print("Deadlock Master Bot - Windows Auto-Start Setup")
    print("=" * 50)
    
    if len(sys.argv) > 1 and sys.argv[1] == "remove":
        remove_startup()
        sys.exit()
    
    print("Wähle Auto-Start Methode:")
    print("1. Task Scheduler (Empfohlen - startet beim Boot)")
    print("2. Registry (startet bei Benutzeranmeldung)")
    print("3. Beides deinstallieren")
    
    choice = input("\nEingabe (1/2/3): ").strip()
    
    if choice == "1":
        if create_startup_task():
            print("\n🎉 Setup abgeschlossen!")
            print("Tipp: Teste mit 'schtasks /run /tn DeadlockMasterBot'")
    elif choice == "2":
        if create_registry_entry():
            print("\n🎉 Setup abgeschlossen!")
    elif choice == "3":
        remove_startup()
        print("\n✅ Auto-Start entfernt")
    else:
        print("❌ Ungültige Eingabe")