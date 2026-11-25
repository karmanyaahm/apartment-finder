from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from aptfinder import identity
from aptfinder.store import append_annotation, connect, init_db
from aptfinder.store.seed import seed_db
from aptfinder.web.app import create_app


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    return TestClient(create_app(db)), db


def test_places_and_detail(client):
    c, _ = client
    places = c.get("/api/places").json()
    assert len(places) == 4
    by_label = {p["label"]: p for p in places}
    showers = next(p for p in places if "Showers" in p["label"])
    assert showers["goals"]["lease"]["status"] == "pass"
    rwc = next(p for p in places if "Brewster" in (p["address"] or ""))
    assert rwc["goals"]["lease"]["status"] == "fail" and rwc["goals"]["temp"]["status"] == "fail"
    assert any(p["goals"]["temp"]["status"] == "pass" for p in places)
    d = c.get(f"/api/places/{showers['place_key']}").json()
    assert {l["source"] for l in d["listings"]} == {"zillow", "apartments_com"}
    z = next(l for l in d["listings"] if l["source"] == "zillow")
    assert [h["price"] for h in z["history"]] == [4900, 4710]
    assert d["matrix"] and d["nearest"]
    assert by_label  # labels exist
    assert c.get("/api/places/nope").status_code == 404


def test_rating_appends_annotation_and_survives_rederive(client):
    c, db = client
    pk = c.get("/api/places").json()[0]["place_key"]
    assert c.post("/api/annotations", json={"entity_key": pk, "attribute": "overall", "rating": 4}).status_code == 200
    assert c.post("/api/annotations", json={"entity_key": pk, "attribute": "overall", "rating": 9}).status_code == 400
    # re-run every derive step: init (views recreated) and identity
    conn = connect(db)
    init_db(conn)
    identity.run(conn)
    p = next(p for p in c.get("/api/places").json() if p["place_key"] == pk)
    assert p["rating"] == 4
    assert conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 1


def test_verify_flow_conflict_first_and_override(client):
    c, _ = client
    d = c.get("/api/verify/next").json()
    assert d["item"]["kind"] == "conflict"
    assert set(d["fields"]) == {"price", "baths", "available_from"}
    apt = next(o for o in d["observations"] if o["source"] == "apartments_com")
    r = c.post("/api/checks", json={"queue": "conflict", "observation_id": apt["obs_id"], "field": "baths",
                                    "verdict": "wrong", "true_value": "2"})
    assert r.status_code == 200
    # wrong without a value is refused; stored value is taken from the db, not the client
    assert c.post("/api/checks", json={"observation_id": apt["obs_id"], "field": "baths",
                                       "verdict": "wrong"}).status_code == 400
    places = c.get("/api/places").json()
    showers = next(p for p in places if "Showers" in p["label"])
    det = c.get(f"/api/places/{showers['place_key']}").json()
    a = next(l for l in det["listings"] if l["source"] == "apartments_com")
    assert a["fields"]["baths"] == 2 and "baths" in a["overridden"]
    status = c.get("/api/status").json()
    assert status["accuracy"][0]["source"] == "apartments_com"


def test_verify_batch_of_conflicts(client):
    c, _ = client
    d = c.get("/api/verify/batch?kind=conflict&skip=0&n=10").json()
    assert (d["total"], d["actionable"], d["pending"]) == (1, 1, 0)
    (e,) = d["items"]
    assert e["item"]["kind"] == "conflict" and e["item"]["pending_render"] is False
    assert set(e["fields"]) == {"price", "baths", "available_from"}
    assert "Showers" in e["label"] and {o["source"] for o in e["observations"]} == {"zillow", "apartments_com"}
    assert all("baths" in o["fields"] for o in e["observations"])
    assert all(o["checked"] == {} for o in e["observations"])
    z = next(o for o in e["observations"] if o["source"] == "zillow")
    c.post("/api/checks", json={"queue": "conflict", "observation_id": z["obs_id"], "field": "baths",
                                "verdict": "correct"})
    (e,) = c.get("/api/verify/batch?kind=conflict").json()["items"]  # partly checked: still open, verdict shown
    assert next(o for o in e["observations"] if o["source"] == "zillow")["checked"] == {"baths": "correct"}
    assert c.get("/api/verify/batch?kind=conflict&skip=10").json()["items"] == []
    assert c.get("/api/verify/batch?kind=sample").status_code == 400
    q = c.get("/api/status").json()["queues"]
    assert (q["conflict"], q["conflict_pending"]) == (1, 0)
    # the one-at-a-time flow without conflicts starts at the rest of the queue
    assert c.get("/api/verify/next?conflicts=false").json()["item"]["kind"] != "conflict"


