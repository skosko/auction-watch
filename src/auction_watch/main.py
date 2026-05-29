import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .artists import Artist, load_artists
from .mailer import cat_image_url, send_digest
from .models import Lot
from .render import render_digest
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
]


async def _gather_lots(artists: list[Artist]) -> list[Lot]:
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(s.collect(client, artists) for s in SCRAPERS), return_exceptions=True
        )
    out: list[Lot] = []
    for scraper, res in zip(SCRAPERS, results):
        if isinstance(res, Exception):
            log.error("%s scraper failed: %s", scraper.name, res)
            continue
        log.info("%s: %d lots returned", scraper.name, len(res))
        out.extend(res)
    return out


def _within_window(lots: list[Lot], days: int) -> list[Lot]:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)
    return [l for l in lots if l.close_date is None or now <= l.close_date <= cutoff]


def _dedupe(lots: list[Lot]) -> list[Lot]:
    seen: set[str] = set()
    out: list[Lot] = []
    for lot in lots:
        if lot.dedupe_key in seen:
            continue
        seen.add(lot.dedupe_key)
        out.append(lot)
    return out


def cli():
    recipient = os.environ.get("DIGEST_RECIPIENT")

    artists = load_artists("artists.yml")
    log.info("Loaded %d artists from artists.yml", len(artists))

    if os.environ.get("EMPTY_PREVIEW") == "1":
        lots: list[Lot] = []
    else:
        all_lots = asyncio.run(_gather_lots(artists))
        in_window = _within_window(all_lots, LOOKAHEAD_DAYS)
        lots = _dedupe(in_window)
        log.info(
            "Total: %d scraped, %d in %d-day window, %d after dedupe",
            len(all_lots),
            len(in_window),
            LOOKAHEAD_DAYS,
            len(lots),
        )

    cat = cat_image_url() if not lots else None
    html = render_digest(lots, cat_image_url=cat)

    out = Path("last_digest.html")
    out.write_text(html, encoding="utf-8")
    log.info("Wrote %s", out.resolve())

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
