import pytest

from aptfinder.intake.extract import check_answer

TEXT = ("Welcome home to Palmetto!\nPalmetto Apartments\n2235 California St\nMountain View, CA 94040\n"
        "https://livecreativejuices.com/ea37wz3g/palmetto-apartments\nPets welcome. Utilities included!")


def test_address_and_url_must_appear_verbatim():
    got = check_answer({"address": "2235 California St, Mountain View, CA 94040", "property_name": "Palmetto Apartments",
                        "website": "https://livecreativejuices.com/ea37wz3g/palmetto-apartments",
                        "facts": {"utilities_included": {"value": ["all"], "evidence": "Utilities included!"},
                                  "furnished": None}}, TEXT)
    assert got["address"].startswith("2235 California St")
    assert got["property_name"] == "Palmetto Apartments"
    assert got["website"] == "https://livecreativejuices.com/ea37wz3g/palmetto-apartments"
    assert got["facts"] == {"utilities_included": {"value": ["all"], "evidence": "Utilities included!"}}


def test_invented_values_are_dropped():
    got = check_answer({"address": "2237 California St, Mountain View", "property_name": "Palmetto Gardens",
                        "website": "https://palmetto-apartments.com/",
                        "facts": {"min_stay_days": {"value": 99999, "evidence": "Pets welcome"}}}, TEXT)
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


# --- whole-entry reading (schema v2) -----------------------------------------------------------------------------

def test_every_fact_needs_a_verbatim_quote_and_a_fee_its_number():
    text = "Resort-style pool and 24-hour Fitness Center. Application fee: $45 per applicant. No dogs, sorry."
    got = check_answer({"facts": {
        "gym": {"value": True, "evidence": "24-hour Fitness Center"},
        "pool": {"value": True, "evidence": "sparkling pool"},           # not in the text
        "application_fee": {"value": 45, "evidence": "Application fee: $45"},
        "admin_fee": {"value": 300, "evidence": "Application fee: $45"},  # number not in its quote
        "dogs_allowed": {"value": False, "evidence": "No dogs"},
        "cats_allowed": {"value": True, "evidence": ""},                  # no quote
        "parking": {"value": "valet", "evidence": "pool"},                # not an allowed kind
    }}, text)["facts"]
    assert set(got) == {"gym", "application_fee", "dogs_allowed"}
    assert got["gym"] == {"value": True, "evidence": "24-hour Fitness Center"}


def test_positive_facts_become_amenity_words_negatives_do_not():
    from aptfinder.intake.extract import amenities_from
    am = amenities_from({"gym": {"value": True}, "dogs_allowed": {"value": False}, "parking": {"value": "garage"},
                         "pool": {"value": False}})
    assert am == ["Gym / fitness center (read from text)", "Parking: garage (read from text)"]


def test_entry_text_uses_allowlisted_raw_fields_and_dedupes_units():
    from aptfinder.intake.extract import entry_text, raw_lines
    item = {"moreDetails": {"desc": "Quiet garden community.", "includedUtilities": ["Water", "Trash"],
                            "onetimeExpenses": [{"amount": "$30", "type": "Application Fee Per Applicant"}],
                            "reviews": [{"text": "no gym here, terrible"}], "schools": [{"name": "X"}]}}
    lines = raw_lines("apartments_com", item)
    assert "About: Quiet garden community." in lines and "Utilities included: Water; Trash" in lines
    assert "One-time fees: $30 Application Fee Per Applicant" in lines
    assert not any("terrible" in x or "School" in x for x in lines)
    a = entry_text({"property_name": "Bali Hai", "amenities": ["Pool"], "unit": "101"}, lines)
    b = entry_text({"property_name": "Bali Hai", "amenities": ["Pool"], "unit": "202"}, lines)
    assert a == b and "101" not in a


def _listings(conn, source, rows):
    """One full intake run holding every (source_id, data): all of them active."""
    from aptfinder.store import append_observation, finish_run, start_run
    run = start_run(conn, "intake", source, {"scope": "full"})
    for sid, data in rows:
        append_observation(conn, run, source, sid, data, url="https://x", price=3000, price_basis="month")
    finish_run(conn, run, "ok", {})


