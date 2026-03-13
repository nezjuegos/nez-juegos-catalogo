import sqlite3
import os
import json
import unicodedata
from datetime import datetime

class Database:
    def __init__(self, db_path='nez_juegos.db'):
        # In Railway, we mount a volume to persist data.
        # Fallback to local directory if not in Railway.
        volume_path = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', os.path.dirname(os.path.dirname(__file__)))
        self.db_path = os.path.join(volume_path, db_path)
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        """Create tables if they don't exist"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Table: config (CMS for Homepage)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            ''')

            # Table: packs (Telegram scraped data)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS packs (
                id TEXT PRIMARY KEY,
                tg_msg_id INTEGER DEFAULT 0,
                raw_text TEXT,
                games_json TEXT,
                price_usd INTEGER,
                price_local INTEGER,
                cover_url TEXT,
                is_new INTEGER DEFAULT 0,
                is_featured INTEGER DEFAULT 0,
                is_manually_deleted INTEGER DEFAULT 0,
                manual_image_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # Migration to add tg_msg_id and is_featured to existing DB
            try:
                cursor.execute("ALTER TABLE packs ADD COLUMN tg_msg_id INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass # Column already exists
                
            try:
                cursor.execute("ALTER TABLE packs ADD COLUMN is_featured INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass # Column already exists
                
            try:
                cursor.execute("ALTER TABLE packs ADD COLUMN manual_image_url TEXT")
            except sqlite3.OperationalError:
                pass # Column already exists

            # Table: juegos (Individual Games CRUD)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS juegos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT NOT NULL,
                plataforma TEXT DEFAULT 'Nintendo Switch',
                precio_codigo INTEGER,
                precio_primaria INTEGER,
                precio_secundaria INTEGER,
                precio_alquiler INTEGER,
                imagen_filename TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')
            
            # Table: hot_titles (For adding 🔥 emojis)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS hot_titles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT UNIQUE NOT NULL
            )
            ''')
            
            # Insert default config if empty
            cursor.execute('SELECT COUNT(*) FROM config')
            if cursor.fetchone()[0] == 0:
                default_config = [
                    ('titulo_principal', 'Tu próxima aventura en Nintendo Switch empieza aquí'),
                    ('subtitulo', 'Descubre el catálogo más amplio de juegos digitales. Cuentas primarias, secundarias, códigos canjeables y alquileres con entrega inmediata.'),
                    ('enlace_whatsapp', 'https://chat.whatsapp.com/GzWbL0aR9SjDkMnvR3O1wZ'),
                    ('numero_whatsapp', '5491160120337'),
                    ('hero_img_1', '/assets/images/smash.png'),
                    ('hero_img_2', '/assets/images/zelda.png'),
                    ('hero_img_3', '/assets/images/mario.png')
                ]
                cursor.executemany('INSERT INTO config (key, value) VALUES (?, ?)', default_config)
            
            # Ensure newer config keys exist on existing databases
            cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('numero_whatsapp', '5491160120337')")
            # If it exists but is empty, set the default number
            cursor.execute("UPDATE config SET value = '5491160120337' WHERE key = 'numero_whatsapp' AND (value IS NULL OR value = '')")
                
            conn.commit()

    # --- CONFIG CRUD ---
    def get_all_config(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT key, value FROM config')
            return {row['key']: row['value'] for row in cursor.fetchall()}

    def update_config(self, key, value):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', (key, value))
            conn.commit()
            return True

    # --- JUEGOS CRUD ---
    def get_all_juegos(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM juegos ORDER BY titulo COLLATE NOCASE ASC')
            results = []
            for row in cursor.fetchall():
                d = dict(row)
                d['precios'] = {
                    'codigo_digital': d.get('precio_codigo'),
                    'primaria': d.get('precio_primaria'),
                    'secundaria': d.get('precio_secundaria'),
                    'alquiler': d.get('precio_alquiler'),
                }
                results.append(d)
            return results

    def get_juego(self, juego_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM juegos WHERE id = ?', (juego_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def create_juego(self, data):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO juegos (titulo, plataforma, precio_codigo, precio_primaria, precio_secundaria, precio_alquiler, imagen_filename)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                data.get('titulo'),
                data.get('plataforma', 'Nintendo Switch'),
                data.get('precio_codigo'),
                data.get('precio_primaria'),
                data.get('precio_secundaria'),
                data.get('precio_alquiler'),
                data.get('imagen_filename')
            ))
            conn.commit()
            return cursor.lastrowid

    def update_juego(self, juego_id, data):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE juegos 
                SET titulo=?, plataforma=?, precio_codigo=?, precio_primaria=?, precio_secundaria=?, precio_alquiler=?, imagen_filename=COALESCE(?, imagen_filename)
                WHERE id=?
            ''', (
                data.get('titulo'),
                data.get('plataforma', 'Nintendo Switch'),
                data.get('precio_codigo'),
                data.get('precio_primaria'),
                data.get('precio_secundaria'),
                data.get('precio_alquiler'),
                data.get('imagen_filename'),
                juego_id
            ))
            conn.commit()
            return True

    def delete_juego(self, juego_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM juegos WHERE id = ?', (juego_id,))
            conn.commit()
            return True

    # --- PACKS CRUD & SCRAPING LOGIC ---
    def save_packs(self, packs_list, is_scrape_today=False):
        """Saves a list of parsed Pack dictionary objects into the database.
        
        If is_scrape_today=True:
          - Packs already in DB are SKIPPED (they're not new).
          - Only truly new packs (ID not in DB) are inserted with is_new=1.
        If is_scrape_today=False (full scrape):
          - Existing packs are updated (preserving is_new flag).
          - New packs are inserted with is_new=0.
        """
        added_count = 0
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            for pack in packs_list:
                # 1. Check if it already exists
                cursor.execute('SELECT id, is_manually_deleted FROM packs WHERE id = ?', (pack['id'],))
                existing = cursor.fetchone()
                
                # Skip manually deleted packs always
                if existing and existing['is_manually_deleted'] == 1:
                    continue
                
                games_json_str = json.dumps(pack.get('games_json', []))
                
                if existing:
                    if is_scrape_today:
                        # "Escanear Hoy": pack already in catalog, skip it
                        continue
                    else:
                        # Full scrape: update existing pack data, keep is_new as-is
                        cursor.execute('''
                            UPDATE packs SET 
                                tg_msg_id=?, raw_text=?, games_json=?, price_usd=?, price_local=?, 
                                cover_url=COALESCE(?, cover_url)
                            WHERE id=?
                        ''', (
                            pack.get('tg_msg_id', 0), pack['raw_text'], games_json_str,
                            pack['price_usd'], pack['price_local'], pack.get('cover_url'),
                            pack['id']
                        ))
                else:
                    # Truly new pack - insert it
                    cursor.execute('''
                        INSERT INTO packs (id, tg_msg_id, raw_text, games_json, price_usd, price_local, cover_url, is_new)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        pack['id'],
                        pack.get('tg_msg_id', 0),
                        pack['raw_text'],
                        games_json_str,
                        pack['price_usd'],
                        pack['price_local'],
                        pack.get('cover_url'),
                        1 if is_scrape_today else 0
                    ))
                    added_count += 1
            
            conn.commit()
            return added_count

    def mark_pack_deleted(self, pack_id, manual=False):
        """Marks a pack as deleted. If 'manual' is True, it flags it so it never comes back."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if manual:
                cursor.execute('UPDATE packs SET is_manually_deleted = 1 WHERE id = ?', (pack_id,))
            else:
                # If deleted by the sync process, we just remove it physically
                cursor.execute('DELETE FROM packs WHERE id = ?', (pack_id,))
            conn.commit()
            return True

    def get_all_active_pack_ids(self):
        """Returns a list of all pack IDs that are currently visible to the client."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM packs WHERE is_manually_deleted = 0')
            return [row['id'] for row in cursor.fetchall()]

    def get_packs(self, query='', exclude='', price_max=None, dlc_only=False, featured_only=False, limit=500):
        """Advanced Search for Packs - Uses SQLite json1 extension and filtering"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Base query: only show packs that weren't manually deleted
            sql = "SELECT * FROM packs WHERE is_manually_deleted = 0"
            params = []
            
            if featured_only:
                sql += " AND is_featured = 1"
                
            if price_max is not None:
                sql += " AND price_local <= ?"
                params.append(price_max)
                
            cursor.execute(sql + " ORDER BY COALESCE(tg_msg_id, 0) DESC, CAST(id AS INTEGER) DESC", params)
            all_packs = cursor.fetchall()
            
            results = []
            query_parts = [q.lower().strip() for q in query.split() if q.strip()]
            exclude_parts = [e.lower().strip() for e in exclude.split() if e.strip()]
            
            for row in all_packs:
                pack_dict = dict(row)
                games = json.loads(pack_dict['games_json']) if pack_dict['games_json'] else []
                pack_dict['games'] = games # parsed list for the UI
                pack_dict['manual_image_url'] = pack_dict.get('manual_image_url')
                
                # 1. ID Match Short-circuit
                if query.strip().isdigit() and query.strip() == pack_dict['id']:
                    results.append(pack_dict)
                    continue
                
                # 2. DLC Only Filter
                if dlc_only:
                    # If the pack doesn't have ANY dlc, skip
                    if not any(g.get('is_dlc', False) for g in games):
                        continue
                        
                # 3. Keyword Match Logic
                games_text_all = self._strip_accents(" ".join([g.get('name', '') for g in games]).lower())
                
                # Require ALL query parts
                matches_query = True
                for kw in query_parts:
                    if self._strip_accents(kw) not in games_text_all:
                        matches_query = False
                        break
                
                if not matches_query:
                    continue
                    
                # 4. Exclusion Logic (Line-aware)
                should_exclude = False
                if exclude_parts:
                    for game in games:
                        g_name = game.get('name', '').lower()
                        # If this specific game line matches the query (or query is empty)
                        is_relevant = not query_parts or any(kw in g_name for kw in query_parts)
                        if is_relevant:
                            # If it also contains an excluded keyword, drop the whole pack
                            if any(ex in g_name for ex in exclude_parts):
                                should_exclude = True
                                break
                                
                if should_exclude:
                    continue
                    
                results.append(pack_dict)
                if len(results) >= limit:
                    break
                    
            return results

    def get_game_name_suggestions(self, partial_name, limit=5):
        """Extracts unique game names from the packs table that match the partial string."""
        if len(partial_name) < 3:
            return []
            
        partial_lower = partial_name.lower()
        partial_norm = self._strip_accents(partial_lower)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT games_json FROM packs WHERE is_manually_deleted = 0')
            rows = cursor.fetchall()
            
            matches = set()
            for row in rows:
                games = json.loads(row['games_json']) if row['games_json'] else []
                for game in games:
                    name = game.get('name', '')
                    if partial_norm in self._strip_accents(name.lower()):
                        matches.add(name)
                        
            # Return alphabetical sorted list
            return sorted(list(matches))[:limit]

    def count_featured_packs(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) as c FROM packs WHERE is_featured = 1 AND is_manually_deleted = 0')
            return cursor.fetchone()['c']

    def toggle_pack_featured(self, pack_id, force=None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if force is not None:
                new_val = 1 if force else 0
            else:
                cursor.execute('SELECT is_featured FROM packs WHERE id = ?', (pack_id,))
                row = cursor.fetchone()
                if not row: return False
                new_val = 0 if row['is_featured'] == 1 else 1

            if new_val == 1:
                # Check limit before toggling on
                if self.count_featured_packs() >= 6:
                    return False # Over limit

            cursor.execute('UPDATE packs SET is_featured = ? WHERE id = ?', (new_val, pack_id))
            conn.commit()
            return True

    def insert_manual_pack(self, pack_data):
        """Insert a manually created pack into the database."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Generate a pseudo-ID for manual packs
            pseudo_id = f"MANUAL-{int(datetime.now().timestamp())}"
            
            cursor.execute('''
                INSERT INTO packs (id, raw_text, games_json, price_usd, price_local, manual_image_url, is_new, is_featured)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                pseudo_id,
                pack_data.get('raw_text', ''),
                json.dumps(pack_data.get('games', [])),
                pack_data.get('price_usd', 0),
                pack_data.get('price_local', 0),
                pack_data.get('manual_image_url'),
                1, # Mark as new so it stands out
                0
            ))
            conn.commit()
            return pseudo_id

    # --- Hot Titles CRUD ---
    def get_hot_titles(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM hot_titles ORDER BY titulo COLLATE NOCASE')
            return [dict(row) for row in cursor.fetchall()]
            
    def add_hot_title(self, titulo):
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('INSERT INTO hot_titles (titulo) VALUES (?)', (titulo.strip(),))
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False # Already exists
            
    def delete_hot_title(self, id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM hot_titles WHERE id = ?', (id,))
            conn.commit()

    @staticmethod
    def _strip_accents(text):
        """Remove diacritics/accents from a string for accent-insensitive comparison."""
        nfkd = unicodedata.normalize('NFKD', text)
        return ''.join(c for c in nfkd if not unicodedata.combining(c))
