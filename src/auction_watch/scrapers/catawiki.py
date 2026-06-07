"""
Catawiki scraper — major online auction platform.

Catawiki uses Akamai Bot Manager which blocks plain HTTP requests.
Strategy: Playwright with real Chrome + webdriver stealth patch.

For each artist:
1. Navigate to /en/s?q=ARTIST_NAME
2. Extract __NEXT_DATA__ → props.pageProps.searchLots.lots
3. Intercept the bidding API response (buyer/api/v3/bidding/lots?ids=...)
   to get bidding_end_time and current_bid_amount per lot
4. Build Lot objects; skip closed lots

Sequential page loads — Akamai is sensitive to parallel requests.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote

from playwright.async_api import async_playwright

from ..artists import Artist
from ..models import Lot
from ._utils import normalize

log = logging.getLogger(__name__)

name = "catawiki"

SEARCH_URL = "https://www.catawiki.com/en/s"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)

STEALTH_SCRIPT = 'Object.defineProperty(navigator, "webdriver", { get: () => undefined });'


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None


async def _search_artist(page, artist: Artist) -> list[Lot]:
    """Load search page, extract lots + bidding data for one artist."""
    bidding_data: dict[int, dict] = {}

    async def _on_response(response):
        if "buyer/api/v3/bidding/lots" in response.url:
            try:
                data = await response.json()
                for bl in data.get("lots", []):
                    bidding_data[bl["id"]] = bl
            except Exception:
                pass

    page.on("response", _on_response)

    try:
        url = f"{SEARCH_URL}?q={quote(artist.name)}"
        await page.goto(url, wait_until="networkidle", timeout=20000)
    except Exception as e:
        log.warning("catawiki: %s navigation failed: %s", artist.name, e)
        page.remove_listener("response", _on_response)
        return []

    # Small delay to let the bidding API response arrive
    await asyncio.sleep(1)
    page.remove_listener("response", _on_response)

    html = await page.content()
    m = NEXT_DATA_RE.search(html)
    if not m:
        return []

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    raw_lots = (
        data.get("props", {})
        .get("pageProps", {})
        .get("searchLots", {})
        .get("lots", [])
    )

    if not raw_lots:
        return []

    artist_norm = normalize(artist.name)
    # Word-boundary match to avoid "Parra" matching "Parras, Antonio"
    artist_re = re.compile(r'(?:^|[\s,\-])' + re.escape(artist_norm) + r'(?:$|[\s,\-])')
    lots: list[Lot] = []

    for raw in raw_lots:
        title = raw.get("title") or ""
        if not artist_re.search(normalize(title)):
            continue

        lot_id = raw.get("id")
        bid_info = bidding_data.get(lot_id, {})

        # Skip closed lots
        if bid_info.get("closed"):
            continue

        # Close date from bidding API (preferred) or fallback to biddingStartTime
        close_dt = _parse_dt(bid_info.get("bidding_end_time"))
        if not close_dt:
            close_dt = _parse_dt(raw.get("biddingStartTime"))
        if not close_dt:
            continue

        # Current bid as estimate_low (no expert estimates available)
        bid_amounts = bid_info.get("current_bid_amount", {})
        estimate_low = None
        if isinstance(bid_amounts, dict) and bid_amounts.get("EUR"):
            estimate_low = int(bid_amounts["EUR"])

        lot_url = raw.get("url") or ""
        if not lot_url:
            continue

        lots.append(Lot(
            source=name,
            artist=artist.name,
            title=title,
            house="Catawiki",
            close_date=close_dt,
            url=lot_url,
            image_url=raw.get("originalImageUrl"),
            estimate_low=estimate_low,
            currency="€" if estimate_low else None,
        ))

    if lots:
        log.info("catawiki: %-30s → %2d lots", artist.name, len(lots))

    return lots


async def collect(client, artists: list[Artist]) -> list[Lot]:
    all_lots: list[Lot] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        await context.add_init_script(STEALTH_SCRIPT)
        page = await context.new_page()

        for artist in artists:
            try:
                lots = await _search_artist(page, artist)
                all_lots.extend(lots)
            except Exception as e:
                log.warning("catawiki: %s failed: %s", artist.name, e)

        await browser.close()

    return all_lots
