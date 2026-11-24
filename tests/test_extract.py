from aptfinder.intake.extract import check_answer

TEXT = ("Welcome home to Palmetto!\nPalmetto Apartments\n2235 California St\nMountain View, CA 94040\n"
        "https://livecreativejuices.com/ea37wz3g/palmetto-apartments\nPets welcome. Utilities included!")


def test_address_and_url_must_appear_verbatim():
    got = check_answer({"address": "2235 California St, Mountain View, CA 94040", "property_name": "Palmetto Apartments",
                        "website": "https://livecreativejuices.com/ea37wz3g/palmetto-apartments",
                        "utilities_included": True, "furnished": None}, TEXT)
    assert got["address"].startswith("2235 California St")
    assert got["property_name"] == "Palmetto Apartments"
    assert got["website"] == "https://livecreativejuices.com/ea37wz3g/palmetto-apartments"
    assert got["utilities_included"] is True and "furnished" not in got


def test_invented_values_are_dropped():
    got = check_answer({"address": "2237 California St, Mountain View", "property_name": "Palmetto Gardens",
                        "website": "https://palmetto-apartments.com/", "min_stay_days": 99999}, TEXT)
    assert got == {}


def test_address_needs_a_street_number_and_aggregators_are_refused():
    assert "address" not in check_answer({"address": "California St, Mountain View"}, TEXT + "\nCalifornia St")
    text = TEXT + "\nsee https://www.zillow.com/b/palmetto"
    assert "website" not in check_answer({"website": "https://www.zillow.com/b/palmetto"}, text)


def test_redirect_link_keeps_the_query_that_names_the_building():
    text = "Highland Gardens, 234 Escuela Avenue. Apply: www.apt-rental.com?n=highland_gardens&rp=r61t&t=wb"
    got = check_answer({"website": "www.apt-rental.com?n=highland_gardens&rp=r61t&t=wb"}, text)
    assert got["website"] == "https://www.apt-rental.com/?n=highland_gardens&rp=r61t&t=wb"


def test_a_checked_redirect_site_is_scraped_at_its_final_url(tmp_path):
    from aptfinder.config import load_sites
    (tmp_path / "discovered_sites.toml").write_text(
        '[[found]]\nid = "highland"\nplace_key = "p_x"\nurl = "https://www.apt-rental.com/?n=highland_gardens"\n'
        'platform = "photos"\naddress = "234 Escuela Ave, Mountain View, CA"\n'
        '[[check]]\nid = "highland"\nok = true\nfinal_url = "https://highlandgardensapts.com/"\nplatform = "photos"\n')
    s = load_sites(tmp_path)["highland"]
    assert s["url"] == "https://highlandgardensapts.com/" and s["link"].startswith("https://www.apt-rental.com/")


def test_text_that_contradicts_the_source_goes_to_verify(conn):
    from aptfinder import verify
    from aptfinder.intake.extract import _disagrees
    from aptfinder.store import append_observation, start_run

    assert _disagrees("furnished", False, True) and not _disagrees("address", "231 Grant Ave", "231 Grant Ave, Palo Alto")
    run = start_run(conn, "extract", "craigslist", {})
    append_observation(conn, run, "craigslist", "7990155240", {
        "address": "231 Grant Ave, Palo Alto", "furnished": False,
        "extra": {"extracted": {"fields": [], "model": "haiku", "run_id": run, "conflicts": {"furnished": True}}}})
    (item,) = verify.text_conflict_queue(conn)
    assert item["fields"] == ["furnished"] and item["text"] == {"furnished": True}
    assert item["text_label"] == "craigslist (text)" and item["listings"][0]["source_id"] == "7990155240"
