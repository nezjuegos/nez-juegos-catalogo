from flask import Flask, jsonify, request, send_from_directory, session, redirect, url_for
import threading
import asyncio
import os
import json
import zipfile
import shutil
from functools import wraps
from scraper import NintendoScraper

app = Flask(__name__)
# Secret key for session encryption. Use env var in production, fixed fallback for dev.
# IMPORTANT: os.urandom would change on every restart, invalidating all sessions!
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-change-me-in-production')
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = bool(os.getenv('RAILWAY_VOLUME_MOUNT_PATH'))  # Secure cookies on HTTPS (Railway)
# Master password loaded from environment variable (default for local dev)
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')

scraper = NintendoScraper()

# --- Auth Decorator ---
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            # Return JSON error for API endpoints, redirect for HTML
            if request.path.startswith('/api/'):
                return jsonify({"error": "Unauthorized"}), 401
            return redirect('/admin/login')
        return f(*args, **kwargs)
    return decorated_function
# ----------------------

# --- Auto-Extract Telegram Session from Zip (For Railway Deployment) ---
# If sesion.zip is found, extract it to the persistent volume automatically
RAILWAY_VOLUME = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', os.getcwd())
ZIP_PATH = os.path.join(os.getcwd(), 'sesion.zip')
EXTRACT_PATH = RAILWAY_VOLUME

if os.path.exists(ZIP_PATH):
    print(f"[DEPLOY] Found sesion.zip! Extracting to {EXTRACT_PATH}...")
    try:
        # The zip already contains a top-level 'browser_data_clean' folder,
        # so extracting to RAILWAY_VOLUME will place it perfectly at /data/browser_data_clean
        with zipfile.ZipFile(ZIP_PATH, 'r') as zip_ref:
            zip_ref.extractall(EXTRACT_PATH)
            
        print("[DEPLOY] Session extracted successfully! Deleting sesion.zip...")
        os.remove(ZIP_PATH) # Remove to save space after extraction
    except Exception as e:
        print(f"[DEPLOY] Error extracting session: {e}")
# ------------------------------------------------------------------------

# --- Asyncio Bridge ---
# Playwright must run on a single event loop.
# We create a dedicated thread for this loop.

loop = asyncio.new_event_loop()

def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

# Start the thread
t = threading.Thread(target=start_background_loop, args=(loop,), daemon=True)
t.start()

def run_on_scraper_thread(coro):
    """Submits a coroutine to the background loop and waits for result."""
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()

@app.route('/')
def root():
    # Redirect base domain to the admin login or dashboard
    if session.get('is_admin'):
        return redirect('/admin')
    return redirect('/admin/login')

