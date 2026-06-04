import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx

from ..artists import Artist
from ..models import Lot
from ._utils import currency_symbol

log = logging.getLogger(__name__)

name = "artsy"

MAX_RETRIES = 5
RETRY_BASE_DELAY = 2.0  # seconds; doubles each attempt
ARTIST_CONCURRENCY = 3

METAPHYSICS = "https://metaphysics-cdn.artsy.net/v2"
ARTSY_BASE = "https://www.artsy.net"

QUERY = """
query ArtistAuctionLots($slug: String!, $first: Int!) {
  artist(id: $slug) {
    filterArtworksConnection(first: $first, atAuction: true) {
      edges {
        node {
          title
          href
          image { url(version: ["large", "medium"]) }
          dimensions { cm in }
          saleArtwork {
            currency
            endAt
            lowEstimate { display }
            highEstimate { display }
            sale {
              name
              endAt
              liveStartAt
            }
          }
        }
      }
    }
  }
}
"""


def _parse_money(display: str | None) -> int | None:
    if not display:
        return None
    digits = re.sub(r"[^\d]", "", display)
    return int(digits) if digits else None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _is_transient(payload: dict) -> bool:
    """True if the GraphQL response is a retry-worthy backend hiccup."""
    for err in payload.get("errors") or []:
        codes = (err.get("extensions") or {}).get("httpStatusCodes") or []
        if any(c in (502, 503, 504) for c in codes):
            return True
        if "Server Error" in (err.get("message") or ""):
            return True
    return False


async def search(
    client: httpx.AsyncClient,
    artist_slug: str,
    artist_name: str,
    first: int = 50,
) -> list[Lot]:
    payload: dict = {}
    for attempt in range(MAX_RETRIES):
        resp = await client.post(
            METAPHYSICS,
            json={"query": QUERY, "variables": {"slug": artist_slug, "first": first}},
            headers={"User-Agent": "auction-watch/0.1"},
            timeout=20.0,
        )
        resp.raise_for_status()
        payload = resp.json()
        if not _is_transient(payload):
            break
        delay = RETRY_BASE_DELAY * (2**attempt)
        log.debug("artsy: %s transient error, retrying in %.1fs", artist_slug, delay)
        await asyncio.sleep(delay)
    if "errors" in payload:
        # Log the first error tersely instead of dumping the whole list
        first_err = (payload["errors"] or [{}])[0].get("message", "?")
        log.warning("artsy: %s gave up after retries: %s", artist_slug, first_err.splitlines()[0])
    artist = (payload.get("data") or {}).get("artist")
    if not artist:
        return []
    edges = (artist.get("filterArtworksConnection") or {}).get("edges") or []

    lots: list[Lot] = []
    for edge in edges:
        node = edge.get("node") or {}
        sa = node.get("saleArtwork") or {}
        sale = sa.get("sale") or {}
        close_dt = _parse_dt(sa.get("endAt")) or _parse_dt(sale.get("endAt")) or _parse_dt(sale.get("liveStartAt"))
        if not close_dt:
            continue
        href = node.get("href") or ""
        url = f"{ARTSY_BASE}{href}" if href.startswith("/") else href
        if not url:
            continue
        image_url = (node.get("image") or {}).get("url")
        dims = node.get("dimensions") or {}
        dimensions = dims.get("cm") or dims.get("in") or None
        currency_code = sa.get("currency")
        currency = currency_symbol(currency_code)
        lots.append(
            Lot(
                source="artsy",
                artist=artist_name,
                title=node.get("title") or "Untitled",
                house=sale.get("name") or "Artsy Auctions",
                close_date=close_dt.astimezone(timezone.utc),
                url=url,
                image_url=image_url or None,
                estimate_low=_parse_money((sa.get("lowEstimate") or {}).get("display")),
                estimate_high=_parse_money((sa.get("highEstimate") or {}).get("display")),
                currency=currency,
                dimensions=dimensions,
            )
        )
    return lots


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    sem = asyncio.Semaphore(ARTIST_CONCURRENCY)

    async def _one(artist: Artist) -> list[Lot]:
        async with sem:
            try:
                lots = await search(client, artist.slug, artist.name)
                if lots:
                    log.info("artsy: %-30s → %2d lots", artist.name, len(lots))
                return lots
            except Exception as e:
                log.warning("artsy: %s failed: %s", artist.slug, e)
                return []

    results = await asyncio.gather(*(_one(a) for a in artists))
    return [lot for sub in results for lot in sub]
