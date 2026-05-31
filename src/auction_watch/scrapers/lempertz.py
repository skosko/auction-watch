"""
Lempertz scraper — Auktionshaus Lempertz (Cologne).

Strategy:
  For each artist:
  1. Generate a slug heuristic (lastname-firstname) and try the artist-index
     page directly; fall back to a site search if the page 404s.
  2. Parse the "Current offers" section of the artist-index page to get lot
     URLs, thumbnail images, and a rough estimate.
  3. Fetch each unique lot page (concurrently) to get the close date,
     full estimate range, and dimensions from the JSON-LD/HTML.

Images are served from lempertz.com/lempertz_api/images/large/ and are
NOT hotlink-protected — unlike Invaluable.
"""

import asyncio
import html as html_lib
import json
import logging
import re
from datetime import datetime, timezone

import httpx

from ..artists import Artist
from ..models import Lot

log = logging.getLogger(__name__)

name = "lempertz"

BASE = "https://www.lempertz.com"
SEARCH_URL = f"{BASE}/en/search"

CONCURRENCY = 5       # artist-index page fetches
CONCURRENCY_LOT = 8   # lot detail page fetches

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Germany is UTC+1 (CET) / UTC+2 (CEST); use UTC+1 as a safe default
_UTC_OFFSET = 1


def _slug(artist_name: str) -> str:
    """'Norbert Bisky' → 'bisky-norbert'  (Lempertz last-first slug format)."""
    clean = re.sub(r"[^a-z0-9 ]", "", artist_name.lower())
    parts = clean.split()
    if len(parts) >= 2:
        return parts[-1] + "-" + "-".join(parts[:-1])
    return parts[0] if parts else ""


async def _artist_index_url(
    client: httpx.AsyncClient, artist: Artist
) -> str | None:
    """Return the artist-index URL or None if not found."""
    slug = _slug(artist.name)
    if slug:
        url = f"{BASE}/en/catalogues/artist-index/detail/{slug}.html"
        try:
            r = await client.head(url, headers=HEADERS, timeout=10.0)
            if r.status_code == 200:
                return url
        except Exception:
            pass

    # Fallback: search the site and grab the first artist-index link
    try:
        r = await client.get(
            SEARCH_URL,
            params={"tx_kesearch_pi1[sword]": artist.name},
            headers=HEADERS,
            timeout=15.0,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("lempertz: search for %s failed: %s", artist.name, e)
        return None

    links = re.findall(
        r'href="(/en/catalogues/artist-index/detail/[^"]+\.html)"',
        r.text,
    )
    if not links:
        return None

    name_norm = re.sub(r"[^a-z0-9]", "", artist.name.lower())
    for link in links:
        link_slug = link.split("/")[-1].replace(".html", "")
        link_norm = re.sub(r"[^a-z0-9]", "", link_slug)
        if name_norm in link_norm or link_norm in name_norm:
            return BASE + link
    return BASE + links[0]


def _parse_current_offers(html: str) -> list[dict]:
    """Parse active (biddable) lot stubs from a Lempertz artist-index page.

    Instead of relying on fragile section-boundary matching, we scan ALL
    .artist-detail-lot-item cards on the page and keep only those that
    contain an active "Bid" button — past lots show a "Result" price instead.
    """
    lots = []
    for card in re.finditer(
        r'<div class="artist-detail-lot-item">(.*?)</div>\s*</div>\s*</a>\s*</div>',
        html,
        re.DOTALL,
    ):
        text = card.group(1)

        # Only upcoming/active lots have a "Bid" button
        if "Bid</span>" not in text and "Online Bid" not in text:
            continue

        url_m = re.search(r'href="(/en/catalogues/lot/[^"]+)"', text)
        if not url_m:
            continue
        lot_url = BASE + url_m.group(1)

        # Build the large-size image URL from the thumbnail src
        img_m = re.search(
            r'src="(https://www\.lempertz\.com/lempertz_api/images/[^"]+)"',
            text,
        )
        image_url = None
        if img_m:
            image_url = re.sub(r"/images/\w+/", "/images/large/", img_m.group(1))

        lots.append({"url": lot_url, "image_url": image_url})

    return lots


async def _fetch_lot(client: httpx.AsyncClient, url: str) -> dict | None:
    """Fetch a lot detail page and return title, estimates, date, dimensions."""
    try:
        r = await client.get(url, headers=HEADERS, timeout=15.0)
        r.raise_for_status()
    except Exception as e:
        log.warning("lempertz: lot fetch failed %s: %s", url, e)
        return None

    html = r.text

    # ── Title + estimates from JSON-LD ────────────────────────────────────
    title, estimate_low, estimate_high, dimensions = None, None, None, None
    jld_m = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL
    )
    if jld_m:
        try:
            data = json.loads(jld_m.group(1))
            title = html_lib.unescape(data.get("name", "")).strip()
            # Strip "Artist - " prefix if present
            if " - " in title:
                title = title.split(" - ", 1)[1].strip()
            offers = data.get("offers", {})
            low = offers.get("lowPrice")
            high = offers.get("highPrice")
            if low:
                estimate_low = int(float(low))
            if high:
                estimate_high = int(float(high))
            # Dimensions from description field
            desc = data.get("description", "")
            dim_m = re.search(
                r"(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)"
                r"(?:\s*[xX×]\s*[\d.,]+)?\s*(cm|in(?:ch(?:es)?)?)\b",
                desc,
                re.IGNORECASE,
            )
            if dim_m:
                try:
                    w = float(dim_m.group(1).replace(",", "."))
                    h = float(dim_m.group(2).replace(",", "."))
                    unit = dim_m.group(3).lower()
                    if unit.startswith("in"):
                        w, h = round(w * 2.54, 1), round(h * 2.54, 1)
                    dimensions = f"{w:g} × {h:g} cm"
                except ValueError:
                    pass
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback title from HTML
    if not title:
        t_m = re.search(r'<h1[^>]*class="[^"]*lot-title[^"]*"[^>]*>(.*?)</h1>', html, re.DOTALL)
        if t_m:
            title = re.sub(r"<[^>]+>", "", t_m.group(1)).strip()

    # ── Estimate from HTML (fallback / supplement) ─────────────────────────
    if not estimate_low or not estimate_high:
        est_m = re.search(
            r'class="lot-price"[^>]*>\s*([\d.,]+)\s*€\s*[-–]\s*([\d.,]+)\s*€',
            html,
        )
        if est_m:
            try:
                estimate_low = int(est_m.group(1).replace(".", "").replace(",", ""))
                estimate_high = int(est_m.group(2).replace(".", "").replace(",", ""))
            except ValueError:
                pass

    # ── Close date from HTML ───────────────────────────────────────────────
    close_date = None
    date_m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", html)
    if date_m:
        day = int(date_m.group(1))
        month = int(date_m.group(2))
        year = int(date_m.group(3))
        # First time match (HH:MM)
        time_m = re.search(r"\b(\d{1,2}):(\d{2})\b", html)
        hour, minute = 14, 0  # default to 14:00 if not found
        if time_m:
            hour, minute = int(time_m.group(1)), int(time_m.group(2))
        try:
            # Lempertz is in Germany (CET = UTC+1)
            from datetime import timedelta
            offset = timedelta(hours=_UTC_OFFSET)
            close_date = datetime(year, month, day, hour, minute, tzinfo=timezone(offset))
        except ValueError:
            pass

    return {
        "title": title,
        "estimate_low": estimate_low,
        "estimate_high": estimate_high,
        "dimensions": dimensions,
        "close_date": close_date,
    }


