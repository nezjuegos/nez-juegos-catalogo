# Chat-Bridge Project Analysis

## Project Overview

A local application that helps search for Nintendo game packs in a Telegram Web session. It scrapes messages from a specific Telegram chat, parses them to extract pack information (ID, games list, prices), and provides a web UI for searching.

---

## Tech Stack

- **Backend**: Python 3, Flask, Playwright (async)
- **Frontend**: Vanilla HTML/CSS/JavaScript
- **Browser Automation**: Playwright with persistent Chrome session

---

## File Structure

```
chat-bridge/
├── bridge.py          # Standalone CLI script (interactive mode)
├── scraper.py         # Core scraper class (NintendoScraper)
├── server.py          # Flask API server
├── browser_data_clean/ # Persistent browser session data
└── ui/
    ├── index.html     # Web UI
    ├── app.js         # Frontend logic
    └── style.css      # Styles
```

---

## How It Works

1. **Server starts** (`server.py`) → launches Flask on port 5000
2. **Scraper initializes** → creates persistent Chromium browser context
3. **User searches** via web UI → calls `/api/search?q=<game>&limit=<n>`
4. **Scraper**:
   - Opens Telegram Web A (`https://web.telegram.org/a/`)
   - Navigates to chat named "evAn Accounts"
   - Uses `Ctrl+F` to open in-chat search
   - Types the query and waits for results
   - Clicks each search result and scrapes message text
   - Parses messages into `GenericPack` objects
5. **Returns** JSON with pack data (ID, games, prices)

---

## Key Classes

### `GenericPack` (in both `bridge.py` and `scraper.py`)

Parses raw message text to extract:
- **ID**: Matches `ID : <number>` pattern
- **Games**: Lines between ID and price
- **Price**: Matches `<number>$` pattern, multiplied by 3500

```python
class GenericPack:
    def _parse(self):
        # Extract ID with regex: r"ID\s*:\s*(\d+)"
        # Extract Price with regex: r"(\d+)\s*\$"
        # Games = lines between ID and Price
        self.is_valid = id_found and price_found and len(self.games) > 0
```

### `NintendoScraper` (in `scraper.py`)

Main automation class:
- `start()` - Launches persistent browser
- `ensure_telegram_login()` - Checks/waits for login
- `search_game(query, limit)` - Performs the search
- `close()` - Cleans up

---

## Current Issue: Search Returns No Results

When searching for a video game, the application returns empty results.

---

## Problems Identified

### 1. **Wrong Search Method - Global Search vs In-Chat Search** (MAIN ISSUE)

Location: `scraper.py`, lines 134-149

```python
# Current code uses Ctrl+F which opens GLOBAL search, not in-chat search
await self.page.keyboard.press("Control+F")
await asyncio.sleep(1.5)

search_input = self.page.locator("input[placeholder*='Search'], input:focus").first
```

**Problem**: `Ctrl+F` in Telegram Web A opens the **global search** (searches across ALL chats) instead of the **in-chat search** (searches within the currently open chat only).

**What's happening**:
1. Opens "evAn Accounts" chat ✓
2. Presses Ctrl+F → Opens **GLOBAL** search in left sidebar ✗
3. Types query → Searches across ALL Telegram chats ✗
4. Results show messages from random chats, not "evAn Accounts"

**The Fix**: Instead of using `Ctrl+F`, click the **magnifying glass icon** in the chat header to open the in-chat search.

```python
# WRONG: Opens global search
await self.page.keyboard.press("Control+F")

# CORRECT: Click the search icon in chat header
# The search button is in the right side of the chat header
search_icon = self.page.locator("button.Button.translucent.round[title='Search']").first
# Alternative selectors to try:
# search_icon = self.page.locator(".ChatInfo button[aria-label='Search']").first
# search_icon = self.page.locator(".RightHeader button").filter(has=self.page.locator("i.icon-search")).first
await search_icon.click()
```

---

### 2. **Incorrect CSS Selectors for Results**

Location: `scraper.py`, line 162

```python
results = self.page.locator(".search-results .ListItem, .search-results div[role='button']")
```

**Problem**: Telegram Web A frequently updates its DOM structure. The classes `.search-results` and `.ListItem` likely don't exist in the current version. When these selectors match nothing, `count == 0` and the loop exits immediately.

**The search flow**:
1. Opens chat ✓
2. Opens search (needs fix) 
3. Types query ✓
4. **Tries to find results with wrong selectors** ✗ ← ALSO FAILS HERE
5. `count == 0`, loop breaks, returns empty array

---

### 2. **Duplicate Return Statement**

Location: `scraper.py`, lines 193-194

```python
        return [p.to_dict() for p in found_packs]

        return [p.to_dict() for p in found_packs]
```

**Problem**: Copy-paste error. Harmless but indicates code quality issues.

---

### 3. **Price Regex Too Strict**

Location: `scraper.py`, line 40

```python
price_match = re.search(r"(\d+)\s*\$", clean_line)
```

