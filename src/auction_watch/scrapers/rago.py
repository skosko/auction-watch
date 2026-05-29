import httpx

from ..artists import Artist
from ..models import Lot
from . import _inertia

name = "rago"


async def collect(client: httpx.AsyncClient, artists: list[Artist]) -> list[Lot]:
    return await _inertia.collect_for_house(
        client, "https://www.ragoarts.com", name, artists
    )