def test_trip_check(client):
    c, db = client
    from aptfinder.store import append_trip

    conn = connect(db)
    pk = c.get("/api/places").json()[0]["place_key"]
    append_trip(conn, pk, "commute_targets:Meta HQ", "drive", 14.0, 9000, [], "weekday_0830", "google")
    skip = 0
    d = c.get("/api/verify/next").json()
    while d["item"]["kind"] != "trip":
        skip += 1
        d = c.get(f"/api/verify/next?skip={skip}").json()
    t = d["trip"]
    assert t["origin"] and t["dest"]
    r = c.post("/api/checks", json={"queue": "sample", "trip_id": t["id"], "field": "minutes",
                                    "verdict": "wrong", "true_value": "12"})
    assert r.status_code == 200
    acc = c.get("/api/status").json()["accuracy"]
    assert any(a["source"] == "trip:google:drive" and a["n"] == 1 for a in acc)


def test_split(client):
    c, _ = client
    places = c.get("/api/places").json()
    showers = next(p for p in places if "Showers" in p["label"])
    r = c.post("/api/links", json={"source": "apartments_com", "source_id": "ac-55102", "place_key": None})
    assert r.status_code == 200
    assert len(c.get("/api/places").json()) == 5
    det = c.get(f"/api/places/{showers['place_key']}").json()
    assert {l["source"] for l in det["listings"]} == {"zillow"}


def test_on_demand_estimate_routing(client, monkeypatch):
    c, _ = client
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    pk = c.get("/api/places").json()[0]["place_key"]
    r = c.post(f"/api/trips/{pk}?provider=estimate")
    assert r.status_code == 200 and r.json()["provider"] == "estimate"


def test_route_shows_new_trips_at_once_and_never_falls_back_silently(tmp_path, monkeypatch):
    # trips count as scraped rows, which the cache may reuse for SCRAPE_REFRESH_S: the user's own routing must
    # still show in the drawer right away (it showed the old estimates for two minutes)
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=False)
    c = TestClient(create_app(db))
    pk = c.get("/api/places").json()[0]["place_key"]
    assert c.get(f"/api/places/{pk}").json()["matrix"] == {}  # warms the cache
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    r = c.post(f"/api/trips/{pk}")
    assert r.status_code == 400 and "GOOGLE_MAPS_API_KEY" in r.json()["detail"]
    assert c.post(f"/api/trips/{pk}?provider=bogus").status_code == 400
    r = c.post(f"/api/trips/{pk}?provider=estimate").json()
    assert r["new_trips"] > 0 and r["provider"] == "estimate"
    matrix = c.get(f"/api/places/{pk}").json()["matrix"]
    assert matrix and all(v["provider"] == "estimate" for v in matrix.values())


def test_static_and_sw(client):
    c, _ = client
    assert "Apartment Finder" in c.get("/").text
    sw = c.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert c.get("/static/app.js").status_code == 200


def test_redacted_view_replaces_strings_and_refuses_writes(tmp_path):
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    c = TestClient(create_app(db, redact={"Downtown Palo Alto": "Office", "Showers": "S████"}))
    assert "commute_targets:Office" in c.get("/api/meta").text
    body = c.get("/api/places").text
    assert "Showers" not in body and "S████" in body and "Downtown Palo Alto" not in body
    assert c.post("/api/annotations", json={}).status_code == 403


def test_places_carry_a_cover_photo_for_the_map(client):
    c, _ = client
    places = c.get("/api/places").json()
    assert all("photo" in p for p in places)
    for p in places:
        d = c.get(f"/api/places/{p['place_key']}").json()
        assert p["photo"] == (d["photos"][0]["url"] if d["photos"] else None)


