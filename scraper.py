import asyncio
import re
import os
import time
import json
from playwright.async_api import async_playwright

# --- CONFIGURATION ---
SOURCE_CHAT = "evAn Accounts"
PRICE_MULTIPLIER = 3000
# Setup Volume Path for Railway Persistence
RAILWAY_VOLUME = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', os.getcwd())
USER_DATA_DIR = os.path.join(RAILWAY_VOLUME, "browser_data_clean")
CACHE_DURATION_SECONDS = 999999  # Manual refresh only (essentially infinite)

# Set of best-seller keywords for highlighting
BEST_SELLERS = set([
    "mario kart", "mario odyssey", "mario bros", "mario party", "mario maker",
    "zelda", "breath of the wild", "tears of the kingdom", "link's awakening",
    "pokemon", "pokémon",
    "animal crossing",
    "smash bros", "super smash",
    "splatoon",
    "kirby",
    "metroid",
    "fire emblem",
    "luigi's mansion", "pikmin", "xenoblade", "bayonetta",
])

# Load game covers from JSON file
GAME_COVERS = {}
DEFAULT_COVER = None
try:
    covers_path = os.path.join(os.path.dirname(__file__), 'game_covers.json')
    with open(covers_path, 'r', encoding='utf-8') as f:
        covers_data = json.load(f)
        GAME_COVERS = covers_data.get('covers', {})
        DEFAULT_COVER = covers_data.get('default', '')
except:
    pass  # No covers available


class GenericPack:
    def __init__(self, raw_text):
        self.raw_text = raw_text
        self.id = None
        self.games = []
        self.original_price = 0
        self.final_price = 0
        self.is_valid = False
        self._parse()

    def _parse(self):
        lines = self.raw_text.split('\n')
        id_found = False
        price_found = False
        game_lines = []

        for line in lines:
            clean_line = line.strip()
            if not clean_line:
                continue
            
            id_match = re.search(r"ID\s*:\s*(\d+)", clean_line, re.IGNORECASE)
            if id_match:
                self.id = id_match.group(1)
                id_found = True
                continue
            
            price_match = re.search(r"(\d+(?:\.\d+)?)\s*\$|\$\s*(\d+(?:\.\d+)?)", clean_line)
            if price_match:
                price_str = price_match.group(1) or price_match.group(2)
                self.original_price = int(float(price_str))
                self.final_price = self.original_price * PRICE_MULTIPLIER
                price_found = True
                continue

            if id_found and not price_found:
                if "NINTENDO SWITCH ACCOUNT" in clean_line: continue
                if "For buy:" in clean_line: continue
                game_lines.append(clean_line)

        self.games = game_lines
        self.is_valid = id_found and price_found and len(self.games) > 0

    def _is_best_seller(self, game_name):
        """Check if a game matches any best-seller keyword"""
        game_lower = game_name.lower()
        for keyword in BEST_SELLERS:
            if keyword in game_lower:
                return True
        return False

    def format_output(self):
        # Format games with 🔥 and *asterisks* for best-sellers
        formatted_games = []
        for game in self.games:
            if self._is_best_seller(game):
                formatted_games.append(f"🔥 *{game}*")
            else:
                formatted_games.append(game)
        
        games_list = "\n".join(formatted_games)
        formatted_price = f"{self.final_price:,.0f}".replace(",", ".")
        
        return f"ID : {self.id}\n\n---Lista de contenidos---\n{games_list}\n\nPrecio: ${formatted_price}"

    @property
    def content_hash(self):
        """Generate a hash based on games and price to detect duplicates"""
        # Sort games to ensure order doesn't matter
        sorted_games = sorted([g.lower().strip() for g in self.games])
        # Include price to differentiate same games with different prices (optional, user said "inconsistent prices")
        # If user wants to dedup same games even with different prices, we should EXCLUDE price.
        # User said "Same title different prices". So we should dedup by content ONLY.
        content_string = "|".join(sorted_games)
        return hash(content_string)

    def matches_filters(self, query, exclude):
        """Line-aware matching logic:
        1. If query is exactly the Pack ID, return True immediately
        2. Otherwise, Pack must contain ALL query keywords (anywhere)
        3. EXCLUDE only applies if the excluded keyword is on the SAME LINE as a query keyword
           (or any line if query is empty)
        """
        raw_query = query.strip()
        
        # 0. ID Match Check
        # If query is a number and matches ID exactly, return True (ignoring excludes)
        if raw_query.isdigit() and raw_query == self.id:
            return True

        query_parts = raw_query.lower().split()
        exclude_parts = exclude.lower().split()
        
        # 1. First check: Pack must contain ALL query keywords
        games_text_all = " ".join(self.games).lower()
        for kw in query_parts:
            if kw not in games_text_all:
                return False
        
        # 2. Exclusion check: line-aware
        if not exclude_parts:
            return True
            
        for game_line in self.games:
            line_lower = game_line.lower()
            
            # Is this line "relevant"? 
            # It's relevant if it contains any query keyword OR if the query is empty
            is_relevant = not query_parts or any(kw in line_lower for kw in query_parts)
            
            if is_relevant:
                # If a relevant line has any excluded keyword, reject the whole pack
                for ex_kw in exclude_parts:
                    if ex_kw in line_lower:
                        return False
        
        return True

    def get_cover_url(self):
        """Get cover URL for the first matching best-seller game in the pack"""
        games_text = " ".join(self.games).lower()
        
        # Sort keys by length (descending) to match specific titles ("mario odyssey") before generic ones ("mario")
        sorted_keys = sorted(GAME_COVERS.keys(), key=len, reverse=True)
        
        # Try to find a matching cover
        for keyword in sorted_keys:
            # STRICTER MATCHING: Keyword must be present as a distinct word or phrase
            # Check if keyword is in the text
            if keyword in games_text:
                return GAME_COVERS[keyword]
        
        return None # No default cover, return None to handle as empty in UI

    def to_dict(self, manual_covers=None):
        # 1. Check for manual override FIRST
        cover_url = None
        if manual_covers and self.id in manual_covers:
            cover_url = manual_covers[self.id]
        
        # 2. Fallback to automatic detection if no manual override
        if not cover_url:
            cover_url = self.get_cover_url()

        return {
            "id": self.id,
            "games": self.games,
            "price_usd": self.original_price,
            "price_local": self.final_price,
            "cover_url": cover_url,
            "formatted_text": self.format_output()
        }


