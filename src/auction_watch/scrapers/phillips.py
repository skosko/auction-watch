"""
Phillips scraper — server-rendered HTML with embedded lot data.

The /auctions listing page embeds all upcoming lots as a JS array in the HTML.
One GET request returns all ~130 upcoming lots across all active sales.
"""

import json
import logging
import re
import unicodedata
from datetime import datetime, timezone

import httpx

from ..artists import Artist
from ..models import Lot

log = logging.getLogger(__name__)

name = "phillips"

AUCTIONS_URL = "https://www.phillips.com/auctions"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

LOTS_ARRAY_RE = re.compile(r'"lots"\s*:\s*\[')


def _normalize(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _walk_array(text: str, start: int) -> str | None:
    depth = 0
    i = start
    in_str = False
    esc = False
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    artists_by_norm = {_normalize(a.name): a.name for a in artists}

    try:
        r = await client.get(
            AUCTIONS_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=25.0,
            follow_redirects=True,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("phillips: failed to fetch auctions page: %s", e)
        return []

    html = r.text
    m = LOTS_ARRAY_RE.search(html)
    if not m:
        log.warning("phillips: lots array not found in page")
        return []

    arr_start = html.index("[", m.start())
    blob = _walk_array(html, arr_start)
    if not blob:
        log.warning("phillips: could not parse lots array")
        return []

    try:
        all_lots = json.loads(blob)
    except json.JSONDecodeError as e:
        log.warning("phillips: JSON parse error: %s", e)
        return []

    log.info("phillips: %d upcoming lots on page", len(all_lots))

    lots: list[Lot] = []
    for raw in all_lots:
        maker_norm = _normalize(raw.get("makerName") or "")
        display = artists_by_norm.get(maker_norm)
        if not display:
            continue
        url = raw.get("detailLink")
        if not url:
            continue
        close_dt = _parse_dt(raw.get("lotEndDateTime"))
        if not close_dt:
            continue

        location = raw.get("locationName") or ""
        sale_title = raw.get("saleTitle") or ""
        house = "Phillips"
        if location:
            house += f" {location}"
        if sale_title:
            house = f"{house} — {sale_title}"

        low = raw.get("lowEstimate")
        high = raw.get("highEstimate")
        lots.append(
            Lot(
                source=name,
                artist=display,
                title=(raw.get("description") or "Untitled").strip(),
                house=house,
                close_date=close_dt.astimezone(timezone.utc),
                url=url,
                image_url=raw.get("imagePath") or None,
                estimate_low=int(low) if low else None,
                estimate_high=int(high) if high else None,
                currency=raw.get("currencySign") or None,
            )
        )

    if lots:
        log.info("phillips: %d lots matched", len(lots))
    return lots