def test_index_versions_its_assets(client):
    c, _ = client
    r = c.get("/")
    assert r.headers["cache-control"] == "no-cache"
    assert 'static/app.js?v=' in r.text and 'static/style.css?v=' in r.text


def test_drawer_cache_sees_new_writes_at_once(client):
    c, _ = client
    pk = c.get("/api/places").json()[0]["place_key"]
    assert c.get(f"/api/places/{pk}").json()["annotations"] == []  # warms the cache
    c.post("/api/annotations", json={"entity_key": pk, "attribute": "overall", "rating": 4})
    assert [a["rating"] for a in c.get(f"/api/places/{pk}").json()["annotations"]] == [4]


def test_raw_is_fetched_per_listing_on_demand(client):
    c, _ = client
    pk = c.get("/api/places").json()[0]["place_key"]
    d = c.get(f"/api/places/{pk}").json()
    assert all("raw" not in l for l in d["listings"])  # not shipped with the drawer any more
    assert c.get("/api/raw", params={"source": "nope", "source_id": "x"}).status_code == 404


def test_places_carry_the_nearest_point_of_each_set(client):
    c, db = client
    places = c.get("/api/places").json()
    p = next(p for p in places if "caltrain_stations|walk" in p["nearest"])
    assert not any(k.startswith("commute_targets|") for k in p["nearest"])  # targets are in `minutes`, one by one
    n = p["nearest"]["caltrain_stations|walk"]
    walks = [v["minutes"] for k, v in p["minutes"].items() if k.startswith("caltrain_stations:") and k.endswith("|walk")]
    assert n["minutes"] == min(m for m in walks if m is not None) and n["point"] and n["provider"]


def _wait_for_job(c):
    import time

    for _ in range(200):
        s = c.get("/api/compute/status").json()
        if s["state"] in ("done", "error"):
            return s
        time.sleep(0.05)
    raise AssertionError("compute job did not finish")


def test_compute_one_attribute_estimate(client):
    c, db = client
    keys = [p["place_key"] for p in c.get("/api/places").json()]
    spec = {"point": "commute_targets:Meta HQ", "mode": "drive", "provider": "estimate", "place_keys": keys}
    d = c.post("/api/compute/dry_run", json=spec).json()
    assert d["places"] >= 1 and d["would_call"] == 0 and d["est_cost_usd"] == 0
    assert c.post("/api/compute/dry_run", json={**spec, "point": "commute_targets:Nowhere"}).status_code == 400
    conn = connect(db)
    before = conn.execute("SELECT MAX(id) FROM trips").fetchone()[0]
    # a mode the set does not list (point_sets.toml: grocery_chains walks only), so every row is new
    r = c.post("/api/compute/run", json={**spec, "point": "set:grocery_chains", "mode": "bike"})
    assert r.status_code == 200
    s = _wait_for_job(c)
    assert s["state"] == "done" and s["result"]["new_trips"] > 0
    rows = conn.execute("SELECT DISTINCT substr(point_id, 1, instr(point_id, ':') - 1), mode, provider FROM trips "
                        "WHERE id > ?", (before,)).fetchall()
    assert [tuple(r) for r in rows] == [("grocery_chains", "bike", "estimate")]  # only that attribute
    assert "grocery_chains|bike" in next(p for p in c.get("/api/places").json() if p["nearest"])["nearest"]


def test_compute_one_attribute_google_needs_the_dry_run_and_stops_at_its_count(client, monkeypatch):
    import sys

    from aptfinder import trips

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from test_commute import FakeSource

    c, db = client
    keys = [p["place_key"] for p in c.get("/api/places").json()]
    spec = {"point": "commute_targets:Meta HQ", "mode": "drive", "provider": "google", "place_keys": keys}
    d = c.post("/api/compute/dry_run", json=spec).json()
    assert d["would_call"] >= 1 and d["by_mode"] == {"drive": d["would_call"]}
    assert d["google_usage_now"]["calls"] == 0  # a dry run calls nothing
    assert c.post("/api/compute/run", json=spec).status_code == 400  # no confirm: refused
    fake = FakeSource()
    fake.name = "google"
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test")
    monkeypatch.setattr(trips, "make_source", lambda provider, max_calls=200, meter_db=None:
                        trips.MeteredSource(fake, max_calls, meter_db))
    r = c.post("/api/compute/run", json={**spec, "confirm_calls": 1})  # fewer than needed: stops at 1 call
    assert r.status_code == 200
    s = _wait_for_job(c)
    assert len(fake.calls) == 1 and s["state"] == "error" and "cap of 1" in s["result"]["error"]
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = 'google'").fetchone()[0] == 1


