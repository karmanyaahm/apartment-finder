# aptfinder

Local apartment finder: gathers listings through Apify actors into an append-only SQLite log, computes commutes, filters by goals, and shows a map dashboard.

```sh
uv sync --extra intake
cp .env.example .env    # add APIFY_TOKEN, GOOGLE_MAPS_API_KEY
uv run aptf-intake coverage   # what's left to scrape
uv run aptf-web               # dashboard at http://127.0.0.1:8765
```

Other commands: `aptf-db`, `aptf-identity`, `aptf-commute`, `aptf-filter`, `aptf-verify`, `aptf-eval`. Goals and search areas live in `config/`. See [design.md](design.md) for the architecture.

## License

MIT, see [LICENSE](LICENSE).
