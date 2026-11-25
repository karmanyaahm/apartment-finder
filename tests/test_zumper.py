"""Zumper and PadMapper parsers, reparse and `aptf-verify samples`. Fixtures are trimmed copies of real items from the
2026-11-24 trials (agent names and phones dropped)."""

from __future__ import annotations

import json

from aptfinder.intake import ADAPTERS
from aptfinder.intake.base import reparse
from aptfinder.verify.cli import samples

# ------------------------------------------------------------------------------------------------ Zumper
ZUMPER_BUILDING = {
    "listing_id": 16558647, "building_id": 173, "building_name": "Reserve at Mountain View",
    "address": "870 E El Camino Real", "city": "Mountain View", "state": "CA", "zipcode": "94040",
    "lat": 37.3786, "lng": -122.0712, "min_price": 3408, "max_price": 4215, "min_bedrooms": 1, "max_bedrooms": 2,
    "min_bathrooms": 1, "max_bathrooms": 2, "min_square_feet": 694, "floorplan_count": 3, "lease_type": 1,
    "property_type": 4, "image_ids": [677678395], "title": "1 Bedroom B",
    "url": "/apartment-buildings/p12999/reserve-at-mountain-view-mountain-view-ca",
    "padmapper_url": "/buildings/p12999/reserve-at-mountain-view-apartments-at-870-e-el-camino-real-mountain-view-ca-94040",
    "detail_entity_data_homepage":
        "https://equityapartments.com/san-francisco-bay/mountain-view/reserve-at-mountain-view-apartments",
    "detail_entity_data_floorplan_listings": [
        {"listing_id": 18832950, "title": "1 Bedroom C", "bedrooms": 1, "bathrooms": 1, "half_bathrooms": 0,
         "square_feet": 694, "min_price": 3408, "max_price": 3535, "date_available": "2026/11/24", "listing_status": 1,
         "unit_data": {"active": 2}},
        {"listing_id": 18832949, "title": "2 Bedrooms B", "bedrooms": 2, "bathrooms": 2, "half_bathrooms": 0,
         "square_feet": 874, "min_price": 4215, "max_price": 4215, "date_available": "2026/11/24", "listing_status": 1,
         "unit_data": {"active": 1}},
        {"listing_id": 16558648, "title": "2 Bedrooms A", "bedrooms": 2, "bathrooms": 1, "square_feet": 874,
         "min_price": 0, "listing_status": 6, "unit_data": {"active": 0}},  # off the market
        {"listing_id": 24234054, "title": "B5", "bedrooms": 2, "bathrooms": 2, "square_feet": 1201,
         "min_price": 0, "listing_status": 1, "unit_data": {"active": 0}},  # listed but nothing offered, no price
    ]}

ZUMPER_BLUEGROUND = {
    "listing_id": 62879871, "address": "854 Jordan Ave #2092", "city": "Los Altos", "state": "CA", "zipcode": "94022",
    "lat": 37.38, "lng": -122.11, "min_price": 3560, "max_price": 3560, "min_bedrooms": 1, "max_bedrooms": 1,
    "min_bathrooms": 1, "max_bathrooms": 1, "min_square_feet": 845, "max_square_feet": 845, "floorplan_count": 1,
    "lease_type": 10, "min_lease_days": 30, "property_type": 4,
    "provider_url": "https://www.theblueground.com/p/furnished-apartments/sfo-2092?utm_source=classified-10",
    "url": "/listings/62879871/1-bedroom-north-los-altos-los-altos-ca", "detail_entity_data_floorplan_listings": None}


def test_zumper_building_gives_offered_plans_and_website():
    out = ADAPTERS["zumper"].parse(ZUMPER_BUILDING, {})
    assert [(p.source_id, p.price, p.data.beds, p.data.baths, p.data.sqft, p.data.available_from) for p in out] == [
        ("plan:18832950", 3408, 1, 1, 694, "2026-11-24"),
        ("plan:18832949", 4215, 2, 2, 874, "2026-11-24")]
    d = out[0].data
    assert d.floor_plan == "1 Bedroom C" and d.property_name == "Reserve at Mountain View" and d.term == "long"
    assert d.address == "870 E El Camino Real, Mountain View, CA 94040"
    assert d.extra["website"] == "https://equityapartments.com/san-francisco-bay/mountain-view/reserve-at-mountain-view-apartments/"
    assert out[0].url == "https://www.zumper.com/apartment-buildings/p12999/reserve-at-mountain-view-mountain-view-ca"


