# Apartment finder: design

Living document. Source of truth for architecture. Any change to behaviour, schema, config shape or a decision updates this file and appends to the [decision log](#decision-log) in the same commit (see `AGENTS.md`).

## Context

A personal tool that runs locally (SQLite plus files on disk, web app on `127.0.0.1`, later exposed through a Cloudflare tunnel). The search has two goals that stay separate but share one global candidate list. The main output is a map dashboard that takes manual comments, ratings and verification checks.

## Goals (`config/goals.toml`)

| | Goal 1: `temp` | Goal 2: `lease` |
|---|---|---|
| What | Airbnb, Furnished Finder, Blueground or similar furnished stay for one person | Apartment or house for a group |
| Sizes | private room is fine; bathroom must be private | 3bd with 2 or more baths (2, 2.5, 3, ...), or 4bd with 3 or more baths. 4bd/2.5ba does not qualify |
| Dates | Dec 1, 2026 to Jan 1, 2027 | from about Jan 1, 2027; availability ignored for now |
| Hard filters | at most 8 min bike, or at most 25 min walk or transit, to downtown Palo Alto; private bathroom; furnished | under $2,500 per bedroom; long-term lease |
| Nice to have | none | at or under $2,000 per bedroom; gym; walk to Caltrain 20 min or less; Google bus stop nearby (list supplied later); living room size or total sqft |

## Principle: gather wide, filter late

- **Ingest takes everything in the area**, whatever its beds, baths or price. Goals are filters over stored data, so changing a threshold or adding a goal needs no new scrape.
- **Everything is timestamped and kept.** Each fact carries when it was observed and which source and run produced it. Nothing is overwritten or deleted.
- **Unknown never rejects.** Any filter whose input is missing passes the place with `needs_check` and names the missing field.
- **Filters report, they do not drop.** A failing place stays in the database and on the map (dimmed) with its fail reasons.
- **Paid routing is the one lazy step.** Straight-line distance is computed for everything for free. Google routing runs first for places passing a loose straight-line prefilter, and on demand for any other place from the dashboard.

Search area (`config/area.toml`): every city along Caltrain from Redwood City to Sunnyvale: Redwood City, North Fair Oaks, Atherton, Menlo Park, Palo Alto (including Stanford), Los Altos, Mountain View, Sunnyvale.

## Storage: logs are the truth, entities are derived

One SQLite file `data/apartments.db`, plus `data/raw/` (untouched scraper output, one JSON file per run and source) and `data/media/` (mirrored photos and floor plans named by content hash, because listing image URLs expire). `data/` is gitignored. Seed/demo data lives in a separate file, `data/seed.db`, so it never mixes with real observations.

### Append-only logs

Triggers reject UPDATE and DELETE on every log table. The only exception: a `runs` row may be updated once to close it out (`finished_at`, `counts`, `est_cost`, `status`, `error`) while its `finished_at` is still NULL; its identifying columns can never change.

- `runs`: id, started_at, finished_at, component, source, `params` JSON, `counts` JSON, est_cost, status, error.
- `observations`: id, run_id, observed_at, source, source_id, url, content_hash, price, price_basis (`month`, `night`, `week`, `stay`), query_checkin, query_checkout, `data` JSON, raw_path. **One row per listing per run, always**, so "seen in run N" is a fact in the table and a taken-down listing is distinguishable from an unchanged one. `content_hash` is the hash of the normalized parsed fields plus query dates (not the raw payload, whose image URLs expire); history views collapse consecutive rows with the same hash.
- `links`: id, ts, author (rule name or `user`), source, source_id, place_key, unit_key, reason. "This listing is this place." The current link for a listing is the newest `user` row if any, else the newest rule row; identity appends a rule row only when its decision differs from the current rule link.
- `trips`: id, computed_at, place_key, point_id (`set:name`), mode, minutes, distance_m, `legs` JSON, depart_assumption, provider.
- `checks`: id, ts, author, queue (`sample` or `conflict`), observation_id or trip_id, field, stored_value (JSON), verdict (`correct`, `wrong`, `cannot_tell`), true_value (JSON), note. A `wrong` verdict's `true_value` overrides the scraped value in the derived views **only while the listing's latest observed value still equals `stored_value`**; once the source changes the value, the correction lapses and the new value is shown.
- `annotations`: id, ts, author, entity_type, entity_key, attribute (free text: `gym`, `walk_to_caltrain`, `living_room`, `overall`, `status`, ...), goal_id (optional), rating (1 to 5, optional), comment. The latest row per entity, attribute and author is the current value. Only the web app writes here.

### `observations.data` JSON

Parsed fields, all optional: `address`, `unit`, `city`, `zip`, `lat`, `lon`, `beds` (0 = studio; a private room is `beds = 1` with `kind = room`), `baths`, `sqft`, `kind` (`apartment`, `house`, `townhome`, `room`, `entire_place`), `term` (`long` or `short`), `furnished`, `private_bath`, `available_from`, `available_to`, `amenities` (list), `floor_plan`, `property_name`, `operator`, `title`, `description`, `media` (list of `data/media/` paths), `extra` (anything source-specific).

### Derived views (never written)

`latest_obs`, `current_links`, `listings`, `units`, `places`, `operators`, `field_overrides` and `obs_history` are SQL views over the logs, created by `store.init_db`. They are views, not tables, so there is no second copy to drift. If they get slow, they become materialized tables rebuilt by one `derive` command with no change to readers.

Goal outcomes are not stored at all: `filter.evaluate` computes them at read time from the views, `trips` and `config/goals.toml`, so changing a threshold takes effect on the next page load.

Keys are deterministic so annotations and trips survive any rebuild: `place_key` is a hash of the normalized street address (or of lat/lon rounded to 4 decimals when there is no address), `unit_key` adds the normalized unit or floor plan label, and a listing is `(source, source_id)`.

### Config (human-edited, read at startup)

- `config/goals.toml`: goal definitions as a list of typed rules per goal (hard rules and nice-to-haves), plus verification thresholds.
- `config/area.toml`: cities and bounding box.
- `config/points/*.csv`: one file per point set, columns `set,name,lat,lon` plus free columns. The single concept for anything a place can be near or travel to:
  - `commute_targets.csv`: Googleplex, Meta HQ, Downtown Palo Alto, Rengstorff industrial complex.
  - `caltrain_stations.csv`: extra columns `service` (best tier that stops there) and `other_transit`.
  - `grocery_chains.csv` (Walmart, Safeway, Whole Foods, Trader Joe's) and `grocery_any.csv` (every supermarket and grocery), generated once from OpenStreetMap by `scripts/osm_groceries.py` and committed.
  - `google_bus_stops.csv`: empty until the user supplies the list.
- `config/point_sets.toml`: per set, which modes to compute and how many nearest points to route to. "Proximity" is a query (the minimum walk `trips` row for a set), not a table.
- `config/caltrain.toml`: station-to-station ride times per service pattern and headways, used by the commute module's train legs.

## Components

All code lives in one Python package `aptfinder` (`src/aptfinder/`), one subpackage per component, each with its own CLI entry point (`aptf-<component>`) and tests under `tests/`.

### store (`aptf-db`)
- Goal: own the schema: log tables, append-only triggers, derived views; append helpers.
- Inputs: a database path.
- Outputs: an initialized SQLite file; `append_*` functions; `seed` command writing demo observations to `data/seed.db`.
- Must not: update or delete log rows; hold business rules beyond key derivation and view definitions.

### intake (`aptf-intake`)
- Goal: bring listings into `observations`, as wide as possible.
- Inputs: `config/area.toml`, source adapter configs, `APIFY_TOKEN`, spend cap.
- Outputs: `runs` rows, one `observations` row per listing per run, raw files under `data/raw/`, mirrored media under `data/media/`.
- Must not: filter by goal; scrape any listing site directly from this machine (Apify only); contact anyone; quietly accept empty or suspicious output (stop and ask the user).
- Tiers: aggregators discover places; direct enrichment reads a known place's own website (through an Apify web-scraping actor) for availability, per-unit pricing and floor plans, stored with `source = direct`; portfolio discovery follows an operator's "other properties" page. When direct and aggregator data disagree both are kept and direct is shown as current.
- No actor is assumed to work: each is chosen by running it for the area and comparing with the benchmark and manual checks. After each new source's first run, the user works a sample queue for it before it is relied on.
- Untested leads carried over from the original brainstorm (`handoff.md`, Apify store listings, never run):

  | Source | Actor to try | Listed price | Caveats noted then |
  |---|---|---|---|
  | Zillow, Apartments.com, Realtor.com, Zumper, Redfin | `tri_angle/real-estate-aggregator` | from $1.50 / 1k | one call, deduped output; verify rental coverage per provider |
  | Property-manager sites (RentCafe, Entrata, RealPage, AppFolio, Buildium) | `housingfeed/rental-listings-api` | unknown | its pages disagree on whether Zillow and Apartments.com are included; low usage |
  | Craigslist (sfbay) | `lentic_clockss/craigslist-housing-scraper` | $2 / 1k | |
  | Airbnb | `tri_angle/airbnb-scraper` | $1.25 / 1k | Apify-maintained; had an empty-price bug; fallback `tri_angle/airbnb-fast-scraper` |
  | Furnished Finder | `rigelbytes/furnishedfinder` or `datascrapers/furnishedfinder` | about $1.20 / 1k | community-built; site behind Cloudflare, may need residential proxy |

  For Airbnb, query with real check-in and check-out dates at least 28 days apart so the returned price is the monthly-discounted total (`price_basis = stay`, query dates recorded on the observation). Blueground has no known actor yet.

### identity (`aptf-identity`)
- Goal: decide which listings are the same place and unit.
- Inputs: `latest_obs`, `current_links`.
- Outputs: appended `links` rows (rules: `addr_unit` = normalized address plus unit; `geo_beds_price` = lat/lon rounded to 4 decimals plus beds plus price, when there is no address).
- Must not: overrule a `user` link; append a row that repeats the current decision.

### commute (`aptf-commute`)
- Goal: minutes and legs from coordinate A to coordinate B by a mode. Fully independent: no imports from the rest of `aptfinder`, no knowledge of apartments, goals or the main schema.
- Inputs: two `(lat, lon)` pairs, a mode, a `LegSource`, a SQLite path for its cache, station list and train table (passed in by the caller).
- Outputs: `Trip(minutes, distance_m, legs, provider, approximate)`.
- `LegSource` answers "minutes from A to B by walk, bike, drive or transit". Implementations: `GoogleRoutesLegSource` (Routes API, `GOOGLE_MAPS_API_KEY`), `StraightLineLegSource` (haversine times a detour factor at a fixed speed; free, labelled `estimate`), and a fake for tests.
- Cache: the module owns one table, `commute_legs`, in the database path it is given, keyed on origin and destination rounded to 4 decimals, mode, departure assumption and provider. Every single leg goes through it; composed modes are built from cached legs, so station-to-workplace legs are fetched once ever and each new apartment costs only its own legs. Callers never manage cache state.
- Modes: `walk`, `bike`, `drive` (no traffic), `transit`, `bike_train_bike` (bike to each of the nearest k stations, in-train time from `config/caltrain.toml` plus half the headway as wait, bike from the far station; fastest kept), `bike_bus_bike` (Google transit route with walking legs re-timed as bike legs; labelled approximate). Both composed modes also consider biking the whole way, so they are never slower than plain bike.
- `nearest(origin, points, mode, k)`: shortlist by straight line, route to the top k.
- Must not: read or write any table but `commute_legs`.

### filter (`aptf-filter`)
- Goal: evaluate each goal over the derived views and `trips`.
- Inputs: `listings`, `places`, `trips`, `config/goals.toml`, per-source accuracy from verify.
- Outputs: per listing and goal, `pass`, `fail` (with reasons) or `needs_check` (naming the missing or untrusted field). A place's outcome is the best outcome among its listings.
- Must not: write to the database; drop anything.

### verify (`aptf-verify`)
- Goal: measure data accuracy instead of assuming it.
- Sample queue: seeded random sample drawn evenly across sources (round robin, not proportional to volume); computed trips sampled the same way by provider and mode.
- Conflict queue: units with more than one source disagreeing on price, beds, baths, sqft or availability. Shown first.
- Outputs: `checks` rows from the dashboard; accuracy per source and field with sample size and a Wilson 95% interval; cross-source agreement rate.
- A source whose measured accuracy on a field is below the threshold in `goals.toml` (with at least the minimum sample) gets that field treated as `needs_check` by the filter.

### web (`aptf-web`)
- Goal: the dashboard. FastAPI bound to `127.0.0.1`, JSON API plus one static page (plain JavaScript and Leaflet, no build step).
- Map: Esri World Imagery satellite with a label overlay, toggle to OpenStreetMap. A service worker caches tiles cache-first. Google tiles are not used (their terms forbid caching).
- Goal switcher (`temp`, `lease`, all); pins coloured pass / fail (dimmed) / needs-check; point sets as toggleable layers; station pins show service tier and other transit.
- Card list sortable and filterable by price, price per bed, beds/baths, minutes to a chosen target and mode, rating, status.
- Detail drawer: photos and floor plans, price and availability history, commute matrix, nearest points per set, all source URLs, raw JSON, per-attribute comments and 1 to 5 ratings (append to `annotations`), each fact's source and hand-check state, on-demand routing.
- Status strip from `runs` and `checks`.
- Verify mode: conflict queue then sample queue, one listing at a time, fields side by side across sources, correct / wrong / cannot tell per field.
- Must not: be exposed beyond localhost without Cloudflare Access or equivalent (it has write endpoints).

### eval (`aptf-eval`)
- Goal: the discovery benchmark for goal 2.
- Inputs: `eval/benchmark.csv` (copy of the user's sheet, snapshot of Nov 23, 2026), `places`.
- Outputs: `aptf-eval recall` prints found and missed per tier (Zillow listings, community sites, ruled out). A miss is reported with a reason: taken down since the snapshot (not counted) or not found (counted); the user decides when unclear. The sheet's "Walk to Caltrain" and "Drive to Meta HQ" columns also check the commute module (within about 2 minutes).

No LLM step is planned. If free-text parsing needs a model (direct property sites are the likely case), it is proposed to the user then, with accuracy measured through `checks`.

## Stack

Python 3.12 (pinned through `uv`; the system Python is 3.10), `apify-client`, FastAPI plus uvicorn, `sqlite3` from the standard library, `pydantic` for row and config shapes, `httpx` for Google Routes, `pytest`. Front end: plain JavaScript and Leaflet from a CDN, no build step. Secrets in a gitignored `.env`. A spend cap per run, with estimated cost logged in `runs`.

## Order of work

1. Repo setup and this document.
2. Skeleton, log schema with triggers, derived views, config loaders, CLI entry points.
3. Point set files: commute targets, Caltrain stations (user confirms), grocery sets from OpenStreetMap.
4. Commute module with tests, then a hand check against Google Maps and the benchmark columns.
5. Identity and filter, with `needs_check`.
6. Dashboard, including verify mode, on seeded example observations (with a deliberate cross-source conflict).
7. Stop and show the user the dashboard.
8. Intake: Airbnb, Furnished Finder and Blueground first, then lease aggregators, direct enrichment, portfolio discovery. User works a sample queue per new source. Run `aptf-eval recall`.

## Still open

- Goal 1 budget; whether the last days need covering.
- Apify token and spend cap (step 8); Google Maps key (step 4 hand check).
- Google bus stop list.

## Decision log

Each entry: date, decision, why, what it replaced.

- **2026-11-23: Local SQLite plus files, no Google Sheet.** Everything runs on the user's PC; a sheet adds sync and quota problems. Replaced: the Sheet idea and `handoff.md`'s plain SQLite-with-upserts model.
- **2026-11-23: Logs are the truth, entities are derived views.** Append-only logs keep every observation and decision with its time and author; views mean no second copy to drift. Replaced: mutable `listings` table with `price_history` side table.
- **2026-11-23: Gather wide, filter late; unknown never rejects; filters report, not drop.** Scraping is the expensive step, so it must not depend on thresholds that change. Replaced: SQL hard filters before ranking.
- **2026-11-23: Two goals (`temp`, `lease`) over one candidate list.** The search split into a furnished stay for one person and a group lease. Replaced: single criteria file.
- **2026-11-23: Lease size rule is 3bd with 2+ baths or 4bd with 3+ baths.** Confirmed with the user; matches the benchmark, which counts 3bd/2.5ba. Replaced: "4bd with 2.5+ baths".
- **2026-11-23: One point-set concept** (CSV per set) for commute targets, stations, groceries and bus stops; proximity is a query over `trips`. Replaced: hardcoded station list with a walking threshold.
- **2026-11-23: Commute is an independent module owning its leg cache.** Lets composed modes share legs (station-to-office legs fetched once) and keeps it testable alone.
- **2026-11-23: Google Routes API for routing, behind `LegSource`; straight-line estimate as the free fallback.** Paid routing is lazy; the estimate gives every place a labelled rough number.
- **2026-11-23: Satellite tiles from Esri, cached by a service worker; no Google tiles.** Google's terms forbid caching.
- **2026-11-23: One full observation row per listing per run.** User's choice; makes liveness ("still listed") a plain fact for recall and the status strip. `content_hash` covers normalized parsed fields plus query dates, not raw payloads. Replaced: write only on content change.
- **2026-11-23: All listing sources go through Apify, property websites included.** User's choice; nothing scrapes a listing site from this machine. Replaced: `handoff.md`'s ban on direct Zillow/Apartments.com/Realtor.com only.
- **2026-11-23: `runs` rows may be closed out once.** A run must be visible while in progress and survive a crash; a single UPDATE while `finished_at IS NULL` is allowed, enforced by trigger. Replaced: blanket no-UPDATE on every log table.
- **2026-11-23: A `wrong` check overrides only while the source still shows the checked value.** Otherwise a stale correction would mask a real price change.
- **2026-11-23: User links beat rule links regardless of age; identity appends only on change.** Re-running identity must never undo a manual merge or split, and the log should not grow on every rerun.
- **2026-11-23: Goal outcomes computed at read time, not stored.** Thresholds live in TOML, which a SQL view cannot read; computing on read keeps "no second copy". Replaced: `goal_matches` as a view.
- **2026-11-23: Seed data in `data/seed.db`.** Demo observations must never mix with real ones.
- **2026-11-23: No LLM step for now.** Deterministic parsing first; a model is proposed only if free-text parsing demands it, measured through `checks`. Replaced: LLM extraction and ranking from `handoff.md`.
- **2026-11-23: Manual verification built in from the first run** (sample and conflict queues, accuracy with intervals). Confidence in scraped data is measured, not assumed.
- **2026-11-23: `handoff.md` deleted, replaced by this file.** Its candidate actor table and the Airbnb 28-day tip were moved into the intake section as untested leads.
- **2026-11-23: Python 3.12 via `uv`, FastAPI, plain JS plus Leaflet.** One language for every component; no front-end build step.
