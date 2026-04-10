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
from uala_bis import UalaBis, calcular_precio_con_uala
from email_sender import send_ps_credentials

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
    return send_from_directory(UPLOAD_FOLDER, filename)


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
    
    required = ['game_titulo', 'tipo_producto', 'buyer_email', 'payment_method', 'precio_base', 'precio_cobrado']
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"Campo requerido: {field}"}), 400
    
    # Validate payment method
    if data['payment_method'] not in ('transferencia', 'uala'):
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
    """
    data = request.json
    if not data:
        return '', 200  # Accept but ignore malformed
    
    logging.info(f"Ualá webhook received: {json.dumps(data)}")
    
    external_ref = data.get('external_reference', '')
    status = data.get('status', '').upper()
    
    order = db.find_order_by_uala_ref(external_ref)
    if not order:
        logging.warning(f"Ualá webhook: order not found for ref {external_ref}")
        return '', 200
    
    if status == 'APPROVED':
        # Check if PS game -> auto-deliver
        plat = (order.get('game_plataforma') or '').upper()
        is_ps = 'PS4' in plat or 'PS5' in plat or 'PLAYSTATION' in plat
        
        if is_ps:
            _deliver_ps_order(order['id'])
        else:
            # Nintendo or other: mark as approved, wait for manual WhatsApp contact
            db.update_order_status(order['id'], 'aprobado')
    elif status in ('DECLINED', 'REJECTED'):
        db.update_order_status(order['id'], 'rechazado')
    
    return '', 200


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
        logging.warning(f"No PS stock for order {order_id} (type={sale_type}). Queued as pendiente_stock.")
        db.update_order_status(order_id, 'pendiente_stock')
        return False
    
    success = send_ps_credentials(
        to_email=order['buyer_email'],
        game_name=order['game_titulo'],
        account_email=account['email'],
        account_password=account['password'],
        activation_key=activation_key
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
        return jsonify({"error": "Aún no hay stock disponible para este juego/tipo. Agregá la cuenta primero."}), 400


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
        
        new_id = db.add_ps_account(
            data['email'], data['password'], keys,
            data.get('notes', ''),
            game_id=data.get('game_id'),
            game_titulo=data.get('game_titulo')
        )
        return jsonify({"status": "ok", "id": new_id})

@app.route('/api/admin/ps-accounts/<int:account_id>', methods=['DELETE'])
@admin_required
def api_admin_delete_ps_account(account_id):
    success = db.delete_ps_account(account_id)
    if success:
        return jsonify({"status": "ok"})
    return jsonify({"error": "No se puede eliminar una cuenta con keys ya usadas."}), 400

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


# --- Ualá Bis Surcharge Calculator (public) ---

@app.route('/api/uala/surcharge')
def api_uala_surcharge():
    """Calculate Ualá Bis surcharge for a given base price."""
    precio_base = request.args.get('precio', type=int)
    if not precio_base:
        return jsonify({"error": "precio requerido"}), 400
    precio_total, surcharge = calcular_precio_con_uala(precio_base)
    return jsonify({"precio_base": precio_base, "surcharge": surcharge, "precio_total": precio_total})


# --- Static Fallback ---
@app.route('/')
@app.route('/<path:path>')
def serve_static(path=''):
    if not path or path == 'index' or path == 'index.html': 
        return send_from_directory(UI_DIR, 'index.html')
    
    # Clean URL routes for new pages
    if path.startswith('juegos/') and path != 'juegos.html':
        # Individual game page: /juegos/<slug>
        return send_from_directory(UI_DIR, 'juego.html')
    
    if path == 'checkout':
        return send_from_directory(UI_DIR, 'checkout.html')
    
    if path == 'terminos-y-condiciones':
        return send_from_directory(UI_DIR, 'terminos.html')
        
    # Security: If trying to access admin views, check auth first
    if path.startswith('admin') and not path.startswith('admin/login'):
        if not session.get('is_admin'):
            return redirect('/admin/login')
            
        # Admin paths route to UI_ADMIN_DIR
        page = path.replace('admin/', '').replace('admin', '')
        if not page or page == 'index': page = 'index'
        
        # Try finding the exact file or adding .html
        if os.path.exists(os.path.join(UI_ADMIN_DIR, page)):
            return send_from_directory(UI_ADMIN_DIR, page)
        elif os.path.exists(os.path.join(UI_ADMIN_DIR, f"{page}.html")):
            return send_from_directory(UI_ADMIN_DIR, f"{page}.html")
        return "Not Found", 404

    # Public paths
    if os.path.exists(os.path.join(UI_DIR, path)):
        return send_from_directory(UI_DIR, path)
    elif os.path.exists(os.path.join(UI_DIR, f"{path}.html")):
        return send_from_directory(UI_DIR, f"{path}.html")
        
    return "Not Found", 404

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"Nez Juegos V2 running on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