def test_compute_is_refused_in_the_redacted_view(tmp_path):
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    c = TestClient(create_app(db, redact={}))
    spec = {"point": "commute_targets:Meta HQ", "mode": "drive", "scope": "zone"}
    assert c.post("/api/compute/dry_run", json=spec).status_code == 403
    assert c.post("/api/compute/run", json=spec).status_code == 403


def test_merge_a_duplicate_place(client):
    """Split a listing off into its own place, then merge it back from the drawer: the listings move as user
    links and keep their own unit."""
    c, _ = client
    places = c.get("/api/places").json()
    showers = next(p for p in places if "Showers" in p["label"])
    pk = c.post("/api/links", json={"source": "apartments_com", "source_id": "ac-55102", "place_key": None}).json()["place_key"]
    n = len(c.get("/api/places").json())
    assert c.post(f"/api/places/{pk}/merge", json={"into": pk}).status_code == 400
    r = c.post(f"/api/places/{pk}/merge", json={"into": showers["place_key"]})
    assert r.status_code == 200 and r.json()["merged"] == 1
    assert len(c.get("/api/places").json()) == n - 1
    det = c.get(f"/api/places/{showers['place_key']}").json()
    assert "apartments_com" in {l["source"] for l in det["listings"]}
    assert all(l["unit_key"].startswith(showers["place_key"]) for l in det["listings"])


def test_compute_over_20_google_calls_needs_the_counts_typed(client, monkeypatch):
    """More than 20 Google calls: the run is refused unless the calls left this month are typed in; the search
    candidates scope works, and every run is in the log."""
    from aptfinder import trips

    c, _ = client
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test")
    real = trips.google_usage
    monkeypatch.setattr(trips, "google_usage", lambda conn, month=None: {**real(conn, month), "calls": 1571})
    keys = [p["place_key"] for p in c.get("/api/places").json()]
    spec = {"point": "commute_targets:Meta HQ", "mode": "drive", "provider": "google", "place_keys": keys,
            "confirm_calls": 25}
    r = c.post("/api/compute/run", json=spec)
    assert r.status_code == 400 and "type the calls left" in r.json()["detail"]
    r = c.post("/api/compute/run", json={**spec, "confirm_left": 1})  # the wrong number
    assert r.status_code == 400
    d = c.post("/api/compute/dry_run", json={"point": "commute_targets:Meta HQ", "mode": "drive",
                                             "provider": "estimate", "scope": "candidates"})
    assert d.status_code in (200, 400)  # 400 only when the seed has no candidate in a walk zone
    c.post("/api/compute/run", json={"point": "commute_targets:Meta HQ", "mode": "drive", "provider": "estimate",
                                     "place_keys": keys})
    _wait_for_job(c)
    log = c.get("/api/compute/log").json()
    assert log and log[0]["source"] == "estimate" and log[0]["params"]["only"] == ["commute_targets:Meta HQ", "drive"]


def test_places_name_the_attribute_each_goal_fails_on(client):
    """Each non-pass reason comes with the attribute it judges, so a card can mark that fact red or amber."""
    c, _ = client
    places = c.get("/api/places").json()
    for p in places:
        for r in p["goals"].values():
            assert len(r["fails"]) == len(r["reasons"])
    rwc = next(p for p in places if "Brewster" in (p["address"] or ""))
    assert rwc["goals"]["lease"]["fails"] == [{"s": "fail", "f": "beds", "v": 2.0}]
    temp = {x["f"]: x for x in rwc["goals"]["temp"]["fails"]}
    assert temp["price_month"] == {"s": "fail", "f": "price_month", "v": 4200.0, "lim": 4000}
    assert temp["furnished"]["v"] is False and temp["private_bath"]["s"] == "needs_check"
    assert temp["commute"]["pt"] == "commute_targets:Downtown Palo Alto" and "bike" in temp["commute"]["modes"]


