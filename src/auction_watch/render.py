from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

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
        return f"{cur}{lot.estimate_low:,}–{lot.estimate_high:,}"
    val = lot.estimate_low or lot.estimate_high
    return f"{cur}{val:,}"


_env.filters["estimate"] = _format_estimate


def render_digest(
    lots: list[Lot],
    cat_image_url: str | None = None,
    artist_slugs: dict[str, str] | None = None,
) -> str:
    template = _env.get_template("email.html.j2")

    def _sort_key(lot: Lot):
        # Auction lots (with close date) first, sorted by date; dealer listings at end by artist
        if lot.close_date is not None:
            return (0, lot.close_date, lot.artist)
        return (1, datetime.max.replace(tzinfo=timezone.utc), lot.artist)

    return template.render(
        lots=sorted(lots, key=_sort_key),
        generated_at=datetime.now(timezone.utc),
        cat_image_url=cat_image_url,
        artist_slugs=artist_slugs or {},
    )
