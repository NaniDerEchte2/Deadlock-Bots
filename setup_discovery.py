import sqlite3
import os

db_path = os.path.join(r"C:\Users\Nani-Admin\Documents\Deadlock\service", "deadlock.sqlite3")
initial_author_id = 91484677

print(f"Setting up build discovery feature in database: {db_path}")

try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. Create the table to store watched authors
    print("Creating 'watched_build_authors' table if it doesn't exist...")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watched_build_authors (
            author_account_id INTEGER PRIMARY KEY NOT NULL,
            notes TEXT,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            last_checked_at INTEGER,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
    """)
    print("Table created successfully.")

    # 2. Add the initial author to the list, ignoring if they already exist
    print(f"Ensuring author {initial_author_id} is on the watch list...")
    cursor.execute("""
        INSERT OR IGNORE INTO watched_build_authors (author_account_id, notes)
        VALUES (?, ?)
    """, (initial_author_id, "Initial author provided by user"))

    conn.commit()

    # 3. Verify the author is in the table
    cursor.execute("SELECT COUNT(*) FROM watched_build_authors WHERE author_account_id = ?", (initial_author_id,))
    count = cursor.fetchone()[0]

    if count > 0:
        print(f"Author {initial_author_id} is successfully in the watch list.")
    else:
        print(f"Error: Could not add author {initial_author_id} to the watch list.")

    conn.close()
    print("Database setup complete.")

except Exception as e:
    print(f"An error occurred: {e}")

