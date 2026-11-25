"""Google Places Text Search. The item follows the API's documented shape (the trial call was refused, design.md)."""

from __future__ import annotations

import json

import pytest

from aptfinder.config import Area
from aptfinder.intake import ADAPTERS, base, places
from aptfinder.intake.base import run_source

# ------------------------------------------------------------------------------------------ Google Places
PLACE = {"id": "ChIJexample", "displayName": {"text": "Avalon Mountain View"},
         "formattedAddress": "1600 Villa St, Mountain View, CA 94041, USA",
         "location": {"latitude": 37.3976, "longitude": -122.0844},
         "websiteUri": "https://www.avaloncommunities.com/california/mountain-view-apartments/avalon-mountain-view/?utm_source=gmb",
         "nationalPhoneNumber": "(650) 555-0100", "rating": 4.1, "userRatingCount": 210,
         "businessStatus": "OPERATIONAL", "types": ["apartment_complex", "point_of_interest"],
         "primaryType": "apartment_complex", "googleMapsUri": "https://maps.google.com/?cid=123"}


def test_places_parse_and_skip_closed():
    p, = ADAPTERS["google_places"].parse(PLACE, {})
    d = p.data
    assert (p.source_id, p.url, d.kind, d.property_name) == ("ChIJexample", "https://maps.google.com/?cid=123",
                                                            "site_info", "Avalon Mountain View")
    assert (d.address, d.city, d.zip, d.lat) == ("1600 Villa St, Mountain View, CA 94041", "Mountain View", "94041", 37.3976)
    assert d.extra["website"] == "https://www.avaloncommunities.com/california/mountain-view-apartments/avalon-mountain-view/"
    assert d.extra["phone"] == "(650) 555-0100" and d.extra["rating"] == 4.1
    assert ADAPTERS["google_places"].skip({**PLACE, "businessStatus": "CLOSED_PERMANENTLY"})


class FakeResp:
    def __init__(self, body, code=200):
        self.status_code, self._b, self.text = code, body, json.dumps(body)

    def json(self):
        return self._b


BOX = Area(kind="box", id="mountain_view", label="", bbox=[37.38, -122.09, 37.41, -122.06])


def test_places_run_meters_every_page_and_marks_full_queries_partial(conn, monkeypatch, tmp_path):
    sent = []

    def post(body):
        sent.append(body)
        page = len(sent)
        return FakeResp({"places": [{**PLACE, "id": f"p{page}-{i}"} for i in range(20)], "nextPageToken": f"t{page}"})

    monkeypatch.setattr(base.settings, "RAW_DIR", tmp_path / "raw")
    adapter = base.Adapter(**{**ADAPTERS["google_places"].__dict__,
                              "fetch": lambda cfg, areas, cap, usd, c: places.fetch(cfg, areas, cap, usd, c, post=post)})
    monkeypatch.setattr(base, "load_sources", lambda: {"spend": {"max_usd_per_run": 1.0}, "google_places": {
        "max_calls": 30, "max_items": 200, "queries": [{"text": "apartments", "type": "apartment_complex"}]}})
    res = run_source(conn, adapter, [BOX])
    assert len(sent) == 3  # three pages, the API's limit
    assert sent[0]["includedType"] == "apartment_complex" and sent[0]["strictTypeFiltering"] is True
    assert sent[0]["locationRestriction"]["rectangle"]["low"] == {"latitude": 37.38, "longitude": -122.09}
    assert sent[1]["pageToken"] == "t1"
    assert conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = 'places'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM api_calls WHERE provider = 'google'").fetchone()[0] == 0
    assert res.counts["observations"] == 60
    assert res.counts["areas"]["mountain_view"]["state"] == "partial"  # 60 = the cap: there may be more


def test_places_caps(conn):
    calls = [0]
    with pytest.raises(places.PlacesCapReached, match="this run's cap"):
        places.search_box(conn, "k", {"text": "a"}, BOX.bbox, 0, calls, post=lambda b: FakeResp({}))
    for _ in range(900):
        conn.execute("INSERT INTO api_calls (ts, provider, mode, ok) VALUES (strftime('%Y-%m-%dT%H:%M:%S', 'now'), "
                     "'places', 'text_search', 1)")
    with pytest.raises(places.PlacesCapReached, match="monthly cap of 900"):
        places.search_box(conn, "k", {"text": "a"}, BOX.bbox, 10, [0], post=lambda b: FakeResp({}))


def test_places_call_cap_spans_every_box_of_a_run(conn, monkeypatch, tmp_path):
    sent = []

    def post(body):
        sent.append(body)
        return FakeResp({"places": [{**PLACE, "id": f"q{len(sent)}"}]})  # one page each

    box2 = Area(kind="box", id="san_antonio", label="", bbox=[37.39, -122.12, 37.42, -122.09])
    monkeypatch.setattr(base.settings, "RAW_DIR", tmp_path / "raw")
    adapter = base.Adapter(**{**ADAPTERS["google_places"].__dict__,
                              "fetch": lambda cfg, areas, cap, usd, c: places.fetch(cfg, areas, cap, usd, c, post=post)})
    monkeypatch.setattr(base, "load_sources", lambda: {"spend": {"max_usd_per_run": 1.0}, "google_places": {
        "max_calls": 3, "queries": [{"text": "a", "type": "apartment_complex"}, {"text": "a", "type": "apartment_building"}]}})
    res = run_source(conn, adapter, [BOX, box2])  # 2 boxes x 2 queries = 4 calls > 3
    assert len(sent) == 3 and res.counts["observations"] == 3  # what was paid for is kept
    assert {a["state"] for a in res.counts["areas"].values()} == {"partial"}
    assert any("CAP_REACHED" in w for w in res.warnings)

