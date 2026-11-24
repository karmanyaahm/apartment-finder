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
    assert by_addr == {"1 A St, Mountain View": PASS, "2 B St, Mountain View": FAIL,
                       "3 C St, Mountain View": CHECK}
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
    """A plan the own site no longer offers (absent from a later complete run) is not compared."""
    r1 = start_run(conn, "intake", "direct")
    obs(conn, r1, "direct", "s:old", 3000, address="10 Retired Way, Mountain View", beds=1, sqft=763,
        unit="101")
    finish_run(conn, r1, "ok")
    r2 = start_run(conn, "intake", "apartments_com")
    obs(conn, r2, "apartments_com", "a1", 3000, address="10 Retired Way, Mountain View", beds=1, sqft=738,
        unit="101")
    finish_run(conn, r2, "ok")
    identity.run(conn)
    assert [c["fields"] for c in conflict_queue(conn)] == [["sqft"]]
    r3 = start_run(conn, "intake", "direct")  # a later full run of the site without that plan
    obs(conn, r3, "direct", "s:new", 3100, address="10 Retired Way, Mountain View", beds=2, unit="202")
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
    # once the site is rendered, the conflict is actionable again
    obs(conn, r, "direct", "alpha:r:x", 4800, address="1 Alpha St, Mountain View", beds=3,
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
