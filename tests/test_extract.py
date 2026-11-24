from aptfinder.intake.extract import check_answer

TEXT = ("Welcome home to Palmetto!\nPalmetto Apartments\n2235 California St\nMountain View, CA 94040\n"
        "https://livecreativejuices.com/ea37wz3g/palmetto-apartments\nPets welcome. Utilities included!")


def test_address_and_url_must_appear_verbatim():
    got = check_answer({"address": "2235 California St, Mountain View, CA 94040", "property_name": "Palmetto Apartments",
                        "website": "https://livecreativejuices.com/ea37wz3g/palmetto-apartments",
                        "utilities_included": True, "furnished": None}, TEXT)
    assert got["address"].startswith("2235 California St")
    assert got["property_name"] == "Palmetto Apartments"
    assert got["website"] == "https://livecreativejuices.com/ea37wz3g/palmetto-apartments/"
    assert got["utilities_included"] is True and "furnished" not in got


def test_invented_values_are_dropped():
    got = check_answer({"address": "2237 California St, Mountain View", "property_name": "Palmetto Gardens",
                        "website": "https://palmetto-apartments.com/", "min_stay_days": 99999}, TEXT)
    assert got == {}


def test_address_needs_a_street_number_and_aggregators_are_refused():
    assert "address" not in check_answer({"address": "California St, Mountain View"}, TEXT + "\nCalifornia St")
    text = TEXT + "\nsee https://www.zillow.com/b/palmetto"
    assert "website" not in check_answer({"website": "https://www.zillow.com/b/palmetto"}, text)
