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
    # the sqft sort: the place's largest stated unit (All) and the goal offer's own sqft (seed: 900 sqft at Brewster)
    assert rwc["max_sqft"] == 900 and rwc["goals"]["lease"]["offer"]["sqft"] == 900
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


def test_verify_conflict_override(client):
    c, _ = client
    (d,) = c.get("/api/verify/batch?kind=conflict").json()["items"]
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
    # the one-at-a-time flow is the sample only: conflicts are paged here
    assert c.get("/api/verify/next").json()["item"]["kind"] != "conflict"


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
    # a set's single points are not in the list's `minutes` (only commute targets and points a goal names); the
    # drawer ranks them
    assert not any(k.startswith("caltrain_stations:") for k in p["minutes"])
    walks = [x["minutes"] for x in c.get(f"/api/places/{p['place_key']}").json()["nearest"]["caltrain_stations|walk"]]
    assert n["minutes"] == min(m for m in walks if m is not None) and n["point"] and n["provider"]


def test_sampling_is_refused_in_the_redacted_view(tmp_path):
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    c = TestClient(create_app(db, redact={}))
    assert c.post("/api/sample/dry_run", json={"method": "cover", "scope": "zone"}).status_code == 403
    assert c.post("/api/sample/run", json={"plan": {}, "confirm_elements": 1}).status_code == 403


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


def test_places_name_the_attribute_each_goal_fails_on(client):
    """Each non-pass reason comes with the attribute it judges, so a card can mark that fact red or amber."""
    c, _ = client
    places = c.get("/api/places").json()
    for p in places:
        for r in p["goals"].values():
            assert len(r["fails"]) == len(r["reasons"])
    rwc = next(p for p in places if "Brewster" in (p["address"] or ""))
    assert rwc["goals"]["lease"]["fails"] == [{"s": "fail", "i": 1, "f": "beds", "v": 2.0}]  # i: the rule's index
    temp = {x["f"]: x for x in rwc["goals"]["temp"]["fails"]}
    assert {k: v for k, v in temp["price_month"].items() if k != "i"} == \
        {"s": "fail", "f": "price_month", "v": 4200.0, "lim": 4000}
    assert temp["furnished"]["v"] is False and temp["private_bath"]["s"] == "needs_check"
    assert temp["commute"]["pt"] == "commute_targets:Downtown Palo Alto" and "bike" in temp["commute"]["modes"]


def test_amenity_presets_from_api_goals_judge_as_amenity_rules(client):
    """The editor's amenity presets (GET /api/goals `amenities`) become amenity rules: an entry matches a preset's
    words, never its exclusions ("Dishwasher" is not laundry, "no parking" is not parking)."""
    from aptfinder.filter.rules import Ctx, evaluate

    c, _ = client
    presets = c.get("/api/goals").json()["amenities"]
    assert set(presets) == {"gym", "in_unit_laundry", "parking", "pets"}

    def has(k, am):
        return evaluate({"type": "amenity", "name": k, **presets[k]}, {"amenities": am}, Ctx())[0]

    am = ["Fitness Center", "Dishwasher", "Washer/Dryer Hookup", "no parking", "Dogs allowed (read from text)"]
    assert {k: has(k, am) for k in presets} == {"gym": "pass", "in_unit_laundry": "fail", "parking": "fail",
                                                "pets": "pass"}
    assert has("in_unit_laundry", ["In-unit laundry (read from text)", "Parking: garage (read from text)"]) == "pass"
    assert has("gym", None) == "needs_check"
    assert "amenities" not in c.get("/api/places").json()[0]
    assert "goals" not in c.get("/api/meta").json()


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


