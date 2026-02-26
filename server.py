from flask import Flask, jsonify, request, send_from_directory
import threading
import asyncio
import os
import json
from scraper import NintendoScraper

app = Flask(__name__)
scraper = NintendoScraper()

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
def home():
    return send_from_directory('ui', 'index.html')

@app.route('/cliente')
def cliente():
    return send_from_directory('ui/cliente', 'index.html')

@app.route('/cliente/<path:path>')
def serve_cliente_static(path):
    return send_from_directory('ui/cliente', path)

@app.route('/<path:path>')
def serve_static(path):
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

@app.route('/api/refresh', methods=['POST'])
def refresh():
    """Force refresh the pack cache - triggered manually by user"""
    count = int(request.args.get('count', 1000))
    print(f"[SERVER] Manual refresh triggered ({count} messages)...")
    try:
        result = run_on_scraper_thread(scraper.manual_refresh(count))
        return jsonify(result)
    except Exception as e:
        print(f"[SERVER] Refresh error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/set-cover', methods=['POST'])
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
    print("Nintendo Reseller App running on http://localhost:5000")
    app.run(port=5000, debug=True, use_reloader=False)
