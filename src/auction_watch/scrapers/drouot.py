"""
Drouot scraper — French auction umbrella (Hôtel Drouot).

Uses Drouot's internal neoGingo JSON API that powers drouot.com.
One lot-search call per artist, then cached sale-detail lookups to
resolve auctioneer name + sale title into the house field.

Concurrency: 10 searches in parallel, 5 parallel sale-detail fetches.
"""

import asyncio
import logging
import re
import unicodedata
from datetime import datetime, timezone

import httpx

from ..artists import Artist
from ..models import Lot

log = logging.getLogger(__name__)

name = "drouot"

API_BASE = "https://api.drouot.com/drouot/gingolem"
LOT_BASE = "https://drouot.com/en/l/"
IMG_BASE = "https://cdn.drouot.com/d/image/lot?size=fullHD&path="

CONCURRENCY = 10
CONCURRENCY_SALE = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://drouot.com",
    "Referer": "https://drouot.com/",
}

CURRENCY_SYMBOL = {
    "USD": "$", "GBP": "£", "EUR": "€", "HKD": "HK$",
    "CHF": "CHF ", "JPY": "¥", "AUD": "A$", "CAD": "C$",
}

# Lots whose sale is in one of these states are still open
ACTIVE_STATUSES = {"CREATED", "IN_PROGRESS", "OPEN"}


def _normalize(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _desc_matches_artist(artist_norm: str, desc_norm: str) -> bool:
    """True iff desc_norm plausibly refers to the queried artist.

    Drouot descriptions start with "LASTNAME Firstname". For single-word
    artist names (e.g. "parra"), require the word to appear at a word
    boundary at the start of the description, not just anywhere.
    """
    if artist_norm not in desc_norm:
        return False
    if " " not in artist_norm:
        first = re.split(r"\W+", desc_norm)[0]
        return first == artist_norm
    return True


async def _get_sale_house(client: httpx.AsyncClient, sale_id: int) -> str:
    """Return 'AuctioneerName — SaleTitle' for a sale, or 'Drouot'."""
    try:
        r = await client.get(
            f"{API_BASE}/neoGingo/sale/{sale_id}",
            headers=HEADERS,
            timeout=10.0,
        )
        if r.status_code == 200:
            sale = r.json().get("sale", {})
            auctioneer = (
                (sale.get("auctioneerCard") or {})
                .get("link", {})
                .get("auctioneerName", "")
                .strip()
            )
            title = sale.get("title", "").strip()
            house = auctioneer or "Drouot"
            if title:
                house = f"{house} — {title}"
            return house
    except Exception:
        pass
    return "Drouot"


async def _search_artist(
    client: httpx.AsyncClient,
    artist: Artist,
) -> list[dict]:
    """Return raw lot dicts (with sale_id) for later enrichment."""
    try:
        r = await client.get(
            f"{API_BASE}/neoGingo/lot/search",
            params={"q": artist.name},
            headers=HEADERS,
            timeout=15.0,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("drouot: %s failed: %s", artist.name, e)
        return []

    hits = r.json().get("lots") or []
    artist_norm = _normalize(artist.name)
    raws = []

    for hit in hits:
        status = hit.get("saleStatus", "")
        if status and status not in ACTIVE_STATUSES:
            continue

        # Artist name verification: description usually starts with "LASTNAME FIRSTNAME"
        desc_norm = _normalize(hit.get("description") or "")
        if not _desc_matches_artist(artist_norm, desc_norm):
            continue

        ts = hit.get("date") or hit.get("bidEndDate")
        if not ts or ts <= 0:
            continue

        lot_id = hit.get("id")
        if not lot_id:
            continue

        slug = hit.get("slug") or ""
        url = f"{LOT_BASE}{lot_id}-{slug}" if slug else f"{LOT_BASE}{lot_id}"

        photo = (hit.get("photo") or {}).get("path")
        image_url = f"{IMG_BASE}{photo}" if photo else None

        raws.append({
            "ts": ts,
            "url": url,
            "description": (hit.get("description") or "Untitled")[:200],
            "sale_id": hit.get("saleId"),
            "currency_id": (hit.get("currencyId") or "EUR").upper(),
            "low": hit.get("lowEstim"),
            "high": hit.get("highEstim"),
            "image_url": image_url,
            "artist_name": artist.name,
        })

    return raws


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    sem_search = asyncio.Semaphore(CONCURRENCY)
    sem_sale = asyncio.Semaphore(CONCURRENCY_SALE)

    async def _one_artist(a: Artist) -> list[dict]:
        async with sem_search:
            return await _search_artist(client, a)

    all_raws_nested = await asyncio.gather(*(_one_artist(a) for a in artists))
    all_raws = [r for sub in all_raws_nested for r in sub]

    if not all_raws:
        return []

    # Resolve unique sale IDs → house strings
    sale_ids = {r["sale_id"] for r in all_raws if r.get("sale_id")}

    async def _one_sale(sid: int) -> tuple[int, str]:
        async with sem_sale:
            return sid, await _get_sale_house(client, sid)

    sale_results = await asyncio.gather(*(_one_sale(sid) for sid in sale_ids))
    sale_map: dict[int, str] = dict(sale_results)

    # Build Lot objects
    lots: list[Lot] = []
    for raw in all_raws:
        try:
            close_dt = datetime.fromtimestamp(raw["ts"], tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            continue

        house = sale_map.get(raw.get("sale_id"), "Drouot")
        currency = CURRENCY_SYMBOL.get(raw["currency_id"], raw["currency_id"]) or None

        lots.append(Lot(
            source=name,
            artist=raw["artist_name"],
            title=raw["description"],
            house=house,
            close_date=close_dt,
            url=raw["url"],
            image_url=raw.get("image_url"),
            estimate_low=int(raw["low"]) if raw.get("low") else None,
            estimate_high=int(raw["high"]) if raw.get("high") else None,
            currency=currency,
        ))

    by_artist: dict[str, int] = {}
    for lot in lots:
        by_artist[lot.artist] = by_artist.get(lot.artist, 0) + 1
    for artist, count in by_artist.items():
        log.info("drouot: %-30s → %2d lots", artist, count)

    return lots
