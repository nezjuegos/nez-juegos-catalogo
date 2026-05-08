import sqlite3
import os
import json
import re
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

            try:
                cursor.execute("ALTER TABLE juegos ADD COLUMN is_featured INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass # Column already exists
                
            try:
                cursor.execute("ALTER TABLE juegos ADD COLUMN orden_destacado INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass # Column already exists

            try:
                cursor.execute("ALTER TABLE juegos ADD COLUMN precio_eshop INTEGER")
            except sqlite3.OperationalError:
                pass # Column already exists
                
            try:
                cursor.execute("ALTER TABLE juegos ADD COLUMN oferta_codigo INTEGER")
                cursor.execute("ALTER TABLE juegos ADD COLUMN oferta_primaria INTEGER")
                cursor.execute("ALTER TABLE juegos ADD COLUMN oferta_secundaria INTEGER")
                cursor.execute("ALTER TABLE juegos ADD COLUMN oferta_alquiler INTEGER")
            except sqlite3.OperationalError:
                pass # Columns already exist

            # PlayStation-specific price columns
            ps_cols = [
                "ALTER TABLE juegos ADD COLUMN precio_primaria_ps5 INTEGER",
                "ALTER TABLE juegos ADD COLUMN precio_primaria_ps4 INTEGER",
                "ALTER TABLE juegos ADD COLUMN precio_secundaria_ps5 INTEGER",
                "ALTER TABLE juegos ADD COLUMN oferta_primaria_ps5 INTEGER",
                "ALTER TABLE juegos ADD COLUMN oferta_primaria_ps4 INTEGER",
                "ALTER TABLE juegos ADD COLUMN oferta_secundaria_ps5 INTEGER",
            ]
            for col_sql in ps_cols:
                try:
                    cursor.execute(col_sql)
                except sqlite3.OperationalError:
                    pass  # Column already exists

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
            
            # Table: title_tags (Unified tagging: juego, dlc, hot)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS title_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                tag TEXT NOT NULL,
                UNIQUE(keyword, tag)
            )
            ''')

            # Migration: move hot_titles -> title_tags if old table exists
            try:
                cursor.execute('SELECT id, titulo FROM hot_titles')
                old_rows = cursor.fetchall()
                for row in old_rows:
                    try:
                        cursor.execute('INSERT OR IGNORE INTO title_tags (keyword, tag) VALUES (?, ?)', (row['titulo'], 'hot'))
                    except: pass
                if old_rows:
                    cursor.execute('DROP TABLE IF EXISTS hot_titles')
            except sqlite3.OperationalError:
                pass  # hot_titles doesn't exist, nothing to migrate
            
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

            # USDT conversion rate for Binance Pay (ARS per 1 USDT, applied to the transfer price)
            cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('usdt_rate', '1440')")
            # Amazon JP tracker conversion rate (JPY per 1 USDT)
            cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('usdt_jpy_rate', '160')")
            # Fallback rate when Amazon renders prices in USD (JPY per 1 USD)
            cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('usd_jpy_rate', '150')")
            cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('binance_pay_id', '192 236 539')")
            # Global % discount on pack list prices (public catalog only; DB keeps scraped price)
            cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('pack_global_discount_pct', '0')")

            # Table: orders (Checkout orders)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER,
                game_titulo TEXT NOT NULL,
                game_plataforma TEXT,
                tipo_producto TEXT NOT NULL,
                buyer_email TEXT NOT NULL,
                buyer_phone TEXT,
                payment_method TEXT NOT NULL,
                payment_status TEXT DEFAULT 'pendiente',
                comprobante_ref TEXT,
                comprobante_file TEXT,
                precio_base INTEGER NOT NULL,
                precio_cobrado INTEGER NOT NULL,
                surcharge INTEGER DEFAULT 0,
                uala_order_uuid TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP,
                delivered_at TIMESTAMP
            )
            ''')

            # Table: ps_accounts (Pool of PlayStation accounts with 10 activation keys each)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS ps_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                password TEXT NOT NULL,
                activation_keys TEXT NOT NULL,
                keys_used INTEGER DEFAULT 0,
                status TEXT DEFAULT 'disponible',
                game_id INTEGER,
                game_titulo TEXT,
                primaria_total INTEGER DEFAULT 1,
                primaria_used INTEGER DEFAULT 0,
                primaria_ps4_total INTEGER DEFAULT 1,
                primaria_ps4_used INTEGER DEFAULT 0,
                secundaria_total INTEGER DEFAULT 2,
                secundaria_used INTEGER DEFAULT 0,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notes TEXT
            )
            ''')
            # Migrations for existing DBs
            for col in ['game_id INTEGER', 'game_titulo TEXT',
                        'game_ids TEXT',
                        'primaria_total INTEGER DEFAULT 1', 'primaria_used INTEGER DEFAULT 0',
                        'primaria_ps4_total INTEGER DEFAULT 1', 'primaria_ps4_used INTEGER DEFAULT 0',
                        'secundaria_total INTEGER DEFAULT 2', 'secundaria_used INTEGER DEFAULT 0']:
                try:
                    cursor.execute(f'ALTER TABLE ps_accounts ADD COLUMN {col}')
                except Exception:
                    pass

            # Backfill game_ids JSON from existing game_id where missing
            try:
                cursor.execute("SELECT id, game_id, game_ids FROM ps_accounts WHERE game_ids IS NULL OR game_ids = ''")
                for row in cursor.fetchall():
                    gid = row['game_id']
                    new_ids = json.dumps([gid]) if gid else json.dumps([])
                    cursor.execute("UPDATE ps_accounts SET game_ids = ? WHERE id = ?", (new_ids, row['id']))
                conn.commit()
            except Exception:
                pass

            # Table: ps_delivery_log (Tracks each individual key delivery)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS ps_delivery_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ps_account_id INTEGER NOT NULL,
                order_id INTEGER NOT NULL,
                key_index INTEGER NOT NULL,
                activation_key TEXT NOT NULL,
                sale_type TEXT DEFAULT 'primaria',
                delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (ps_account_id) REFERENCES ps_accounts(id),
                FOREIGN KEY (order_id) REFERENCES orders(id)
            )
            ''')
            # Migration: add sale_type if missing
            try:
                cursor.execute("ALTER TABLE ps_delivery_log ADD COLUMN sale_type TEXT DEFAULT 'primaria'")
            except Exception:
                pass

            # Table: recurring_expenses (monthly business payments calendar)
            # day_of_month is 1..31 — for months without that day (e.g. Feb 30) the UI
            # clamps to the last day of that month when rendering the calendar.
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS recurring_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                day_of_month INTEGER NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'ARS',
                notes TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

            # Table: amazon_jp_tracker (Amazon Japan listing price tracking)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS amazon_jp_tracker (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name_es TEXT NOT NULL,
                amazon_url TEXT NOT NULL UNIQUE,
                asin TEXT,
                image_url TEXT,
                title_source TEXT,
                price_jpy REAL,
                list_price_jpy REAL,
                is_on_sale INTEGER NOT NULL DEFAULT 0,
                price_usdt REAL,
                price_ars REAL,
                is_active INTEGER NOT NULL DEFAULT 1,
                last_checked_at TIMESTAMP,
                last_status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

            # Table: nintendo_mirror_custom (manual entries for private Nintendo preview)
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS nintendo_mirror_custom (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                image_url TEXT,
                image_filename TEXT,
                codigo_regular_ars REAL,
                codigo_offer_ars REAL,
                primaria_regular_ars REAL,
                primaria_offer_ars REAL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            ''')

            # Migration: manual price override columns
            for col_sql in [
                "ALTER TABLE amazon_jp_tracker ADD COLUMN price_manual_jpy REAL",
                "ALTER TABLE amazon_jp_tracker ADD COLUMN list_manual_jpy REAL",
                "ALTER TABLE amazon_jp_tracker ADD COLUMN price_manual_override INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE amazon_jp_tracker ADD COLUMN mirror_image_url TEXT",
                "ALTER TABLE amazon_jp_tracker ADD COLUMN mirror_image_filename TEXT",
            ]:
                try:
                    cursor.execute(col_sql)
                except sqlite3.OperationalError:
                    pass

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
    def get_all_juegos(self, featured_only=False):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if featured_only:
                cursor.execute('SELECT * FROM juegos WHERE is_featured = 1 ORDER BY CASE WHEN orden_destacado = 0 THEN 9999 ELSE orden_destacado END ASC, titulo COLLATE NOCASE ASC')
            else:
                cursor.execute('SELECT * FROM juegos ORDER BY titulo COLLATE NOCASE ASC')
            results = []
            for row in cursor.fetchall():
                d = dict(row)
                d['precios'] = {
                    'codigo_digital': d.get('precio_codigo'),
                    'primaria': d.get('precio_primaria'),
                    'secundaria': d.get('precio_secundaria'),
                    'alquiler': d.get('precio_alquiler'),
                    'eshop': d.get('precio_eshop'),
                    'oferta_codigo_digital': d.get('oferta_codigo'),
                    'oferta_primaria': d.get('oferta_primaria'),
                    'oferta_secundaria': d.get('oferta_secundaria'),
                    'oferta_alquiler': d.get('oferta_alquiler'),
                    # PlayStation-specific
                    'primaria_ps5': d.get('precio_primaria_ps5'),
                    'primaria_ps4': d.get('precio_primaria_ps4'),
                    'secundaria_ps5': d.get('precio_secundaria_ps5'),
                    'oferta_primaria_ps5': d.get('oferta_primaria_ps5'),
                    'oferta_primaria_ps4': d.get('oferta_primaria_ps4'),
                    'oferta_secundaria_ps5': d.get('oferta_secundaria_ps5'),
                }
                d['orden_destacado'] = d.get('orden_destacado', 0)
                results.append(d)
            return self.apply_title_tags(results)

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
                INSERT INTO juegos (titulo, plataforma, precio_codigo, precio_primaria, precio_secundaria, precio_alquiler, precio_eshop,
                                    oferta_codigo, oferta_primaria, oferta_secundaria, oferta_alquiler,
                                    precio_primaria_ps5, precio_primaria_ps4, precio_secundaria_ps5,
                                    oferta_primaria_ps5, oferta_primaria_ps4, oferta_secundaria_ps5,
                                    imagen_filename, is_featured, orden_destacado)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                data.get('titulo'),
                data.get('plataforma', 'Nintendo Switch'),
                data.get('precio_codigo') or None,
                data.get('precio_primaria') or None,
                data.get('precio_secundaria') or None,
                data.get('precio_alquiler') or None,
                data.get('precio_eshop') or None,
                data.get('oferta_codigo') or None,
                data.get('oferta_primaria') or None,
                data.get('oferta_secundaria') or None,
                data.get('oferta_alquiler') or None,
                data.get('precio_primaria_ps5') or None,
                data.get('precio_primaria_ps4') or None,
                data.get('precio_secundaria_ps5') or None,
                data.get('oferta_primaria_ps5') or None,
                data.get('oferta_primaria_ps4') or None,
                data.get('oferta_secundaria_ps5') or None,
                data.get('imagen_filename'),
                int(data.get('is_featured', 0)),
                int(data.get('orden_destacado') or 0)
            ))
            conn.commit()
            return cursor.lastrowid

    def update_juego(self, juego_id, data):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE juegos
                SET titulo=?, plataforma=?, precio_codigo=?, precio_primaria=?, precio_secundaria=?, precio_alquiler=?, precio_eshop=?,
                    oferta_codigo=?, oferta_primaria=?, oferta_secundaria=?, oferta_alquiler=?,
                    precio_primaria_ps5=?, precio_primaria_ps4=?, precio_secundaria_ps5=?,
                    oferta_primaria_ps5=?, oferta_primaria_ps4=?, oferta_secundaria_ps5=?,
                    imagen_filename=COALESCE(?, imagen_filename), is_featured=?, orden_destacado=?
                WHERE id=?
            ''', (
                data.get('titulo'),
                data.get('plataforma', 'Nintendo Switch'),
                data.get('precio_codigo') or None,
                data.get('precio_primaria') or None,
                data.get('precio_secundaria') or None,
                data.get('precio_alquiler') or None,
                data.get('precio_eshop') or None,
                data.get('oferta_codigo') or None,
                data.get('oferta_primaria') or None,
                data.get('oferta_secundaria') or None,
                data.get('oferta_alquiler') or None,
                data.get('precio_primaria_ps5') or None,
                data.get('precio_primaria_ps4') or None,
                data.get('precio_secundaria_ps5') or None,
                data.get('oferta_primaria_ps5') or None,
                data.get('oferta_primaria_ps4') or None,
                data.get('oferta_secundaria_ps5') or None,
                data.get('imagen_filename'),
                int(data.get('is_featured', 0)),
                int(data.get('orden_destacado') or 0),
                juego_id
            ))
            conn.commit()
            return True

    def toggle_featured_juego(self, juego_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE juegos SET is_featured = CASE WHEN is_featured = 1 THEN 0 ELSE 1 END WHERE id = ?', (juego_id,))
            conn.commit()
            cursor.execute('SELECT is_featured FROM juegos WHERE id = ?', (juego_id,))
            row = cursor.fetchone()
            return row['is_featured'] if row else 0

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
            # Load tags once for the entire loop (avoids N+1 DB queries)
            tags = self.get_title_tags()

            for row in all_packs:
                pack_dict = dict(row)
                games = json.loads(pack_dict['games_json']) if pack_dict['games_json'] else []
                games = self._apply_tags(games, tags)
                pack_dict['games'] = games # parsed list for the UI
                pack_dict['manual_image_url'] = pack_dict.get('manual_image_url')
                
                # Auto-expire "Nuevo" tag after 48 hours
                if pack_dict.get('is_new') == 1 and pack_dict.get('created_at'):
                    try:
                        created = datetime.strptime(pack_dict['created_at'], '%Y-%m-%d %H:%M:%S')
                        if (datetime.now() - created).total_seconds() > 48 * 3600:
                            pack_dict['is_new'] = 0
                    except: pass
                
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

    # --- Title Tags CRUD (Unified: juego, dlc, hot) ---
    def get_title_tags(self, tag_filter=None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if tag_filter:
                cursor.execute('SELECT * FROM title_tags WHERE tag = ? ORDER BY keyword COLLATE NOCASE', (tag_filter,))
            else:
                cursor.execute('SELECT * FROM title_tags ORDER BY tag, keyword COLLATE NOCASE')
            return [dict(row) for row in cursor.fetchall()]

    def add_title_tag(self, keyword, tag):
        if tag not in ('juego', 'dlc', 'hot'):
            return False
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('INSERT INTO title_tags (keyword, tag) VALUES (?, ?)', (keyword.strip().lower(), tag))
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False  # Already exists

    def delete_title_tag(self, id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM title_tags WHERE id = ?', (id,))
            conn.commit()

    def update_title_tag(self, id, keyword, tag):
        if tag not in ('juego', 'dlc', 'hot'):
            return False
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('UPDATE title_tags SET keyword = ?, tag = ? WHERE id = ?', (keyword.strip().lower(), tag, id))
                conn.commit()
                return True
        except sqlite3.IntegrityError:
            return False


    def apply_title_tags(self, games_list):
        """Override is_dlc and add is_hot based on title_tags keywords."""
        tags = self.get_title_tags()
        return self._apply_tags(games_list, tags)

    def _apply_tags(self, games_list, tags):
        """Apply pre-loaded title tags to a list of games (avoids repeated DB queries)."""
        dlc_keywords = [t['keyword'] for t in tags if t['tag'] == 'dlc']
        hot_keywords = [t['keyword'] for t in tags if t['tag'] == 'hot']
        juego_keywords = [t['keyword'] for t in tags if t['tag'] == 'juego']

        for game in games_list:
            name_lower = game.get('name', game.get('titulo', '')).lower()
            name_norm = self._strip_accents(name_lower)

            forced_juego = any(self._strip_accents(kw.lower()) in name_norm for kw in juego_keywords)
            matched_dlc = any(self._strip_accents(kw.lower()) in name_norm for kw in dlc_keywords)
            matched_hot = any(self._strip_accents(kw.lower()) in name_norm for kw in hot_keywords)

            if forced_juego:
                game['is_dlc'] = False
                game['is_mixed'] = False
            elif matched_dlc:
                game['is_dlc'] = True
                game['is_mixed'] = '+' in name_lower and game.get('is_dlc', False)

            game['is_hot'] = matched_hot

        return games_list

    @staticmethod
    def _strip_accents(text):
        """Remove diacritics/accents from a string for accent-insensitive comparison."""
        nfkd = unicodedata.normalize('NFKD', text)
        return ''.join(c for c in nfkd if not unicodedata.combining(c))

    @staticmethod
    def generate_slug(titulo, plataforma=''):
        """Generate URL slug from title + platform, skipping platform if already in title."""
        import re
        def slugify(s):
            nfkd = unicodedata.normalize('NFKD', s.strip())
            s = ''.join(c for c in nfkd if not unicodedata.combining(c))
            s = s.lower()
            s = re.sub(r'[^a-z0-9\s-]', '', s)
            return re.sub(r'[\s-]+', '-', s).strip('-')

        title_slug = slugify(titulo)
        plat_slug = slugify(plataforma) if plataforma else ''
        if not plat_slug or plat_slug in title_slug:
            return title_slug
        return f"{title_slug}-{plat_slug}"

    # Sellable modalities used to disambiguate slugs when multiple game rows
    # share the same (titulo, plataforma) — e.g., Primaria PS5 vs Secundaria PS5.
    _PS_MODALITY_KEYS = (
        ('primaria_ps5',   'primaria-ps5'),
        ('secundaria_ps5', 'secundaria-ps5'),
        ('primaria_ps4',   'primaria-ps4'),
    )
    _NIN_MODALITY_KEYS = (
        ('primaria',       'primaria'),
        ('secundaria',     'secundaria'),
        ('codigo_digital', 'codigo-digital'),
        ('alquiler',       'alquiler'),
    )

    @staticmethod
    def _active_modalities(game):
        """Return the list of active sellable modality slugs for a game."""
        precios = game.get('precios') or {}
        plataforma = (game.get('plataforma') or '').upper()
        is_ps = any(tok in plataforma for tok in ('PS5', 'PS4', 'PLAYSTATION'))
        keys = Database._PS_MODALITY_KEYS if is_ps else Database._NIN_MODALITY_KEYS
        return [mod for k, mod in keys if precios.get(k) or precios.get(f'oferta_{k}')]

    @staticmethod
    def generate_game_slug(game):
        """Generate URL slug for a game, disambiguating rows that share the same
        (titulo, plataforma) by appending the modality when only one is active.
        Falls back to the legacy title+platform slug otherwise (backwards-compatible
        for games that bundle multiple modalities in a single row)."""
        base = Database.generate_slug(game.get('titulo', ''), game.get('plataforma', '') or '')
        active = Database._active_modalities(game)
        if len(active) == 1:
            return f"{base}-{active[0]}"
        return base

    # --- GAME BY SLUG ---
    def get_game_by_slug(self, slug):
        """Find a game whose generated slug matches the provided slug.
        Tries the modality-aware slug first, then the legacy title+platform slug
        for backwards compatibility with older URLs and ambiguous rows."""
        all_games = self.get_all_juegos()
        # First pass: exact modality-aware match
        for game in all_games:
            if self.generate_game_slug(game) == slug:
                return game
        # Fallback: legacy slug (title + platform). Only use as a last resort so
        # that ambiguous rows (same titulo+plataforma) still resolve to *something*.
        for game in all_games:
            if self.generate_slug(game['titulo'], game.get('plataforma', '')) == slug:
                return game
        return None

    # --- ORDERS CRUD ---
    def create_order(self, data):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO orders (game_id, game_titulo, game_plataforma, tipo_producto,
                                    buyer_email, buyer_phone, payment_method, payment_status,
                                    precio_base, precio_cobrado, surcharge, uala_order_uuid, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                data.get('game_id'),
                data['game_titulo'],
                data.get('game_plataforma'),
                data['tipo_producto'],
                data['buyer_email'],
                data.get('buyer_phone'),
                data['payment_method'],
                data.get('payment_status', 'pendiente'),
                data['precio_base'],
                data['precio_cobrado'],
                data.get('surcharge', 0),
                data.get('uala_order_uuid'),
                data.get('notes')
            ))
            conn.commit()
            return cursor.lastrowid

    def get_order(self, order_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_orders(self, status_filter=None, limit=100):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if status_filter and status_filter != 'todos':
                cursor.execute('SELECT * FROM orders WHERE payment_status = ? ORDER BY created_at DESC LIMIT ?', (status_filter, limit))
            else:
                cursor.execute('SELECT * FROM orders ORDER BY created_at DESC LIMIT ?', (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def update_order_status(self, order_id, status):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            delivered_at = now if status == 'entregado' else None
            cursor.execute('''
                UPDATE orders SET payment_status = ?, updated_at = ?, delivered_at = COALESCE(?, delivered_at)
                WHERE id = ?
            ''', (status, now, delivered_at, order_id))
            conn.commit()
            return cursor.rowcount > 0

    def update_order_comprobante(self, order_id, comprobante_ref=None, comprobante_file=None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('''
                UPDATE orders SET comprobante_ref = COALESCE(?, comprobante_ref),
                                  comprobante_file = COALESCE(?, comprobante_file),
                                  updated_at = ?
                WHERE id = ?
            ''', (comprobante_ref, comprobante_file, now, order_id))
            conn.commit()
            return cursor.rowcount > 0

    def update_order_uala(self, order_id, uala_uuid):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute('UPDATE orders SET uala_order_uuid = ?, updated_at = ? WHERE id = ?',
                           (uala_uuid, now, order_id))
            conn.commit()

    def find_order_by_uala_ref(self, external_ref):
        """Find order by external_reference (which is 'nez-{order_id}')."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # external_ref format: nez-123
            try:
                order_id = int(external_ref.split('-')[1])
            except (IndexError, ValueError):
                return None
            cursor.execute('SELECT * FROM orders WHERE id = ?', (order_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    # --- PS ACCOUNTS CRUD ---
    @staticmethod
    def _parse_game_ids(raw):
        """Parse game_ids JSON column -> list of ints."""
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [int(x) for x in parsed if x is not None]
        except Exception:
            pass
        return []

    def add_ps_account(self, email, password, keys_list, notes='', game_id=None, game_titulo=None,
                       primaria_total=1, primaria_ps4_total=1, secundaria_total=2, game_ids=None):
        """Add a PS account with 10 activation keys, linked to one or more games.
        game_ids: list of int game IDs. If None, falls back to [game_id].
        Default: 1 primaria PS5 + 1 primaria PS4 + 2 secundaria = 4 slots."""
        if game_ids is None:
            game_ids = [game_id] if game_id else []
        game_ids = [int(x) for x in game_ids if x is not None]
        primary_gid = game_ids[0] if game_ids else game_id
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO ps_accounts (email, password, activation_keys, notes, game_id, game_titulo,
                                         game_ids, primaria_total, primaria_ps4_total, secundaria_total)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (email, password, json.dumps(keys_list), notes, primary_gid, game_titulo,
                  json.dumps(game_ids), primaria_total, primaria_ps4_total, secundaria_total))
            conn.commit()
            return cursor.lastrowid

    def get_ps_accounts(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM ps_accounts ORDER BY added_at DESC')
            results = []
            for row in cursor.fetchall():
                d = dict(row)
                d['activation_keys_list'] = json.loads(d['activation_keys'])
                d['game_ids_list'] = self._parse_game_ids(d.get('game_ids'))
                # Get delivery log for this account
                cursor2 = conn.cursor()
                cursor2.execute('SELECT * FROM ps_delivery_log WHERE ps_account_id = ? ORDER BY key_index', (d['id'],))
                d['deliveries'] = [dict(r) for r in cursor2.fetchall()]
                # Fetch titles of linked games for convenience
                if d['game_ids_list']:
                    placeholders = ','.join('?' * len(d['game_ids_list']))
                    cursor2.execute(f"""SELECT id, titulo, plataforma,
                                          precio_primaria_ps5, oferta_primaria_ps5,
                                          precio_primaria_ps4, oferta_primaria_ps4,
                                          precio_secundaria_ps5, oferta_secundaria_ps5
                                        FROM juegos WHERE id IN ({placeholders})""", d['game_ids_list'])
                    linked = []
                    for r in cursor2.fetchall():
                        g = dict(r)
                        has_p5 = bool(g.get('precio_primaria_ps5') or g.get('oferta_primaria_ps5'))
                        has_p4 = bool(g.get('precio_primaria_ps4') or g.get('oferta_primaria_ps4'))
                        has_s5 = bool(g.get('precio_secundaria_ps5') or g.get('oferta_secundaria_ps5'))
                        active = sum([has_p5, has_p4, has_s5])
                        if active == 1:
                            if has_p5:    g['version_label'] = 'Primaria PS5'
                            elif has_p4:  g['version_label'] = 'Primaria PS4'
                            elif has_s5:  g['version_label'] = 'Secundaria PS5'
                        else:
                            plat = g.get('plataforma') or ''
                            g['version_label'] = 'PS5' if 'PS5' in plat else ('PS4' if 'PS4' in plat else plat)
                        linked.append(g)
                    d['linked_games'] = linked
                else:
                    d['linked_games'] = []
                results.append(d)
            return results

    def get_available_ps_key(self, game_id=None, sale_type='primaria'):
        """Get the next available activation key from the pool, filtered by game_id and sale type.
        Looks up accounts whose game_ids JSON contains the given game_id (or legacy game_id column match).
        sale_type: 'primaria' (PS5), 'primaria_ps4', or 'secundaria'.
        Returns (account_dict, key_index, activation_key) or (None, None, None) if no stock."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if sale_type == 'secundaria':
                slot_filter = 'secundaria_used < secundaria_total'
            elif sale_type == 'primaria_ps4':
                slot_filter = 'primaria_ps4_used < primaria_ps4_total'
            else:
                slot_filter = 'primaria_used < primaria_total'

            if game_id:
                cursor.execute(
                    f"SELECT * FROM ps_accounts WHERE status = 'disponible' AND keys_used < 10 AND {slot_filter} ORDER BY added_at ASC"
                )
                row = None
                for candidate in cursor.fetchall():
                    linked = self._parse_game_ids(candidate['game_ids'])
                    if not linked and candidate['game_id']:
                        linked = [candidate['game_id']]
                    if int(game_id) in linked:
                        row = candidate
                        break
            else:
                cursor.execute(f"SELECT * FROM ps_accounts WHERE status = 'disponible' AND keys_used < 10 AND {slot_filter} ORDER BY added_at ASC LIMIT 1")
                row = cursor.fetchone()

            if not row:
                return None, None, None
            
            account = dict(row)
            keys = json.loads(account['activation_keys'])
            key_index = account['keys_used']  # 0-indexed, next unused key
            activation_key = keys[key_index]
            
            # Increment keys_used + slot used
            new_keys_used = key_index + 1
            if sale_type == 'secundaria':
                new_val = account['secundaria_used'] + 1
                cursor.execute('UPDATE ps_accounts SET keys_used = ?, secundaria_used = ? WHERE id = ?',
                               (new_keys_used, new_val, account['id']))
            elif sale_type == 'primaria_ps4':
                new_val = account['primaria_ps4_used'] + 1
                cursor.execute('UPDATE ps_accounts SET keys_used = ?, primaria_ps4_used = ? WHERE id = ?',
                               (new_keys_used, new_val, account['id']))
            else:
                new_val = account['primaria_used'] + 1
                cursor.execute('UPDATE ps_accounts SET keys_used = ?, primaria_used = ? WHERE id = ?',
                               (new_keys_used, new_val, account['id']))
            
            # Check if account is fully exhausted (all slots filled or all keys used)
            cursor.execute('SELECT * FROM ps_accounts WHERE id = ?', (account['id'],))
            updated = dict(cursor.fetchone())
            all_slots_full = (updated['primaria_used'] >= updated['primaria_total'] and
                              updated.get('primaria_ps4_used', 0) >= updated.get('primaria_ps4_total', 1) and
                              updated['secundaria_used'] >= updated['secundaria_total'])
            all_keys_used = updated['keys_used'] >= 10
            if all_slots_full or all_keys_used:
                cursor.execute("UPDATE ps_accounts SET status = 'agotada' WHERE id = ?", (account['id'],))
            
            conn.commit()
            return account, key_index, activation_key

    def log_ps_delivery(self, ps_account_id, order_id, key_index, activation_key, sale_type='primaria'):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO ps_delivery_log (ps_account_id, order_id, key_index, activation_key, sale_type)
                VALUES (?, ?, ?, ?, ?)
            ''', (ps_account_id, order_id, key_index, activation_key, sale_type))
            conn.commit()

    def add_ps_slots(self, account_id, primaria_add=0, primaria_ps4_add=0, secundaria_add=0):
        """Add more primaria/primaria_ps4/secundaria slots to an existing account."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM ps_accounts WHERE id = ?', (account_id,))
            row = cursor.fetchone()
            if not row:
                return False
            account = dict(row)
            new_pri = account['primaria_total'] + primaria_add
            new_pri4 = account.get('primaria_ps4_total', 1) + primaria_ps4_add
            new_sec = account['secundaria_total'] + secundaria_add
            # If adding slots and account was agotada, reopen it
            new_status = account['status']
            has_open_slots = (account['primaria_used'] < new_pri or
                              account.get('primaria_ps4_used', 0) < new_pri4 or
                              account['secundaria_used'] < new_sec)
            if has_open_slots and account['keys_used'] < 10:
                new_status = 'disponible'
            cursor.execute('UPDATE ps_accounts SET primaria_total = ?, primaria_ps4_total = ?, secundaria_total = ?, status = ? WHERE id = ?',
                           (new_pri, new_pri4, new_sec, new_status, account_id))
            conn.commit()
            return True

    def delete_ps_account(self, account_id):
        """Delete a PS account only if no keys have been used."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT keys_used FROM ps_accounts WHERE id = ?', (account_id,))
            row = cursor.fetchone()
            if not row:
                return False
            if row['keys_used'] > 0:
                return False  # Cannot delete accounts with used keys
            cursor.execute('DELETE FROM ps_accounts WHERE id = ?', (account_id,))
            conn.commit()
            return True

    def update_ps_account(self, account_id, updates):
        """Update email, password, notes and/or linked game_ids of a PS account."""
        allowed = {'email', 'password', 'notes'}
        fields = {k: v for k, v in updates.items() if k in allowed}
        game_ids = updates.get('game_ids')
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if fields:
                set_clause = ', '.join(f'{k} = ?' for k in fields)
                values = list(fields.values()) + [account_id]
                cursor.execute(f'UPDATE ps_accounts SET {set_clause} WHERE id = ?', values)
            if game_ids is not None:
                clean_ids = [int(x) for x in game_ids if x is not None]
                primary_gid = clean_ids[0] if clean_ids else None
                # Look up primary title for legacy display column
                primary_titulo = None
                if primary_gid:
                    cursor.execute("SELECT titulo FROM juegos WHERE id = ?", (primary_gid,))
                    r = cursor.fetchone()
                    if r:
                        primary_titulo = r['titulo']
                cursor.execute(
                    "UPDATE ps_accounts SET game_ids = ?, game_id = ?, game_titulo = ? WHERE id = ?",
                    (json.dumps(clean_ids), primary_gid, primary_titulo, account_id)
                )
            conn.commit()
        return True

    def update_order_notes(self, order_id, notes):
        """Set notes on an order (used to store Nintendo account delivery data)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE orders SET notes = ? WHERE id = ?', (notes, order_id))
            conn.commit()

    def remap_order_game(self, order_id, new_game_id):
        """Point an existing order at a different (current) game.
        Useful when a game was split into multiple versions and old orders still
        reference the deleted/merged original. Also refreshes game_titulo and
        game_plataforma from the target game's current data."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT titulo, plataforma FROM juegos WHERE id = ?', (new_game_id,))
            row = cursor.fetchone()
            if not row:
                return False
            cursor.execute(
                'UPDATE orders SET game_id = ?, game_titulo = ?, game_plataforma = ? WHERE id = ?',
                (new_game_id, row['titulo'], row['plataforma'], order_id)
            )
            conn.commit()
            return True


    def get_ps_stock_count(self):
        """Count total available sales across all accounts."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""SELECT 
                SUM(primaria_total - primaria_used) as pri5_disp,
                SUM(primaria_ps4_total - primaria_ps4_used) as pri4_disp,
                SUM(secundaria_total - secundaria_used) as sec_disp
                FROM ps_accounts WHERE status = 'disponible'""")
            row = cursor.fetchone()
            pri5 = row['pri5_disp'] or 0
            pri4 = row['pri4_disp'] or 0
            sec = row['sec_disp'] or 0
            return {'primaria_ps5': pri5, 'primaria_ps4': pri4, 'secundaria': sec, 'total': pri5 + pri4 + sec}

    def check_ps_game_stock(self, game_id):
        """Check if a specific game has PS accounts with available slots.
        Returns dict with availability per sale type.
        Considers game_ids JSON membership (falls back to legacy game_id column)."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ps_accounts WHERE status = 'disponible' AND keys_used < 10")
            pri5 = pri4 = sec = 0
            for row in cursor.fetchall():
                linked = self._parse_game_ids(row['game_ids'])
                if not linked and row['game_id']:
                    linked = [row['game_id']]
                if int(game_id) not in linked:
                    continue
                if row['primaria_used'] < row['primaria_total']:
                    pri5 += 1
                if (row['primaria_ps4_used'] or 0) < (row['primaria_ps4_total'] or 1):
                    pri4 += 1
                if row['secundaria_used'] < row['secundaria_total']:
                    sec += 1
            return {
                'primaria': pri5 > 0,
                'primaria_ps4': pri4 > 0,
                'secundaria': sec > 0
            }

    # --- RECURRING EXPENSES CRUD ---
    def get_expenses(self, include_inactive=False):
        """Return all recurring expenses, ordered by day_of_month then name."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if include_inactive:
                cursor.execute(
                    'SELECT * FROM recurring_expenses ORDER BY day_of_month ASC, name COLLATE NOCASE ASC'
                )
            else:
                cursor.execute(
                    'SELECT * FROM recurring_expenses WHERE is_active = 1 '
                    'ORDER BY day_of_month ASC, name COLLATE NOCASE ASC'
                )
            return [dict(r) for r in cursor.fetchall()]

    @staticmethod
    def _validate_expense(data):
        """Normalize and validate expense input. Returns (clean_dict, error_or_None)."""
        name = (data.get('name') or '').strip()
        if not name:
            return None, 'name requerido'
        try:
            day = int(data.get('day_of_month'))
        except (TypeError, ValueError):
            return None, 'day_of_month debe ser un numero'
        if not 1 <= day <= 31:
            return None, 'day_of_month debe estar entre 1 y 31'
        try:
            amount = float(data.get('amount') or 0)
        except (TypeError, ValueError):
            return None, 'amount invalido'
        if amount < 0:
            return None, 'amount no puede ser negativo'
        currency = (data.get('currency') or 'ARS').upper().strip()
        if currency not in ('ARS', 'USD'):
            return None, 'currency debe ser ARS o USD'
        notes = (data.get('notes') or '').strip() or None
        is_active = 1 if data.get('is_active', 1) else 0
        return {
            'name': name,
            'day_of_month': day,
            'amount': amount,
            'currency': currency,
            'notes': notes,
            'is_active': is_active,
        }, None

    def add_expense(self, data):
        clean, err = self._validate_expense(data)
        if err:
            return None, err
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO recurring_expenses (name, day_of_month, amount, currency, notes, is_active) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (clean['name'], clean['day_of_month'], clean['amount'],
                 clean['currency'], clean['notes'], clean['is_active'])
            )
            conn.commit()
            return cursor.lastrowid, None

    def update_expense(self, expense_id, data):
        clean, err = self._validate_expense(data)
        if err:
            return False, err
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE recurring_expenses SET name=?, day_of_month=?, amount=?, '
                'currency=?, notes=?, is_active=? WHERE id=?',
                (clean['name'], clean['day_of_month'], clean['amount'],
                 clean['currency'], clean['notes'], clean['is_active'], expense_id)
            )
            conn.commit()
            return cursor.rowcount > 0, None

    def delete_expense(self, expense_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM recurring_expenses WHERE id=?', (expense_id,))
            conn.commit()
            return cursor.rowcount > 0

    # --- AMAZON JP TRACKER CRUD ---
    def get_amazon_jp_items(self, include_inactive=True):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if include_inactive:
                cursor.execute(
                    'SELECT * FROM amazon_jp_tracker ORDER BY is_active DESC, updated_at DESC, id DESC'
                )
            else:
                cursor.execute(
                    'SELECT * FROM amazon_jp_tracker WHERE is_active = 1 ORDER BY updated_at DESC, id DESC'
                )
            return [dict(r) for r in cursor.fetchall()]

    @staticmethod
    def _extract_asin_from_url(url):
        url = url or ''
        patterns = (
            r'/dp/([A-Z0-9]{10})',
            r'/gp/product/([A-Z0-9]{10})',
            r'/gp/aw/d/([A-Z0-9]{10})',
            r'/product/([A-Z0-9]{10})',
            r'[?&](?:asin|pd_rd_i)=([A-Z0-9]{10})',
        )
        for pat in patterns:
            m = re.search(pat, url, re.IGNORECASE)
            if m:
                return m.group(1).upper()
        return None

    @staticmethod
    def _validate_amazon_jp_item(data):
        name = (data.get('display_name_es') or '').strip()
        if not name:
            return None, 'display_name_es requerido'
        url = (data.get('amazon_url') or '').strip()
        if not url:
            return None, 'amazon_url requerido'
        valid_hosts = ('amazon.co.jp', 'amzn.asia', 'amzn.to')
        if not any(h in url.lower() for h in valid_hosts):
            return None, 'La URL debe ser de Amazon Japón (amazon.co.jp o amzn.asia)'
        is_active = 1 if data.get('is_active', 1) else 0
        return {
            'display_name_es': name,
            'amazon_url': url,
            'asin': Database._extract_asin_from_url(url),
            'is_active': is_active,
        }, None

    def add_amazon_jp_item(self, data):
        clean, err = self._validate_amazon_jp_item(data)
        if err:
            return None, err
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    'INSERT INTO amazon_jp_tracker (display_name_es, amazon_url, asin, is_active) VALUES (?, ?, ?, ?)',
                    (clean['display_name_es'], clean['amazon_url'], clean['asin'], clean['is_active'])
                )
                conn.commit()
                return cursor.lastrowid, None
            except sqlite3.IntegrityError:
                return None, 'Ya existe una publicación con esa URL'

    def update_amazon_jp_item(self, item_id, data):
        clean, err = self._validate_amazon_jp_item(data)
        if err:
            return False, err
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    'UPDATE amazon_jp_tracker SET display_name_es=?, amazon_url=?, asin=?, is_active=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                    (clean['display_name_es'], clean['amazon_url'], clean['asin'], clean['is_active'], item_id)
                )
                conn.commit()
                return cursor.rowcount > 0, None
            except sqlite3.IntegrityError:
                return False, 'Ya existe una publicación con esa URL'

    def delete_amazon_jp_item(self, item_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM amazon_jp_tracker WHERE id = ?', (item_id,))
            conn.commit()
            return cursor.rowcount > 0

    def get_amazon_jp_item(self, item_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM amazon_jp_tracker WHERE id = ?', (item_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_active_amazon_jp_items(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM amazon_jp_tracker WHERE is_active = 1 ORDER BY id DESC')
            return [dict(r) for r in cursor.fetchall()]

    def update_amazon_jp_snapshot(self, item_id, snapshot):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Check if manual override is active — if so, preserve manual prices
            row = cursor.execute(
                'SELECT price_manual_override, price_manual_jpy, list_manual_jpy FROM amazon_jp_tracker WHERE id = ?',
                (item_id,)
            ).fetchone()
            has_override = row and row['price_manual_override']
            if has_override:
                # Keep manual prices; still update image, title, status and last_checked
                cursor.execute(
                    '''
                    UPDATE amazon_jp_tracker
                    SET asin = COALESCE(?, asin),
                        image_url = COALESCE(?, image_url),
                        title_source = COALESCE(?, title_source),
                        last_checked_at = CURRENT_TIMESTAMP,
                        last_status = ?,
                        last_error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    ''',
                    (
                        snapshot.get('asin'),
                        snapshot.get('image_url'),
                        snapshot.get('title_source'),
                        snapshot.get('last_status', 'ok'),
                        snapshot.get('last_error'),
                        item_id
                    )
                )
            else:
                cursor.execute(
                    '''
                    UPDATE amazon_jp_tracker
                    SET asin = COALESCE(?, asin),
                        image_url = COALESCE(?, image_url),
                        title_source = COALESCE(?, title_source),
                        price_jpy = ?,
                        list_price_jpy = ?,
                        is_on_sale = ?,
                        price_usdt = ?,
                        price_ars = ?,
                        last_checked_at = CURRENT_TIMESTAMP,
                        last_status = ?,
                        last_error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    ''',
                    (
                        snapshot.get('asin'),
                        snapshot.get('image_url'),
                        snapshot.get('title_source'),
                        snapshot.get('price_jpy'),
                        snapshot.get('list_price_jpy'),
                        1 if snapshot.get('is_on_sale') else 0,
                        snapshot.get('price_usdt'),
                        snapshot.get('price_ars'),
                        snapshot.get('last_status', 'ok'),
                        snapshot.get('last_error'),
                        item_id
                    )
                )
            conn.commit()
            return cursor.rowcount > 0

    def set_amazon_jp_manual_price(self, item_id, price_jpy, list_price_jpy):
        """Set manual price override. Clears override when both prices are None."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            has_override = price_jpy is not None or list_price_jpy is not None
            # Recompute is_on_sale based on manual prices
            is_on_sale = 1 if (price_jpy and list_price_jpy and list_price_jpy > price_jpy) else 0
            cursor.execute(
                '''
                UPDATE amazon_jp_tracker
                SET price_manual_jpy = ?,
                    list_manual_jpy = ?,
                    price_manual_override = ?,
                    price_jpy = CASE WHEN ? IS NOT NULL THEN ? ELSE price_jpy END,
                    list_price_jpy = CASE WHEN ? IS NOT NULL THEN ? ELSE list_price_jpy END,
                    is_on_sale = ?,
                    last_status = CASE
                        WHEN ? = 1 AND ( ? IS NOT NULL OR ? IS NOT NULL ) THEN 'ok'
                        ELSE last_status
                    END,
                    last_error = CASE
                        WHEN ? = 1 AND ( ? IS NOT NULL OR ? IS NOT NULL ) THEN NULL
                        ELSE last_error
                    END,
                    last_checked_at = CASE
                        WHEN ? = 1 AND ( ? IS NOT NULL OR ? IS NOT NULL ) THEN CURRENT_TIMESTAMP
                        ELSE last_checked_at
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                ''',
                (
                    price_jpy, list_price_jpy,
                    1 if has_override else 0,
                    price_jpy, price_jpy,
                    list_price_jpy, list_price_jpy,
                    is_on_sale,
                    1 if has_override else 0, price_jpy, list_price_jpy,
                    1 if has_override else 0, price_jpy, list_price_jpy,
                    1 if has_override else 0, price_jpy, list_price_jpy,
                    item_id
                )
            )
            conn.commit()
            return cursor.rowcount > 0

    def set_amazon_jp_mirror_image(self, item_id, mirror_image_url=None, mirror_image_filename=None):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE amazon_jp_tracker
                SET mirror_image_url = ?,
                    mirror_image_filename = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                ''',
                (mirror_image_url, mirror_image_filename, item_id)
            )
            conn.commit()
            return cursor.rowcount > 0

    # --- NINTENDO MIRROR CUSTOM CRUD ---
    def get_nintendo_mirror_custom_items(self, include_inactive=False):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if include_inactive:
                cursor.execute('SELECT * FROM nintendo_mirror_custom ORDER BY updated_at DESC, id DESC')
            else:
                cursor.execute('SELECT * FROM nintendo_mirror_custom WHERE is_active = 1 ORDER BY updated_at DESC, id DESC')
            return [dict(r) for r in cursor.fetchall()]

    def add_nintendo_mirror_custom_item(self, data):
        title = (data.get('title') or '').strip()
        if not title:
            return None, 'title requerido'
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO nintendo_mirror_custom (
                    title, image_url, image_filename,
                    codigo_regular_ars, codigo_offer_ars,
                    primaria_regular_ars, primaria_offer_ars,
                    is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    title,
                    (data.get('image_url') or '').strip() or None,
                    data.get('image_filename'),
                    data.get('codigo_regular_ars'),
                    data.get('codigo_offer_ars'),
                    data.get('primaria_regular_ars'),
                    data.get('primaria_offer_ars'),
                    1 if data.get('is_active', 1) else 0,
                )
            )
            conn.commit()
            return cursor.lastrowid, None

    def delete_nintendo_mirror_custom_item(self, item_id):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM nintendo_mirror_custom WHERE id = ?', (item_id,))
            conn.commit()
            return cursor.rowcount > 0
