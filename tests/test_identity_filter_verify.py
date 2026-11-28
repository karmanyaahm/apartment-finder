from __future__ import annotations

import pytest

from aptfinder import identity
from aptfinder.config import GoalsConfig, load_goals
from aptfinder.filter import evaluate_all
from aptfinder.filter.rules import CHECK, FAIL, PASS, Ctx, evaluate
from aptfinder.store import append_check, append_link, append_observation, append_trip, finish_run, start_run
from aptfinder.verify import accuracy, agreement, conflict_queue, pending_render, sample_queue, wilson

DOWNTOWN = "commute_targets:Downtown Palo Alto"


def obs(conn, run, source, sid, price=None, basis="month", **data):
    return append_observation(conn, run, source, sid, data, price=price, price_basis=basis)


# ---------------------------------------------------------------- identity
def test_identity_links_and_is_idempotent(conn):
    r = start_run(conn, "intake", "zillow")
    obs(conn, r, "zillow", "1", 5000, address="825 Menlo Ave APT F, Menlo Park, CA 94025", beds=3)
    obs(conn, r, "apartments", "x", 5100, address="825 Menlo Avenue #F", city="Menlo Park", beds=3)
    obs(conn, r, "airbnb", "a", 3000, lat=37.44, lon=-122.16, beds=1)
    obs(conn, r, "craigslist", "c", 2000)  # nothing identifies it
    c1 = identity.run(conn)
    assert c1 == {"listings": 4, "linked": 3, "unchanged": 0, "unidentified": 1}
    c2 = identity.run(conn)
    assert c2["linked"] == 0 and c2["unchanged"] == 3
    units = conn.execute("SELECT unit_key, n_listings FROM units ORDER BY n_listings DESC").fetchall()
    assert units[0]["n_listings"] == 2 and units[0]["unit_key"].endswith("/f")
    assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 3


def test_identity_never_overrules_user(conn):
    r = start_run(conn, "intake", "zillow")
    obs(conn, r, "zillow", "1", 5000, address="1 A St, Palo Alto")
    identity.run(conn)
    append_link(conn, "user", "zillow", "1", "p_manual", "p_manual", "split")
    identity.run(conn)
    assert conn.execute("SELECT place_key FROM listings").fetchone()[0] == "p_manual"


# ---------------------------------------------------------------- rules
def test_size_rule():
    rule = load_goals().goals["lease"].rules[1]
    assert rule["type"] == "size_options"
    cases = {(3, 2): PASS, (3, 2.5): PASS, (3, 3): PASS, (4, 3): FAIL, (3, 1.5): FAIL, (2, 2): FAIL}
    for (beds, baths), want in cases.items():
        assert evaluate(rule, {"beds": beds, "baths": baths}, Ctx())[0] == want, (beds, baths)
    assert evaluate(rule, {"beds": None, "baths": 2}, Ctx()) == (CHECK, "size 3bd/2+ba: beds unknown")
    assert evaluate(rule, {"beds": 3, "baths": None}, Ctx())[0] == CHECK
    assert evaluate(rule, {"beds": 2, "baths": None}, Ctx())[0] == FAIL  # 2bd fails whatever the baths


def test_unknown_never_rejects():
    goals = load_goals().goals
    for goal in goals.values():
        for rule in goal.rules:
            assert evaluate(rule, {}, Ctx())[0] == CHECK, rule


def test_price_per_bed_strict():
    rule = {"type": "max", "field": "price_per_bed", "value": 2500, "strict": True}
    assert evaluate(rule, {"price_per_bed": 2500}, Ctx())[0] == FAIL
    assert evaluate(rule, {"price_per_bed": 2499}, Ctx())[0] == PASS
    assert evaluate(rule, {"price_per_bed": 2000}, Ctx(untrusted={"price"}))[0] == CHECK


def test_dates_cover():
    rule = {"type": "dates_cover", "start": "2026-12-01", "end": "2027-01-01"}
    assert evaluate(rule, {"query_checkin": "2026-12-01", "query_checkout": "2027-01-01"}, Ctx())[0] == PASS
    assert evaluate(rule, {"available_from": "2026-11-20"}, Ctx())[0] == PASS
    assert evaluate(rule, {"available_from": "2026-12-10"}, Ctx())[0] == FAIL
    assert evaluate(rule, {"available_from": "2026-11-20", "available_to": "2026-12-20"}, Ctx())[0] == FAIL


def test_commute_any_with_estimates_and_google():
    rule = next(r for r in load_goals().goals["temp"].rules if r["type"] == "commute_any")

    def ctx(**trips):
        return Ctx(trips={(DOWNTOWN, m): {"minutes": v[0], "provider": v[1]} for m, v in trips.items()})

    assert evaluate(rule, {}, ctx(bike=(7.9, "google")))[0] == PASS
    assert evaluate(rule, {}, ctx(bike=(8.1, "google"), walk=(30, "google"), transit=(26, "google")))[0] == FAIL
    assert evaluate(rule, {}, ctx(bike=(8.1, "google"), walk=(30, "google")))[0] == CHECK  # transit missing
    # estimates: decisive only far from the threshold
    assert evaluate(rule, {}, ctx(bike=(5, "estimate")))[0] == PASS
    assert evaluate(rule, {}, ctx(bike=(9, "estimate"), walk=(30, "estimate"), transit=(40, "google")))[0] == CHECK
    assert evaluate(rule, {}, ctx(bike=(13, "estimate"), walk=(40, "estimate"), transit=(40, "google")))[0] == FAIL


