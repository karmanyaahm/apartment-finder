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
    assert res[("rentcafe", "plan:1")]["fails"] == [{"s": FAIL, "i": 0, "f": "restriction", "v": "veterans",
                                                      "ev": '"(VASH)" (rentcafe)'}]
    # nothing read (a RentCafe /housing/ row, a name alone): assumed none, so the rule passes
    assert res[("rentcafe", "plan:2")]["status"] == PASS
    assert res[("rentcafe", "plan:3")]["status"] == PASS
    assert res[("rentcafe", "plan:4")]["status"] == PASS
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
    # nothing in the seed is read: assumed none, so the rule passes
    assert {p["goals"]["lease"]["status"] for p in places if "lease" in p["goals"]} == {PASS}
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
    assert evaluate_all(conn, _goal())["listings"]["g"][("direct", "vm:r:studio-1-bath")]["status"] == PASS
    append_annotation(conn, "user", "place", pk, "restriction", comment="income")
    assert cheap_queue(conn, 1000) == []
    res = evaluate_all(conn, _goal())["listings"]["g"][("direct", "vm:r:studio-1-bath")]
    assert res["status"] == FAIL and "marked in Verify" in res["reasons"][0][1]


def test_an_income_limit_needs_eligibility_words_and_the_strongest_quote_leads():
    from aptfinder.intake.extract import check_restriction

    page = ("MV Apartments. Residents Below-Market Rates Floor Plans. Luxury homes with a pool, a gym and a park. "
            "MidPen Housing Corp. built Redwood Court, Redwood City's first subsidized family housing development. "
            "The community consists of both affordable garden apartments and townhomes. Aster Park is an affordable "
            "housing community in Sunnyvale. Income Requirements 3X's Rent. 16 Affordable Family Units. "
            "All homes are market-rate.")
    # marketing words alone are no evidence: unknown, not income
    for weak in ("Below-Market Rates", "both affordable garden apartments and townhomes", "Income Requirements 3X's Rent"):
        assert check_restriction({"value": "income", "evidence": weak}, page) is None, weak
    for ok in ("an affordable housing community", "16 Affordable Family Units",
               "Redwood City's first subsidized family housing development"):
        assert check_restriction({"value": "income", "evidence": ok}, page)["evidence"] == ok
    # the stronger quote leads whichever order Haiku gives them; a weak one is dropped
    got = check_restriction({"value": "income", "evidence": "an affordable housing community",
                             "evidence_2": "first subsidized family housing development"}, page)
    assert got == {"value": "income", "evidence": "first subsidized family housing development",
                   "evidence_2": "an affordable housing community"}
    got = check_restriction({"value": "income", "evidence": "affordable garden apartments",
                             "evidence_2": "first subsidized family housing development"}, page)
    assert got == {"value": "income", "evidence": "first subsidized family housing development"}
    # other limits keep their plain quote check
    assert check_restriction({"value": "senior", "evidence": "pool, a gym"}, page)["value"] == "senior"
    # none keeps a quote only when it says market-rate in so many words
    assert check_restriction({"value": "none", "evidence": "All homes are market-rate."}, page) == \
        {"value": "none", "evidence": "All homes are market-rate."}
    assert check_restriction({"value": "none", "evidence": "Below-Market Rates"}, page) == {"value": "none"}
    assert check_restriction({"value": "none", "evidence": ""}, page) == {"value": "none"}


def test_render_reread_clears_a_reading_the_stricter_check_rejects(conn, tmp_path, monkeypatch):
    from aptfinder.intake import render as rd

    settings = _site_rows(conn, tmp_path)
    monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(rd, "load_sites", lambda duplicates=True: {"vm": {"url": "https://vm.example/",
                                                                          "name": "Villa Montgomery"}})
    answer = {"value": "income", "evidence": "This Community is Income Restricted."}
    monkeypatch.setattr(rd, "ask_restriction", lambda name, text, model, cap: (dict(answer), 0.001))
    assert rd.reread_restrictions(conn)["observations"] == 2
    assert rd.reread_restrictions(conn)["observations"] == 0  # unchanged: nothing appended
    answer.update(evidence="Studio/1 bath")  # on the page, but no eligibility words: unknown now
    res = rd.reread_restrictions(conn)
    assert res["observations"] == 2 and res["restriction"] == {"unknown": 1, "cleared": 2}
    for r in conn.execute("SELECT data FROM listings WHERE source = 'direct'"):
        assert "page_restriction" not in json.loads(r["data"])["extra"]


