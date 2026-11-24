"""Demo observations for the dashboard, written to data/seed.db (never the real database).

Three places as the plan asks, plus a deliberate cross-source conflict:
  * near downtown Palo Alto (Palo Alto): an Airbnb private room with private bath (temp candidate, no address,
    identified by lat/lon), and a Furnished Finder studio with an address
  * Mountain View, 49 Showers Dr (a benchmark row): Zillow with two runs (price drop) and Apartments.com
    disagreeing on price and baths -> conflict queue
  * Redwood City: a 2bd/1ba unfurnished lease that fails both goals
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .. import identity, settings
from .db import append_observation, connect, finish_run, init_db, start_run

FLOORPLAN_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 200" font-family="sans-serif"
 font-size="12"><rect width="300" height="200" fill="#f4f1ea"/><g fill="none" stroke="#333" stroke-width="3">
<rect x="10" y="10" width="280" height="180"/><line x1="120" y1="10" x2="120" y2="120"/>
<line x1="10" y1="120" x2="200" y2="120"/><line x1="200" y1="10" x2="200" y2="190"/></g>
<text x="30" y="60">Bed 1</text><text x="140" y="60">Bed 2</text><text x="220" y="60">Bed 3</text>
<text x="60" y="160">Living</text><text x="215" y="160">Bath x2</text>
<text x="150" y="196" font-size="9" text-anchor="middle">SEED DATA: placeholder floor plan</text></svg>"""


def _media(svg: str) -> str:
    settings.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(svg.encode()).hexdigest()[:16] + ".svg"
    (settings.MEDIA_DIR / name).write_text(svg)
    return name


def seed_db(path: str | Path, fresh: bool = False, with_trips: bool = True) -> None:
    path = Path(path)
    if fresh:
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
    conn = connect(path)
    init_db(conn)
    if conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]:
        print("seed db already has observations; use --fresh to rebuild")
        return
    plan = _media(FLOORPLAN_SVG)
    full = {"scope": "full", "seed": True}

    def run(source, day, obs):
        rid = start_run(conn, "intake", source, full, started_at=f"2026-11-{day:02d}T14:00:00+00:00")
        for o in obs:
            sid, price, basis, data = o[:4]
            q = o[4] if len(o) > 4 else (None, None)
            append_observation(conn, rid, source, sid, data, url=f"https://example.invalid/{source}/{sid}",
                               price=price, price_basis=basis, query_checkin=q[0], query_checkout=q[1],
                               observed_at=f"2026-11-{day:02d}T14:05:00+00:00", commit=False)
        finish_run(conn, rid, "ok", {"observations": len(obs)}, est_cost=0.0,
                   finished_at=f"2026-11-{day:02d}T14:10:00+00:00")

    downtown_room = ("abnb-1001", 3100.0, "stay", {
        "lat": 37.4461, "lon": -122.1589, "beds": 1, "baths": 1, "kind": "room", "term": "short",
        "furnished": True, "private_bath": True, "title": "SEED: Quiet room with private bath near University Ave",
        "amenities": ["Wifi", "Kitchen", "Washer"], "city": "Palo Alto"}, ("2026-12-01", "2027-01-01"))
    ff_studio = ("ff-77", 3400.0, "month", {
        "address": "1010 Waverley St, Palo Alto, CA 94301", "lat": 37.4407, "lon": -122.1531, "beds": 0, "baths": 1, "kind": "entire_place",
        "term": "short", "furnished": True, "private_bath": True, "available_from": "2027-01-05",
        "title": "SEED: Furnished studio, Professorville", "sqft": 420})
    showers_z = lambda price: ("z-2053923019", price, "month", {  # noqa: E731
        "address": "49 Showers Dr Floor 1-ID1622, Mountain View, CA 94040", "beds": 3, "baths": 2,
        "sqft": 1180, "kind": "apartment", "term": "long", "furnished": False, "lat": 37.4062, "lon": -122.1058,
        "available_from": "2026-11-20", "amenities": ["Gym", "Pool", "In-unit laundry"],
        "property_name": "SEED: Showers Apartments", "media": [plan]})
    showers_a = ("ac-55102", 4950.0, "month", {
        "address": "49 Showers Drive Floor 1-ID1622, Mountain View, CA 94040", "beds": 3, "baths": 2.5,
        "sqft": 1180, "kind": "apartment", "term": "long", "lat": 37.4061, "lon": -122.1059,
        "available_from": "2026-12-01", "amenities": ["Fitness center", "Pool"],
        "property_name": "SEED: Showers Apartments"})
    rwc = ("z-999", 4200.0, "month", {
        "address": "800 Brewster Ave, Redwood City, CA 94063", "beds": 2, "baths": 1, "sqft": 900,
        "kind": "house", "term": "long", "furnished": False, "lat": 37.4829, "lon": -122.2326,
        "available_from": "2027-01-01"})

    run("airbnb", 20, [downtown_room])
    run("furnishedfinder", 20, [ff_studio])
    run("zillow", 18, [showers_z(4900.0), rwc])
    run("zillow", 21, [showers_z(4710.0), rwc])
    run("apartments_com", 21, [showers_a])
    conn.commit()
    identity.run(conn)
    if with_trips:
        from ..trips import build_commute, compute_all, make_source

        commute = build_commute(str(path), make_source("estimate"))
        rid = start_run(conn, "commute", "estimate", {"seed": True})
        res = compute_all(conn, commute)
        finish_run(conn, rid, "ok", res, est_cost=0.0)
    conn.close()
