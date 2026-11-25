"""Goals edited in the dashboard: goals.toml is the seed, annotation rows overlay it (filter.goals)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aptfinder.config import load_goals
from aptfinder.filter import evaluate_all
from aptfinder.filter.goals import effective_goals
from aptfinder.filter.rules import FAIL, PASS, Ctx, evaluate, validate_rule
from aptfinder.store import connect
from aptfinder.store.seed import seed_db
from aptfinder.web.app import create_app

MODES = ["walk", "bike", "transit", "drive"]


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    return TestClient(create_app(db)), db


def statuses(c, goal):
    return {p["place_key"]: p["goals"][goal]["status"] for p in c.get("/api/places").json() if goal in p["goals"]}


def test_no_edits_means_goals_toml_exactly(client):
    _, db = client
    conn = connect(db)
    assert effective_goals(conn) == load_goals()
    assert evaluate_all(conn) == evaluate_all(conn, load_goals())


def test_editing_a_goal_changes_its_results(client):
    c, db = client
    before = statuses(c, "lease")
    g = next(g for g in c.get("/api/goals").json()["goals"] if g["id"] == "lease")
    assert g["origin"] == "goals.toml"
    # drop every hard rule: nothing can fail any more
    r = c.put("/api/goals/lease", json={"label": "Lease, anything", "rules": [], "nice": g["nice"]})
    assert r.status_code == 200
    after = statuses(c, "lease")
    assert set(after.values()) == {"pass"} and after != before
    g = next(g for g in c.get("/api/goals").json()["goals"] if g["id"] == "lease")
    assert g["origin"] == "edited" and g["label"] == "Lease, anything"
    assert c.get("/api/meta").json()["goals"][1] == {"id": "lease", "label": "Lease, anything"}
    conn = connect(db)  # stored as one append-only annotation row
    assert conn.execute("SELECT COUNT(*) FROM annotations WHERE entity_type = 'goal'").fetchone()[0] == 1


def test_new_goal_appears_in_evaluate_all(client):
    c, db = client
    body = {"label": "3bd near Meta", "rules": [
        {"type": "min", "field": "beds", "value": 3},
        {"type": "commute_any", "target": "commute_targets:Meta HQ", "options": [{"mode": "drive", "max": 25}]},
        {"type": "amenity", "name": "gym", "match": ["gym", "fitness"]}], "nice": []}
    r = c.post("/api/goals", json=body)
    assert r.status_code == 200 and r.json()["goal"] == "3bd_near_meta"
    assert c.post("/api/goals", json=body).status_code == 409  # the id exists
    res = evaluate_all(connect(db))
    assert list(res["places"]) == ["temp", "lease", "3bd_near_meta"]
    assert res["places"]["3bd_near_meta"]
    assert all("3bd_near_meta" in p["goals"] for p in c.get("/api/places").json())


def test_delete_is_a_tombstone(client):
    c, db = client
    assert c.delete("/api/goals/temp").status_code == 200
    assert [g["id"] for g in c.get("/api/goals").json()["goals"]] == ["lease"]
    assert c.get("/api/goals").json()["deleted"] == ["temp"]
    assert "temp" not in evaluate_all(connect(db))["places"]
    assert c.delete("/api/goals/temp").status_code == 404
    # the id can be used again; the earlier rows stay
    assert c.post("/api/goals", json={"id": "temp", "label": "again", "rules": [], "nice": []}).status_code == 200
    rows = connect(db).execute("SELECT comment FROM annotations WHERE entity_type = 'goal' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ['{"deleted": true}', '{"label": "again", "nice": [], "rules": []}']


@pytest.mark.parametrize("rule,msg", [
    ({"type": "nope"}, "unknown rule type"),
    ({"type": "max", "field": "price_per_bed"}, "missing value"),
    ({"type": "max", "field": "price_per_bed", "value": "cheap"}, "must be a number"),
    ({"type": "max", "field": "address", "value": 3}, "field must be one of"),
    ({"type": "equals", "field": "furnished", "value": "yes"}, "furnished must be one of"),
    ({"type": "commute_any", "target": "commute_targets:Nowhere", "options": [{"mode": "walk", "max": 5}]},
     "unknown point"),
    ({"type": "commute_any", "target": "commute_targets:Meta HQ", "options": [{"mode": "teleport", "max": 5}]},
     "options must be"),
    ({"type": "near", "set": "caltrain_stations", "mode": "walk", "max": -1}, "max must be"),
    ({"type": "size_options", "options": []}, "non-empty list"),
    ({"type": "dates_cover", "start": "2027-02-01", "end": "2027-01-01"}, "start is after end"),
    ({"type": "amenity", "name": "gym", "colour": "red"}, "unknown key colour"),
])
def test_invalid_rules_are_refused(client, rule, msg):
    c, _ = client
    r = c.post("/api/goals", json={"label": "x", "rules": [rule], "nice": []})
    assert r.status_code == 400 and msg in r.json()["detail"], r.json()
    assert c.put("/api/goals/lease", json={"label": "x", "rules": [], "nice": [rule]}).status_code == 400
    assert c.post("/api/goals/preview", json={"label": "x", "rules": [rule]}).status_code == 400
    assert c.post("/api/goals", json={"label": "", "rules": []}).status_code == 400
    assert c.post("/api/goals", json={"id": "all", "label": "x"}).status_code == 400


def test_goals_toml_rules_validate(client):
    c, _ = client
    for g in c.get("/api/goals").json()["goals"]:
        assert c.post("/api/goals/preview", json=g).status_code == 200


def test_preview_writes_nothing_and_matches_saved(client):
    c, db = client
    g = next(g for g in c.get("/api/goals").json()["goals"] if g["id"] == "lease")
    prev = c.post("/api/goals/preview", json=g).json()["places"]
    assert {pk: v["status"] for pk, v in prev.items()} == statuses(c, "lease")
    assert connect(db).execute("SELECT COUNT(*) FROM annotations").fetchone()[0] == 0


def test_goal_rows_only_through_the_goals_api(client):
    c, _ = client
    r = c.post("/api/annotations", json={"entity_type": "goal", "entity_key": "x", "attribute": "definition",
                                         "comment": "{}"})
    assert r.status_code == 400


def test_goal_writes_refused_in_the_redacted_view(tmp_path):
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    c = TestClient(create_app(db, redact={}))
    assert c.get("/api/goals").status_code == 200
    assert c.post("/api/goals", json={"label": "x", "rules": []}).status_code == 403
    assert c.put("/api/goals/lease", json={"label": "x", "rules": []}).status_code == 403
    assert c.delete("/api/goals/lease").status_code == 403


def test_min_rule_and_amenity_exclusions():
    row = {"beds": 2, "amenities": ["No parking", "Dishwasher"]}
    assert evaluate({"type": "min", "field": "beds", "value": 3}, row, Ctx())[0] == FAIL
    assert evaluate({"type": "min", "field": "beds", "value": 2}, row, Ctx())[0] == PASS
    park = {"type": "amenity", "name": "parking", "match": ["parking"], "exclude": ["no parking"]}
    assert evaluate(park, row, Ctx())[0] == FAIL
    assert validate_rule(park, modes=MODES, points=set(), sets=set()) is None


# ---------------------------------------------------------------- net commute
ROOMMATES = [
    {"type": "commute_any", "target": "commute_targets:Googleplex", "options": [{"mode": "drive", "max": 25}]},
    {"type": "commute_any", "target": "commute_targets:Meta HQ",
     "options": [{"mode": "drive", "max": 30}, {"mode": "bike", "max": 30}]},
    {"type": "near", "set": "caltrain_stations", "mode": "walk", "max": 20},
]


def _trip(m, provider="google"):
    return {"minutes": m, "provider": provider}


def test_commute_terms_use_each_rules_pair():
    from aptfinder.filter.rules import commute_terms
    trips = {("commute_targets:Googleplex", "drive"): _trip(20),
             ("commute_targets:Meta HQ", "drive"): _trip(31), ("commute_targets:Meta HQ", "bike"): _trip(12, "estimate"),
             ("commute_targets:Meta HQ", "walk"): _trip(1)}  # walk is not an option of that rule: ignored
    nearest = {("caltrain_stations", "walk"): _trip(9)}
    assert commute_terms(ROOMMATES, trips, nearest) == [(20, False), (12, True), (9, False)]
    # unknown stays unknown, never 0
    assert commute_terms(ROOMMATES, {}, {}) == [(None, False)] * 3


@pytest.mark.parametrize("agg,want", [("total", 60), ("mean", 15), ("median", 12.5), ("spread", 9.3541435)])
def test_aggregates(agg, want):
    from aptfinder.filter.rules import aggregate
    assert aggregate([10, 30, 15, 5], agg) == pytest.approx(want)
    assert aggregate([], agg) is None
    assert aggregate([7, 3, 5], "median") == 5


def test_net_commute_rule():
    from aptfinder.filter.rules import CHECK
    trips = {("commute_targets:Googleplex", "drive"): _trip(20), ("commute_targets:Meta HQ", "drive"): _trip(30)}
    nearest = {("caltrain_stations", "walk"): _trip(10)}
    ctx = Ctx(trips=trips, nearest=nearest, commute_rules=ROOMMATES)
    assert evaluate({"type": "net_commute", "agg": "total", "max": 60}, {}, ctx)[0] == PASS
    assert evaluate({"type": "net_commute", "agg": "total", "max": 59}, {}, ctx)[0] == FAIL
    assert evaluate({"type": "net_commute", "agg": "mean", "max": 20}, {}, ctx)[0] == PASS
    assert evaluate({"type": "net_commute", "agg": "spread", "max": 5}, {}, ctx)[0] == FAIL  # 8.2 min
    # one roommate's time unknown: never counted as 0
    del trips[("commute_targets:Googleplex", "drive")]
    s, why = evaluate({"type": "net_commute", "agg": "total", "max": 1000}, {}, ctx)
    assert s == CHECK and "1 of 3" in why
    assert evaluate({"type": "net_commute", "agg": "total", "max": 9}, {}, Ctx())[0] == CHECK
    assert validate_rule({"type": "net_commute", "agg": "sum", "max": 9}, modes=MODES, points=set(), sets=set())


def test_places_carry_the_goals_commute_terms(client):
    c, _ = client
    body = {"label": "roommates", "rules": [
        {"type": "commute_any", "target": "commute_targets:Downtown Palo Alto", "options": [{"mode": "bike", "max": 30}]},
        {"type": "near", "set": "caltrain_stations", "mode": "walk", "max": 30},
        {"type": "net_commute", "agg": "total", "max": 45}], "nice": []}
    assert c.post("/api/goals", json=body).status_code == 200
    places = c.get("/api/places").json()
    for p in places:
        terms = p["goals"]["roommates"]["commute"]
        assert len(terms) == 2
        bike = p["minutes"].get("commute_targets:Downtown Palo Alto|bike")
        assert terms[0][0] == (bike["minutes"] if bike else None)
    prev = c.post("/api/goals/preview", json=body).json()["places"]
    assert all(prev[p["place_key"]]["commute"] == p["goals"]["roommates"]["commute"] for p in places)
    assert {p["goals"]["roommates"]["status"] for p in places} <= {"pass", "needs_check", "fail"}
