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
    text = "Title: The Laurel at Mountain View (55&#x2B;)\nIncome limits apply. Section 8 welcome. Quiet garden homes."
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
                    "beds": 1, "property_name": "Market Flats", "extra": {"vertical": "apartments"},
                    "description": "Bright one bedroom homes near the train with a pool, a gym and parking."}, 3200),
        # a name alone says nothing about who may rent: 'none' from it is unknown
        ("plan:4", {"address": "9 Plain St, Sunnyvale, CA", "lat": 37.38, "lon": -122.02, "kind": "apartment",
                    "beds": 1, "property_name": "Plain Flats", "extra": {"vertical": "apartments"}}, 3100)])
    _run(conn, "direct", [("laurel:r:a1", {"address": "1 Laurel Ln, Mountain View, CA", "lat": 37.40, "lon": -122.08,
                                            "kind": "apartment", "beds": 1,
                                            "property_name": "The Laurel at Mountain View (55+)"}, 2650)])
    _read(conn, tmp_path, monkeypatch, {"VASH": {"value": "veterans", "evidence": "(VASH)"},
                                        "55+": {"value": "senior", "evidence": "(55+)"}})
    got = {r["source_id"]: (json.loads(r["data"])["extra"]["extracted"].get("restriction") or {}).get("value")
           for r in conn.execute("SELECT source_id, data FROM listings")}
    assert got == {"building:1": "veterans", "plan:1": None, "plan:2": None, "plan:3": "none", "plan:4": None,
                   "laurel:r:a1": "senior"}
    res = evaluate_all(conn, _goal())["listings"]["g"]
    assert res[("rentcafe", "plan:1")]["status"] == FAIL  # pooled from the building row
    assert '"(VASH)" (rentcafe)' in res[("rentcafe", "plan:1")]["reasons"][0][1]
    assert res[("rentcafe", "plan:1")]["fails"] == [{"s": FAIL, "f": "restriction", "v": "veterans",
                                                      "ev": '"(VASH)" (rentcafe)'}]
    assert res[("rentcafe", "plan:2")]["status"] == CHECK
    assert res[("rentcafe", "plan:3")]["status"] == PASS
    assert res[("rentcafe", "plan:4")]["status"] == CHECK
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


def test_none_needs_a_description_not_a_name():
    from aptfinder.intake.extract import described

    none = {"restriction": {"value": "none", "evidence": ""}}
    # Villa Montgomery's own-site plan: a name and a plan name, nothing that would state a limit
    thin = "Name: Villa Montgomery Apartments\nTitle: 1 Bedroom/1 bath\nAmenities: Dishwasher; Pool"
    assert not described(thin) and "restriction" not in check_answer(none, thin)
    full = thin + "\nSpacious homes on El Camino Real, a short walk to Caltrain and shops downtown."
    assert described(full) and check_answer(none, full)["restriction"] == {"value": "none"}
    # a limit stated in the name still counts
    assert check_answer({"restriction": {"value": "senior", "evidence": "(55+)"}},
                        "Name: The Laurel (55+)")["restriction"]["value"] == "senior"


def test_recheck_none_turns_a_thin_none_unknown_for_free(conn, tmp_path, monkeypatch):
    from aptfinder.intake import extract as ex

    _run(conn, "direct", [("vm:r:1bd", {"address": "1500 El Camino Real, Redwood City, CA", "lat": 37.48,
                                        "lon": -122.22, "kind": "apartment", "beds": 1,
                                        "property_name": "Villa Montgomery Apartments", "floor_plan": "1 Bedroom/1 bath"},
                          1012)])
    _read(conn, tmp_path, monkeypatch, {})  # stub answers none; cached
    # an old reading said none from the name alone (before the description gate)
    r = conn.execute("SELECT * FROM latest_obs WHERE source_id = 'vm:r:1bd'").fetchone()
    d = json.loads(r["data"])
    d["extra"]["extracted"]["restriction"] = {"value": "none"}
    run = start_run(conn, "extract", "direct", {})
    append_observation(conn, run, "direct", "vm:r:1bd", d, url="https://x", price=1012, price_basis="month")
    finish_run(conn, run, "ok", {})

    def boom(*a):
        raise AssertionError("no Claude call on a recheck")
    res = ex.extract(conn, "direct", ask_fn=boom, recheck_none=True)
    assert res["observations"] == 1
    rx = conn.execute("SELECT json_extract(data, '$.extra.extracted.restriction') FROM listings "
                      "WHERE source_id = 'vm:r:1bd'").fetchone()[0]
    assert rx is None
    assert ex.extract(conn, "direct", ask_fn=boom, recheck_none=True)["listings"] == 0  # nothing left to fix


