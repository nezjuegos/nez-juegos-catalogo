import asyncio
import re
import os
import argparse
import sys
from playwright.async_api import async_playwright

# --- CONFIGURATION ---
SOURCE_CHAT = "evAn Accounts"
PRICE_MULTIPLIER = 3500
USER_DATA_DIR = os.path.join(os.getcwd(), "browser_data_clean") # Persistent session folder (New)

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
            
            # 1. Extract ID
            id_match = re.search(r"ID\s*:\s*(\d+)", clean_line, re.IGNORECASE)
            if id_match:
                self.id = id_match.group(1)
                id_found = True
                continue
            
            # 2. Extract Price (Look for number followed by $)
            # Example: "10$" or "10 $"
            price_match = re.search(r"^(\d+)\s*\$", clean_line)
            if price_match:
                self.original_price = int(price_match.group(1))
                self.final_price = self.original_price * PRICE_MULTIPLIER
                price_found = True
                continue

            # 3. Collect Games (Everything between ID and Price roughly)
            # We filter out known header/footer text
            if id_found and not price_found:
                if "NINTENDO SWITCH ACCOUNT" in clean_line: continue
                if "For buy:" in clean_line: continue
                game_lines.append(clean_line)

        self.games = game_lines
        self.is_valid = id_found and price_found and len(self.games) > 0

    def format_output(self):
        # Apply strict formatting rules
        games_list = "\n".join(self.games)
        
        # Format currency with thousands separator (e.g., 35.000)
        formatted_price = f"{self.final_price:,.0f}".replace(",", ".")

        return (
            f"ID : {self.id}\n\n"
            f"---Lista de contenidos---\n"
            f"{games_list}\n\n"
            f"Precio: ${formatted_price}"
        )

    def contains_game(self, query):
        # Case insensitive search in game lines
        return any(query.lower() in g.lower() for g in self.games)


