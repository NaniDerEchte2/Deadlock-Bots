import sqlite3
import sys
import os

# Database path
DB_PATH = 'service/deadlock.sqlite3'

def add_monitored_streamer(login):
    login = login.strip().lower()
    if not login:
        print("Error: Empty login provided.")
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Check if already exists
        cursor.execute("SELECT is_monitored_only FROM twitch_streamers WHERE twitch_login = ?", (login,))
        row = cursor.fetchone()
        
        if row:
            if row[0] == 1:
                print(f"Streamer '{login}' is already being monitored.")
            else:
                print(f"Streamer '{login}' is already a partner/registered (is_monitored_only=0). No action needed.")
        else:
            # Insert new monitored streamer
            cursor.execute(
                """
                INSERT INTO twitch_streamers (twitch_login, is_monitored_only)
                VALUES (?, 1)
                """,
                (login,)
            )
            conn.commit()
            print(f"Successfully added '{login}' to monitored list.")
            
        conn.close()
    except Exception as e:
        print(f"Database error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/add_monitored_streamer.py <twitch_login>")
    else:
        add_monitored_streamer(sys.argv[1])
