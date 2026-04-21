import threading
import asyncio
import os
import json
import time
import logging
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, session, redirect, send_file, make_response

from scraper import NintendoScraper
from database import Database
from uala_bis import UalaBis
from email_sender import send_ps_credentials
import requests
import uuid
import hmac
import hashlib

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

# MP Config
MP_ACCESS_TOKEN = os.getenv('MP_ACCESS_TOKEN', 'APP_USR-test')
MP_PUBLIC_KEY = os.getenv('MP_PUBLIC_KEY', 'TEST-public-key')
MP_WEBHOOK_SECRET = os.getenv('MP_WEBHOOK_SECRET', 'test-secret')
DESCUENTO_SALDO_MP = float(os.getenv('DESCUENTO_SALDO_MP', '0.26'))
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

@app.route('/api/config/mp')
def api_config_mp():
    return jsonify({"public_key": MP_PUBLIC_KEY})

@app.route('/api/packs')
def search_packs():
    query = request.args.get('q', '')
    exclude = request.args.get('exclude', '')
    limit = int(request.args.get('limit', 500))
    price_max = request.args.get('price_max', type=int)
    dlc_only = request.args.get('dlc_only', 'false').lower() == 'true'
    featured = request.args.get('featured', 'false').lower() == 'true'
    
    results = db.get_packs(query=query, exclude=exclude, price_max=price_max, dlc_only=dlc_only, featured_only=featured, limit=limit)
    return jsonify({"results": results})

@app.route('/api/packs/suggestions')
def pack_suggestions():
    q = request.args.get('q', '')
    return jsonify({"suggestions": db.get_game_name_suggestions(q)})

@app.route('/api/juegos')
def get_juegos():
    featured = request.args.get('featured', 'false').lower() == 'true'
    return jsonify({"results": db.get_all_juegos(featured_only=featured)})

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
    game['slug'] = db.generate_slug(game['titulo'], game.get('plataforma', ''))
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
    if data['payment_method'] not in ('transferencia', 'uala', 'mercadopago', 'binance'):
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

# --- Mercado Pago Payment API ---

@app.route('/api/pricing')
def api_pricing():
    """Calculate all price tiers from the base (card) price stored in DB."""
    precio_transfer = request.args.get('precio', type=int)
    if not precio_transfer:
        return jsonify({"error": "precio requerido"}), 400
    # DB stores the transfer (minimum) price; card and MP are calculated upward from it
    precio_tarjeta = int(precio_transfer / (1 - DESCUENTO_TRANSFERENCIA))
    precio_saldo_mp = int(precio_tarjeta * (1 - DESCUENTO_SALDO_MP))

    cfg = db.get_all_config()
    try:
        usdt_rate = float(cfg.get('usdt_rate', '1440') or '1440')
    except ValueError:
        usdt_rate = 1440.0
    precio_usdt = round(precio_transfer / usdt_rate, 2) if usdt_rate > 0 else 0

    return jsonify({
        "precio_transferencia": precio_transfer,
        "precio_saldo_mp": precio_saldo_mp,
        "precio_tarjeta": precio_tarjeta,
        "precio_usdt": precio_usdt,
        "usdt_rate": usdt_rate,
        "descuento_saldo_pct": int(DESCUENTO_SALDO_MP * 100),
        "descuento_transfer_pct": int(DESCUENTO_TRANSFERENCIA * 100)
    })

