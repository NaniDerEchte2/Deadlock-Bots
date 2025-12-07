import sqlite3

conn = sqlite3.connect('service/deadlock.sqlite3')
c = conn.cursor()

print("="*60)
print("CURRENT BUILD STATUS")
print("="*60)

sources = c.execute('SELECT COUNT(*) FROM hero_build_sources').fetchone()[0]
clones = c.execute('SELECT COUNT(*) FROM hero_build_clones').fetchone()[0]

print(f"\nTotal sources: {sources}")
print(f"Total clones: {clones}")

print("\nClone status breakdown:")
for row in c.execute('SELECT status, COUNT(*) as cnt FROM hero_build_clones GROUP BY status').fetchall():
    print(f"  {row[0]}: {row[1]}")

print("\nBuilds per hero (top 10):")
for row in c.execute('SELECT hero_id, COUNT(*) as cnt FROM hero_build_clones GROUP BY hero_id ORDER BY cnt DESC LIMIT 10').fetchall():
    print(f"  Hero {row[0]}: {row[1]} builds")

# Check if any hero has more than 3 builds
over_limit = c.execute('SELECT hero_id, COUNT(*) as cnt FROM hero_build_clones GROUP BY hero_id HAVING cnt > 3').fetchall()
if over_limit:
    print("\nWARNING - Heroes with MORE than 3 builds:")
    for row in over_limit:
        print(f"  Hero {row[0]}: {row[1]} builds")
else:
    print("\nAll heroes have max 3 builds or less!")

conn.close()
