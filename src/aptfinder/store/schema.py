"""DDL: append-only log tables, triggers that enforce append-only, and derived views.

Views are dropped and recreated on every init so their definitions always match this file;
tables and triggers are created only if missing.
"""

from __future__ import annotations

from .models import CHECKABLE_FIELDS

LOG_TABLES = ["runs", "observations", "links", "trips", "checks", "annotations", "api_calls"]

TABLES = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    component   TEXT NOT NULL,
    source      TEXT,
    params      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(params)),
    counts      TEXT CHECK (counts IS NULL OR json_valid(counts)),
    est_cost    REAL,
    status      TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'ok', 'error', 'aborted')),
    error       TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES runs(id),
    observed_at    TEXT NOT NULL,
    source         TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    url            TEXT,
    content_hash   TEXT NOT NULL,
    price          REAL,
    price_basis    TEXT CHECK (price_basis IS NULL OR price_basis IN ('month', 'night', 'week', 'stay')),
    query_checkin  TEXT,
    query_checkout TEXT,
    data           TEXT NOT NULL CHECK (json_valid(data)),
    raw_path       TEXT
);
CREATE INDEX IF NOT EXISTS obs_listing ON observations(source, source_id, observed_at);
CREATE INDEX IF NOT EXISTS obs_run ON observations(run_id);

CREATE TABLE IF NOT EXISTS links (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    author    TEXT NOT NULL,
    source    TEXT NOT NULL,
    source_id TEXT NOT NULL,
    place_key TEXT NOT NULL,
    unit_key  TEXT NOT NULL,
    reason    TEXT
);
CREATE INDEX IF NOT EXISTS links_listing ON links(source, source_id);

CREATE TABLE IF NOT EXISTS trips (
    id                INTEGER PRIMARY KEY,
    computed_at       TEXT NOT NULL,
    place_key         TEXT NOT NULL,
    point_id          TEXT NOT NULL,
    mode              TEXT NOT NULL,
    minutes           REAL,
    distance_m        REAL,
    legs              TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(legs)),
    depart_assumption TEXT,
    provider          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trips_place ON trips(place_key, point_id, mode);

