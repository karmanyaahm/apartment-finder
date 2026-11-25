"""Who may rent: extract's `restriction` reading, its pooling per place and the `equals restriction none` rule."""

from __future__ import annotations

import json

from aptfinder import identity
from aptfinder.config import Goal, GoalsConfig, load_goals
from aptfinder.filter import evaluate_all
from aptfinder.filter.rules import CHECK, FAIL, PASS, fact
from aptfinder.intake.extract import check_answer
from aptfinder.store import append_observation, finish_run, start_run


def test_a_limit_needs_its_quote_and_none_needs_none():
    text = "Title: The Laurel at Mountain View (55&#x2B;)\nIncome limits apply. Section 8 welcome."
    assert check_answer({"restriction": {"value": "senior", "evidence": "(55+)"}}, text)["restriction"] == \
        {"value": "senior", "evidence": "(55+)"}
    assert check_answer({"restriction": {"value": "income", "evidence": "Income limits apply"}}, text)["restriction"] \
        == {"value": "income", "evidence": "Income limits apply"}
    assert check_answer({"restriction": {"value": "none", "evidence": ""}}, text)["restriction"] == {"value": "none"}
    glued = "Educator HousingAt least one member of your household is a current employee of Palo Alto Unified"
    assert check_answer({"restriction": {"value": "employer", "evidence": "At least one member of your household is a "
                                         "current employee of Palo Alto Unified"}}, glued)["restriction"]["value"] == \
        "employer"
    # a claimed limit without its words in the text is unknown, not 'none'
    assert "restriction" not in check_answer({"restriction": {"value": "student", "evidence": "students only"}}, text)
    assert "restriction" not in check_answer({"restriction": {"value": "bogus", "evidence": "Income"}}, text)


def _run(conn, source, rows):
    run = start_run(conn, "intake", source, {"scope": "full"})
    for sid, data, price in rows:
        append_observation(conn, run, source, sid, data, url="https://x", price=price, price_basis="month")
    finish_run(conn, run, "ok", {})


def _read(conn, tmp_path, monkeypatch, answers):
    """Run extract over every source with a stub that answers by a word in the entry text."""
    from aptfinder import settings
    from aptfinder.intake import extract as ex
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)

    def stub(batch, model, cap):
        out = []
        for b in batch:
            rx = next((v for w, v in answers.items() if w in b["text"]), {"value": "none", "evidence": ""})
            out.append({"key": b["key"], "restriction": rx})
        return out, 0.0
    for src in ("rentcafe", "direct"):
        ex.extract(conn, src, ask_fn=stub)
    identity.run(conn)


def _goal() -> GoalsConfig:
    cfg = load_goals()
    return GoalsConfig(goals={"g": Goal(label="g", rules=[{"type": "equals", "field": "restriction",
                                                          "value": "none"}], nice=[])}, verify=cfg.verify)


def test_name_only_rows_are_read_and_a_place_limit_holds_for_all_its_listings(conn, tmp_path, monkeypatch):
    belo = dict(address="1 Belo Way, Santa Clara, CA", lat=37.35, lon=-121.95)
    _run(conn, "rentcafe", [
        # the building row names the limit; the priced plan at the same address does not
        ("building:1", {**belo, "kind": "site_info", "property_name": "PBV - Belovida (VASH)",
                        "extra": {"vertical": "housing"}}, None),
        ("plan:1", {**belo, "kind": "apartment", "beds": 0, "property_name": "Belovida Santa Clara, L.P.",
                    "extra": {"vertical": "housing"}}, 2706),
        # rentcafe.com/housing/ with nothing stated: unknown (6tenEast is market rate, Alta Torre is not)
        ("plan:2", {"address": "3895 Fabian Way, Palo Alto, CA", "lat": 37.42, "lon": -122.10, "kind": "apartment",
                    "beds": 1, "property_name": "Alta Torre", "extra": {"vertical": "housing"}}, 880),
        ("plan:3", {"address": "500 Market St, Sunnyvale, CA", "lat": 37.39, "lon": -122.01, "kind": "apartment",
                    "beds": 1, "property_name": "Market Flats", "extra": {"vertical": "apartments"}}, 3200)])
    _run(conn, "direct", [("laurel:r:a1", {"address": "1 Laurel Ln, Mountain View, CA", "lat": 37.40, "lon": -122.08,
                                            "kind": "apartment", "beds": 1,
                                            "property_name": "The Laurel at Mountain View (55+)"}, 2650)])
    _read(conn, tmp_path, monkeypatch, {"VASH": {"value": "veterans", "evidence": "(VASH)"},
                                        "55+": {"value": "senior", "evidence": "(55+)"}})
    got = {r["source_id"]: (json.loads(r["data"])["extra"]["extracted"].get("restriction") or {}).get("value")
           for r in conn.execute("SELECT source_id, data FROM listings")}
    assert got == {"building:1": "veterans", "plan:1": None, "plan:2": None, "plan:3": "none",
                   "laurel:r:a1": "senior"}
    res = evaluate_all(conn, _goal())["listings"]["g"]
    assert res[("rentcafe", "plan:1")]["status"] == FAIL  # pooled from the building row
    assert '"(VASH)" (rentcafe)' in res[("rentcafe", "plan:1")]["reasons"][0][1]
    assert res[("rentcafe", "plan:1")]["fails"] == [{"s": FAIL, "f": "restriction", "v": "veterans",
                                                      "ev": '"(VASH)" (rentcafe)'}]
    assert res[("rentcafe", "plan:2")]["status"] == CHECK
    assert res[("rentcafe", "plan:3")]["status"] == PASS
    assert res[("direct", "laurel:r:a1")]["status"] == FAIL


def test_restriction_fact_rule_editor_and_web_badge(tmp_path):
    from fastapi.testclient import TestClient

    from aptfinder.store.seed import seed_db
    from aptfinder.web.app import create_app

    assert fact({"type": "equals", "field": "restriction", "value": "none"},
                {"restriction": "income", "restriction_evidence": '"BMR unit" (craigslist)'}) == \
        {"f": "restriction", "v": "income", "ev": '"BMR unit" (craigslist)'}
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    c = TestClient(create_app(db))
    assert c.get("/api/goals").json()["equals_fields"]["restriction"][0] == "none"
    r = c.put("/api/goals/lease", json={"label": "Lease", "nice": [], "rules": [
        {"type": "equals", "field": "restriction", "value": "none"}]})
    assert r.status_code == 200, r.text
    places = c.get("/api/places").json()
    assert places and all("restriction" in p for p in places)
    # nothing in the seed is read: the rule says needs_check, never fail
    assert {p["goals"]["lease"]["status"] for p in places if "lease" in p["goals"]} == {CHECK}
    assert "restriction" in c.get(f"/api/places/{places[0]['place_key']}").json()["place"]
