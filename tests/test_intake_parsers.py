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


def test_airbnb_monthly_qualifier_is_per_month():
    item = airbnb_item(price={"label": "$6,709 monthly, originally $8,786", "qualifier": "monthly",
                              "price": "$6,709", "discountedPrice": "$6,709"})
    p = ADAPTERS["airbnb"].parse(item, AIRBNB_CFG)
    assert (p.price, p.price_basis) == (6709, "month")  # not a total for the dates


def test_airbnb_shared_and_ambiguous_baths():
    shared = airbnb_item(sharingConfigTitle="Home · 1 bedroom · 1 bed · 2 shared baths", subDescription={"items": []})
    assert ADAPTERS["airbnb"].parse(shared, AIRBNB_CFG).data.private_bath is False
    vague = airbnb_item(sharingConfigTitle="Home · 1 bedroom · 2 beds · 2.5 baths", subDescription={"items": []})
    assert ADAPTERS["airbnb"].parse(vague, AIRBNB_CFG).data.private_bath is None  # unknown, not guessed
    entire = airbnb_item(roomType="Entire home/apt", sharingConfigTitle="Rental unit · Studio · 1 bed · 1 bath")
    d = ADAPTERS["airbnb"].parse(entire, AIRBNB_CFG).data
    assert d.kind == "entire_place" and d.private_bath is True and d.beds == 0


# Trimmed items of kaix/airbnb-listing-scraper, Mountain View box, 2026-11-25 probe (Jan 11 - Feb 15 2027).
KAIX_ROOM = {
    "id": "982709184868357633", "name": "Silicon Valley~4~Mtn View private primary & bath",
    "sharingTitle": "Home in Mountain View \u00b7 \u26055.0 \u00b7 1 bedroom \u00b7 2 beds \u00b7 2.5 baths",
    "roomType": "private_room", "propertyType": "Private room in home",
    "calendarDescriptionItems": ["Private room in home", "2 beds", "Private attached bathroom"], "personCapacity": 2,
    "overallRating": 5, "overallReviewCount": 8,
    "location": {"lat": 37.39977, "lng": -122.06039, "subtitle": "Mountain View, California, United States"},
    "price": {"label": "$2,561 monthly, originally $2,929", "amount": "$2,561", "qualifier": "monthly"},
    "htmlDescription": "Your bedroom and bathroom are completely private,<br />while the kitchen &amp; living room are shared",
    "amenitiesGrouped": [{"groupTitle": "Bedroom and laundry", "amenities": [{"title": "Washer", "available": True},
                                                                             {"title": "Dryer", "available": True}]},
                         {"groupTitle": "Not included", "amenities": [{"title": "Air conditioning", "available": False}]}],
    "heroImages": [{"baseUrl": "https://a0.muscache.com/im/pictures/5c5884fd-0246-40ea-a7b3-45f1d996c141.jpg"}]}


def test_airbnb_kaix_room():
    cfg = {"checkin": "2027-01-11", "checkout": "2027-02-15"}
    p = ADAPTERS["airbnb"].parse(KAIX_ROOM, cfg)
    assert (p.source_id, p.url) == ("982709184868357633", "https://www.airbnb.com/rooms/982709184868357633")
    assert (p.price, p.price_basis) == (2561, "month")
    assert (p.query_checkin, p.query_checkout) == ("2027-01-11", "2027-02-15")
    d = p.data
    assert (d.kind, d.beds, d.baths, d.private_bath) == ("room", 1.0, 2.5, True)
    assert (d.lat, d.lon, d.city) == (37.39977, -122.06039, "Mountain View")
    assert d.amenities == ["Washer", "Dryer"]  # 'Not included' is not an amenity
    assert d.description == "Your bedroom and bathroom are completely private,\nwhile the kitchen & living room are shared"
    assert d.available_from == "2027-01-11" and d.furnished is True and d.term == "short"


