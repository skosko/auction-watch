"""
Dorotheum scraper — major Austrian/CEE auction house (dorotheum.com).

Dorotheum blocks plain HTTP requests (403) and uses Cloudflare Bot Management,
which also blocks XHR/fetch from within headless Playwright sessions. The search
AJAX endpoint (/en/?type=1748360621) requires a POST but Cloudflare blocks all
non-navigation HTTP requests from headless browsers.

Currently returns 0 lots — kept as a stub for future improvement (e.g., with
playwright-stealth or a different data source).
"""

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from ..artists import Artist
from ..models import Lot

log = logging.getLogger(__name__)

name = "dorotheum"

SEARCH_URL = "https://www.dorotheum.com/en/search/"
CONCURRENCY = 2

CURRENCY_SYMBOL = {
    "USD": "$", "GBP": "£", "EUR": "€", "HKD": "HK$",
    "CHF": "CHF ", "JPY": "¥", "AUD": "A$", "CAD": "C$",
}


def _normalize(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, (int, float)):
        try:
            return datetime.fromtimestamp(s / 1000 if s > 1e10 else s, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    for fmt in (None, "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            if fmt is None:
                return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            return datetime.strptime(str(s), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _item_to_lot(item: dict, artist_name: str) -> Lot | None:
    url = item.get("url") or item.get("href") or item.get("link") or item.get("detailUrl")
    if not url:
        lot_id = item.get("id") or item.get("lotId") or item.get("lotNumber")
        if lot_id:
            url = f"https://www.dorotheum.com/en/l/lots/{lot_id}.html"
        else:
            return None
    if not url.startswith("http"):
        url = f"https://www.dorotheum.com{url}"

    close_raw = (
        item.get("auctionDate") or item.get("endDate") or item.get("closeDate")
        or item.get("dateTime") or item.get("date") or item.get("saleDate")
    )
    close_dt = _parse_dt(close_raw)
    if not close_dt:
        return None

    title = (
        item.get("title") or item.get("description") or item.get("name")
        or item.get("objectTitle") or "Untitled"
    )
    auction_title = item.get("auctionTitle") or item.get("saleName") or ""
    location = item.get("location") or item.get("saleLocation") or ""
    house = "Dorotheum"
    if location:
        house += f" {location}"
    if auction_title:
        house = f"{house} — {auction_title}"

    currency_raw = item.get("currency") or item.get("currencyCode") or "EUR"
    currency = CURRENCY_SYMBOL.get(str(currency_raw).upper(), str(currency_raw))

    low = item.get("estimateLow") or item.get("lowEstimate") or item.get("priceEstimateFrom")
    high = item.get("estimateHigh") or item.get("highEstimate") or item.get("priceEstimateTo")

    image = item.get("image") or item.get("imageUrl") or item.get("thumbnailUrl")
    if isinstance(image, dict):
        image = image.get("url") or image.get("src")

    return Lot(
        source=name,
        artist=artist_name,
        title=str(title),
        house=house,
        close_date=close_dt.astimezone(timezone.utc),
        url=url,
        image_url=image or None,
        estimate_low=int(low) if low else None,
        estimate_high=int(high) if high else None,
        currency=currency,
    )


def _extract_from_payload(payload, artist_name: str) -> list[Lot]:
    lots = []
    if isinstance(payload, dict):
        for key in ("lots", "results", "items", "data", "hits", "objects"):
            val = payload.get(key)
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        lot = _item_to_lot(item, artist_name)
                        if lot:
                            lots.append(lot)
                        else:
                            lots.extend(_extract_from_payload(item, artist_name))
                break
        else:
            for v in payload.values():
                if isinstance(v, (dict, list)):
                    lots.extend(_extract_from_payload(v, artist_name))
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                lot = _item_to_lot(item, artist_name)
                if lot:
                    lots.append(lot)
                else:
                    lots.extend(_extract_from_payload(item, artist_name))
    return lots


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("dorotheum: playwright not installed, skipping")
        return []

    from ._playwright_utils import new_browser_context, wait_for_content

    all_lots: list[Lot] = []
    sem = asyncio.Semaphore(CONCURRENCY)

    async with async_playwright() as p:
        browser, context = await new_browser_context(p)

        async def _search_one(artist: Artist) -> list[Lot]:
            async with sem:
                page = await context.new_page()
                lots: list[Lot] = []
                captured: list = []

                async def on_response(response):
                    ct = response.headers.get("content-type", "")
                    if response.status == 200 and "json" in ct:
                        if any(s in response.url for s in ("/auth", "/user", "/cart", "/config", "/static")):
                            return
                        try:
                            data = await response.json()
                            captured.append(data)
                        except Exception:
                            pass

                page.on("response", on_response)
                try:
                    url = f"{SEARCH_URL}?q={quote(artist.name)}&type=lots&status=upcoming"
                    await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    await wait_for_content(page, timeout=12000)
                except Exception as e:
                    log.debug("dorotheum: %s page error: %s", artist.name, e)
                finally:
                    page.remove_listener("response", on_response)
                    await page.close()

                for payload in captured:
                    lots.extend(_extract_from_payload(payload, artist.name))

                seen: set[str] = set()
                unique = []
                for lot in lots:
                    key = str(lot.url)
                    if key not in seen:
                        seen.add(key)
                        unique.append(lot)

                if unique:
                    log.info("dorotheum: %-30s → %2d lots", artist.name, len(unique))
                return unique

        results = await asyncio.gather(*(_search_one(a) for a in artists))
        all_lots = [lot for sub in results for lot in sub]
        await browser.close()

    return all_lots