class NintendoScraper:
    def __init__(self):
        self.playwright = None
        self.browser_context = None
        self.page = None
        self.is_running = False
        # Cache for scraped packs
        self.cached_packs = []
        self.cache_timestamp = 0
        
        # Load manual covers persistence
        self.manual_covers = {}
        self.load_manual_covers()

    def load_manual_covers(self):
        """Load manual cover overrides from JSON"""
        try:
            # Fallback to local if not in Railway
            railway_volume = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', os.path.dirname(__file__))
            path = os.path.join(railway_volume, 'manual_covers.json')
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    self.manual_covers = json.load(f)
            else:
                self.manual_covers = {}
        except Exception as e:
            print(f"[SCRAPER] Error loading manual covers: {e}")
            self.manual_covers = {}

    async def reload_manual_covers(self):
        """Reload manual covers (called by bulk update API)"""
        print("[SCRAPER] Reloading manual covers persistence...")
        self.load_manual_covers()
        return True

    async def update_manual_cover(self, pack_id, url):
        """Update a single manual cover in memory"""
        pack_id_str = str(pack_id)
        if url:
            self.manual_covers[pack_id_str] = url
            print(f"[SCRAPER] Set manual cover for {pack_id_str}: {url[:50]}...")
        elif pack_id_str in self.manual_covers:
            del self.manual_covers[pack_id_str]
            print(f"[SCRAPER] Removed manual cover for {pack_id_str}")
        return True

    async def delete_pack(self, pack_id):
        """Remove a pack from the cache by ID"""
        pack_id = str(pack_id)
        initial_count = len(self.cached_packs)
        self.cached_packs = [p for p in self.cached_packs if p.id != pack_id]
        deleted = initial_count - len(self.cached_packs)
        
        if deleted > 0:
            print(f"[SCRAPER] Deleted pack {pack_id} from cache")
            return True
        else:
            print(f"[SCRAPER] Pack {pack_id} not found in cache to delete")
            return False

    async def start(self):
        if self.is_running: return
        
        # Prevent "connect_over_cdp" crashes due to stale lock files
        print("[SCRAPER] Checking for stale Chromium lock files...")
        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lf_path = os.path.join(USER_DATA_DIR, lock_file)
            if os.path.exists(lf_path):
                try:
                    # In some environments, it's a symlink, so we unlink/remove
                    os.unlink(lf_path) if os.path.islink(lf_path) else os.remove(lf_path)
                    print(f"[SCRAPER] Removed old lock file: {lock_file}")
                except Exception as e:
                    print(f"[SCRAPER] Warning, could not remove {lock_file}: {e}")

        self.playwright = await async_playwright().start()
        is_server = bool(os.getenv('RAILWAY_VOLUME_MOUNT_PATH'))
        self.browser_context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=is_server,  # strictly enforce headless if on server
            args=[
                "--disable-blink-features=AutomationControlled", 
                "--disable-notifications", 
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ] + (["--headless"] if is_server else [])
        )
        
        pages = self.browser_context.pages
        self.page = pages[0] if pages else await self.browser_context.new_page()
        self.is_running = True

    async def ensure_telegram_login(self):
        if not self.is_running: await self.start()
        
        if "web.telegram.org" not in self.page.url:
            await self.page.goto("https://web.telegram.org/a/")

        try:
            # Espera a que aparezca la lista de chats o el canvas del QR
            await self.page.wait_for_selector(".chat-list, canvas", timeout=15000)
            
            # Comprobar si Telegram está pidiendo login (mostrando el QR)
            if await self.page.locator("canvas").is_visible():
                print("[LOGIN] Sesión no detectada o expirada. Generando captura del QR...")
                qr_path = os.path.join(os.path.dirname(__file__), 'ui', 'qr_login.png')

                # Wait for Telegram to actually DRAW the QR inside the canvas
                print("[LOGIN] Esperando 2.5s para que el QR se renderice...")
                await self.page.wait_for_timeout(2500)

                await self.page.screenshot(path=qr_path)
                print(f"[LOGIN] QR guardado en {qr_path}. Revisa /admin para escanearlo.")
                
                # Return False IMMEDIATELY so the frontend can show the QR
                # The next /api/status poll will check again and detect login
                return False
                
            # If we see chat-list, we are logged in! Remove any leftover QR image
            qr_path = os.path.join(os.path.dirname(__file__), 'ui', 'qr_login.png')
            if os.path.exists(qr_path):
                try: os.remove(qr_path)
                except: pass

            await self.page.wait_for_selector(".chat-list", timeout=5000)
            print("[LOGIN] Telegram conectado exitosamente.")
            return True
        except Exception as e:
            print(f"[LOGIN] Timeout o error conectando a Telegram: {e}")
            return False

    async def scrape_messages(self, message_count=250):
        """Scroll through chat and collect messages"""
        print(f"[SCRAPE] Collecting last {message_count} messages...")
        
        # IMPORTANT: Bring window to front and click to ensure focus
        await self.page.bring_to_front()
        try:
            # Click on the message area to ensure it has focus
            chat_area = self.page.locator('.messages-container, .MessageList, .chat-content, .bubbles').first
            await chat_area.click(force=True)
            await asyncio.sleep(0.3)
        except:
            # Fallback: click anywhere on the page
            await self.page.mouse.click(500, 400)
        
        all_texts = set()
        packs = []
        scroll_attempts = 0
        max_scrolls = max(50, message_count // 15)  # Scale scrolls with message count
        
        while len(all_texts) < message_count and scroll_attempts < max_scrolls:
            # Get all visible message text contents
            elements = await self.page.locator("div.text-content").all()
            
            for el in elements:
                try:
                    text = await el.inner_text()
                    if text and text not in all_texts:
                        all_texts.add(text)
                        # Try to parse as pack
                        pack = GenericPack(text)
                        if pack.is_valid:
                            # 1. Check for duplicate IDs
                            if any(p.id == pack.id for p in packs):
                                continue
                                
                            # 2. Check for duplicate CONTENT (same games)
                            # If content matches, keep the one with lower ID? Or just skip?
                            # Usually newer messages are better. We are scrolling UP (older).
                            # So existing packs in 'packs' list are NEWER because we found them first (scrolling up).
                            # If we find a duplicate of what we already have, it's an OLDER repost. Skip it.
                            if any(p.content_hash == pack.content_hash for p in packs):
                                continue

                            packs.append(pack)
                except:
                    continue
            
            print(f"[SCRAPE] Collected {len(all_texts)} messages, {len(packs)} valid packs")
            
            # Scroll up to load more messages
            await self.page.keyboard.press("Home")
            await asyncio.sleep(0.5)
            
            # Also try scrolling the message container
            try:
                await self.page.evaluate("""
                    const container = document.querySelector('.messages-container, .MessageList, [class*="messages"]');
                    if (container) container.scrollTop = 0;
                """)
            except:
                pass
            
            await asyncio.sleep(1)
            scroll_attempts += 1
        
        # Reverse order: newest messages first (we scrape from bottom going up)
        packs.reverse()

        print(f"[SCRAPE] Done! Found {len(packs)} valid packs")
        return packs

    async def search_game(self, query, limit=5, exclude=""):
        if not self.is_running: await self.start()

        is_logged_in = await self.ensure_telegram_login()
        if not is_logged_in:
            try:
                print("[LOGIN] Waiting for Telegram login...")
                await self.page.wait_for_selector(".chat-list", timeout=300000)
            except:
                raise Exception("Login Timeout")

        # Open target chat
        print(f"[CHAT] Opening '{SOURCE_CHAT}'...")
        chat = self.page.get_by_text(SOURCE_CHAT, exact=False).first
        await chat.click(force=True)
        await asyncio.sleep(2)

        # Check if we need to refresh the cache
        current_time = time.time()
        cache_age = current_time - self.cache_timestamp
        
        if not self.cached_packs or cache_age > CACHE_DURATION_SECONDS:
            print(f"[CACHE] Cache expired or empty (age: {cache_age:.0f}s). Re-scraping...")
            self.cached_packs = await self.scrape_messages(250)
            self.cache_timestamp = current_time
        else:
            print(f"[CACHE] Using cached data ({len(self.cached_packs)} packs)")

        # If no query and no exclusion, return all packs
        if not query.strip() and not exclude.strip():
            print(f"[SEARCH] No filters - returning all {len(self.cached_packs)} packs")
            return [p.to_dict(self.manual_covers) for p in self.cached_packs[:limit]]

        # Filter cached packs by query (multi-keyword search)
        print(f"[SEARCH] Filtering for: '{query}' (Exclude: '{exclude}')")
        matching_packs = []
        
        for pack in self.cached_packs:
            if pack.matches_filters(query, exclude):
                matching_packs.append(pack)

        print(f"[RESULT] Found {len(matching_packs)} matching packs")
        return [p.to_dict(self.manual_covers) for p in matching_packs[:limit]]

    async def manual_refresh(self, message_count=1000):
        """Force refresh of cached packs - called by user manually
        
        For small counts (<= 100): Incremental update - merge new packs without duplicates
        For large counts (> 100): Full refresh - replace all cached packs
        """
        if not self.is_running: await self.start()

        is_logged_in = await self.ensure_telegram_login()
        if not is_logged_in:
            try:
                print("[LOGIN] Waiting for Telegram login...")
                await self.page.wait_for_selector(".chat-list", timeout=300000)
            except:
                raise Exception("Login Timeout")

        # Open target chat
        print(f"[CHAT] Opening '{SOURCE_CHAT}'...")
        chat = self.page.get_by_text(SOURCE_CHAT, exact=False).first
        await chat.click(force=True)
        await asyncio.sleep(2)

        # Scrape messages
        print(f"[REFRESH] Scraping {message_count} messages...")
        new_packs = await self.scrape_messages(message_count)
        
        if message_count <= 100:
            # SYNC MODE: Sync cache with fresh 100 messages
            # 1. Remove packs from cache that are NOT in fresh 100 (deleted from Telegram)
            # 2. Add new packs from fresh 100 (avoiding duplicates)
            print("[REFRESH] Sync mode - syncing cache with fresh 100...")
            
            fresh_ids = {pack.id for pack in new_packs}
            
            # Step 1: Remove packs no longer in fresh 100
            old_count = len(self.cached_packs)
            self.cached_packs = [p for p in self.cached_packs if p.id in fresh_ids]
            removed_count = old_count - len(self.cached_packs)
            
            # Step 2: Add new packs from fresh 100 (skipping duplicates)
            existing_ids = {pack.id for pack in self.cached_packs}
            added_count = 0
            for new_pack in new_packs:
                if new_pack.id not in existing_ids:
                    self.cached_packs.insert(0, new_pack)  # Add to front (newest first)
                    existing_ids.add(new_pack.id)
                    added_count += 1
            
            print(f"[REFRESH] Removed {removed_count}, added {added_count}, total: {len(self.cached_packs)}")
        else:
            # FULL MODE: Replace all cached packs
            print("[REFRESH] Full refresh mode - replacing cache...")
            self.cached_packs = new_packs
        
        self.cache_timestamp = time.time()
        
        return {"status": "ok", "packs_found": len(self.cached_packs)}

    async def close(self):
        if self.browser_context:
            await self.browser_context.close()
        if self.playwright:
            await self.playwright.stop()
        self.is_running = False