def test_amenity_annotation_overrides():
    rule = {"type": "amenity", "name": "gym"}
    assert evaluate(rule, {"amenities": '["Fitness center", "Pool"]'}, Ctx())[0] == FAIL
    assert evaluate(rule, {"amenities": '["Gym", "Pool"]'}, Ctx())[0] == PASS
    assert evaluate(rule, {}, Ctx(annotations={"gym": {"rating": 4}}))[0] == PASS


# ---------------------------------------------------------------- evaluate_all on a db
def _lease_goals() -> GoalsConfig:
    return load_goals()


def test_evaluate_all_place_takes_best_listing(conn):
    r = start_run(conn, "intake", "zillow")
    obs(conn, r, "zillow", "1", 5000, address="1 A St, Mountain View", unit="1", beds=3, baths=2, term="long")
    obs(conn, r, "zillow", "2", 9000, address="1 A St, Mountain View", unit="2", beds=3, baths=2, term="long")
    obs(conn, r, "zillow", "3", 9000, address="2 B St, Mountain View", beds=2, baths=1, term="long")
    obs(conn, r, "zillow", "4", None, address="3 C St, Mountain View", beds=3, baths=2, term="long")
    identity.run(conn)
    res = evaluate_all(conn, _lease_goals())
    places = res["places"]["lease"]
    by_addr = {conn.execute("SELECT address FROM places WHERE place_key=?", (pk,)).fetchone()[0]: v["status"]
               for pk, v in places.items()}
    # 3 C St: a 3bd/2ba that fits but has no price: no listings, not needs_check (filter.place_outcome)
    assert by_addr == {"1 A St, Mountain View": PASS, "2 B St, Mountain View": FAIL,
                       "3 C St, Mountain View": "no_listings"}
    assert places[next(pk for pk in places if places[pk]["status"] == "no_listings")]["why"] == "no_price"
    # filters report, they do not drop: every listing has a result
    assert len(res["listings"]["lease"]) == 4


def test_low_accuracy_source_becomes_needs_check(conn):
    r = start_run(conn, "intake", "badsource")
    oids = [obs(conn, r, "badsource", str(i), 4000, address=f"{i} Z St, Palo Alto", beds=3, baths=2, term="long")
            for i in range(1, 7)]
    identity.run(conn)
    for i, oid in enumerate(oids[:5]):
        append_check(conn, "user", "sample", "price", "wrong" if i < 3 else "correct", observation_id=oid,
                     stored_value=4000, true_value=9000)
    acc = accuracy(conn)
    assert acc[("badsource", "price")]["n"] == 5
    res = evaluate_all(conn, _lease_goals())["listings"]["lease"]
    # listing 6 is unchecked but its source's price accuracy is 2/5 < 0.8
    assert res[("badsource", "6")]["status"] == CHECK


# ---------------------------------------------------------------- verify
def test_wilson():
    lo, hi = wilson(19, 20)
    assert 0.75 < lo < 0.8 and hi > 0.99
    assert wilson(0, 0) == (0.0, 1.0)


def test_sample_even_across_sources_and_repeatable(conn):
    r = start_run(conn, "intake", "big")
    for i in range(50):
        obs(conn, r, "big", str(i), 1000, address=f"{i} Big St, Palo Alto")
    for i in range(3):
        obs(conn, r, "small", str(i), 1000, address=f"{i} Small St, Palo Alto")
    identity.run(conn)
    q1 = sample_queue(conn, seed=7, include_trips=False)
    q2 = sample_queue(conn, seed=7, include_trips=False)
    assert q1 == q2
    first6 = [i["source"] for i in q1[:6]]
    assert first6.count("small") == 3 and first6.count("big") == 3
    assert sample_queue(conn, seed=8, include_trips=False) != q1


def test_trips_sampled_by_provider_mode(conn):
    for i in range(4):
        append_trip(conn, f"p{i}", DOWNTOWN, "bike", 5 + i, 1000, [], None, "google")
    append_trip(conn, "p0", DOWNTOWN, "walk", 20, 1000, [], None, "google")
    append_trip(conn, "p0", DOWNTOWN, "drive", 9, 1000, [], None, "estimate")
    q = sample_queue(conn, seed=1)
    assert {i["provider"] + i["mode"] for i in q[:2]} == {"googlebike", "googlewalk"}
    assert all(i["provider"] != "estimate" for i in q)  # straight-line guesses are never sampled


def test_check_state_survives_new_runs_until_value_changes(conn):
    def run(price, baths, day):
        r = start_run(conn, "intake", "zillow")
        a = append_observation(conn, r, "zillow", "1", {"address": "49 Showers Dr, Mountain View", "beds": 3,
                               "baths": baths}, price=price, price_basis="month",
                               observed_at=f"2026-11-0{day}T00:00:00+00:00")
        b = append_observation(conn, r, "apartments", "9", {"address": "49 Showers Dr, Mountain View",
                               "beds": 3, "baths": 2.5}, price=5400, price_basis="month",
                               observed_at=f"2026-11-0{day}T00:00:00+00:00")
        identity.run(conn)
        return a, b

    a, b = run(5000, 2, 1)
    for oid, f, v in ((a, "baths", 2), (b, "baths", 2.5), (a, "price", 5000), (b, "price", 5400)):
        append_check(conn, "user", "conflict", f, "correct", observation_id=oid, stored_value=v)
    assert conflict_queue(conn) == []
    assert not [i for i in sample_queue(conn, include_trips=False)]
    # run 2, nothing changed: checks still current, queues stay empty, accuracy not double counted
    run(5000, 2, 2)
    assert conflict_queue(conn) == []
    assert not sample_queue(conn, include_trips=False)
    a2, _ = run(5000, 2, 3)
    append_check(conn, "user", "sample", "price", "correct", observation_id=a2, stored_value=5000)
    assert accuracy(conn)[("zillow", "price")]["n"] == 1
    # run 4, zillow's baths change: that fact is unchecked again, conflict and sample entries return
    run(5000, 3, 4)
    q = conflict_queue(conn)
    assert len(q) == 1 and q[0]["fields"] == ["baths"]
    assert [i["source"] for i in sample_queue(conn, include_trips=False)] == ["zillow"]


