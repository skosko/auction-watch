# Scraper Status

Last updated: 2026-05-31

| Scraper | Source | Status | Notes |
|---|---|---|---|
| Artsy | artsy.net | ✅ Working | GraphQL API; ~50 lots/run |
| Rago | ragoarts.com | ✅ Working | Inertia.js scrape; also covers Wright, LA Modern Auctions, Landry Pop, Poster Auctions International |
| Sotheby's | sothebys.com | ✅ Working | Direct API; ~20 lots/run |
| Christie's | christies.com | ✅ Working | Direct API; typically few lots/run |
| Phillips | phillips.com | ✅ Working | Direct API; ~8 lots/run |
| Invaluable | invaluable.com | ✅ Working | Direct Algolia API (app: 0HJBNDV358, index: upcoming_lots_prod); ~1000+ lots/run |
| Drouot | drouot.com | ✅ Working | neoGingo REST API; ~180 lots/run |
| Van Ham | auction.van-ham.com | ✅ Working | HTML scrape; two lot types: online (expiry from `product:expiration_time` meta) and live auction (date from `time_of_event` span) |
| Ketterer | ketterer-kunst.de | ⚠️ Working | HTML scrape; returns 0 when no matching lots are upcoming |
| Lempertz | lempertz.com | ✅ Working | HTML scrape of artist-index pages (`/en/catalogues/artist-index/detail/lastname-firstname.html`); lot details via JSON-LD; images from `lempertz_api/images/large/` (not hotlink-protected) |
| Juliens | juliensauctions.com | ⚠️ Working | Supabase PostgREST API; returns 0 when no matching lots (entertainment/pop culture focus) |
| Bonhams | bonhams.com | ❌ Blocked | Cloudflare IP reputation blocks GitHub Actions (AWS) IPs; works locally |
| Dorotheum | dorotheum.com | ❌ Blocked | Cloudflare Bot Management blocks all requests from datacenter IPs; times out after 5 min |
| 1stDibs | 1stdibs.com | ❌ Blocked | Cloudflare blocks GitHub Actions IPs even with Playwright; times out after 5 min. Note: buy-now marketplace, no auction close dates |

## Notes

**Cloudflare-blocked scrapers** (Bonhams, Dorotheum, 1stDibs) work fine when run locally from a residential IP. Fixing them in CI would require routing through a residential proxy service.

**Cross-source deduplication** runs in four passes:
1. Exact URL dedup
2. Artsy mirror dedup — drops Artsy lots when a direct-house lot matches on (artist, date, title prefix)
3. Invaluable dedup — drops Invaluable lots when a direct-house lot matches on (artist, date) for known aggregated houses (Van Ham, Bonhams, Drouot, Phillips, Rago, Wright, Julien's, Lempertz)
4. Drouot dedup — drops Drouot lots for houses that also publish directly (e.g. Van Ham cross-lists on both platforms)

**Per-scraper timeout**: Each scraper is wrapped in a 300-second timeout. If a scraper hangs (e.g. due to a blocked browser session), it is cancelled and logged as an error rather than blocking the whole job.
