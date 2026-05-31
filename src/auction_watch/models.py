import re
from datetime import datetime
from pydantic import BaseModel, HttpUrl


class Lot(BaseModel):
    source: str
    artist: str
    title: str
    house: str
    close_date: datetime | None = None  # None for dealer listings
    url: HttpUrl
    image_url: HttpUrl | None = None
    estimate_low: int | None = None
    estimate_high: int | None = None
    currency: str | None = None
    dimensions: str | None = None  # e.g. "50 × 60 cm" — Artsy only for now
    is_new: bool = False

    @property
    def dedupe_key(self) -> str:
        return f"{self.source}::{self.url}"

    @property
    def area_cm2(self) -> int | None:
        """Best-effort area in cm² for size sorting. Returns None when unparseable."""
        if not self.dimensions:
            return None
        m = re.search(r"([\d.]+)\s*[×x]\s*([\d.]+)\s*cm", self.dimensions, re.IGNORECASE)
        if m:
            return round(float(m.group(1)) * float(m.group(2)))
        m = re.search(r"([\d.]+)\s*[×x]\s*([\d.]+)\s*in", self.dimensions, re.IGNORECASE)
        if m:
            return round(float(m.group(1)) * float(m.group(2)) * 6.4516)
        return None