def test_conflict_detection_and_resolution(conn):
    r = start_run(conn, "intake", "zillow")
    a = obs(conn, r, "zillow", "1", 5000, address="49 Showers Dr, Mountain View", beds=3, baths=2)
    b = obs(conn, r, "apartments", "9", 5400, address="49 Showers Drive, Mountain View", beds=3, baths=2.5)
    obs(conn, r, "zillow", "2", 5000, address="5 Agree St, Mountain View", beds=3)
    obs(conn, r, "apartments", "3", 5010, address="5 Agree St, Mountain View", beds=3)  # within 1%
    identity.run(conn)
    q = conflict_queue(conn)
    assert len(q) == 1 and set(q[0]["fields"]) == {"price_month", "baths"}
    ag = agreement(conn)
    assert ag["price_month"] == {"agree": 1, "compared": 2}
    for oid, p in ((a, 5000), (b, 5400)):
        append_check(conn, "user", "conflict", "price", "correct" if p == 5000 else "wrong",
                     observation_id=oid, stored_value=p, true_value=5000)
    assert conflict_queue(conn)[0]["fields"] == ["baths"]
    append_check(conn, "user", "conflict", "baths", "correct", observation_id=a, stored_value=2)
    append_check(conn, "user", "conflict", "baths", "cannot_tell", observation_id=b, stored_value=2.5)
    assert conflict_queue(conn) == []
    # the wrong price check overrides apartments' price in the views
    assert conn.execute("SELECT price FROM listings WHERE source='apartments' AND source_id='9'").fetchone()[0] == 5000


def test_retired_listing_never_makes_a_conflict(conn):
    """A plan the own site no longer offers (absent from a later complete run) is not compared. (The own site
    gives no prices here; a priced one would settle every conflict at the place: own website wins.)"""
    r1 = start_run(conn, "intake", "direct")
    obs(conn, r1, "direct", "s:old", None, None, address="10 Retired Way, Mountain View", beds=1, sqft=763,
        unit="101")
    finish_run(conn, r1, "ok")
    r2 = start_run(conn, "intake", "apartments_com")
    obs(conn, r2, "apartments_com", "a1", 3000, address="10 Retired Way, Mountain View", beds=1, sqft=738,
        unit="101")
    finish_run(conn, r2, "ok")
    identity.run(conn)
    assert [c["fields"] for c in conflict_queue(conn)] == [["sqft"]]
    r3 = start_run(conn, "intake", "direct")  # a later full run of the site without that plan
    obs(conn, r3, "direct", "s:new", None, None, address="10 Retired Way, Mountain View", beds=2, unit="202")
    finish_run(conn, r3, "ok")
    identity.run(conn)
    assert conn.execute("SELECT active FROM listings WHERE source_id = 's:old'").fetchone()[0] == 0
    assert conflict_queue(conn) == []
    assert agreement(conn)["sqft"] == {"agree": 0, "compared": 0}


def test_conflicts_waiting_for_an_own_site_render_come_last(conn):
    r = start_run(conn, "intake", "zillow")
    for addr, sid in (("1 Alpha St, Mountain View", "a"), ("2 Beta St, Mountain View", "b")):
        obs(conn, r, "zillow", sid, 5000, address=addr, beds=2, baths=1)
        obs(conn, r, "apartments", sid, 5000, address=addr, beds=2, baths=2)
    # 1 Alpha St has an own site (photos only so far) that has not been rendered
    obs(conn, r, "direct", "alpha:photos", None, None, address="1 Alpha St, Mountain View", kind="site_info",
        extra={"site": "alpha", "platform": "photos"})
    identity.run(conn)
    q = conflict_queue(conn)
    assert [(c["listings"][0]["source_id"], c["pending_render"]) for c in q] == [("b", False), ("a", True)]
    assert "alpha" in q[1]["pending_reason"] and q[0]["pending_reason"] is None
    # once the site is rendered, the conflict is actionable again (a priced plan would settle it: own site wins)
    obs(conn, r, "direct", "alpha:r:x", None, None, address="1 Alpha St, Mountain View", beds=3,
        extra={"site": "alpha", "platform": "render"})
    identity.run(conn)
    assert not any(c["pending_render"] for c in conflict_queue(conn))
    # an Avalon site never needs a render
    obs(conn, r, "direct", "avalon-mountain-view:1", 4000, address="2 Beta St, Mountain View", beds=1,
        extra={"site": "avalon-mountain-view", "platform": "avalon"})
    identity.run(conn)
    assert pending_render(conn) == {}


@pytest.mark.parametrize("goal", ["temp", "lease"])
def test_repo_goals_have_labels(goal):
    assert load_goals().goals[goal].label


