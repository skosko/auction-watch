"""
Shared scraper logic for auction houses on the Inertia.js platform (Rago, Wright).

The site embeds a full JSON page payload in `<div id="app" data-page="...">`.
Strategy:
  1. GET /auctions → parse `sessions` and `auctions` from the page props.
  2. For each upcoming session within the lookahead window, GET its alias URL.
  3. The session page embeds an `items` dict with every lot's artist + metadata.
  4. Match `item.artist_name` against the followed-artist set; emit Lot rows.
"""

import asyncio
import html
import json
import logging
import re
from datetime import datetime, timezone

import httpx

from ..artists import Artist
from ..models import Lot
from ._utils import normalize

log = logging.getLogger(__name__)

DATA_PAGE_RE = re.compile(r'id="app"[^>]*data-page="([^"]*)"')
ESTIMATE_NUM_RE = re.compile(r"[\d,]+")
DOMAIN_RE = re.compile(r"^(?:https?:)?//(?:www\.)?([^/]+)", re.IGNORECASE)

SESSION_FETCH_CONCURRENCY = 4

# Sister houses on the same Inertia platform — Rago's index lists sessions
# from all of them via absolute-URL aliases. (source_id, display_name)
PLATFORM_HOUSES = {
    "ragoarts.com": ("rago", "Rago"),
    "wright20.com": ("wright", "Wright"),
    "lamodern.com": ("lamodern", "Los Angeles Modern Auctions"),
    "landrypop.com": ("landrypop", "Landry Pop"),
    "posterauctions.com": ("posterauctions", "Poster Auctions International"),
}


def _parse_inertia(html_text: str) -> dict:
    m = DATA_PAGE_RE.search(html_text)
    if not m:
        return {}
    try:
        return json.loads(html.unescape(m.group(1)))
    except json.JSONDecodeError:
        return {}



def _parse_high_estimate(formatted: str | None) -> int | None:
    if not formatted:
        return None
    # e.g. "estimate: $6,000&ndash;8,000" → low=6,000  high=8,000
    nums = ESTIMATE_NUM_RE.findall(html.unescape(formatted))
    if len(nums) >= 2:
        return int(nums[1].replace(",", ""))
    return None


def _resolve_alias(base_url: str, alias: str) -> str:
    """Aliases can be relative ('/auctions/...') or absolute ('www.wright20.com/...').

    Rago's index includes sessions from sister houses with bare domain prefixes.
    """
    alias = alias.strip()
    if alias.startswith(("http://", "https://")):
        return alias
    if alias.startswith("//"):
        return f"https:{alias}"
    if alias.startswith("www."):
        return f"https://{alias}"
    return f"{base_url.rstrip('/')}/{alias.lstrip('/')}"


def _house_from_url(url: str, fallback: tuple[str, str]) -> tuple[str, str]:
    m = DOMAIN_RE.match(url)
    if not m:
        return fallback
    return PLATFORM_HOUSES.get(m.group(1).lower(), fallback)


async def _fetch_session(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str
) -> dict | None:
    async with sem:
        try:
            r = await client.get(
                url, headers={"User-Agent": "auction-watch/0.1"}, timeout=20.0
            )
            r.raise_for_status()
        except Exception as e:
            log.warning("inertia: failed to fetch %s: %s", url, e)
            return None
        return _parse_inertia(r.text)


async def collect_for_house(
    client: httpx.AsyncClient,
    base_url: str,
    source: str,
    artists: list[Artist],
    days: int = 90,
) -> list[Lot]:
    artists_by_norm = {normalize(a.name): a.name for a in artists}

    index = await _fetch_session(
        client, asyncio.Semaphore(1), f"{base_url}/auctions"
    )
    if not index:
        return []
    props = index.get("props", {})
    sessions = props.get("sessions") or {}
    auctions = props.get("auctions") or {}
    house_label = props.get("house_name") or source.title()

    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff_ts = now_ts + days * 86400
    upcoming = [
        s for s in sessions.values()
        if s.get("start_date") and now_ts <= s["start_date"] <= cutoff_ts and s.get("alias")
    ]
    log.info("%s: %d upcoming sessions within %d days", source, len(upcoming), days)

    sem = asyncio.Semaphore(SESSION_FETCH_CONCURRENCY)
    session_urls = [_resolve_alias(base_url, s["alias"]) for s in upcoming]
    session_payloads = await asyncio.gather(
        *(_fetch_session(client, sem, u) for u in session_urls)
    )

    lots: list[Lot] = []
    for session, session_url, payload in zip(upcoming, session_urls, session_payloads):
        if not payload:
            continue
        items = (payload.get("props") or {}).get("items") or {}
        auction = auctions.get(session.get("auction_fd_key")) or auctions.get(str(session.get("auction_fd_key"))) or {}
        sale_title = auction.get("title") or session.get("title") or ""
        close_dt = datetime.fromtimestamp(session["start_date"], tz=timezone.utc)
        session_source, session_house_label = _house_from_url(session_url, (source, house_label))
        session_base = session_url.split("/auctions/")[0] if "/auctions/" in session_url else base_url

        for item in items.values():
            raw_artist = (item.get("artist_name") or "").strip()
            if not raw_artist:
                continue
            display = artists_by_norm.get(normalize(raw_artist))
            if not display:
                continue
            alias = (item.get("alias") or "").lstrip("/")
            lot_url = f"{session_base}/{alias}" if alias else session_url
            lots.append(
                Lot(
                    source=session_source,
                    artist=display,
                    title=item.get("name") or "Untitled",
                    house=f"{session_house_label} — {sale_title}".strip(" —"),
                    close_date=close_dt,
                    url=lot_url,
                    image_url=item.get("primary_mosaic_image") or item.get("primary_index_image") or None,
                    estimate_low=item.get("estimate_low"),
                    estimate_high=_parse_high_estimate(item.get("estimate_formatted")),
                    currency="$",
                )
            )
    return lots
