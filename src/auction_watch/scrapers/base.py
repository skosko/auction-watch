from typing import Protocol

import httpx

from ..artists import Artist
from ..models import Lot


class Scraper(Protocol):
    name: str

    async def collect(
        self, client: httpx.AsyncClient, artists: list[Artist]
    ) -> list[Lot]: ...