def test_airbnb_kaix_shared_bath_and_entire_home():
    shared = {**KAIX_ROOM, "sharingTitle": "Home in Mountain View \u00b7 1 bedroom \u00b7 1 bed \u00b7 1 shared bath",
              "calendarDescriptionItems": ["Private room in home", "1 bed", "Shared bathroom"]}
    assert ADAPTERS["airbnb"].parse(shared, AIRBNB_CFG).data.private_bath is False
    vague = {**KAIX_ROOM, "sharingTitle": "Home \u00b7 1 bedroom \u00b7 1 bed \u00b7 1 bath",
             "calendarDescriptionItems": ["Private room in home", "1 bed", "1 bath"]}
    assert ADAPTERS["airbnb"].parse(vague, AIRBNB_CFG).data.private_bath is None  # unknown, not guessed
    entire = {**KAIX_ROOM, "roomType": "entire_home",
              "sharingTitle": "Home in Mountain View \u00b7 \u26055.0 \u00b7 3 bedrooms \u00b7 4 beds \u00b7 1 bath"}
    d = ADAPTERS["airbnb"].parse(entire, AIRBNB_CFG).data
    assert (d.kind, d.beds, d.baths, d.private_bath) == ("entire_place", 3.0, 1.0, True)


def test_airbnb_kaix_input_and_area():
    from aptfinder.config import Area
    from aptfinder.intake.airbnb import _coords, build_input

    box = Area(id="b", kind="box", label="b", bbox=[37.38, -122.10, 37.41, -122.06])
    inp = build_input({"checkin": "2027-01-11", "checkout": "2027-02-15", "max_items": 270}, [box])
    assert (inp["latitude"], inp["longitude"]) == (37.395, -122.08)
    assert inp["maxLocationDeviation"] == 2.48  # half the diagonal (2.43 km) plus 50 m: the corners are searched
    assert inp["maxListings"] == 270 and inp["maxSearchPages"] == 15 and inp["checkIn"] == "2027-01-11"
    assert _coords(KAIX_ROOM) == (37.39977, -122.06039)
    assert _coords(airbnb_item()) == (37.37946, -122.10106)  # the earlier actor's shape still works


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
    assert "complex_home_count" not in out[1].data.extra  # only when the card gives one (rows hash as before)
    item["rawData"]["property"]["matchingHomeCount"] = 10
    assert all(p.data.extra["complex_home_count"] == 10 for p in ADAPTERS["zillow"].parse(item, {}))


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


def test_apartments_building_website_company_and_phone():
    item = {"listingId": "x1", "listingName": "Sevens", "listingAddress": "1 A St", "listingCity": "Mountain View",
            "listingState": "CA", "listingZip": "94041", "location": {}, "url": "u",
            "models": [{"modelName": "S1", "rentLabel": "$3,000", "details": ["Studio", "1 Bath"], "units": []}],
            "moreDetails": {"propertyWebsiteUri": "https://sevensmv.com/?funnelleasing=apts.com&utm_source=x",
                            "managementCompany": {"name": "Greystar"}, "phone": "(650) 555-0100"}}
    (p,) = ADAPTERS["apartments_com"].parse(item, {})
    assert p.data.extra["website"] == "https://sevensmv.com/"
    assert (p.data.extra["management_company"], p.data.extra["phone"]) == ("Greystar", "(650) 555-0100")
    item["moreDetails"]["propertyWebsiteUri"] = "https://www.apartments.com/sevens/x1/"  # aggregator: dropped
    assert ADAPTERS["apartments_com"].parse(item, {})[0].data.extra["website"] is None


def test_craigslist():
    item = {"postId": "7998593532", "priceUsd": 4001, "location": "Mountain View", "latitude": 37.399,
            "longitude": -122.094, "postedAt": "2026-11-10T14:27:12-0800", "url": "u",
            "attributes": {"bedrooms": "2", "bathrooms": "1.5", "sqft": "1064", "housing_type": "apartment",
                           "rent_period": "monthly", "available": "dec 13", "laundry": "w/d in unit"}}
    p = ADAPTERS["craigslist"].parse(item, {})
    assert p.price == 4001 and p.price_basis == "month" and p.data.term == "long"
    assert (p.data.beds, p.data.baths, p.data.sqft) == (2, 1.5, 1064)
    assert p.data.available_from == "2026-12-13"


