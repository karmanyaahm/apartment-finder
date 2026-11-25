"""Parking: extract's checked `parking` object, the rule fields and their pooling per place."""

import json

from aptfinder.filter import goal_inputs, parking_fields, place_parking, row_parking
from aptfinder.filter.rules import FAIL, PASS, CHECK, Ctx, evaluate, validate_rule
from aptfinder.intake.extract import check_answer, check_parking, parking_amenity

TEXT = ("Spacious 2 bed. One assigned covered parking space included. Additional garage parking $150/month. "
        "Street parking available.")


def test_values_need_quotes_and_numbers_their_quote():
    got = check_parking({"offered": "yes", "kinds": ["covered", "assigned", "garage", "valet"], "spots": 1,
                         "cost": "included", "usd_month": 150,
                         "evidence": ["One assigned covered parking space included", "made up quote"]}, TEXT)
    # 'valet' is no kind; the invented quote is dropped; an included space has no monthly price
    assert got == {"offered": "yes", "kinds": ["assigned", "covered", "garage"], "spots": 1, "cost": "included",
                   "usd_month": None, "evidence": ["One assigned covered parking space included"]}
    assert check_parking({"offered": "yes", "cost": "included", "evidence": ["free parking"]}, TEXT) is None
    # a spot count or price not in any quote is unknown, the rest stays
    got = check_parking({"offered": "yes", "spots": 2, "cost": "paid", "usd_month": 95,
                         "evidence": ["Additional garage parking $150/month"]}, TEXT)
    assert got["spots"] is None and got["usd_month"] is None and got["cost"] == "paid"


def test_garage_for_rent_is_paid_and_street_parking_is_no_space():
    got = check_parking({"offered": "yes", "kinds": ["garage"], "usd_month": 1200,
                         "evidence": ["garage for rent $1,200/mo"]}, "Also a garage for rent $1,200/mo.")
    assert got["cost"] == "paid" and got["usd_month"] == 1200
    got = check_parking({"offered": "yes", "kinds": ["street"], "cost": "included",
                         "evidence": ["Street parking available"]}, TEXT)
    assert got["offered"] == "no" and got["cost"] is None and got["spots"] == 0
    assert parking_amenity(got) == []
    # Airbnb: a space on the premises beside street parking is no street-only home, whatever kinds Haiku tagged
    text = "Free parking on premises. Free street parking."
    got = check_parking({"offered": "yes", "kinds": ["street"], "cost": "included",
                         "evidence": ["Free parking on premises", "Free street parking"]}, text)
    assert got["offered"] == "yes" and got["cost"] == "included"
    assert parking_amenity({"offered": "yes", "kinds": ["garage", "street"]}) == ["Parking: garage (read from text)"]


def test_check_answer_keeps_parking_beside_restriction():
    got = check_answer({"parking": {"offered": "no", "kinds": [], "evidence": ["No parking"]}}, "No parking. Quiet.")
    assert got["parking"]["offered"] == "no"


def test_rule_fields_and_the_editor_shapes():
    inc = {"offered": "yes", "spots": 1, "cost": "included", "evidence": ["1 space included"], "source": "zillow"}
    paid = {"offered": "yes", "spots": None, "cost": "paid", "usd_month": 75, "evidence": ["$75"], "source": "x"}
    assert parking_fields([inc])["parking_included"] is True
    f = parking_fields([paid, inc])  # the own reading first: paid; spots come from the next reading
    assert (f["parking_included"], f["parking_usd_month"], f["parking_spots"]) == (False, 75, 1)
    assert f["parking_usd_month_evidence"] == '"$75" (x)'
    unk = parking_fields([{"offered": "yes", "cost": None, "evidence": ["parking"], "source": "x"}])
    assert unk["parking_included"] is None and unk["parking_spots"] is None
    for r in ({"type": "equals", "field": "parking_included", "value": True},
              {"type": "min", "field": "parking_spots", "value": 1},
              {"type": "max", "field": "parking_usd_month", "value": 100}):
        assert validate_rule(r, modes=[], points=set(), sets=set()) is None
    rule = {"type": "equals", "field": "parking_included", "value": True}
    assert evaluate(rule, {"parking_included": None}, Ctx())[0] == CHECK  # unknown never rejects
    s, why = evaluate(rule, {**f}, Ctx())
    assert s == FAIL and '"$75"' in why
    assert evaluate({"type": "max", "field": "parking_usd_month", "value": 100}, f, Ctx())[0] == PASS


