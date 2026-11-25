"""Blueground's own search JSON (intake/blueground.py) and its feed winning at its stay places (filter)."""

import json
from urllib.parse import parse_qs, urlparse

import pytest

from aptfinder import settings
from aptfinder.intake import blueground as bg
from aptfinder.store import append_observation, finish_run, start_run

# trimmed real responses of 2026-11-25 (search box around Palo Alto; 1868's undated card rebuilt from its quote)
FX = json.loads(r'''{
 "u1895": {
  "id": 76363,
  "code": "1895",
  "name": "2721 Midtown Court",
  "path": "p/furnished-apartments/sfo-1895",
  "businessModel": "CORE",
  "partnerSlug": null,
  "bedrooms": 1,
  "bathrooms": 1,
  "hasJuniorBedroom": false,
  "lotSize": 716,
  "address": {
   "buildingComplex": null,
   "building": "Midtown",
   "lng": -122.128288,
   "lat": 37.4343797,
   "city": "Palo Alto",
   "area": "Palo Alto",
   "level2": "Midtown"
  },
  "availableFrom": "2026-12-07",
  "baseRent": {
   "amount": 0,
   "currency": "USD"
  },
  "cityCode": "SFO",
  "source": "blueground",
  "type": "APARTMENT",
  "utilities": [
   {
    "name": "LIVE_TV"
   },
   {
    "name": "POWER"
   }
  ],
  "utilitiesIncludedInRent": true,
  "photos": [
   {
    "url": "https://photos2.theblueground.com/736/pg76363-o-9d535dda-dc99-428c-aab3-5db7576f4c34.jpeg",
    "captionKey": "livingRoom",
    "tags": [
     "furnished",
     "livingRoom"
    ]
   }
  ],
  "rent": {
   "minDuration": {
    "start": "2026-12-06",
    "end": "2027-05-02",
    "months": 4,
    "days": 27
   },
   "amount": 0,
   "currency": "USD"
  }
 },
 "u1868_undated": {
  "id": 70735,
  "code": "1868",
  "name": "2721 Midtown Court",
  "path": "p/furnished-apartments/sfo-1868",
  "businessModel": "CORE",
  "partnerSlug": null,
  "bedrooms": 1,
  "bathrooms": 1,
  "hasJuniorBedroom": false,
  "lotSize": 716,
  "address": {
   "buildingComplex": null,
   "building": "Midtown",
   "lng": -122.128288,
   "lat": 37.4343797,
   "city": "Palo Alto",
   "area": "Palo Alto",
   "level2": "Midtown"
  },
  "availableFrom": "2027-01-11",
  "baseRent": {
   "amount": 0,
   "currency": "USD"
  },
  "cityCode": "SFO",
  "source": "blueground",
  "type": "APARTMENT",
  "utilities": [
   {
    "name": "LIVE_TV"
   },
   {
    "name": "POWER"
   }
  ],
  "utilitiesIncludedInRent": true,
  "photos": [
   {
    "url": "https://photos2.theblueground.com/736/pg70735-o-b827419a-b6f0-4271-9031-1be094a95c09.jpeg",
    "captionKey": "livingRoom",
    "tags": [
     "furnished",
     "livingRoom"
    ]
   }
  ],
  "rent": {
   "amount": 0,
   "currency": "USD",
   "minDuration": {
    "start": "2027-01-11",
    "end": "2027-06-08",
    "months": 4,
    "days": 28
   }
  }
 },
 "q1868": {
  "id": 70735,
  "code": "1868",
  "name": "2721 Midtown Court",
  "path": "p/furnished-apartments/sfo-1868",
  "businessModel": "CORE",
  "partnerSlug": null,
  "bedrooms": 1,
  "bathrooms": 1,
  "hasJuniorBedroom": false,
  "lotSize": 716,
  "address": {
   "buildingComplex": null,
   "building": "Midtown",
   "lng": -122.128288,
   "lat": 37.4343797,
   "city": "Palo Alto",
   "area": "Palo Alto",
   "level2": "Midtown"
  },
  "availableFrom": "2027-01-11",
  "baseRent": {
   "amount": 5600,
   "currency": "USD"
  },
  "cityCode": "SFO",
  "source": "blueground",
  "type": "APARTMENT",
  "utilities": [
   {
    "name": "LIVE_TV"
   },
   {
    "name": "POWER"
   }
  ],
  "utilitiesIncludedInRent": true,
  "photos": [
   {
    "url": "https://photos2.theblueground.com/736/pg70735-o-b827419a-b6f0-4271-9031-1be094a95c09.jpeg",
    "captionKey": "livingRoom",
    "tags": [
     "furnished",
     "livingRoom"
    ]
   }
  ],
  "rent": {
   "amount": 5555,
   "currency": "USD",
   "minDuration": {
    "start": "2027-01-11",
    "end": "2027-02-15"
   }
  }
 },
 "q1895": {
  "id": 76363,
  "code": "1895",
  "name": "2721 Midtown Court",
  "path": "p/furnished-apartments/sfo-1895",
  "businessModel": "CORE",
  "partnerSlug": null,
  "bedrooms": 1,
  "bathrooms": 1,
  "hasJuniorBedroom": false,
  "lotSize": 716,
  "address": {
   "buildingComplex": null,
   "building": "Midtown",
   "lng": -122.128288,
   "lat": 37.4343797,
   "city": "Palo Alto",
   "area": "Palo Alto",
   "level2": "Midtown"
  },
  "availableFrom": "2026-12-07",
  "baseRent": {
   "amount": 5770,
   "currency": "USD"
  },
  "cityCode": "SFO",
  "source": "blueground",
  "type": "APARTMENT",
  "utilities": [
   {
    "name": "LIVE_TV"
   },
   {
    "name": "POWER"
   }
  ],
  "utilitiesIncludedInRent": true,
  "photos": [
   {
    "url": "https://photos2.theblueground.com/736/pg76363-o-9d535dda-dc99-428c-aab3-5db7576f4c34.jpeg",
    "captionKey": "livingRoom",
    "tags": [
     "furnished",
     "livingRoom"
    ]
   }
  ],
  "rent": {
   "amount": 10445,
   "currency": "USD",
   "minDuration": {
    "start": "2027-01-11",
    "end": "2027-02-15"
   }
  }
 },
 "rb1868": null
}''')
# real rent breakdown of 1868 for 2027-01-11..2027-02-15 (its search total is 5555)
RB1868 = {"totalAmount": 5555, "currency": "USD", "lineItems": [
    {"itemName": "RENT", "itemAmount": 5130, "itemTotal": 6046.07},
    {"itemName": "CLIENT_INSURANCE", "itemAmount": 35, "itemTotal": 35},
    {"itemName": "UTILITIES", "itemAmount": 390, "itemTotal": 459.64}]}