def test_coordinate_only_listing_joins_nearby_address_place(conn):
    r = start_run(conn, "intake", "x")
    obs(conn, r, "apartments_com", "a1", 4000, address="2680 Fayette Dr, Mountain View, CA 94040",
        lat=37.40507, lon=-122.11440, beds=1, property_name="Fayette Arms")
    obs(conn, r, "zillow", "h1", 6000, address="12 House Ln, Mountain View, CA 94040", lat=37.4200, lon=-122.1000)
    obs(conn, r, "craigslist", "c1", 3900, lat=37.40530, lon=-122.11420, beds=1)   # ~30 m away
    obs(conn, r, "craigslist", "c3", 5900, lat=37.42010, lon=-122.10010, beds=3)   # 14 m from a house
    obs(conn, r, "airbnb", "b1", 3000, lat=37.40510, lon=-122.11441, beds=1)        # pins are offset
    obs(conn, r, "craigslist", "c2", 3900, lat=37.41000, lon=-122.11440, beds=1)   # ~550 m away
    identity.run(conn)
    pk = {row[0]: row[1] for row in conn.execute("SELECT source_id, place_key FROM listings")}
    assert pk["c1"] == pk["a1"] and pk["c2"] != pk["a1"]
    assert pk["c3"] != pk["h1"] and pk["b1"] != pk["a1"]
    rule = conn.execute("SELECT author FROM current_links WHERE source_id='c1'").fetchone()[0]
    assert rule == "geo_near_address"


def test_third_party_stay_is_its_own_place_with_the_buildings_trips():
    from aptfinder.filter import _Trips
    from aptfinder.identity import decide

    base = {"address": "1600 Villa St, Mountain View, CA 94041", "city": "Mountain View", "unit": "SI FL1-ID1",
            "floor_plan": None, "lat": None, "lon": None, "beds": 3, "price": 6240, "property_name": None}
    lease = decide({**base, "source": "apartments_com", "term": "long", "operator": None})
    stay = decide({**base, "source": "apartments_com", "term": "short", "operator": "Blueground"})
    own = decide({**base, "source": "direct", "term": "long", "operator": "AvalonBay"})
    assert stay[1] == lease[1] + "~blueground" and stay[0] == "addr_stay"
    assert own[1] == lease[1]  # the building's own furnished option stays with the building
    t = _Trips(dict)
    t[lease[1]][("x", "bike")] = {"minutes": 5}
    assert t.get(stay[1]) == {("x", "bike"): {"minutes": 5}} and t.get("p_other") is None
    t[stay[1]][("x", "bike")] = {"minutes": None}  # an old estimate of the stay's own does not hide the building's
    t[stay[1]][("y", "walk")] = {"minutes": 9}  # ...and still fills a (point, mode) the building lacks
    assert t.get(stay[1]) == {("x", "bike"): {"minutes": 5}, ("y", "walk"): {"minutes": 9}}


def test_unitless_ads_in_a_building_are_not_one_unit():
    from aptfinder.identity import decide

    base = {"source": "craigslist", "address": "200 Infinity Way, Mountain View, CA 94043", "unit": None,
            "city": None, "floor_plan": None, "property_name": "Revela", "kind": "apartment", "lat": None,
            "lon": None, "term": "long", "operator": None}
    studio = decide({**base, "source_id": "1", "beds": 0, "price": 3706})
    two_bed = decide({**base, "source_id": "2", "beds": 2, "price": 5242})
    repost = decide({**base, "source_id": "3", "beds": 2, "price": 5242})
    assert studio[1] == two_bed[1]  # same building
    assert studio[2] != two_bed[2] and two_bed[2] == repost[2]  # different units; a repost merges
    house = {**base, "property_name": None, "kind": "house", "address": "49 Showers Dr, Mountain View"}
    assert decide({**house, "source_id": "4", "beds": 3, "price": 5000})[2] == decide(
        {**house, "source_id": "5", "beds": 3, "price": 5400})[2]  # a house stays one unit


# ---------------------------------------------------------------- own website wins
def test_own_site_prices_decide_the_place_and_aggregators_only_show(conn):
    from aptfinder.filter import routing_candidates
    from aptfinder.verify import text_conflict_queue

    r = start_run(conn, "intake", "zillow")
    # own site: 3/2 at $8,400 (fails lease's per-bedroom cap); Zillow: the same plan at $6,000 (would pass)
    obs(conn, r, "direct", "own:r:c1", 8400, address="7 Own St, Mountain View", beds=3, baths=2, sqft=1100,
        term="long", extra={"site": "own", "platform": "render"})
    obs(conn, r, "zillow", "z1", 6000, address="7 Own St, Mountain View", beds=3, baths=2, sqft=1100, term="long")
    obs(conn, r, "apartments", "a1", 6100, address="7 Own St, Mountain View", beds=3, baths=2.5, sqft=1100,
        term="long", extra={"extracted": {"conflicts": {"beds": 2}}})
    # another building without own-site prices: unchanged, the best listing decides
    obs(conn, r, "zillow", "z2", 6000, address="8 Agg St, Mountain View", beds=3, baths=2, term="long")
    obs(conn, r, "apartments", "a2", 6000, address="8 Agg St, Mountain View", beds=3, baths=2.5, term="long")
    finish_run(conn, r, "ok")
    identity.run(conn)
    res = evaluate_all(conn, _lease_goals())
    pk = {a: conn.execute("SELECT place_key FROM places WHERE address LIKE ?", (a + "%",)).fetchone()[0]
          for a in ("7 Own", "8 Agg")}
    own = res["places"]["lease"][pk["7 Own"]]
    assert own["status"] == FAIL and own["source"] == "direct" and own["own_site"]
    # filters report, never drop: the aggregator listing keeps its result but does not decide
    z = res["listings"]["lease"][("zillow", "z1")]
    assert z["status"] == PASS and not z["decides"]
    assert res["places"]["lease"][pk["8 Agg"]]["status"] == PASS
    assert pk["7 Own"] not in routing_candidates(conn, res=res)
    # verify: no conflict at the own-site place, neither own vs aggregator nor among aggregators, nor text ones
    assert [c["place_key"] for c in conflict_queue(conn)] == [pk["8 Agg"]]
    assert text_conflict_queue(conn) == []


