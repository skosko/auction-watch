"""
Shared Playwright helpers used by scrapers that require a real browser.
"""

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

VIEWPORT = {"width": 1280, "height": 800}

EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


async def new_browser_context(playwright: Any):
    """Launch Chromium and return a browser context with realistic headers."""
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport=VIEWPORT,
        extra_http_headers=EXTRA_HEADERS,
        locale="en-US",
        timezone_id="America/New_York",
    )
    return browser, context


async def wait_for_content(page: Any, timeout: float = 12000) -> None:
    """Wait for network to quiet down and DOM content to load."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        # networkidle can timeout on heavy pages; domcontentloaded is enough
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
