"""
Sotheby's scraper — uses their public Algolia search index.

The index `bsp_dotcom_prod_en` contains every lot (auction + retail). Filtering
to `type:"Lot" AND available:"true"` returns only active auction lots, and each
hit ships with `endDate` (close time), `lowEstimate`, `highEstimate`,
`estimateCurrency`, `auctionTitle`, `auctionLocations`, `image`, and `url` — so
no follow-up GraphQL call is needed.

The Algolia app ID and API key below are intentionally public; Algolia client
keys are designed to ship in browser JS and we captured them from a real page
load.
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from ..artists import Artist
from ..models import Lot
from ._utils import currency_symbol

log = logging.getLogger(__name__)

name = "sothebys"

ALGOLIA_URL = "https://o28sy4q7wu-dsn.algolia.net/1/indexes/bsp_dotcom_prod_en/query"
HEADERS = {
    "X-Algolia-Application-Id": "O28SY4Q7WU",
    "X-Algolia-API-Key": "e732e65c70ebf8b51d4e2f922b536496",
    "Content-Type": "application/json",
}
CONCURRENCY = 5

def _hit_to_lot(hit: dict, artist_name: str) -> Lot | None:
    end_ms = hit.get("endDate")
    if not end_ms:
        return None
    close_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    url = hit.get("url")
    if not url:
        return None
    locations = hit.get("auctionLocations") or []
    house = "Sotheby's"
    if locations:
        house += f" {locations[0]}"
    auction_title = hit.get("auctionTitle")
    if auction_title:
        house = f"{house} — {auction_title}"
    cur_code = hit.get("estimateCurrency")
    return Lot(
        source=name,
        artist=artist_name,
        title=(hit.get("title") or "Untitled").strip(),
        house=house,
        close_date=close_dt,
        url=url,
        image_url=hit.get("image") or None,
        estimate_low=hit.get("lowEstimate"),
        estimate_high=hit.get("highEstimate"),
        currency=currency_symbol(cur_code),
    )


async def _search_artist(client: httpx.AsyncClient, artist: Artist) -> list[Lot]:
    body = {
        "query": "",
        "filters": 'type:"Lot" AND available:"true"',
        "facetFilters": [[f"artists:{artist.name}"]],
        "hitsPerPage": 100,
    }
    try:
        r = await client.post(ALGOLIA_URL, headers=HEADERS, json=body, timeout=15.0)
        r.raise_for_status()
    except Exception as e:
        log.warning("sothebys: %s failed: %s", artist.name, e)
        return []
    hits = r.json().get("hits") or []
    lots = [lot for hit in hits if (lot := _hit_to_lot(hit, artist.name))]
    if lots:
        log.info("sothebys: %-30s → %2d lots", artist.name, len(lots))
    return lots


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _one(a: Artist) -> list[Lot]:
        async with sem:
            return await _search_artist(client, a)

    results = await asyncio.gather(*(_one(a) for a in artists))
    return [lot for sub in results for lot in sub]