def test_own_site_without_prices_or_retired_does_not_win(conn):
    r1 = start_run(conn, "intake", "direct")
    obs(conn, r1, "direct", "s:old", 8400, address="9 Old St, Mountain View", beds=3, baths=2, term="long",
        extra={"area": "s#render"})
    obs(conn, r1, "direct", "s:plan", None, address="9 Old St, Mountain View", beds=3, baths=2, term="long")
    finish_run(conn, r1, "ok", {"areas": {"s#render": {"state": "complete"}}})
    r2 = start_run(conn, "intake", "direct", {"areas": ["s#render"]})
    finish_run(conn, r2, "ok", {"areas": {"s#render": {"state": "empty"}}})  # the plan is gone
    r3 = start_run(conn, "intake", "zillow")
    obs(conn, r3, "zillow", "z", 6000, address="9 Old St, Mountain View", beds=3, baths=2, term="long")
    finish_run(conn, r3, "ok")
    identity.run(conn)
    assert conn.execute("SELECT active FROM listings WHERE source_id = 's:old'").fetchone()[0] == 0
    res = evaluate_all(conn, _lease_goals())
    (place,) = res["places"]["lease"].values()
    assert place["status"] == PASS and place["source"] == "zillow" and not place["own_site"]


def test_cheap_listing_goes_to_verify_until_its_price_is_checked(conn):
    from aptfinder.verify import all_conflicts, cheap_queue

    r = start_run(conn, "intake", "zumper")
    a = obs(conn, r, "zumper", "z1", 1600, address="425 Oak Ave #A, Redwood City", beds=5, baths=3.5, kind="house")
    obs(conn, r, "zumper", "z2", 900, address="1 Studio St, Redwood City", beds=0)  # studio counts as 1 bed
    obs(conn, r, "zumper", "ok", 3000, address="2 Fine St, Redwood City", beds=2)  # $1,500/bed
    obs(conn, r, "zumper", "nb", 500, address="3 Nobeds St, Redwood City")  # beds unknown: never flagged
    obs(conn, r, "airbnb", "s", 2000, address="4 Stay St, Redwood City", beds=3, term="short", furnished=True)
    obs(conn, r, "craigslist", "rm", 1200, address="5 Room St, Redwood City", beds=4, kind="room")
    obs(conn, r, "airbnb", "n", 50, "night", address="6 Night St, Redwood City", beds=3)  # not a monthly lease
    obs(conn, r, "zumper", "fl", 1500, address="7 Long St, Redwood City", beds=2, furnished=True)  # furnished lease
    identity.run(conn)
    q = cheap_queue(conn, 1000)
    assert [c["listings"][0]["source_id"] for c in q] == ["z1", "fl", "z2"]  # lowest $/bed first
    z1 = q[0]
    assert z1["per_bed"] == 320 and z1["kind"] == "conflict" and z1["fields"] == ["price_month", "beds"]
    assert z1["reason"] == "price per bedroom $320 < $1,000: check for per-room or misread price"
    assert any(c.get("cheap") for c in all_conflicts(conn))
    # right (the price is real): off the list
    append_check(conn, "user", "conflict", "price", "correct", observation_id=a, stored_value=1600)
    assert "z1" not in [c["listings"][0]["source_id"] for c in cheap_queue(conn, 1000)]
    # wrong with the true rent: off the list, and the corrected price is what the views show
    z2 = conn.execute("SELECT obs_id FROM listings WHERE source_id = 'z2'").fetchone()[0]
    append_check(conn, "user", "conflict", "price", "wrong", observation_id=z2, stored_value=900, true_value=2900)
    assert [c["listings"][0]["source_id"] for c in cheap_queue(conn, 1000)] == ["fl"]
    assert conn.execute("SELECT price FROM listings WHERE source_id = 'z2'").fetchone()[0] == 2900


