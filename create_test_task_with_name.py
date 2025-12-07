import sqlite3
import json
import os

db_path = os.path.join(r"C:\Users\Nani-Admin\Documents\Deadlock\service", "deadlock.sqlite3")

task_payload = {
    "origin_hero_build_id": 55579,
    "target_name": "Gemini Test - Author Name",
    "target_language": 1,
    "target_description": "Automated test build by Gemini. Testing author name feature."
}

payload_json = json.dumps(task_payload)

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO steam_tasks (type, payload, status)
        VALUES ('BUILD_PUBLISH', ?, 'PENDING')
    """, (payload_json,))

    conn.commit()
    new_task_id = cursor.lastrowid
    conn.close()

    print(f"Successfully created new build task for Abrams with ID: {new_task_id}")

except Exception as e:
    print(f"An error occurred: {e}")