def test_places_carry_amenity_flags_for_the_require_checkboxes(client):
    import json

    from aptfinder.filter import amenity_flags

    assert amenity_flags(None) == {"gym": None, "in_unit_laundry": None, "parking": None, "pets": None}
    f = amenity_flags(["Fitness Center", "Dishwasher", "Washer/Dryer Hookup", "no parking", "Dogs allowed (read from text)"])
    assert f == {"gym": True, "in_unit_laundry": False, "parking": False, "pets": True}
    assert amenity_flags(["In-unit laundry (read from text)", "Parking: garage (read from text)"])["in_unit_laundry"]
    c, db = client
    places = c.get("/api/places").json()
    assert all(set(p["amenities"]) == {"gym", "in_unit_laundry", "parking", "pets"} for p in places)
    conn = connect(db)
    for p in places:
        listed = [a for (am,) in conn.execute("SELECT amenities FROM listings WHERE place_key = ?", (p["place_key"],))
                  if am for a in json.loads(am)]
        assert p["amenities"] == amenity_flags(listed)


def test_compute_job_says_it_waits_for_the_database_lock(client):
    import sqlite3
    import time

    c, db = client
    keys = [p["place_key"] for p in c.get("/api/places").json()]
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")  # another job writing
    spec = {"point": "set:grocery_chains", "mode": "bike", "provider": "estimate", "place_keys": keys}
    assert c.post("/api/compute/run", json=spec).status_code == 200
    for _ in range(100):
        s = c.get("/api/compute/status").json()
        if s.get("note"):
            break
        time.sleep(0.05)
    assert s["state"] == "running" and s["note"] == "waiting for the database (another job is writing)"
    holder.execute("COMMIT")
    s = _wait_for_job(c)
    assert s["state"] == "done" and "note" not in s


def test_verify_batch_filters_by_goal_and_zone_and_counts_cheap(client):
    c, db = client
    from aptfinder.store import append_observation, start_run

    base = c.get("/api/verify/batch?kind=conflict").json()
    assert base["hidden"] == {"irrelevant": 0, "settled": 0, "same": 0, "zone": 0} and base["all_total"] == base["total"]
    n0 = c.get("/api/status").json()["queues"]["conflict"]
    conn = connect(db)
    r = start_run(conn, "intake", "zumper")
    append_observation(conn, r, "zumper", "cheap1", {"address": "425 Oak Ave #A, Redwood City, CA 94061", "beds": 5,
                                                      "baths": 3.5, "lat": 37.48, "lon": -122.23},
                       price=1600, price_basis="month")
    identity.run(conn)
    c = TestClient(create_app(db))  # a fresh app: scraped rows refresh the cache only every SCRAPE_REFRESH_S
    d = c.get("/api/verify/batch?kind=conflict").json()
    assert d["cheap"] == 1 and d["total"] == base["total"] + 1
    (e,) = [e for e in d["items"] if e["item"].get("cheap")]
    assert e["fields"] == ["price", "beds"] and e["item"]["per_bed"] == 320
    assert "$320 < $1,000" in e["item"]["reason"]
    assert c.get("/api/status").json()["queues"]["conflict"] == n0 + 1
    # the lease goal (3bd) cannot pass a 5bd whatever its price: hidden as settled
    g = c.get("/api/verify/batch?kind=conflict&goal=lease").json()
    assert g["all_total"] == d["total"] and g["total"] + sum(g["hidden"].values()) == g["all_total"]
    assert not any(x["item"].get("cheap") for x in g["items"]) and g["hidden"]["settled"] >= 1
    z = c.get("/api/verify/batch?kind=conflict&zone=walk").json()
    assert z["total"] + z["hidden"]["zone"] == z["all_total"]
    assert c.get("/api/verify/batch?kind=conflict&goal=nope").status_code == 400
    assert c.get("/api/verify/batch?kind=conflict&zone=far").status_code == 400
