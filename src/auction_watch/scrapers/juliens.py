"""
Julien's Auctions scraper — entertainment & pop culture memorabilia.

Julien's uses a Supabase/PostgREST backend at api.juliensauctions.com.
We query the lot_campaign_view table directly with httpx — no browser needed.

The public anon key (juliensApiAnonToken) is embedded in their JS bundle.
Estimates are stored in cents (divide by 100 for USD).
Lot URL pattern: https://www.juliensauctions.com/en/items/{id}

Concurrency: 10 (pure API calls, fast).
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

name = "juliens"

API_BASE = "https://api.juliensauctions.com/rest/v1"
LOT_BASE = "https://www.juliensauctions.com/en/items/"
API_KEY = "sb_publishable_iQTmMpf0DZCzSGfPr4Eu7A_Nf80Trrk"

CONCURRENCY = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "apikey": API_KEY,
    "Authorization": f"Bearer {API_KEY}",
    "Accept-Profile": "public",
    "Origin": "https://www.juliensauctions.com",
    "Referer": "https://www.juliensauctions.com/",
}


def _normalize(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _fts_query(artist_name: str) -> str:
    """Build a PostgREST FTS query like 'Damien & Hirst:*'."""
    words = re.split(r"\s+", artist_name.strip())
    # Escape special tsquery characters
    safe = [re.sub(r"[^A-Za-z0-9]", "", w) for w in words if w]
    safe = [w for w in safe if w]
    if not safe:
        return ""
    # AND all terms, prefix search on last word
    return " & ".join(safe[:-1] + [safe[-1] + ":*"])


async def _search_artist(client: httpx.AsyncClient, artist: Artist) -> list[Lot]:
    query = _fts_query(artist.name)
    if not query:
        return []

    params = {
        "select": (
            "id,title,end_date,low_estimate,high_estimate,image_url,"
            "auction_campaign!inner(title,slug,status)"
        ),
        "fts": f"fts.{query}",
        "status": "eq.ITEM_OPEN",
        "is_hidden": "is.false",
        "auction_campaign.is_hidden": "is.false",
        "order": "end_date.asc.nullslast",
        "limit": "50",
    }

    try:
        r = await client.get(
            f"{API_BASE}/lot_campaign_view",
            params=params,
            headers=HEADERS,
            timeout=15.0,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("juliens: %s failed: %s", artist.name, e)
        return []

    hits = r.json()
    if not isinstance(hits, list):
        return []

    artist_norm = _normalize(artist.name)
    lots = []

    for hit in hits:
        # Verify artist name actually appears in the lot title
        title_norm = _normalize(hit.get("title") or "")
        if artist_norm not in title_norm:
            continue

        end_raw = hit.get("end_date")
        if not end_raw:
            continue
        try:
            close_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        lot_id = hit.get("id")
        if not lot_id:
            continue

        auction = hit.get("auction_campaign") or {}
        auction_title = auction.get("title") or ""
        house = "Julien's Auctions"
        if auction_title:
            house = f"{house} — {auction_title}"

        # Estimates stored in cents
        low_cents = hit.get("low_estimate")
        high_cents = hit.get("high_estimate")

        lots.append(Lot(
            source=name,
            artist=artist.name,
            title=hit["title"],
            house=house,
            close_date=close_dt.astimezone(timezone.utc),
            url=f"{LOT_BASE}{lot_id}",
            image_url=hit.get("image_url") or None,
            estimate_low=int(low_cents // 100) if low_cents else None,
            estimate_high=int(high_cents // 100) if high_cents else None,
            currency="$",
        ))

    if lots:
        log.info("juliens: %-30s → %2d lots", artist.name, len(lots))
    return lots


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _one(a: Artist) -> list[Lot]:
        async with sem:
            return await _search_artist(client, a)

    results = await asyncio.gather(*(_one(a) for a in artists))
    return [lot for sub in results for lot in sub]