**Problem**: Only matches `10$` or `10 $` format. Won't match:
- `$10` (dollar sign first)
- `10.50$` (decimals)
- `USD 10` (text prefix)

If price isn't found, `is_valid = False` and the pack is discarded.

---

### 4. **Insufficient Wait Times**

Location: `scraper.py`, line 150

```python
await asyncio.sleep(2)  # Wait for results
```

**Problem**: 2 seconds may not be enough for large chats or slow connections.

---

### 5. **Text Content Selector May Be Wrong**

Location: `scraper.py`, line 174

```python
all_chunks = await self.page.locator("div.text-content").all_inner_texts()
```

**Problem**: The `.text-content` class may also have changed in Telegram's DOM.

---

## Recommended Fixes

### Fix 1: Use In-Chat Search Instead of Global Search (Priority: CRITICAL)

Replace `Ctrl+F` with clicking the in-chat search button. In `scraper.py`, replace lines 134-149:

**BEFORE (broken):**
```python
# Open chat search using Ctrl+F (most reliable method)
await self.page.keyboard.press("Control+F")
await asyncio.sleep(1.5)

search_input = self.page.locator("input[placeholder*='Search'], input:focus").first
```

**AFTER (fixed):**
```python
# Click the search icon in the chat header (opens in-chat search)
# Wait for chat to fully load first
await asyncio.sleep(1)

# The search button is typically in the top-right area of the chat
# Try multiple selectors in case DOM structure varies
search_button = self.page.locator("button[title='Search']").first
if await search_button.count() == 0:
    search_button = self.page.locator(".RightHeader button.Button.round").first
if await search_button.count() == 0:
    # Fallback: look for any button with search icon
    search_button = self.page.locator("button:has(i.icon-search)").first

await search_button.click()
await asyncio.sleep(1.5)

# Now find the in-chat search input (different from global search)
search_input = self.page.locator(".RightSearch input, .ChatSearch input, input[placeholder*='Search']").first
```

**Key insight**: Telegram Web A has TWO search inputs:
1. **Global search** (left sidebar) - searches all chats
2. **In-chat search** (right panel) - searches only the current chat

The `Ctrl+F` shortcut activates the global search. You must click the chat header's search icon to get the in-chat search.

---

### Fix 2: Update Result Selectors (Priority: HIGH)

Need to inspect actual Telegram Web A DOM to find correct selectors. Common patterns in Telegram Web A:

```python
# Possible alternatives to try:
results = self.page.locator("[class*='search'] [class*='ListItem']")
results = self.page.locator(".search-content .ListItem")
results = self.page.locator("[data-message-id]")  # Direct message selector
```

### Fix 2: Add Debugging

Add logging to see what's actually present:

```python
# Before the selector
html = await self.page.content()
print(html[:5000])  # See actual DOM

# Or check what elements exist
elements = await self.page.locator("[class*='search']").all()
for el in elements:
    print(await el.get_attribute("class"))
```

### Fix 3: Improve Price Regex

```python
# More flexible price matching
price_match = re.search(r"(\d+(?:\.\d+)?)\s*\$|\$\s*(\d+(?:\.\d+)?)", clean_line)
```

### Fix 4: Remove Duplicate Return

Delete line 194 in `scraper.py`.

### Fix 5: Increase Wait Times

```python
await asyncio.sleep(4)  # Increase from 2 to 4 seconds
```

---

## Configuration

Located at top of `scraper.py` and `bridge.py`:

```python
SOURCE_CHAT = "evAn Accounts"  # Telegram chat to search
PRICE_MULTIPLIER = 3500        # USD to local currency
USER_DATA_DIR = "browser_data_clean"  # Browser profile path
```

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serves web UI |
| `/api/search?q=<query>&limit=<n>` | GET | Search for game packs |
| `/api/status` | GET | Check Telegram connection status |

---

## To Run

```bash
# Install dependencies
pip install flask playwright

# Install browser
playwright install chromium

# Start server
python server.py
```

Then open `http://localhost:5000` in browser.

---

## Next Steps

1. **Fix the search method** (CRITICAL): Replace `Ctrl+F` with clicking the in-chat search button in the chat header
2. **Inspect Telegram DOM**: With browser visible, right-click → Inspect Element on the search button and results to get exact selectors
3. **Update result selectors**: Replace `.search-results .ListItem` with correct classes from DOM inspection
4. **Test with known content**: Search for a game you know exists in the "evAn Accounts" chat
5. **Add error logging**: Print actual element counts and HTML snippets for debugging

---

## Visual Reference

The in-chat search button is the **magnifying glass icon** in the top-right header of the chat (next to the chat name), NOT the search bar in the left sidebar.

```
┌─────────────────────────────────────────────────────┐
│ [Chat List]     │  evAn Accounts    [🔍] [⋮]       │  ← Click THIS 🔍
│                 │                                   │
│ Global Search   │  [Messages appear here]          │
│ (DON'T USE)     │                                   │
│                 │                                   │
└─────────────────────────────────────────────────────┘
```
