"""Adapters parse the shapes seen in the 2026-11-24 trials. Fixtures are trimmed copies of real items."""

from __future__ import annotations

from datetime import date

from aptfinder.intake import ADAPTERS
from aptfinder.intake.base import Parsed, suspicious
from aptfinder.intake.lease import _avail, _first_num
from aptfinder.store import append_observation, start_run

AIRBNB_CFG = {"checkin": "2026-12-01", "checkout": "2027-01-01"}


def airbnb_item(**kw):
    item = {"id": "48905693", "roomType": "Private room", "location": "Los Altos",
            "sharingConfigTitle": "Home in Los Altos · ★4.87 · 1 bedroom · 1 bed · 1 private bath",
            "subDescription": {"items": ["1 queen bed", "Dedicated bathroom"]},
            "coordinates": {"latitude": 37.37946, "longitude": -122.10106}, "isAvailable": True,
            "price": {"label": "$2,814 per month", "qualifier": "month", "price": "$2,814"},
            "amenities": [{"title": "Bathroom", "values": [{"title": "Hair dryer", "available": True}]}]}
    item.update(kw)
    return item


def test_airbnb_private_room():
    p = ADAPTERS["airbnb"].parse(airbnb_item(), AIRBNB_CFG)
    assert (p.price, p.price_basis) == (2814, "month")
    assert p.data.kind == "room" and p.data.private_bath is True and p.data.beds == 1 and p.data.baths == 1
    assert (p.query_checkin, p.query_checkout) == ("2026-12-01", "2027-01-01")
    assert p.url == "https://www.airbnb.com/rooms/48905693"


def test_airbnb_shared_and_ambiguous_baths():
    shared = airbnb_item(sharingConfigTitle="Home · 1 bedroom · 1 bed · 2 shared baths", subDescription={"items": []})
    assert ADAPTERS["airbnb"].parse(shared, AIRBNB_CFG).data.private_bath is False
    vague = airbnb_item(sharingConfigTitle="Home · 1 bedroom · 2 beds · 2.5 baths", subDescription={"items": []})
    assert ADAPTERS["airbnb"].parse(vague, AIRBNB_CFG).data.private_bath is None  # unknown, not guessed
    entire = airbnb_item(roomType="Entire home/apt", sharingConfigTitle="Rental unit · Studio · 1 bed · 1 bath")
    d = ADAPTERS["airbnb"].parse(entire, AIRBNB_CFG).data
    assert d.kind == "entire_place" and d.private_bath is True and d.beds == 0


def test_furnishedfinder():
    item = {"listing_id": "1070098_1", "property_type_class": "room", "property_type": "Room - House",
            "available_on": "Available: Dec. 13, 2026", "rent_amount": "$1600.00/month", "bedrooms": 1,
            "bathrooms": 1, "latitude": 37.39, "longitude": -122.09, "city": "mountain-view",
            "url": "https://www.furnishedfinder.com/property/1070098_1"}
    p = ADAPTERS["furnishedfinder"].parse(item, {})
    assert p.price == 1600 and p.price_basis == "month" and p.data.available_from == "2026-12-13"
    assert p.data.kind == "room" and p.data.private_bath is None and p.data.city == "Mountain View"


def test_zillow_building_splits_by_bedrooms():
    item = {"listingUrl": "https://www.zillow.com/homedetails/466646297_zpid/", "rawData": {"property": {
        "zpid": 466646297, "propertyType": "apartment", "title": "Arley",
        "address": {"streetAddress": "111 Lena Way #6903513", "city": "Mountain View", "state": "CA",
                    "zipcode": "94040"},
        "location": {"latitude": 37.39, "longitude": -122.1},
        "unitsGroup": [{"bedrooms": 0, "minPrice": 3885, "isRoomForRent": False},
                       {"bedrooms": 3, "minPrice": 7922, "isRoomForRent": False}]}}}
    out = ADAPTERS["zillow"].parse(item, {})
    assert [p.source_id for p in out] == ["466646297:studio", "466646297:3bd"]
    assert out[1].data.beds == 3 and out[1].data.baths is None and out[1].price == 7922
    assert out[1].data.address == "111 Lena Way, Mountain View, CA 94040"  # placeholder unit dropped


