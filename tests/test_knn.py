"""KNN ranges from nearby Google trips, the rules over a range, sampling plans and the Route Matrix prefetch."""

from __future__ import annotations

import json
from collections import defaultdict

import pytest

from aptfinder.commute import Commute, LegResult
from aptfinder.filter.knn import RADIUS_M, add_knn
from aptfinder.filter.rules import CHECK, FAIL, PASS, _minutes_status

META = "commute_targets:Meta HQ"


def _g(minutes):
    return {"provider": "google", "minutes": minutes}


def _trips(rows):
    t = defaultdict(dict)
    for pk, key, trip in rows:
        t[pk][key] = trip
    return t


def test_knn_range_from_routed_places_within_the_radius():
    # ~110 m apart along a line of latitude (0.001 deg)
    loc = {f"p{i}": (37.40 + i * 0.001, -122.10) for i in range(6)}
    loc["far"] = (37.45, -122.10)  # 5 km away: no neighbour within 400 m
    trips = _trips([("p0", (META, "drive"), _g(15.0)), ("p1", (META, "drive"), _g(16.0)),
                    ("p2", (META, "drive"), _g(18.0)), ("p5", (META, "drive"), _g(30.0)),
                    ("p3", (META, "drive"), {"provider": "estimate", "minutes": 27.0})])
    add_knn(trips, loc, [(META, "drive")])
    t = trips["p3"][(META, "drive")]
    assert t["provider"] == "knn" and (t["min"], t["max"]) == (16.0, 30.0)  # p2, p1 (~220 m) and p5 (~220 m)
    assert len(t["neighbors"]) == 3 and t["minutes"] == 18.0
    assert trips["p0"][(META, "drive")]["provider"] == "google"  # own Google trip wins
    assert (META, "drive") not in trips["far"]  # nothing within RADIUS_M: keeps whatever it had (here none)
    assert RADIUS_M == 400


def test_knn_one_neighbour_is_a_single_value_and_only_checked_cells_get_knn():
    loc = {"a": (37.40, -122.10), "b": (37.401, -122.10)}
    trips = _trips([("a", (META, "drive"), _g(15.0)), ("a", (META, "walk"), _g(150.0))])
    add_knn(trips, loc, [(META, "drive")])
    assert trips["b"][(META, "drive")]["min"] == trips["b"][(META, "drive")]["max"] == 15.0
    assert (META, "walk") not in trips["b"]


def test_knn_set_cell_covers_every_point_of_the_set_and_stay_places_read_their_building():
    loc = {"a": (37.40, -122.10), "b": (37.401, -122.10), "b~blueground": (37.401, -122.10)}
    trips = _trips([("a", ("caltrain_stations:Mountain View", "walk"), _g(12.0))])
    add_knn(trips, loc, [("set:caltrain_stations", "walk")])
    assert trips["b"][("caltrain_stations:Mountain View", "walk")]["provider"] == "knn"
    assert "b~blueground" not in trips


@pytest.mark.parametrize("lo,hi,limit,want", [
    (16.0, 19.0, 20, PASS),      # 19 + 0.5 <= 20
    (16.0, 19.6, 20, CHECK),     # padded max over the limit
    (21.0, 23.0, 20, CHECK),     # 0.9 x (21 - 0.5) = 18.45: not past the margin
    (23.0, 25.0, 20, FAIL),      # 0.9 x (23 - 0.5) = 20.25 > 20
])
def test_rules_over_a_knn_range(lo, hi, limit, want):
    trip = {"provider": "knn", "minutes": (lo + hi) / 2, "min": lo, "max": hi,
            "neighbors": [{"minutes": lo}, {"minutes": hi}]}
    status, text = _minutes_status(trip, limit)
    assert status == want
    assert "nearby Google route" in text


def test_cover_plan_picks_until_every_place_has_a_routed_place_within_the_radius():
    from aptfinder.sampling import cover_plan, random_plan

    # two clusters 3 km apart, 5 places each within ~100 m
    loc = {f"a{i}": (37.40 + i * 0.0002, -122.10) for i in range(5)}
    loc.update({f"b{i}": (37.43 + i * 0.0002, -122.10) for i in range(5)})
    keys = sorted(loc)
    trips = _trips([("a0", (META, "drive"), _g(15.0))])
    plan = cover_plan(trips, [(META, "drive")], keys, loc, 400)
    assert len(plan[(META, "drive")]) == 1 and plan[(META, "drive")][0].startswith("b")  # cluster a is covered
    r = random_plan(trips, [(META, "drive")], keys, 50, seed=1)
    assert len(r[(META, "drive")]) in (4, 5) and "a0" not in r[(META, "drive")]  # 5 picked, minus a routed one


class FakeMatrix:
    name = "google"

    def __init__(self):
        self.requests = []

    def leg(self, o, d, mode, depart):
        raise AssertionError("single legs must come from the cache")

    def matrix(self, origins, dests, mode, depart):
        self.requests.append((len(origins), len(dests), mode))
        return {(i, 0): LegResult(10.0 + i, 1000.0) for i in range(len(origins)) if i != 1} | {(1, 0): None}


