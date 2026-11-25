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
    assert (META, "drive") not in trips["far"]  # nothing within RADIUS_M and no row of its own: nothing
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


def test_transit_knn_fills_a_place_with_no_transit_row_and_the_far_estimate_without_a_neighbour():
    pa = "commute_targets:Downtown Palo Alto"
    loc = {"a": (37.40, -122.10), "b": (37.401, -122.10), "far": (37.45, -122.10)}
    # b has no transit row at all; far has the straight-line estimate's row, which has no time for transit
    trips = _trips([("a", (pa, "transit"), _g(22.0)), ("far", (pa, "transit"), {"provider": "estimate", "minutes": None})])
    add_knn(trips, loc, [(pa, "transit")])
    assert trips["b"][(pa, "transit")]["provider"] == "knn" and trips["b"][(pa, "transit")]["minutes"] == 22.0
    far = trips["far"][(pa, "transit")]  # no routed neighbour within 400 m: the far estimate, never '–'
    assert far["provider"] == "estimate" and far["minutes"] == 22.0 and not far["no_route"]


def test_transit_prefetch_splits_at_100_and_the_google_body_departs_in_the_future(tmp_path):
    import json as _json
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import httpx

    from aptfinder.commute.sources import GoogleRoutesLegSource

    src = FakeMatrix()
    c = Commute(tmp_path / "c.db", src)
    dest = (37.4447, -122.1610)
    legs = [((37.30 + i * 0.001, -122.10), dest, "transit") for i in range(250)]
    res = c.prefetch(legs)
    assert res["elements"] == 250 and [r[0] for r in src.requests] == [100, 100, 50]

    seen = []

    def handler(req):
        seen.append(_json.loads(req.content))
        return httpx.Response(200, json=[{"originIndex": 1, "condition": "ROUTE_EXISTS", "duration": "1500s",
                                          "distanceMeters": 9000}, {"condition": "ROUTE_NOT_FOUND"}])

    g = GoogleRoutesLegSource("k", httpx.Client(transport=httpx.MockTransport(handler)))
    out = g.matrix([(37.40, -122.10), (37.41, -122.10)], [dest], "transit", "weekday_0830")
    assert out[(1, 0)].minutes == 25.0 and out[(0, 0)] is None
    body = seen[0]
    assert body["travelMode"] == "TRANSIT" and "routingPreference" not in body
    dep = datetime.fromisoformat(body["departureTime"])
    assert dep > datetime.now(ZoneInfo("America/Los_Angeles")) and dep.weekday() == 1 and dep.hour == 8
    with pytest.raises(ValueError, match="at most 100"):
        g.matrix([(37.40, -122.10)] * 101, [dest], "transit", "weekday_0830")


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
    assert d["cells"] == [] and "drive" in d["modes"] and "transit" in d["modes"]
    assert any(r["id"] == META for r in d["rows"])
    cal = next(r for r in d["rows"] if r["id"] == "set:caltrain_stations")
    assert cal["k"] >= 1 and cal["points"] >= cal["k"] and d["k_stations"] == trips.K_STATIONS
    assert "matrix_cap" in d and "matrix_free" in d and d["knn_k"] == 3 and d["typed_over"] == 20
    assert c.post("/api/knn/cells", json={"cells": [["set:caltrain_stations", "bike_train_bike"]]}).status_code == 400
    assert c.post("/api/knn/cells", json={"cells": [["set:caltrain_stations", "transit"]]}).status_code == 400
    assert c.post("/api/knn/cells", json={"cells": [[META, "drive"]]}).status_code == 200
    assert c.get("/api/knn/cells").json()["cells"] == [[META, "drive"]]
    keys = [p["place_key"] for p in c.get("/api/places").json()]
    r = c.post("/api/sample/dry_run", json={"method": "random", "pct": 50, "scope": "list", "place_keys": keys,
                                            "seed": 3}).json()
    pair = r["pairs"][f"{META}|drive"]
    assert r["scope"] == len(keys) and pair["places"] == round(len(keys) * 0.5) and r["elements"] == pair["elements"]
    assert r["plan"][f"{META}|drive"] and r["usage"]["matrix_elements"] == 0
    acc = r["accuracy"][f"{META}|drive"]
    assert acc["scope"] == len(keys) and acc["counts"]["google"] + acc["counts"]["knn"] + acc["counts"]["estimate"] == len(keys)
    assert set(acc["loo"]) == {"n", "median", "p95", "p99", "inside_pct"}
    a2 = c.post("/api/knn/accuracy", json={"scope": "list", "place_keys": keys}).json()
    assert a2["cells"][f"{META}|drive"] == acc and a2["pad_min"] == 0.5
    ref = c.post("/api/sample/dry_run", json={"method": "refine", "scope": "zone"}).json()
    assert set(ref["pairs"][f"{META}|drive"]) >= {"straddle", "wide", "no_neighbour"}
    assert set(ref["hinge"]) == {"total", "by_goal", "by_pair", "after"}
    st = c.post("/api/knn/status", json={"place_keys": keys}).json()
    assert st["list"]["scope"] == len(keys) and f"{META}|drive" in st["zone"]["cells"]
    assert st["list"]["cells"][f"{META}|drive"]["verdict"]["state"] in ("done", "run", "settled", "unknown")
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
    params = json.loads(conn.execute("SELECT params FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert params["sample"] and params["from"] == "dashboard"  # closed out on the next start if a restart cuts it
    log = c.get("/api/compute/log").json()  # the Pipeline panel's run log
    assert log[0]["status"] == "ok" and log[0]["params"]["sample"] and log[0]["source"]


def test_prefetch_keeps_the_requests_that_returned_when_a_later_one_fails(tmp_path):
    # a run cut off mid-way (a spend cap, an error, a restart) must not pay again for what already came back
    class FailsSecond(FakeMatrix):
        def matrix(self, origins, dests, mode, depart):
            if self.requests:
                raise RuntimeError("cut off")
            return super().matrix(origins, dests, mode, depart)

    src = FailsSecond()
    c = Commute(tmp_path / "c.db", src)
    dest = (37.4847, -122.1484)
    legs = [((37.40 + i * 0.001, -122.10), dest, "drive") for i in range(4)]
    with pytest.raises(RuntimeError):
        c.prefetch(legs, batch=3)
    assert c._db.execute("SELECT COUNT(*) FROM commute_legs").fetchone()[0] == 3  # the first request's 3 elements
    src.requests.clear()
    src.matrix = FakeMatrix.matrix.__get__(src)
    assert c.prefetch(legs, batch=3)["elements"] == 1  # the rerun asks only for the one never answered


def test_leave_one_out_matches_add_knn_without_the_place_and_scores_against_the_padded_range():
    from aptfinder.filter.knn import accuracy, loo_errors

    loc = {f"p{i}": (37.40 + i * 0.001, -122.10) for i in range(5)}
    loc["far"] = (37.45, -122.10)
    mins = {"p0": 15.0, "p1": 16.0, "p2": 18.0, "p3": 30.0, "p4": 17.0, "far": 50.0}
    trips = _trips([(pk, (META, "drive"), _g(m)) for pk, m in mins.items()])
    errs = loo_errors(trips, loc, [(META, "drive")])
    # p2 predicted from p1, p3, p0/p4: what add_knn gives p2 when its own trip is gone
    without = _trips([(pk, (META, "drive"), _g(m)) for pk, m in mins.items() if pk != "p2"])
    add_knn(without, loc, [(META, "drive")])
    k = without["p2"][(META, "drive")]
    assert len(errs) == 5  # 'far' has no routed neighbour within 400 m: not scored
    lo, hi = k["min"] - 0.5, k["max"] + 0.5
    assert (0.0 if lo <= 18.0 <= hi else min(abs(18.0 - lo), abs(18.0 - hi))) in errs
    a = accuracy([0.0, 0.0, 0.0, 1.0, 12.5])
    assert a == {"n": 5, "median": 0.0, "p95": 12.5, "p99": None, "inside_pct": 60.0}  # n < 100: no p99
    big = accuracy([0.0] * 97 + [3.0, 7.0, 20.0])  # 100 places: p99 is the 99th, p95 the 95th
    assert big["n"] == 100 and big["p99"] == 7.0 and big["p95"] == 0.0
    assert accuracy([])["n"] == 0


def test_cell_shown_splits_the_scope_by_the_provider_the_list_shows():
    from aptfinder.sampling import cell_shown

    loc = {"a": (37.40, -122.10), "b": (37.401, -122.10), "c": (37.45, -122.10)}
    trips = _trips([("a", (META, "drive"), _g(15.0)), ("c", (META, "drive"), {"provider": "estimate", "minutes": 9.0})])
    add_knn(trips, loc, [(META, "drive")])
    s = cell_shown(trips, (META, "drive"), ["a", "b", "c"])
    assert s["counts"] == {"google": 1, "knn": 1, "estimate": 1} and s["google_pct"] == 33.3


def test_restart_closes_the_dashboards_open_commute_runs_only(tmp_path):
    from fastapi.testclient import TestClient

    from aptfinder.store import connect, start_run
    from aptfinder.store.seed import seed_db
    from aptfinder.web.app import INTERRUPTED, create_app

    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    conn = connect(db)
    mine = start_run(conn, "commute", "google", {"sample": "cover within 400 m", "from": "dashboard"})
    cli = start_run(conn, "commute", "google", {"sample": "cover within 400 m"})  # another process's: left alone
    c = TestClient(create_app(db))
    s = c.get("/api/compute/status").json()
    assert s["state"] == "error" and s["interrupted"] == [mine] and INTERRUPTED in s["result"]["error"]
    rows = dict(conn.execute("SELECT id, status FROM runs WHERE id IN (?, ?)", (mine, cli)).fetchall())
    assert rows == {mine: "error", cli: "running"}
    assert conn.execute("SELECT error FROM runs WHERE id = ?", (mine,)).fetchone()[0] == INTERRUPTED
    assert TestClient(create_app(db)).get("/api/compute/status").json() == {"state": "idle"}  # closed once


def _cfg(rules):
    from types import SimpleNamespace

    return SimpleNamespace(goals={"lease": SimpleNamespace(rules=rules, nice=[])})


def test_refine_picks_wide_or_straddling_ranges_of_candidates_only_and_runs_out():
    from aptfinder.sampling import REFINE_MAX_SPREAD_MIN, refine_plan

    pair = (META, "drive")
    cfg = _cfg([{"type": "commute_any", "target": META, "options": [{"mode": "drive", "max": 30}]}])
    # a (20) and c (25) routed; b, x between them read a 20-25 range (5 min wide); d is 5 km away (estimate only)
    loc = {"a": (37.400, -122.10), "b": (37.401, -122.10), "c": (37.402, -122.10), "x": (37.4012, -122.10),
           "d": (37.45, -122.10)}

    def trips_with(routed):
        t = _trips([(pk, pair, _g(m)) for pk, m in routed.items()]
                   + [("d", pair, {"provider": "estimate", "minutes": 31.0})])
        add_knn(t, loc, [pair])
        return t

    t = trips_with({"a": 20.0, "c": 25.0})
    assert t["b"][pair]["max"] - t["b"][pair]["min"] > REFINE_MAX_SPREAD_MIN
    goals_of = {"b": {"lease"}, "d": {"lease"}, "x": set()}  # x fails the goal on price: never routed
    plan, notes = refine_plan(t, [pair], sorted(loc), cfg, goals_of)
    assert plan[pair] == ["b"] and notes[pair] == {"straddle": 0, "wide": 1, "no_neighbour": 1}
    # after routing b, nothing a candidate reads is wide any more: refine converges
    t2 = trips_with({"a": 20.0, "c": 25.0, "b": 22.0})
    assert refine_plan(t2, [pair], sorted(loc), cfg, goals_of)[0][pair] == []
    # a narrow range straddling the limit is picked whatever its width
    t3 = trips_with({"a": 29.5, "c": 30.0})
    assert refine_plan(t3, [pair], sorted(loc), cfg, goals_of)[1][pair]["straddle"] == 1


def test_hinges_count_places_waiting_only_on_a_commute_and_the_plan_settles_them():
    from aptfinder.sampling import commute_status, hinge_counts, hinges

    pair = (META, "drive")
    cfg = _cfg([{"type": "commute_any", "target": META, "options": [{"mode": "drive", "max": 30}]}])
    loc = {"a": (37.400, -122.10), "b": (37.401, -122.10), "x": (37.4012, -122.10), "c": (37.402, -122.10)}
    t = _trips([("a", pair, _g(29.5)), ("c", pair, _g(30.0))])
    add_knn(t, loc, [pair])
    commute = ("needs_check", "too close", "commute_any")
    res = {"listings": {"lease": {
        ("s", "1"): {"place_key": "b", "decides": True, "status": "needs_check",
                     "reasons": [("pass", "price", "max"), commute]},
        ("s", "2"): {"place_key": "x", "decides": True, "status": "needs_check",  # price unknown too: not only commute
                     "reasons": [("needs_check", "price unknown", "max"), commute]},
        ("s", "3"): {"place_key": "a~op", "decides": True, "status": "pass", "reasons": [("pass", "", "max")]}}}}
    h = hinges(res, t, cfg, loc)
    assert h == {"b": {"lease": {pair}}}
    hc = hinge_counts(h, sorted(loc), [pair], {pair: ["b"]})
    assert (hc["total"], hc["by_goal"], hc["by_pair"]) == (1, {"lease": 1}, {f"{META}|drive": 1})
    assert hc["after"]["total"] == 0
    st = commute_status(t, [pair], sorted(loc), h, lambda p: [(0.0, 0.5)], lambda p: [3.0], lambda m, p: (1, 1))
    row = st["cells"][f"{META}|drive"]
    assert row["undecided"] == 1 and row["verdict"]["state"] == "run" and (row["p90"], row["p99"]) == (0.0, 0.0)
    st = commute_status(t, [pair], sorted(loc), {}, lambda p: [(0.0, 0.5)], lambda p: [3.0], lambda m, p: (0, 0))
    assert st["cells"][f"{META}|drive"]["verdict"]["state"] == "done" and st["done"]


def test_a_place_with_no_time_and_no_neighbour_within_the_radius_gets_the_far_estimate_through_the_band():
    pa = "commute_targets:Downtown Palo Alto"
    # three routed places ~2 km south of `far`, 50-60 min; `far` has a Google row with no route
    loc = {"a": (37.430, -122.10), "b": (37.431, -122.10), "c": (37.432, -122.10), "far": (37.45, -122.10)}
    trips = _trips([("a", (pa, "transit"), _g(50.0)), ("b", (pa, "transit"), _g(55.0)),
                    ("c", (pa, "transit"), _g(60.0)), ("far", (pa, "transit"), _g(None))])
    add_knn(trips, loc, [(pa, "transit")])
    t = trips["far"][(pa, "transit")]
    assert t["provider"] == "estimate" and t["far"] and t["no_route"] and t["minutes"] == 55.0
    s, why = _minutes_status(t, 25)
    assert s == FAIL and why.startswith("Google found no route; 55 min (estimate: nearest 3 Google routes")
    assert _minutes_status({**t, "minutes": 30.0}, 25)[0] == CHECK  # inside the band: needs_check, never a rule


def test_a_google_row_without_minutes_never_hides_a_time(tmp_path):
    from aptfinder.filter import best_trips
    from aptfinder.store import connect, init_db

    conn = connect(tmp_path / "t.db")
    init_db(conn)
    pa = "commute_targets:Downtown Palo Alto"
    for prov, m in (("estimate", 40.0), ("google", None)):
        conn.execute("INSERT INTO trips (computed_at, place_key, point_id, mode, minutes, provider, o_lat, o_lon) "
                     "VALUES ('2026-11-25', 'x', ?, 'bike', ?, ?, 37.4, -122.1)", (pa, m, prov))
    conn.commit()
    t = best_trips(conn, {"x": (37.4, -122.1)})["x"][(pa, "bike")]
    assert t["provider"] == "estimate" and t["minutes"] == 40.0


def test_goal_cover_plan_covers_per_pair_only_the_candidates_of_a_goal_with_a_rule_on_it():
    from aptfinder.sampling import goal_cover_plan

    pa = "commute_targets:Downtown Palo Alto"
    cfg = _cfg([{"type": "commute_any", "target": pa, "options": [{"mode": "transit", "max": 25}]}])  # goal 'lease'
    loc = {"stay": (37.40, -122.10), "flat": (37.45, -122.10)}  # 5 km apart, neither routed
    plan = goal_cover_plan(_trips([]), [(pa, "transit"), (META, "drive")], sorted(loc), loc, cfg,
                           {"stay": {"lease"}, "flat": {"other"}})
    assert plan == {(pa, "transit"): ["stay"], (META, "drive"): []}  # no goal has a Meta drive rule


def test_routed_set_pair_needs_a_google_trip_to_the_current_nearest_point():
    # a stop added to a set: p0's Google walk goes to the old stop, its estimate to the new (nearer) one is faster,
    # so p0 is routed again (to its current nearest); p1 has only its Google trip and stays routed
    from aptfinder.sampling import cover_plan, routed

    pair = ("set:google_bus_stops", "walk")
    old, new = ("google_bus_stops:Palo Alto Caltrain", "walk"), ("google_bus_stops:Menlo Park CalTrain", "walk")
    trips = _trips([("p0", old, _g(26.0)), ("p0", new, {"provider": "estimate", "minutes": 3.0}),
                    ("p1", old, _g(5.0))])
    assert routed(trips, pair) == {"p1"}
    loc = {"p0": (37.4533, -122.1821), "p1": (37.4433, -122.1650)}  # 1.9 km apart
    assert cover_plan(trips, [pair], sorted(loc), loc, 400)[pair] == ["p0"]
