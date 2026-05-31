import asyncio
import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .artists import Artist, load_artists
from .mailer import cat_image_url, send_digest
from .models import Lot
from .render import render_digest, render_web
from .scrapers import (
    artsy,
    bonhams,
    christies,
    dorotheum,
    drouot,
    firstdibs,
    invaluable,
    juliens,
    ketterer,
    lempertz,
    phillips,
    rago,
    sothebys,
    vanham,
)

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Quiet httpx's per-request logging — too noisy with hundreds of calls.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("auction-watch")

LOOKAHEAD_DAYS = 90
# Rago's platform index covers Rago, Wright, LA Modern Auctions, Landry Pop, and
# Poster Auctions International (same Inertia.js backend) — so one scraper covers all five.
SCRAPERS = [
    artsy,
    rago,
    sothebys,
    christies,
    phillips,
    bonhams,
    ketterer,
    invaluable,
    drouot,
    dorotheum,
    juliens,
    firstdibs,
    vanham,
    lempertz,
]


SCRAPER_TIMEOUT = 300  # seconds; kills any scraper that hangs

# Invaluable house name keywords → our scraper source name.
# Used to deduplicate Invaluable lots already covered by a direct scraper.
_INVALUABLE_HOUSE_MAP: list[tuple[str, str]] = [
    ("van ham", "vanham"),
    ("bonham", "bonhams"),
    ("drouot", "drouot"),
    ("phillips", "phillips"),
    ("rago", "rago"),
    ("wright", "rago"),    # Wright Auction is on the same Inertia platform
    ("julien", "juliens"),
    ("lempertz", "lempertz"),
]


def _invaluable_direct_source(house: str) -> str | None:
    h = house.lower()
    for keyword, source in _INVALUABLE_HOUSE_MAP:
        if keyword in h:
            return source
    return None


async def _fetch_eur_rates(client: httpx.AsyncClient) -> dict[str, float]:
    """Fetch EUR-based exchange rates from frankfurter.app (free, no key required)."""
    try:
        r = await client.get(
            "https://api.frankfurter.app/latest?base=EUR", timeout=10.0
        )
        r.raise_for_status()
        rates: dict[str, float] = r.json().get("rates", {})
        rates["EUR"] = 1.0
        return rates
    except Exception as e:
        log.warning("EUR rate fetch failed: %s", e)
        return {}


async def _gather_lots(artists: list[Artist]) -> tuple[list[Lot], dict[str, float]]:
    async with httpx.AsyncClient() as client:
        scraper_results, eur_rates = await asyncio.gather(
            asyncio.gather(
                *(
                    asyncio.wait_for(s.collect(client, artists), timeout=SCRAPER_TIMEOUT)
                    for s in SCRAPERS
                ),
                return_exceptions=True,
            ),
            _fetch_eur_rates(client),
        )
    out: list[Lot] = []
    for scraper, res in zip(SCRAPERS, scraper_results):
        if isinstance(res, Exception):
            log.error("%s scraper failed: %s", scraper.name, res)
            continue
        log.info("%s: %d lots returned", scraper.name, len(res))
        out.extend(res)
    return out, eur_rates


def _apply_eur_prices(lots: list[Lot], rates: dict[str, float]) -> None:
    """Set estimate_eur (midpoint) on each lot using today's exchange rates."""
    for lot in lots:
        code = lot.currency_code
        if not code or code not in rates:
            continue
        rate = rates[code]  # units of code per 1 EUR
        if lot.estimate_low and lot.estimate_high:
            mid = (lot.estimate_low + lot.estimate_high) / 2
        elif lot.estimate_low:
            mid = float(lot.estimate_low)
        elif lot.estimate_high:
            mid = float(lot.estimate_high)
        else:
            continue
        lot.estimate_eur = round(mid / rate)


def _within_window(lots: list[Lot], days: int) -> list[Lot]:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)
    return [l for l in lots if l.close_date is None or now <= l.close_date <= cutoff]


