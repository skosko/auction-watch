from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .artists import Artist
from .models import Lot

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)


def _format_estimate(lot: Lot) -> str:
    if lot.estimate_low is None and lot.estimate_high is None:
        return "Estimate not published"
    cur = lot.currency or ""
    if lot.estimate_low and lot.estimate_high:
        lo, hi = sorted([lot.estimate_low, lot.estimate_high])
        return f"{cur}{lo:,}–{hi:,}"
    val = lot.estimate_low or lot.estimate_high
    return f"{cur}{val:,}"


_env.filters["estimate"] = _format_estimate


def _sort_key(lot: Lot):
    if lot.close_date is not None:
        return (0, lot.close_date, lot.artist)
    return (1, datetime.max.replace(tzinfo=timezone.utc), lot.artist)


def render_digest(
    lots: list[Lot],
    cat_image_url: str | None = None,
    artist_slugs: dict[str, str] | None = None,
    site_url: str = "",
) -> str:
    template = _env.get_template("email.html.j2")
    return template.render(
        lots=sorted(lots, key=_sort_key),
        generated_at=datetime.now(timezone.utc),
        cat_image_url=cat_image_url,
        artist_slugs=artist_slugs or {},
        site_url=site_url,
    )


def render_web(
    lots: list[Lot],
    artist_slugs: dict[str, str] | None = None,
    proxy_url: str = "",
    tracked_artists: list[Artist] | None = None,
    search_terms: list[str] | None = None,
) -> str:
    template = _env.get_template("web.html.j2")
    sorted_lots = sorted(lots, key=_sort_key)
    seen: dict[str, None] = {}
    for lot in sorted_lots:
        seen.setdefault(lot.artist, None)
    artists = sorted(seen.keys())
    return template.render(
        lots=sorted_lots,
        artists=artists,
        generated_at=datetime.now(timezone.utc),
        artist_slugs=artist_slugs or {},
        proxy_url=proxy_url,
        tracked_artists=tracked_artists or [],
        search_terms=search_terms or [],
    )