def test_duplicates_batch_answers_pairs_in_order(client):
    """The batch endpoint writes the same links and annotations as the single buttons, in order: a place merged
    away earlier in the batch cannot merge again (reported, nothing written for it); a 'not the same building'
    pair leaves the duplicate list."""
    c, db = client
    places = c.get("/api/places").json()
    showers = next(p for p in places if "Showers" in p["label"])
    other = next(p for p in places if p["place_key"] != showers["place_key"] and "~" not in p["place_key"])
    pk = c.post("/api/links", json={"source": "apartments_com", "source_id": "ac-55102", "place_key": None}).json()["place_key"]
    n = len(c.get("/api/places").json())
    r = c.post("/api/verify/duplicates/batch", json={"items": [
        {"a": pk, "b": showers["place_key"], "into": showers["place_key"]},
        {"a": pk, "b": other["place_key"], "into": other["place_key"]},  # pk has no listings left
        {"a": pk, "b": other["place_key"], "into": "nowhere"},
        {"a": showers["place_key"], "b": other["place_key"]},
    ]}).json()
    assert [x["ok"] for x in r["results"]] == [True, False, False, True]
    assert r["results"][0]["merged"] == 1 and r["results"][1]["error"] == "no listings at this place"
    assert (r["done"], r["failed"]) == (2, 2)
    assert len(c.get("/api/places").json()) == n - 1
    det = c.get(f"/api/places/{showers['place_key']}").json()
    assert "apartments_com" in {l["source"] for l in det["listings"]}
    import sqlite3
    con = sqlite3.connect(db)
    assert con.execute("SELECT place_key, reason FROM links WHERE author = 'user' ORDER BY id DESC LIMIT 1").fetchone() \
        == (showers["place_key"], f"manual merge of {pk}")
    pair = "|".join(sorted((showers["place_key"], other["place_key"])))
    assert con.execute("SELECT comment FROM annotations WHERE entity_type = 'place_pair' AND entity_key = ? "
                       "AND attribute = 'same_building'", (pair,)).fetchall() == [("no",)]
    assert pair not in {e["pair_key"] for e in c.get("/api/verify/duplicates?n=500").json()["items"]}


def test_an_amenity_rating_is_the_buildings_for_every_goal_and_a_later_comment_keeps_it(client):
    """A rating is a fact about the building: every goal's amenity rule reads the latest rating, whatever goal (if
    any) an old row carries, and a comment typed after it does not hide it."""
    c, db = client
    pk = c.get("/api/places").json()[0]["place_key"]
    gym = {"label": "gym", "rules": [{"type": "amenity", "name": "gym"}], "nice": []}
    conn = connect(db)
    append_annotation(conn, "user", "place", pk, "gym", goal_id="temp", rating=2)  # an old row labelled by goal
    assert c.post("/api/goals/preview", json=gym).json()["places"][pk]["status"] == "fail"
    assert c.post("/api/annotations", json={"entity_key": pk, "attribute": "gym", "rating": 5}).status_code == 200
    assert c.post("/api/annotations", json={"entity_key": pk, "attribute": "gym", "comment": "new gym"}).status_code == 200
    res = c.post("/api/goals/preview", json=gym).json()["places"][pk]
    assert res["status"] == "pass"
    assert c.get("/api/places").json()[0]["place_key"] == pk  # the card and drawer read the same rows
    assert [a["goal_id"] for a in c.get(f"/api/places/{pk}").json()["annotations"]] == ["temp", None, None]


def test_a_preview_fail_names_its_rule_index(client):
    """The goal editor's draft diff names the edited rule that cuts a place: each fail carries `i`, the rule's index
    in the goal's must-haves, and the preview's status and reasons are all it needs (no second request)."""
    c, _ = client
    loose = {"label": "d", "rules": [{"type": "min", "field": "beds", "value": 0},
                                     {"type": "max", "field": "price_month", "value": 100000}], "nice": []}
    tight = {**loose, "rules": [loose["rules"][0], {"type": "max", "field": "price_month", "value": 1}]}
    before = c.post("/api/goals/preview", json=loose).json()["places"]
    after = c.post("/api/goals/preview", json=tight).json()["places"]
    cut = [pk for pk, r in before.items() if r["status"] in ("pass", "needs_check") and after[pk]["status"] == "fail"]
    assert cut
    for pk in cut:
        r = after[pk]
        assert [x["i"] for x in r["fails"] if x["s"] == "fail"] == [1]
        assert any(t.startswith("price_month <= 1") for t in r["reasons"]), r["reasons"]