def test_apartments_units_plans_and_single_rentals():
    building = {"listingId": "8mzlqg0", "listingName": "Landsby", "listingAddress": "100 Aspen Way",
                "listingCity": "Mountain View", "listingState": "CA", "listingZip": "94040",
                "location": {"latitude": 37.4057, "longitude": -122.1094}, "url": "u",
                "models": [{"modelName": "Three Bedroom", "rentLabel": "$7,836",
                            "details": ["3 Beds", "2 Baths", "1,248 Sq Ft"], "units": []},
                           {"modelName": "One Bedroom", "rentLabel": "$4,175", "details": ["1 Bed", "1 Bath"],
                            "units": [{"type": "A-101", "price": "$4,175", "sqft": "577", "availability": "Now"}]}]}
    out = ADAPTERS["apartments_com"].parse(building, {})
    plan = next(p for p in out if p.data.beds == 3)
    assert plan.source_id == "8mzlqg0:m:three-bedroom-1248" and plan.data.sqft == 1248 and plan.price == 7836
    unit = next(p for p in out if p.data.unit == "A-101")
    assert unit.source_id == "8mzlqg0:u:A-101" and unit.data.available_from == date.today().isoformat()
    single = {"listingId": "v3wxv5g", "listingName": "", "listingAddress": "555 San Antonio Rd",
              "listingCity": "Mountain View", "listingState": "CA", "listingZip": "94040", "location": {},
              "propertyType": "ApartmentUnitForRent", "models": [],
              "rentals": [{"RentalKey": "h0g3303", "Beds": 1, "Baths": 1, "Rent": 3000, "SquareFeet": 651,
                           "UnitNumber": "GO FL2-ID279", "AvailableDate": "2026-11-24T00:00:00",
                           "Description": "contemporary furnished apartment (ID #SFO279)"}]}
    (s,) = ADAPTERS["apartments_com"].parse(single, {})
    assert s.source_id == "v3wxv5g:r:h0g3303" and s.price == 3000 and s.data.furnished is True
    assert s.data.property_name is None


def test_craigslist():
    item = {"postId": "7998593532", "priceUsd": 4001, "location": "Mountain View", "latitude": 37.399,
            "longitude": -122.094, "postedAt": "2026-11-10T14:27:12-0800", "url": "u",
            "attributes": {"bedrooms": "2", "bathrooms": "1.5", "sqft": "1064", "housing_type": "apartment",
                           "rent_period": "monthly", "available": "dec 13", "laundry": "w/d in unit"}}
    p = ADAPTERS["craigslist"].parse(item, {})
    assert p.price == 4001 and p.price_basis == "month" and p.data.term == "long"
    assert (p.data.beds, p.data.baths, p.data.sqft) == (2, 1.5, 1064)
    assert p.data.available_from == "2026-12-13"


def test_avail_and_numbers():
    ref = date(2026, 11, 24)
    assert _avail("now", ref) == "2026-11-24"
    assert _avail("Jan 5", ref) == "2027-01-05"  # earlier month than ref rolls to next year
    assert _avail("2026-12-01T00:00:00-05:00", ref) == "2026-12-01"
    assert _first_num("1,248 Sq Ft") == 1248


def test_suspicious_flags():
    assert suspicious([], 0, ("price",)) == ["actor returned no items"]
    from aptfinder.store.models import ObservationData

    ps = [Parsed(str(i), None, ObservationData(lat=None), price=1000) for i in range(10)]
    w = suspicious(ps, 10, ("price", "lat"), n_unparsed=0)
    assert any("lat missing" in x for x in w) and any("identical" in x for x in w)


def test_observation_roundtrip_of_parsed(conn):
    p = ADAPTERS["airbnb"].parse(airbnb_item(), AIRBNB_CFG)
    run = start_run(conn, "intake", "airbnb")
    append_observation(conn, run, "airbnb", p.source_id, p.data, url=p.url, price=p.price, price_basis=p.price_basis,
                       query_checkin=p.query_checkin, query_checkout=p.query_checkout)
    row = conn.execute("SELECT private_bath, price_month FROM listings").fetchone()
    assert row["private_bath"] == 1 and row["price_month"] == 2814


def test_furnished_text_and_blueground_units_are_short_stays():
    from aptfinder.intake.lease import furnished_from_text

    assert furnished_from_text("Spacious unfurnished 2BR") is False
    assert furnished_from_text("elegantly furnished apartment") is True
    assert furnished_from_text("in-unit laundry") is None
    assert furnished_from_text("looking for an unfurnished lease? upgrade to furnished living") is None
    single = {"listingId": "99nmck6", "listingName": "", "listingAddress": "1600 Villa St", "listingCity": "Mountain View",
              "listingState": "CA", "listingZip": "94041", "location": {}, "propertyType": "ApartmentUnitForRent",
              "models": [], "rentals": [{"RentalKey": "k", "Beds": 1, "Baths": 1, "Rent": 2460, "MinLeaseTerm": 1,
                                          "Description": "Were you looking for an unfurnished lease? ... (ID #SFO1141)"}]}
    (p,) = ADAPTERS["apartments_com"].parse(single, {})
    assert p.data.furnished is True and p.data.term == "short" and p.data.operator == "Blueground"
    plain = {**single, "rentals": [{**single["rentals"][0], "MinLeaseTerm": 12, "Description": "unfurnished home"}]}
    (q,) = ADAPTERS["apartments_com"].parse(plain, {})
    assert q.data.furnished is False and q.data.term == "long" and q.data.operator is None
