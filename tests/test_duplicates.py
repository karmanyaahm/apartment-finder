"""verify.duplicate_queue: places that are probably one building, and the two answers that clear a pair."""

from __future__ import annotations

from fastapi.testclient import TestClient

from aptfinder import identity
from aptfinder.store import append_annotation, append_observation, connect, init_db, start_run
from aptfinder.verify import duplicate_queue, pair_key
from aptfinder.web.app import create_app


def _db(path):
    conn = connect(path)
    init_db(conn)
    r = start_run(conn, "intake", "test")

    def obs(source, sid, **data):
        append_observation(conn, r, source, sid, {"beds": 2, **data}, price=3000, price_basis="month")

    # address kind: one street written two ways, same number, 20 m apart
    obs("zillow", "z1", address="999 W Evelyn Ter, Sunnyvale, CA 94086", lat=37.3800, lon=-122.0300)
    obs("zumper", "u1", address="999 Evelyn Ter, Sunnyvale, CA 94086", lat=37.3802, lon=-122.0300)
    # website kind: two addresses of one complex that name the same own website
    obs("apartments_com", "a1", address="100 Whisman Station Dr, Mountain View, CA", lat=37.3900, lon=-122.0600,
        property_name="Central Park", extra={"website": "https://www.liveatcentralpark.com/"})
    obs("craigslist", "c1", address="200 Hope St, Mountain View, CA", lat=37.3920, lon=-122.0600,
        extra={"website": "liveatcentralpark.com"})
    # name kind: the same name at two addresses 100 m apart
    obs("apartments_com", "a2", address="1 Oak Ave, Menlo Park, CA", lat=37.4500, lon=-122.1800,
        property_name="Sharon Grove Apartments")
    obs("craigslist", "c2", address="9 Elm St, Menlo Park, CA", lat=37.4509, lon=-122.1800,
        property_name="The Sharon Grove")
    # the same name far apart is not a pair
    obs("zillow", "z3", address="5 Pine St, San Jose, CA", lat=37.3000, lon=-121.9000, property_name="Sharon Grove")
    identity.run(conn)
    return conn


def _kinds(q):
    return {frozenset(r["kind"] for r in e["reasons"]) for e in q}


def test_queue_finds_address_website_and_name_pairs(tmp_path):
    conn = _db(tmp_path / "t.db")
    q = duplicate_queue(conn)
    assert _kinds(q) == {frozenset({"address"}), frozenset({"website"}), frozenset({"name"})}
    web = next(e for e in q if e["reasons"][0]["kind"] == "website")
    assert web["reasons"][0]["detail"] == "liveatcentralpark.com" and web["metres"] < 300
    assert q[0]["reasons"][0]["kind"] == "website"  # strongest evidence first
    assert all(p["n_listings"] == 1 for e in q for p in e["places"])


def test_not_the_same_building_removes_the_pair(tmp_path):
    conn = _db(tmp_path / "t.db")
    q = duplicate_queue(conn)
    name = next(e for e in q if e["reasons"][0]["kind"] == "name")
    assert name["pair_key"] == pair_key(name["b"], name["a"])
    append_annotation(conn, "user", "place_pair", name["pair_key"], "same_building", comment="no")
    assert name["pair_key"] not in {e["pair_key"] for e in duplicate_queue(conn)}
    assert len(duplicate_queue(conn)) == len(q) - 1


def test_merge_removes_the_pair_and_status_counts_it(tmp_path):
    db = tmp_path / "t.db"
    _db(db).close()
    c = TestClient(create_app(db))
    d = c.get("/api/verify/duplicates").json()
    assert d["total"] == 3 and c.get("/api/status").json()["queues"]["duplicates"] == 3
    e = next(e for e in d["items"] if e["reasons"][0]["kind"] == "address")
    assert all(p["label"] for p in e["places"])
    assert c.post(f"/api/places/{e['a']}/merge", json={"into": e["b"]}).status_code == 200
    d2 = c.get("/api/verify/duplicates").json()
    assert d2["total"] == 2 and e["pair_key"] not in {x["pair_key"] for x in d2["items"]}
    n = d2["items"][0]
    assert c.post("/api/annotations", json={"entity_type": "place_pair", "entity_key": n["pair_key"],
                                            "attribute": "same_building", "comment": "no"}).status_code == 200
    assert c.get("/api/status").json()["queues"]["duplicates"] == 1
