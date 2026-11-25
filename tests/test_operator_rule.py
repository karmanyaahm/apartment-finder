"""The `not_operator` rule: who rents an offer out, per listing (filter.listing_operators), from config/operators.toml
names and domains."""

from __future__ import annotations

import json

from aptfinder import identity, settings
from aptfinder.config import Goal, GoalsConfig, load_goals
from aptfinder.filter import evaluate_all
from aptfinder.filter.rules import FAIL, PASS, fact, validate_rule
from aptfinder.store import append_observation, finish_run, start_run


def _run(conn, source, rows):
    run = start_run(conn, "intake", source, {"scope": "full"})
    for sid, data, price in rows:
        append_observation(conn, run, source, sid, data, url=f"https://{source}.example/{sid}", price=price,
                           price_basis="month")
    finish_run(conn, run, "ok", {})


def test_not_operator_fails_each_repost_and_passes_the_rest(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
    home = {"address": "108 Bryant St, Mountain View, CA 94041", "lat": 37.3954, "lon": -122.0794, "beds": 3,
            "kind": "townhome", "term": "long"}
    _run(conn, "zumper", [("59875773", {**home, "term": "short", "operator": "Blueground",
                                        "extra": {"brokerage": "BLUEGROUND"}}, 5380)])
    _run(conn, "padmapper", [("59875773", home, 5380), ("11111111", home, 5100)])
    _run(conn, "apartments_com", [("fdnnp5s:r:", {**home, "extra": {"management_company": "Blueground"}}, 5380)])
    _run(conn, "zillow", [("351574202", {**home, "extra": {"zpid": "351574202"}}, 5380),
                          ("999", {**home, "extra": {"zpid": "999"}}, 5200),
                          ("777:3bd", {**home, "extra": {"zpid": "777"}}, 5000)])
    # Zillow's detail pages (raw): the unit's page names Blueground as its lister; a building row's page is one
    # representative unit's, so it names nobody for the building row
    raw = tmp_path / "raw" / "zillow_detail"
    raw.mkdir(parents=True)
    page = lambda z, broker: {"zillow_id": z, "attribution_info": {"brokerName": broker, "agentName": "Leasing Agent"}}
    (raw / "run1_01.json").write_text(json.dumps({"items": [page("351574202", "Blueground Furnished Apartments"),
                                                            page("999", "Vivmark Residential"),
                                                            page("777", "Blueground Furnished Apartments")]}))
    identity.run(conn)
    rule = {"type": "not_operator", "name": "Blueground"}
    cfg = GoalsConfig(goals={"g": Goal(label="g", rules=[rule], nice=[])}, verify=load_goals().verify)
    res = evaluate_all(conn, cfg)["listings"]["g"]
    assert res[("zumper", "59875773")]["status"] == FAIL
    assert "operator field 'Blueground' (zumper)" in res[("zumper", "59875773")]["reasons"][0][1]
    assert res[("padmapper", "59875773")]["status"] == FAIL  # Zumper's twin: the same listing id
    assert "same listing as Zumper 59875773" in res[("padmapper", "59875773")]["reasons"][0][1]
    assert res[("apartments_com", "fdnnp5s:r:")]["status"] == FAIL  # management company
    assert res[("zillow", "351574202")]["status"] == FAIL
    assert "Zillow page lister 'Blueground Furnished Apartments'" in res[("zillow", "351574202")]["reasons"][0][1]
    # nothing names Blueground: pass (an exclusion has nothing to check), never needs_check
    for key in (("padmapper", "11111111"), ("zillow", "999"), ("zillow", "777:3bd")):
        assert res[key]["status"] == PASS, key
    assert fact(rule, {}) == {"f": "operator", "v": "Blueground"}
    assert validate_rule(rule, modes=[], points=set(), sets=set()) is None
    assert validate_rule({"type": "not_operator", "name": " "}, modes=[], points=set(), sets=set())

