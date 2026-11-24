# aptfinder

Local apartment finder: gathers listings through Apify actors into an append-only SQLite log, computes commutes, filters by goals, and shows a map dashboard.

```sh
uv sync --extra intake
cp .env.example .env    # add APIFY_TOKEN, GOOGLE_MAPS_API_KEY
cp -r config.example config  # your own goals, targets and sites; config/ is gitignored
uv run aptf-intake coverage   # what's left to scrape
uv run aptf-web               # dashboard at http://127.0.0.1:8765
```

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
