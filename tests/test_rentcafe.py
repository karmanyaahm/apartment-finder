"""RentCafe parser. Fixtures are trimmed copies of real items from the 2026-11-25 Mountain View probe
(benthepythondev/rentcafe-apartments-scraper with details; descriptions, images and phones dropped)."""

from __future__ import annotations

import json

from aptfinder.intake import ADAPTERS
from aptfinder.intake.base import coverage, reparse

MARI_COURT = {
    "name": "Mari-Court Apartments",
    "url": "https://www.rentcafe.com/apartments/ca/mountain-view/mari-court-apartments/default.aspx",
    "street": "582 & 586 Mariposa Ave", "city": "Mountain View", "state": "CA", "zip": "94041",
    "latitude": 37.3928, "longitude": -122.0912, "rentMin": 2895, "rentMax": 2895, "currency": "USD",
    "petFriendly": True, "amenities": ["Pet friendly"], "searchLocation": "Mountain View, CA", "siteTotal": 462,
    "propertyId": "1438231", "floorPlanCount": 2, "bedsMin": 1, "bedsMax": 2, "hasAvailableUnits": True,
    "units": None, "applicationFee": 30, "catsAllowed": True, "dogsAllowed": True, "detailsMissing": False,
    "floorPlans": [
        {"floorPlanId": "4114487", "name": "2-Bed, 1-Bath", "beds": 2, "baths": 1, "sqft": 688, "rentMin": 2895,
         "rentMax": 2895, "rentText": "$2,895", "hasAvailableUnits": True, "priceDrop": False, "rentPerSqft": 4.21},
        {"floorPlanId": "4114486", "name": "1-Bed, 1-Bath", "beds": 1, "baths": 1, "sqft": 600, "rentMin": None,
         "rentMax": None, "rentText": "Ask for pricing", "hasAvailableUnits": False, "priceDrop": False,
         "rentPerSqft": None},
    ],
}

GREENWOOD = {  # every plan "Ask for pricing": plans without an offer
    "name": "Greenwood", "url": "https://www.rentcafe.com/apartments/ca/mountain-view/greenwood/default.aspx",
    "street": "1797 California Street", "city": "Mountain View", "state": "CA", "zip": "94041",
    "latitude": 37.3956, "longitude": -122.0926, "rentMin": None, "rentMax": None, "petRent": 100,
    "amenities": ["Pet friendly", "Washer/Dryer", "Pool"], "searchLocation": "Mountain View, CA", "siteTotal": 462,
    "propertyId": "221177", "floorPlanCount": 2, "hasAvailableUnits": False, "detailsMissing": False,
    "floorPlans": [
        {"floorPlanId": "3128053", "name": "One Bedroom One Bathroom", "beds": 1, "baths": 1, "sqft": 600,
         "rentMin": None, "rentMax": None, "rentText": "Ask for pricing", "hasAvailableUnits": False},
        {"floorPlanId": "1046323", "name": "House", "beds": 2, "baths": 1, "sqft": 807, "rentMin": None,
         "rentMax": None, "rentText": "Ask for pricing", "hasAvailableUnits": False},
    ],
}

VERONA = {  # the Mountain View search spills into Sunnyvale (plan made up: the probe item had no priced plan)
    "name": "Verona Gardens", "url": "https://www.rentcafe.com/apartments/ca/sunnyvale/verona-gardens-0/default.aspx",
    "street": "471 Acalanes Drive", "city": "Sunnyvale", "state": "CA", "zip": "94086", "latitude": 37.38,
    "longitude": -122.04, "searchLocation": "Mountain View, CA", "siteTotal": 462, "propertyId": "999",
    "floorPlans": [{"floorPlanId": "9", "name": "Studio", "beds": 0, "baths": 1, "sqft": None, "rentMin": 1600,
                    "rentMax": 1600, "rentText": "$1,600", "hasAvailableUnits": True}],
}


