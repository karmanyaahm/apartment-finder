"""Place position (Craigslist pins count only alone) and stale trips (a trip counts while its place is where it was
routed from)."""

from __future__ import annotations

import json

from aptfinder import trips
from aptfinder.filter import best_trips
from aptfinder.store import append_link, append_observation, append_trip, start_run

PA = "commute_targets:Downtown Palo Alto"


def _place(conn, pk):
    return conn.execute("SELECT lat, lon FROM places WHERE place_key = ?", (pk,)).fetchone()


def _obs(conn, run, source, sid, pk, **data):
    append_observation(conn, run, source, sid, data, price=3000, price_basis="month")
    append_link(conn, "test", source, sid, pk, pk, "test")


def test_craigslist_pin_ignored_when_another_source_places_the_building(conn):
    r = start_run(conn, "intake", "x")
    _obs(conn, r, "zillow", "z", "p_mixed", lat=37.40, lon=-122.10)
    _obs(conn, r, "craigslist", "c", "p_mixed", lat=37.42, lon=-122.08)  # an account pin 2.8 km away
    _obs(conn, r, "craigslist", "c2", "p_cl", lat=37.42, lon=-122.08)  # Craigslist only: its pin is all there is
    _obs(conn, r, "direct", "d", "p_nolat")  # an own-site row without coordinates places nothing
    _obs(conn, r, "craigslist", "c3", "p_nolat", lat=37.43, lon=-122.07)
    assert tuple(_place(conn, "p_mixed")) == (37.40, -122.10)
    assert tuple(_place(conn, "p_cl")) == (37.42, -122.08)
    assert tuple(_place(conn, "p_nolat")) == (37.43, -122.07)


def test_a_trip_from_an_old_position_is_stale(conn):
    here = (37.40, -122.10)
    assert trips.is_current({"o_lat": 37.4001, "o_lon": -122.1001}, here)  # 14 m: jitter, still current
    assert not trips.is_current({"o_lat": 37.401, "o_lon": -122.10}, here)  # 111 m: moved
    assert not trips.is_current({"o_lat": None, "o_lon": None}, here)  # origin unknown


def test_merge_moves_the_place_and_its_google_trip_goes_stale(conn):
    r = start_run(conn, "intake", "x")
    _obs(conn, r, "craigslist", "c", "p_a", lat=37.42, lon=-122.08)
    append_trip(conn, "p_a", PA, "walk", 18.6, 1500, [], None, "google", origin=(37.42, -122.08))
    append_trip(conn, "p_a", PA, "walk", 30.0, 2500, [], None, "estimate", origin=(37.42, -122.08))
    assert best_trips(conn)["p_a"][(PA, "walk")]["minutes"] == 18.6
    # the building's own listing joins the place (a merge): the place moves to it, 2.8 km away
    _obs(conn, r, "zillow", "z", "p_a", lat=37.40, lon=-122.10)
    t = best_trips(conn)
    assert (PA, "walk") not in t.get("p_a", {})  # never the old pin's 18.6 min
    # the free estimate is recomputed for the moved place
    assert "p_a" in trips.missing_estimates(conn)
    # and the Google pair is planned again
    assert trips.stale_plan(conn) == {(PA, "walk"): ["p_a"]}