def test_conflicts_relevant_to_a_goal(conn):
    """A goal keeps a conflict only when one of its rules reads a disputed field and no rule fails on every reading."""
    from aptfinder.config import Goal, VerifyConfig
    from aptfinder.filter import goal_inputs
    from aptfinder.verify import all_conflicts, goal_relevance

    r = start_run(conn, "intake", "zillow")
    one = {"beds": 1, "baths": 1}
    obs(conn, r, "zillow", "s1", 2400, address="1 Straddle St, Mountain View", **one)  # $/bed 2400 vs 2700: straddles
    obs(conn, r, "apartments", "s2", 2700, address="1 Straddle St, Mountain View", **one)
    obs(conn, r, "zillow", "f1", 3100, address="2 Fail St, Mountain View", **one)  # both over: settled
    obs(conn, r, "apartments", "f2", 3300, address="2 Fail St, Mountain View", **one)
    obs(conn, r, "zillow", "q1", 2000, address="3 Sqft St, Mountain View", sqft=700, **one)  # sqft: no rule reads it
    obs(conn, r, "apartments", "q2", 2000, address="3 Sqft St, Mountain View", sqft=750, **one)
    obs(conn, r, "zillow", "b1", 2400, address="4 Two Bed St, Mountain View", beds=2, baths=1)  # 2bd: size fails both
    obs(conn, r, "apartments", "b2", 2700, address="4 Two Bed St, Mountain View", beds=2, baths=1)
    obs(conn, r, "zillow", "u1", 2400, address="5 Unknown St, Mountain View", beds=1)  # baths unknown: still shown
    obs(conn, r, "apartments", "u2", 2700, address="5 Unknown St, Mountain View", beds=1)
    obs(conn, r, "zumper", "c1", 900, address="6 Cheap St, Mountain View", **one)  # too cheap, passes the price rule
    obs(conn, r, "craigslist", "t1", 2000, address="7 Text St, Mountain View", furnished=False, **one,
        extra={"extracted": {"conflicts": {"furnished": True}}})  # text says furnished, the goal wants furnished
    identity.run(conn)
    goal = Goal(label="1bd", rules=[{"type": "max", "field": "price_per_bed", "value": 2500, "strict": True},
                                    {"type": "size_options", "options": [{"beds": 1, "min_baths": 1}]}])
    furnished = Goal(label="furnished", rules=[{"type": "equals", "field": "furnished", "value": True}])
    cfg = GoalsConfig(goals={"g": goal, "furn": furnished}, verify=VerifyConfig())
    res = evaluate_all(conn, cfg)
    rows = {(x["source"], x["source_id"]): x for x in goal_inputs(conn, cfg)["listings"]}

    def verdicts(gid, g):
        out = {}
        for c in all_conflicts(conn):
            for l in c["listings"]:
                out[l["source_id"]] = goal_relevance(c, g.rules, res["listings"][gid], rows)
        return out

    v = verdicts("g", goal)
    assert v["s1"] == "shown" and v["u1"] == "shown" and v["c1"] == "shown"
    assert v["f1"] == "settled" and v["b1"] == "settled"
    assert v["q1"] == "irrelevant" and v["t1"] == "irrelevant"
    w = verdicts("furn", furnished)
    assert w["t1"] == "shown"  # stored False fails, the text's True would pass
    assert w["s1"] == "irrelevant"


def test_conflict_hidden_when_every_reading_gives_the_same_result(conn):
    """The Gardens of Sunnyvale unit d: 3bd/2ba vs 3bd/2.5ba, both pass '3bd with 2+ baths', so resolving the baths
    changes nothing ('same'). A beds conflict is judged with price per bed recomputed for each candidate."""
    from aptfinder.config import Goal, VerifyConfig
    from aptfinder.filter import goal_inputs
    from aptfinder.verify import all_conflicts, goal_relevance

    r = start_run(conn, "intake", "zillow")
    obs(conn, r, "zillow", "g1", 7000, address="1 Gardens Ct, Sunnyvale", beds=3, baths=2)
    obs(conn, r, "zumper", "g2", 7000, address="1 Gardens Ct, Sunnyvale", beds=3, baths=2.5)
    obs(conn, r, "zillow", "k1", 9000, address="2 Beds Ct, Sunnyvale", beds=3, baths=3)  # $3,000/bed as a 3bd
    obs(conn, r, "zumper", "k2", 9000, address="2 Beds Ct, Sunnyvale", beds=4, baths=3)  # $2,250/bed as a 4bd
    obs(conn, r, "zillow", "m1", 9000, address="3 Both Ct, Sunnyvale", beds=3, baths=2)  # 3bd and 4bd both pass
    obs(conn, r, "zumper", "m2", 12000, address="3 Both Ct, Sunnyvale", beds=4, baths=3)  # beds and price disagree
    identity.run(conn)
    goal = Goal(label="lease", rules=[{"type": "size_options", "options": [{"beds": 3, "min_baths": 2},
                                                                          {"beds": 4, "min_baths": 3}]},
                                      {"type": "max", "field": "price_per_bed", "value": 3100, "strict": True}])
    cfg = GoalsConfig(goals={"g": goal}, verify=VerifyConfig())
    res = evaluate_all(conn, cfg)
    inp = goal_inputs(conn, cfg)
    rows = {(x["source"], x["source_id"]): x for x in inp["listings"]}
    v = {l["source_id"]: goal_relevance(c, goal.rules, res["listings"]["g"], rows, inp["low"])
         for c in all_conflicts(conn) for l in c["listings"]}
    assert v["g1"] == "same"
    assert v["k1"] == "same"  # 9000/3 and 9000/4 both under 3100, 3bd/3ba and 4bd/3ba both fit
    # 4bd at 9000 passes, 3bd at 12000 ($4,000/bed) fails: two fields, every combination tried
    assert v["m1"] == "shown"
    tight = Goal(label="tight", rules=[*goal.rules[:1], {"type": "max", "field": "price_per_bed", "value": 2500}])
    res2 = evaluate_all(conn, GoalsConfig(goals={"t": tight}, verify=VerifyConfig()))
    k = next(c for c in all_conflicts(conn) if any(l["source_id"] == "k1" for l in c["listings"]))
    # $3,000/bed as a 3bd fails, $2,250/bed as a 4bd passes: the beds decide
    assert goal_relevance(k, tight.rules, res2["listings"]["t"], rows, inp["low"]) == "shown"


