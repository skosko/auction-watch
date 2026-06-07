# auction-watch

Tracks upcoming auction lots and dealer listings for a list of artists. Sends a daily HTML email digest and publishes a browsable website.

## What it does

- Scrapes 15 auction houses and platforms (Sotheby's, Christie's, Phillips, Bonhams, Invaluable, Drouot, Julien's, Rago/Wright, Ketterer, Lempertz, Van Ham, Catawiki, Artsy, and more)
- Deduplicates across sources — direct house scrapers take priority over Artsy mirrors
- Sends a responsive HTML email via [Resend](https://resend.com)
- Publishes all current lots to a filterable website on GitHub Pages

## Website

`https://<your-github-username>.github.io/auction-watch/`

Rebuilt daily. Filter by artist, sort by date or house. Like individual lots (saved in your browser). Manage tracked artists directly from the website with Artsy autocomplete. No login required.

**One-time setup:** Repo Settings → Pages → Source: `gh-pages` branch, `/ (root)`.

## Workflows

### Daily digest (automatic)

Runs every day at 14:00 UTC. Also triggerable manually:

1. Go to **Actions → Auction Digest**
2. Click **Run workflow**

Requires GitHub secrets: `RESEND_API_KEY`, `DIGEST_RECIPIENT`.

---

### Add a single artist

1. Go to **Actions → Add Artist**
2. Click **Run workflow**
3. Enter the artist name (e.g. `Cindy Sherman`)

The workflow searches Artsy, takes the top result, appends the artist to `artists.yml`, and commits. Exits cleanly if the artist is already tracked.

Also works locally:

```sh
uv run auction-watch-add "Cindy Sherman"
```

---

### Sync your full Artsy follows list

Imports every artist you follow on Artsy into `artists.yml` in one go. New artists are appended; existing entries are left unchanged.

1. Go to **Actions → Sync Artists from Artsy**
2. Click **Run workflow**

**Required secrets** (set once in Repo Settings → Secrets):

| Secret | How to get it |
|---|---|
| `ARTSY_CLIENT_ID` | Create an app at [developers.artsy.net](https://developers.artsy.net/) |
| `ARTSY_CLIENT_SECRET` | Same app |
| `ARTSY_EMAIL` | Your Artsy account email |
| `ARTSY_PASSWORD` | Your Artsy account password |

Also works locally (with those vars exported):

```sh
uv run auction-watch-sync
```

---

## Artist list

`artists.yml` — edit freely. Each entry needs a `name` and an Artsy `slug` (the path segment from `artsy.net/artist/<slug>`). The `bio` field is optional and ignored by scrapers.

You can also add free-text `search_terms` that are sent to text-based scrapers (everything except Artsy). Useful for partial names, alternate spellings, or non-artist searches.

```yaml
artists:
  - name: "Cindy Sherman"
    slug: cindy-sherman
    bio: "American, b. 1954"

search_terms:
  - "Bauhaus furniture"
  - "Memphis Milano"
```

### Website artist management

When `PROXY_URL` is set, the website shows **+ Add** and **Manage** buttons:

- **+ Add**: Type to search Artsy's artist database with autocomplete. Click a result to add it to `artists.yml` via a Cloudflare Worker proxy. You can also add a free-text search term from the bottom of the dropdown.
- **Manage**: Shows all tracked artists and search terms with delete buttons. Removals commit directly to `artists.yml`.

The proxy keeps the GitHub PAT server-side (GitHub auto-revokes tokens found in public repos). Setup:

1. `npm install -g wrangler && wrangler login`
2. `cd worker && wrangler deploy`
3. `wrangler secret put GITHUB_TOKEN` (paste a PAT with `contents:write` scope)
4. Add `PROXY_URL` as a GitHub repo secret, e.g. `https://auction-watch-proxy.<account>.workers.dev`

### Email favourites

Each lot in the email digest includes a heart link. Clicking it opens the website, auto-likes the lot (saved in localStorage), scrolls to it, and briefly highlights it.

## Local setup

```sh
# Install dependencies
uv sync

# Install Playwright browser (needed by a few scrapers)
uv run playwright install chromium --with-deps

# Copy and fill in env vars
cp .env.example .env

# Run (writes last_digest.html and _site/index.html, sends email if configured)
uv run auction-watch
```

## Scrapers

| House | Method |
|---|---|
| Artsy | GraphQL (CDN) |
| Sotheby's | Algolia API |
| Christie's | Direct API |
| Phillips | Direct API |
| Bonhams, Skinner, Cornette de Saint Cyr | `__NEXT_DATA__` |
| Rago, Wright, LA Modern, Landry Pop, Poster Auctions | Inertia.js |
| Invaluable | Algolia API |
| Drouot | neoGingo REST API |
| Julien's | Supabase PostgREST |
| Ketterer | HTML scrape |
| Lempertz | HTML scrape (artist-index pages) |
| Van Ham | HTML scrape |
| Catawiki | Playwright browser scrape |
| Dorotheum | Stub (Cloudflare blocks) |
| 1stDibs | Stub (buy-now marketplace) |
