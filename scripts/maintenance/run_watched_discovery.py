import sqlite3
import json
import os

db_path = os.path.join(r"C:\Users\Nani-Admin\Documents\Deadlock\service", "deadlock.sqlite3")

discover_payload = {}

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print("Inserting DISCOVER_WATCHED_BUILDS task...")
    cursor.execute("""
        INSERT INTO steam_tasks (type, payload, status)
        VALUES ('DISCOVER_WATCHED_BUILDS', ?, 'PENDING')
    """, (json.dumps(discover_payload),))
    task_id = cursor.lastrowid
    print(f"  -> Created task with ID: {task_id}")

    conn.commit()
    conn.close()

    print("\nSuccessfully created DISCOVER_WATCHED_BUILDS task.")

except Exception as e:
    print(f"An error occurred: {e}")