def test_a_gone_listing_never_holds_a_place_open_over_an_active_one():
    """The Chateau (2026-11-25): an active Craigslist 3bd fails 'beds >= 4'; a Zillow listing removed from the
    market, with every field empty, said needs_check and decided the place. An active listing's result decides;
    a place with only gone listings keeps the best of theirs."""
    from aptfinder.config import Goal
    from aptfinder.filter import evaluate_goal

    goal = Goal(label="g", rules=[{"type": "min", "field": "beds", "value": 4}])

    def row(sid, pk, active, beds):
        return {"source": "x", "source_id": sid, "place_key": pk, "unit_key": None, "active": active, "beds": beds}

    inp = {"listings": [row("cl", "p1", 1, 3), row("zgone", "p1", 0, None),
                        row("old", "p2", 0, None), row("older", "p2", 0, 2)],
           "trips": {}, "anns": {}, "low": set(), "own": set()}
    _, places = evaluate_goal(inp, goal)
    assert (places["p1"]["status"], places["p1"]["source_id"]) == ("fail", "cl")
    # p2: 'old' (no beds, no price) is no plan, so the place's only plan, a 2bd, fails (filter.place_outcome)
    assert (places["p2"]["status"], places["p2"]["source_id"]) == ("fail", "older")


def _nl_goal():
    from aptfinder.config import Goal

    return Goal(label="g", people=3, rules=[
        {"type": "size_options", "options": [{"beds": 3, "min_baths": 2}]},
        {"type": "max", "field": "price_per_person", "value": 2500}])


def _nl_row(sid, pk, beds=None, baths=None, price=None, unavailable=None, source="x", active=1, sqft=None):
    return {"source": source, "source_id": sid, "place_key": pk, "unit_key": None, "active": active, "beds": beds,
            "baths": baths, "price": price, "price_month": price, "unavailable": unavailable, "sqft": sqft}


def _nl_eval(listings, unpriced=(), places=None):
    from aptfinder.filter import evaluate_goal

    inp = {"listings": list(listings), "unpriced": list(unpriced), "trips": {}, "anns": {}, "low": set(),
           "own": set(), "places": set(places or [])}
    return evaluate_goal(inp, _nl_goal())


def test_no_listings_when_nothing_is_known_for_rent():
    """Case 1: a place with no offer at all (only site_info rows, or a building row with no beds and no price) is
    'no listings: no offers known', not needs_check."""
    lres, places = _nl_eval([_nl_row("bldg", "p1")], places={"p1", "p2"})
    for pk in ("p1", "p2"):  # p2 has no row a goal reads at all (site_info only)
        assert places[pk]["status"] == "no_listings" and places[pk]["why"] == "none"
        assert places[pk]["reasons"][0][1] == "no offers known"
    assert lres[("x", "bldg")]["status"] == "needs_check"  # listings keep pass / needs_check / fail


def test_no_listings_when_every_fitting_plan_has_no_price_or_is_unavailable():
    """Case 2: a 3bd/2ba fits but has no price (or the source says it is unavailable): no listings, even when a
    priced 1bd fails beside it; an unpriced own-site plan (goal_inputs' `unpriced`) counts as a fitting plan."""
    _, places = _nl_eval([_nl_row("a", "p1", 3, 2), _nl_row("b", "p1", 1, 1, 3000)])
    assert (places["p1"]["status"], places["p1"]["why"]) == ("no_listings", "no_price")
    assert places["p1"]["reasons"][0][1].startswith("no price for a fitting plan")
    _, places = _nl_eval([_nl_row("a", "p1", 3, 2, 6000, unavailable="0 Available units"),
                          _nl_row("b", "p1", 3, None, unavailable="0 Available units")])
    assert (places["p1"]["status"], places["p1"]["why"]) == ("no_listings", "unavailable")
    assert '"0 Available units"' in places["p1"]["reasons"][0][1]
    # beds unknown fits too (unknown never rejects); a fitting plan the own site lists without a price
    _, places = _nl_eval([_nl_row("b", "p1", 1, 1, 3000)],
                         unpriced=[_nl_row("own", "p1", 3, 2, source="direct", unavailable="Call for details")])
    assert (places["p1"]["status"], places["p1"]["why"]) == ("no_listings", "unavailable")


def test_place_fails_when_no_plan_fits_beds_baths():
    """Case 3: plans exist and none fits beds/baths: fail, whether priced or not, and a building row with nothing
    known beside them does not hold the place open."""
    _, places = _nl_eval([_nl_row("a", "p1", 2, 1, 3000), _nl_row("bldg", "p1")],
                         unpriced=[_nl_row("own", "p1", 1, 1, source="direct")])
    assert places["p1"]["status"] == "fail"
    _, places = _nl_eval([], unpriced=[_nl_row("own", "p1", 3, 1, source="direct")])
    assert places["p1"]["status"] == "fail"  # 3bd/1ba: baths fail


