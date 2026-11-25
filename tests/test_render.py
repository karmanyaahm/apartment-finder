from aptfinder.intake.render import check_plans

PAGE = ("Park Place | 851 Church Street, Mountain View, CA 94041 | Floor Plans\nNorth 2CR\n2 Beds - 2 Baths\n"
        "965 sq. ft.\n** Utilities Included\nStarting at $6,145\nAvailable Now\nSouth 1AR\n1 Bed - 1 Bath\n745 sq. ft.\n"
        "Starting at $4,500\nAvailable Dec 6\nThe Dean | Starting at $4,073 | Beds : Studio - 3")


def test_plans_are_kept_only_with_what_the_page_states():
    got = check_plans([
        {"name": "North 2CR", "beds": 2, "baths": 2, "sqft": 965, "price": 6145, "available": "Available Now"},
        {"name": "South 1AR", "beds": 1, "baths": 1, "sqft": 745, "price": 4600, "available": "Available Dec 6"},
        {"name": "North 3A", "beds": 3, "baths": 2, "sqft": 1100, "price": 7000},  # not on the page
    ], PAGE)
    assert [p["name"] for p in got] == ["North 2CR", "South 1AR"]
    assert got[0] == {"name": "North 2CR", "beds": 2.0, "baths": 2.0, "sqft": 965.0, "price": 6145.0,
                      "available": "Available Now"}
    assert "price" not in got[1] and got[1]["sqft"] == 745.0  # $4,600 is not on the page: dropped



from aptfinder.intake.render import MAX_UNIT_PAGES, PAGE_FUNCTION, site_text  # noqa: E402


def test_site_text_puts_unit_pages_in_with_their_own_budget():
    pages = [{"kind": "units", "url": f"https://a.example/plan/{i}", "text": f"Unit {i}0{i} $4,{i}00"} for i in range(7)]
    pages += [{"kind": "start", "url": "https://a.example/", "text": "S" * 50000},
              {"kind": "floorplans", "url": "https://a.example/floorplans", "text": "Plan B2"}]
    t = site_text(pages)
    assert t.index("[page https://a.example/]") < t.index("floorplans") < t.index("/plan/0")
    assert t.count("[page ") == 2 + MAX_UNIT_PAGES and "Unit 404 $4,400" in t and "/plan/6" not in t
    assert "S" * 12001 not in t  # the start page is cut to its budget


def test_page_function_follows_plan_unit_links_within_scope_and_never_actions():
    assert "const MAX_UNITS = 5;" in PAGE_FUNCTION and "__MAX_UNIT_PAGES__" not in PAGE_FUNCTION
    assert "kind: 'units', scope" in PAGE_FUNCTION and "NEVER.test(h.slice(scope.length))" in PAGE_FUNCTION
    import re
    units = re.compile(r"^(view|see|show)\s+(available\s+)?(apartments|units|homes)\b|^available\s+(units|apartments)\b"
                       r"|^view\s+availability$", re.I)
    assert units.pattern.replace("\\\\", "\\") in PAGE_FUNCTION.replace("\\\\", "\\")
    for t in ("VIEW APARTMENTS", "View units", "Available units", "View available units"):
        assert units.search(t), t
    for t in ("Apply now", "Schedule a tour", "View all floor plans"):
        assert not units.search(t), t