def test_compute_appends_again_when_only_the_origin_changed(conn, tmp_path):
    class Fixed(trips.StraightLineLegSource):
        name = "estimate"

        def leg(self, o, d, mode, depart):
            return trips.LegResult(12.0, 1000.0)

    commute = trips.Commute(str(tmp_path / "c.db"), Fixed())
    place = {"place_key": "p_a", "lat": 37.40, "lon": -122.10}
    assert trips.compute_for_place(conn, commute, place, only=(PA, "walk")) == 1
    assert trips.compute_for_place(conn, commute, place, only=(PA, "walk")) == 0  # same place, same minutes
    moved = {**place, "lat": 37.41}
    assert trips.compute_for_place(conn, commute, moved, only=(PA, "walk")) == 1  # same minutes, new origin
    row = conn.execute("SELECT o_lat, o_lon FROM trips ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(row) == (37.41, -122.10)


def test_adopt_origins_reads_a_legacy_trip_origin_from_the_leg_cache(conn):
    from aptfinder.commute.engine import CACHE_DDL
    from aptfinder.config import load_points

    p = next(x for x in load_points()["commute_targets"] if x.id == PA)
    r = start_run(conn, "intake", "x")
    _obs(conn, r, "zillow", "z", "p_a", lat=37.40, lon=-122.10)
    _obs(conn, r, "zillow", "y", "p_b", lat=37.45, lon=-122.15)
    conn.execute(CACHE_DDL)
    legs = [{"mode": "walk", "minutes": 20.5, "distance_m": 1600.0, "steps": []}]
    for pk, o, m in (("p_a", (37.4, -122.1), 20.5), ("p_b", (37.44, -122.15), 31.0)):  # p_b routed 1.1 km off
        conn.execute("INSERT INTO commute_legs VALUES (?,?,?,?,'walk','weekday_0830','google',1,?,1600.0,'[]','x')",
                     (*o, round(p.lat, 4), round(p.lon, 4), m))
        conn.execute("INSERT INTO trips (computed_at, place_key, point_id, mode, minutes, distance_m, legs, provider) "
                     "VALUES ('2026-11-20', ?, ?, 'walk', ?, 1600.0, ?, 'google')",
                     (pk, PA, m, json.dumps([{**legs[0], "minutes": m}])))
    conn.commit()
    assert trips.stale_plan(conn) == {(PA, "walk"): ["p_a", "p_b"]}  # no origin: stale
    res = trips.adopt_origins(conn)
    assert (res["legacy"], res["adopted"], res["adopted_current"]) == (2, 2, 1)
    assert best_trips(conn)["p_a"][(PA, "walk")]["minutes"] == 20.5
    assert trips.stale_plan(conn) == {(PA, "walk"): ["p_b"]}


def _geo(conn, address, lat, lon, precise=1, location_type="ROOFTOP"):
    conn.execute("INSERT INTO geocodes (ts, address, status, precise, lat, lon, location_type, types) "
                 "VALUES ('2026-11-25T00:00:00Z', ?, 'OK', ?, ?, ?, ?, '[\"street_address\"]')",
                 (address, precise, lat, lon, location_type))
    conn.commit()


def test_craigslist_only_place_sits_on_its_geocoded_address(conn):
    r = start_run(conn, "intake", "x")
    _obs(conn, r, "craigslist", "a", "p_a", address="1 A St, Palo Alto, CA", lat=37.42, lon=-122.08)
    _obs(conn, r, "craigslist", "far", "p_far", address="2 B St, Palo Alto, CA", lat=37.42, lon=-122.08)
    _obs(conn, r, "craigslist", "vague", "p_vague", address="3 C St, Palo Alto, CA", lat=37.42, lon=-122.08)
    _obs(conn, r, "craigslist", "mix", "p_mix", address="4 D St, Palo Alto, CA", lat=37.42, lon=-122.08)
    _obs(conn, r, "zillow", "z", "p_mix", address="4 D St, Palo Alto, CA", lat=37.41, lon=-122.09)
    before = {pk: tuple(_place(conn, pk)) for pk in ("p_a", "p_far", "p_vague", "p_mix")}
    _geo(conn, "1 A St, Palo Alto, CA", 37.4250, -122.0850)  # 720 m from the pin: used
    _geo(conn, "2 B St, Palo Alto, CA", 37.50, -122.08)  # 8.9 km off: the pin stays
    _geo(conn, "3 C St, Palo Alto, CA", 37.43, -122.09, precise=0)  # not the written number: the pin stays
    _geo(conn, "4 D St, Palo Alto, CA", 37.43, -122.07)  # another source places the building: it wins
    assert tuple(_place(conn, "p_a")) == (37.4250, -122.0850)
    assert {pk: tuple(_place(conn, pk)) for pk in ("p_far", "p_vague", "p_mix")} == \
        {pk: before[pk] for pk in ("p_far", "p_vague", "p_mix")}
    assert before["p_mix"] == (37.41, -122.09)
    _geo(conn, "1 A St, Palo Alto, CA", 37.43, -122.09, precise=1, location_type="APPROXIMATE")  # latest row rules
    assert tuple(_place(conn, "p_a")) == (37.42, -122.08)


def test_positions_geocode_cached_craigslist_addresses_once(conn, tmp_path, monkeypatch):
    from aptfinder import settings
    from aptfinder.identity import addresses

    cache = {"1 A St, Palo Alto, CA": {"street": "1 a st", "city": "palo alto"},
             "9 Z St, Palo Alto, CA": {"fallback": True},  # Google never placed it: no position to ask for
             "4 D St, Palo Alto, CA": {"street": "4 d st", "city": "palo alto"}}
    monkeypatch.setattr(addresses, "load_geocodes", lambda: cache)
    r = start_run(conn, "intake", "x")
    _obs(conn, r, "craigslist", "a", "p_a", address="1 A St, Palo Alto, CA", lat=37.42, lon=-122.08)
    _obs(conn, r, "craigslist", "z", "p_z", address="9 Z St, Palo Alto, CA", lat=37.42, lon=-122.08)
    _obs(conn, r, "craigslist", "mix", "p_mix", address="4 D St, Palo Alto, CA", lat=37.42, lon=-122.08)
    _obs(conn, r, "zillow", "zz", "p_mix", address="4 D St, Palo Alto, CA", lat=37.41, lon=-122.09)
    assert addresses.positions_todo(conn) == ["1 A St, Palo Alto, CA"]

    class Http:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, params):
            class R:
                def json(self_):
                    return {"status": "OK", "results": [{
                        "types": ["street_address"], "place_id": "x", "formatted_address": "1 A St, Palo Alto",
                        "geometry": {"location": {"lat": 37.425, "lng": -122.085}, "location_type": "ROOFTOP"},
                        "address_components": [
                            {"long_name": "1", "types": ["street_number"]},
                            {"long_name": "A Street", "types": ["route"]},
                            {"long_name": "Palo Alto", "types": ["locality"]}]}]}
            return R()

    import httpx
    monkeypatch.setattr(httpx, "Client", Http)
    monkeypatch.setattr(settings, "secret", lambda name: "k")
    assert addresses.positions(conn)["calls"] == 1
    assert addresses.positions_todo(conn) == []  # kept: never asked twice
    assert conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = 'geocoding'").fetchone()[0] == 1
    assert tuple(_place(conn, "p_a")) == (37.425, -122.085)
