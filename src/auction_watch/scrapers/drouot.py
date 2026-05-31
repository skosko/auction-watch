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


# N x N [x N] cm/in/mm — also handles comma as decimal separator
_DIM_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)"
    r"(?:\s*[xX×]\s*[\d.,]+)?\s*(cm|in(?:ch(?:es)?)?|mm)\b",
    re.IGNORECASE,
)

# French H/L labelled format: "H: 19; L: 26 cm" or "Hauteur: 50, Largeur: 70 cm"
# High-confidence: both labels present + explicit unit.
_DIM_HL_RE = re.compile(
    r"[Hh](?:auteur)?[.:]\s*(\d+(?:[.,]\d+)?)\s*(?:cm|in|mm)?\s*[;,]?\s*"
    r"[Ll](?:argeur)?[.:]\s*(\d+(?:[.,]\d+)?)\s*(cm|in(?:ch(?:es)?)?|mm)\b",
    re.IGNORECASE,
)


def _to_cm(a: float, b: float, unit: str) -> str:
    u = unit.lower()
    if u == "mm":
        a, b = round(a / 10, 1), round(b / 10, 1)
    elif u.startswith("in"):
        a, b = round(a * 2.54, 1), round(b * 2.54, 1)
    return f"{a:g} × {b:g} cm"


def _extract_dimensions(text: str) -> str | None:
    m = _DIM_RE.search(text)
    if m:
        try:
            w, h = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
        except ValueError:
            return None
        return _to_cm(w, h, m.group(3))
    m = _DIM_HL_RE.search(text)
    if m:
        try:
            h_val, l_val = float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", "."))
        except ValueError:
            return None
        return _to_cm(h_val, l_val, m.group(3))
    return None


def _parse_title(description: str) -> str:
    """Extract a clean title from a raw Drouot description.

    Two formats seen in the wild:
    1. Structured (labelled fields): contains "Titre :\\nActual Title.\\n..."
       — extract the Titre field directly.
    2. Flat: starts with "LASTNAME FIRSTNAME ..." in ALL-CAPS
       — strip leading all-caps tokens until the first mixed-case word.
    """
    # Format 1: structured labelled fields
    m = re.search(r"(?:Titre|Title)\s*:\s*\n([^\n]+)", description, re.IGNORECASE)
    if m:
        title = m.group(1).strip().rstrip(".")
        if title:
            return title

    # Format 2: strip ALL-CAPS artist header tokens
    tokens = description.split()
    i = 0
    while i < len(tokens):
        letters = re.sub(r"[^a-zA-Z]", "", tokens[i])
        # Skip tokens that are all-caps or contain no letters (punctuation)
        if not letters or letters.isupper():
            i += 1
        else:
            break
    title = " ".join(tokens[i:]) if i < len(tokens) else ""
    return title.strip() or description.strip()


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
        desc = raw["description"]
        title = _parse_title(desc)[:200]
        dimensions = _extract_dimensions(desc)

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
            currency=currency,
            dimensions=dimensions,
        ))

    by_artist: dict[str, int] = {}
    for lot in lots:
        by_artist[lot.artist] = by_artist.get(lot.artist, 0) + 1
    for artist, count in by_artist.items():
        log.info("drouot: %-30s → %2d lots", artist, count)

    return lots