CREATE TABLE IF NOT EXISTS checks (
    id             INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL,
    author         TEXT NOT NULL,
    queue          TEXT NOT NULL CHECK (queue IN ('sample', 'conflict', 'adhoc')),
    observation_id INTEGER REFERENCES observations(id),
    trip_id        INTEGER REFERENCES trips(id),
    field          TEXT NOT NULL,
    stored_value   TEXT CHECK (stored_value IS NULL OR json_valid(stored_value)),
    verdict        TEXT NOT NULL CHECK (verdict IN ('correct', 'wrong', 'cannot_tell')),
    true_value     TEXT CHECK (true_value IS NULL OR json_valid(true_value)),
    note           TEXT,
    CHECK (observation_id IS NOT NULL OR trip_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS annotations (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    author      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_key  TEXT NOT NULL,
    attribute   TEXT NOT NULL,
    goal_id     TEXT,
    rating      INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
    comment     TEXT
);
CREATE INDEX IF NOT EXISTS ann_entity ON annotations(entity_type, entity_key);

-- Every paid API call (Google Routes), for metering against config/google.toml.
CREATE TABLE IF NOT EXISTS api_calls (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    provider TEXT NOT NULL,
    mode     TEXT,
    ok       INTEGER NOT NULL,
    note     TEXT
);
CREATE INDEX IF NOT EXISTS api_calls_ts ON api_calls(provider, ts);
"""


def _triggers() -> str:
    out = []
    for t in LOG_TABLES:
        out.append(
            f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} "
            f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
        )
        if t == "runs":
            # One close-out update is allowed while the run is open; identity columns never change.
            out.append(
                "CREATE TRIGGER IF NOT EXISTS runs_no_update BEFORE UPDATE ON runs "
                "WHEN OLD.finished_at IS NOT NULL OR NEW.id IS NOT OLD.id "
                "OR NEW.started_at IS NOT OLD.started_at OR NEW.component IS NOT OLD.component "
                "OR NEW.source IS NOT OLD.source OR NEW.params IS NOT OLD.params "
                "BEGIN SELECT RAISE(ABORT, 'runs rows can only be closed out once'); END;"
            )
        else:
            out.append(
                f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
                f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;"
            )
    return "\n".join(out)


def _field_expr(field: str, alias: str = "lo") -> str:
    if field == "price":
        return f"{alias}.price"
    return f"json_extract({alias}.data, '$.{field}')"


def _overridden(field: str) -> str:
    # `ov.o` is one JSON object of {field: true_value} per listing, joined once (see listings_base).
    # A per-field correlated subquery here took 73 s for 1,946 listings; the join takes well under 1 s.
    return f"COALESCE(json_extract(ov.o, '$.{field}'), {_field_expr(field)})"


PLAIN_DATA_FIELDS = ["city", "zip", "floor_plan", "property_name", "operator", "title"]

VIEW_NAMES = [
    "operators", "places", "units", "listings", "listings_base", "field_overrides", "current_checks",
    "obs_history", "current_links", "latest_obs", "listing_stats", "source_last_full_run",
]


def _views() -> str:
    current_value = (
        "CASE x.field WHEN 'price' THEN lo.price ELSE json_extract(lo.data, '$.' || x.field) END"
    )
    checked = ",\n        ".join(f"{_overridden(f)} AS {f}" for f in CHECKABLE_FIELDS if f != "price")
    plain = ",\n        ".join(f"{_field_expr(f)} AS {f}" for f in PLAIN_DATA_FIELDS)
    return f"""
CREATE VIEW latest_obs AS
SELECT * FROM (
    SELECT o.*, ROW_NUMBER() OVER (PARTITION BY source, source_id ORDER BY observed_at DESC, id DESC) AS rn
    FROM observations o
) WHERE rn = 1;

CREATE VIEW listing_stats AS
SELECT source, source_id, MIN(observed_at) AS first_seen, MAX(observed_at) AS last_seen,
       COUNT(*) AS n_obs, COUNT(DISTINCT content_hash) AS n_versions
FROM observations GROUP BY source, source_id;

-- Latest successful full-coverage run per source. A listing absent from it is no longer active.
-- Runs whose params.scope is 'targeted' (direct enrichment of one place) do not count.
CREATE VIEW source_last_full_run AS
SELECT source, MAX(id) AS run_id FROM runs
WHERE status = 'ok' AND component = 'intake' AND COALESCE(json_extract(params, '$.scope'), 'full') = 'full'
GROUP BY source;

-- The newest user link wins over any rule link; otherwise the newest rule link.
CREATE VIEW current_links AS
SELECT * FROM (
    SELECT l.*, ROW_NUMBER() OVER (
        PARTITION BY source, source_id ORDER BY (author = 'user') DESC, id DESC) AS rn
    FROM links l
) WHERE rn = 1;

-- Consecutive observations with the same content collapsed: the change history of each listing.
CREATE VIEW obs_history AS
SELECT * FROM (
    SELECT o.*, LAG(content_hash) OVER (PARTITION BY source, source_id ORDER BY observed_at, id) AS prev_hash
    FROM observations o
) WHERE prev_hash IS NULL OR prev_hash != content_hash;

-- The hand-check state of each listing and field, independent of which run's observation was checked:
-- the latest check per listing and field, valid only while the listing's latest observed value still
-- equals the value that was checked. A new run with the same value keeps it; a changed value drops it.
CREATE VIEW current_checks AS
SELECT x.source, x.source_id, x.field, x.verdict, x.stored_value, x.true_value, x.check_id,
       x.observation_id, x.queue, x.ts
FROM (
    SELECT o.source, o.source_id, c.field, c.verdict, c.stored_value, c.true_value, c.id AS check_id,
           c.observation_id, c.queue, c.ts,
           ROW_NUMBER() OVER (PARTITION BY o.source, o.source_id, c.field ORDER BY c.id DESC) AS rn
    FROM checks c JOIN observations o ON o.id = c.observation_id
) x
JOIN latest_obs lo ON lo.source = x.source AND lo.source_id = x.source_id
WHERE x.rn = 1 AND json_extract(x.stored_value, '$') IS {current_value};

-- A current 'wrong' check overrides the scraped value.
CREATE VIEW field_overrides AS
SELECT source, source_id, field, true_value, check_id FROM current_checks WHERE verdict = 'wrong';

CREATE VIEW listings_base AS
SELECT lo.source, lo.source_id, lo.url, lo.id AS obs_id, lo.run_id, lo.observed_at,
       ls.first_seen, ls.last_seen, ls.n_obs, ls.n_versions,
       -- inactive: absent from a newer whole-area run, or from a newer run that completed this listing's
       -- intake area (counts.areas.<id>.state). Partial or failed reruns prove nothing.
       CASE WHEN sl.run_id IS NOT NULL AND lo.run_id < sl.run_id THEN 0
            WHEN json_extract(lo.data, '$.extra.area') IS NOT NULL AND EXISTS (
                SELECT 1 FROM runs r
                WHERE r.source = lo.source AND r.component = 'intake' AND r.status = 'ok' AND r.id > lo.run_id
                  AND json_extract(r.counts, '$.areas."' || json_extract(lo.data, '$.extra.area') || '".state')
                      IN ('complete', 'empty')) THEN 0
            -- superseded by a later reparse of the same raw file that no longer produced this listing
            WHEN lo.raw_path IS NOT NULL AND EXISTS (
                SELECT 1 FROM runs r
                WHERE r.source = lo.source AND r.id > lo.run_id AND r.status = 'ok'
                  AND json_extract(r.params, '$.reparse_of') IS NOT NULL
                  AND lo.raw_path LIKE '%' || json_extract(r.params, '$.reparse_of')) THEN 0
            ELSE 1 END AS active,
       cl.place_key, cl.unit_key,
       {_overridden('price')} AS price,
       lo.price_basis, lo.query_checkin, lo.query_checkout,
       {checked},
       {plain},
       json_extract(lo.data, '$.amenities') AS amenities,
       json_extract(lo.data, '$.media') AS media,
       ov.fields AS overridden,
       lo.data AS data, lo.raw_path
FROM latest_obs lo
JOIN listing_stats ls ON ls.source = lo.source AND ls.source_id = lo.source_id
LEFT JOIN source_last_full_run sl ON sl.source = lo.source
LEFT JOIN current_links cl ON cl.source = lo.source AND cl.source_id = lo.source_id
LEFT JOIN (SELECT source, source_id, json_group_object(field, json(true_value)) AS o,
                  group_concat(field) AS fields
           FROM field_overrides GROUP BY source, source_id) ov
       ON ov.source = lo.source AND ov.source_id = lo.source_id;

CREATE VIEW listings AS
SELECT b.*,
       CASE b.price_basis
           WHEN 'month' THEN b.price
           WHEN 'night' THEN b.price * 30.4375
           WHEN 'week'  THEN b.price * 30.4375 / 7
           WHEN 'stay'  THEN b.price * 30.4375 /
                NULLIF(julianday(b.query_checkout) - julianday(b.query_checkin), 0)
       END AS price_month,
       CASE WHEN b.beds > 0 THEN
           (CASE b.price_basis
               WHEN 'month' THEN b.price
               WHEN 'night' THEN b.price * 30.4375
               WHEN 'week'  THEN b.price * 30.4375 / 7
               WHEN 'stay'  THEN b.price * 30.4375 /
                    NULLIF(julianday(b.query_checkout) - julianday(b.query_checkin), 0)
           END) / b.beds
       END AS price_per_bed
FROM listings_base b;

CREATE VIEW units AS
SELECT unit_key, place_key, COUNT(*) AS n_listings, SUM(active) AS n_active,
       group_concat(DISTINCT source) AS sources,
       MIN(price_month) AS min_price_month, MAX(beds) AS beds, MAX(baths) AS baths, MAX(sqft) AS sqft,
       MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen
FROM listings WHERE unit_key IS NOT NULL GROUP BY unit_key;

-- Each descriptive field comes from the best listing that has it: direct (the place's own site) first,
-- then the most recently observed. Window functions keep this one pass over `listings`.
CREATE VIEW places AS
WITH l AS MATERIALIZED (
    SELECT *,
        FIRST_VALUE(address) OVER (PARTITION BY place_key
            ORDER BY address IS NULL, source = 'direct' DESC, observed_at DESC) AS p_address,
        FIRST_VALUE(city) OVER (PARTITION BY place_key
            ORDER BY city IS NULL, source = 'direct' DESC, observed_at DESC) AS p_city,
        FIRST_VALUE(property_name) OVER (PARTITION BY place_key
            ORDER BY property_name IS NULL, source = 'direct' DESC, observed_at DESC) AS p_name,
        FIRST_VALUE(operator) OVER (PARTITION BY place_key
            ORDER BY operator IS NULL, source = 'direct' DESC, observed_at DESC) AS p_operator
    FROM listings WHERE place_key IS NOT NULL
)
SELECT place_key, MAX(p_address) AS address, MAX(p_city) AS city, MAX(p_name) AS property_name,
       MAX(p_operator) AS operator,
       AVG(lat) AS lat, AVG(lon) AS lon,
       SUM(kind IS NOT 'site_info') AS n_listings, SUM(active AND kind IS NOT 'site_info') AS n_active, COUNT(DISTINCT unit_key) AS n_units,
       group_concat(DISTINCT source) AS sources,
       MIN(price_month) AS min_price_month, MIN(price_per_bed) AS min_price_per_bed,
       MAX(beds) AS max_beds,
       MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen
FROM l GROUP BY place_key;

CREATE VIEW operators AS
SELECT operator, COUNT(DISTINCT place_key) AS n_places, group_concat(DISTINCT place_key) AS place_keys
FROM listings WHERE operator IS NOT NULL GROUP BY operator;
"""


def ddl() -> str:
    drops = "\n".join(f"DROP VIEW IF EXISTS {v};" for v in VIEW_NAMES)
    return "\n".join([TABLES, _triggers(), drops, _views()])
