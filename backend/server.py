import threading
import asyncio
import os
import json
import time
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, session, redirect

from scraper import NintendoScraper
from database import Database

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
    return jsonify({"results": db.get_all_juegos()})

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
    return jsonify(scrape_status)

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
        is_dlc = 'dlc' in line.lower()
        is_mixed = '+' in line
        games.append({
            "name": line,
            "is_dlc": is_dlc,
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

@app.route('/api/admin/telegram/status')
def telegram_status():
    """Check if headless browser QR needs scanning"""
    try:
        logged_in = run_on_scraper_thread(scraper.ensure_telegram_login())
        return jsonify({"telegram_connected": logged_in})
    except:
        return jsonify({"telegram_connected": False})

# --- Hot Titles API ---

@app.route('/api/admin/hot_titles', methods=['GET', 'POST'])
@admin_required
def api_admin_hot_titles():
    if request.method == 'GET':
        return jsonify(db.get_hot_titles())
    else:
        # POST: add a new title
        data = request.json
        if not data or not data.get('titulo'):
            return jsonify({"error": "Título requerido"}), 400
        
        success = db.add_hot_title(data['titulo'])
        if success:
            return jsonify({"status": "ok"})
        else:
            return jsonify({"error": "El título ya existe"}), 400

@app.route('/api/admin/hot_titles/<int:id>', methods=['DELETE'])
@admin_required
def api_admin_delete_hot_title(id):
    db.delete_hot_title(id)
    return jsonify({"status": "ok"})

@app.route('/api/public/hot_titles', methods=['GET'])
def api_public_hot_titles():
    # Public endpoint so index.html can know which titles get the fire emoji
    return jsonify([t['titulo'] for t in db.get_hot_titles()])


# --- Static Fallback ---
@app.route('/')
@app.route('/<path:path>')
def serve_static(path=''):
    if not path or path == 'index' or path == 'index.html': 
        return send_from_directory(UI_DIR, 'index.html')
        
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
