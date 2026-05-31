"""
Van Ham Kunstauktionen scraper — major German auction house (Cologne).

auction.van-ham.com is a custom PHP/jQuery bidding platform.

Strategy:
1. GET /suche--browse.html?search_title=ARTIST&search_closed=n — lot listing
2. Parse lot cards: id, url, artist, title, estimate (Taxe), image, lot type
   - data-type="2": online-only lot (countdown timer; exact close time on detail page)
   - data-type="7": live auction lot (time_of_event span has the date)
3. Match artist name against followed artists (normalized exact match)
4. Fetch each matched lot's detail page for product:expiration_time + sale name
   - Online lots: use product:expiration_time directly
   - Live lots: expiration_time is a placeholder (year ~2036); fall back to time_of_event
5. House: first segment of page <title> (e.g. "Evening Sale", "The Enduring Appeal of Abstraction")

Concurrency: 5 searches, 10 detail fetches.
"""

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import httpx

from ..artists import Artist
from ..models import Lot

log = logging.getLogger(__name__)

name = "vanham"

BASE = "https://auction.van-ham.com"
SEARCH_URL = f"{BASE}/suche--browse.html"

CONCURRENCY_SEARCH = 5
CONCURRENCY_DETAIL = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,*/*",
}


def _normalize(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _parse_german_int(s: str) -> int | None:
    try:
        return int(s.replace(".", "").replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _parse_german_date(s: str) -> datetime | None:
    """Parse '10.06.2026 - ca.18:23' → datetime (Europe/Berlin ≈ UTC+2)."""
    m = re.match(r"(\d{1,2})\.(\d{2})\.(\d{4})(?:\s*-\s*ca\.(\d{2}):(\d{2}))?", s.strip())
    if not m:
        return None
    day, mon, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour = int(m.group(4)) if m.group(4) else 18
    minute = int(m.group(5)) if m.group(5) else 0
    # Van Ham is in Cologne; CEST is UTC+2 in auction season (May-Oct)
    offset = timezone(timedelta(hours=2))
    try:
        return datetime(year, mon, day, hour, minute, tzinfo=offset).astimezone(timezone.utc)
    except ValueError:
        return None


async def _fetch(client: httpx.AsyncClient, url: str, **kwargs) -> str | None:
    try:
        r = await client.get(url, headers=HEADERS, timeout=20.0, **kwargs)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning("vanham: failed to fetch %s: %s", url, e)
        return None


def _parse_search_page(html: str, artist_norm: str, artist_display: str) -> list[dict]:
    """Extract lot raw data from a search results page."""
    # Card boundaries: each starts with data-id="NNN" data-type="N"
    card_starts = [
        (m.start(), m.group(1), m.group(2))
        for m in re.finditer(r'data-id="(\d+)"\s+data-type="(\d+)"', html)
    ]

    seen_ids: set[str] = set()
    raws = []

    for i, (start, lot_id, lot_type) in enumerate(card_starts):
        if lot_id in seen_ids:
            continue

        end = card_starts[i + 1][0] if i + 1 < len(card_starts) else start + 4000
        card = html[start:end]

        # Artist name from <strong> tag
        artist_m = re.search(r"<strong>\s*([\s\S]+?)\s*</strong>", card)
        if not artist_m:
            continue
        card_artist = re.sub(r"<[^>]+>", "", artist_m.group(1)).strip()
        if _normalize(card_artist) != artist_norm:
            continue

        seen_ids.add(lot_id)

        # Lot URL
        url_m = re.search(
            r'href="(https://auction\.van-ham\.com/[^"]*--id-\d+-item\.html)"', card
        )
        if not url_m:
            continue
        lot_url = url_m.group(1)

        # Title (text after </strong><br> before </a>)
        title_m = re.search(r"</strong><br>\s*([\s\S]+?)\s*</a>", card)
        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else "Untitled"

        # Estimate (Taxe: LOW - HIGH €)
        est_m = re.search(
            r"Taxe:</b>([\s\S]*?)([\d.]+)[\s\S]*?-[\s\S]*?([\d.]+)[\s\S]*?€",
            card,
        )
        estimate_low = estimate_high = None
        if est_m:
            estimate_low = _parse_german_int(est_m.group(2))
            estimate_high = _parse_german_int(est_m.group(3))

        # Fallback date for live auction lots (type 7) from time_of_event span
        fallback_dt = None
        if lot_type == "7":
            te_m = re.search(r'class="time_of_event">([^<]+)<', card)
            if te_m:
                fallback_dt = _parse_german_date(te_m.group(1))

        raws.append(
            {
                "id": lot_id,
                "url": lot_url,
                "artist": artist_display,
                "title": title,
                "estimate_low": estimate_low,
                "estimate_high": estimate_high,
                "image_url": f"{BASE}/images/item_images/gallery/{lot_id}_0.jpg",
                "lot_type": lot_type,
                "fallback_dt": fallback_dt,
            }
        )

    return raws


async def _enrich_lot(
    client: httpx.AsyncClient, raw: dict
) -> Lot | None:
    """Fetch detail page; extract expiration_time + sale name."""
    html = await _fetch(client, raw["url"])
    if not html:
        return None

    # Close date
    exp_m = re.search(
        r'<meta\s+property="product:expiration_time"\s+content="([^"]+)"', html
    )
    close_dt = None
    if exp_m:
        try:
            dt = datetime.fromisoformat(exp_m.group(1)).astimezone(timezone.utc)
            now_utc = datetime.now(tz=timezone.utc)
            # Accept if within ~1 year; live lots get a far-future placeholder (~2036)
            if dt - now_utc < timedelta(days=400):
                close_dt = dt
        except ValueError:
            pass

    if close_dt is None:
        close_dt = raw.get("fallback_dt")

    if close_dt is None:
        return None

    # Prefer og:image over the constructed gallery URL (more reliable)
    og_m = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
    if not og_m:
        og_m = re.search(r'<meta\s+content="([^"]+)"\s+property="og:image"', html)
    image_url = og_m.group(1) if og_m else raw["image_url"]

    # Sale name from page <title>: "Sale Name | ... | Artist-Title | Van Ham ..."
    title_m = re.search(r"<title[^>]*>([^<]+)</title>", html)
    house = "Van Ham"
    if title_m:
        parts = [p.strip() for p in title_m.group(1).split("|")]
        sale = parts[0] if parts else ""
        if sale and sale.lower() not in ("van ham kunstauktionen", "kunstauktionen", ""):
            house = f"Van Ham — {sale}"

    return Lot(
        source=name,
        artist=raw["artist"],
        title=raw["title"],
        house=house,
        close_date=close_dt,
        url=raw["url"],
        image_url=image_url,
        estimate_low=raw["estimate_low"],
        estimate_high=raw["estimate_high"],
        currency="€",
    )


async def _search_artist(
    client: httpx.AsyncClient, artist: Artist
) -> list[dict]:
    html = await _fetch(
        client, SEARCH_URL, params={"search_title": artist.name, "search_closed": "n"}
    )
    if not html:
        return []
    return _parse_search_page(html, _normalize(artist.name), artist.name)


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    sem_search = asyncio.Semaphore(CONCURRENCY_SEARCH)
    sem_detail = asyncio.Semaphore(CONCURRENCY_DETAIL)

    async def _one_search(a: Artist) -> list[dict]:
        async with sem_search:
            return await _search_artist(client, a)

    all_raws_nested = await asyncio.gather(*(_one_search(a) for a in artists))

    # Flatten and deduplicate by lot URL
    seen_urls: set[str] = set()
    unique_raws = []
    for raws in all_raws_nested:
        for raw in raws:
            if raw["url"] not in seen_urls:
                seen_urls.add(raw["url"])
                unique_raws.append(raw)

    if not unique_raws:
        return []

    async def _one_detail(raw: dict) -> Lot | None:
        async with sem_detail:
            return await _enrich_lot(client, raw)

    lot_results = await asyncio.gather(*(_one_detail(r) for r in unique_raws))
    lots = [lot for lot in lot_results if lot is not None]

    if lots:
        for artist_name in sorted(set(lot.artist for lot in lots)):
            count = sum(1 for lot in lots if lot.artist == artist_name)
            log.info("vanham: %-30s → %2d lots", artist_name, count)

    return lots