def test_parallel_reading_writes_amenities_facts_and_caches(conn, tmp_path, monkeypatch):
    import json
    import threading
    from aptfinder import settings
    from aptfinder.intake import extract as ex
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    _listings(conn, "craigslist", [(f"cl{i}", {"title": f"Room {i}", "lat": 37.4, "lon": -122.1, "description":
              f"Listing number {i}. Building has a fitness center and a pool. No cats."}) for i in range(25)])
    threads, calls = set(), []

    def stub(batch, model, cap):
        threads.add(threading.get_ident())
        calls.append(len(batch))
        return [{"key": b["key"], "facts": {"gym": {"value": True, "evidence": "a fitness center"},
                                            "cats_allowed": {"value": False, "evidence": "No cats"},
                                            "pool": {"value": True, "evidence": "made up quote"}}}
                for b in batch], 0.001

    res = ex.extract(conn, "craigslist", batch=4, workers=3, ask_fn=stub)
    assert res["listings"] == 25 and res["calls"] == 7 and sum(calls) == 25 and res["observations"] == 25
    assert res["facts"] == {"gym": 25, "cats_allowed": 25}
    row = conn.execute("SELECT data FROM latest_obs WHERE source_id = 'cl3'").fetchone()
    d = json.loads(row["data"])
    assert d["amenities"] == ["Gym / fitness center (read from text)"]
    assert d["extra"]["extracted"]["v"] == 2
    assert d["extra"]["extracted"]["facts"]["cats_allowed"] == {"value": False, "evidence": "No cats"}
    assert len(json.loads((tmp_path / "extract_cache.json").read_text())) == 25
    # read now: nothing pending, and the goal rule sees the gym
    assert ex.extract(conn, "craigslist", ask_fn=stub)["listings"] == 0
    from aptfinder.filter.rules import evaluate, Ctx
    s, _ = evaluate({"type": "amenity", "name": "gym"}, {"amenities": d["amenities"]},
                    Ctx(trips={}, nearest={}, annotations={}, untrusted=set()))
    assert s == "pass"


def test_no_positive_fact_leaves_amenities_unknown(conn, tmp_path, monkeypatch):
    import json
    from aptfinder import settings
    from aptfinder.intake import extract as ex
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    _listings(conn, "furnishedfinder", [("ff1", {"title": "Room", "description": "A quiet room near the train, " * 3})])
    ex.extract(conn, "furnishedfinder", ask_fn=lambda b, m, c: ([{"key": x["key"]} for x in b], 0.0))
    d = json.loads(conn.execute("SELECT data FROM latest_obs WHERE source_id = 'ff1'").fetchone()["data"])
    assert d.get("amenities") is None and d["extra"]["extracted"]["fields"] == []


def test_a_blocker_stops_new_calls_and_keeps_answers_paid_for(conn, tmp_path, monkeypatch):
    import pytest
    from aptfinder import settings
    from aptfinder.intake import extract as ex
    from aptfinder.intake.discover import Blocker
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    _listings(conn, "craigslist", [(f"cl{i:02d}", {"title": "t", "description": f"Text {i} " + "words " * 10})
                                   for i in range(30)])
    n = []

    def stub(batch, model, cap):
        n.append(1)
        if len(n) == 2:
            raise Blocker("claude call failed: rate limited")
        return [{"key": b["key"]} for b in batch], 0.01

    with pytest.raises(Blocker, match="rate limited"):
        ex.extract(conn, "craigslist", batch=5, workers=1, ask_fn=stub)
    assert len(n) == 2  # nothing submitted after the blocker
    run = conn.execute("SELECT * FROM runs WHERE component = 'extract'").fetchone()
    assert run["status"] == "error" and "rate limited" in run["error"]
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE run_id = ?", (run["id"],)).fetchone()[0] == 5


def test_spend_cap_stops_submitting():
    from aptfinder.intake.extract import ask_parallel
    got = []
    res = ask_parallel([[{"key": "0"}]] * 10, lambda g, cap: ([], 0.4), 1, 1.0, lambda g, a: got.append(g))
    assert res["calls"] == 3 and res["left"] == 7


def test_after_intake_hook_reads_text_sources_only(conn, tmp_path, monkeypatch):
    from aptfinder import settings
    from aptfinder.intake import extract as ex
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ex, "after_steps", lambda *a, **k: "after intake: stub")
    _listings(conn, "zumper", [("plan:1", {"title": "The Laurel", "amenities": ["Fitness Center"]})])
    stub = lambda b, m, c: ([{"key": x["key"]} for x in b], 0.0)  # noqa: E731
    assert ex.after_intake(conn, "google_places", 5, ask_fn=stub) is None
    assert ex.after_intake(conn, "zumper", 5, skip=True, ask_fn=stub) is None
    assert ex.after_intake(conn, "zumper", 0, ask_fn=stub) is None
    assert ex.after_intake(conn, "zumper", 1, ask_fn=stub)["observations"] == 1
    # a reparse appends a new scraped row: cached-only reading restores it without asking
    _listings(conn, "zumper", [("plan:1", {"title": "The Laurel", "amenities": ["Fitness Center"]})])

    def boom(*a):
        raise AssertionError("asked")
    assert ex.after_intake(conn, "zumper", 1, cached_only=True, ask_fn=boom)["observations"] == 1


