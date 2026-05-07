import threading
import asyncio
import os
import json
import math
import time
import logging
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, session, redirect, send_file, make_response

from scraper import NintendoScraper, AmazonJpPriceScraper
from database import Database
from uala_bis import UalaBis
from email_sender import send_ps_credentials
import requests
import uuid

logging.basicConfig(level=logging.INFO)

# --- App Setup ---
app = Flask(__name__)
# Dev secret, use env in prod
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'nez-juegos-v2-super-secret')
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Paths (absolute to avoid relative-path issues)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(BASE_DIR, 'ui')
UI_ADMIN_DIR = os.path.join(UI_DIR, 'admin')
VOLUME_PATH = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', BASE_DIR)
UPLOAD_FOLDER = os.path.join(VOLUME_PATH, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Admin Password
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')

# Init Database & Scraper
db = Database()
scraper = NintendoScraper(db)
amazon_jp_scraper = AmazonJpPriceScraper()

DESCUENTO_TRANSFERENCIA = float(os.getenv('DESCUENTO_TRANSFERENCIA', '0.32'))


# --- Asyncio Bridge ---
# Playwright needs its own loop in a background thread
loop = asyncio.new_event_loop()

def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

t = threading.Thread(target=start_background_loop, args=(loop,), daemon=True)
t.start()

def run_on_scraper_thread(coro):
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


# --- Auth Guard ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            if request.path.startswith('/api/'):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect('/admin/login')
        return f(*args, **kwargs)
    return decorated_function


# HTML routing is handled automatically by the fallback catch-all route at the bottom

@app.route('/admin/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Safely handle JSON (Fetch API) or Form Data (Classic HTML)
        data = request.json if request.is_json else request.form
        if data and data.get('password') == ADMIN_PASSWORD:
            session['is_admin'] = True
            if request.is_json:
                return jsonify({"status": "ok"})
            return redirect('/admin')
        
        if request.is_json:
            return jsonify({"error": "Invalid password"}), 401
        return "<h1>Acceso Denegado</h1><p>Contraseña Incorrecta</p><a href='/admin/login'>Volver</a>", 401
        
    if session.get('is_admin'): return redirect('/admin')
    return send_from_directory(UI_ADMIN_DIR, 'login.html')

@app.route('/admin/logout', methods=['POST'])
def logout():
    session.pop('is_admin', None)
    return jsonify({"status": "ok"})


# --- Public API Routes (Data Fetching) ---
@app.route('/api/config')
def get_config():
    """Return CMS homepage configuration"""
    return jsonify(db.get_all_config())

def _pack_global_discount_pct(cfg):
    """Integer 0–90: percent subtracted from public pack prices (DB unchanged)."""
    try:
        pct = int(float(str(cfg.get('pack_global_discount_pct') or 0).replace(',', '.')))
    except (ValueError, TypeError):
        pct = 0
    return max(0, min(90, pct))


def _inflate_price_max_for_pack_filter(price_max, pct):
    """Widen SQL price filter so packs that end up ≤ price_max after discount are not dropped."""
    if price_max is None or pct <= 0:
        return price_max
    factor = 1 - pct / 100.0
    if factor <= 0:
        return price_max
    return max(1, int(math.ceil(price_max / factor)))


def _apply_pack_global_discount(packs, pct):
    if pct <= 0:
        return packs
    factor = 1 - pct / 100.0
    for p in packs:
        orig = int(p.get('price_local') or 0)
        if orig <= 0:
            continue
        discounted = max(1, int(round(orig * factor)))
        if discounted < orig:
            p['price_local_original'] = orig
            p['price_local'] = discounted
    return packs


@app.route('/api/packs')
def search_packs():
    query = request.args.get('q', '')
    exclude = request.args.get('exclude', '')
    limit = int(request.args.get('limit', 500))
    price_max = request.args.get('price_max', type=int)
    dlc_only = request.args.get('dlc_only', 'false').lower() == 'true'
    featured = request.args.get('featured', 'false').lower() == 'true'

    cfg = db.get_all_config()
    discount_pct = _pack_global_discount_pct(cfg)
    is_admin = bool(session.get('is_admin'))

    if not is_admin and discount_pct > 0 and price_max is not None:
        price_max = _inflate_price_max_for_pack_filter(price_max, discount_pct)

    results = db.get_packs(query=query, exclude=exclude, price_max=price_max, dlc_only=dlc_only, featured_only=featured, limit=limit)

    if not is_admin:
        results = _apply_pack_global_discount(results, discount_pct)

    return jsonify({
        "results": results,
        "pack_global_discount_pct": discount_pct,
    })

@app.route('/api/packs/suggestions')
def pack_suggestions():
    q = request.args.get('q', '')
    return jsonify({"suggestions": db.get_game_name_suggestions(q)})

@app.route('/api/juegos')
def get_juegos():
    featured = request.args.get('featured', 'false').lower() == 'true'
    return jsonify({"results": db.get_all_juegos(featured_only=featured)})


# --- Google Merchant Center Feed ---

MODALITY_MAP = [
    # (precio_field,         oferta_field,              slug,             label)
    ('precio_primaria_ps5',  'oferta_primaria_ps5',     'primaria-ps5',   'Primaria PS5'),
    ('precio_secundaria_ps5','oferta_secundaria_ps5',   'secundaria-ps5', 'Secundaria PS5'),
    ('precio_primaria_ps4',  'oferta_primaria_ps4',     'primaria-ps4',   'Primaria PS4'),
    ('precio_primaria',      'oferta_primaria',         'primaria',       'Primaria'),
    ('precio_secundaria',    'oferta_secundaria',       'secundaria',     'Secundaria'),
    ('precio_codigo',        'oferta_codigo',           'codigo-digital', 'Codigo Digital'),
    ('precio_alquiler',      'oferta_alquiler',         'alquiler',       'Alquiler'),
    ('precio_eshop',         None,                      'eshop',          'eShop'),
]

DESCUENTO_TRANSFERENCIA = 0.32  # transfer price is 32% cheaper than card

def get_game_modalities(game):
    """Return list of active modalities FOR SALE for a game.
    Each entry: {slug, label, precio_transfer, precio_tarjeta, url}
    - DB stores the transfer (minimum) price as precio.
    - Card price = precio_transfer / (1 - DESCUENTO_TRANSFERENCIA).
    - precio_eshop is the PS Store / eShop REFERENCE price (for the struck-through UI),
      it's NOT a sale modality and is never emitted as a feed item.
    - PS games only emit PS modalities; Nintendo games only emit Nintendo modalities.
    """
    p = game.get('precios') or {}
    plataforma = (game.get('plataforma') or '').upper()
    is_ps = any(tok in plataforma for tok in ('PS5', 'PS4', 'PLAYSTATION'))

    # Prefer oferta (active sale) over precio (regular). Same logic the UI uses.
    def pr(key, oferta_key):
        return p.get(oferta_key) or p.get(key)

    if is_ps:
        candidates = [
            ('primaria-ps5',   'Primaria PS5',   pr('primaria_ps5',   'oferta_primaria_ps5')),
            ('secundaria-ps5', 'Secundaria PS5', pr('secundaria_ps5', 'oferta_secundaria_ps5')),
            ('primaria-ps4',   'Primaria PS4',   pr('primaria_ps4',   'oferta_primaria_ps4')),
        ]
    else:
        candidates = [
            ('primaria',       'Primaria',       pr('primaria',       'oferta_primaria')),
            ('secundaria',     'Secundaria',     pr('secundaria',     'oferta_secundaria')),
            ('codigo-digital', 'Codigo Digital', pr('codigo_digital', 'oferta_codigo_digital')),
            ('alquiler',       'Alquiler',       pr('alquiler',       'oferta_alquiler')),
        ]

    game_slug = db.generate_game_slug(game)
    modalities = []
    for mod_slug, label, precio_transfer in candidates:
        if not precio_transfer:
            continue
        precio_tarjeta = int(round(precio_transfer / (1 - DESCUENTO_TRANSFERENCIA)))
        modalities.append({
            'slug': mod_slug,
            'label': label,
            'precio_transfer': int(precio_transfer),
            'precio_tarjeta': precio_tarjeta,
            'url': f"{SITE_URL}/juegos/{game_slug}",
            'feed_id': f"nez-{game['id']}-{mod_slug}",
        })
    return modalities


def _xml_escape(s):
    """Escape characters that are invalid in XML text/attribute values."""
    return (str(s)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


@app.route('/google-feed.xml')
def google_merchant_feed():
    """Google Merchant Center product feed (RSS 2.0 / Google Base format).
    One item per active modality per game.
    Prices: g:price = card price (tachado), g:sale_price = transfer price (~32% OFF).
    """
    games = db.get_all_juegos()
    items_xml = []
    for game in games:
        modalities = get_game_modalities(game)
        if not modalities:
            continue
        titulo = game.get('titulo', '')
        if game.get('imagen_filename'):
            from urllib.parse import quote
            safe_name = quote(game['imagen_filename'], safe='')
            image_url = f"{SITE_URL}/uploads/{safe_name}"
        else:
            image_url = f"{SITE_URL}/nez-logo.jpg"

        for mod in modalities:
            item = f"""    <item>
      <g:id>{_xml_escape(mod['feed_id'])}</g:id>
      <g:title>{_xml_escape(titulo + ' - ' + mod['label'])}</g:title>
      <g:description>Juego digital para {_xml_escape(mod['label'])}. Entrega inmediata. Hasta 6 cuotas sin interes con Uala Bis.</g:description>
      <g:link>{_xml_escape(mod['url'])}</g:link>
      <g:image_link>{_xml_escape(image_url)}</g:image_link>
      <g:price>{mod['precio_tarjeta']}.00 ARS</g:price>
      <g:sale_price>{mod['precio_transfer']}.00 ARS</g:sale_price>
      <g:availability>in_stock</g:availability>
      <g:condition>new</g:condition>
      <g:brand>Nez Juegos</g:brand>
      <g:google_product_category>471</g:google_product_category>
      <g:identifier_exists>false</g:identifier_exists>
    </item>"""
            items_xml.append(item)

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
  <channel>
    <title>Nez Juegos</title>
    <link>{SITE_URL}</link>
    <description>Juegos digitales Nintendo Switch y PlayStation con entrega inmediata.</description>
{chr(10).join(items_xml)}
  </channel>
</rss>"""

    from flask import Response
    return Response(feed, mimetype='application/xml; charset=utf-8')

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    if filename.startswith('comprobante_') and not session.get('is_admin'):
        return jsonify({"error": "Unauthorized"}), 401
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route('/gtm.js')
def serve_gtm():
    """Serve GTM initialization script"""
    return send_from_directory(UI_DIR, 'gtm.js')


# --- Admin API Routes (CMS config) ---
@app.route('/api/admin/config', methods=['POST'])
@admin_required
def save_config():
    if request.content_type and request.content_type.startswith('multipart/form-data'):
        data = dict(request.form)
        for key in ['file_img_juegos', 'file_img_packs']:
            file = request.files.get(key)
            if file and file.filename:
                filename = f"config_{key}_{int(time.time())}_{file.filename}"
                file.save(os.path.join(UPLOAD_FOLDER, filename))
                # Map the file upload to the corresponding config key
                db_key = 'img_juegos' if key == 'file_img_juegos' else 'img_packs'
                data[db_key] = filename
    else:
        data = request.json
        
    for key, value in data.items():
        db.update_config(key, value)
    return jsonify({"status": "ok"})


# --- Admin API Routes (Individual Games) ---
@app.route('/api/admin/juegos', methods=['POST'])
@admin_required
def create_juego():
    # Supports JSON or Form Data (if file upload is included)
    if request.content_type and request.content_type.startswith('multipart/form-data'):
        data = dict(request.form)
        file = request.files.get('image')
        if file:
            filename = f"game_{int(time.time())}_{file.filename}"
            file.save(os.path.join(UPLOAD_FOLDER, filename))
            data['imagen_filename'] = filename
    else:
        data = request.json
        
    new_id = db.create_juego(data)
    return jsonify({"status": "ok", "id": new_id})

@app.route('/api/admin/juegos/bulk-delete', methods=['POST'])
@admin_required
def bulk_delete_juegos():
    ids = request.json.get('ids', [])
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "ids requeridos"}), 400
    deleted = 0
    for juego_id in ids:
        if isinstance(juego_id, int):
            db.delete_juego(juego_id)
            deleted += 1
    return jsonify({"status": "ok", "deleted": deleted})

@app.route('/api/admin/juegos/<int:juego_id>', methods=['PUT', 'DELETE'])
@admin_required
def manage_juego(juego_id):
    if request.method == 'DELETE':
        db.delete_juego(juego_id)
        return jsonify({"status": "ok"})
    else:
        # PUT supporting both JSON and multipart/form-data
        if request.content_type and request.content_type.startswith('multipart/form-data'):
            data = dict(request.form)
            file = request.files.get('image')
            if file:
                filename = f"game_{int(time.time())}_{file.filename}"
                file.save(os.path.join(UPLOAD_FOLDER, filename))
                data['imagen_filename'] = filename
        else:
            data = request.json
            
        db.update_juego(juego_id, data)
        return jsonify({"status": "ok"})

@app.route('/api/admin/juegos/<int:juego_id>/toggle_featured', methods=['PATCH'])
@admin_required
def toggle_featured_juego(juego_id):
    new_val = db.toggle_featured_juego(juego_id)
    return jsonify({"status": "ok", "is_featured": new_val})


# --- Admin API Routes (Scraping & Telegram Packs) ---
# Scrape tasks run in background to avoid HTTP timeout on Railway
scrape_status = {"running": False, "result": None, "error": None, "action": None}

def _run_scrape_bg(coro, action_name):
    """Run a scrape coroutine in the background thread, updating scrape_status."""
    global scrape_status
    scrape_status = {"running": True, "result": None, "error": None, "action": action_name}
    
    def callback(future):
        global scrape_status
        try:
            result = future.result()
            scrape_status = {"running": False, "result": result, "error": None, "action": action_name}
        except Exception as e:
            scrape_status = {"running": False, "result": None, "error": str(e), "action": action_name}
    
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    future.add_done_callback(callback)

@app.route('/api/admin/scrape/status', methods=['GET'])
@admin_required
def api_scrape_status():
    status_data = scrape_status.copy()
    if hasattr(scraper, 'progress'):
        status_data['progress'] = scraper.progress
    return jsonify(status_data)

@app.route('/api/admin/scrape/today', methods=['POST'])
@admin_required
def api_scrape_today():
    if scrape_status.get('running'):
        return jsonify({"error": "Ya hay un scrape en ejecución"}), 409
    _run_scrape_bg(scraper.scrape_today(), 'scrape_today')
    return jsonify({"status": "started", "action": "scrape_today"})

@app.route('/api/admin/scrape/full', methods=['POST'])
@admin_required
def api_scrape_full():
    if scrape_status.get('running'):
        return jsonify({"error": "Ya hay un scrape en ejecución"}), 409
    _run_scrape_bg(scraper.scrape_full(1000), 'scrape_full')
    return jsonify({"status": "started", "action": "scrape_full"})

@app.route('/api/admin/scrape/verify', methods=['POST'])
@admin_required
def api_verify_deleted():
    if scrape_status.get('running'):
        return jsonify({"error": "Ya hay un scrape en ejecución"}), 409
    _run_scrape_bg(scraper.verify_deleted(), 'verify_deleted')
    return jsonify({"status": "started", "action": "verify_deleted"})

@app.route('/api/admin/packs/<pack_id>', methods=['DELETE'])
@admin_required
def manual_delete_pack(pack_id):
    """Admin clicked 'Delete' -> Prevent scraper from ever re-adding it"""
    db.mark_pack_deleted(pack_id, manual=True)
    return jsonify({"status": "ok"})

@app.route('/api/admin/packs/<pack_id>/toggle_featured', methods=['POST'])
@admin_required
def toggle_pack_featured(pack_id):
    """Admin clicked 'Destacar' -> toggles the is_featured flag (max 6)"""
    # Accept optional force parameter from JSON body
    force = request.json.get('force') if request.is_json else None
    success = db.toggle_pack_featured(pack_id, force=force)
    if success:
        return jsonify({"status": "ok"})
    else:
        return jsonify({"error": "No se pudo destacar. El límite de 6 packs ha sido alcanzado o el pack no existe."}), 400

@app.route('/api/admin/packs/manual', methods=['POST'])
@admin_required
def create_manual_pack():
    """Admin form to manually add a pack."""
    data = request.json
    if not data or not data.get('games_text') or not data.get('price_local'):
        return jsonify({"error": "Faltan datos obligatorios (juegos o precio)."}), 400
        
    games_lines = [g.strip() for g in data['games_text'].split('\n') if g.strip()]
    games = []
    
    # Very simple parser for manual input: if it has +, assume mixed.
    for line in games_lines:
        is_mixed = '+' in line
        games.append({
            "name": line,
            "is_dlc": False,
            "is_mixed": is_mixed
        })
        
    pack_data = {
        "raw_text": data['games_text'], # fallback
        "games": games,
        "price_usd": 0,
        "price_local": int(data['price_local']),
        "manual_image_url": data.get('image_url', '').strip()
    }
    
    try:
        new_id = db.insert_manual_pack(pack_data)
        return jsonify({"status": "ok", "id": new_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/ui/qr_login.png')
def serve_qr():
    """Serve QR with no-cache headers so browser always fetches fresh copy."""
    qr_path = os.path.join(UI_DIR, 'qr_login.png')
    if not os.path.exists(qr_path):
        return '', 404
    response = make_response(send_file(qr_path, mimetype='image/png'))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response

@app.route('/api/admin/telegram/status')
def get_telegram_status():
    return jsonify({"telegram_connected": getattr(scraper, 'telegram_connected', False)})

@app.route('/api/admin/telegram/refresh_qr', methods=['POST'])
@admin_required
def api_refresh_qr():
    success = run_on_scraper_thread(scraper.refresh_qr())
    return jsonify({"status": "ok", "telegram_connected": success})

# --- Title Tags API (Unified: juego, dlc, hot) ---

@app.route('/api/admin/title_tags', methods=['GET', 'POST'])
@admin_required
def api_admin_title_tags():
    if request.method == 'GET':
        return jsonify(db.get_title_tags())
    else:
        data = request.json
        if not data or not data.get('keyword') or not data.get('tag'):
            return jsonify({"error": "Keyword y tag requeridos"}), 400
        
        success = db.add_title_tag(data['keyword'], data['tag'])
        if success:
            return jsonify({"status": "ok"})
        else:
            return jsonify({"error": "Ya existe esa combinación o tag inválido"}), 400

@app.route('/api/admin/title_tags/<int:id>', methods=['PUT', 'DELETE'])
@admin_required
def api_admin_manage_title_tag(id):
    if request.method == 'DELETE':
        db.delete_title_tag(id)
        return jsonify({"status": "ok"})
    else:
        data = request.json
        if not data or not data.get('keyword') or not data.get('tag'):
            return jsonify({"error": "Keyword y tag requeridos"}), 400
        success = db.update_title_tag(id, data['keyword'], data['tag'])
        if success:
            return jsonify({"status": "ok"})
        else:
            return jsonify({"error": "Error de base de datos o tag inválido"}), 400

@app.route('/api/admin/title_tags/bulk', methods=['POST'])
@admin_required
def api_admin_bulk_title_tags():
    data = request.json
    keyword = data.get('keyword')
    tags = data.get('tags', [])
    if not keyword:
        return jsonify({"error": "Keyword requerido"}), 400
    
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM title_tags WHERE LOWER(keyword) = ?', (keyword.strip().lower(),))
        for t in tags:
            if t in ('juego', 'dlc', 'hot'):
                try:
                    cursor.execute('INSERT INTO title_tags (keyword, tag) VALUES (?, ?)', (keyword.strip().lower(), t))
                except:
                    pass
        conn.commit()
    return jsonify({"status": "ok"})

@app.route('/api/admin/title_tags/by_keyword', methods=['DELETE'])
@admin_required
def api_admin_delete_title_tags_by_keyword():
    keyword = request.json.get('keyword')
    if keyword:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM title_tags WHERE LOWER(keyword) = ?', (keyword.strip().lower(),))
            conn.commit()
    return jsonify({"status": "ok"})

@app.route('/api/public/title_tags', methods=['GET'])
def api_public_title_tags():
    # Public endpoint so frontend can know which titles are hot
    tags = db.get_title_tags(tag_filter='hot')
    return jsonify([t['keyword'] for t in tags])


# --- Public Checkout API ---

@app.route('/api/games/<slug>')
def api_get_game_by_slug(slug):
    """Get game data by URL slug."""
    game = db.get_game_by_slug(slug)
    if not game:
        return jsonify({"error": "Juego no encontrado"}), 404
    # Add slug to response
    game['slug'] = db.generate_game_slug(game)
    # For PS games, include stock availability per sale type
    plat = (game.get('plataforma') or '').upper()
    if 'PS4' in plat or 'PS5' in plat or 'PLAYSTATION' in plat:
        game['ps_stock'] = db.check_ps_game_stock(game['id'])
    return jsonify(game)

@app.route('/api/orders', methods=['POST'])
def api_create_order():
    """Create a new order from checkout."""
    data = request.json
    if not data:
        return jsonify({"error": "Datos requeridos"}), 400
    
    required = ['game_titulo', 'tipo_producto', 'payment_method', 'precio_base', 'precio_cobrado']
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Campo requerido: {field}"}), 400
    
    # Email required only for PS (auto email delivery); Nintendo uses WhatsApp
    plat = (data.get('game_plataforma') or '').upper()
    is_ps = 'PS4' in plat or 'PS5' in plat or 'PLAYSTATION' in plat
    if is_ps and not data.get('buyer_email'):
        return jsonify({"error": "El email es requerido para compras de PlayStation"}), 400
    
    # Validate payment method
    if data['payment_method'] not in ('transferencia', 'uala', 'binance'):
        return jsonify({"error": "Método de pago inválido"}), 400
    
    order_id = db.create_order(data)
    return jsonify({"status": "ok", "order_id": order_id})

@app.route('/api/orders/<int:order_id>/comprobante', methods=['POST'])
def api_upload_comprobante(order_id):
    """Upload payment receipt (image/PDF) for a transfer order."""
    order = db.get_order(order_id)
    if not order:
        return jsonify({"error": "Pedido no encontrado"}), 404
    
    comprobante_ref = request.form.get('comprobante_ref')
    comprobante_file = None
    
    file = request.files.get('comprobante')
    if file and file.filename:
        # Validate file size (5MB max)
        file.seek(0, 2)
        size = file.tell()
        file.seek(0)
        if size > 5 * 1024 * 1024:
            return jsonify({"error": "Archivo demasiado grande (máx 5MB)"}), 400
        
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ('.jpg', '.jpeg', '.png', '.pdf', '.webp'):
            return jsonify({"error": "Formato no permitido. Usá JPG, PNG, PDF o WEBP"}), 400
        
        filename = f"comprobante_{order_id}_{int(time.time())}{ext}"
        file.save(os.path.join(UPLOAD_FOLDER, filename))
        comprobante_file = filename
    
    if not comprobante_ref and not comprobante_file:
        return jsonify({"error": "Debés enviar al menos un ID de comprobante o un archivo"}), 400
    
    db.update_order_comprobante(order_id, comprobante_ref, comprobante_file)
    return jsonify({"status": "ok"})

@app.route('/api/orders/<int:order_id>/status')
def api_order_status(order_id):
    """Public endpoint to check order status."""
    order = db.get_order(order_id)
    if not order:
        return jsonify({"error": "Pedido no encontrado"}), 404
    return jsonify({
        "order_id": order['id'],
        "status": order['payment_status'],
        "game": order['game_titulo'],
        "tipo": order['tipo_producto'],
        "payment_method": order['payment_method'],
        "created_at": order['created_at']
    })


# --- Ualá Bis Payment API ---

uala = UalaBis()

@app.route('/api/uala/create', methods=['POST'])
def api_uala_create():
    """Create a Ualá Bis checkout. Creates the order and returns the payment link.
    Frontend sends game data + buyer info, and gets back a redirect link.
    Order is created here so no phantom orders exist before payment."""
    data = request.json
    if not data:
        return jsonify({"error": "Datos requeridos"}), 400
    
    # Accept either an existing order_id OR full order data
    if data.get('order_id'):
        order = db.get_order(data['order_id'])
        if not order:
            return jsonify({"error": "Pedido no encontrado"}), 404
        order_id = order['id']
    else:
        # Create order from submitted data
        required = ['game_id', 'game_titulo', 'tipo_producto', 'buyer_email', 'precio_base', 'precio_cobrado']
        for field in required:
            if not data.get(field):
                return jsonify({"error": f"Campo requerido: {field}"}), 400
        
        order_id = db.create_order({
            'game_id': data['game_id'],
            'game_titulo': data['game_titulo'],
            'game_plataforma': data.get('game_plataforma', ''),
            'tipo_producto': data['tipo_producto'],
            'buyer_email': data['buyer_email'],
            'buyer_phone': data.get('buyer_phone'),
            'payment_method': 'uala',
            'precio_base': data['precio_base'],
            'precio_cobrado': data['precio_cobrado'],
            'surcharge': data.get('surcharge', 0)
        })
        order = db.get_order(order_id)
    
    external_ref = f"nez-{order_id}"
    base_url = request.url_root.rstrip('/')
    # Use X-Forwarded headers if behind proxy
    if request.headers.get('X-Forwarded-Proto') == 'https':
        base_url = base_url.replace('http://', 'https://')
    
    try:
        result = uala.create_checkout(
            amount=order['precio_cobrado'],
            description=f"Nez Juegos: {order['game_titulo']} ({order['tipo_producto']})",
            external_ref=external_ref,
            base_url=base_url
        )
        
        # Save Ualá UUID in order
        db.update_order_uala(order_id, result.get('uuid'))
        
        return jsonify({
            "status": "ok",
            "order_id": order_id,
            "checkout_link": result['links']['checkout_link']
        })
    except Exception as e:
        logging.error(f"Ualá Bis create error: {e}")
        return jsonify({"error": "Error al crear el pago. Intentá nuevamente."}), 500

@app.route('/api/uala/webhook', methods=['POST'])
def api_uala_webhook():
    """Receive payment notification from Ualá Bis.
    Expected payload: { uuid, external_reference, status, created_date, api_version }
    Must respond 200 or Ualá retries 3 more times.
    
    Ualá Bis does not provide a webhook secret, so we verify the payment
    by querying the Ualá Bis API directly with the UUID before processing.
    """
    data = request.json
    if not data:
        return '', 200

    logging.info(f"Ualá webhook received: {json.dumps(data)}")

    uala_uuid = data.get('uuid', '')
    external_ref = data.get('external_reference', '')
    status_from_payload = data.get('status', '').upper()

    order = db.find_order_by_uala_ref(external_ref)
    if not order:
        logging.warning(f"Ualá webhook: order not found for ref {external_ref}")
        return '', 200

    # Verify payment by querying Ualá Bis API directly (don't trust payload alone)
    verified_status = status_from_payload
    if uala_uuid:
        try:
            token = uala.get_token()
            resp = requests.get(
                f"{uala.checkout_url}/{uala_uuid}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10
            )
            if resp.status_code == 200:
                verified_status = resp.json().get('status', '').upper()
                logging.info(f"Ualá webhook: verified status={verified_status} for uuid={uala_uuid}")
            else:
                logging.warning(f"Ualá webhook: could not verify UUID {uala_uuid}, using payload status")
        except Exception as e:
            logging.warning(f"Ualá webhook: verification request failed ({e}), using payload status")

    if verified_status == 'APPROVED':
        plat = (order.get('game_plataforma') or '').upper()
        is_ps = 'PS4' in plat or 'PS5' in plat or 'PLAYSTATION' in plat
        if is_ps:
            _deliver_ps_order(order['id'])
        else:
            db.update_order_status(order['id'], 'aprobado')
    elif verified_status in ('DECLINED', 'REJECTED'):
        db.update_order_status(order['id'], 'rechazado')

    return '', 200

@app.route('/api/pricing')
def api_pricing():
    """Calculate card price and USDT from the transfer (minimum) price stored in DB."""
    precio_transfer = request.args.get('precio', type=int)
    if not precio_transfer:
        return jsonify({"error": "precio requerido"}), 400
    precio_tarjeta = int(precio_transfer / (1 - DESCUENTO_TRANSFERENCIA))

    cfg = db.get_all_config()
    try:
        usdt_rate = float(cfg.get('usdt_rate', '1440') or '1440')
    except ValueError:
        usdt_rate = 1440.0
    precio_usdt = round(precio_transfer / usdt_rate, 2) if usdt_rate > 0 else 0

    return jsonify({
        "precio_transferencia": precio_transfer,
        "precio_tarjeta": precio_tarjeta,
        "precio_usdt": precio_usdt,
        "usdt_rate": usdt_rate,
        "descuento_transfer_pct": int(DESCUENTO_TRANSFERENCIA * 100)
    })


@app.route('/mp/success')
@app.route('/mp/pending')
@app.route('/mp/failure')
def mp_legacy_redirect():
    """Old MercadoPago return URLs — método descontinuado."""
    return redirect('/')


def _ps_stock_diagnostic(game_id, sale_type):
    """Return a human-readable explanation of why no key was available for
    (game_id, sale_type). Used by the admin UI when a delivery fails."""
    accounts = db.get_ps_accounts()
    linked = [a for a in accounts if game_id in (a.get('game_ids_list') or [])]
    if not linked:
        return "Ninguna cuenta PS está vinculada a este juego. Abrí una cuenta en 'Cuentas PS' y vinculala."

    slot_cols = {
        'secundaria': ('secundaria_used', 'secundaria_total'),
        'primaria_ps4': ('primaria_ps4_used', 'primaria_ps4_total'),
        'primaria': ('primaria_used', 'primaria_total'),
    }
    used_col, total_col = slot_cols.get(sale_type, slot_cols['primaria'])
    label = {'primaria': 'Primaria PS5', 'primaria_ps4': 'Primaria PS4', 'secundaria': 'Secundaria'}[sale_type]

    usable = [a for a in linked
              if a['status'] == 'disponible' and a['keys_used'] < 10
              and (a.get(used_col) or 0) < (a.get(total_col) or 0)]
    if usable:
        return None  # there IS stock; caller shouldn't be here

    details = []
    for a in linked:
        used = a.get(used_col) or 0
        total = a.get(total_col) or 0
        reasons = []
        if a['status'] != 'disponible':
            reasons.append(f"cuenta {a['status']}")
        if a['keys_used'] >= 10:
            reasons.append("10/10 keys usadas")
        if used >= total:
            reasons.append(f"slot {label} lleno ({used}/{total})")
        details.append(f"{a['email']}: {', '.join(reasons) or 'ok'}")

    return (f"No hay slot '{label}' libre en las cuentas vinculadas a este juego. "
            f"Podés usar '+ Ampliar' en la cuenta para agregar otro slot, o vincular una cuenta nueva. "
            f"Detalle: {' | '.join(details)}")


def _deliver_ps_order(order_id):
    """Internal: Deliver PS credentials via email."""
    order = db.get_order(order_id)
    if not order:
        return False
    
    # Determine sale type from tipo_producto
    tipo = (order.get('tipo_producto') or '').lower()
    if 'secundaria' in tipo:
        sale_type = 'secundaria'
    elif 'primaria' in tipo and 'ps4' in tipo:
        sale_type = 'primaria_ps4'
    else:
        sale_type = 'primaria'
    
    account, key_index, activation_key = db.get_available_ps_key(
        game_id=order.get('game_id'), sale_type=sale_type
    )
    if not account:
        reason = _ps_stock_diagnostic(order.get('game_id'), sale_type)
        logging.warning(f"No PS stock for order {order_id} (type={sale_type}): {reason}")
        db.update_order_status(order_id, 'pendiente_stock')
        _deliver_ps_order.last_reason = reason
        return False
    
    success = send_ps_credentials(
        to_email=order['buyer_email'],
        game_name=order['game_titulo'],
        account_email=account['email'],
        account_password=account['password'],
        activation_key=activation_key,
        sale_type=sale_type
    )
    
    if success:
        db.log_ps_delivery(account['id'], order_id, key_index, activation_key, sale_type=sale_type)
        db.update_order_status(order_id, 'entregado')
        return True
    else:
        logging.error(f"Email send failed for order {order_id}")
        db.update_order_status(order_id, 'error_email')
        return False


# --- Admin: Orders ---

@app.route('/api/admin/orders')
@admin_required
def api_admin_orders():
    status_filter = request.args.get('status', 'todos')
    orders = db.get_orders(status_filter=status_filter)
    return jsonify({"orders": orders})

@app.route('/api/admin/orders/<int:order_id>')
@admin_required
def api_admin_order_detail(order_id):
    order = db.get_order(order_id)
    if not order:
        return jsonify({"error": "Pedido no encontrado"}), 404
    return jsonify(order)

@app.route('/api/admin/orders/<int:order_id>/approve', methods=['POST'])
@admin_required
def api_admin_approve_order(order_id):
    """Approve a transfer order. If PS, auto-deliver."""
    order = db.get_order(order_id)
    if not order:
        return jsonify({"error": "Pedido no encontrado"}), 404
    if order['payment_status'] not in ('pendiente', 'pendiente_stock'):
        return jsonify({"error": f"No se puede aprobar un pedido con estado '{order['payment_status']}'"}), 400
    
    plat = (order.get('game_plataforma') or '').upper()
    is_ps = 'PS4' in plat or 'PS5' in plat or 'PLAYSTATION' in plat
    
    if is_ps:
        success = _deliver_ps_order(order_id)
        if success:
            return jsonify({"status": "ok", "message": "Pedido aprobado y credenciales enviadas por email."})
        else:
            return jsonify({"error": "Error en la entrega automática. Revisá el stock de cuentas PS."}), 500
    else:
        # Nintendo: mark as approved, admin contacts via WhatsApp manually
        db.update_order_status(order_id, 'aprobado')
        phone = order.get('buyer_phone', 'No proporcionado')
        return jsonify({"status": "ok", "message": f"Pedido aprobado. Contactar al cliente por WhatsApp: {phone}"})

@app.route('/api/admin/orders/<int:order_id>/reject', methods=['POST'])
@admin_required
def api_admin_reject_order(order_id):
    db.update_order_status(order_id, 'rechazado')
    return jsonify({"status": "ok"})

@app.route('/api/admin/orders/<int:order_id>/deliver', methods=['POST'])
@admin_required
def api_admin_manual_deliver(order_id):
    """Mark a Nintendo order as delivered (after WhatsApp contact)."""
    db.update_order_status(order_id, 'entregado')
    return jsonify({"status": "ok"})

@app.route('/api/admin/orders/<int:order_id>/retry-delivery', methods=['POST'])
@admin_required
def api_admin_retry_delivery(order_id):
    """Retry PS delivery for orders stuck in pendiente_stock (after admin adds account)."""
    order = db.get_order(order_id)
    if not order:
        return jsonify({"error": "Pedido no encontrado"}), 404
    if order['payment_status'] != 'pendiente_stock':
        return jsonify({"error": f"Solo se puede reintentar pedidos con estado 'pendiente_stock', este tiene '{order['payment_status']}'"}), 400
    success = _deliver_ps_order(order_id)
    if success:
        return jsonify({"status": "ok", "message": "Credenciales enviadas por email exitosamente."})
    else:
        reason = getattr(_deliver_ps_order, 'last_reason', None) or "Aún no hay stock disponible para este juego/tipo."
        return jsonify({"error": reason}), 400


@app.route('/api/admin/orders/<int:order_id>/remap-game', methods=['POST'])
@admin_required
def api_admin_remap_order_game(order_id):
    """Point an old order at a different (current) game_id — useful for orders
    placed before a game was split into multiple versions. Also triggers a
    delivery retry when the order is PS and in pendiente/pendiente_stock."""
    data = request.json or {}
    new_game_id = data.get('game_id')
    if not new_game_id:
        return jsonify({"error": "game_id requerido"}), 400

    order = db.get_order(order_id)
    if not order:
        return jsonify({"error": "Pedido no encontrado"}), 404

    ok = db.remap_order_game(order_id, int(new_game_id))
    if not ok:
        return jsonify({"error": "Juego destino no existe"}), 400

    # Auto-retry delivery for PS orders waiting on stock
    updated = db.get_order(order_id)
    plat = (updated.get('game_plataforma') or '').upper()
    is_ps = 'PS4' in plat or 'PS5' in plat or 'PLAYSTATION' in plat
    retried = False
    if is_ps and updated['payment_status'] in ('pendiente_stock', 'aprobado'):
        retried = _deliver_ps_order(order_id)

    msg = f"Pedido remapeado a '{updated['game_titulo']}'"
    if retried:
        msg += " y credenciales enviadas por email."
    elif is_ps and updated['payment_status'] == 'pendiente_stock':
        reason = getattr(_deliver_ps_order, 'last_reason', None)
        msg += f". Sin stock: {reason}" if reason else ". Sin stock disponible."
    return jsonify({"status": "ok", "message": msg, "delivered": retried})


@app.route('/api/admin/orders/<int:order_id>/nintendo-account', methods=['POST'])
@admin_required
def api_admin_nintendo_account(order_id):
    """Record Nintendo delivery account data on an order."""
    order = db.get_order(order_id)
    if not order:
        return jsonify({"error": "Pedido no encontrado"}), 404
    data = request.json or {}
    account_data = data.get('account_data', '').strip()
    if not account_data:
        return jsonify({"error": "account_data requerido"}), 400
    db.update_order_notes(order_id, f"Cuenta Nintendo: {account_data}")
    db.update_order_status(order_id, 'entregado')
    return jsonify({"status": "ok"})


# --- Admin: Recurring Expenses ---

@app.route('/api/admin/expenses', methods=['GET', 'POST'])
@admin_required
def api_admin_expenses():
    if request.method == 'GET':
        include_inactive = request.args.get('include_inactive') in ('1', 'true', 'yes')
        return jsonify({"expenses": db.get_expenses(include_inactive=include_inactive)})
    new_id, err = db.add_expense(request.json or {})
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"status": "ok", "id": new_id})


@app.route('/api/admin/expenses/<int:expense_id>', methods=['PUT', 'DELETE'])
@admin_required
def api_admin_expense_detail(expense_id):
    if request.method == 'DELETE':
        ok = db.delete_expense(expense_id)
        if not ok:
            return jsonify({"error": "No encontrado"}), 404
        return jsonify({"status": "ok"})
    ok, err = db.update_expense(expense_id, request.json or {})
    if err:
        return jsonify({"error": err}), 400
    if not ok:
        return jsonify({"error": "No encontrado"}), 404
    return jsonify({"status": "ok"})


# --- Admin: PS Account Pool ---

@app.route('/api/admin/ps-accounts', methods=['GET', 'POST'])
@admin_required
def api_admin_ps_accounts():
    if request.method == 'GET':
        accounts = db.get_ps_accounts()
        stock = db.get_ps_stock_count()
        return jsonify({"accounts": accounts, "stock_disponible": stock})
    else:
        data = request.json
        if not data or not data.get('email') or not data.get('password') or not data.get('activation_keys'):
            return jsonify({"error": "Email, password y activation_keys requeridos"}), 400
        
        keys = data['activation_keys']
        if isinstance(keys, str):
            # Allow newline-separated input
            keys = [k.strip() for k in keys.split('\n') if k.strip()]
        
        if len(keys) != 10:
            return jsonify({"error": f"Se requieren exactamente 10 activation keys, recibí {len(keys)}"}), 400
        
        # Accept either a single game_id or a list of game_ids
        game_ids = data.get('game_ids')
        if game_ids is None and data.get('game_id'):
            game_ids = [data.get('game_id')]
        new_id = db.add_ps_account(
            data['email'], data['password'], keys,
            data.get('notes', ''),
            game_id=data.get('game_id'),
            game_titulo=data.get('game_titulo'),
            game_ids=game_ids
        )
        return jsonify({"status": "ok", "id": new_id})

@app.route('/api/admin/ps-accounts/<int:account_id>', methods=['DELETE', 'PUT'])
@admin_required
def api_admin_manage_ps_account(account_id):
    if request.method == 'DELETE':
        success = db.delete_ps_account(account_id)
        if success:
            return jsonify({"status": "ok"})
        return jsonify({"error": "No se puede eliminar una cuenta con keys ya usadas."}), 400
    else:  # PUT
        data = request.json or {}
        updates = {}
        if data.get('email'): updates['email'] = data['email']
        if data.get('password'): updates['password'] = data['password']
        if 'notes' in data: updates['notes'] = data['notes']
        if 'game_ids' in data and isinstance(data['game_ids'], list):
            updates['game_ids'] = data['game_ids']
        if not updates:
            return jsonify({"error": "Nada que actualizar"}), 400
        db.update_ps_account(account_id, updates)
        return jsonify({"status": "ok"})

@app.route('/api/admin/ps-accounts/<int:account_id>/add-slots', methods=['POST'])
@admin_required
def api_admin_add_ps_slots(account_id):
    """Add more primaria/secundaria slots to an existing account."""
    data = request.json or {}
    primaria_add = data.get('primaria_add', 0)
    primaria_ps4_add = data.get('primaria_ps4_add', 0)
    secundaria_add = data.get('secundaria_add', 0)
    if primaria_add == 0 and primaria_ps4_add == 0 and secundaria_add == 0:
        return jsonify({"error": "Especificá cuántos slots agregar."}), 400
    success = db.add_ps_slots(account_id, primaria_add=primaria_add, primaria_ps4_add=primaria_ps4_add, secundaria_add=secundaria_add)
    if success:
        return jsonify({"status": "ok"})
    return jsonify({"error": "Cuenta no encontrada."}), 404




# --- Admin: Amazon JP tracker ---
amazon_jp_status = {
    "running": False,
    "action": None,
    "message": "",
    "updated_count": 0,
    "error_count": 0,
    "current": 0,
    "total": 0,
    "last_run_at": None,
    "started_at": None,
    "last_error": None,
}
amazon_jp_last_daily_run = time.time()
amazon_jp_status_lock = threading.Lock()


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _convert_jpy_to_usdt_ars(price_jpy):
    cfg = db.get_all_config()
    usdt_jpy_rate = _safe_float(cfg.get('usdt_jpy_rate', '160'), 160.0)
    usdt_ars_rate = _safe_float(cfg.get('usdt_rate', '1440'), 1440.0)
    if usdt_jpy_rate <= 0 or usdt_ars_rate <= 0 or not price_jpy:
        return None, None
    price_usdt = round(float(price_jpy) / usdt_jpy_rate, 4)
    price_ars = round(price_usdt * usdt_ars_rate, 2)
    return price_usdt, price_ars


def _is_amazon_job_stale(status, max_seconds=60):
    started = status.get("started_at")
    if not started:
        return False
    try:
        return (time.time() - float(started)) > max_seconds
    except (TypeError, ValueError):
        return True


def _refresh_amazon_jp_items(item_ids=None):
    global amazon_jp_status, amazon_jp_last_daily_run

    try:
        if item_ids:
            items = []
            for item_id in item_ids:
                item = db.get_amazon_jp_item(int(item_id))
                if item:
                    items.append(item)
        else:
            items = db.get_active_amazon_jp_items()

        if not items:
            with amazon_jp_status_lock:
                amazon_jp_status.update({
                    "running": False,
                    "message": "No hay publicaciones activas para actualizar.",
                    "current": 0,
                    "total": 0,
                    "last_run_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "started_at": None,
                })
            return True, None

        updated_count = 0
        error_count = 0
        total = len(items)
        with amazon_jp_status_lock:
            amazon_jp_status["current"] = 0
            amazon_jp_status["total"] = total

        for idx, item in enumerate(items, 1):
            with amazon_jp_status_lock:
                amazon_jp_status["message"] = f"Actualizando {idx}/{total}: {item.get('display_name_es')}"
                amazon_jp_status["current"] = idx
            try:
                scraped = amazon_jp_scraper.scrape_listing(item['amazon_url'])
                price_usdt, price_ars = _convert_jpy_to_usdt_ars(scraped.get('price_jpy'))
                snapshot = {
                    **scraped,
                    "price_usdt": price_usdt,
                    "price_ars": price_ars,
                    "last_status": "ok",
                    "last_error": None,
                }
                db.update_amazon_jp_snapshot(item['id'], snapshot)
                updated_count += 1
            except Exception as e:
                err_text = str(e)[:300]
                db.update_amazon_jp_snapshot(item['id'], {
                    "price_jpy": None,
                    "list_price_jpy": None,
                    "price_usdt": None,
                    "price_ars": None,
                    "is_on_sale": False,
                    "last_status": "error",
                    "last_error": err_text,
                })
                error_count += 1

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with amazon_jp_status_lock:
            amazon_jp_status.update({
                "running": False,
                "message": f"Actualización completada. OK: {updated_count} · Errores: {error_count}",
                "updated_count": updated_count,
                "error_count": error_count,
                "current": total,
                "total": total,
                "last_run_at": now_str,
                "started_at": None,
                "last_error": None if error_count == 0 else "Algunas publicaciones no pudieron actualizarse",
            })
        amazon_jp_last_daily_run = time.time()
        return True, None
    except Exception as e:
        with amazon_jp_status_lock:
            amazon_jp_status.update({
                "running": False,
                "message": "Error durante la actualización",
                "last_error": str(e),
                "current": 0,
                "total": 0,
                "last_run_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "started_at": None,
            })
        return False, str(e)


def _run_amazon_refresh_bg(item_ids=None):
    with amazon_jp_status_lock:
        if amazon_jp_status.get("running") and not _is_amazon_job_stale(amazon_jp_status, 60):
            return False
        if amazon_jp_status.get("running") and _is_amazon_job_stale(amazon_jp_status, 60):
            amazon_jp_status.update({
                "running": False,
                "message": "Se liberó un estado de actualización colgado.",
                "last_error": "Timeout de ejecución anterior",
                "current": 0,
                "total": 0,
                "last_run_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "started_at": None,
            })
        amazon_jp_status.update({
            "running": True,
            "action": "refresh",
            "message": "Iniciando actualización...",
            "updated_count": 0,
            "error_count": 0,
            "current": 0,
            "total": 0,
            "last_error": None,
            "started_at": time.time(),
        })
    t = threading.Thread(target=_refresh_amazon_jp_items, args=(item_ids,), daemon=True)
    t.start()
    return True


def _amazon_jp_daily_scheduler_loop():
    """Runs once per day and refreshes active Amazon JP listings."""
    global amazon_jp_last_daily_run
    while True:
        try:
            now_ts = time.time()
            if now_ts - amazon_jp_last_daily_run >= 24 * 3600:
                _run_amazon_refresh_bg()
        except Exception as e:
            logging.error(f"Amazon JP scheduler error: {e}")
        time.sleep(60)


amazon_jp_scheduler_thread = threading.Thread(target=_amazon_jp_daily_scheduler_loop, daemon=True)
amazon_jp_scheduler_thread.start()


@app.route('/api/admin/amazon-jp-tracker', methods=['GET', 'POST'])
@admin_required
def api_admin_amazon_jp_tracker():
    if request.method == 'GET':
        include_inactive = request.args.get('include_inactive', '1') in ('1', 'true', 'yes')
        return jsonify({"items": db.get_amazon_jp_items(include_inactive=include_inactive)})

    item_id, err = db.add_amazon_jp_item(request.json or {})
    if err:
        return jsonify({"error": err}), 400
    refresh_started = _run_amazon_refresh_bg(item_ids=[item_id])
    return jsonify({
        "status": "ok",
        "id": item_id,
        "refresh_started": refresh_started
    })


@app.route('/api/admin/amazon-jp-tracker/<int:item_id>', methods=['PUT', 'DELETE'])
@admin_required
def api_admin_amazon_jp_tracker_item(item_id):
    if request.method == 'DELETE':
        ok = db.delete_amazon_jp_item(item_id)
        if not ok:
            return jsonify({"error": "No encontrado"}), 404
        return jsonify({"status": "ok"})

    ok, err = db.update_amazon_jp_item(item_id, request.json or {})
    if err:
        return jsonify({"error": err}), 400
    if not ok:
        return jsonify({"error": "No encontrado"}), 404
    item = db.get_amazon_jp_item(item_id)
    refresh_started = False
    if item and item.get("is_active"):
        refresh_started = _run_amazon_refresh_bg(item_ids=[item_id])
    return jsonify({"status": "ok", "refresh_started": refresh_started})


@app.route('/api/admin/amazon-jp-tracker/status', methods=['GET'])
@admin_required
def api_admin_amazon_jp_tracker_status():
    with amazon_jp_status_lock:
        status_copy = amazon_jp_status.copy()
        # Failsafe: if a worker gets stuck, auto-release status.
        started_at = status_copy.get("started_at")
        if status_copy.get("running") and started_at and (time.time() - float(started_at) > 60):
            amazon_jp_status.update({
                "running": False,
                "message": "Actualización finalizada por timeout de estado.",
                "last_error": "Timeout de monitoreo (el proceso tardó demasiado).",
                "last_run_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "started_at": None,
            })
            status_copy = amazon_jp_status.copy()
        return jsonify(status_copy)


@app.route('/api/admin/amazon-jp-tracker/refresh', methods=['POST'])
@admin_required
def api_admin_amazon_jp_tracker_refresh():
    payload = request.json or {}
    item_ids = payload.get('item_ids')
    if item_ids is not None and not isinstance(item_ids, list):
        return jsonify({"error": "item_ids debe ser un array"}), 400
    with amazon_jp_status_lock:
        if amazon_jp_status.get("running") and not _is_amazon_job_stale(amazon_jp_status, 60):
            return jsonify({"error": "Ya hay una actualización en curso"}), 409
    if item_ids:
        started = _run_amazon_refresh_bg(item_ids=item_ids)
        return jsonify({"status": "started" if started else "busy", "scope": "selected"})
    started = _run_amazon_refresh_bg(item_ids=None)
    return jsonify({"status": "started" if started else "busy", "scope": "all_active"})


# --- Static Fallback ---

SITE_URL = os.getenv('SITE_URL', 'https://nezjuegos.com')

DEFAULT_OG = {
    'title': 'Nez Juegos | Juegos Digitales Nintendo Switch y PlayStation',
    'description': 'Compra juegos digitales para Nintendo Switch y PlayStation con entrega inmediata y precios imbatibles. Hasta 6 cuotas sin interés.',
    'image': f'{SITE_URL}/nez-logo.jpg'
}

def inject_head_tags(html_content, og_overrides=None):
    """Inject favicon and OG meta tags into HTML. Always runs regardless of GTM presence."""
    if '</head>' not in html_content:
        return html_content

    og = {**DEFAULT_OG, **(og_overrides or {})}
    # Ensure og:image is always an absolute URL
    if og['image'].startswith('/'):
        og['image'] = f"{SITE_URL}{og['image']}"

    tags = ''

    # Always inject favicon if not present
    if 'rel="icon"' not in html_content:
        tags += '<link rel="icon" type="image/jpeg" href="/nez-logo.jpg">\n'

    # Inject OG tags if not present
    if 'og:title' not in html_content:
        tags += f'<meta property="og:title" content="{og["title"]}">\n'
        tags += f'<meta property="og:description" content="{og["description"]}">\n'
        tags += f'<meta property="og:image" content="{og["image"]}">\n'
        tags += f'<meta property="og:url" content="{og.get("url", SITE_URL)}">\n'
        tags += f'<meta property="og:type" content="website">\n'
        tags += f'<meta name="twitter:card" content="summary_large_image">\n'

    if tags:
        html_content = html_content.replace('</head>', f'{tags}</head>', 1)
    return html_content

@app.route('/')
@app.route('/<path:path>')
def serve_static(path=''):
    def get_html(file_path, og_overrides=None):
        with open(file_path, 'r', encoding='utf-8') as f:
            html = f.read()
        return inject_head_tags(html, og_overrides)

    if not path or path == 'index' or path == 'index.html':
        return get_html(os.path.join(UI_DIR, 'index.html'))

    # Separate platform catalogs
    if path == 'nintendo':
        return get_html(os.path.join(UI_DIR, 'juegos.html'))
    if path in ('playstation', 'juegos'):
        return get_html(os.path.join(UI_DIR, 'juegos.html'))

    # Individual game page with dynamic OG tags
    if path.startswith('juegos/') and path != 'juegos.html':
        slug = path.replace('juegos/', '')
        og = None
        game = db.get_game_by_slug(slug) if slug else None
        if game:
            cover = f"/uploads/{game.get('imagen_filename')}" if game.get('imagen_filename') else '/nez-logo.jpg'
            og = {
                'title': f"{game['titulo']} | Nez Juegos",
                'description': f"Compra {game['titulo']} en Nez Juegos con entrega inmediata. Hasta 6 cuotas sin interés.",
                'image': cover,
                'url': f"{SITE_URL}/juegos/{slug}"
            }
        return get_html(os.path.join(UI_DIR, 'juego.html'), og)

    if path == 'checkout':
        return get_html(os.path.join(UI_DIR, 'checkout.html'))

    if path == 'terminos-y-condiciones':
        return get_html(os.path.join(UI_DIR, 'terminos.html'))

    if path in ['success', 'uala/success', 'uala/failure']:
        return get_html(os.path.join(UI_DIR, 'success.html'))

    # Security: If trying to access admin views, check auth first
    if path.startswith('admin') and not path.startswith('admin/login'):
        if not session.get('is_admin'):
            return redirect('/admin/login')

        page = path.replace('admin/', '').replace('admin', '')
        if not page or page == 'index': page = 'index'

        admin_file = None
        if os.path.exists(os.path.join(UI_ADMIN_DIR, page)):
            admin_file = os.path.join(UI_ADMIN_DIR, page)
        elif os.path.exists(os.path.join(UI_ADMIN_DIR, f"{page}.html")):
            admin_file = os.path.join(UI_ADMIN_DIR, f"{page}.html")

        if admin_file:
            return get_html(admin_file)
        return "Not Found", 404

    # Public paths
    if os.path.exists(os.path.join(UI_DIR, path)):
        if path.endswith('.html'):
            return get_html(os.path.join(UI_DIR, path))
        return send_from_directory(UI_DIR, path)
    elif os.path.exists(os.path.join(UI_DIR, f"{path}.html")):
        return get_html(os.path.join(UI_DIR, f"{path}.html"))

    return "Not Found", 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"Nez Juegos V2 running on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
