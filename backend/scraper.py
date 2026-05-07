import asyncio
import re
import os
import time
import json
import base64
import threading
from datetime import datetime
import requests
from html import unescape
from playwright.async_api import async_playwright

# --- CONFIGURATION ---
SOURCE_CHAT = "evAn Accounts"
PRICE_MULTIPLIER = 3000
RAILWAY_VOLUME = os.getenv('RAILWAY_VOLUME_MOUNT_PATH', os.getcwd())
USER_DATA_DIR = os.path.join(RAILWAY_VOLUME, "browser_data_clean")

# DLC Keywords for classification
DLC_KEYWORDS = ["only dlc", "expansion pass", "dlc", "upgrade pack", "season pass"]

# Known Nintendo first-party DLC titles that don't contain standard DLC keywords
# These get detected as full games otherwise
KNOWN_DLC_TITLES = [
    "happy home paradise",
    "octo expansion",
    "torna the golden country",
    "the ancient gods",
    "piranha plant standalone fighter",
    "fighters pass",
    "challenger pack",
    "mii fighter costume",
    "pase de expansión",
    "pase de expansion",
]

# Load game covers from JSON file
GAME_COVERS = {}
try:
    covers_path = os.path.join(os.path.dirname(__file__), 'game_covers.json')
    if os.path.exists(covers_path):
        with open(covers_path, 'r', encoding='utf-8') as f:
            covers_data = json.load(f)
            GAME_COVERS = covers_data.get('covers', {})
except:
    pass

class GenericPack:
    def __init__(self, raw_text, tg_msg_id=0):
        self.raw_text = raw_text
        self.tg_msg_id = tg_msg_id
        self.id = None
        self.games = []
        self.games_json = []  # List of dicts {name: str, is_dlc: bool}
        self.original_price = 0
        self.final_price = 0
        self.is_valid = False
        self._parse()

    def _parse(self):
        lines = self.raw_text.split('\n')
        id_found = False
        price_found = False
        
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
                
                # Check if it's a DLC line
                lower_line = clean_line.lower()
                is_dlc_trigger = any(kw in lower_line for kw in DLC_KEYWORDS) or any(title in lower_line for title in KNOWN_DLC_TITLES)
                is_mixed = "+" in lower_line and is_dlc_trigger
                
                is_dlc = is_dlc_trigger and not is_mixed
                
                # Translations for the UI
                translated_name = clean_line
                if "only dlc" in lower_line:
                    translated_name = re.sub(r"(?i)only dlc", "Solo DLC", translated_name)
                elif "upgrade pack" in lower_line:
                    translated_name = re.sub(r"(?i)upgrade pack", "- Mejora", translated_name)
                elif "expansion pass" in lower_line:
                    translated_name = re.sub(r"(?i)expansion pass", "Pase de Expansión", translated_name)
                
                self.games.append(clean_line) # raw original
                self.games_json.append({
                    "name": translated_name,
                    "is_dlc": is_dlc,
                    "is_mixed": is_mixed
                })

        self.is_valid = id_found and price_found and len(self.games) > 0

    @property
    def content_hash(self):
        """Generate a hash based on games to detect duplicates"""
        sorted_games = sorted([g.lower().strip() for g in self.games])
        content_string = "|".join(sorted_games)
        return hash(content_string)

    def get_cover_url(self):
        """Get cover URL for the first matching best-seller game in the pack"""
        games_text = " ".join(self.games).lower()
        sorted_keys = sorted(GAME_COVERS.keys(), key=len, reverse=True)
        for keyword in sorted_keys:
            if keyword in games_text:
                return GAME_COVERS[keyword]
        return None

    def to_dict(self):
        """Convert to dict structure matching the SQLite DB schema parameters"""
        return {
            "id": self.id,
            "tg_msg_id": self.tg_msg_id,
            "raw_text": self.raw_text,
            "games_json": self.games_json,
            "price_usd": self.original_price,
            "price_local": self.final_price,
            "cover_url": self.get_cover_url()
        }


