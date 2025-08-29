#!/usr/bin/env python3
"""
Sicherer Forum KI Bot Killer - beendet nur Forum KI Bot Subprozesse
"""

import psutil
import sys
import time

def kill_forum_ki_bots():
    """Beendet alle Forum KI Bot Prozesse sicher"""
    killed_pids = []
    
    print("Suche nach Forum KI Bot Prozessen...")
    
    for proc in psutil.process_iter():
        try:
            cmdline = proc.cmdline()
            if cmdline and len(cmdline) > 1:
                cmdline_str = ' '.join(cmdline)
                
                # Suche spezifisch nach forum-ki-bot.py Prozessen
                if 'forum-ki-bot.py' in cmdline_str:
                    print(f"Gefunden: PID {proc.pid}")
                    print(f"   Command: {' '.join(cmdline[:2])}...")
                    
                    # Beende den Prozess
                    proc.terminate()
                    killed_pids.append(proc.pid)
                    
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception as e:
            continue
    
    if killed_pids:
        print(f"Warte auf Beendigung der {len(killed_pids)} Prozesse...")
        time.sleep(3)
        
        # Prüfe ob alle beendet wurden
        still_running = []
        for pid in killed_pids:
            try:
                proc = psutil.Process(pid)
                if proc.is_running():
                    print(f"Forciere Beendigung von PID {pid}")
                    proc.kill()
                    still_running.append(pid)
            except psutil.NoSuchProcess:
                # Bereits beendet
                pass
            except Exception as e:
                print(f"Fehler bei PID {pid}: {e}")
        
        if still_running:
            time.sleep(1)
    
    # Finale Prüfung
    print("\nFinale Pruefung...")
    remaining = 0
    for proc in psutil.process_iter():
        try:
            cmdline = proc.cmdline()
            if cmdline and 'forum-ki-bot.py' in ' '.join(cmdline):
                remaining += 1
        except:
            continue
    
    if remaining == 0:
        print("Alle Forum KI Bot Prozesse erfolgreich beendet!")
        return True
    else:
        print(f"{remaining} Forum KI Bot Prozesse sind noch aktiv")
        return False

if __name__ == "__main__":
    print("Forum KI Bot Killer")
    print("=" * 40)
    
    success = kill_forum_ki_bots()
    
    if success:
        print("\nMission erfolgreich!")
        sys.exit(0)
    else:
        print("\nEinige Prozesse konnten nicht beendet werden")
        sys.exit(1)