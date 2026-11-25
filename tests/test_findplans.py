"""Own-site plan finding: decimal prices, the relaxed address check, search picks and the click agent's guard."""

from aptfinder.intake.discover import name_tokens, vouched
from aptfinder.intake.findplans import AGENT_PAGE, check_choice, names_city, pick_result
from aptfinder.intake.render import PAGE_FUNCTION, check_plans

TEXT = "Page text " * 30


def test_price_with_cents_is_on_the_page():
    # Savoy, 2026-11-25: '$3,857.80 /mo*' was dropped because 3857.8 rounds to 3,858
    text = "A7 | View unit | #1-121 | 1 bed | 796 sq. ft. | $3,857.80 /mo* | $3,851 Base Rent"
    got = check_plans([{"name": "A7", "price": 3857.8, "sqft": 796}], text)
    assert got[0]["price"] == 3857.8
    assert "price" not in check_plans([{"name": "A7", "price": 3858.5, "sqft": 796}], text)[0]


def test_render_reads_iframes():
    assert "page.frames()" in PAGE_FUNCTION and "page.frames()" in AGENT_PAGE


def test_name_tokens():
    assert name_tokens("The Heltsley") == ["heltsley"]
    assert name_tokens("Park Plaza Apartments") == []
    assert name_tokens("Flats West at Cityline") == ["flats", "cityline"]


def site(**kw):
    return {"name": "The Heltsley", "address": "1212 Whipple Ave, Redwood City, CA 94062", "url": "x", **kw}


def test_vouched_by_name_in_url_needs_the_city():
    it = {"url": "https://www.theheltsley.com/", "title": "Apartments", "text": TEXT + " Redwood City, CA"}
    assert "names the building" in vouched(site(), it)
    assert vouched(site(), {**it, "text": TEXT + " Santa Rosa"}) is None  # another town's namesake
    assert vouched(site(), {**it, "url": "https://www.sofiredwoodpark.com/"}) is None


def test_vouched_never_for_error_pages_or_aggregators():
    it = {"url": "https://www.theheltsley.com/", "title": "404 Not Found", "text": TEXT + " Redwood City"}
    assert vouched(site(), it) is None
    assert vouched(site(), {**it, "title": "x", "url": "https://www.apartments.com/heltsley"}) is None


def test_vouched_by_you_and_by_places_host():
    it = {"url": "https://other.example/", "title": "Home", "text": TEXT}
    assert vouched(site(found_by="you (Verify)"), it) == "you added this URL"
    assert "Google Places" in vouched(site(found_by="zumper listing"), it, {"other.example"})
    # a site found from Google Places' own website field cannot vouch for itself
    assert vouched(site(found_by="google_places listing"), it, {"other.example"}) is None


def test_pick_result_skips_aggregators_and_other_buildings():
    res = [{"url": "https://www.apartments.com/the-heltsley-redwood-city-ca/abc/", "title": "x"},
           {"url": "https://www.sofiredwoodpark.com/floorplans", "title": "x"},
           {"url": "https://www.theheltsley.com/redwood-city/floor-plans", "title": "x"}]
    url, why = pick_result(res, "The Heltsley", set())
    assert url == "https://www.theheltsley.com/redwood-city/floor-plans" and "result 3" in why
    # a path naming the building on another host is picked, then must name the city once rendered
    assert pick_result([{"url": "https://prometheusapartments.com/ca/los-altos-apartments/harken", "title": ""}],
                       "Harken", set())[0]
    assert not names_city({"title": "Colton plan", "text": "Austin, TX"}, "Redwood City")
    assert pick_result(res[:2], "The Heltsley", set())[0] is None


