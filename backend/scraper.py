import asyncio
import re
import os
import time
import json
import base64
import threading
import logging
from datetime import datetime
import requests
from html import unescape
from urllib.parse import unquote
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

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
    """Amazon Japan listing scraper — HTTP first, Playwright fallback (Amazon blocks naive bots)."""

    _CHROME_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self._CHROME_UA,
            "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        })
        self._batch_depth = 0
        self._pw_sync = None
        self._browser_sync = None
        self._context_sync = None

    @staticmethod
    def extract_asin(url):
        raw = (url or "").strip()
        decoded = unquote(raw)
        patterns = (
            r"/dp/([A-Z0-9]{10})",
            r"/gp/product/([A-Z0-9]{10})",
            r"/gp/aw/d/([A-Z0-9]{10})",
            r"/product/([A-Z0-9]{10})",
            r"/dp%2F([A-Z0-9]{10})",
            r"[?&](?:asin|pd_rd_i)=([A-Z0-9]{10})",
        )
        for pat in patterns:
            m = re.search(pat, raw, re.IGNORECASE) or re.search(pat, decoded, re.IGNORECASE)
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
            r"<meta\s+property='og:image'\s+content='([^']+)'",
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

    def batch_begin(self):
        """Reuse one Chromium instance across many listings (caller should pair with batch_end)."""
        self._batch_depth += 1
        if self._batch_depth == 1:
            self._ensure_playwright_sync()

    def batch_end(self):
        if self._batch_depth <= 0:
            self._shutdown_playwright_sync()
            self._batch_depth = 0
            return
        self._batch_depth -= 1
        if self._batch_depth == 0:
            self._shutdown_playwright_sync()

    def _ensure_playwright_sync(self):
        if self._context_sync is not None:
            return
        is_server = bool(os.getenv("RAILWAY_VOLUME_MOUNT_PATH"))
        base_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        if is_server:
            base_args.extend(["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"])
        self._pw_sync = sync_playwright().start()
        self._browser_sync = self._pw_sync.chromium.launch(headless=True, args=base_args)
        self._context_sync = self._browser_sync.new_context(
            locale="ja-JP",
            user_agent=self._CHROME_UA,
            viewport={"width": 1366, "height": 768},
        )
        # Force Amazon to display prices in JPY regardless of server location
        self._context_sync.add_cookies([
            {"name": "i18n-prefs",    "value": "currency=JPY", "domain": ".amazon.co.jp", "path": "/"},
            {"name": "lc-acbjp",      "value": "ja_JP",        "domain": ".amazon.co.jp", "path": "/"},
            {"name": "sp-cdn",        "value": "L5Z9:JP",      "domain": ".amazon.co.jp", "path": "/"},
        ])

    def _shutdown_playwright_sync(self):
        ctx = self._context_sync
        br = self._browser_sync
        pw = self._pw_sync
        self._context_sync = None
        self._browser_sync = None
        self._pw_sync = None
        for obj in (ctx, br):
            if obj:
                try:
                    obj.close()
                except Exception:
                    pass
        if pw:
            try:
                pw.stop()
            except Exception:
                pass

    @staticmethod
    def _html_usable(html):
        if not html or len(html) < 8000:
            return False
        low = html.lower()
        if "validatecaptcha" in low or "/errors/validatecaptcha" in low:
            return False
        if "enter the characters you see below" in low and "sorry" in low:
            return False
        if "api-services-support@amazon" in low and len(html) < 20000:
            return False
        return any(
            x in low
            for x in ("pricetopay", "priceblock", "a-price-whole", "a-offscreen", "apex_desktop")
        )

    def _read_buybox_jpy_playwright(self, page):
        """Read visible PDP buy-box price first (strict), then fallback selectors."""
        offer = list_price = None

        # Strict priority: the exact displayed price block on PDP.
        pay_selectors = [
            "#corePriceDisplay_desktop_feature_div .reinventPricePriceToPayMargin .a-offscreen",
            "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
            "#apex_desktop #corePriceDisplay_desktop_feature_div .a-offscreen",
            "#apex_desktop .reinventPricePriceToPayMargin .a-offscreen",
            "#buybox .reinventPricePriceToPayMargin .a-offscreen",
            "#twister-plus-price-data-price .a-offscreen",
            ".reinventPricePriceToPayMargin .a-offscreen",
        ]
        list_selectors = [
            ".basisPrice .a-offscreen",
            ".a-price.a-text-price .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price.a-text-price .a-offscreen",
        ]

        for sel in pay_selectors:
            try:
                t = page.locator(sel).first.inner_text(timeout=4500)
                v = self._normalize_price(t)
                if v and v >= 300:
                    offer = v
                    break
            except Exception:
                continue

        cand_lists = []
        for sel in list_selectors:
            try:
                for el in page.locator(sel).all()[:6]:
                    t = el.inner_text(timeout=600)
                    v = self._normalize_price(t)
                    if v and v >= 300:
                        cand_lists.append(v)
            except Exception:
                continue
        list_price = max(cand_lists) if cand_lists else None
        if offer and list_price and list_price <= offer:
            list_price = None
        return offer, list_price

    def _read_prices_js(self, page):
        """Use JS evaluation in browser context — most robust against layout changes."""
        try:
            result = page.evaluate("""() => {
                const norm = t => {
                    if (!t) return '';
                    return t.replace(/[¥￥$USD,\\s\\u00a5\\u20b9]/g, '').trim();
                };
                const numVal = t => {
                    const n = norm(t);
                    const m = n.match(/^(\\d+)(\\.\\d+)?$/);
                    return m ? parseFloat(m[0]) : null;
                };
                const first = sels => {
                    for (const s of sels) {
                        try {
                            for (const el of document.querySelectorAll(s)) {
                                const v = numVal(el.textContent);
                                if (v && v >= 300) return v;
                            }
                        } catch(e) {}
                    }
                    return null;
                };

                // Also try reading .a-price-whole + .a-price-fraction
                const fromWholeElements = (container) => {
                    const wholes = container.querySelectorAll('.a-price-whole');
                    for (const w of wholes) {
                        const priceEl = w.closest('.a-price');
                        if (priceEl && priceEl.classList.contains('a-text-price')) continue;
                        const raw = norm(w.textContent);
                        if (raw && /^\\d+$/.test(raw)) {
                            const v = parseInt(raw, 10);
                            if (v >= 300) return v;
                        }
                    }
                    return null;
                };

                let offer = first([
                    '#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen',
                    '#corePriceDisplay_desktop_feature_div .reinventPricePriceToPayMargin .a-offscreen',
                    '#apex_desktop .reinventPricePriceToPayMargin .a-offscreen',
                    '.reinventPricePriceToPayMargin .a-offscreen',
                    '#buybox .a-price:not(.a-text-price) .a-offscreen',
                    '#newBuyBoxPrice',
                    '#price_inside_buybox',
                    '.priceToPay .a-offscreen',
                    '#priceblock_ourprice',
                    '#priceblock_dealprice',
                    '#corePrice_feature_div .a-price:not(.a-text-price) .a-price-whole',
                    '#apex_desktop .a-price:not(.a-text-price) .a-price-whole',
                ]);

                // Fallback: look at a-price-whole in buybox containers
                if (!offer) {
                    const containers = document.querySelectorAll(
                        '#corePrice_feature_div, #corePriceDisplay_desktop_feature_div, #apex_desktop, #buybox, #price'
                    );
                    for (const c of containers) {
                        offer = fromWholeElements(c);
                        if (offer) break;
                    }
                }

                // Last resort: find any ¥X,XXX pattern in the buybox area
                if (!offer) {
                    const bb = document.querySelector('#rightCol, #buyBoxAccordion, #buybox, #corePrice_feature_div, #apex_desktop');
                    if (bb) {
                        const text = bb.textContent || '';
                        const m = text.match(/[¥￥]\\s*([\\d,]+)/);
                        if (m) {
                            const v = parseInt(m[1].replace(/,/g, ''), 10);
                            if (v >= 300) offer = v;
                        }
                    }
                }

                const list = first([
                    '.basisPrice .a-offscreen',
                    '.a-price.a-text-price .a-offscreen',
                    '#priceblock_listprice',
                    '#listPrice',
                    '.a-price.a-text-price .a-price-whole',
                ]);

                return {offer: offer || null, list: list || null};
            }""")
            o = result.get("offer") if result else None
            l = result.get("list") if result else None
            if o and l and l <= o:
                l = None
            return (float(o) if o and o >= 300 else None,
                    float(l) if l and l >= 300 else None)
        except Exception as e:
            logging.warning("JS price eval error: %s", e)
            return None, None

    def _read_buybox_text_prices_js(self, page):
        """
        Parse visible buybox text directly. This is robust when DOM classes are A/B-tested.
        Returns (jpy_price, usd_price).
        """
        try:
            result = page.evaluate("""() => {
                const box = document.querySelector(
                    '#corePrice_feature_div, #corePriceDisplay_desktop_feature_div, #apex_desktop, #buybox, #rightCol'
                );
                const text = (box ? box.innerText : document.body.innerText || '').slice(0, 12000);
                const yen = text.match(/[¥￥]\\s*([\\d,]{2,})/);
                const usdA = text.match(/USD\\s*([0-9]+(?:\\.[0-9]+)?)/i);
                const usdB = text.match(/\\$\\s*([0-9]+(?:\\.[0-9]+)?)/);
                const listA = text.match(/(?:参考価格|通常価格|List\\s*Price|Price)\\s*[¥￥]?\\s*([\\d,]{2,})/i);
                return {
                    yen: yen ? yen[1] : null,
                    usd: usdA ? usdA[1] : (usdB ? usdB[1] : null),
                    listYen: listA ? listA[1] : null
                };
            }""")
            jpy = self._normalize_price(result.get("yen")) if result else None
            usd = self._normalize_price(result.get("usd")) if result else None
            list_jpy = self._normalize_price(result.get("listYen")) if result else None
            if jpy and jpy < 300:
                jpy = None
            if usd and not (1 <= usd <= 300):
                usd = None
            if list_jpy and list_jpy < 300:
                list_jpy = None
            if jpy and list_jpy and list_jpy <= jpy:
                list_jpy = None
            return jpy, usd, list_jpy
        except Exception as e:
            logging.warning("Buybox text price eval error: %s", e)
            return None, None, None

    def _fetch_with_playwright(self, urls, nav_timeout_ms=55000, settle_ms=3000):
        last_error = None
        try:
            self._ensure_playwright_sync()
            page = self._context_sync.new_page()
        except Exception as e:
            return "", None, None, f"Playwright init error: {type(e).__name__}: {e}"

        last_html = ""
        last_dom_offer, last_dom_list = None, None
        try:
            for url in urls:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                    page.wait_for_timeout(settle_ms)
                    try:
                        page.wait_for_selector(
                            ".reinventPricePriceToPayMargin, #corePriceDisplay_desktop_feature_div,"
                            " #buybox, #corePrice_feature_div",
                            timeout=15000,
                        )
                    except Exception:
                        pass

                    title = page.title()
                    last_html = page.content()
                    low = last_html.lower()
                    markers = {k: (k in low) for k in
                               ["captcha", "a-offscreen", "a-price-whole", "priceblock",
                                "pricetopay", "apex_desktop"]}
                    logging.info("AmazonJP PW url=%s title=%r html_len=%d markers=%s",
                                 url[:80], title, len(last_html), markers)

                    # Try JS evaluation first (most reliable), then CSS selectors
                    dom_offer, dom_list = self._read_prices_js(page)
                    if dom_offer is None:
                        dom_offer, dom_list = self._read_buybox_jpy_playwright(page)
                    text_jpy, text_usd, text_list_jpy = self._read_buybox_text_prices_js(page)
                    if dom_offer is None and text_jpy:
                        dom_offer = text_jpy
                    if dom_list is None and text_list_jpy:
                        dom_list = text_list_jpy

                    logging.info(
                        "AmazonJP prices dom_offer=%s dom_list=%s text_jpy=%s text_list_jpy=%s text_usd=%s",
                        dom_offer, dom_list, text_jpy, text_list_jpy, text_usd
                    )

                    last_dom_offer, last_dom_list = dom_offer, dom_list
                    if self._html_usable(last_html):
                        # Pass USD value inside error channel when no JPY was found.
                        usd_hint = f"USD_BUYBOX:{text_usd}" if (text_usd and not dom_offer) else None
                        return last_html, dom_offer, dom_list, usd_hint
                except Exception as e:
                    last_error = f"{type(e).__name__}: {e}"
                    logging.warning("AmazonJP PW attempt failed url=%s err=%s", url[:80], last_error)
                    continue
            return last_html, last_dom_offer, last_dom_list, last_error
        finally:
            page.close()

    def _pick_prices(self, html):
        MIN_VALID_JPY = 300
        offer_candidates = []
        list_candidates = []
        hard_offer_candidates = []

        # More reliable structured values (highest priority)
        hard_offer_patterns = [
            r'"priceToPay"\s*:\s*\{[^}]*"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            r'"apex_desktop"\s*:\s*\{[^}]*"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            r'\\"priceToPay\\"\s*:\s*\{[^}]*\\"priceAmount\\"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            r'\\"apex_desktop\\"\s*:\s*\{[^}]*\\"priceAmount\\"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        ]
        offer_patterns = [
            r'"displayPrice"\s*:\s*"[¥￥]\s*([0-9,]+)"',
            r'id="priceblock_dealprice"[^>]*>\s*[¥￥]?\s*([0-9,]+)',
            r'id="priceblock_ourprice"[^>]*>\s*[¥￥]?\s*([0-9,]+)',
            r'class="a-offscreen">\s*[¥￥]\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]{4,7})\s*<',
            r'class="a-price-whole">\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]{4,7})\s*<',
            r'[¥￥]\s*([0-9]{1,3}(?:,[0-9]{3})+)',
            r'[¥￥]\s*([0-9]{4,7})',
        ]
        list_patterns = [
            r'"listPrice"\s*:\s*\{[^}]*"priceAmount"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            r'\\"listPrice\\"\s*:\s*\{[^}]*\\"priceAmount\\"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            r'id="priceblock_listprice"[^>]*>\s*[¥￥]?\s*([0-9,]+)',
            r'class="a-text-price"[^>]*>\s*<span[^>]*>[¥￥]?\s*([0-9,]+)',
            r'参考価格[^0-9¥￥]{0,20}[¥￥]?\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,7})',
            r'通常価格[^0-9¥￥]{0,20}[¥￥]?\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,7})',
        ]
        jpy_meta_patterns = [
            r'"currency"\s*:\s*"JPY"\s*,\s*"value"\s*:\s*"?(?P<val>[0-9]+(?:\.[0-9]+)?)',
            r'\\"currency\\"\s*:\s*\\"JPY\\"\s*,\s*\\"value\\"\s*:\s*\\"?(?P<val>[0-9]+(?:\.[0-9]+)?)',
            r'\\u00a5\s*([0-9]{1,3}(?:,[0-9]{3})+)',
            r'&#165;\s*([0-9]{1,3}(?:,[0-9]{3})+)',
        ]

        for pat in hard_offer_patterns:
            for match in re.findall(pat, html, re.IGNORECASE | re.DOTALL):
                val = self._normalize_price(match)
                if val and val >= MIN_VALID_JPY:
                    hard_offer_candidates.append(val)

        for pat in offer_patterns:
            for match in re.findall(pat, html, re.IGNORECASE | re.DOTALL):
                val = self._normalize_price(match)
                # Ignore tiny values that usually come from unrelated tokens
                if val and val >= MIN_VALID_JPY:
                    offer_candidates.append(val)

        for pat in list_patterns:
            for match in re.findall(pat, html, re.IGNORECASE | re.DOTALL):
                val = self._normalize_price(match)
                if val and val >= MIN_VALID_JPY:
                    list_candidates.append(val)
        for pat in jpy_meta_patterns:
            for match in re.findall(pat, html, re.IGNORECASE | re.DOTALL):
                if isinstance(match, tuple):
                    match = match[0]
                val = self._normalize_price(match)
                if val and val >= MIN_VALID_JPY:
                    offer_candidates.append(val)

        # Prefer structured prices first; fallback to lowest visible valid price
        offer_price = min(hard_offer_candidates) if hard_offer_candidates else (min(offer_candidates) if offer_candidates else None)
        list_price = max(list_candidates) if list_candidates else None
        if offer_price and list_price and list_price < offer_price:
            list_price = None
        return offer_price, list_price

    def _pick_yen_loose(self, html):
        """
        Emergency fallback when structured selectors fail (common with geo/A-B variants).
        Extract visible Yen amounts and pick the first plausible buybox-like value.
        """
        candidates = []
        patterns = [
            r'[¥￥]\s*([0-9]{1,3}(?:,[0-9]{3})+)',
            r'[¥￥]\s*([0-9]{4,7})',
            r'a-price-whole">\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]{4,7})\s*<',
        ]
        for pat in patterns:
            for m in re.findall(pat, html, re.IGNORECASE | re.DOTALL):
                v = self._normalize_price(m)
                if v and 300 <= v <= 500000:
                    candidates.append(v)
        if not candidates:
            return None
        # Prefer mid-range game prices and ignore tiny accessory/noise values.
        for v in candidates:
            if v >= 1000:
                return v
        return candidates[0]

    def _infer_list_price_from_yen_context(self, html, offer_price):
        """
        If offer was found but list price is missing, infer it from nearby Yen values.
        Works for variants like: "List Price: ¥7,100" + current "¥6,264".
        """
        if not offer_price:
            return None
        low = (html or "").lower()

        # First, prefer explicit list-price labels around Yen values.
        labeled_patterns = [
            r'(?:list\s*price|参考価格|通常価格|price)\s*[:：]?\s*[¥￥]\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,7})',
            r'(?:basisprice|a-text-price)[^¥￥]{0,120}[¥￥]\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,7})',
        ]
        labeled = []
        for pat in labeled_patterns:
            for m in re.findall(pat, html, re.IGNORECASE | re.DOTALL):
                v = self._normalize_price(m)
                if v and v > offer_price and v <= offer_price * 3:
                    labeled.append(v)
        if labeled:
            return max(labeled)

        # Fallback: gather all Yen values from the HTML and pick the nearest higher value.
        vals = []
        for m in re.findall(r'[¥￥]\s*([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,7})', html, re.IGNORECASE):
            v = self._normalize_price(m)
            if v and 300 <= v <= 500000:
                vals.append(v)
        higher = sorted({v for v in vals if v > offer_price and v <= offer_price * 3})
        return higher[0] if higher else None

    def _pick_usd_loose(self, html):
        """Fallback when Amazon geo-renders buybox price in USD."""
        candidates = []
        patterns = [r'USD\s*([0-9]+(?:\.[0-9]+)?)', r'\$\s*([0-9]+(?:\.[0-9]+)?)']
        for pat in patterns:
            for m in re.findall(pat, html, re.IGNORECASE | re.DOTALL):
                if isinstance(m, tuple):
                    whole = (m[0] or "").replace(",", "")
                    frac = m[1] or ""
                    raw = f"{whole}.{frac}" if frac else whole
                else:
                    raw = m
                v = self._normalize_price(raw)
                if v and 1 <= v <= 300:
                    candidates.append(v)
        # Variant: whole/fraction split where USD appears nearby in HTML.
        for m in re.finditer(
            r'a-price-whole">\s*([0-9]{1,3}).{0,120}?a-price-fraction">\s*([0-9]{2})',
            html,
            re.IGNORECASE | re.DOTALL,
        ):
            window = html[max(0, m.start() - 220):m.end() + 30]
            if "USD" not in window.upper() and "$" not in window:
                continue
            raw = f"{m.group(1)}.{m.group(2)}"
            v = self._normalize_price(raw)
            if v and 1 <= v <= 300:
                candidates.append(v)
        return max(candidates) if candidates else None

    def _resolve_short_amazon_url(self, url, timeout=8):
        """Resolve amzn.asia/amzn.to links to final amazon.co.jp product URL."""
        raw = (url or "").strip()
        if not raw:
            return raw
        if any(h in raw.lower() for h in ("amzn.asia", "amzn.to")):
            try:
                r = self.session.get(raw, timeout=timeout, allow_redirects=True)
                final_url = (r.url or raw).strip()
                if "amazon.co.jp" in final_url:
                    return final_url
                return final_url
            except Exception as e:
                logging.warning("Amazon short URL resolve failed for %s: %s", raw, e)
                return raw
        return raw

    def scrape_listing(self, url, timeout=12, usd_jpy_rate=150.0):
        input_url = (url or "").strip()
        resolved_url = self._resolve_short_amazon_url(input_url, timeout=min(timeout, 8))
        asin = self.extract_asin(resolved_url) or self.extract_asin(input_url)
        if not asin:
            raise ValueError("No se pudo detectar ASIN en la URL")

        standalone = self._batch_depth == 0
        if standalone:
            self.batch_begin()

        try:
            # Always use Japanese URLs — -/en/ variants cause USD pricing on non-JP IPs
            try_urls = [
                f"https://www.amazon.co.jp/dp/{asin}?th=1&psc=1",
                f"https://www.amazon.co.jp/gp/product/{asin}",
                resolved_url,
                input_url,
            ]

            # Try lightweight HTTP first; Amazon usually blocks it, so Playwright is the real path.
            html = ""
            last_status = None
            for candidate in try_urls:
                try:
                    resp = self.session.get(candidate, timeout=timeout, allow_redirects=True)
                    last_status = resp.status_code
                except requests.RequestException:
                    continue
                if resp.status_code < 400 and resp.text and self._html_usable(resp.text):
                    html = resp.text
                    break

            dom_offer = dom_list = pw_error = None
            if not self._html_usable(html):
                # HTTP was blocked (CAPTCHA etc.) — use Playwright
                pw_html, dom_offer, dom_list, pw_error = self._fetch_with_playwright(try_urls)
                if self._html_usable(pw_html):
                    html = pw_html
            else:
                # HTTP worked — still read DOM prices via Playwright for accuracy
                pw_html, dom_offer, dom_list, pw_error = self._fetch_with_playwright(try_urls)
                if self._html_usable(pw_html):
                    html = pw_html

            if not self._html_usable(html):
                logging.warning("Amazon JP unusable HTML for ASIN %s http=%s pw_err=%s html_len=%d",
                                asin, last_status, pw_error, len(html))
                raise RuntimeError(
                    "Amazon no devolvió la página del producto (CAPTCHA o bloqueo). "
                    f"HTTP: {last_status}. PW: {pw_error or 'sin detalle'}"
                )

            offer_price, list_price = self._pick_prices(html)
            # Force visible buy-box value when available (user-facing price).
            if dom_offer and dom_offer >= 300:
                offer_price = dom_offer
                if dom_list and dom_list > dom_offer:
                    list_price = dom_list

            if offer_price and not list_price:
                inferred_list = self._infer_list_price_from_yen_context(html, offer_price)
                if inferred_list:
                    list_price = inferred_list
                    logging.info("AmazonJP inferred list price=%s for offer=%s", list_price, offer_price)

            if offer_price is None and list_price is None:
                loose = self._pick_yen_loose(html)
                if loose:
                    logging.info("AmazonJP loose yen fallback=%s", loose)
                    offer_price = loose

            if offer_price is None and list_price is None:
                usd_price = None
                if pw_error and str(pw_error).startswith("USD_BUYBOX:"):
                    usd_price = self._normalize_price(str(pw_error).split(":", 1)[1])
                if usd_price is None:
                    usd_price = self._pick_usd_loose(html)
                if usd_price and usd_jpy_rate and usd_jpy_rate > 0:
                    converted = round(float(usd_price) * float(usd_jpy_rate), 2)
                    logging.info(
                        "AmazonJP USD fallback usd=%s rate=%s -> jpy=%s",
                        usd_price, usd_jpy_rate, converted
                    )
                    offer_price = converted

            if offer_price is None and list_price is None:
                for marker in ("a-price-whole", "a-offscreen", "priceblock", "apex_desktop", "pricetopay"):
                    idx = html.lower().find(marker)
                    if idx >= 0:
                        logging.info("AmazonJP price snippet [%s] at %d: ...%s...",
                                     marker, idx, html[max(0, idx-80):idx+200].replace("\n", " "))
                        break
                else:
                    logging.warning("AmazonJP no price markers in HTML (len=%d)", len(html))
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
        finally:
            if standalone:
                self.batch_end()
