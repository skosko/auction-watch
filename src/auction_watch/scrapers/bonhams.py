"""
Bonhams scraper — Next.js SSR with __NEXT_DATA__ containing search results.

One request per artist: https://www.bonhams.com/search/?q={name}&isUpcoming=true
The page embeds lot results in __NEXT_DATA__ JSON with full estimate/date/image data.

Bonhams group includes Skinner and Cornette de Saint Cyr; lot URLs differ by brand.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from curl_cffi.requests import AsyncSession

from ..artists import Artist
from ..models import Lot
from ._utils import currency_symbol, normalize

log = logging.getLogger(__name__)

name = "bonhams"

SEARCH_URL = "https://www.bonhams.com/search/"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)

BRAND_BASE = {
    "bonhams": "https://www.bonhams.com",
    "skinner": "https://skinner.bonhams.com",
    "cornette": "https://csc.bonhams.com",
}

CONCURRENCY = 5


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _lot_to_model(raw: dict, artist_name: str) -> Lot | None:
    if (raw.get("flags") or {}).get("isAuctionEnded"):
        return None
    slug = raw.get("slug")
    if not slug:
        return None

    brand = raw.get("brand") or "bonhams"
    base = BRAND_BASE.get(brand, BRAND_BASE["bonhams"])
    url = f"{base}/lot/{slug}/"

    end_date = raw.get("auctionEndDate") or {}
    close_dt = _parse_dt(end_date.get("datetime"))
    if not close_dt:
        return None

    price = raw.get("price") or {}
    iso_code = (raw.get("currency") or {}).get("iso_code") or ""
    currency = currency_symbol(iso_code)

    house = "Bonhams"
    if brand == "skinner":
        house = "Skinner"
    elif brand == "cornette":
        house = "Cornette de Saint Cyr"

    return Lot(
        source=name,
        artist=artist_name,
        title=(raw.get("title") or "Untitled"),
        house=house,
        close_date=close_dt.astimezone(timezone.utc),
        url=url,
        image_url=(raw.get("image") or {}).get("url") or None,
        estimate_low=int(price["estimateLow"]) if price.get("estimateLow") else None,
        estimate_high=int(price["estimateHigh"]) if price.get("estimateHigh") else None,
        currency=currency,
    )


async def _search_artist(session: AsyncSession, artist: Artist) -> list[Lot]:
    try:
        r = await session.get(
            SEARCH_URL,
            params={"q": artist.name, "isUpcoming": "true"},
            timeout=20.0,
            allow_redirects=True,
            impersonate="chrome124",
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("bonhams: %s failed: %s", artist.name, e)
        return []

    m = NEXT_DATA_RE.search(r.text)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    raw_lots = (
        data.get("props", {})
        .get("pageProps", {})
        .get("lotData", {})
        .get("auctionLots", [])
    )

    artist_norm = normalize(artist.name)
    lots = []
    for raw in raw_lots:
        # Bonhams full-text search returns false positives; verify the artist
        # name actually appears in the lot title before including.
        title_norm = normalize(raw.get("title") or "")
        if artist_norm not in title_norm:
            continue
        lot = _lot_to_model(raw, artist.name)
        if lot:
            lots.append(lot)
    if lots:
        log.info("bonhams: %-30s → %2d lots", artist.name, len(lots))
    return lots


async def collect(client, artists: list[Artist]) -> list[Lot]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async with AsyncSession() as session:
        async def _one(a: Artist) -> list[Lot]:
            async with sem:
                return await _search_artist(session, a)

        results = await asyncio.gather(*(_one(a) for a in artists))
    return [lot for sub in results for lot in sub]
