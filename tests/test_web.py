from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aptfinder import identity
from aptfinder.store import connect, init_db
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
