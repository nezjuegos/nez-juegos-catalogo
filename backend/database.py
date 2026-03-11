import sqlite3
import os
import json
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
                raw_text TEXT,
                games_json TEXT,
                price_usd INTEGER,
                price_local INTEGER,
                cover_url TEXT,
                is_new INTEGER DEFAULT 0,
                is_manually_deleted INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

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
            
            # Insert default config if empty
            cursor.execute('SELECT COUNT(*) FROM config')
            if cursor.fetchone()[0] == 0:
                default_config = [
                    ('titulo_principal', 'Tu próxima aventura en Nintendo Switch empieza aquí'),
                    ('subtitulo', 'Descubre el catálogo más amplio de juegos digitales. Cuentas primarias, secundarias, códigos canjeables y alquileres con entrega inmediata.'),
                    ('enlace_whatsapp', 'https://chat.whatsapp.com/GzWbL0aR9SjDkMnvR3O1wZ'),
                    ('hero_image_url', '')
                ]
                cursor.executemany('INSERT INTO config (key, value) VALUES (?, ?)', default_config)
                
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
            return [dict(row) for row in cursor.fetchall()]

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
        Optionally marks them as 'is_new' if it's a 'Scan Today' operation."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # If doing a full scrape, we might want to clear 'is_new' flag globally first
            # But let's leave it manual for now.
            
            for pack in packs_list:
                # 1. Check if it exists and was manually deleted
                cursor.execute('SELECT is_manually_deleted FROM packs WHERE id = ?', (pack['id'],))
                existing = cursor.fetchone()
                if existing and existing['is_manually_deleted'] == 1:
                    continue # Skip this pack, the admin manually deleted it
                
                games_json_str = json.dumps(pack.get('games_json', []))
                
                # Check if it exists to maintain created_at and is_new properties if not overwriting
                if existing:
                    # Update without changing is_new unless explicitly asked
                    is_new_val = 1 if is_scrape_today else '(SELECT is_new FROM packs WHERE id = ?)'
                    query = f'''
                        UPDATE packs SET 
                            raw_text=?, games_json=?, price_usd=?, price_local=?, cover_url=COALESCE(?, cover_url), is_new={is_new_val}
                        WHERE id=?
                    '''
                    params = [pack['raw_text'], games_json_str, pack['price_usd'], pack['price_local'], pack.get('cover_url')]
                    if not is_scrape_today:
                        params.append(pack['id']) # for the subquery
                    params.append(pack['id'])
                    cursor.execute(query, params)
                else:
                    # Insert new
                    cursor.execute('''
                        INSERT INTO packs (id, raw_text, games_json, price_usd, price_local, cover_url, is_new)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        pack['id'],
                        pack['raw_text'],
                        games_json_str,
                        pack['price_usd'],
                        pack['price_local'],
                        pack.get('cover_url'),
                        1 if is_scrape_today else 0
                    ))
            
            conn.commit()
            return True

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
                sql += " AND is_new = 1"
                
            if price_max is not None:
                sql += " AND price_local <= ?"
                params.append(price_max)
                
            cursor.execute(sql + " ORDER BY id DESC", params)
            all_packs = cursor.fetchall()
            
            results = []
            query_parts = [q.lower().strip() for q in query.split() if q.strip()]
            exclude_parts = [e.lower().strip() for e in exclude.split() if e.strip()]
            
            for row in all_packs:
                pack_dict = dict(row)
                games = json.loads(pack_dict['games_json']) if pack_dict['games_json'] else []
                pack_dict['games'] = games # parsed list for the UI
                
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
                games_text_all = " ".join([g.get('name', '') for g in games]).lower()
                
                # Require ALL query parts
                matches_query = True
                for kw in query_parts:
                    if kw not in games_text_all:
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
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT games_json FROM packs WHERE games_json LIKE ?', (f'%{partial_lower}%',))
            rows = cursor.fetchall()
            
            matches = set()
            for row in rows:
                games = json.loads(row['games_json']) if row['games_json'] else []
                for game in games:
                    name = game.get('name', '')
                    if partial_lower in name.lower():
                        matches.add(name)
                        
            # Return alphabetical sorted list
            return sorted(list(matches))[:limit]
