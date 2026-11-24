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