RB1868_YEAR = {"totalAmount": 4900, "lineItems": [{"itemName": "RENT", "itemAmount": 4475, "itemTotal": 53700},
                                                 {"itemName": "UTILITIES", "itemAmount": 390, "itemTotal": 4680},
                                                 {"itemName": "CLIENT_INSURANCE", "itemAmount": 35, "itemTotal": 420}]}
TEMP = {"name": "temp", "term": "short", "check_in": "2027-01-11", "check_out": "2027-02-15"}
YEAR = {"name": "year", "term": "long", "check_in": "2027-02-15", "check_out": "2028-02-15"}


def _page(units, total=None):
    return {"totalItems": len(units) if total is None else total, "properties": {"main": units}}


def fake(undated, temp, year, calls, fail=None):
    """get(url) answering the search by its dates and the breakdowns by unit id."""
    def get(url):
        calls.append(url)
        if fail and fail in url:
            return 503, None, "Service Unavailable"
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path.endswith("/rent-breakdown"):
            pid = int(u.path.split("/")[-2])
            assert pid == FX["q1868"]["id"]
            return 200, (RB1868 if q["checkIn"] == TEMP["check_in"] else RB1868_YEAR), ""
        assert u.path == "/api/sp" and q["items"] == "18" and q["ne"] and q["sw"]
        units = {None: undated, TEMP["check_in"]: temp, YEAR["check_in"]: year}[q.get("checkIn")]
        page = int(q["offset"])
        return 200, _page(units[page * 18:(page + 1) * 18], len(units)), ""
    return get