def test_operators_fill_in_who_may_rent_below_a_quoted_limit(conn):
    from aptfinder.filter import place_restriction

    def row(sid, url, addr, lat, page=None):
        return (sid, {"address": addr, "lat": lat, "lon": -122.2, "kind": "apartment", "beds": 1,
                      "property_name": sid, **({"extra": {"page_restriction": page}} if page else {})}, 900, url)
    rows = [row("rc:r:a", "https://www.midpen-housing.org/property/redwood-court/", "1 Court St, Redwood City, CA",
                37.48),
            row("mr:r:a", "https://www.midpen-housing.org/property/market/", "2 Mkt St, Redwood City, CA", 37.49,
                {"value": "none", "evidence": "All homes are market-rate."}),
            row("sr:r:a", "https://altahousing.org/sr/", "3 Old St, Palo Alto, CA", 37.44,
                {"value": "senior", "evidence": "62+"}),
            row("pl:r:a", "https://notmidpen-housing.org/x", "4 Plain St, Palo Alto, CA", 37.43),
            # market-rate operators: none where nothing was read; a quoted limit still wins
            row("av:r:a", "https://www.avaloncommunities.com/california/x/", "5 Av St, Mountain View, CA", 37.40),
            row("ab:r:a", "https://www.avaloncommunities.com/california/y/", "6 Av St, Mountain View, CA", 37.41,
                {"value": "income", "evidence": "income limits"}),
            ("hi:r:a", {"address": "7 Hi St, Mountain View, CA", "lat": 37.39, "lon": -122.2, "kind": "apartment",
                        "beds": 1, "property_name": "hi", "operator": "Prometheus"}, 900, "https://hiro.example/")]
    run = start_run(conn, "intake", "direct", {"scope": "full"})
    for sid, data, price, url in rows:
        append_observation(conn, run, "direct", sid, data, url=url, price=price, price_basis="month")
    finish_run(conn, run, "ok", {})
    identity.run(conn)
    res = evaluate_all(conn, _goal())["listings"]["g"]
    assert res[("direct", "rc:r:a")]["status"] == FAIL
    assert '"operator: MidPen Housing (affordable-housing nonprofit)" (operator)' in \
        res[("direct", "rc:r:a")]["reasons"][0][1]
    assert res[("direct", "mr:r:a")]["status"] == PASS  # the page says market-rate: it beats the operator
    assert res[("direct", "sr:r:a")]["fails"][0]["v"] == "senior"  # a quoted limit wins
    assert res[("direct", "pl:r:a")]["status"] == PASS  # host suffix, not substring: nothing read, assumed none
    assert res[("direct", "pl:r:a")]["fails"] == []
    assert res[("direct", "av:r:a")]["status"] == PASS  # AvalonBay's domain: market-rate
    assert res[("direct", "hi:r:a")]["status"] == PASS  # the operator field names Prometheus
    assert res[("direct", "ab:r:a")]["fails"][0]["v"] == "income"
    by = {r["source_id"]: {**dict(r), "data": json.loads(r["data"])} for r in conn.execute("SELECT * FROM listings")}
    assert place_restriction([by["rc:r:a"]])["source"] == "operator"
    assert place_restriction([by["mr:r:a"]])["evidence"] == "All homes are market-rate."
    assert place_restriction([by["pl:r:a"]]) is None
    assert place_restriction([by["sr:r:a"]])["value"] == "senior"
    assert place_restriction([by["rc:r:a"]], {"restriction": {"comment": "none"}}) is None
    assert place_restriction([by["av:r:a"]])["evidence"] == "operator: AvalonBay (market-rate)"
    assert place_restriction([by["ab:r:a"]])["value"] == "income"
    from aptfinder.config import operator_restriction
    assert operator_restriction(["https://www.avaloncommunities.com/x"])["evidence"] == "operator: AvalonBay (market-rate)"


def test_employer_operator_restricts_its_own_hosts_only():
    from aptfinder.config import operator_restriction

    # Stanford's staff housing (Middle Plaza): employer over a page's evidence-less 'none'
    got = operator_restriction(["https://middleplaza.stanford.edu/living-here/floor-plans"])
    assert got["value"] == "employer" and got["evidence"] == "operator: Stanford faculty and staff housing (employer housing)"
    # the operator's hosts, never the whole stanford.edu domain (student housing is not employer housing)
    assert operator_restriction(["https://rde.stanford.edu/studenthousing/"]) is None
    assert operator_restriction(["https://ev.stanford.edu/ev-neighborhoods/blackwelder/"]) is None


def test_employer_operator_place_restriction(conn):
    from aptfinder.filter import place_restriction

    run = start_run(conn, "intake", "direct", {"scope": "full"})
    append_observation(conn, run, "direct", "mp:r:1a",
                       {"address": "400a El Camino Real, Menlo Park, CA", "lat": 37.45, "lon": -122.18,
                        "kind": "apartment", "beds": 1, "property_name": "Middle Plaza",
                        "extra": {"page_restriction": {"value": "none", "url": "https://middleplaza.stanford.edu/"}}},
                       url="https://middleplaza.stanford.edu/living-here/floor-plans", price=3000, price_basis="month")
    finish_run(conn, run, "ok", {})
    identity.run(conn)
    res = evaluate_all(conn, _goal())["listings"]["g"]
    assert res[("direct", "mp:r:1a")]["status"] == FAIL
    assert res[("direct", "mp:r:1a")]["fails"][0]["v"] == "employer"
    row = {**dict(conn.execute("SELECT * FROM listings").fetchone())}
    row["data"] = json.loads(row["data"])
    assert place_restriction([row])["value"] == "employer"
