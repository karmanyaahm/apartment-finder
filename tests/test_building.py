"""building_units: how many units a place's building has (filter.building), and the min / max rules over it."""

from aptfinder.filter.building import place_building_units, text_count
from aptfinder.filter.rules import CHECK, FAIL, PASS, Ctx, evaluate


def row(source, kind="apartment", unit=None, floor_plan=None, title=None, description=None, **extra):
    return {"source": source, "kind": kind, "unit": unit, "floor_plan": floor_plan,
            "data": {"title": title, "description": description, "extra": extra}}


def test_text_counts_need_unit_next_to_a_building_noun():
    assert text_count("This is a small 44-unit community where we")[0] == 44
    assert text_count("Small privately owned apartment building with 12 units.")[0] == 12
    assert text_count("Only two units in the building, one upstairs")[0] == 2
    assert text_count("Small, cozy, and friendly six-unit building.")[0] == 6
    # names, plan prices and 'homes' are not counts (the first, noisy pattern matched all of these)
    for t in ("Apply @ 1101 Apartments!", "1/1 units: $1,999 2/1.5 units: $2,350", "We present two homes in",
              "will add an additional 79 apartments", "2 units available now"):
        assert text_count(t) is None, t


def test_rentcafe_total_beats_everything():
    b = place_building_units([row("zillow", kind="house"), row("rentcafe", kind="site_info", units_total=167)])
    assert (b["value"], b["source"], b["floor"], b["house"]) == (167, "rentcafe", False, False)
    # 0 is RentCafe's 'not given'
    assert place_building_units([row("rentcafe", kind="site_info", units_total=0)])["value"] is None


def test_house_types_give_one_duplex_two():
    b = place_building_units([row("zillow", kind="house")])
    assert (b["value"], b["type"], b["house"]) == (1, "single-family", True)
    assert place_building_units([row("craigslist", housing_type="duplex")])["value"] == 2
    assert place_building_units([row("apartments_com", kind="townhome", property_type="Townhouse")])["type"] == "townhome"


def test_floor_is_the_most_plans_one_source_lists_and_beats_a_house_type():
    rows = [row("apartments_com", floor_plan=f"plan {i}") for i in range(3)] + [row("zillow", kind="house")]
    b = place_building_units(rows)
    assert (b["value"], b["floor"], b["source"]) == (3, True, "apartments_com")
    # the same plan on two sources is not two units; rooms and site_info rows never count
    rows = [row("apartments_com", floor_plan="A"), row("zumper", floor_plan="A"),
            row("airbnb", kind="room", unit="1"), row("airbnb", kind="room", unit="2")]
    assert place_building_units(rows)["value"] is None  # an Apartments.com community: no number


def test_an_apartment_type_alone_gives_no_number():
    b = place_building_units([row("google_places", kind="site_info", primary_type="apartment_complex")])
    assert (b["value"], b["type"]) == (None, "apartment building")
    assert place_building_units([row("craigslist")]) is None  # nothing says what the building is


def _rule(t, v):
    return {"type": t, "field": "building_units", "value": v}


def test_a_floor_never_fails_a_min_rule():
    known = {"building_units": 167, "building_units_evidence": "RentCafe: 167 units (rentcafe)"}
    assert evaluate(_rule("min", 5), known, Ctx())[0] == PASS
    assert "rentcafe" in evaluate(_rule("min", 5), known, Ctx())[1]
    assert evaluate(_rule("min", 5), {"building_units": 1}, Ctx())[0] == FAIL
    floor = {"building_units": 3, "building_units_floor": True, "building_units_evidence": "3 plans (zillow)"}
    assert evaluate(_rule("min", 5), floor, Ctx())[0] == CHECK
    assert evaluate(_rule("min", 2), floor, Ctx())[0] == PASS
    assert evaluate(_rule("max", 2), floor, Ctx())[0] == FAIL
    assert evaluate(_rule("max", 4), floor, Ctx())[0] == CHECK
    assert evaluate(_rule("min", 5), {"building_units": None}, Ctx())[0] == CHECK
