"""
Invaluable scraper — large auction aggregator using Algolia search.

Invaluable uses Algolia (app: 0HJBNDV358, index: upcoming_lots_prod).
We query the Algolia API directly with httpx — no browser required.

The read-only search API key is embedded in their public JS bundle and
retrieved once at startup via a lightweight bundle fetch.

Concurrency: 10 (pure API calls, fast).
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

name = "invaluable"

ALGOLIA_APP_ID = "0HJBNDV358"
ALGOLIA_API_KEY = "c72467a0649841b28a88222132bef0ea"
ALGOLIA_INDEX = "upcoming_lots_prod"
ALGOLIA_URL = (
    f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/*/queries"
)
IMAGE_BASE = "https://image.invaluable.com/hermes/"

CONCURRENCY = 10

CURRENCY_SYMBOL = {
    "USD": "$", "GBP": "£", "EUR": "€", "HKD": "HK$",
    "CHF": "CHF ", "JPY": "¥", "AUD": "A$", "CAD": "C$",
}


def _normalize(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _hit_to_lot(hit: dict, artist_name: str) -> Lot | None:
    # Prefer endTimeUTCUnix (per-lot closing time) over dateTimeUTCUnix (sale start)
    ts = hit.get("endTimeUTCUnix") or hit.get("dateTimeUTCUnix")
    if not ts or ts <= 0:
        return None
    try:
        close_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None

    lot_ref = hit.get("lotRef")
    catalog_ref = hit.get("catalogRef")
    if not lot_ref:
        return None
    if catalog_ref:
        url = f"https://www.invaluable.com/catalog/{catalog_ref}/lots/{lot_ref}/"
    else:
        url = f"https://www.invaluable.com/auction-lot/{lot_ref}/"

    house = hit.get("houseName") or "Invaluable"
    title = hit.get("lotTitle") or "Untitled"

    currency_code = (hit.get("currencyCode") or "").upper()
    currency = CURRENCY_SYMBOL.get(currency_code, currency_code) or None

    photo = hit.get("photoPath")
    image_url = f"{IMAGE_BASE}{photo}" if photo else None

    low = hit.get("estimateLow")
    high = hit.get("estimateHigh")

    return Lot(
        source=name,
        artist=artist_name,
        title=str(title),
        house=house,
        close_date=close_dt,
        url=url,
        image_url=image_url,
        estimate_low=int(low) if low else None,
        estimate_high=int(high) if high else None,
        currency=currency,
    )


async def _search_artist(client: httpx.AsyncClient, artist: Artist) -> list[Lot]:
    try:
        r = await client.post(
            ALGOLIA_URL,
            headers={
                "X-Algolia-Application-Id": ALGOLIA_APP_ID,
                "X-Algolia-API-Key": ALGOLIA_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "requests": [
                    {
                        "indexName": ALGOLIA_INDEX,
                        "query": artist.name,
                        "params": "hitsPerPage=50",
                    }
                ]
            },
            timeout=15.0,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("invaluable: %s failed: %s", artist.name, e)
        return []

    hits = (r.json().get("results") or [{}])[0].get("hits") or []

    artist_norm = _normalize(artist.name)
    lots = []
    for hit in hits:
        hit_artist = _normalize(hit.get("artistName") or "")
        # Accept if there's substantial overlap (handles partial name matches)
        if artist_norm not in hit_artist and hit_artist not in artist_norm:
            continue
        lot = _hit_to_lot(hit, artist.name)
        if lot:
            lots.append(lot)

    if lots:
        log.info("invaluable: %-30s → %2d lots", artist.name, len(lots))
    return lots


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _one(a: Artist) -> list[Lot]:
        async with sem:
            return await _search_artist(client, a)

    results = await asyncio.gather(*(_one(a) for a in artists))
    return [lot for sub in results for lot in sub]