def _stub_steps(monkeypatch, ex):
    calls = []
    monkeypatch.setattr(ex, "_geocode_new", lambda conn: calls.append("geocode") or "geocoded 2 (cached 5)")
    monkeypatch.setattr(ex, "_identity", lambda conn: calls.append("identity") or "identity rebuilt (3 links)")
    monkeypatch.setattr(ex, "_estimates", lambda conn, db: calls.append("estimates") or "estimates for 1 new places")
    return calls


def test_after_intake_runs_free_steps_in_order(conn, capsys, monkeypatch):
    from aptfinder.intake import extract as ex
    calls = _stub_steps(monkeypatch, ex)
    assert ex.after_intake(conn, "google_places", 5) is None  # not a text source: no reading, the steps still run
    assert calls == ["geocode", "identity", "estimates"]
    assert capsys.readouterr().out.strip() == ("after intake: geocoded 2 (cached 5), identity rebuilt (3 links), "
                                               "estimates for 1 new places")
    calls.clear()
    ex.after_intake(conn, "google_places", 5, followup=False)
    ex.after_intake(conn, "google_places", 0)  # nothing written
    assert calls == []
    ex.after_intake(conn, "google_places", 5, geocode=False)  # a reparse: no Google calls
    assert calls == ["identity", "estimates"]


def test_after_intake_steps_run_when_reading_is_blocked(conn, monkeypatch):
    from aptfinder.intake import extract as ex

    def blocked(*a, **k):
        raise ex.Blocker("claude: rate limited")
    calls = _stub_steps(monkeypatch, ex)
    monkeypatch.setattr(ex, "extract", blocked)
    with pytest.raises(ex.Blocker):
        ex.after_intake(conn, "zumper", 1)
    assert calls == ["geocode", "identity", "estimates"]


def test_cli_reparse_never_geocodes(monkeypatch, tmp_path):
    from aptfinder.intake import cli, extract as ex
    calls = _stub_steps(monkeypatch, ex)
    monkeypatch.setattr(cli, "reparse", lambda *a, **k: {"observations": 4})
    cli.main(["--db", str(tmp_path / "x.db"), "reparse", "google_places", "raw.json"])
    assert calls == ["identity", "estimates"]
    calls.clear()
    cli.main(["--db", str(tmp_path / "x.db"), "reparse", "google_places", "raw.json", "--no-followup"])
    assert calls == []


def test_geocode_step_skips_quietly_without_key(conn, monkeypatch):
    from aptfinder import settings
    from aptfinder.identity import addresses
    from aptfinder.intake import extract as ex
    monkeypatch.setattr(addresses, "geocode", lambda conn, dry_run=False: {"to_geocode": 3, "cached": 7})
    monkeypatch.setattr(settings, "secret", lambda name: None)
    assert ex._geocode_new(conn) == "geocoding skipped, no GOOGLE_MAPS_API_KEY (3 new addresses)"


def test_estimates_only_for_places_without_any(conn, tmp_path, monkeypatch):
    from aptfinder import trips
    seen = []
    monkeypatch.setattr(trips, "compute_all", lambda conn, commute, keys: seen.append(keys) or
                        {"places": len(keys), "places_done": len(keys), "new_trips": 0, "error": None})
    monkeypatch.setattr(trips, "build_commute", lambda db, src: None)
    conn.execute("DROP VIEW places")
    conn.execute("CREATE TEMP TABLE places (place_key TEXT, lat REAL, lon REAL)")
    conn.executemany("INSERT INTO places VALUES (?, ?, ?)",
                     [("p_a", 37.4, -122.1), ("p_b", 37.5, -122.2), ("p_b~blueground", 37.5, -122.2),
                      ("p_c", None, None)])
    point = next(p.id for pts in trips.load_points().values() for p in pts)
    trips.append_trip(conn, "p_a", point, "bike", 10.0, 3000, [], "08:30", "estimate")
    assert trips.estimate_missing(conn, str(tmp_path / "x.db"))["places_done"] == 1
    assert seen == [["p_b"]]


def test_cli_run_calls_the_hook(monkeypatch):
    from aptfinder.intake import cli, extract as ex
    from aptfinder.intake.base import IntakeResult
    seen = {}
    monkeypatch.setattr(cli, "run_source", lambda *a, **k: IntakeResult(1, "ok", {"observations": 3}))
    monkeypatch.setattr(ex, "after_intake", lambda conn, src, n, **kw: seen.update(src=src, n=n, **kw))
    cli.main(["--db", ":memory:", "run", "craigslist", "--area", "94043", "--reload", "--workers", "12"])
    assert seen["src"] == "craigslist" and seen["n"] == 3 and seen["workers"] == 12 and seen["skip"] is False
