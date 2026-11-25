from aptfinder.identity.addresses import geocode_parts, place_key_from
from aptfinder.store.keys import place_key


def test_google_reading_is_used_only_for_a_precise_match_with_the_same_number():
    res = {"types": ["street_address"], "place_id": "X", "formatted_address": "101 Infinity Way, Mountain View, CA 94043",
           "address_components": [{"long_name": "101", "types": ["street_number"]},
                                  {"long_name": "Infinity Way", "types": ["route"]},
                                  {"long_name": "Mountain View", "types": ["locality", "political"]},
                                  {"long_name": "94043", "types": ["postal_code"]}]}
    p = geocode_parts(res, "101 Infinity Wy, Mountain View, CA 94043")
    assert place_key_from(p, None) == place_key("101 Infinity Way, Mountain View, CA 94043", None, None, None)
    assert geocode_parts(res, "103 Infinity Wy, Mountain View") is None  # Google matched a different number
    assert geocode_parts({**res, "types": ["route"]}, "101 Infinity Wy") is None  # street-level only


def test_a_one_number_range_gets_the_parsers_key():
    assert place_key_from({"street": "881-881 e el camino real", "city": "mountain view"}, None) == place_key(
        "881 E El Camino Real, Mountain View, CA 94040", None, None, None)


def test_a_number_list_address_takes_googles_pick_among_its_numbers():
    # RentCafe 'Middle Park Apartments': Google answered 1028, the building's main number; the check wanted 1024
    res = {"types": ["subpremise"], "place_id": "Y", "formatted_address": "1028 Middle Ave 1024 1026, Menlo Park, CA",
           "address_components": [{"long_name": "1028", "types": ["street_number"]},
                                  {"long_name": "Middle Avenue", "types": ["route"]},
                                  {"long_name": "Menlo Park", "types": ["locality", "political"]}]}
    p = geocode_parts(res, "1024, 1026, 1028 Middle Avenue, Menlo Park, CA 94025")
    assert place_key_from(p, None) == place_key("1028 Middle Ave, Menlo Park, CA 94025", None, None, None)
    assert geocode_parts(res, "1030, 1032 Middle Avenue, Menlo Park") is None  # not one of the listed numbers