async def _search_artist(
    client: httpx.AsyncClient, artist: Artist
) -> list[dict]:
    artist_url = await _artist_index_url(client, artist)
    if not artist_url:
        return []

    try:
        r = await client.get(artist_url, headers=HEADERS, timeout=15.0)
        r.raise_for_status()
    except Exception as e:
        log.warning("lempertz: artist page fetch failed for %s: %s", artist.name, e)
        return []

    stubs = _parse_current_offers(r.text)
    for stub in stubs:
        stub["artist_name"] = artist.name
    return stubs


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    sem = asyncio.Semaphore(CONCURRENCY)
    sem_lot = asyncio.Semaphore(CONCURRENCY_LOT)

    async def _one_artist(a: Artist) -> list[dict]:
        async with sem:
            return await _search_artist(client, a)

    # Phase 1: gather lot stubs from artist-index pages
    results = await asyncio.gather(*(_one_artist(a) for a in artists))

    # Deduplicate by URL (multiple artists may share a lot, e.g. collaborations)
    seen_urls: dict[str, dict] = {}
    for artist, stubs in zip(artists, results):
        for stub in stubs:
            url = stub["url"]
            if url not in seen_urls:
                seen_urls[url] = stub

    if not seen_urls:
        return []

    # Phase 2: fetch lot detail pages
    async def _one_lot(url: str) -> tuple[str, dict | None]:
        async with sem_lot:
            return url, await _fetch_lot(client, url)

    detail_results = await asyncio.gather(*(_one_lot(url) for url in seen_urls))

    # Build Lot objects
    lots: list[Lot] = []
    by_artist: dict[str, int] = {}

    for url, detail in detail_results:
        if detail is None:
            continue
        stub = seen_urls[url]
        artist_name = stub["artist_name"]

        title = detail.get("title") or "Untitled"
        if not title:
            title = "Untitled"

        lots.append(
            Lot(
                source=name,
                artist=artist_name,
                title=title,
                house="Lempertz",
                close_date=detail.get("close_date"),
                url=url,
                image_url=stub.get("image_url"),
                estimate_low=detail.get("estimate_low"),
                estimate_high=detail.get("estimate_high"),
                currency="€",
                dimensions=detail.get("dimensions"),
            )
        )
        by_artist[artist_name] = by_artist.get(artist_name, 0) + 1

    for artist_name, count in by_artist.items():
        log.info("lempertz: %-30s → %2d lots", artist_name, count)

    return lots
