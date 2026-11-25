"""Prometheus own sites read from Prometheus's availability JSON (intake/prometheus.py)."""

import json

from aptfinder import settings
from aptfinder.intake.prometheus import API, read, unit_rows, units_outcome
from aptfinder.intake.render import SKIP_PLATFORMS, last_outcomes
from aptfinder.store import append_observation, finish_run, start_run

# trimmed real response: The Dean (community id 4495429), available-units?date=2026-12-01, fetched 2026-11-25
DEAN = json.loads("""[
{"rent":"4854.0000","deposit":"500","floorPlanID":"4","floorPlanName":"Plan 1D","madeReadyDate":"8/5/2026",
 "unitHoldStatus":"false","unitHoldUntil":null,"floor":"2","bedrooms":"1","bathrooms":"1","area":"677",
 "unitID":"152","unitNumber":"1217","buildingNumber":null,"unitLabel":"Apartment 1217","bestTerm":"15",
 "bestRent":"4854","unitOccupancy":"3"},
{"rent":"6398.0000","deposit":"500","floorPlanID":"19","floorPlanName":"Plan 2C","madeReadyDate":"9/12/2026",
 "unitHoldStatus":"false","unitHoldUntil":null,"floor":"1","bedrooms":"2","bathrooms":"2","area":"962",
 "unitID":"398","unitNumber":"2107","buildingNumber":null,"unitLabel":"Apartment 2107","bestTerm":"15",
 "bestRent":"6398","unitOccupancy":"5"}]""")
DRAWINGS = {"Plan 1D": "https://assets.prometheusapartments.com/m/34382ddf40793f44/original/2023_DEANO_677sqft_11CC_1B1BR_1D.svg",
            "Plan 1C": "https://assets.prometheusapartments.com/m/486e7f4c96da0673/original/2023_DEANO_667sqft_11CB2_1B1BR_1C.svg"}
CHALLENGE = '{"error":{"code":"challenge","message":"This request requires a challenge to be completed."}}'


def test_units_become_unit_rows_with_month_prices_and_the_plan_drawing():
    rows = unit_rows(DEAN, "the-dean", {"name": "The Dean"}, address="460 N Shoreline Blvd, Mountain View, CA",
                     drawings=DRAWINGS, community="4495429")
    assert [r[0] for r in rows] == ["the-dean:r:1217", "the-dean:r:2107"]  # render's `<site>:r:<unit>` scheme
    sid, d, price = rows[0]
    assert price == 4854 and d.floor_plan == "Plan 1D" and d.unit == "1217"
    assert (d.beds, d.baths, d.sqft, d.available_from) == (1, 1, 677, "2026-08-05")
    assert d.extra["area"] == "the-dean#render" and d.extra["platform"] == "render"
    assert d.extra["reader"] == "prometheus_json" and d.extra["best_term"] == "15" and d.extra["rent"] == 4854
    assert d.extra["floorplan_image"] == DRAWINGS["Plan 1D"]
    assert "floorplan_image" not in rows[1][1].extra  # no drawing named Plan 2C: none, never a guess


def test_only_a_json_list_is_an_answer():
    assert units_outcome(200, json.dumps(DEAN))[0] == DEAN
    assert units_outcome(200, "[]") == ([], {"http_status": 200})
    assert units_outcome(403, CHALLENGE)[1]["outcome"] == "blocked"
    assert units_outcome(200, CHALLENGE)[1]["outcome"] == "blocked"
    assert units_outcome(429, "Too Many Requests")[1]["outcome"] == "blocked"
    assert units_outcome(500, "<html>oops</html>")[1]["outcome"] == "error"


def test_render_skips_prometheus_sites():
    assert "prometheus" in SKIP_PLATFORMS


def _fake(responses, calls):
    def get(url, headers):
        calls.append(url)
        return responses[url]
    return get


def test_a_read_retires_old_render_plans_and_a_refused_one_retires_nothing(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
    (tmp_path / "prometheus_ids.json").write_text('{"the-dean": "4495429"}')
    # an earlier render read plan rows, and the site row carries the plan drawing map
    r0 = start_run(conn, "intake", "direct", {"render": True, "areas": ["the-dean#render"], "scope": "partial"})
    append_observation(conn, r0, "direct", "the-dean:r:plan-1d", {"floor_plan": "Plan 1D", "extra": {
        "area": "the-dean#render", "platform": "render", "site": "the-dean"}}, price=4854, price_basis="month")
    append_observation(conn, r0, "direct", "the-dean:site", {"kind": "site_info", "extra": {
        "site": "the-dean", "platform": "prometheus", "plan_drawings": DRAWINGS}})
    finish_run(conn, r0, "ok", {"areas": {"the-dean#render": {"state": "complete", "outcome": "ok", "items": 1}}})

    url = API.format(id="4495429", date="2026-12-01")
    calls: list[str] = []
    res = read(conn, sites=["the-dean"], get=_fake({url: (200, json.dumps(DEAN))}, calls), delay=0,
               today=__import__("datetime").date(2026, 12, 1))
    assert calls == [url]  # the id came from the cache: one request, no page fetch
    assert res["sites"]["the-dean"] == {"units": 2, "plans": 2, "price_min": 4854, "price_max": 6398}
    active = dict(conn.execute("SELECT source_id, active FROM listings WHERE source = 'direct'").fetchall())
    assert active == {"the-dean:r:plan-1d": 0, "the-dean:r:1217": 1, "the-dean:r:2107": 1, "the-dean:site": 1}
    img = conn.execute("SELECT json_extract(data, '$.extra.floorplan_image') FROM listings "
                       "WHERE source_id = 'the-dean:r:1217'").fetchone()[0]
    assert img == DRAWINGS["Plan 1D"]
    assert last_outcomes(conn)["the-dean"]["outcome"] == "ok"
    raw = json.loads(open(res["raw"]).read())
    assert raw["sites"]["the-dean"]["units"] == DEAN and res["raw"].startswith(str(tmp_path / "raw" / "direct"))

    # refused (Cloudflare challenge): recorded as blocked, nothing retired
    read(conn, sites=["the-dean"], get=_fake({url: (403, CHALLENGE)}, []), delay=0,
         today=__import__("datetime").date(2026, 12, 1))
    assert last_outcomes(conn)["the-dean"]["outcome"] == "blocked"
    assert conn.execute("SELECT SUM(active) FROM listings WHERE source_id LIKE 'the-dean:r:%'").fetchone()[0] == 2


def test_the_community_id_comes_from_the_stored_page_data_before_any_page_fetch(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
    nd = json.dumps({"props": {"pageProps": {"apartmentPageData": {"id": 4495429, "name": "The Dean"}}}})
    rawf = tmp_path / "run5_the_dean.json"
    rawf.write_text(json.dumps({"items": [{"site": "the-dean", "url": "x", "scripts": [
        {"type": "application/json", "id": "__NEXT_DATA__", "text": nd}]}]}))
    r = start_run(conn, "intake", "direct", {"areas": ["the-dean"]})
    append_observation(conn, r, "direct", "the-dean:site", {"kind": "site_info"}, raw_path=str(rawf))
    finish_run(conn, r, "ok", {})
    url = API.format(id="4495429", date="2026-12-01")
    calls: list[str] = []
    read(conn, sites=["the-dean"], get=_fake({url: (200, "[]")}, calls), delay=0,
         today=__import__("datetime").date(2026, 12, 1))
    assert calls == [url]
    assert json.loads((tmp_path / "prometheus_ids.json").read_text()) == {"the-dean": "4495429"}
    assert last_outcomes(conn)["the-dean"]["outcome"] == "no_plans"