def test_zero_price_is_unknown():
    # real items, trimmed: Craigslist posts with no price come back as priceUsd 0 (Overlook at Walnut Creek,
    # 2026-11-24), FurnishedFinder as "$0.00/month" (Sonesta ES Suites Sunnyvale); 0 means no price given
    cl = {"url": "https://www.craigslist.org/view/d/walnut-creek-convenient-computer-desks/mL9bUtN7qKcVzvx5ucJHRo",
          "postId": "8006083734", "title": "Convenient computer desks and dry bars, EV charging stations",
          "priceUsd": 0, "currency": "USD", "postedAt": "2026-11-24T17:19:19-0800", "location": "walnut creek",
          "latitude": 37.929723, "longitude": -122.053973,
          "attributes": {"bedrooms": "1", "bathrooms": "1", "sqft": "676", "rent_period": "monthly",
                         "housing_type": "apartment", "laundry": "w/d in unit"}}
    p = ADAPTERS["craigslist"].parse(cl, {})
    assert p.price is None and p.price_basis == "month" and p.data.beds == 1
    ff = {"listing_id": "252654_1", "title": "Sonesta ES Suites Sunnyvale California", "rent_amount": "$0.00/month",
          "rent_currency": "USD", "bedrooms": 1, "latitude": 37.37, "longitude": -122.03, "city": "sunnyvale"}
    assert ADAPTERS["furnishedfinder"].parse(ff, {}).price is None


def test_avail_and_numbers():
    ref = date(2026, 11, 24)
    assert _avail("now", ref) == "2026-11-24"
    assert _avail("Jan 5", ref) == "2027-01-05"  # earlier month than ref rolls to next year
    assert _avail("2026-12-01T00:00:00-05:00", ref) == "2026-12-01"
    assert _avail("12/1/2026", ref) == "2026-12-01" and _avail("Available 1/29/2027", ref) == "2027-01-29"  # UDR units
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
    assert p.data.private_bath is True  # a whole furnished home: the bath is the renter's own
    plain = {**single, "rentals": [{**single["rentals"][0], "MinLeaseTerm": 12, "Description": "unfurnished home"}]}
    (q,) = ADAPTERS["apartments_com"].parse(plain, {})
    assert q.data.furnished is False and q.data.term == "long" and q.data.operator is None
    assert q.data.private_bath is None


def test_furnished_stay_template_without_operator_name():
    """Units in Blueground's listing template that never name it are still furnished short stays."""
    item = {"listingId": "e819ehm", "listingName": "", "listingAddress": "908 Rich Ave", "listingCity": "Mountain View",
            "listingState": "CA", "listingZip": "94040", "location": {}, "propertyType": "ApartmentUnitForRent",
            "models": [], "rentals": [{"RentalKey": "k", "Beds": 2, "Baths": 2, "Rent": 9822, "MinLeaseTerm": 1,
                                       "MaxLeaseTerm": 12, "UnitNumber": "SI ID1343876P",
                                       "Description": "The advertised rate of $9,822/month is the lowest available "
                                                      "price, based on a 30 day(s) stay"}]}
    (p,) = ADAPTERS["apartments_com"].parse(item, {})
    assert p.data.furnished is True and p.data.term == "short" and p.data.operator is None
    assert p.data.extra["stay_template"] is True
    bg = {**item, "rentals": [{**item["rentals"][0], "UnitNumber": "SI FL1-ID13012A",
                               "Description": "furnished apartment ... (ID #SFO13012A)"}]}
    (q,) = ADAPTERS["apartments_com"].parse(bg, {})
    assert q.data.operator == "Blueground" and q.data.term == "short"


def test_plan_offers_never_mix():
    from aptfinder.plans import plan_key

    base = {"beds": 1, "baths": 1, "sqft": 701}
    keys = {plan_key({**base, "term": "long"}), plan_key({**base, "term": "long", "furnished": True}),
            plan_key({**base, "term": "short", "furnished": True})}
    assert len(keys) == 3


