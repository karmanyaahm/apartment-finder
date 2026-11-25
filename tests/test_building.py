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


def test_a_house_type_against_apartment_evidence_is_unknown():
    b = place_building_units([row("zillow", kind="townhome", group_type="apartmentComplex")])
    assert (b["value"], b["house"], b["type"]) == (None, False, "townhome or apartment building")


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


def test_zillow_complex_home_count_is_a_floor():
    # an apartmentComplex card groups 22 matching homes: at least 22 units, more than its 3 bedroom rows say
    rows = [row("zillow", floor_plan=f"{n}bd", group_type="apartmentComplex", complex_home_count=22) for n in range(3)]
    b = place_building_units(rows)
    assert (b["value"], b["floor"], b["source"], b["evidence"]) == (22, True, "zillow",
                                                                     "Zillow: 22 homes listed in the complex")
    # the most of a place's cards, never their sum; more plans listed elsewhere win
    rows = [row("zillow", group_type="apartmentComplex", complex_home_count=n) for n in (2, 3)]
    assert place_building_units(rows)["value"] == 3
    rows += [row("apartments_com", floor_plan=f"p{i}") for i in range(4)]
    assert place_building_units(rows)["source"] == "apartments_com"
    # a townhome on a card of 6 homes: at least 6 units, not a house
    b = place_building_units([row("zillow", kind="townhome", group_type="apartmentComplex", complex_home_count=6)])
    assert (b["value"], b["floor"], b["house"]) == (6, True, False)


def test_is_apartment_building():
    def ab(*rows):
        return place_building_units(list(rows))["apartment_building"]

    assert ab(row("rentcafe", kind="site_info", units_total=167)) is True  # 5+ units
    assert ab(*[row("apartments_com", kind="apartment", unit=str(i), property_type="Condo") for i in range(5)]) is True
    assert ab(row("zillow", group_type="apartmentComplex")) is True  # building-level type, no house evidence
    assert ab(row("google_places", kind="site_info", primary_type="apartment_building")) is True
    assert ab(row("zumper", property_type=4)) is True
    assert ab(row("direct", kind="site_info")) is True  # the building's own site
    assert ab(row("zillow", kind="house")) is False
    assert ab(row("zillow", kind="townhome")) is False
    assert ab(row("craigslist", housing_type="duplex")) is False
    # unknown: a house type against apartment evidence; a floor of 2 to 4 on a townhome card; unit-level types;
    # a stated small count
    assert ab(row("zillow", kind="townhome", group_type="apartmentComplex")) is None
    assert ab(row("zillow", kind="townhome", group_type="apartmentComplex", complex_home_count=3)) is None
    assert ab(row("craigslist", housing_type="apartment")) is None
    assert ab(row("craigslist", description="only two units in the building")) is None
    assert "apartmentComplex" in place_building_units([row("zillow", group_type="apartmentComplex")])["apartment_evidence"]


def test_is_apartment_building_rule():
    rule = {"type": "equals", "field": "is_apartment_building", "value": True}
    assert evaluate(rule, {"is_apartment_building": True}, Ctx())[0] == PASS
    assert evaluate(rule, {"is_apartment_building": 1}, Ctx())[0] == PASS  # SQLite's JSON true
    st, why = evaluate(rule, {"is_apartment_building": False, "is_apartment_building_evidence": "kind house (zillow)"},
                       Ctx())
    assert st == FAIL and "kind house (zillow)" in why
    assert evaluate(rule, {"is_apartment_building": None}, Ctx())[0] == CHECK