@app.route('/api/mp/preference', methods=['POST'])
def api_mp_preference():
    data = request.json
    if not data:
        return jsonify({"error": "Datos requeridos"}), 400
        
    required = ['game_id', 'game_titulo', 'tipo_producto', 'precio_base', 'precio_cobrado']
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Campo requerido: {field}"}), 400

    # Crear la orden como "iniciada". Si abandona el brick, queda así en DB.
    order_id = db.create_order({
        'game_id': data['game_id'],
        'game_titulo': data['game_titulo'],
        'game_plataforma': data.get('game_plataforma', ''),
        'tipo_producto': data['tipo_producto'],
        'buyer_email': data.get('buyer_email', 'comprador-wallet@nezjuegos.com'),
        'buyer_phone': data.get('buyer_phone'),
        'payment_method': 'mercadopago',
        'precio_base': data['precio_base'],
        'precio_cobrado': data['precio_cobrado'],
        'surcharge': data.get('surcharge', 0)
    })
    
    # Cambiamos forzando a 'iniciado' asumiendo que db.create_order devuelve pendiente, lo acomodamos despues
    # Fallback to host_url for local testing, but prefer site_url from frontend for proxy safety in Railway
    site_url = data.get('site_url', request.host_url).rstrip('/')
    
    payload = {
        "items": [{
            "id": str(data['game_id']),
            "title": data['game_titulo'],
            "quantity": 1,
            "unit_price": float(data['precio_cobrado'])
        }],
        "payer": {
            "email": data.get('buyer_email', 'comprador-wallet@nezjuegos.com')
        },
        "back_urls": {
            "success": f"{site_url}/mp/success",
            "pending": f"{site_url}/mp/pending",
            "failure": f"{site_url}/mp/failure"
        },
        "auto_return": "approved",
        "external_reference": f"nez-{order_id}"
    }
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {MP_ACCESS_TOKEN}'
    }
    
    try:
        resp = requests.post('https://api.mercadopago.com/checkout/preferences', json=payload, headers=headers, timeout=15)
        resp_data = resp.json()
        
        if resp.status_code >= 400:
            logging.error(f"MP Preference error: {resp.text}")
            # Try to extract a clean message from MP if possible, else return the raw text
            error_msg = resp_data.get('message', resp.text)
            return jsonify({"error": f"MP Rechazó: {error_msg}"}), 400
            
        return jsonify({"status": "ok", "preference_id": resp_data['id'], "order_id": order_id})
    except Exception as e:
        logging.error(f"Failed to call MP Preferences: {e}")
        return jsonify({"error": "Error de conexión con procesador de pagos"}), 500

@app.route('/api/mp/create', methods=['POST'])
def api_mp_create():
    data = request.json
    if not data:
        return jsonify({"error": "Datos requeridos"}), 400
    
    # Required for brick processing
    required = ['buyer_email', 'precio_cobrado', 'mp_token', 'mp_installments', 'mp_payment_method_id', 'order_id']
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Campo requerido: {field}"}), 400
            
    order_id = data['order_id']
    order = db.get_order(order_id)
    if not order:
        return jsonify({"error": "Orden no encontrada"}), 404
            
    # Update some info if it was missing during preference creation (like the real email)
    if order['buyer_email'] == 'comprador-wallet@nezjuegos.com' and data.get('buyer_email'):
        order['buyer_email'] = data['buyer_email']
    
    # We still use the Orders API to process the payment transparently as long as we have the token
    payload = {
      "type": "online",
      "processing_mode": "automatic",
      "total_amount": str(data['precio_cobrado']),
      "external_reference": f"nez-{order_id}",
      "payer": { "email": order['buyer_email'] },
      "transactions": {
        "payments": [{
          "amount": str(data['precio_cobrado']),
          "payment_method": {
            "id": data['mp_payment_method_id'],
            "type": "credit_card",
            "token": data['mp_token'],
            "installments": int(data['mp_installments'])
          }
        }]
      }
    }
    
    headers = {
        'Content-Type': 'application/json',
        'X-Idempotency-Key': str(uuid.uuid4()),
        'Authorization': f'Bearer {MP_ACCESS_TOKEN}'
    }
    
    try:
        resp = requests.post('https://api.mercadopago.com/v1/orders', json=payload, headers=headers, timeout=15)
        resp_data = resp.json()
        
        if resp.status_code >= 400:
            logging.error(f"MP Order error: {resp.text}")
            import traceback
            traceback.print_exc()
            return jsonify({"error": "Error interno al procesar el pago con MP"}), 400
            
        mp_order_id = resp_data.get('id')
        db.update_order_notes(order_id, f"MP_ORDER_V2:{mp_order_id}")
        
        txs = resp_data.get('transactions', {}).get('payments', [])
        payment_status = txs[0].get('status') if txs else 'unknown'
        
        return jsonify({"status": "ok", "order_id": order_id, "mp_status": payment_status})
        
    except Exception as e:
        logging.error(f"Failed to call MP: {e}")
        return jsonify({"error": "Error de conexión con procesador de pagos"}), 500

