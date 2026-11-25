# Apartment finder: status, 2026-11-24

> Historical snapshot of 2026-11-24, kept as written. Current state: [README.md](README.md) and [design.md](design.md) ('Still open' and the decision log).

`design.md` is the full reference: architecture, schema and a dated decision log. This page is the short version: how it works, what changed today, what's blocked, and what's next.

Dashboard (real data): http://127.0.0.1:8766

## Where things stand

| | |
|---|---|
| Listings | 2,633 (all active) in 944 places; 85 places have more than one source |
| Area being worked | Mountain View chunk (Mountain View + San Antonio station walk zones) |
| Lease goal, Mountain View | 20 pass · 9 needs check · 279 fail |
| Temp goal, Mountain View | 4 pass · 3 needs check · 301 fail |
| Coverage ledger | 23 area-source pairs done, 56 left (`aptf-intake coverage`) |
| Apify | $4.42 left (your top-up has arrived); resets Dec 23 |
| Google Routes | 477 of the 5,000-call monthly cap, inside the free tier ($0) |
| Code | 39 commits, 86 tests passing, real-data browser check passing |

Most of Mountain View fails the temp goal on the commute to downtown Palo Alto. The temp goal needs Palo Alto and the Palo Alto box.

## How it works (one screen)

```
 Apify actors ──► intake ──► observations (append-only log, one row per listing per run)
 (Zillow, Apartments.com,      │
  Craigslist, Airbnb,          ▼
  Furnished Finder,        identity ──► links (which listings are the same place/unit)
  community websites)          │
                               ▼
 Google Routes / estimate ─► trips (commute times, metered)
                               │
 config/goals.toml ────────► filter (computed on read: pass / needs_check / fail, with reasons)
                               │
                               ▼
                           dashboard (map, cards, floor plans, photos, verify queue)
```

- **Logs are the truth; entities are views.** Runs, observations, links, trips, checks and annotations are only ever appended. Places, units, listings and operators are SQL views over them.
- **Gather wide, filter late.** Intake never narrows by goal. Changing a goal needs no new scrape.
- **Unknown never rejects.** A missing field gives "needs check" and names the field.
- **Everything from listing sites goes through Apify.** Nothing on this machine fetches a listing page.
- **Area ledger.** The search region is the ZIPs, cities and station boxes the station walk zones touch. Each area × source is done, partial or TODO, and `aptf-intake run <source>` picks up whatever is left of that source's areas. Spend caps: $1 per run, $0.30 Apify reserve, Google monthly caps.

## What changed today

1. **Temp goal requires a short-term stay** (`term = short`). Temp needs-check in Mountain View went from 26 to 3. Craigslist assumes a lease when a post doesn't state its rent period. No furnished Craigslist listing is affected today, but note it.

2. **Building photos come from each building's own website, not Apartments.com.** Apartments.com blocks its images from being shown elsewhere. A new `photos` platform reads every image on a community's page and drops logos, icons, social and stock images, videos and thumbnails. The browser loads the photos straight from the building's site. Live for Sevens, The Village Residences, Verve, Birch Creek and Fayette Arms (4 to 10 photos each, all load). The gallery shows a building's own photos first. These photo-only rows (`kind = site_info`) are not listings: goal results, plan tables, the verify queue and listing counts ignore them.

3. **Airbnb box searches.** Each box can now name its own city in the search URL (`slug` in `config/areas.toml`).
   - The Mountain View box went from 1 of 80 results inside the box to 13 of 20 in a trial.
   - A full run returned 137 results, 33 inside the box, with 25 new listings.
   - Airbnb's map search returns a ranked selection, not everything: that run missed 18 of the 51 listings known to be in the box. So Airbnb boxes now never count as complete, and missing from a run no longer retires an Airbnb listing.

4. **`aptf-intake discover-sites`: finds building websites automatically.** Built and tested, not yet run on real buildings.
   - Runs a local headless Claude Code (`claude -p`) whose only tool is web search. It proposes each building's own site, or none.
   - Every proposal is then fetched through Apify and accepted only if the page shows the building's street number and street name. A wrong site is rejected and logged, never attached.
   - Known operators get their full parser. AvalonBay sites (e.g. eaves, Avalon Towers) give every unit with prices; other sites give photos.
   - Results are appended to `config/discovered_sites.toml`: found, miss and check entries. Delete a found entry to drop that site.
   - Each Claude call is logged with its cost. It is trimmed to about $0.02–0.03 per building; the untrimmed default cost about $0.25.
   - A call that can't be trusted (not logged in, search denied, or no search made) stops the command as a blocker.
   - The dry run lists about 40 Mountain View buildings, estimated at $0.60 of Claude usage.

## Blockers

| Blocker | What failed | What would unblock it |
|---|---|---|
| Prometheus (The Dean, Park Place) and Landsby prices | Prices and availability load with JavaScript after the page; the cheerio scraper sees plans and sizes only | A browser-rendering Apify actor (e.g. puppeteer/playwright scraper), a few cents per page |
| Apartments.com photos | Their image server refuses to show images on other sites (`ERR_BLOCKED_BY_ORB`) | Now mostly bypassed by own-site photos (item 2); `discover-sites` extends it |
| Building websites | Corrected later on 2026-11-24: memo23 does return the property website (`moreDetails.propertyWebsiteUri`); 65 buildings now have one | Turn those URLs into scraped sites; `discover-sites` for the rest |
| Airbnb completeness | Map search is ranked; a box can't be proven complete | Accepted: reruns refresh. Smaller boxes would get closer |
| Google bike routing SKU | Unconfirmed whether BICYCLE bills as Essentials or Pro | Check in Google Cloud billing; bike calls capped at 900/month until then |
| Google bus stop list | Not provided yet | Your list of Google shuttle stops (`config/points/google_bus_stops.csv`) |

## What's left

- **Mountain View chunk:**
  - Craigslist for 94040, 94041, 94043 and 94306 (partial: earlier runs hit caps).
  - Airbnb San Antonio box.
  - Website discovery for about 40 buildings.
- **Every other chunk is TODO:**
  - Palo Alto (incl. California Ave, Menlo Park): the important one for the temp goal.
  - Redwood City, Sunnyvale, Lawrence.
- **Proposals, not done:**
  - A plan-level conflict check (two sources disagreeing on one floor plan).
  - Spot-check the Furnished Finder location-based merges.
  - A browser-rendering actor for the Prometheus and Landsby prices.

## Decisions for you

1. **Run `discover-sites` on Mountain View.** About $0.60 of Claude usage plus about $0.02 Apify. I'd trial 5 buildings first, then the rest.
2. **Next chunk with the new credit.** I'd do Palo Alto for both goals: Zillow, Apartments.com, Craigslist, Furnished Finder and the Airbnb boxes there. That's roughly $2–3 at past rates.
3. **Browser-rendering actor for the Prometheus and Landsby prices**, or wait?