def _year_quote():
    return {**FX["q1868"], "rent": {"amount": 4900, "currency": "USD"}}


def test_rows_one_per_unit_and_stay_base_rent_with_fees_and_no_minimum_stay():
    units = [FX["u1895"], FX["u1868_undated"]]
    quotes = {"temp": {"SFO-1868": FX["q1868"]}, "year": {"SFO-1868": _year_quote()}}
    splits = {("SFO-1868", "temp"): bg.split(RB1868), ("SFO-1868", "year"): bg.split(RB1868_YEAR)}
    rows = {r["source_id"]: r for r in bg.rows(units, [TEMP, YEAR], quotes, splits)}
    # 1895 is not quoted: its short row says so, and it has no long row (a long stay is only an offer)
    assert sorted(rows) == ["SFO-1868:temp", "SFO-1868:year", "SFO-1895:temp"]
    t = rows["SFO-1868:temp"]
    assert t["price"] == 5130  # base rent; the search's 5555 is rent + utilities + insurance
    assert (t["query_checkin"], t["query_checkout"]) == ("2027-01-11", "2027-02-15")
    d = t["data"]
    assert d.extra["total_monthly"] == 5555 and d.extra["fees"] == {"client_insurance": 35, "utilities": 390}
    assert d.extra["stay_total"] == 6540.71 and d.extra["list_price"] == 5600
    assert d.address == "2721 Midtown Court #1868, Palo Alto, CA" and d.unit == "1868"
    assert (d.term, d.furnished, d.private_bath, d.operator, d.kind) == ("short", True, True, "Blueground", "apartment")
    assert (d.beds, d.baths, d.sqft, d.available_from) == (1, 1, 716, "2027-01-11")
    assert "min_stay_days" not in d.extra and "minimum_stay_days" not in d.extra  # minDuration is a pricing window
    assert rows["SFO-1868:year"]["price"] == 4475 and rows["SFO-1868:year"]["data"].term == "long"
    u = rows["SFO-1895:temp"]
    assert u["price"] is None and u["query_checkin"] is None
    assert u["data"].extra["availability"] == "Unavailable for 2027-01-11 to 2027-02-15"
    assert u["data"].available_from == "2026-12-07"
    assert u["data"].extra["pricing_window"]["months"] == 4  # kept, never read as a minimum stay


def test_without_breakdown_the_total_is_the_price_and_marked():
    rows = bg.rows([FX["u1868_undated"]], [TEMP], {"temp": {"SFO-1868": FX["q1868"]}}, {})
    assert rows[0]["price"] == 5555 and rows[0]["data"].extra["price_includes_fees"] is True


def test_street_and_name():
    assert bg.street_and_name("Cowper Apartments, 837 Cowper St") == ("837 Cowper St", "Cowper Apartments")
    assert bg.street_and_name("2721 Midtown Court") == ("2721 Midtown Court", None)
    assert bg.street_and_name("Seale Ave") == (None, None)  # a partner home: no house number, coordinates only


def _cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(bg.time, "sleep", lambda s: None)


