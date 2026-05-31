"""
Ketterer Kunst scraper — traditional server-rendered PHP site.

Strategy:
1. GET /auktion/auktion-info.php?gebiet=9 → parse auction number → date mappings.
2. GET /result.php?gebiet=9&auswahl=kommend&shw=1 (with pagination) → upcoming lots.
3. Extract artist + title from img alt text ("Artist Name - Title").
4. Match against followed artists; build lot URL from object/auction numbers.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx

from ..artists import Artist
from ..models import Lot
from ._utils import normalize

log = logging.getLogger(__name__)

name = "ketterer"

BASE = "https://www.kettererkunst.de"
LOTS_URL = f"{BASE}/result.php"
AUCTION_INFO_URL = f"{BASE}/auktion/auktion-info.php"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
PAGE_SIZE = 30


def _parse_german_date(s: str) -> datetime | None:
    try:
        return datetime.strptime(s.strip(), "%d.%m.%Y").replace(hour=18, tzinfo=timezone.utc)
    except ValueError:
        return None


async def _fetch(client: httpx.AsyncClient, url: str, **kwargs) -> str | None:
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=20.0, **kwargs)
        r.raise_for_status()
        return r.content.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("ketterer: failed to fetch %s: %s", url, e)
        return None


async def _get_auction_dates(client: httpx.AsyncClient) -> dict[str, datetime]:
    html = await _fetch(client, AUCTION_INFO_URL, params={"gebiet": "9"})
    if not html:
        return {}
    # "Auktion: 606 am 12.06.2026" — the colon and HTML tags vary
    pairs = re.findall(
        r"Auktion:?\s*(?:<[^>]*>)*\s*(\d+)[^<\n]*?am\s+(\d{1,2}\.\d{2}\.\d{4})",
        html,
        re.IGNORECASE,
    )
    result: dict[str, datetime] = {}
    for num, date_str in pairs:
        dt = _parse_german_date(date_str)
        if dt:
            result[num] = dt
    return result


# Matches each lot image: captures auction number, object number, and alt text.
# Lots render as <input type='image' src='...' alt='Artist - Title'> (not <img>).
_IMG_RE = re.compile(
    r"<input\s+type=['\"]image['\"][^>]*"
    r"src=['\"](?P<img>/still/kunst/pic\d+/(?P<auc>\d+)/(?P<obnr>\d+)\.(?:jpg|png))['\"]"
    r"[^>]*alt=['\"](?P<alt>[^'\"]+)['\"]",
    re.IGNORECASE,
)


def _parse_lots_html(
    html: str,
    artists_by_norm: dict[str, str],
    auction_dates: dict[str, datetime],
) -> list[Lot]:
    lots = []
    for m in _IMG_RE.finditer(html):
        alt = m.group("alt")
        if " - " not in alt:
            continue
        artist_part, title_part = alt.split(" - ", 1)
        artist_norm = normalize(artist_part)

        display = artists_by_norm.get(artist_norm)
        if not display:
            continue

        auction_num = m.group("auc")
        obnr = m.group("obnr")
        close_dt = auction_dates.get(auction_num)
        if not close_dt:
            continue

        # Scan forward for estimate (up to ~1200 chars covers artist name + title + price block)
        ctx = html[m.start() : m.start() + 1200]
        est_m = re.search(
            r"[Ss]ch(?:&auml;|ä|a)tzpreis[^€€]*(?:&euro;|€)\s*([\d]{1,3}(?:[.,]\d{3})*)",
            ctx,
        )
        estimate: int | None = None
        if est_m:
            try:
                estimate = int(est_m.group(1).replace(".", "").replace(",", ""))
            except ValueError:
                pass

        lot_url = f"{BASE}/kunst/kd/details.php?obnr={obnr}&anummer={auction_num}"
        image_url = f"{BASE}{m.group('img')}"

        lots.append(
            Lot(
                source=name,
                artist=display,
                title=title_part.strip(),
                house=f"Ketterer Kunst — Auktion {auction_num}",
                close_date=close_dt,
                url=lot_url,
                image_url=image_url,
                estimate_low=None,
                estimate_high=estimate,
                currency="€",
            )
        )
    return lots


async def _get_all_lots_html(client: httpx.AsyncClient) -> list[str]:
    """Fetch upcoming-lot detail pages.

    Each page (shw=1) shows the main lot in detail plus ~26 related upcoming
    lots in a sidebar. We advance by PAGE_SIZE until the page yields no new
    lots.  Fetching a few spread-out pages ensures we cover lots from all
    upcoming auctions, not just the first one.
    """
    pages = []
    seen_obnrs: set[str] = set()
    offsets = [1, PAGE_SIZE + 1, PAGE_SIZE * 2 + 1, PAGE_SIZE * 3 + 1]
    for offset in offsets:
        html = await _fetch(
            client,
            LOTS_URL,
            params={"gebiet": "9", "auswahl": "kommend", "shw": "1", "objsei": str(offset)},
        )
        if not html:
            break
        new_obnrs = {m.group("obnr") for m in _IMG_RE.finditer(html)}
        if not new_obnrs - seen_obnrs:
            break  # no new lots on this page → stop
        seen_obnrs |= new_obnrs
        pages.append(html)
    return pages


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    artists_by_norm = {normalize(a.name): a.name for a in artists}

    auction_dates, pages = await asyncio.gather(
        _get_auction_dates(client),
        _get_all_lots_html(client),
    )
    if not pages:
        return []

    log.info("ketterer: %d pages of upcoming lots, %d auctions dated", len(pages), len(auction_dates))

    lots: list[Lot] = []
    for html in pages:
        lots.extend(_parse_lots_html(html, artists_by_norm, auction_dates))

    if lots:
        for artist in sorted(set(l.artist for l in lots)):
            count = sum(1 for l in lots if l.artist == artist)
            log.info("ketterer: %-30s → %2d lots", artist, count)
    return lots