def test_zumper_furnished_stay_and_summary():
    p, = ADAPTERS["zumper"].parse(ZUMPER_BLUEGROUND, {})
    assert (p.source_id, p.price, p.data.beds, p.data.sqft) == ("62879871", 3560, 1, 845)
    assert (p.data.operator, p.data.furnished, p.data.term) == ("Blueground", True, "short")
    # a listing spanning bedroom counts without plan details is the building, not an offer
    s, = ADAPTERS["zumper"].parse({**ZUMPER_BUILDING, "detail_entity_data_floorplan_listings": None}, {})
    assert s.data.kind == "site_info" and s.price is None and s.source_id == "building:173"


# --------------------------------------------------------------------------------------------- PadMapper
PADMAPPER_UNIT = {
    "listing_id": 41703699, "address": "2065 California St", "city": "Mountain View", "state": "CA",
    "zipcode": "94040", "latitude": 37.40, "longitude": -122.10, "price_min": 3150, "price_max": 3150,
    "beds_min": 1, "beds_max": 1, "baths_min": 1, "baths_max": 1, "square_feet_min": 800, "square_feet_max": 800,
    "property_type_code": 4, "date_available": "2026-12-01", "name": "One Bedroom One Bath",
    "url": "https://www.padmapper.com/buildings/p209871/el-portal-apartments-at-2065-california-st-mountain-view-ca-94040"}


def test_padmapper_unit_and_building_range():
    p, = ADAPTERS["padmapper"].parse(PADMAPPER_UNIT, {})
    assert (p.source_id, p.price, p.data.beds, p.data.baths, p.data.sqft, p.data.available_from) == (
        "41703699", 3150, 1, 1, 800, "2026-12-01")
    assert p.data.property_name == "El Portal Apartments" and p.data.kind == "apartment"
    rng = {**PADMAPPER_UNIT, "beds_min": 1, "beds_max": 3, "price_max": 5449,
           "url": "https://www.padmapper.com/buildings/1724400/apartments-at-2400-w-el-camino-real-mountain-view-ca-94040"}
    s, = ADAPTERS["padmapper"].parse(rng, {})
    assert s.data.kind == "site_info" and s.price is None and s.data.property_name is None  # 'apartments' is no name
    assert s.data.extra["beds_range"] == [1, 3]


# ------------------------------------------------------------------------------- reparse and verify samples
def test_reparse_and_samples_are_reproducible(conn, tmp_path):
    raw = tmp_path / "run1_Mountain_View.json"
    raw.write_text(json.dumps({"actor": "scrapemind/zumperCom-Scraper", "items": [ZUMPER_BUILDING, ZUMPER_BLUEGROUND]}))
    res = reparse(conn, ADAPTERS["zumper"], str(raw))
    assert res["observations"] == 3
    a = samples(conn, "zumper", 2, seed=7)
    assert a == samples(conn, "zumper", 2, seed=7) and len(a) == 2
    assert all(s["open"].startswith("https://www.zumper.com/") for s in a)
    assert {"price", "beds", "available_from", "extra.website"} <= set(a[0]["fields"])


def test_reparse_covers_credits_the_trial_areas(conn, tmp_path):
    from aptfinder.intake.base import coverage

    raw = tmp_path / "trial_zumper_mountain_view.json"
    raw.write_text(json.dumps({"actor": "scrapemind/zumperCom-Scraper", "areas": ["Mountain View"],
                               "items": [ZUMPER_BUILDING]}))
    reparse(conn, ADAPTERS["zumper"], str(raw))
    assert not [k for k in coverage(conn) if k[0] == "zumper"]  # a plain reparse is never coverage
    reparse(conn, ADAPTERS["zumper"], str(raw), covers="partial")
    cov = {k: v["state"] for k, v in coverage(conn).items() if k[0] == "zumper"}
    assert len(cov) == 1 and list(cov.values()) == ["partial"]
