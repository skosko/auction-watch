"""
Drouot scraper — French auction umbrella (Hôtel Drouot).

Uses Drouot's internal neoGingo JSON API that powers drouot.com.
One lot-search call per artist, then cached sale-detail lookups to
resolve auctioneer name + sale title into the house field.

Concurrency: 10 searches in parallel, 5 parallel sale-detail fetches.
Sale details are cached to disk (drouot_sale_cache.json in the working
directory) so repeat runs skip already-seen sales.
"""

import asyncio
import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ..artists import Artist
from ..models import Lot
from ._utils import currency_symbol, extract_dimensions, normalize

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

# Lots whose sale is in one of these states are still open
ACTIVE_STATUSES = {"CREATED", "IN_PROGRESS", "OPEN"}

# ── Sale detail cache ──────────────────────────────────────────────────────
_SALE_CACHE_PATH = Path("drouot_sale_cache.json")
_sale_cache: dict[int, str] = {}


def _load_sale_cache() -> None:
    global _sale_cache
    if _SALE_CACHE_PATH.exists():
        try:
            raw = json.loads(_SALE_CACHE_PATH.read_text(encoding="utf-8"))
            _sale_cache = {int(k): v for k, v in raw.items()}
            log.debug("drouot: loaded %d cached sale names", len(_sale_cache))
        except Exception as e:
            log.debug("drouot: sale cache load failed: %s", e)
            _sale_cache = {}


