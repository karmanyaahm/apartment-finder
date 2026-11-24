from aptfinder.plans import inherit_prices, plan_key
from aptfinder.web.app import group_plans


def L(source, beds, baths, sqft, price=None, unit=None, term="long", operator=None, plan=None, active=1):
    f = {"beds": beds, "baths": baths, "sqft": sqft, "price_month": price, "unit": unit, "term": term,
         "operator": operator, "floor_plan": plan, "available_from": None}
    return {"source": source, "source_id": f"{source}-{unit or plan or price}", "active": active, "fields": f,
            "data": {"extra": {}}}


def test_plan_key_separates_stays_and_rounds_size():
    assert plan_key({"beds": 3, "baths": 2, "sqft": 1056}) == plan_key({"beds": 3, "baths": 2, "sqft": 1058})
    assert plan_key({"beds": 3, "baths": 2, "sqft": 1056}) != plan_key({"beds": 3, "baths": 2, "sqft": 1056,
                                                                          "term": "short"})


def test_inherit_price_from_same_plan_other_source():
    rows = [{"place_key": "p", "source": "direct", "beds": 3, "baths": 2, "sqft": 1196, "price_month": None},
            {"place_key": "p", "source": "apartments_com", "beds": 3, "baths": 2, "sqft": 1196, "price_month": 8570},
            {"place_key": "q", "source": "direct", "beds": 3, "baths": 2, "sqft": 1196, "price_month": None}]
    inherit_prices(rows)
    assert rows[0]["price_month"] == 8570 and rows[0]["price_from"] == "apartments_com"
    assert abs(rows[0]["price_per_bed"] - 8570 / 3) < 0.01
    assert rows[2]["price_month"] is None  # other place: nothing to inherit


def test_group_plans_direct_is_current_and_stays_are_separate():
    ls = [L("direct", 3, 2, 1056, 5320, "174", plan="C1"), L("direct", 3, 2, 1056, 5449, "380", plan="C1"),
          L("apartments_com", 3, 2, 1056, 6300, "SI-1", term="short", operator="Blueground"),
          L("apartments_com", 3, 2, 1056, 5400, "x9"), L("zillow", 3, None, None, 5320, plan="3bd")]
    plans = group_plans(ls)
    lease = next(p for p in plans if p["offer"] == "lease")
    stay = next(p for p in plans if p["offer"] != "lease")
    assert lease["name"] == "C1" and lease["price_month"] == [5320, 5449] and lease["n_units"] == 2
    assert lease["from_direct"] and lease["also_on"] == ["apartments_com", "zillow"]
    assert stay["offer"] == "furnished stay (Blueground)" and stay["price_month"] == [6300, 6300]