@app.route('/api/mp/webhook', methods=['POST'])
def api_mp_webhook():
    # 1. Parse Query and Header
    data_id = request.args.get('data.id', '')
    if not data_id:
        return 'OK', 200
        
    x_request_id = request.headers.get('x-request-id', '')
    x_signature = request.headers.get('x-signature', '')
    
    if x_signature:
        try:
            parts = x_signature.split(',')
            ts = parts[0].split('=')[1]
            hash_val = parts[1].split('=')[1]
            manifest = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
            mi_firma = hmac.new(MP_WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
            if mi_firma != hash_val:
                logging.warning("MP Webhook: Firma HMAC inválida")
                return 'Forbidden', 403
        except Exception as e:
            logging.error(f"Error en x-signature validation: {e}")
            return 'Bad Request', 400
            
    # Consultar Orden de MP
    headers = {'Authorization': f'Bearer {MP_ACCESS_TOKEN}'}
    try:
        resp = requests.get(f'https://api.mercadopago.com/v1/orders/{data_id}', headers=headers, timeout=10)
        if resp.status_code == 200:
            mp_order = resp.json()
            ext_ref = mp_order.get('external_reference', '')
            if ext_ref.startswith('nez-'):
                order_id_str = ext_ref.split('-')[1]
                order_id = int(order_id_str)
                order = db.get_order(order_id)
                if order and order['payment_status'] not in ('entregado', 'aprobado'):
                    txs = mp_order.get('transactions', {}).get('payments', [])
                    if txs:
                        pay_status = txs[0].get('status', '').upper()
                        if pay_status == 'APPROVED':
                            plat = (order.get('game_plataforma') or '').upper()
                            is_ps = 'PS4' in plat or 'PS5' in plat or 'PLAYSTATION' in plat
                            if is_ps:
                                _deliver_ps_order(order_id)
                            else:
                                db.update_order_status(order_id, 'aprobado')
                        elif pay_status in ('REJECTED', 'CANCELLED'):
                            db.update_order_status(order_id, 'rechazado')
    except Exception as e:
        logging.error(f"Error consultando mp order webhook: {e}")
            
    return 'OK', 200

# --- Mercado Pago Redirect Routes (For Wallet) ---
@app.route('/mp/success')
def api_mp_success():
    return """
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;800&display=swap" rel="stylesheet">
        <style>
            body { background: #0a0a0c; color: #fff; font-family: 'Outfit', sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; text-align: center; }
            .card { background: #16161a; padding: 3rem; border-radius: 12px; border: 1px solid #8b5cf6; max-width: 400px; }
            h1 { color: #8b5cf6; margin-top: 0; }
            a { display: inline-block; background: #8b5cf6; color: #fff; text-decoration: none; padding: 0.8rem 1.5rem; border-radius: 6px; font-weight: 600; margin-top: 1.5rem; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>¡Pago Aprobado!</h1>
            <p>Tu orden ha sido confirmada vía Mercado Pago.</p>
            <p>Si compraste un juego de PlayStation, el sistema automático te enviará las credenciales a tu email en breves instantes (revisa Spam).</p>
            <p>Si compraste para Nintendo Switch, te contactaremos por WhatsApp.</p>
            <a href="/">Volver al Inicio</a>
        </div>
    </body>
    </html>
    """

@app.route('/mp/pending')
def api_mp_pending():
    return """
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;800&display=swap" rel="stylesheet">
        <style>
            body { background: #0a0a0c; color: #fff; font-family: 'Outfit', sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; text-align: center; }
            .card { background: #16161a; padding: 3rem; border-radius: 12px; border: 1px solid #eab308; max-width: 400px; }
            h1 { color: #eab308; margin-top: 0; }
            a { display: inline-block; background: #8b5cf6; color: #fff; text-decoration: none; padding: 0.8rem 1.5rem; border-radius: 6px; font-weight: 600; margin-top: 1.5rem; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>⏳ Pago Pendiente</h1>
            <p>Tu pago está en revisión por Mercado Pago.</p>
            <p>Te contactaremos por WhatsApp cuando se confirme. Puede tardar unos minutos.</p>
            <a href="/">Volver al Inicio</a>
        </div>
    </body>
    </html>
    """

@app.route('/mp/failure')
def api_mp_failure():
    return """
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;800&display=swap" rel="stylesheet">
        <style>
            body { background: #0a0a0c; color: #fff; font-family: 'Outfit', sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; text-align: center; }
            .card { background: #16161a; padding: 3rem; border-radius: 12px; border: 1px solid #ef4444; max-width: 400px; }
            h1 { color: #ef4444; margin-top: 0; }
            a { display: inline-block; background: #8b5cf6; color: #fff; text-decoration: none; padding: 0.8rem 1.5rem; border-radius: 6px; font-weight: 600; margin-top: 1.5rem; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>❌ Pago Rechazado</h1>
            <p>El pago fue rechazado o cancelado por Mercado Pago.</p>
            <p>Podés intentarlo de nuevo o contactarnos por WhatsApp si necesitás ayuda.</p>
            <a href="/">Volver al Inicio</a>
        </div>
    </body>
    </html>
    """


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

    if path in ['success', 'mp/success', 'mp/failure', 'mp/pending', 'uala/success', 'uala/failure']:
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
