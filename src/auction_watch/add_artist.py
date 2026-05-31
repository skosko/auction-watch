import asyncio
import sys
from pathlib import Path

import httpx
import yaml

GRAPHQL_URL = "https://metaphysics-cdn.artsy.net/v2"
ARTISTS_FILE = Path("artists.yml")

_QUERY = """
query SearchArtist($query: String!) {
  searchConnection(query: $query, entities: [ARTIST], first: 3) {
    edges {
      node {
        ... on Artist {
          name
          slug
          bio
        }
      }
    }
  }
}
"""


async def _search(query: str) -> dict | None:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GRAPHQL_URL,
            json={"query": _QUERY, "variables": {"query": query}},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    edges = (data.get("data") or {}).get("searchConnection", {}).get("edges", [])
    for edge in edges:
        node = edge.get("node") or {}
        if node.get("slug"):
            return node
    return None


def _existing_slugs() -> set[str]:
    if not ARTISTS_FILE.exists():
        return set()
    with ARTISTS_FILE.open() as f:
        data = yaml.safe_load(f) or {}
    return {a["slug"] for a in data.get("artists", [])}


def _append(name: str, slug: str, bio: str | None) -> None:
    text = ARTISTS_FILE.read_text()
    lines = []
    if text and not text.endswith("\n"):
        lines.append("\n")
    lines.append(f'  - name: "{name}"\n')
    lines.append(f"    slug: {slug}\n")
    if bio:
        lines.append(f'    bio: "{bio.replace(chr(34), chr(92) + chr(34))}"\n')
    with ARTISTS_FILE.open("a") as f:
        f.writelines(lines)


def cli() -> None:
    if len(sys.argv) < 2:
        print("Usage: auction-watch-add <artist name>", file=sys.stderr)
        sys.exit(1)
    query = " ".join(sys.argv[1:])

    artist = asyncio.run(_search(query))
    if not artist:
        print(f"No Artsy result found for: {query!r}", file=sys.stderr)
        sys.exit(1)

    name: str = artist.get("name") or query
    slug: str = artist["slug"]
    bio: str | None = artist.get("bio") or None

    if slug in _existing_slugs():
        print(f"Already tracked: {name} ({slug})")
        sys.exit(0)

    _append(name, slug, bio)
    print(f"Added: {name} ({slug})")
