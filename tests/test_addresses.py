from aptfinder.identity.addresses import check_parts, place_key_from
from aptfinder.store.keys import place_key


def test_parts_must_come_from_the_address():
    a = "2270 W. El Camino Real Mountain View, CA 94040"
    p = check_parts({"number": "2270", "predirectional": "W.", "street_name": "El Camino Real",
                     "street_type": None, "city": "Mountain View", "zip": "94040"}, a)
    assert p == {"street": "2270 w el camino real", "city": "mountain view", "unit": None}
    assert place_key_from(p, None) == place_key("2270 W El Camino Real, Mountain View, CA 94040", None, None, None)
    assert check_parts({"number": "2272", "street_name": "El Camino Real"}, a) is None  # number not in the text
    assert check_parts({"number": "2270", "street_name": "Camino Real Ave"}, a) is None  # 'ave' not in the text


def test_a_city_not_written_in_the_address_is_dropped():
    p = check_parts({"number": "850", "street_name": "Calderon", "street_type": "Ave", "city": "Mountain View"},
                    "850 Calderon Ave")
    assert p["city"] is None and place_key_from(p, "mountain view") == place_key_from(
        {"street": "850 calderon ave", "city": "mountain view"}, None)


def test_a_one_number_range_from_haiku_gets_the_parsers_key():
    p = check_parts({"number": "881-881", "predirectional": "E", "street_name": "El Camino Real", "city": "Mountain View"},
                    "881-881 E El Camino Real, Mountain View, CA 94040")
    assert place_key_from(p, None) == place_key("881 E El Camino Real, Mountain View, CA 94040", None, None, None)


def test_google_reading_is_used_only_for_a_precise_match_with_the_same_number():
    from aptfinder.identity.addresses import geocode_parts

    res = {"types": ["street_address"], "place_id": "X", "formatted_address": "101 Infinity Way, Mountain View, CA 94043",
           "address_components": [{"long_name": "101", "types": ["street_number"]},
                                  {"long_name": "Infinity Way", "types": ["route"]},
                                  {"long_name": "Mountain View", "types": ["locality", "political"]},
                                  {"long_name": "94043", "types": ["postal_code"]}]}
    p = geocode_parts(res, "101 Infinity Wy, Mountain View, CA 94043")
    assert place_key_from(p, None) == place_key("101 Infinity Way, Mountain View, CA 94043", None, None, None)
    assert geocode_parts(res, "103 Infinity Wy, Mountain View") is None  # Google matched a different number
    assert geocode_parts({**res, "types": ["route"]}, "101 Infinity Wy") is None  # street-level only