def test_a_full_read_stores_rows_and_the_next_retires_a_unit_gone(conn, tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    calls = []
    out = bg.read(conn, get=fake([FX["u1895"], FX["u1868_undated"]], [FX["q1868"]], [_year_quote()], calls))
    assert out["units"] == 2 and out["observations"] == 3 and out["quoted"] == {"temp": 1, "year": 1}
    assert len(calls) == 5  # 3 search pages + 2 breakdowns, nothing else
    act = {r["source_id"]: r["active"] for r in conn.execute("SELECT * FROM listings WHERE source = 'blueground'")}
    assert act == {"SFO-1868:temp": 1, "SFO-1868:year": 1, "SFO-1895:temp": 1}
    r = conn.execute("SELECT * FROM listings WHERE source_id = 'SFO-1868:temp'").fetchone()
    assert r["price"] == 5130 and r["query_checkin"] == "2027-01-11"
    raw = json.loads((tmp_path / "raw" / "blueground" / f"run{out['run_id']}.json").read_text())
    assert len(raw["log"]) == 5
    # replaying the raw file makes no request
    again = bg.read(conn, get=lambda url: pytest.fail("no request"), from_raw=out["raw"], only=["temp"])
    assert again["observations"] == 2
    # 1895 gone from Blueground: the next full run retires it
    bg.read(conn, get=fake([FX["u1868_undated"]], [FX["q1868"]], [_year_quote()], []))
    act = {r["source_id"]: r["active"] for r in conn.execute("SELECT * FROM listings WHERE source = 'blueground'")}
    assert act["SFO-1895:temp"] == 0 and act["SFO-1868:temp"] == 1


def test_a_failed_or_short_read_retires_nothing(conn, tmp_path, monkeypatch):
    _cfg(tmp_path, monkeypatch)
    bg.read(conn, get=fake([FX["u1895"], FX["u1868_undated"]], [FX["q1868"]], [], []))
    with pytest.raises(bg.FeedError):
        bg.read(conn, get=fake([FX["u1868_undated"]], [FX["q1868"]], [], [], fail="checkIn=2027-01-11"))
    st = [r["status"] for r in conn.execute("SELECT status FROM runs WHERE source = 'blueground' ORDER BY id")]
    assert st == ["ok", "error"]
    assert conn.execute("SELECT active FROM listings WHERE source_id = 'SFO-1895:temp'").fetchone()[0] == 1


def test_pages_that_do_not_add_up_to_total_items_stop():
    c = bg.Client(lambda url: (200, _page([FX["u1895"]], total=5), ""), 0)
    with pytest.raises(bg.FeedError, match="totalItems 5"):
        bg.search(c, {"placeId": "x"})


def test_blocked_stops():
    c = bg.Client(lambda url: (429, None, "Too Many Requests"), 0)
    with pytest.raises(bg.FeedError, match="blocked"):
        bg.search(c, {"placeId": "x"})


def test_blueground_rows_decide_at_their_stay_place(conn):
    """A Zumper repost without dates no longer holds a ~blueground place at needs_check once Blueground's own row
    for the unit is there (here: unavailable for the goal's dates)."""
    from aptfinder.config import Goal
    from aptfinder.filter import evaluate_goal, goal_inputs

    pk = "p_x~blueground"
    for src, sid, data, price in [
            ("zumper", "62264037", {"address": "2721 Midtown Ct #1895, Palo Alto, CA", "beds": 1, "baths": 1,
                                    "term": "short", "furnished": True, "private_bath": True,
                                    "operator": "Blueground"}, 3860),
            ("blueground", "SFO-1895:temp", {"address": "2721 Midtown Court #1895, Palo Alto, CA", "beds": 1,
                                             "baths": 1, "term": "short", "furnished": True, "private_bath": True,
                                             "operator": "Blueground", "available_from": "2027-02-01"}, None)]:
        run = start_run(conn, "intake", src, {"scope": "full"})
        append_observation(conn, run, src, sid, data, price=price, price_basis="month" if price else None)
        finish_run(conn, run, "ok", {})
        conn.execute("INSERT INTO links (ts, author, source, source_id, place_key, unit_key, reason) "
                     "VALUES ('2026-11-25', 'test', ?, ?, ?, ?, 'test')", (src, sid, pk, pk + "/1895"))
    conn.commit()
    goal = Goal(label="temp", rules=[{"type": "dates_cover", "start": "2027-01-11", "end": "2027-02-15"}],
                nice=[])
    inp = goal_inputs(conn)
    assert pk in inp["feed"]
    lres, pres = evaluate_goal(inp, goal)
    assert lres[("zumper", "62264037")]["status"] == "needs_check"  # still judged, shown in the drawer
    assert lres[("zumper", "62264037")]["decides"] is False
    assert pres[pk]["status"] == "fail" and pres[pk]["source"] == "blueground"
