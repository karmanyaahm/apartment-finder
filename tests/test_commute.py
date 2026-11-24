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