def _save_sale_cache() -> None:
    try:
        _SALE_CACHE_PATH.write_text(
            json.dumps({str(k): v for k, v in _sale_cache.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        log.debug("drouot: sale cache save failed: %s", e)


# ── Title / description parsing ────────────────────────────────────────────

# Drouot descriptions appear in several formats:
#   1. Structured: "Titre :\nActual Title\n..."
#   2. Quoted:     'Artist (bio). "Title". year'  or  'ARTIST ... «Title». year'
#   3. Newline:    "Artist.\n\nTitle. year\n\nMaterials..."
#   4. ALL-CAPS:   "ARTIST bio TITLE year" (fallback)

# High-confidence quoted title: "…" «…» or similar typographic quotes.
_DQUOTE_RE = re.compile(r'["\u00ab\u201c]([^"\u00bb\u201d\n]{3,150})["\u00bb\u201d]')
# Lower-confidence single-quoted title: 'ALL-CAPS …' — require uppercase start.
_SQUOTE_RE = re.compile(r"'([A-ZÀÂÄÉÈÊËÎÏÔÙÛÜŒ][^'\n]{2,100})'")
# After double-newline: "Artist.\n\nTitle. year\n\n..."
_AFTER_NL_RE = re.compile(r"\n\n([^\n]{5,})")


def _parse_title(description: str) -> str:
    """Extract a clean title from a raw Drouot description."""
    # Format 1: structured labelled fields "Titre :\nTitle" or "Title :\nTitle"
    m = re.search(r"(?:Titre|Title)\s*:\s*\n([^\n]+)", description, re.IGNORECASE)
    if m:
        title = m.group(1).strip().rstrip(".")
        if title:
            return title

    # Format 2a: double-quoted or guillemet title — high confidence
    m = _DQUOTE_RE.search(description)
    if m:
        title = m.group(1).strip().rstrip(".")
        if title:
            return title

    # Format 3: title immediately after the first double newline
    m = _AFTER_NL_RE.search(description)
    if m:
        # Take only the first sentence (before the first period)
        first = m.group(1).split(".")[0].strip()
        if first and len(first) < 200:
            return first

    # Format 2b: single-quoted ALL-CAPS title — lower confidence
    m = _SQUOTE_RE.search(description)
    if m:
        title = m.group(1).strip().rstrip(".")
        if title:
            return title

    # Format 4: strip ALL-CAPS artist header tokens (original fallback)
    tokens = description.split()
    i = 0
    while i < len(tokens):
        letters = re.sub(r"[^a-zA-Z]", "", tokens[i])
        if not letters or letters.isupper():
            i += 1
        else:
            break
    title = " ".join(tokens[i:]) if i < len(tokens) else ""
    return title.strip() or description.strip()


def _strip_artist_prefix(text: str, artist_name: str) -> str:
    """Remove artist-name tokens from the start of text when they match.

    Handles both "FIRSTNAME LASTNAME" and "LASTNAME FIRSTNAME" orderings by
    comparing sorted normalised word sets. Requires all name words to appear
    in the first N tokens (where N = number of name words).
    """
    def _n(w: str) -> str:
        nfkd = unicodedata.normalize("NFKD", w)
        return re.sub(r"[^a-z0-9]", "", "".join(
            c for c in nfkd if not unicodedata.combining(c)
        ).lower())

    artist_words = [_n(w) for w in artist_name.split() if re.search(r"[a-zA-Z]", w)]
    if not artist_words:
        return text
    n = len(artist_words)
    tokens = text.split()
    if len(tokens) <= n:
        return text
    prefix = [_n(t) for t in tokens[:n]]
    if sorted(prefix) == sorted(artist_words):
        return " ".join(tokens[n:]).lstrip(",-–—· ").strip()
    return text


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
    if sale_id in _sale_cache:
        return _sale_cache[sale_id]
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
            _sale_cache[sale_id] = house
            return house
    except Exception:
        pass
    _sale_cache[sale_id] = "Drouot"
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
    artist_norm = normalize(artist.name)
    raws = []

    for hit in hits:
        status = hit.get("saleStatus", "")
        if status and status not in ACTIVE_STATUSES:
            continue

        # Artist name verification: description usually starts with "LASTNAME FIRSTNAME"
        desc_norm = normalize(hit.get("description") or "")
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
            "description": (hit.get("description") or "Untitled")[:600],
            "sale_id": hit.get("saleId"),
            "currency_id": (hit.get("currencyId") or "EUR").upper(),
            "low": hit.get("lowEstim"),
            "high": hit.get("highEstim"),
            "image_url": image_url,
            "artist_name": artist.name,
        })

    return raws


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    _load_sale_cache()

    sem_search = asyncio.Semaphore(CONCURRENCY)
    sem_sale = asyncio.Semaphore(CONCURRENCY_SALE)

    async def _one_artist(a: Artist) -> list[dict]:
        async with sem_search:
            return await _search_artist(client, a)

    all_raws_nested = await asyncio.gather(*(_one_artist(a) for a in artists))
    all_raws = [r for sub in all_raws_nested for r in sub]

    if not all_raws:
        _save_sale_cache()
        return []

    # Resolve unique sale IDs → house strings (cache-aware)
    sale_ids = {r["sale_id"] for r in all_raws if r.get("sale_id")}
    uncached = sale_ids - set(_sale_cache.keys())

    async def _one_sale(sid: int) -> tuple[int, str]:
        async with sem_sale:
            return sid, await _get_sale_house(client, sid)

    if uncached:
        await asyncio.gather(*(_one_sale(sid) for sid in uncached))

    _save_sale_cache()

    # Build Lot objects
    lots: list[Lot] = []
    for raw in all_raws:
        try:
            close_dt = datetime.fromtimestamp(raw["ts"], tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            continue

        house = _sale_cache.get(raw.get("sale_id"), "Drouot")
        cur = currency_symbol(raw["currency_id"])
        desc = raw["description"]
        title = _strip_artist_prefix(_parse_title(desc), raw["artist_name"])[:200]
        dimensions = extract_dimensions(desc)

        lots.append(Lot(
            source=name,
            artist=raw["artist_name"],
            title=title,
            house=house,
            close_date=close_dt,
            url=raw["url"],
            image_url=raw.get("image_url"),
            estimate_low=int(raw["low"]) if raw.get("low") else None,
            estimate_high=int(raw["high"]) if raw.get("high") else None,
            currency=cur,
            dimensions=dimensions,
        ))

    by_artist: dict[str, int] = {}
    for lot in lots:
        by_artist[lot.artist] = by_artist.get(lot.artist, 0) + 1
    for artist, count in by_artist.items():
        log.info("drouot: %-30s → %2d lots", artist, count)

    return lots