@app.route('/admin/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # Handle login attempt from our custom form
        data = request.json
        password = data.get('password', '')
        if password == ADMIN_PASSWORD:
            session['is_admin'] = True
            return jsonify({"status": "ok"})
        return jsonify({"error": "Invalid password"}), 401
        
    # GET request: serve the login page
    if session.get('is_admin'):
        return redirect('/admin')
    return send_from_directory('ui', 'login.html')

@app.route('/admin/logout', methods=['POST'])
def logout():
    session.pop('is_admin', None)
    return jsonify({"status": "ok"})

@app.route('/admin')
@admin_required
def admin_dashboard():
    # Protected Admin Dashboard
    return send_from_directory('ui', 'index.html')

@app.route('/cliente')
def cliente():
    return send_from_directory('ui/cliente', 'index.html')

@app.route('/cliente/<path:path>')
def serve_cliente_static(path):
    return send_from_directory('ui/cliente', path)

@app.route('/<path:path>')
def serve_static(path):
    # Static files should still be served, but HTML pages might need checking
    # We leave this open so JS/CSS loads properly, but the main index is handled by /admin
    if path == 'index.html':
        return redirect('/admin') # Don't allow bypassing via /index.html
    return send_from_directory('ui', path)

@app.route('/api/search')
def search():
    query = request.args.get('q', '')
    exclude = request.args.get('exclude', '')
    limit = int(request.args.get('limit', 500))
    price_min = request.args.get('price_min', type=int)
    price_max = request.args.get('price_max', type=int)

    print(f"[SERVER] Searching for '{query or 'ALL'}' (Exclude: '{exclude or 'NONE'}', Limit: {limit}, Price: {price_min or 'any'}-{price_max or 'any'})...")
    
    try:
        # Run on the dedicated loop
        results = run_on_scraper_thread(scraper.search_game(query, limit, exclude))
        
        # Apply price filters
        if price_min is not None or price_max is not None:
            filtered = []
            for pack in results:
                price = pack['price_local']
                if price_min is not None and price < price_min:
                    continue
                if price_max is not None and price > price_max:
                    continue
                filtered.append(pack)
            results = filtered
        
        return jsonify({"results": results})
    except Exception as e:
        print(f"[SERVER] Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/status')
def status():
    # Check if we need login
    try:
        logged_in = run_on_scraper_thread(scraper.ensure_telegram_login())
        cache_size = len(scraper.cached_packs)
        return jsonify({"telegram_connected": logged_in, "cached_packs": cache_size})
    except:
        return jsonify({"telegram_connected": False, "cached_packs": 0})

refresh_status = {
    "is_running": False,
    "last_result": None,
    "error": None
}

def background_refresh(count):
    global refresh_status
    try:
        # We need to run the coroutine in the pre-existing scraper thread loop
        future = asyncio.run_coroutine_threadsafe(scraper.manual_refresh(count), loop)
        result = future.result() # Wait for it to finish in this background thread
        refresh_status["last_result"] = result
        print(f"[SERVER] Background refresh finished: {result.get('packs_found', 0)} packs found", flush=True)
    except Exception as e:
        refresh_status["error"] = str(e)
        print(f"[SERVER] Background refresh error: {e}", flush=True)
    finally:
        refresh_status["is_running"] = False

@app.route('/api/refresh', methods=['POST'])
@admin_required
def refresh():
    """Start refreshing the pack cache in the background"""
    global refresh_status
    if refresh_status["is_running"]:
        return jsonify({"error": "A refresh is already perfectly in progress."}), 409
        
    count = int(request.args.get('count', 1000))
    print(f"[SERVER] Manual refresh triggered ({count} messages)...", flush=True)
    
    refresh_status["is_running"] = True
    refresh_status["error"] = None
    refresh_status["last_result"] = None
    
    # Start the refresh task in a NEW standard python thread so Flask can respond immediately
    import threading
    t = threading.Thread(target=background_refresh, args=(count,))
    t.start()
    
    return jsonify({"status": "started", "message": "Scraping started in background"})

@app.route('/api/refresh/status', methods=['GET'])
@admin_required
def get_refresh_status():
    """Poll for the background refresh status"""
    global refresh_status
    if refresh_status["is_running"]:
        return jsonify({"status": "running"})
    elif refresh_status["error"]:
        return jsonify({"status": "error", "error": refresh_status["error"]}), 500
    elif refresh_status["last_result"]:
        # Return result and clear it so we don't send it twice on subsequent calls
        res = refresh_status["last_result"]
        refresh_status["last_result"] = None
        return jsonify({"status": "finished", "result": res})
    else:
        return jsonify({"status": "idle"})

@app.route('/api/admin/set-cover', methods=['POST'])
@admin_required
def set_cover():
    """Set a manual cover for a specific pack ID"""
    data = request.json
    pack_id = str(data.get('id'))
    url = data.get('url')
    
    if not pack_id:
        return jsonify({"error": "Missing pack ID"}), 400

    try:
        # Update manual covers persistence
        railway_volume = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', os.path.dirname(__file__))
        manual_path = os.path.join(railway_volume, 'manual_covers.json')
        manual_data = {}
        
        if os.path.exists(manual_path):
            with open(manual_path, 'r', encoding='utf-8') as f:
                try:
                    manual_data = json.load(f)
                except:
                    pass
        
        if url:
            manual_data[pack_id] = url
        else:
            # If URL is empty/null, remove the override
            if pack_id in manual_data:
                del manual_data[pack_id]
        
        with open(manual_path, 'w', encoding='utf-8') as f:
            json.dump(manual_data, f, indent=4)
            
        # Update run-time scraper cache
        run_on_scraper_thread(scraper.update_manual_cover(pack_id, url))
        
        return jsonify({"status": "ok", "id": pack_id, "url": url})
    except Exception as e:
        print(f"[SERVER] Error setting cover: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/bulk-set-covers', methods=['POST'])
@admin_required
def bulk_set_covers():
    """Set multiple manual covers at once"""
    data = request.json
    covers = data.get('covers', []) # List of {id, url}
    
    try:
        # Update manual covers persistence
        railway_volume = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', os.path.dirname(__file__))
        manual_path = os.path.join(railway_volume, 'manual_covers.json')
        manual_data = {}
        
        if os.path.exists(manual_path):
            with open(manual_path, 'r', encoding='utf-8') as f:
                try:
                    manual_data = json.load(f)
                except:
                    pass
        
        updated_count = 0
        for item in covers:
            p_id = str(item.get('id'))
            p_url = item.get('url')
            if p_id:
                if p_url:
                    manual_data[p_id] = p_url
                elif p_id in manual_data:
                    del manual_data[p_id]
                updated_count += 1
        
        with open(manual_path, 'w', encoding='utf-8') as f:
            json.dump(manual_data, f, indent=4)
            
        # Reload scraper cache
        run_on_scraper_thread(scraper.reload_manual_covers())
        
        return jsonify({"status": "ok", "updated": updated_count})
    except Exception as e:
        print(f"[SERVER] Error bulk setting covers: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/delete-pack', methods=['POST'])
@admin_required
def delete_pack():
    """Delete a pack by ID"""
    data = request.json
    pack_id = str(data.get('id'))
    
    if not pack_id:
        return jsonify({"error": "Missing pack ID"}), 400

    try:
        # We don't need run_on_scraper_thread for this sync operation, 
        # but for consistency and thread safety with the list, we can use it, 
        # or just access directly since list operations are atomic-ish in Python (GIL).
        # Better safe: use the scraper thread if we were doing complex stuff. 
        # But here a simple list comprehension is fast.
        # However, run_on_scraper_thread expects async coroutine if using that helper?
        # Wait, run_on_scraper_thread uses asyncio.run_coroutine_threadsafe.
        # But delete_pack is SYNC.
        # Let's make delete_pack strictly sync and just call it. 
        # Thread safety: modifying a list while reading it might be an issue?
        # The reader is on the asyncio loop thread. We are on Flask thread.
        # Ideally we should schedule it on the loop.
        
        # Let's make delete_pack async to be safe and use uniform mechanism.
        success = run_on_scraper_thread(scraper.delete_pack(pack_id))
        
        if success:
            return jsonify({"status": "ok", "id": pack_id})
        else:
            return jsonify({"error": "Pack not found"}), 404
            
    except Exception as e:
        print(f"[SERVER] Error deleting pack: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"Nintendo Reseller App running on HTTP port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
