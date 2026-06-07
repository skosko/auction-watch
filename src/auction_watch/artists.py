from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Artist:
    name: str
    slug: str
    bio: str | None = None


def load_artists(path: Path | str = "artists.yml") -> list[Artist]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [
        Artist(name=a["name"], slug=a["slug"], bio=a.get("bio"))
        for a in data.get("artists", [])
    ]


def load_search_terms(path: Path | str = "artists.yml") -> list[str]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return list(data.get("search_terms", []))
