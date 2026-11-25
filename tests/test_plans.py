from aptfinder.plans import inherit_prices, plan_image, plan_key
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
    # every other source's lowest price for the plan: "also listed at $X on <source>"
    assert lease["also_listed"] == [{"source": "apartments_com", "price_month": 5400},
                                    {"source": "zillow", "price_month": 5320}]
    assert not lease["not_on_own_site"]


def test_plan_only_other_sources_list_at_an_own_site_place_is_marked():
    ls = [L("direct", 2, 2, 980, 4100, "12", plan="B1"), L("zillow", 3, 2, 1200, 5000, "77")]
    by_beds = {p["beds"]: p for p in group_plans(ls)}
    assert by_beds[3]["not_on_own_site"] and by_beds[3]["also_listed"] == [{"source": "zillow", "price_month": 5000}]
    # no priced own-site offer at the place: nothing is marked
    assert not any(p["not_on_own_site"] for p in group_plans([L("zillow", 3, 2, 1200, 5000, "77")]))


def _img(l, url):
    l["data"] = {"extra": {"floorplan_image": url}, "floor_plan": l["fields"]["floor_plan"]}
    return l


def test_plan_drawing_prefers_own_site_then_any_source_never_hotlink_blocked():
    apts = _img(L("apartments_com", 1, 1, 694, 3400, plan="A1"), "https://images1.apartments.com/i2/x/115/image.jpg")
    zumper = _img(L("zumper", 1, 1, 694, 3408, plan="A1"), "https://img.zumpercdn.com/908537559/1280x960")
    own = _img(L("direct", 1, 1, 694, 3500, unit="130", plan="A1"),
               "https://resource.avalonbay.com/floorplans/ca049/ca049-a1.png/1024/768")
    skip = ("images1.apartments.com",)
    assert plan_image([apts, zumper, own], {}, skip) == own["data"]["extra"]["floorplan_image"]
    assert plan_image([apts, zumper], {}, skip) == "https://img.zumpercdn.com/908537559/1280x960"
    assert plan_image([apts], {}, skip) is None
    # the own site's drawing named after the plan (Prometheus' map, a page's alt text) beats another source's
    render = L("direct", 2, 2, 910, 4100, plan="North 2A")
    render["data"] = {"extra": {}, "floor_plan": "North 2A"}
    drawings = {"North 2A": "https://assets.prometheusapartments.com/m/4b17/original/2023_PP_22CA_910SQFT_2B2BR_NORTH-2A.svg",
                "North 2B": "https://assets.prometheusapartments.com/m/29d9/original/2023_PP_22CB_930SQFT_2B2BR_NORTH-2B.svg"}
    z2 = _img(L("zumper", 2, 2, 910, 4100, plan="North 2A"), "https://img.zumpercdn.com/1/1280x960")
    assert plan_image([z2, render], drawings, skip) == drawings["North 2A"]


def test_group_plans_reads_drawings_from_the_site_info_row():
    info = L("direct", None, None, None, plan=None)
    info["fields"]["kind"] = "site_info"
    info["data"] = {"extra": {"plan_drawings": {"1x1-770": "https://cdn.example/clara-770.png"}}}
    plan = L("direct", 1, 1, 770, 3300, plan="1x1-770")
    plan["data"] = {"extra": {}, "floor_plan": "1x1-770"}
    (p,) = group_plans([info, plan])
    assert p["image"] == "https://cdn.example/clara-770.png"