def test_click_agent_never_applies_or_tours():
    page = {"url": "https://a.example/", "links": [["Apply now", "https://a.example/apply"],
                                                   ["Floor plans", "https://a.example/floorplans"]]}
    assert check_choice({"action": "click", "target": "Schedule a Tour", "reason": ""}, page)["action"] == "stop"
    assert check_choice({"action": "follow", "target": "https://a.example/apply", "reason": ""}, page)["action"] == "stop"
    assert check_choice({"action": "follow", "target": "https://b.example/x", "reason": ""}, page)["action"] == "stop"
    page["links"].append(["Floor plans", "https://a.example/floor-plans-and-virtual-tours"])
    assert check_choice({"action": "follow", "target": "https://a.example/floor-plans-and-virtual-tours",
                         "reason": ""}, page)["action"] == "follow"
    ok = check_choice({"action": "follow", "target": "https://a.example/floorplans", "reason": ""}, page)
    assert ok["action"] == "follow"
    assert check_choice({"action": "click", "target": "SEARCH BY FLOOR PLANS", "reason": ""}, page)["action"] == "click"


def test_pick_on_a_shared_manager_host_needs_the_building_in_the_path():
    res = [{"url": "https://www.caprop.com/", "title": ""},
           {"url": "https://www.caprop.com/the-camille-north-apartments/", "title": ""},
           {"url": "https://www.caprop.com/noel-oaks-apartment-homes/", "title": ""}]
    assert pick_result(res, "Noel Oaks Apartments", {"caprop.com"})[0] == "https://www.caprop.com/noel-oaks-apartment-homes/"
    assert pick_result([{"url": "https://www.thecoltonapts.com/floor-plans", "title": ""}], "The Colton",
                       {"thecoltonapts.com"})[0]


def test_city_words_are_not_the_buildings():
    res = [{"url": "https://www.paloalto.gov/files/plan.pdf", "title": ""}]
    assert pick_result(res, "Palo Alto Plaza", set(), "Palo Alto")[0] is None


def test_units_of_other_buildings_on_a_managers_page_are_dropped():
    from aptfinder.intake.findplans import other_address

    noel = "1010 Noel Dr, Menlo Park, CA 94025"
    assert other_address("1055 Laurel Street", noel)
    assert other_address("3455 Alameda de las Pulgas, #03", noel)
    assert not other_address("1010 Noel Drive #12", noel)
    assert not other_address("A7", noel) and not other_address("2 Bedroom", noel)
    assert not other_address("1055 Laurel Street", "1041 Laurel St #1055, Menlo Park, CA 94025")


def test_click_starts_put_the_known_site_before_a_name_only_pick():
    from aptfinder.intake.findplans import click_starts

    # pilot 1: Claude's pick for Encore was a Salem 'Encore'; The Martin's Google pick a builder's site
    assert click_starts([None, "https://encoremdi.prospectportal.com/salem/encore/"],
                        "https://www.encoreredwoodcity.com/", set())[0] == "https://www.encoreredwoodcity.com/"
    assert click_starts(["https://themartingroup.com/projects", None], "https://livethemartin.com/",
                        {"livethemartin.com"})[0] == "https://livethemartin.com/"
    # a pick on the building's own host comes first (The Colton's floor-plans page)
    fp = "https://www.thecoltonapts.com/apartments/ca/redwood-city/floor-plans"
    assert click_starts([fp, None], "https://www.thecoltonapts.com/", {"thecoltonapts.com"}) == [
        fp, "https://www.thecoltonapts.com/"]
    # no known site: the picks in order (Domus)
    assert click_starts(["https://www.liveatdomus.com/x", "https://www.willowbridgepc.com/y"], None, set())[0] \
        == "https://www.liveatdomus.com/x"


def test_a_city_named_only_in_page_json_does_not_count():
    s = {"name": "Villa Oaks Apartments", "address": "158 N Sunnyvale Ave, Sunnyvale, CA 94086", "url": "x"}
    it = {"url": "https://www.rentvillaoaks.com/", "title": "Santa Rosa Townhomes & Apartments for Rent",
          "text": TEXT + ' Villa Oaks, Santa Rosa {"display_name":"Birchwood","city":"Sunnyvale"}'}
    assert vouched(s, it) is None
