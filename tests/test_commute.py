"""Commute module tested alone against a fake LegSource."""

from __future__ import annotations

import pytest

from aptfinder.commute import Commute, LegResult, Station, StraightLineLegSource
from aptfinder.commute.geo import haversine_m

STATIONS = [Station("A", 37.40, -122.10), Station("B", 37.45, -122.16), Station("C", 37.48, -122.23)]
TRAIN = {"patterns": {"local": {"headway_min": 30, "offsets": {"A": 10, "B": 5, "C": 0}},
                      "express": {"headway_min": 60, "offsets": {"A": 7, "C": 0}}},
         "peak_trains_per_hour": {"A": 4, "B": 3, "C": 4}}


class FakeSource:
    """Minutes = straight-line km times a per-mode factor. Counts every call."""

    name = "fake"
    FACTOR = {"walk": 12.0, "bike": 4.0, "drive": 1.5, "transit": 3.0}

    def __init__(self):
        self.calls: list[tuple] = []

    def leg(self, o, d, mode, depart):
        self.calls.append((o, d, mode))
        km = haversine_m(o, d) / 1000
        steps = []
        if mode == "transit":
            steps = [{"mode": "walk", "minutes": 6.0, "distance_m": 500},
                     {"mode": "transit", "minutes": km * 2, "distance_m": km * 1000, "line": "22"},
                     {"mode": "walk", "minutes": 6.0, "distance_m": 500}]
            return LegResult(minutes=round(km * 2 + 12 + 5, 3), distance_m=km * 1000, steps=steps)
        return LegResult(minutes=round(km * self.FACTOR[mode], 3), distance_m=km * 1000)


@pytest.fixture
def make(tmp_path):
    def _make(source=None):
        src = source or FakeSource()
        return Commute(tmp_path / "cache.db", src, STATIONS, TRAIN), src
    return _make


def test_basic_mode_and_cache(make):
    c, src = make()
    o, d = (37.401, -122.101), (37.47, -122.22)
    t1 = c.trip(o, d, "walk")
    t2 = c.trip(o, d, "walk")
    assert t1.minutes == t2.minutes and t1.minutes > 0
    assert len(src.calls) == 1
    assert t1.provider == "fake"


def test_cache_survives_new_instance(make, tmp_path):
    c, src = make()
    c.trip((37.401, -122.101), (37.47, -122.22), "bike")
    c2 = Commute(tmp_path / "cache.db", src, STATIONS, TRAIN)
    c2.trip((37.401, -122.101), (37.47, -122.22), "bike")
    assert len(src.calls) == 1


def test_bike_train_bike_reuses_far_end_legs(make):
    c, src = make()
    target = (37.481, -122.231)  # near station C
    c.trip((37.401, -122.101), target, "bike_train_bike")
    c.trip((37.402, -122.099), target, "bike_train_bike")  # different origin, same target
    far_end = [call for call in src.calls if call[1] == (37.481, -122.231) and call[0] in
               {(s.lat, s.lon) for s in STATIONS}]
    assert len(far_end) == len(set(far_end))  # each station->target leg fetched once only
    assert far_end


def test_bike_train_bike_uses_train_and_never_slower_than_bike(make):
    c, _ = make()
    o, d = (37.401, -122.101), (37.481, -122.231)
    btb = c.trip(o, d, "bike_train_bike")
    bike = c.trip(o, d, "bike")
    assert btb.minutes <= bike.minutes
    modes = [leg["mode"] for leg in btb.legs]
    assert modes == ["bike", "wait", "train", "bike"]
    train = btb.legs[2]
    assert (train["from"], train["to"], train["minutes"]) == ("A", "C", 7)  # express beats local


def test_composed_modes_fall_back_to_bike_for_short_trips(make):
    c, _ = make()
    o, d = (37.4000, -122.1000), (37.4030, -122.1030)
    bike = c.trip(o, d, "bike")
    assert c.trip(o, d, "bike_train_bike").minutes == bike.minutes
    assert c.trip(o, d, "bike_bus_bike").minutes == bike.minutes


def test_bike_bus_bike_retimes_walks_and_counts_waits(make):
    c, _ = make()
    o, d = (37.30, -122.00), (37.48, -122.23)  # long: transit beats bike in the fake
    t = c.trip(o, d, "bike_bus_bike")
    assert t.approximate
    bike_legs = [leg for leg in t.legs if leg["mode"] == "bike"]
    assert [leg["minutes"] for leg in bike_legs] == [2.0, 2.0]  # 500 m at 250 m/min
    assert any(leg["mode"] == "wait" for leg in t.legs)
    assert t.minutes <= c.trip(o, d, "bike").minutes