async def run():
    print(f"\n[INFO] Using persistent browser profile at: {USER_DATA_DIR}")

    parser = argparse.ArgumentParser()
    parser.add_argument("--game", help="Game to search for")
    parser.add_argument("--limit", type=int, default=5, help="Number of packs")
    parser.add_argument("--target", help="Target WhatsApp contact")
    
    # Check if run with arguments or interactive
    if len(sys.argv) > 1:
        args = parser.parse_args()
        game_query = args.game
        limit = args.limit
        target_contact = args.target # We will confirm this later
    else:
        # Fallback to interactive if no args provided
        game_query = input("\n[INPUT] What game are you looking for? (e.g. 'Mario Kart'): ").strip()
        limit_str = input(f"[INPUT] How many packs do you want? (Default 5): ").strip()
        limit = int(limit_str) if limit_str.isdigit() else 5
        target_contact = input("[INPUT] Send to WhatsApp contact (Exact Name): ").strip()

    print(f"\n[CONFIG] Searching: {game_query} | Limit: {limit} | Target: {target_contact}\n")

    async with async_playwright() as p:
        # Launch persistent context (Saves cookies/local storage)
        # We need specific args to avoid conflicts if Chrome is open, but usually safer to close Chrome first.
        browser_context = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled", # Try to hide bot nature
                "--disable-notifications" # BLOCK ALERTS
            ] 
        )
        
        page = await browser_context.new_page()

        print("\n" + "="*50)
        print("NINTENDO RESELLER BOT STARTED")
        print("="*50)

        # --- 1. TELEGRAM PHASE ---
        print("\n[1/2] Accessing Telegram...")
        await page.goto("https://web.telegram.org/a/")

        # Login Check
        try:
            # Wait for chat list to appear to confirm login
            await page.wait_for_selector(".chat-list", timeout=10000) 
            print("    [OK] Session active! No login needed.")
        except:
            print("\n[LOGIN REQUIRED] Waiting for you to log in...")
            await page.wait_for_selector(".chat-list", timeout=300000) # 5 min to login
            print("    [OK] Login detected!")

        # Find Chat
        print(f"    Searching for source chat: '{SOURCE_CHAT}'...")
        try:
            chat_element = page.get_by_text(SOURCE_CHAT, exact=False).first
            await chat_element.click(force=True)
            print("    [OK] Chat opened.")
        except:
            print(f"[ERROR] Could not find chat '{SOURCE_CHAT}'. Is it in your list?")
            await browser_context.close()
            return
            
        
        # --- NEW: NATIVE SEARCH LOGIC ---
        print(f"\n    [SEARCH] Using Telegram Search for: '{game_query}'...")
        
        found_packs = [] # Initialize here to prevent crash on error
        
        try:
            # 1. Open Search (Magnifying Glass)
            # Web A uses 'button[title="Search in this chat"]' usually.
            # Web K uses different classes.
            # We try a very generic approach: Find any button with an SVG inside that looks like search, or title.
            print("       Looking for search button...")
            
            # Helper to find search button. 
            # We look for the top bar specifically to avoid clicking random stuff.
            header = page.locator(".sidebar-header, .chat-info") 
            
            # Try specific selectors first
            search_btn = page.locator("button[title='Search in this chat']").first
            
            if await search_btn.count() == 0:
                 # Fallback: Look for any button with 'search' in aria-label
                 search_btn = page.locator("button[aria-label*='Search']").first
            
            if await search_btn.count() == 0:
                 # Fallback: Look for the input directly (maybe it's already open?)
                 search_input = page.locator("input[placeholder='Search']").first
                 if await search_input.count() > 0:
                     print("       Search input already visible!")
                     await search_input.click()
                 else:
                     raise Exception("Could not find Search button or Input")
            else:
                 await search_btn.click()
            
            await asyncio.sleep(1) # Animation
            
            # 2. Type Query
            search_input = page.locator("input[placeholder='Search']").first
            await search_input.fill(game_query)
            await asyncio.sleep(3) # Wait for results to load
            
            print("       Waiting for results...")
            seen_ids = set()
            
            # We try to scroll the search results container to load more if needed
            # But let's start by grabbing the visible ones.
            # Common outcome: Results are list items.
            
            # Selector for search result items (generic guess for Web A)
            failed_attempts = 0
            while len(found_packs) < limit and failed_attempts < 5:
                # Re-locate elements each time
                # Try to find the container of search results
                result_elements = await page.locator(".search-results .message, .c-ripple").all()
                
                # If naive selector fails, try getting all text from the sidebar
                if not result_elements:
                     # Fallback: Just grab text content of likely areas
                     content_text = await page.locator(".search-group, .scrollable").all_inner_texts()
                     # This is messy. Let's try iterating locator matches.
                     items = page.locator(".search-group .ListItem, .SearchResult").first
                     if await items.count() == 0:
                         print("       [INFO] No results loaded yet...")
                         await asyncio.sleep(2)
                         failed_attempts += 1
                         continue

                # Iterate through visible results
                # Note: In some versions, you click the result to see the message.
                # But often the snippet is too short. 
                # STRATEGY: Click each result -> Read the 'Focused' message in main view -> Go back?
                # No, that's too slow.
                # Let's hope the search result contains enough text or we can extract the full message from the DOM.
                
                # BETTER STRATEGY FOR WEB A:
                # The search results list usually contains the full text or a large part of it.
                # Let's scrape the innerText of the search results column.
                
                column_text = await page.locator(".search-results, .Search-content").all_inner_texts()
                
                # Simplify: Just get all text chunks on screen that look like packs
                all_chunks = await page.locator("div.text-content").all_inner_texts()
                
                for text in all_chunks:
                    if text in seen_ids: continue
                    seen_ids.add(text)
                    
                    if game_query.lower() in text.lower():
                        pack = GenericPack(text)
                        if pack.is_valid:
                            print(f"       [MATCH] Found Match! ID: {pack.id}")
                            found_packs.append(pack)
                            if len(found_packs) >= limit: break
                
                if len(found_packs) >= limit: break
                
                # Try to scroll the search results
                # This is tricky without knowing the exact container class.
                # We'll just wait a bit and see if user scrolls or if we grabbed enough.
                await asyncio.sleep(1)
                failed_attempts += 1 

        except Exception as e:
            print(f"    [WARN] Native search failed/timed out: {e}")
            print("    [WARN] Falling back to manual scroll...")
            # Fallback code could go here, but let's stick to the search attempt.

        print(f"\n[OK] Search finished. Found {len(found_packs)} packs.")

        if not found_packs:
            print("[INFO] No packs found in search results. Exiting.")
            await browser_context.close()
            return
        
        # --- 2. WHATSAPP PHASE ---
        print("\n[2/2] Switching to WhatsApp...")
        await page.goto("https://web.whatsapp.com/")
        
        try:
            await page.wait_for_selector("div[contenteditable='true'][data-tab='3']", timeout=10000)
            print("    [OK] Session active! No login needed.")
        except:
            print("\n[LOGIN REQUIRED] Waiting for WhatsApp login...")
            await page.wait_for_selector("div[contenteditable='true'][data-tab='3']", timeout=300000)

        # CONFIRMATION BEFORE SENDING
        print(f"\n[CONFIRM] Found {len(found_packs)} packs. Target: '{target_contact}'")
        confirm = input("[INPUT] Press ENTER to send, or type a new name to change target: ").strip()
        if confirm:
            target_contact = confirm
            print(f"    [UPDATE] Target changed to: '{target_contact}'")

        # Search Contact
        print(f"    Searching for target: '{target_contact}'...")
        search_box = page.locator("div[contenteditable='true'][data-tab='3']")
        await search_box.click()
        await search_box.fill(target_contact)
        await asyncio.sleep(1.5)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)

        # Send Messages
        print(f"\n[SENDING] Sending {len(found_packs)} messages...")
        message_box = page.locator("div[contenteditable='true'][data-tab='10']")

        for i, pack in enumerate(found_packs):
            formatted_msg = pack.format_output()
            
            # Type and send
            await message_box.click()
            # We use clipboard paste or fill. Fill is safer for large text.
            await message_box.fill(formatted_msg)
            await asyncio.sleep(0.5)
            await page.keyboard.press("Enter")
            print(f"    -> Sent pack ID: {pack.id}")
            await asyncio.sleep(1) # Anti-spam delay

        print("\n" + "="*50)
        print("BATCH COMPLETE. Browser closing.")
        await asyncio.sleep(3)
        await browser_context.close()

if __name__ == "__main__":
    asyncio.run(run())