def test_a_restriction_answer_must_be_one_of_the_rules_values(client):
    c, _ = client
    pk = c.get("/api/places").json()[0]["place_key"]
    post = lambda comment: c.post("/api/annotations", json={"entity_key": pk, "attribute": "Restriction",
                                                            "comment": comment})
    r = post("students welcome")
    assert r.status_code == 400 and "none, income, senior, veterans, student, employer, other" in r.json()["detail"]
    assert post(None).status_code == 400
    assert post(" Income ").status_code == 200
    a = c.get(f"/api/places/{pk}").json()["annotations"][-1]
    assert (a["attribute"], a["comment"]) == ("restriction", "income")
    assert c.post("/api/annotations", json={"entity_key": pk, "attribute": "gym", "comment": "students"}).status_code == 200


def test_api_goals_lists_the_operators(client):
    c, _ = client
    names = c.get("/api/goals").json()["operators"]  # a not_operator rule's choices (config/operators.toml)
    assert "Blueground" in names and "MidPen Housing" in names


def test_a_place_with_nothing_for_rent_is_no_listings_in_list_and_preview(client):
    """A place whose only row is a site_info row (a community website, nothing for rent) has a result for every goal:
    no_listings, why 'none', with its reason, in /api/places and in a draft preview; never a missing goal that the
    page would count as needs check."""
    from aptfinder.store import finish_run, start_run
    from aptfinder.store import append_observation

    c, db = client
    conn = connect(db)
    r = start_run(conn, "intake", "direct")
    append_observation(conn, r, "direct", "site:1", {"address": "9 Nowhere Ln, Palo Alto, CA 94301",
                                                     "kind": "site_info", "property_name": "Empty Towers"})
    finish_run(conn, r, counts={"n": 1})
    identity.run(conn)
    places = c.get("/api/places").json()
    p = next(p for p in places if p["label"] == "Empty Towers")
    assert {g: (v["status"], v["why"]) for g, v in p["goals"].items()} == \
        {g: ("no_listings", "none") for g in p["goals"]} and p["goals"]
    assert p["goals"]["lease"]["reasons"] == ["no offers known"]
    assert all(set(x["goals"]) == set(p["goals"]) for x in places)  # every place, every goal
    goal = next(g for g in c.get("/api/goals").json()["goals"] if g["id"] == "lease")
    prev = c.post("/api/goals/preview", json={k: goal[k] for k in ("label", "rules", "nice", "people")}).json()
    assert prev["places"][p["place_key"]]["status"] == "no_listings"
    assert set(prev["places"]) == {x["place_key"] for x in places}


def test_a_stop_added_to_a_point_file_shows_as_the_nearest(tmp_path, monkeypatch):
    # editing a point file is a config edit: the cached results are out of date at once, and the place's new
    # nearest stop shows once its estimate is in (trips.estimate_missing, which the served app runs on the change)
    import shutil

    from aptfinder import settings, trips

    cfg = tmp_path / "cfg"
    shutil.copytree(settings.ROOT / "config.example", cfg)
    monkeypatch.setattr(settings, "CONFIG_DIR", cfg)
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    conn = connect(db)
    pk, lat, lon = conn.execute("SELECT place_key, lat, lon FROM places WHERE lat IS NOT NULL").fetchone()
    stops = cfg / "points" / "google_bus_stops.csv"
    stops.write_text(f"set,name,lat,lon\ngoogle_bus_stops,Old,{lat + 0.01},{lon}\n")
    trips.estimate_missing(conn, str(db))
    c = TestClient(create_app(db))
    near = lambda: c.get(f"/api/places/{pk}").json()["nearest"]["google_bus_stops|walk"][0]["point"]  # noqa: E731
    assert near() == "Old"
    stops.write_text(stops.read_text() + f"google_bus_stops,New,{lat + 0.001},{lon}\n")
    assert trips.missing_estimates(conn) and trips.estimate_missing(conn, str(db))["new_trips"]
    assert near() == "New"
