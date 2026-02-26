---
name: nintendo-reseller
description: Automates finding Nintendo Switch game packs in Telegram and reselling them via WhatsApp. Search, formats price, and sends catalog.
---

# Nintendo Reseller Bot

This skill controls a persistent browser to find specific game deals and forward them to clients.

## Capabilities
*   **Search**: Scans Telegram history for specific games (e.g. "Mario").
*   **Format**: Cleans up formatting and calculates local price (USD * 3500).
*   **Persistence**: Saves login sessions so you don't scan QR codes every time.

## Instructions

1.  **Usage**:
    *   Command: `.\venv\Scripts\python bridge.py`
    *   The script is interactive. It will ask:
        1.  Game Name to search.
        2.  Quantity limit.
        3.  Target WhatsApp contact.

2.  **First Run**:
    *   The brower will open without being logged in.
    *   **Action**: Log in to BOTH Telegram and WhatsApp manually.
    *   Next time, it will remember you.

3.  **Troubleshooting**:
    *   If "No packs found", try a more common game name.
    *   If scrolling gets stuck, the internet might be slow.
