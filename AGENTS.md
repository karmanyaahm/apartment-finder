# Working rules for coding agents in this repo

1. **`design.md` is the source of truth** for architecture, schema, config shapes and component boundaries. Read it before changing anything.
2. **Design changes travel with code.** Any change to a component's behaviour, a table or view, a config file's shape, or a design decision updates `design.md` *and* appends a dated entry to its decision log (date, decision, why, what it replaced) in the same commit as the code.
3. **Log tables are append-only.** `runs`, `observations`, `links`, `trips`, `checks`, `annotations` are only ever INSERTed into (the one exception: closing out an open `runs` row, see `design.md`). Triggers enforce this; never drop or weaken them. Entities (`places`, `units`, `listings`, `operators`) are derived views. Never add a table that holds a second copy of a fact.
4. **Gather wide, filter late.** Intake never narrows collection by a goal's criteria. Goals are filters over stored data.
5. **Unknown never rejects.** A filter whose input is missing returns `needs_check` and names the missing field. Filters report, they never delete.
6. **No direct scraping of listing sites from this machine.** Every listing source, including property and operator websites, goes through Apify actors. Non-listing reference data (OpenStreetMap, Caltrain timetable) may be fetched directly.
7. **Never contact a landlord**, submit a form, apply, or send a message on the user's behalf.
8. **If a source returns bad, empty or suspicious data, stop and tell the user.** Do not work around it quietly.
9. **Secrets** (`APIFY_TOKEN`, `GOOGLE_MAPS_API_KEY`) come from `.env`, never from code or commits. Respect the per-run spend cap.
10. **The `commute` package stays independent:** no imports from the rest of the project, no knowledge of apartments, goals or the main schema.
11. Run `env -u PYTHONPATH uv run pytest` before committing (this machine's ROS `PYTHONPATH` otherwise injects broken pytest plugins). Commit at each step of the order of work in `design.md`.