def test_ride_and_wait():
    c = Commute(":memory:", FakeSource(), STATIONS, TRAIN)
    assert c.ride_minutes("A", "B") == 5
    assert c.ride_minutes("A", "C") == 7
    assert c.wait_minutes("A") == 7.5 and c.wait_minutes("B") == 10


def test_nearest_k(make):
    c, src = make()
    pts = [Station(f"p{i}", 37.40 + i * 0.01, -122.10) for i in range(6)]
    res = c.nearest((37.40, -122.10), pts, "walk", k=2)
    assert [p.name for p, _ in res] == ["p0", "p1"]
    assert len(src.calls) == 1  # p0 is the origin itself: zero-length, no call
    assert res[0][1].minutes == 0


def test_estimate_source_has_no_transit(make):
    c, _ = make(StraightLineLegSource())
    t = c.trip((37.40, -122.10), (37.44, -122.16), "transit")
    assert t.minutes is None and t.legs == []
    w = c.trip((37.40, -122.10), (37.44, -122.16), "walk")
    assert w.approximate and w.minutes > 0


def test_unknown_mode(make):
    c, _ = make()
    with pytest.raises(ValueError):
        c.trip((0, 0), (1, 1), "teleport")


def test_metered_source_logs_and_enforces_monthly_caps(tmp_path, monkeypatch):
    from aptfinder import trips

    monkeypatch.setattr(trips, "load_google_caps", lambda: {"monthly_call_cap": 3, "monthly_cap_by_mode": {"bike": 1},
                                                            "free_calls_per_month": 2, "price_per_1000_usd": 5.0})
    inner = FakeSource()
    inner.name = "google"
    meter_db = str(tmp_path / "meter.db")
    src = trips.MeteredSource(inner, max_calls=10, meter_db=meter_db)
    c = Commute(tmp_path / "cache.db", src, STATIONS, TRAIN)
    c.trip((37.40, -122.10), (37.41, -122.11), "bike")
    c.trip((37.40, -122.10), (37.41, -122.11), "bike")  # cached: not a call
    with pytest.raises(trips.SpendCapReached, match="bike cap"):
        c.trip((37.40, -122.10), (37.42, -122.12), "bike")
    c.trip((37.40, -122.10), (37.41, -122.11), "walk")
    c.trip((37.40, -122.10), (37.41, -122.11), "drive")
    with pytest.raises(trips.SpendCapReached, match="monthly cap"):
        c.trip((37.40, -122.10), (37.41, -122.11), "transit")
    use = trips.google_usage(src.meter)
    assert use["calls"] == 3 and use["by_mode"] == {"bike": 1, "walk": 1, "drive": 1}
    assert use["est_cost_usd"] == 0.01  # 1 call beyond 2 free at $5/1000 -> 0.005, rounded


def test_auto_modes_limit_paid_routing_and_on_demand_routes_the_full_table(tmp_path):
    """point_sets.toml `auto`: Google routes only the listed point/mode pairs by itself; on demand routes all."""
    from aptfinder import trips
    from aptfinder.config import load_point_sets, load_points
    from aptfinder.store import connect, init_db

    sets, points = load_point_sets(), load_points()
    for name, cfg in sets.items():  # a misspelled point name would silently route nothing
        assert set(cfg.auto or {}) <= {p.name for p in points.get(name, [])}, name
    db = str(tmp_path / "a.db")
    conn = connect(db)
    init_db(conn)
    src = FakeSource()
    src.name = "google"
    place = {"place_key": "p1", "lat": 37.44, "lon": -122.16}  # Palo Alto: every target within max_km
    trips.compute_for_place(conn, trips.build_commute(db, src), place)
    got = {(r[0].split(":", 1)[1], r[1]) for r in conn.execute("SELECT point_id, mode FROM trips")}
    want = {(p, m) for p, ms in sets["commute_targets"].auto.items() for m in ms}
    assert got == want
    trips.compute_for_place(conn, trips.build_commute(db, src), place, on_demand=True)
    modes = {r[0] for r in conn.execute("SELECT DISTINCT mode FROM trips WHERE point_id LIKE 'commute_targets:%'")}
    assert {"walk", "bike", "drive"} <= modes