def _site_rows(conn, tmp_path):
    from aptfinder import settings

    raw = tmp_path / "raw" / "render"
    raw.mkdir(parents=True)
    page = ("Villa Montgomery 1500 El Camino Real, Redwood City Application Criteria This Community is Income "
            "Restricted. Average income of the households may not exceed the maximum income limits. "
            "Studio/1 bath $838 1 Bedroom/1 bath $1012")
    (raw / "run7.json").write_text(json.dumps({"items": [
        {"site": "vm", "url": "https://vm.example/", "kind": "start", "text": page},
        {"site": "other", "url": "https://o.example/", "kind": "start", "text": "Income restricted elsewhere"}]}))
    run = start_run(conn, "intake", "direct", {"render": True, "areas": ["vm#render"], "scope": "partial"})
    for key, beds, price in (("2-bedrooms-1-bath", 2, 1212), ("studio-1-bath", 0, 838)):
        append_observation(conn, run, "direct", f"vm:r:{key}",
                           {"address": "1500 El Camino Real, Redwood City, CA", "lat": 37.48, "lon": -122.22,
                            "kind": "apartment", "beds": beds, "property_name": "Villa Montgomery Apartments",
                            "floor_plan": key, "extra": {"site": "vm", "platform": "render", "area": "vm#render"}},
                           url="https://vm.example/", price=price, price_basis="month",
                           raw_path=str(raw / "run7.json"))
    finish_run(conn, run, "ok", {"areas": {"vm#render": {"state": "complete", "outcome": "ok", "items": 2}}})
    return settings


def test_render_page_restriction_is_pooled_and_survives_extract(conn, tmp_path, monkeypatch):
    from aptfinder.intake import render as rd

    settings = _site_rows(conn, tmp_path)
    monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rd, "load_sites", lambda duplicates=True: {"vm": {"url": "https://vm.example/",
                                                                          "name": "Villa Montgomery"}})
    asked = []

    def ask_rx(name, text, model, cap):
        asked.append(name)
        assert "Income restricted elsewhere" not in text  # only this site's pages
        return {"value": "income", "evidence": "This Community is Income Restricted."}, 0.001
    monkeypatch.setattr(rd, "ask_restriction", ask_rx)
    assert rd.reread_restrictions(conn, dry_run=True)["to_read"] == 1
    res = rd.reread_restrictions(conn)
    assert asked == ["Villa Montgomery"] and res["observations"] == 2 and res["restriction"] == {"income": 1}
    # a quote that is not on the page is dropped
    assert rd.page_restriction({"value": "senior", "evidence": "62+ only"}, "Studio $838 " * 10, "u") is None
    # extract's later reading of the name-only plan rows (none -> unknown) keeps the page's reading
    _read(conn, tmp_path, monkeypatch, {})
    for r in conn.execute("SELECT data FROM listings WHERE source = 'direct'"):
        extra = json.loads(r["data"])["extra"]
        assert extra["page_restriction"]["value"] == "income" and "restriction" not in extra["extracted"]
    res = evaluate_all(conn, _goal())["listings"]["g"]
    assert res[("direct", "vm:r:studio-1-bath")]["status"] == FAIL
    assert "(direct page)" in res[("direct", "vm:r:studio-1-bath")]["reasons"][0][1]


def test_verify_income_answer_sets_the_place_restriction_and_resolves_too_cheap(conn, tmp_path, monkeypatch):
    from aptfinder.store import append_annotation
    from aptfinder.verify import cheap_queue

    _site_rows(conn, tmp_path)
    identity.run(conn)
    pk = conn.execute("SELECT place_key FROM listings WHERE source_id = 'vm:r:studio-1-bath'").fetchone()[0]
    assert {c["listings"][0]["source_id"] for c in cheap_queue(conn, 1000)} == {"vm:r:2-bedrooms-1-bath",
                                                                                "vm:r:studio-1-bath"}
    assert evaluate_all(conn, _goal())["listings"]["g"][("direct", "vm:r:studio-1-bath")]["status"] == CHECK
    append_annotation(conn, "user", "place", pk, "restriction", comment="income")
    assert cheap_queue(conn, 1000) == []
    res = evaluate_all(conn, _goal())["listings"]["g"][("direct", "vm:r:studio-1-bath")]
    assert res["status"] == FAIL and "marked in Verify" in res["reasons"][0][1]
