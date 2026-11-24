# aptfinder

Local apartment finder: gathers listings through Apify actors into an append-only SQLite log, computes commutes, filters by goals, and shows a map dashboard.

![Map of Mountain View with every place colored by goal result, and cards sorted by status](docs/screenshots/1-overview.jpg)

## What it does

- **Gathers wide, filters late.** Zillow, Apartments.com, Craigslist, Airbnb, Furnished Finder and buildings' own websites, all through Apify actors, area by area (Caltrain station walk zones) with a coverage ledger of what's done and what's left. Goals never narrow what gets collected.
- **One building, every source.** Listings from different sites are matched to the same place and unit, so a building shows its own website's floor plans and units next to the aggregator listings, with photos from all of them.
- **Goals are filters you can change any time.** Each listing gets pass, fail (with reasons) or needs check (naming the missing field). Unknown never rejects.
- **Real commutes.** Walk, bike, transit, drive, bike + Caltrain + bike and bike + bus + bike to your targets, with free straight-line estimates for everything and metered Google routing only where a goal needs it.
- **Measures its own accuracy.** A verify queue samples listings evenly across sources and flags fields where sources disagree, so you know which source to trust for which field.
- **Append-only history.** Every scrape is kept; price history and changes come for free.

| | |
|---|---|
| ![Building drawer: goal reasons, photos and floor plans with prices](docs/screenshots/2-building.jpg) | ![Floor plans with unit counts, furnished leases and price history](docs/screenshots/3-units.jpg) |
| Building drawer: why it passes or fails, photos from every source, floor plans | Floor plans from the building's own site, furnished leases, price history |
| ![Commute table and nearby Caltrain and groceries](docs/screenshots/4-commute.jpg) | ![Temp-stay goal showing furnished stays that pass or need a check](docs/screenshots/5-temp-goal.jpg) |
| Commute to every target by every mode, nearest stations and groceries, your ratings | A second goal (furnished temporary stay) over the same data |

![Verify queue: two sources disagree on the number of baths](docs/screenshots/6-verify.jpg)

Screenshots are real data, shown with `aptf-web --redact` (below).

## Everything it considers

| | |
|---|---|
| **Listing** | price and what it covers (month, night, stay), price per month and per bedroom, beds (studio = 0), baths, sqft, kind (apartment, house, townhome, room, entire place), term (long lease or short stay), furnished, private bath, available from / to, amenities, floor plan, property name, operator, title, description, photos, source URL, first and last seen, active or gone |
| **Place and unit** | address, unit, city, ZIP, coordinates; which listings are the same building and the same unit; the building's own website and its floor plans |
| **Commute** | targets in `config/points/commute_targets.csv`; modes walk, bike, transit, drive (no traffic), bike + Caltrain + bike, bike + bus + bike; straight-line estimate or Google route |
| **Nearby** | nearest Caltrain stations, grocery stores (any, or chains), shuttle bus stops: any point set in `config/points/` |
| **Goal rules** | `equals` (field = value), `max` (field at most a value), `size_options` (beds with a minimum of baths), `dates_cover` (available for a date range), `commute_any` (any mode within N minutes of a target), `near` (nearest point of a set within N minutes), `amenity`, `known` (field present); each rule is either required or nice to have |
| **Trust** | per-source accuracy per field from hand checks; a field below the threshold counts as unknown (needs check) |
| **You** | ratings and comments per place on any attribute (overall, gym, living room, ...), plus hand checks of listing fields |
| **Search area** | cities, a bounding box, and Caltrain walk-zone chunks in `config/areas.toml` |
| **Spend** | Apify cost per run and a monthly reserve; Google Routes calls per run and per month |

Goal examples are in [config.example/goals.toml](config.example/goals.toml).

```sh
uv sync --extra intake
cp .env.example .env    # add APIFY_TOKEN, GOOGLE_MAPS_API_KEY
cp -r config.example config  # your own goals, targets and sites; config/ is gitignored
uv run aptf-intake coverage   # what's left to scrape
uv run aptf-web               # dashboard at http://127.0.0.1:8765
```

`aptf-web --redact` serves a read-only dashboard with the strings listed in `config/redact.toml` replaced in every API response (goal labels, private commute targets and their coordinates, search dates), for screenshots or demos of real data. `scripts/screenshots.py` takes the images above from it.

Other commands: `aptf-db`, `aptf-identity`, `aptf-commute`, `aptf-filter`, `aptf-verify`, `aptf-eval`. Goals, commute targets, search areas and community sites live in `config/`; without it the committed example in `config.example/` is used. Tests always use `config.example/`. See [design.md](design.md) for the architecture.

## Reference data (refresh by hand)

These files are generated once and committed in `config.example/`; nothing updates them automatically. Scripts write to your `config/`; copy a refreshed file into `config.example/` to commit it.

- **Grocery stores** (`config/points/grocery_any.csv`, `grocery_chains.csv`), from OpenStreetMap through the Overpass API (free, fetched directly: reference data, not a listing site). Re-run when the search area in `config/area.toml` changes, or every few months:
  ```sh
  env -u PYTHONPATH uv run python scripts/osm_groceries.py   # writes to config/points/
  ```
- **Caltrain timetable** (`config/caltrain.toml`: ride-time offsets and headways per service pattern), worked out by hand from the [Caltrain GTFS feed](https://data.trilliumtransit.com/gtfs/caltrain-ca-us/caltrain-ca-us.zip) of 13-Nov-2026. **The feed is valid to 2027-01-31**; after a timetable change, update the offsets and headways (and the `source` line) by hand. There is no script.
- **Caltrain stations** (`config/points/caltrain_stations.csv`) and **commute targets** (`config/points/commute_targets.csv`): hand-edited.
- **Google bus stops** (`config/points/google_bus_stops.csv`): empty until a stop list is added by hand.

## License

MIT, see [LICENSE](LICENSE).