def _norm(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _dedupe(lots: list[Lot]) -> list[Lot]:
    # Pass 1: exact URL dedup
    seen: set[str] = set()
    out: list[Lot] = []
    for lot in lots:
        if lot.dedupe_key in seen:
            continue
        seen.add(lot.dedupe_key)
        out.append(lot)

    # Pass 2: cross-source dedup — Artsy is an aggregator and often mirrors lots
    # already found by a direct-house scraper.
    #
    # For Drouot: title matching is unreliable because our Drouot scraper stores
    # the raw lot description (e.g. "CHAPMAN JAKE AND DINOS Chess set, 2003 ...")
    # rather than a clean title. Drop any Artsy lot when a Drouot lot exists for
    # the same (artist, date-day) without requiring a title match.
    #
    # For all other direct sources: use prefix-aware title matching so edition
    # suffixes ("Too Darn Hot 69" vs "Too Darn Hot") don't cause misses.
    drouot_artist_dates: set[tuple] = set()
    direct_by_artist_date: dict[tuple, set[str]] = {}
    for lot in out:
        if lot.source == "artsy" or lot.close_date is None:
            continue
        key = (_norm(lot.artist), lot.close_date.date())
        if lot.source == "drouot":
            drouot_artist_dates.add(key)
        else:
            direct_by_artist_date.setdefault(key, set()).add(_norm(lot.title))

    def _artsy_is_dup(lot: Lot) -> bool:
        if lot.source != "artsy" or lot.close_date is None:
            return False
        key = (_norm(lot.artist), lot.close_date.date())
        if key in drouot_artist_dates:
            return True
        candidates = direct_by_artist_date.get(key)
        if not candidates:
            return False
        a = _norm(lot.title)
        for d in candidates:
            short, long_ = (a, d) if len(a) <= len(d) else (d, a)
            if len(short) >= 8 and long_.startswith(short):
                return True
        return False

    out = [lot for lot in out if not _artsy_is_dup(lot)]

    # Pass 3: Invaluable dedup — Invaluable aggregates houses we also scrape directly
    # (Van Ham, Bonhams, …). When a direct-source lot exists for the same
    # (artist, date-day), prefer the direct lot (it has a working image URL).
    # Title matching is intentionally skipped: titles may differ by language.
    direct_by_source: dict[str, set[tuple]] = {}
    for lot in out:
        mapped = _invaluable_direct_source(lot.house)
        if lot.source == mapped and lot.close_date is not None:
            direct_by_source.setdefault(mapped, set()).add(
                (_norm(lot.artist), lot.close_date.date())
            )

    out = [
        lot for lot in out
        if not (
            lot.source == "invaluable"
            and lot.close_date is not None
            and (mapped := _invaluable_direct_source(lot.house)) is not None
            and (_norm(lot.artist), lot.close_date.date())
            in direct_by_source.get(mapped, set())
        )
    ]

    # Pass 4: Drouot dedup — some auction houses (e.g. Van Ham) list their lots
    # on both their own site and the Drouot umbrella platform. Prefer the direct
    # scraper when we have a matching (artist, date-day) lot.
    drouot_direct_by_source: dict[str, set[tuple]] = {}
    for lot in out:
        mapped = _invaluable_direct_source(lot.house)
        if lot.source == mapped and lot.close_date is not None:
            drouot_direct_by_source.setdefault(mapped, set()).add(
                (_norm(lot.artist), lot.close_date.date())
            )

    out = [
        lot for lot in out
        if not (
            lot.source == "drouot"
            and lot.close_date is not None
            and (mapped := _invaluable_direct_source(lot.house)) is not None
            and (_norm(lot.artist), lot.close_date.date())
            in drouot_direct_by_source.get(mapped, set())
        )
    ]

    # Pass 5: Invaluable vs Drouot dedup — Invaluable aggregates Drouot lots
    # but lists them under the specific auctioneer name (e.g. "De Baecque &
    # Associés") rather than "Drouot", so Pass 3's house-name matching misses
    # them. Prefer Drouot because it reliably provides images.
    drouot_artist_dates: set[tuple] = {
        (_norm(lot.artist), lot.close_date.date())
        for lot in out
        if lot.source == "drouot" and lot.close_date is not None
    }

    return [
        lot for lot in out
        if not (
            lot.source == "invaluable"
            and lot.close_date is not None
            and (_norm(lot.artist), lot.close_date.date()) in drouot_artist_dates
        )
    ]


def cli():
    recipient = os.environ.get("DIGEST_RECIPIENT")

    artists = load_artists("artists.yml")
    log.info("Loaded %d artists from artists.yml", len(artists))

    if os.environ.get("EMPTY_PREVIEW") == "1":
        lots: list[Lot] = []
    else:
        all_lots, eur_rates = asyncio.run(_gather_lots(artists))
        in_window = _within_window(all_lots, LOOKAHEAD_DAYS)
        lots = _dedupe(in_window)
        _apply_eur_prices(lots, eur_rates)
        log.info(
            "Total: %d scraped, %d in %d-day window, %d after dedupe",
            len(all_lots),
            len(in_window),
            LOOKAHEAD_DAYS,
            len(lots),
        )

    cat = cat_image_url() if not lots else None
    artist_slugs = {a.name: a.slug for a in artists}
    html = render_digest(lots, cat_image_url=cat, artist_slugs=artist_slugs)

    out = Path("last_digest.html")
    out.write_text(html, encoding="utf-8")
    log.info("Wrote %s", out.resolve())

    web = render_web(lots, artist_slugs=artist_slugs, github_token=os.environ.get("ADD_ARTIST_TOKEN", ""))
    web_out = Path("_site/index.html")
    web_out.parent.mkdir(exist_ok=True)
    web_out.write_text(web, encoding="utf-8")
    log.info("Wrote %s", web_out.resolve())

    if not os.environ.get("RESEND_API_KEY"):
        log.info("RESEND_API_KEY not set — skipping send.")
        return
    if not recipient:
        log.info("DIGEST_RECIPIENT not set — skipping send.")
        return

    result = send_digest(html, recipient)
    log.info("Sent: id=%s", result.get("id"))


if __name__ == "__main__":
    cli()
