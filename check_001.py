import sqlite3

conn = sqlite3.connect('data/file_index.db')
c = conn.cursor()
c.execute("SELECT file_id, name, path, source, description, webContentLink FROM files WHERE name LIKE '%20anos001%'")
for r in c.fetchall():
    print(f"FILE_ID: {r[0]}")
    print(f"PATH:    {r[2]}")
    print(f"SOURCE:  {r[3]}")
    print(f"DESC:    {repr(r[4])}")
    print(f"LINK:    {r[5]}")
    print("-" * 50)
