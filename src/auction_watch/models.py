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
    is_new: bool = False

    @property
    def dedupe_key(self) -> str:
        return f"{self.source}::{self.url}"
