"""
seed_db.py - Populate local DB with production data from nezjuegos.com
Run this once to get real packs data for local development.
"""
import urllib.request
import json
import sqlite3
import os

API_BASE = "https://nezjuegos.com"
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nez_juegos.db")

def fetch_json(endpoint):
    url = f"{API_BASE}{endpoint}"
    print(f"  Fetching {url}...")
    req = urllib.request.Request(url, headers={"User-Agent": "NezJuegos-Seed/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))

def seed_packs(conn):
    print("\n📦 Seeding packs...")
    data = fetch_json("/api/packs?limit=500")
    packs = data.get("results", [])
    
    cursor = conn.cursor()
    inserted = 0
    for p in packs:
        games_json_str = p.get("games_json", "[]")
        # If games_json is already parsed as a list, re-serialize it
        if isinstance(games_json_str, list):
            games_json_str = json.dumps(games_json_str)
        
        cursor.execute("""
            INSERT OR REPLACE INTO packs 
            (id, tg_msg_id, raw_text, games_json, price_usd, price_local, cover_url, is_new, is_featured, is_manually_deleted, manual_image_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            p["id"],
            p.get("tg_msg_id", 0),
            p.get("raw_text", ""),
            games_json_str,
            p.get("price_usd", 0),
            p.get("price_local", 0),
            p.get("cover_url"),
            p.get("is_new", 0),
            p.get("is_featured", 0),
            p.get("is_manually_deleted", 0),
            p.get("manual_image_url"),
            p.get("created_at"),
        ))
        inserted += 1
    
    conn.commit()
    print(f"  ✅ Inserted {inserted} packs")

def seed_config(conn):
    print("\n⚙️  Seeding config...")
    try:
        data = fetch_json("/api/config")
        cursor = conn.cursor()
        for key, value in data.items():
            cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        print(f"  ✅ Inserted {len(data)} config entries")
    except Exception as e:
        print(f"  ⚠️  Could not seed config: {e}")

def main():
    print(f"🗄️  Database: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    
    # Ensure tables exist
    conn.execute("""CREATE TABLE IF NOT EXISTS packs (
        id TEXT PRIMARY KEY, tg_msg_id INTEGER DEFAULT 0, raw_text TEXT,
        games_json TEXT, price_usd INTEGER, price_local INTEGER, cover_url TEXT,
        is_new INTEGER DEFAULT 0, is_featured INTEGER DEFAULT 0,
        is_manually_deleted INTEGER DEFAULT 0, manual_image_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    
    seed_packs(conn)
    seed_config(conn)
    
    # Show summary
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM packs")
    print(f"\n📊 Total packs in local DB: {cursor.fetchone()[0]}")
    cursor.execute("SELECT COUNT(*) FROM packs WHERE is_featured = 1")
    print(f"⭐ Featured packs: {cursor.fetchone()[0]}")
    cursor.execute("SELECT COUNT(*) FROM packs WHERE is_new = 1")
    print(f"🔥 New packs: {cursor.fetchone()[0]}")
    
    conn.close()
    print("\n🎉 Done! Run 'cd backend && python server.py' to start the local server.")

if __name__ == "__main__":
    main()
