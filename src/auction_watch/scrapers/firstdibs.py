"""
1stDibs scraper — luxury dealer marketplace (1stdibs.com).

1stDibs is a buy-now marketplace (fixed price / make-an-offer), not a
traditional auction house. Lots are returned with close_date=None and
displayed as "Available now" in the digest.

The site blocks plain httpx (Cloudflare), so we use Playwright to load
one search page per artist and capture the GraphQL itemSearch response.

Results are sorted by "newest" on 1stDibs so recently-listed works
appear first. Up to MAX_PER_ARTIST results are returned per artist.

Concurrency: 3 simultaneous browser pages.
"""

import asyncio
import logging
import re
import unicodedata

import httpx

from ..artists import Artist
from ..models import Lot

log = logging.getLogger(__name__)

name = "1stdibs"

BASE = "https://www.1stdibs.com"
SEARCH_BASE = f"{BASE}/search/art/"
CONCURRENCY = 3
MAX_PER_ARTIST = 10


def _normalize(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _preferred_price(converted: list[dict]) -> tuple[int | None, str]:
    """Return (amount_int, currency_symbol) preferring EUR then GBP then USD."""
    by_currency = {d["currency"]: d["amount"] for d in converted if d.get("currency")}
    for code, symbol in (("EUR", "€"), ("GBP", "£"), ("USD", "$")):
        if code in by_currency:
            return int(round(by_currency[code])), symbol
    return None, "$"


def _item_to_lot(item: dict, artist_display: str) -> Lot | None:
    if item.get("isSold") or item.get("isUnavailable") or item.get("isOnHold"):
        return None

    url_path = item.get("localizedPdpUrl") or ""
    if not url_path:
        return None
    url = f"{BASE}{url_path}" if not url_path.startswith("http") else url_path

    title = item.get("title") or "Untitled"

    # Price from displayPriceTracking or displayPrice
    price_amt = None
    currency = "$"
    for price_field in ("displayPriceTracking", "displayPrice"):
        entries = item.get(price_field) or []
        if entries and isinstance(entries, list):
            converted = (entries[0] or {}).get("convertedAmountList") or []
            if converted:
                price_amt, currency = _preferred_price(converted)
                break

    # Image
    photos = item.get("carouselPhotos") or item.get("productPhotos") or []
    image_url = None
    if photos:
        image_url = photos[0].get("smallPath") or photos[0].get("masterOrZoomPath")

    # Seller / gallery name
    seller_co = (
        ((item.get("seller") or {}).get("sellerProfile") or {}).get("company") or ""
    )
    house = f"1stDibs — {seller_co}" if seller_co else "1stDibs"

    return Lot(
        source=name,
        artist=artist_display,
        title=title,
        house=house,
        close_date=None,
        url=url,
        image_url=image_url or None,
        estimate_low=price_amt,
        estimate_high=None,
        currency=currency,
    )


def _extract_items(gql_data: dict) -> list[dict]:
    """Walk the GraphQL itemSearch response and return raw item dicts."""
    edges = (
        (gql_data.get("data") or {})
        .get("viewer", {})
        .get("itemSearch", {})
        .get("edges") or []
    )
    items = []
    for edge in edges:
        # Walk nested structure to find item with serviceId
        def find(node):
            if isinstance(node, dict):
                if node.get("serviceId") and node.get("title"):
                    return node
                for v in node.values():
                    r = find(v)
                    if r:
                        return r
            return None
        item = find(edge)
        if item:
            items.append(item)
    return items


async def _search_artist_pw(page, artist: Artist) -> list[Lot]:
    """Load one search page and extract matching lots."""
    artist_norm = _normalize(artist.name)
    captured = []

    async def on_response(response):
        if "soa/graphql" in response.url and response.status == 200:
            try:
                data = await response.json()
                if (data.get("data") or {}).get("viewer", {}).get("itemSearch"):
                    captured.append(data)
            except Exception:
                pass

    page.on("response", on_response)
    try:
        url = f"{SEARCH_BASE}?q={artist.name.replace(' ', '+')}&sort=newest"
        await page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception as e:
        log.debug("1stdibs: %s page error: %s", artist.name, e)
    finally:
        page.remove_listener("response", on_response)

    lots = []
    for gql in captured:
        for item in _extract_items(gql):
            # Verify artist via creators list; fall back to title only if no creators
            creators = item.get("creators") or []
            creator_names = [
                _normalize((c.get("creator") or {}).get("displayName") or "")
                for c in creators
            ]
            if creator_names:
                if artist_norm not in creator_names:
                    continue
            else:
                if artist_norm not in _normalize(item.get("title") or ""):
                    continue

            lot = _item_to_lot(item, artist.name)
            if lot:
                lots.append(lot)
                if len(lots) >= MAX_PER_ARTIST:
                    break
        if len(lots) >= MAX_PER_ARTIST:
            break

    return lots


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("1stdibs: playwright not installed, skipping")
        return []

    from ._playwright_utils import new_browser_context

    sem = asyncio.Semaphore(CONCURRENCY)
    all_lots: list[Lot] = []

    async with async_playwright() as p:
        browser, context = await new_browser_context(p)

        async def _one(artist: Artist) -> list[Lot]:
            async with sem:
                page = await context.new_page()
                try:
                    return await _search_artist_pw(page, artist)
                finally:
                    await page.close()

        results = await asyncio.gather(*(_one(a) for a in artists))
        all_lots = [lot for sub in results for lot in sub]
        await browser.close()

    if all_lots:
        for artist_name in sorted(set(lot.artist for lot in all_lots)):
            count = sum(1 for lot in all_lots if lot.artist == artist_name)
            log.info("1stdibs: %-30s → %2d listings", artist_name, count)

    return all_lots
