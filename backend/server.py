import threading
import asyncio
import os
import json
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
    data = request.json
    for key, value in data.items():
        db.update_config(key, value)
    return jsonify({"status": "ok"})


# --- Admin API Routes (Individual Games) ---
@app.route('/api/admin/juegos', methods=['POST'])
@admin_required
def create_juego():
    # Supports JSON or Form Data (if file upload is included)
    if request.content_type.startswith('multipart/form-data'):
        data = dict(request.form)
        file = request.files.get('image')
        if file:
            # Basic save for Custom Game Covers
            filename = f"game_{int(asyncio.get_event_loop().time())}_{file.filename}"
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
        data = request.json
        db.update_juego(juego_id, data)
        return jsonify({"status": "ok"})


# --- Admin API Routes (Scraping & Telegram Packs) ---
@app.route('/api/admin/scrape/today', methods=['POST'])
@admin_required
def api_scrape_today():
    try:
        count = run_on_scraper_thread(scraper.scrape_today())
        return jsonify({"status": "ok", "packs_added": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/scrape/full', methods=['POST'])
@admin_required
def api_scrape_full():
    try:
        count = run_on_scraper_thread(scraper.scrape_full(1000))
        return jsonify({"status": "ok", "packs_added": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/scrape/verify', methods=['POST'])
@admin_required
def api_verify_deleted():
    try:
        deleted = run_on_scraper_thread(scraper.verify_deleted())
        return jsonify({"status": "ok", "packs_removed_from_db": deleted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/packs/<pack_id>', methods=['DELETE'])
@admin_required
def manual_delete_pack(pack_id):
    """Admin clicked 'Delete' -> Prevent scraper from ever re-adding it"""
    db.mark_pack_deleted(pack_id, manual=True)
    return jsonify({"status": "ok"})

@app.route('/api/admin/telegram/status')
def telegram_status():
    """Check if headless browser QR needs scanning"""
    try:
        logged_in = run_on_scraper_thread(scraper.ensure_telegram_login())
        return jsonify({"telegram_connected": logged_in})
    except:
        return jsonify({"telegram_connected": False})


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