def test_building_statements_pool_a_unit_post_does_not(conn):
    from aptfinder.store import append_observation, finish_run, start_run
    from aptfinder import identity
    run = start_run(conn, "intake", "direct", {"scope": "full"})
    base = {"address": "100 Main St, Mountain View, CA", "lat": 37.39, "lon": -122.08, "kind": "apartment",
            "term": "long", "beds": 1}
    page = {"offered": "yes", "kinds": ["garage"], "spots": 1, "cost": "included", "usd_month": None,
            "evidence": ["one garage space included"], "url": "https://site"}
    append_observation(conn, run, "direct", "s:r:a1", {**base, "extra": {"page_parking": page}}, url="https://s",
                       price=3000, price_basis="month")
    finish_run(conn, run, "ok", {})
    run = start_run(conn, "intake", "craigslist", {"scope": "full"})
    post = {"offered": "no", "kinds": ["street"], "spots": 0, "cost": None, "usd_month": None,
            "evidence": ["street parking only"]}
    append_observation(conn, run, "craigslist", "c1", {**base, "extra": {"extracted": {"v": 4, "parking": post}}},
                       url="https://c", price=2900, price_basis="month")
    append_observation(conn, run, "craigslist", "c2", {**base}, url="https://c2", price=2800, price_basis="month")
    finish_run(conn, run, "ok", {})
    identity.run(conn)
    rows = {r["source_id"]: r for r in goal_inputs(conn)["listings"]}
    assert rows["s:r:a1"]["parking_included"] is True
    assert rows["c1"]["parking_included"] is False  # its own post says street only
    assert rows["c2"]["parking_included"] is True and "direct page" in rows["c2"]["parking_included_evidence"]
    own, pooled = row_parking(None, page, "direct", "s:r:a1")
    assert own and pooled
    own, pooled = row_parking(post, None, "craigslist", "c1")
    assert own and not pooled
    parsed = [{**dict(r), "data": json.loads(r["data"])} for r in conn.execute("SELECT * FROM listings")]
    assert place_parking(parsed)["page"] is True  # both decided; an own site's page wins the tie


def test_parking_cost_reads_one_reading_whole_and_passes_only_at_zero():
    from aptfinder.filter.rules import FAIL, PASS, CHECK, Ctx, evaluate, parking_cost, parking_cost_text, validate_rule

    def rd(**k):
        return {"evidence": ["q"], "source": "x", "page": False, **k}

    inc2 = rd(offered="yes", cost="included", spots=2)
    paid = rd(offered="yes", cost="paid", usd_month=75)
    assert parking_cost([inc2], 2)["usd"] == 0
    assert parking_cost([paid], 2)["usd"] == 150 and parking_cost_text(parking_cost([paid], 2)) == "+$150/mo for 2 spots"
    assert parking_cost([rd(offered="yes", cost="included", spots=1), paid], 2)["usd"] is None  # 1 included: not mixed
    assert parking_cost([rd(offered="yes", cost=None), paid], 2)["usd"] == 150  # a cost-less reading is skipped
    assert parking_cost([rd(offered="no", spots=0), paid], 2)["none"]  # street only: not paired with another's $
    assert parking_cost([rd(offered="yes", cost="paid")], 2)["why"] == "paid, price not stated"
    rule = {"type": "parking_cost", "spots": 2}
    assert validate_rule(rule, modes=[], points=set(), sets=set()) is None
    assert validate_rule({**rule, "spots": 0}, modes=[], points=set(), sets=set())
    st = lambda rs: evaluate(rule, {"parking_readings": rs}, Ctx())[0]  # noqa: E731
    assert (st([inc2]), st([paid]), st([]), st([rd(offered="no")])) == (PASS, FAIL, CHECK, FAIL)
    assert evaluate(rule, {"parking_readings": [paid]}, Ctx())[1].startswith("parking for 2 spots: +$150/mo for 2 spots ($75/spot")
