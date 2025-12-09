import sqlite3

DB_PATH = r"C:\Users\Nani-Admin\Documents\Deadlock\service\deadlock.sqlite3"
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

print("=== hero_build_sources schema ===")
for row in c.execute("PRAGMA table_info(hero_build_sources)"):
    print(f"  {row[1]} - {row[2]}")

print("\n=== hero_build_clones schema ===")
for row in c.execute("PRAGMA table_info(hero_build_clones)"):
    print(f"  {row[1]} - {row[2]}")

print("\n=== steam_tasks schema ===")
for row in c.execute("PRAGMA table_info(steam_tasks)"):
    print(f"  {row[1]} - {row[2]}")

print("\n=== standalone_bot_state schema ===")
for row in c.execute("PRAGMA table_info(standalone_bot_state)"):
    print(f"  {row[1]} - {row[2]}")

conn.close()