def test_priced_plans_are_offers_and_ask_for_pricing_is_a_plan_without_price():
    p, ask = ADAPTERS["rentcafe"].parse(MARI_COURT, {})
    assert (p.source_id, p.price, p.price_basis) == ("plan:4114487", 2895, "month")
    d = p.data
    assert (d.beds, d.baths, d.sqft, d.floor_plan, d.kind, d.term) == (2, 1, 688, "2-Bed, 1-Bath", "apartment", "long")
    assert d.address == "582 & 586 Mariposa Ave, Mountain View, CA 94041" and d.property_name == "Mari-Court Apartments"
    assert (d.lat, d.lon) == (37.3928, -122.0912)
    assert d.extra["website"] is None  # rentcafe.com is an aggregator page, never the building's own site
    assert d.extra["plan_available"] is True and d.extra["application_fee"] == 30 and d.extra["vertical"] == "apartments"
    # the 1x1 says "Ask for pricing": its beds, baths and sqft are kept, no price is invented
    assert (ask.source_id, ask.price, ask.price_basis) == ("plan:4114486", None, None)
    assert (ask.data.beds, ask.data.baths, ask.data.sqft, ask.data.kind) == (1, 1, 600, "apartment")
    assert ask.data.extra["rent_text"] == "Ask for pricing" and ask.data.extra["plan_price_max"] is None


def test_building_whose_plans_all_ask_for_pricing_gives_its_plans():
    out = ADAPTERS["rentcafe"].parse(GREENWOOD, {})
    assert [(p.source_id, p.price, p.data.beds, p.data.baths, p.data.sqft) for p in out] == [
        ("plan:3128053", None, 1, 1, 600), ("plan:1046323", None, 2, 1, 807)]


def test_a_plan_that_is_no_home_is_dropped():
    """Real plans (Carriage House 'RV Storage' $300, Mathilda Development 'Office' $1,500): 0 beds, no baths."""
    plans = [{"floorPlanId": "2132126", "name": "RV Storage", "beds": 0, "baths": None, "sqft": None,
              "rentMin": 300, "rentText": "$300"},
             {"floorPlanId": "4277368", "name": "Office", "beds": 0, "baths": None, "sqft": 637, "rentMin": 1500},
             {"floorPlanId": "1", "name": "SRO", "beds": 0, "baths": None, "sqft": 104, "rentMin": None},
             {"floorPlanId": "2", "name": "2 Bed with Office", "beds": 2, "baths": 2, "sqft": 1100, "rentMin": 4000}]
    out = ADAPTERS["rentcafe"].parse({**GREENWOOD, "floorPlans": plans}, {})
    assert [p.source_id for p in out] == ["plan:1", "plan:2"]
    s, = ADAPTERS["rentcafe"].parse({**GREENWOOD, "floorPlans": plans[:2]}, {})
    assert s.source_id == "building:221177" and s.data.kind == "site_info"


def test_building_without_a_floor_plan_is_site_info():
    s, = ADAPTERS["rentcafe"].parse({**GREENWOOD, "floorPlans": [], "floorPlanCount": 0}, {})
    assert s.source_id == "building:221177" and s.price is None and s.data.kind == "site_info"
    assert s.data.extra["plan_count"] == 0


def test_own_site_link_is_kept_as_website():
    own = {**GREENWOOD, "url": "https://www.rentcafewebsite.com/greenwood/default.aspx?x=1"}
    s, _ = ADAPTERS["rentcafe"].parse(own, {})
    assert s.data.extra["website"] == "https://www.rentcafewebsite.com/greenwood/default.aspx"


def test_items_count_for_the_city_they_state(conn, tmp_path):
    raw = tmp_path / "run1_Mountain_View.json"
    raw.write_text(json.dumps({"actor": "benthepythondev/rentcafe-apartments-scraper", "areas": ["Mountain View"],
                               "items": [MARI_COURT, GREENWOOD, VERONA]}))
    res = reparse(conn, ADAPTERS["rentcafe"], str(raw), covers="complete")
    assert res["observations"] == 5
    tags = dict(conn.execute("SELECT source_id, json_extract(data, '$.extra.area') FROM observations "
                             "WHERE source = 'rentcafe'").fetchall())
    assert tags == {"plan:4114487": "Mountain View", "plan:4114486": "Mountain View", "plan:3128053": "Mountain View",
                    "plan:1046323": "Mountain View", "plan:9": None}
    assert coverage(conn)[("rentcafe", "Mountain View")]["state"] == "complete"
