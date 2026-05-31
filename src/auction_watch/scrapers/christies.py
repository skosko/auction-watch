"""
Christie's scraper — server-rendered, but search API is Akamai-bot-blocked.

Workflow:
  1. GET /en/calendar → walk embedded JSON for upcoming sale records.
  2. Filter to sales within the lookahead window.
  3. For each sale, GET the sale page → walk embedded `"lots":[...]` array.
  4. Substring-match `title_primary_txt` against followed-artist names.

Christie's puts each lot's full record (title, image, estimate, URL) inline
in the sale-page HTML, so no per-lot fetch is required.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from ..artists import Artist
from ..models import Lot
from ._utils import currency_symbol, normalize

log = logging.getLogger(__name__)

name = "christies"

CALENDAR_URL = "https://www.christies.com/en/calendar"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

SALE_FETCH_CONCURRENCY = 4

LANDING_URL_RE = re.compile(r'"landing_url"\s*:\s*"https?://[^"]*/en/auction/')
LOTS_ARRAY_RE = re.compile(r'"lots"\s*:\s*\[')
ESTIMATE_CURRENCY_RE = re.compile(r"^([A-Z]{3})\s")

def _walk_object(text: str, start: int) -> str | None:
    """Return the slice of `text` starting at `text[start] == '{'` that closes the object."""
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
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _walk_array(text: str, start: int) -> str | None:
    """Same as `_walk_object` but for arrays starting at `text[start] == '['`."""
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


def _extract_sales(html: str) -> list[dict]:
    """Find every JSON sale record on the calendar page by walking back from each `landing_url`."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in LANDING_URL_RE.finditer(html):
        i = m.start()
        depth = 0
        while i > 0:
            c = html[i]
            if c == "}":
                depth += 1
            elif c == "{":
                if depth == 0:
                    blob = _walk_object(html, i)
                    if blob:
                        try:
                            record = json.loads(blob)
                            url = record.get("landing_url")
                            if url and url not in seen:
                                seen.add(url)
                                out.append(record)
                        except json.JSONDecodeError:
                            pass
                    break
                depth -= 1
            i -= 1
    return out


def _parse_calendar_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_lots(sale_html: str) -> list[dict]:
    m = LOTS_ARRAY_RE.search(sale_html)
    if not m:
        return []
    blob = _walk_array(sale_html, m.end() - 1)
    if not blob:
        return []
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return []


def _lot_to_model(
    lot: dict,
    artist_display: str,
    sale_title: str,
    sale_location: str,
    sale_close: datetime,
    sale_url: str,
) -> Lot | None:
    # Christie's "custom highlight" preview lots lack a published lot URL until
    # closer to the sale; fall back to the sale page so users can still click through.
    url = lot.get("url") or sale_url
    if not url:
        return None
    image = (lot.get("image") or {}).get("image_src")
    estimate_low = lot.get("estimate_low")
    estimate_high = lot.get("estimate_high")
    try:
        estimate_low_int = int(float(estimate_low)) if estimate_low else None
    except (TypeError, ValueError):
        estimate_low_int = None
    try:
        estimate_high_int = int(float(estimate_high)) if estimate_high else None
    except (TypeError, ValueError):
        estimate_high_int = None
    cur = None
    m = ESTIMATE_CURRENCY_RE.match(lot.get("estimate_txt") or "")
    if m:
        cur = currency_symbol(m.group(1))

    # Prefer the lot's own end_date when it's plausible; otherwise fall back to sale date.
    close_dt = _parse_iso(lot.get("end_date")) or sale_close

    house = "Christie's"
    if sale_location:
        house += f" {sale_location}"
    if sale_title:
        house = f"{house} — {sale_title}"

    return Lot(
        source=name,
        artist=artist_display,
        title=(lot.get("title_primary_txt") or lot.get("title_secondary_txt") or "Untitled").title(),
        house=house,
        close_date=close_dt,
        url=url,
        image_url=image or None,
        estimate_low=estimate_low_int,
        estimate_high=estimate_high_int,
        currency=cur,
    )


async def _fetch(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=25.0)
        r.raise_for_status()
    except Exception as e:
        log.warning("christies: failed to fetch %s: %s", url, e)
        return None
    return r.text


def _match_artist(text: str, artists_by_norm: dict[str, str]) -> str | None:
    """Return the display name if any followed artist's normalized name appears in `text`."""
    if not text:
        return None
    norm_text = normalize(text)
    for norm_name, display in artists_by_norm.items():
        # word-ish boundary check to avoid e.g. "ai" matching "fair"
        if norm_name in norm_text:
            # Confirm the match isn't a substring of a larger word
            idx = norm_text.find(norm_name)
            before = norm_text[idx - 1] if idx > 0 else " "
            after_idx = idx + len(norm_name)
            after = norm_text[after_idx] if after_idx < len(norm_text) else " "
            if not before.isalnum() and not after.isalnum():
                return display
    return None


async def collect(client: httpx.AsyncClient, artists: list[Artist], days: int = 90) -> list[Lot]:
    artists_by_norm = {normalize(a.name): a.name for a in artists}

    calendar_html = await _fetch(client, CALENDAR_URL)
    if not calendar_html:
        return []

    all_sales = _extract_sales(calendar_html)
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)
    upcoming = []
    for sale in all_sales:
        start = _parse_calendar_date(sale.get("start_date"))
        if not start:
            continue
        if now <= start <= cutoff:
            upcoming.append((sale, start))
    log.info("christies: %d upcoming sales within %d days", len(upcoming), days)

    sem = asyncio.Semaphore(SALE_FETCH_CONCURRENCY)

    async def _scrape_sale(sale: dict, sale_close: datetime) -> list[Lot]:
        async with sem:
            html = await _fetch(client, sale["landing_url"])
            if not html:
                return []
            lots_raw = _extract_lots(html)
            matched: list[Lot] = []
            for lot in lots_raw:
                title = lot.get("title_primary_txt") or ""
                display = _match_artist(title, artists_by_norm)
                if not display:
                    continue
                model = _lot_to_model(
                    lot,
                    artist_display=display,
                    sale_title=sale.get("title_txt") or "",
                    sale_location=sale.get("location_txt") or "",
                    sale_close=sale_close,
                    sale_url=sale.get("landing_url") or "",
                )
                if model:
                    matched.append(model)
            if matched:
                log.info(
                    "christies: %-30s → %2d lots",
                    (sale.get("title_txt") or "?")[:30],
                    len(matched),
                )
            return matched

    results = await asyncio.gather(*(_scrape_sale(s, d) for s, d in upcoming))
    return [lot for sub in results for lot in sub]