def test_priced_fitting_offer_keeps_pass_and_needs_check():
    """Case 4: one fitting priced offer decides as before: pass, or needs_check on some other unknown."""
    _, places = _nl_eval([_nl_row("a", "p1", 3, 2, 6000), _nl_row("b", "p1", 3, 2)])
    assert places["p1"]["status"] == "pass" and "why" not in places["p1"]
    _, places = _nl_eval([_nl_row("a", "p1", 3, None, 6000), _nl_row("b", "p1", 3, 2)])
    assert places["p1"]["status"] == "needs_check"  # priced, baths unknown: a real check
    # a Zillow building row whose plans disagree on baths (3bd, baths unknown) beside a 3bd/1ba that fails: the
    # unknown one decides, needs_check, never fail or no listings (checked live 2026-11-25: 0 places otherwise)
    _, places = _nl_eval([_nl_row("z:3bd", "p1", 3, None, 6000), _nl_row("a", "p1", 3, 1, 6000)])
    assert places["p1"]["status"] == "needs_check" and places["p1"]["source_id"] == "z:3bd"
    _, places = _nl_eval([_nl_row("a", "p1", 3, 2, 9000)])
    assert places["p1"]["status"] == "fail"  # $3,000/person over $2,500: the plan fits beds/baths but fails price


def test_near_misses_fail_never_check():
    """A known value just over a limit is a fail, never needs_check: the limits already allow the margin (user,
    2026-11-25). Only an estimate or KNN range that cannot tell is needs_check."""
    from aptfinder.filter.rules import Ctx, evaluate

    _, places = _nl_eval([_nl_row("a", "p1", 3, 2, 7836)])  # $2,612/person vs $2,500
    assert places["p1"]["status"] == "fail"
    rule = {"type": "commute_any", "target": "commute_targets:x", "options": [{"mode": "walk", "max": 21}]}
    google = Ctx(trips={("commute_targets:x", "walk"): {"minutes": 21.1, "provider": "google"}})
    assert evaluate(rule, {}, google)[0] == FAIL

    def knn(lo, hi):
        return Ctx(trips={("commute_targets:x", "walk"): {"minutes": lo, "provider": "knn", "min": lo, "max": hi,
                                                          "neighbors": [1, 2, 3]}})
    assert evaluate(rule, {}, knn(30, 31))[0] == FAIL
    # a KNN range is not this place's own time: near the limit it cannot tell (KNN_FAIL_MARGIN), an unknown
    assert evaluate(rule, {}, knn(22, 23))[0] == CHECK


def test_an_active_failing_plan_beats_a_gone_fitting_offer():
    """Active listings decide (372b6eb): a gone 3bd/2ba priced offer does not pass a place whose active plan, a 2bd,
    fails; with nothing active, the gone offer decides."""
    _, places = _nl_eval([_nl_row("a", "p1", 2, 1, 3000), _nl_row("g", "p1", 3, 2, 6000, active=0)])
    assert (places["p1"]["status"], places["p1"]["source_id"]) == ("fail", "a")
    _, places = _nl_eval([_nl_row("g", "p1", 3, 2, 6000, active=0)])
    assert places["p1"]["status"] == "pass"


def test_goal_offer_is_the_cheapest_of_the_best_status_not_the_cheapest_unit():
    """Avalon Mountain View (2026-11-25): the lease card showed $3,799 (a 1bd) / 3 people. Under a goal the card
    prices the cheapest passing offer, else needs_check, else the cheapest overall (filter.goal_offer)."""
    _, places = _nl_eval([_nl_row("1bd", "p1", 1, 1, 3799), _nl_row("3a", "p1", 3, 2, 7400),
                          _nl_row("3b", "p1", 3, 2, 7100, sqft=1100), _nl_row("3c", "p1", 3, None, 6500)])
    o = places["p1"]["offer"]
    assert (o["source_id"], o["price_month"], o["beds"], o["baths"]) == ("3b", 7100, 3, 2)
    assert o["sqft"] == 1100  # the offer's own unit's sqft: the card and the sqft sort read it
    _, places = _nl_eval([_nl_row("1bd", "p1", 1, 1, 3799), _nl_row("3c", "p1", 3, None, 6500)])
    assert places["p1"]["offer"]["source_id"] == "3c"  # no pass: the needs_check one, though dearer
    _, places = _nl_eval([_nl_row("1bd", "p1", 1, 1, 3799), _nl_row("3x", "p1", 3, 2, 9500)])
    assert places["p1"]["offer"]["source_id"] == "1bd"  # all fail: the cheapest overall


def test_dates_cover_fails_on_a_stated_minimum_stay_longer_than_the_goals_stay():
    """Furnished Finder 829696_1 (2026-11-25): minimum_stay_days 180 ('6-Months Min Beginning March') passed temp's
    35-day Jan 11 - Feb 15 stay. A known minimum longer than the stay fails; unknown stays as before."""
    from aptfinder.filter import min_stay
    from aptfinder.filter.rules import FAIL, PASS, Ctx, evaluate

    rule = {"type": "dates_cover", "start": "2027-01-11", "end": "2027-02-15"}
    row = {"available_from": "2026-12-01", "min_stay_days": 180.0, "min_stay_evidence": "minimum_stay_days"}
    s, why = evaluate(rule, row, Ctx())
    assert s == FAIL and "minimum stay 180 days (minimum_stay_days), the stay is 35" in why
    assert evaluate(rule, {**row, "min_stay_days": 30.0}, Ctx())[0] == PASS
    assert evaluate(rule, {**row, "min_stay_days": None}, Ctx())[0] == PASS
    assert min_stay(None, 180, None, None) == (180.0, "minimum_stay_days")
    assert min_stay(None, None, None, 1) == (30.0, "min_lease_months") and min_stay(None, None, None, None) is None
