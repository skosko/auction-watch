"""
1stDibs scraper — luxury buy-now marketplace (1stdibs.com).

1stDibs is primarily a dealer marketplace (fixed price / make-an-offer),
not a traditional auction house with timed bidding. Their GraphQL search API
returns listing items with price fields but no close dates or endDate fields.
The /auctions/ URL redirects to the homepage and their "context=auction"
search filter does not surface timed lots.

Currently returns 0 lots — kept as a stub for future improvement if
1stDibs introduces or exposes a timed-auction API.
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

name = "1stdibs"

SEARCH_URL = "https://www.1stdibs.com/search/"
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
    for fmt in (None, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            if fmt is None:
                return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            return datetime.strptime(str(s), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _gql_item_to_lot(item: dict, artist_name: str) -> Lot | None:
    """Handle a GraphQL item node which may be a Lot or Product type."""
    # Try lot-specific fields first
    url = item.get("pdpUrl") or item.get("url") or item.get("href")
    if not url:
        return None
    if not url.startswith("http"):
        url = f"https://www.1stdibs.com{url}"

    # Close date: lots have auctionEndDate / endDate / closingDate
    close_raw = (
        item.get("auctionEndDate") or item.get("endDate") or item.get("closingDate")
        or item.get("saleDate") or item.get("date")
    )
    close_dt = _parse_dt(close_raw)
    if not close_dt:
        return None

    title = item.get("title") or item.get("name") or item.get("description") or "Untitled"
    auction_title = (
        item.get("auctionTitle") or (item.get("auction") or {}).get("title") or ""
    )
    house = "1stDibs"
    if auction_title:
        house = f"{house} — {auction_title}"

    # Price/estimate
    price_info = item.get("price") or item.get("estimate") or {}
    if isinstance(price_info, dict):
        low = price_info.get("min") or price_info.get("low") or price_info.get("amount")
        high = price_info.get("max") or price_info.get("high")
        currency_raw = price_info.get("currency") or price_info.get("currencyCode") or "USD"
    else:
        low = item.get("lowEstimate") or item.get("estimateLow")
        high = item.get("highEstimate") or item.get("estimateHigh")
        currency_raw = item.get("currency") or item.get("currencyCode") or "USD"

    currency = CURRENCY_SYMBOL.get(str(currency_raw).upper(), str(currency_raw))

    # Image
    image = item.get("firstPhotoUrl") or item.get("imageUrl") or item.get("photo")
    if isinstance(image, dict):
        image = image.get("url") or image.get("src") or image.get("smallPath")

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


def _extract_from_gql(payload: dict, artist_name: str) -> list[Lot]:
    """Walk a GraphQL response to find lot nodes."""
    lots = []
    if not isinstance(payload, dict):
        return lots

    # Try common GraphQL response shapes
    data = payload.get("data") or payload

    def _walk(node):
        if isinstance(node, dict):
            # Check if this looks like a lot/item node
            if node.get("pdpUrl") or (node.get("url") and node.get("endDate")):
                lot = _gql_item_to_lot(node, artist_name)
                if lot:
                    lots.append(lot)
                    return
            # Look for edges/nodes pattern
            for key in ("edges", "nodes", "items", "results", "lots", "hits"):
                val = node.get(key)
                if isinstance(val, list):
                    for item in val:
                        _walk(item)
            # Walk other dict values
            for v in node.values():
                if isinstance(v, (dict, list)):
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return lots


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    # 1stDibs is a buy-now marketplace; no timed auction lots are exposed via API.
    return []
