import re
from datetime import datetime
from pydantic import BaseModel, HttpUrl

# Map currency symbol → ISO code. $ is assumed USD (most common case).
_SYMBOL_TO_CODE: dict[str, str] = {
    "$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY",
    "CHF": "CHF", "HK$": "HKD", "A$": "AUD", "C$": "CAD",
}


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
    dimensions: str | None = None  # e.g. "50 × 60 cm"
    estimate_eur: int | None = None  # midpoint converted to EUR post-scrape
    is_new: bool = False

    @property
    def currency_code(self) -> str | None:
        if not self.currency:
            return None
        return _SYMBOL_TO_CODE.get(self.currency.strip())

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
