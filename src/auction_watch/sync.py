import asyncio
import os
import sys
from pathlib import Path

import httpx
import yaml

ARTSY_OAUTH_URL = "https://api.artsy.net/oauth2/access_token"
ARTSY_FOLLOWS_URL = "https://api.artsy.net/api/v1/follow_artists"
ARTISTS_FILE = Path("artists.yml")
PAGE_SIZE = 100


async def _get_token(client: httpx.AsyncClient, client_id: str, client_secret: str, email: str, password: str) -> str:
    resp = await client.post(
        ARTSY_OAUTH_URL,
        json={
            "client_id": client_id,
            "client_secret": client_secret,
            "email": email,
            "password": password,
            "grant_type": "credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


async def _fetch_follows(client: httpx.AsyncClient, token: str) -> list[dict]:
    results: list[dict] = []
    offset = 0
    while True:
        resp = await client.get(
            ARTSY_FOLLOWS_URL,
            params={"access_token": token, "size": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        resp.raise_for_status()
        batch: list[dict] = resp.json()
        if not batch:
            break
        for item in batch:
            artist = item.get("artist") or {}
            if artist.get("slug"):
                results.append(artist)
        if len(batch) < PAGE_SIZE:
            break
        offset += len(batch)
    return results


def _existing_slugs() -> set[str]:
    if not ARTISTS_FILE.exists():
        return set()
    with ARTISTS_FILE.open() as f:
        data = yaml.safe_load(f) or {}
    return {a["slug"] for a in data.get("artists", [])}


def _append_entries(entries: list[tuple[str, str, str | None]]) -> None:
    text = ARTISTS_FILE.read_text()
    lines = []
    if text and not text.endswith("\n"):
        lines.append("\n")
    for name, slug, bio in entries:
        lines.append(f'  - name: "{name}"\n')
        lines.append(f"    slug: {slug}\n")
        if bio:
            lines.append(f'    bio: "{bio.replace(chr(34), chr(92) + chr(34))}"\n')
    with ARTISTS_FILE.open("a") as f:
        f.writelines(lines)


async def _run(client_id: str, client_secret: str, email: str, password: str) -> int:
    async with httpx.AsyncClient() as client:
        token = await _get_token(client, client_id, client_secret, email, password)
        follows = await _fetch_follows(client, token)

    existing = _existing_slugs()
    new_entries: list[tuple[str, str, str | None]] = []
    for artist in follows:
        slug = artist["slug"]
        if slug in existing:
            continue
        name: str = artist.get("name") or slug
        blurb: str | None = artist.get("blurb") or None
        new_entries.append((name, slug, blurb))
        existing.add(slug)

    if new_entries:
        _append_entries(new_entries)

    return len(new_entries)


def cli() -> None:
    client_id = os.environ.get("ARTSY_CLIENT_ID", "")
    client_secret = os.environ.get("ARTSY_CLIENT_SECRET", "")
    email = os.environ.get("ARTSY_EMAIL", "")
    password = os.environ.get("ARTSY_PASSWORD", "")

    missing = [k for k, v in {
        "ARTSY_CLIENT_ID": client_id,
        "ARTSY_CLIENT_SECRET": client_secret,
        "ARTSY_EMAIL": email,
        "ARTSY_PASSWORD": password,
    }.items() if not v]

    if missing:
        print(f"Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    added = asyncio.run(_run(client_id, client_secret, email, password))
    if added:
        print(f"Added {added} new artist(s) from Artsy follows")
    else:
        print("No new artists to add — already up to date")