def test_prefetch_fills_the_cache_one_request_per_destination_and_skips_cached(tmp_path):
    src = FakeMatrix()
    c = Commute(tmp_path / "c.db", src)
    dest = (37.4847, -122.1484)
    legs = [((37.40 + i * 0.001, -122.10), dest, "drive") for i in range(4)]
    res = c.prefetch(legs + [((37.40, -122.10), (37.42, -122.08), "drive")], batch=3)
    assert res["elements"] == 5 and res["requests"] == 3  # 4 origins to Meta in 3 + 1, one to the other point
    assert c.leg((37.40, -122.10), dest, "drive").minutes == 10.0
    assert c.leg((37.401, -122.10), dest, "drive") is None  # no route, cached as such
    assert c.prefetch(legs)["elements"] == 0  # all cached now


def test_metered_matrix_logs_elements_and_stops_at_the_caps(tmp_path, monkeypatch):
    from aptfinder import trips
    from aptfinder.store import connect, init_db

    db = str(tmp_path / "a.db")
    init_db(connect(db))
    inner = FakeMatrix()
    m = trips.MeteredMatrix(inner, max_elements=5, meter_db=db)
    m.PER_MINUTE = 10**9  # no pacing in the test
    m.cap = 4
    m.matrix([(37.40, -122.10), (37.41, -122.10)], [(37.48, -122.14)], "drive", "weekday_0830")
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = 'google_matrix'").fetchone()[0] == 2
    assert trips.google_usage(conn)["matrix_elements"] == 2
    with pytest.raises(trips.SpendCapReached, match="monthly cap of 4"):
        m.matrix([(37.40, -122.10)] * 3, [(37.48, -122.14)], "drive", "weekday_0830")
    assert len(inner.requests) == 1


def test_planned_legs_lists_uncached_legs_without_writing(tmp_path):
    from aptfinder import trips
    from aptfinder.store.seed import seed_db

    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    from aptfinder.store import connect

    conn = connect(db)
    keys = [r[0] for r in conn.execute("SELECT place_key FROM places WHERE lat IS NOT NULL")]
    n_trips = conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0]
    legs = trips.planned_legs(str(db), keys, (META, "drive"))
    assert len(legs) == len(keys) and {m for _, _, m in legs} == {"drive"}
    btb = trips.planned_legs(str(db), keys[:1], ("commute_targets:Downtown Palo Alto", "bike_train_bike"))
    assert btb and {m for _, _, m in btb} == {"bike"}  # bike+train asks for its bike legs
    assert conn.execute("SELECT COUNT(*) FROM trips").fetchone()[0] == n_trips


def test_knn_cells_and_sampling_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from aptfinder import trips
    from aptfinder.store import connect
    from aptfinder.store.seed import seed_db
    from aptfinder.web.app import create_app

    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    c = TestClient(create_app(db))
    d = c.get("/api/knn/cells").json()
    assert d["cells"] == [] and "drive" in d["modes"] and "transit" not in d["modes"]
    assert any(r["id"] == META for r in d["rows"])
    cal = next(r for r in d["rows"] if r["id"] == "set:caltrain_stations")
    assert cal["k"] >= 1 and cal["points"] >= cal["k"] and d["k_stations"] == trips.K_STATIONS
    assert "matrix_cap" in d and "matrix_free" in d and d["knn_k"] == 3 and d["typed_over"] == 20
    assert c.post("/api/knn/cells", json={"cells": [["set:caltrain_stations", "bike_train_bike"]]}).status_code == 400
    assert c.post("/api/knn/cells", json={"cells": [[META, "drive"]]}).status_code == 200
    assert c.get("/api/knn/cells").json()["cells"] == [[META, "drive"]]
    keys = [p["place_key"] for p in c.get("/api/places").json()]
    r = c.post("/api/sample/dry_run", json={"method": "random", "pct": 50, "scope": "list", "place_keys": keys,
                                            "seed": 3}).json()
    pair = r["pairs"][f"{META}|drive"]
    assert r["scope"] == len(keys) and pair["places"] == round(len(keys) * 0.5) and r["elements"] == pair["elements"]
    assert r["plan"][f"{META}|drive"] and r["usage"]["matrix_elements"] == 0
    cover = c.post("/api/sample/dry_run", json={"method": "cover", "radius_m": 400, "scope": "list",
                                                "place_keys": keys}).json()
    assert cover["pairs"][f"{META}|drive"]["covered_after"] == len(keys)
    # the run goes through the metered matrix: a fake one, then trips from the cache it filled
    fake = FakeMatrix()
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test")
    monkeypatch.setattr(trips, "GoogleRoutesLegSource", lambda key: fake)
    monkeypatch.setattr(trips.MeteredMatrix, "PER_MINUTE", 10**9)
    run = c.post("/api/sample/run", json={"plan": r["plan"], "label": r["label"], "confirm_elements": r["elements"]})
    assert run.status_code == 200
    import time

    for _ in range(200):
        s = c.get("/api/compute/status").json()
        if s["state"] != "running":
            break
        time.sleep(0.05)
    assert s["state"] == "done", s
    conn = connect(db)
    n = conn.execute("SELECT COUNT(*) FROM trips WHERE provider = 'google' AND point_id = ?", (META,)).fetchone()[0]
    assert n == len(r["plan"][f"{META}|drive"])  # one trip row per routed place (a 'no route' one included)
    assert conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = 'google_matrix'").fetchone()[0] == r["elements"]
    assert json.loads(conn.execute("SELECT params FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0])["sample"]