class NintendoScraper:
    def __init__(self, db_instance):
        self.playwright = None
        self.browser_context = None
        self.page = None
        self.is_running = False
        self.telegram_connected = False
        self.db = db_instance
        self.progress = {"status": "idle", "current": 0, "total": 0, "message": ""}
        
        self.monitor_task = None

    async def start(self):
        if self.is_running: return
        
        print("[SCRAPER] Checking for stale Chromium lock files...")
        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            lf_path = os.path.join(USER_DATA_DIR, lock_file)
            if os.path.exists(lf_path):
                try:
                    os.unlink(lf_path) if os.path.islink(lf_path) else os.remove(lf_path)
                    print(f"[SCRAPER] Removed old lock file: {lock_file}")
                except Exception as e:
                    pass

        self.playwright = await async_playwright().start()
        is_server = bool(os.getenv('RAILWAY_VOLUME_MOUNT_PATH'))
        
        self.browser_context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=is_server,
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

    async def _restart_browser(self):
        print("[SCRAPER] ⚠️ Browser crashed! Restarting Chromium...")
        self.is_running = False
        self.telegram_connected = False
        try:
            if self.browser_context: await self.browser_context.close()
        except: pass
        try:
            if self.playwright: await self.playwright.stop()
        except: pass
        self.playwright = None
        self.browser_context = None
        self.page = None
        await asyncio.sleep(2)
        await self.start()
        print("[SCRAPER] ✅ Browser restarted successfully.")

    async def ensure_telegram_login(self):
        if hasattr(self, 'telegram_connected') and self.telegram_connected:
            return True

        if not self.is_running: await self.start()
        
        if "web.telegram.org" not in self.page.url:
            await self.page.goto("https://web.telegram.org/a/")

        try:
            try:
                await self.page.wait_for_selector(".chat-list", timeout=5000)
                print("[LOGIN] Telegram conectado exitosamente.")
                self.telegram_connected = True
                self.qr_base64 = None  # Clear any old QR
                return True
            except:
                pass
            
            print("[LOGIN] Sesión no detectada. Capturando QR...")
            await self._capture_qr()
            self.telegram_connected = False
            return False
            
        except Exception as e:
            print(f"[LOGIN] Timeout o error conectando a Telegram: {e}")
            self.telegram_connected = False
            return False

    async def _capture_qr(self):
        """Take a screenshot of the QR area and store as base64."""
        try:
            # Wait for the QR container (canvas or img) to appear
            qr_element = None
            try:
                qr_element = self.page.locator("canvas").first
                await qr_element.wait_for(timeout=10000, state="visible")
            except:
                # Some Telegram Web versions use an img for the QR
                try:
                    qr_element = self.page.locator(".qr-container img, .auth-image").first
                    await qr_element.wait_for(timeout=5000, state="visible")
                except:
                    pass
            
            # Brief wait for rendering
            await self.page.wait_for_timeout(1500)
            
            # Take screenshot of just the QR area, or full page as fallback
            if qr_element:
                screenshot_bytes = await qr_element.screenshot()
            else:
                screenshot_bytes = await self.page.screenshot()
            
            self.qr_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
            print(f"[LOGIN] QR capturado ({len(screenshot_bytes)} bytes)")
        except Exception as e:
            print(f"[LOGIN] Error capturando QR: {e}")
            # Full page screenshot as absolute fallback
            try:
                screenshot_bytes = await self.page.screenshot()
                self.qr_base64 = base64.b64encode(screenshot_bytes).decode('utf-8')
            except:
                self.qr_base64 = None

    async def refresh_qr(self):
        """Reload Telegram Web and save a fresh QR as PNG."""
        if not self.is_running: await self.start()
        qr_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ui', 'qr_login.png')

        try:
            if await self.page.query_selector(".chat-list"):
                self.telegram_connected = True
                return True
        except: pass

        try:
            print("[SCRAPER] Recargando Telegram Web para QR nuevo...")
            await self.page.goto("https://web.telegram.org/a/", wait_until="domcontentloaded")
            await self.page.wait_for_timeout(4000)  # wait for QR to render
            await self.page.screenshot(path=qr_path)
            print(f"[SCRAPER] QR guardado en {qr_path}")
            return False
        except Exception as e:
            print(f"[SCRAPER] Error refreshing QR: {e}")
            return False

    def get_qr_base64(self):
        return getattr(self, 'qr_base64', None)

    async def _open_chat(self):
        is_logged_in = await self.ensure_telegram_login()
        if not is_logged_in:
            try:
                await self.page.wait_for_selector(".chat-list", timeout=300000)
            except:
                raise Exception("Login Timeout")

        chat = self.page.get_by_text(SOURCE_CHAT, exact=False).first
        await chat.click(force=True)
        await asyncio.sleep(2)
        await self.page.bring_to_front()
        
        try:
            chat_area = self.page.locator('.messages-container, .MessageList, .chat-content, .bubbles').first
            await chat_area.click(force=True)
            await asyncio.sleep(0.3)
        except:
            await self.page.mouse.click(500, 400)

    # --- MODE 1: Scrape Today Only ---
    async def scrape_today(self, max_scrolls=7):
        """Scan the last ~100 messages. The DB layer handles deduplication:
        packs already in the catalog are skipped, only truly new ones are inserted."""
        self.progress = {"status": "running", "current": 0, "total": max_scrolls, "message": "Iniciando escaneo rápido..."}
        print("[SCRAPE] Starting 'Escanear Hoy' mode (last ~100 messages)...")
        await self._open_chat()
        
        all_texts = set()
        packs = []
        
        for scroll in range(max_scrolls):
            elements = await self.page.locator(".message, .Message, .bubble").all()
            for el in elements:
                try:
                    text_el = el.locator("div.text-content, .text-content, .message-text").first
                    text_content = await text_el.inner_text(timeout=500)
                    if text_content and text_content not in all_texts:
                        msg_id_str = await el.get_attribute("data-message-id") or await el.get_attribute("data-mid")
                        tg_msg_id = int(msg_id_str) if msg_id_str else 0
                        
                        all_texts.add(text_content)
                        pack = GenericPack(text_content, tg_msg_id)
                        if pack.is_valid:
                            if any(p.content_hash == pack.content_hash for p in packs):
                                continue
                            packs.append(pack)
                except: continue
                
            await self.page.keyboard.press("Home")
            await asyncio.sleep(1)
            self.progress["current"] = scroll + 1
            self.progress["message"] = f"Scroll {scroll+1}/{max_scrolls}. Encontrados {len(packs)} packs válidos..."
            print(f"[SCRAPE] Scroll {scroll+1}/{max_scrolls}, found {len(packs)} valid packs so far")
            
        packs.reverse()  # Newest first
        self.progress["message"] = "Guardando packs en el catálogo..."
        added = self.db.save_packs([p.to_dict() for p in packs], is_scrape_today=True)
        print(f"[SCRAPE] Finished. Scanned {len(packs)} packs total, {added} truly new packs added.")
        self.progress = {"status": "idle", "current": 0, "total": 0, "message": f"Completado. {added} packs nuevos añadidos."}
        return added

    # --- MODE 2: Full Scrape ---
    async def scrape_full(self, message_count=1000):
        max_scrolls = max(50, message_count // 15)
        self.progress = {"status": "running", "current": 0, "total": max_scrolls, "message": f"Iniciando escaneo de los últimos {message_count} mensajes..."}
        print(f"[SCRAPE] Full Scrape mode: {message_count} messages...")
        await self._open_chat()
        
        all_texts = set()
        packs = []
        
        for _ in range(max_scrolls):
            if len(all_texts) >= message_count: break
            
            elements = await self.page.locator(".message, .Message, .bubble").all()
            for el in elements:
                try:
                    text_el = el.locator("div.text-content, .text-content, .message-text").first
                    text_content = await text_el.inner_text(timeout=500)
                    if text_content and text_content not in all_texts:
                        msg_id_str = await el.get_attribute("data-message-id") or await el.get_attribute("data-mid")
                        tg_msg_id = int(msg_id_str) if msg_id_str else 0
                        
                        all_texts.add(text_content)
                        pack = GenericPack(text_content, tg_msg_id)
                        if pack.is_valid:
                            if any(p.content_hash == pack.content_hash for p in packs):
                                continue
                            packs.append(pack)
                except: continue
                
            await self.page.keyboard.press("Home")
            await asyncio.sleep(0.5)
            self.progress["current"] = _ + 1
            self.progress["message"] = f"Scroll {_+1}/{max_scrolls}. Encontrados {len(packs)} packs históricos..."
            
        packs.reverse()
        self.progress["message"] = "Reconstruyendo catálogo en base de datos..."
        # In a full scrape, we do NOT flag packs as "is_new". We just build the catalog.
        self.db.save_packs([p.to_dict() for p in packs], is_scrape_today=False)
        print(f"[SCRAPE] Full Scrape Done. Guardados {len(packs)} packs en la base de datos.")
        self.progress = {"status": "idle", "current": 0, "total": 0, "message": f"Escaneo histórico finalizado. {len(packs)} packs guardados."}
        return len(packs)

    # --- MODE 3: Verify Deleted (Sync IDs) ---
    async def verify_deleted(self):
        """Verifies if packs have been deleted from the channel by scanning recent messages.
        Has multiple safety guards to prevent accidental mass deletion."""
        print("[VERIFY] Starting 'Verify Deleted' mode using passive scan...")
        max_scrolls = 35
        self.progress = {"status": "running", "current": 0, "total": max_scrolls, "message": "Abriendo canal de Telegram..."}
        await self._open_chat()
        
        db_ids = self.db.get_all_active_pack_ids()
        if not db_ids:
            print("[VERIFY] No active packs in DB. Nothing to verify.")
            self.progress = {"status": "idle", "current": 0, "total": 0, "message": "No hay packs activos en la base de datos."}
            return 0
            
        total_db_packs = len(db_ids)
        print(f"[VERIFY] Auditing {total_db_packs} active packs stored in database...")
        
        # 1. Scan the last ~500 messages to collect active IDs
        active_ids_in_tg = set()
        all_texts = set()
        
        for scroll_i in range(max_scrolls):
            elements = await self.page.locator(".message, .Message, .bubble").all()
            for el in elements:
                try:
                    text_el = el.locator("div.text-content, .text-content, .message-text").first
                    text_content = await text_el.inner_text(timeout=500)
                    if text_content and text_content not in all_texts:
                        all_texts.add(text_content)
                        pack = GenericPack(text_content, 0)
                        if pack.is_valid:
                            active_ids_in_tg.add(str(pack.id))
                except: continue
                
            await self.page.keyboard.press("Home")
            await asyncio.sleep(1)
            self.progress["current"] = scroll_i + 1
            self.progress["message"] = f"Scroll {scroll_i+1}/{max_scrolls}. IDs encontrados en Telegram: {len(active_ids_in_tg)}..."
            print(f"[VERIFY] Scroll {scroll_i+1}/{max_scrolls}, found {len(active_ids_in_tg)} IDs in Telegram so far")
        
        # ========== SAFETY GUARD 1 ==========
        # If we found very few packs in the scan, something is wrong (chat not loaded, etc.)
        # ABORT to prevent accidental mass deletion.
        MIN_SCAN_THRESHOLD = 10
        if len(active_ids_in_tg) < MIN_SCAN_THRESHOLD:
            print(f"[VERIFY] ⚠️ SAFETY ABORT: Only found {len(active_ids_in_tg)} packs in Telegram scan.")
            print(f"[VERIFY] This is below the minimum threshold of {MIN_SCAN_THRESHOLD}.")
            print(f"[VERIFY] The chat likely didn't load properly. NO packs were deleted.")
            self.progress = {"status": "idle", "current": 0, "total": 0, "message": f"⚠️ Abortado: solo se encontraron {len(active_ids_in_tg)} IDs (mínimo {MIN_SCAN_THRESHOLD}). El chat puede no haber cargado correctamente."}
            return 0
            
        # 2. Find which ones to delete
        to_delete = []
        for pack_id in db_ids:
            str_id = str(pack_id)
            if str_id.startswith("MANUAL-"):
                continue # Manually added packs are always safe
                
            if str_id not in active_ids_in_tg:
                to_delete.append(str_id)
        
        # ========== SAFETY GUARD 2 ==========
        # If the operation would delete more than 60% of the database, abort.
        # This prevents catastrophic wipes from scan errors.
        scraped_pack_count = len([pid for pid in db_ids if not str(pid).startswith("MANUAL-")])
        if scraped_pack_count > 0 and len(to_delete) > scraped_pack_count * 0.6:
            print(f"[VERIFY] ⚠️ SAFETY ABORT: Would delete {len(to_delete)} of {scraped_pack_count} scraped packs (>{60}%).")
            print(f"[VERIFY] This looks like a scan error, not real deletions. NO packs were deleted.")
            self.progress = {"status": "idle", "current": 0, "total": 0, "message": f"⚠️ Abortado: se iban a eliminar {len(to_delete)} de {scraped_pack_count} packs (>60%). Posible error de escaneo."}
            return 0
        
        # 3. Actually delete
        self.progress["message"] = f"Eliminando {len(to_delete)} packs caducados de la DB..."
        deleted_count = 0
        for str_id in to_delete:
            self.db.mark_pack_deleted(str_id, manual=False)
            deleted_count += 1
            print(f"[VERIFY] Pack #{str_id} no longer in recent Telegram feed. Removing from DB.")
                
        print(f"[VERIFY] Audit Complete. Scanned {len(active_ids_in_tg)} IDs in Telegram. Packs removed: {deleted_count}")
        self.progress = {"status": "idle", "current": 0, "total": 0, "message": f"Verificación completa. {deleted_count} packs eliminados del catálogo."}
        return deleted_count

    # --- MODE 4: (removed) ---

    async def close(self):
        if self.browser_context:
            await self.browser_context.close()
        if self.playwright:
            await self.playwright.stop()
        self.is_running = False


class AmazonJpPriceScraper:
    """Best-effort Amazon Japan listing scraper (price + image + title)."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        })

    @staticmethod
    def extract_asin(url):
        for pat in (r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})", r"asin=([A-Z0-9]{10})"):
            m = re.search(pat, url or "", re.IGNORECASE)
            if m:
                return m.group(1).upper()
        return None

    @staticmethod
    def _normalize_price(value):
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        txt = str(value).replace(",", "").replace("¥", "").replace("￥", "").strip()
        m = re.search(r"(\d+(?:\.\d+)?)", txt)
        return float(m.group(1)) if m else None

    @staticmethod
    def _pick_image_url(html):
        patterns = [
            r'<meta\s+property="og:image"\s+content="([^"]+)"',
            r'"landingImageUrl"\s*:\s*"([^"]+)"',
            r'"hiRes"\s*:\s*"([^"]+)"',
            r'"large"\s*:\s*"([^"]+)"'
        ]
        for pat in patterns:
            m = re.search(pat, html, re.IGNORECASE)
            if m and m.group(1):
                return unescape(m.group(1)).replace("\\/", "/")
        return None

    @staticmethod
    def _pick_title(html):
        m = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        return re.sub(r"\s+", " ", unescape(m.group(1))).strip()

    def _pick_prices(self, html):
        offer_candidates = []
        list_candidates = []

        offer_patterns = [
            r'"priceToPay"\s*:\s*\{[^}]*"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            r'"apex_desktop"\s*:\s*\{[^}]*"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            r'id="priceblock_dealprice"[^>]*>\s*[¥￥]?\s*([0-9,]+)',
            r'id="priceblock_ourprice"[^>]*>\s*[¥￥]?\s*([0-9,]+)',
            r'class="a-price-whole">\s*([0-9,]+)\s*<',
        ]
        list_patterns = [
            r'"listPrice"\s*:\s*\{[^}]*"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            r'id="priceblock_listprice"[^>]*>\s*[¥￥]?\s*([0-9,]+)',
            r'class="a-text-price"[^>]*>\s*<span[^>]*>[¥￥]?\s*([0-9,]+)',
        ]

        for pat in offer_patterns:
            for match in re.findall(pat, html, re.IGNORECASE | re.DOTALL):
                val = self._normalize_price(match)
                if val and val > 0:
                    offer_candidates.append(val)

        for pat in list_patterns:
            for match in re.findall(pat, html, re.IGNORECASE | re.DOTALL):
                val = self._normalize_price(match)
                if val and val > 0:
                    list_candidates.append(val)

        offer_price = min(offer_candidates) if offer_candidates else None
        list_price = max(list_candidates) if list_candidates else None
        if offer_price and list_price and list_price < offer_price:
            list_price = None
        return offer_price, list_price

    def scrape_listing(self, url, timeout=20):
        asin = self.extract_asin(url)
        if not asin:
            raise ValueError("No se pudo detectar ASIN en la URL")

        resp = self.session.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code >= 400:
            raise RuntimeError(f"Amazon devolvió HTTP {resp.status_code}")

        html = resp.text or ""
        offer_price, list_price = self._pick_prices(html)
        if offer_price is None and list_price is None:
            raise RuntimeError("No se pudo extraer precio de la publicación")

        price_jpy = offer_price or list_price
        is_on_sale = bool(list_price and price_jpy and list_price > price_jpy)

        return {
            "asin": asin,
            "title_source": self._pick_title(html),
            "image_url": self._pick_image_url(html),
            "price_jpy": price_jpy,
            "list_price_jpy": list_price,
            "is_on_sale": is_on_sale,
        }
