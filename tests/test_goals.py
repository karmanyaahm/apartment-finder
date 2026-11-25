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