def test_zillow_baths_from_full_and_half_counts():
    item = {"rawData": {"property": {"zpid": 7, "address": {"streetAddress": "1 A St", "city": "Mountain View",
            "state": "CA", "zipcode": "94041"}, "bedrooms": 4, "bathrooms": 4, "price": 6500,
            "factsAndFeatures": {"fullBathroomCount": 2, "halfBathroomCount": 1}}}}
    (p,) = ADAPTERS["zillow"].parse(item, {})
    assert p.data.baths == 2.5 and p.data.extra["baths_total"] == 4  # not 4: 2 full + 1 half
    item["rawData"]["property"]["factsAndFeatures"] = {}
    assert ADAPTERS["zillow"].parse(item, {})[0].data.baths == 4  # no breakdown: the total
    assert "floorplan_image" not in ADAPTERS["zillow"].parse(item, {})[0].data.extra


def test_zillow_unit_floor_plan_drawing():
    # trimmed from zpid 463499730 (run 4): Zillow's own floor plan drawing of a single unit
    item = {"rawData": {"property": {"zpid": 463499730, "address": {"streetAddress": "1 A St", "city": "Mountain View",
            "state": "CA", "zipcode": "94041"}, "bedrooms": 2, "bathrooms": 2, "price": 4000, "hasFloorPlan": True,
            "richMedia": [{"rmxData": [{"floorPlan": {"heroImageUrl":
                "https://www.zillowstatic.com/floor_map/b3ae4c27-8976-428b-8b44-accd62cf4a59/hero.png"},
                "virtualTour": {"viewerUrl": "https://www.zillow.com/view-3d-home/0c7b3bcf"}}]}]}}}
    (p,) = ADAPTERS["zillow"].parse(item, {})
    assert p.data.extra["floorplan_image"] == \
        "https://www.zillowstatic.com/floor_map/b3ae4c27-8976-428b-8b44-accd62cf4a59/hero.png"


def test_zillow_amenities_skip_card_badges():
    # the search card's flex fields as stored in the raw files: badges, a time stamp, one real feature
    item = {"rawData": {"property": {"zpid": 8, "address": {"streetAddress": "1 A St", "city": "Mountain View",
            "state": "CA", "zipcode": "94041"}, "bedrooms": 1, "bathrooms": 1, "price": 3000,
            "listCardRecommendation": {"flexFieldRecommendations": [
                {"contentType": "frZillowApplication", "displayString": "Apply instantly"},
                {"contentType": "timeOnInfo", "displayString": "227 days ago"},
                {"contentType": "frWaitlisted", "displayString": "Waitlisted"},
                {"contentType": "homeInsight", "displayString": "Swimming pool"}],
                "zovInsight": {"displayString": "Clubhouse"}}}}}
    assert ADAPTERS["zillow"].parse(item, {})[0].data.amenities == ["Swimming pool", "Clubhouse"]
    item["rawData"]["property"]["listCardRecommendation"]["flexFieldRecommendations"] = [
        {"contentType": "frZillowApplication", "displayString": "Apply instantly"}]
    del item["rawData"]["property"]["listCardRecommendation"]["zovInsight"]
    assert ADAPTERS["zillow"].parse(item, {})[0].data.amenities is None  # badges only: unknown, not a list


def test_zillow_required_fees_are_text_to_read():
    from aptfinder.intake.extract import entry_text

    item = {"rawData": {"property": {"zpid": 9, "address": {"streetAddress": "1 A St", "city": "Mountain View",
            "state": "CA", "zipcode": "94041"}, "bedrooms": 1, "bathrooms": 1, "price": 3000,
            "rental": {"totalRequiredMonthlyMinFee": 40, "totalRequiredMonthlyMaxFee": 40}}}}
    (p,) = ADAPTERS["zillow"].parse(item, {})
    assert p.data.extra["required_monthly_fees"] == "$40"
    assert entry_text(p.data.model_dump()) == "Required monthly fees: $40"
