"""verify.reconcile: Sonnet's answers accepted only with a verbatim quote stating the value, written as machine
checks that close the conflict, never over the user's own check."""

from aptfinder import identity
from aptfinder.store import append_check, append_observation, start_run
from aptfinder.verify import accuracy, conflict_queue, sample_queue
from aptfinder.verify import reconcile as rc


def two_sources(conn):
    r = start_run(conn, "intake", "zillow")
    a = append_observation(conn, r, "zillow", "1", {"address": "49 Showers Dr, Mountain View", "beds": 2, "baths": 2,
                                                   "description": "Sunny 2 bed 2 bath, $3,000/mo, parking."},
                           price=3000, price_basis="month", url="https://z/1")
    b = append_observation(conn, r, "zumper", "9", {"address": "49 Showers Drive, Mountain View", "beds": 2,
                                                   "baths": 2, "description": "Two bedrooms. Rent $3,200 per month."},
                           price=3200, price_basis="month", url="https://zu/9")
    identity.run(conn)
    return a, b


def answer(value, quote, evidence="E1", confidence="high", field="price"):
    return lambda prompt: ([{"field": field, "status": "decided", "value": value, "confidence": confidence,
                             "evidence": evidence, "quote": quote, "reason": "newest"}], 0.01)


def run(conn, fn, **kw):
    return rc.reconcile(conn, ask_fn=fn, relevance={}, log=lambda m: None, **kw)


def test_accepted_answer_closes_the_conflict_as_machine_checks(conn):
    a, b = two_sources(conn)
    assert [c["fields"] for c in conflict_queue(conn)] == [["price_month"]]
    st = run(conn, answer(3200, "Rent $3,200 per month", evidence="E2"))
    assert st["resolved"] == 1 and conflict_queue(conn) == []
    rows = conn.execute("SELECT author, observation_id, verdict, true_value FROM checks ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("reconcile:sonnet", a, "wrong", "3200"), ("reconcile:sonnet", b, "correct", None)]
    assert conn.execute("SELECT price FROM listings WHERE source = 'zillow'").fetchone()[0] == 3200
    assert accuracy(conn) == {}  # no hand-checked votes
    assert {i["source"] for i in sample_queue(conn, include_trips=False)} == {"zillow", "zumper"}


def test_rejected_answers_write_nothing(conn):
    two_sources(conn)
    for fn in (answer(3200, "Rent $3,200 a month", evidence="E2"),   # not verbatim
               answer(3200, "Rent $3,200 per month", evidence="E1"),  # not in that block
               answer(3100, "Rent $3,200 per month", evidence="E2"),  # value not in quote
               answer(3200, "Rent $3,200 per month", evidence="E2", confidence="medium")):
        st = run(conn, fn)
        assert st["undecided"] == 1
    assert conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 0
    run(conn, answer(3200, "Rent $3,200 per month", evidence="E2"), dry_run=True)
    assert conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == 0


def test_never_over_the_users_check(conn):
    a, b = two_sources(conn)
    append_check(conn, "user", "conflict", "price", "correct", observation_id=a, stored_value=3000)
    items, skipped = rc.select(conn, relevance={})
    assert items == [] and skipped["user_checked"] == 1


def test_value_in_quote():
    t = "2026-11-25"
    assert rc.value_in_quote("baths", 1.5, "2 Beds, 1.5 Baths", t) and rc.value_in_quote("baths", 1.5, "1 bath + powder room", t)
    assert not rc.value_in_quote("price", 3200, "$13,200", t) and rc.value_in_quote("price", 3200, "$3,200/mo", t)
    assert rc.value_in_quote("beds", 0, "Studio apartment", t) and not rc.value_in_quote("beds", 2, "12 bed", t)
    assert rc.value_in_quote("available_from", "2026-11-20", "Available Now", t)
    assert rc.value_in_quote("available_from", "2026-12-06", "Available Dec 6", t)


def test_open_price_goes_to_the_current_page(conn):
    two_sources(conn)
    calls = []

    def fake_fetch(urls, residential, max_usd):
        calls.append((tuple(urls), residential))
        return ({"https://z/1": {"text": "Price change: now $3,200/mo for this 2 bed", "title": "t", "status": 200,
                                 "fetched": "2026-11-25"}} if not residential else {}), \
               {"usd": 0.01, "read": 1, "failed": [], "statuses": {"https://zu/9": 404}, "items": []}

    def fn(prompt):
        if "### P1" in prompt:
            return answer(3200, "now $3,200/mo for this 2 bed", evidence="P1")(prompt)
        return [{"field": "price", "status": "undecided", "confidence": "low", "reason": "?", "need_page": True}], 0.01

    import aptfinder.intake.apify as ap
    ap_credit = ap.credit
    ap.credit = lambda: {"left": 5.0}
    try:
        st = run(conn, fn, max_usd=0.5, fetch_fn=fake_fetch)
    finally:
        ap.credit = ap_credit
    assert calls == [(("https://z/1", "https://zu/9"), False)]  # the 404 is not retried through residential
    assert st["resolved"] == 1 and conflict_queue(conn) == []
    note = conn.execute("SELECT note FROM checks LIMIT 1").fetchone()[0]
    assert '"url": "https://z/1"' in note and '"evidence": "P1"' in note


def test_retraction_reopens_the_conflict(conn):
    two_sources(conn)
    run(conn, answer(3200, "Rent $3,200 per month", evidence="E2"))
    assert conflict_queue(conn) == []
    ids = [r[0] for r in conn.execute("SELECT id FROM checks ORDER BY id")]
    rc.retract(conn, ids[0], "test")
    assert [c["fields"] for c in conflict_queue(conn)] == [["price_month"]]
    append_check(conn, "user", "conflict", "price", "correct", observation_id=1, stored_value=3000)
    import pytest
    with pytest.raises(ValueError):
        rc.retract(conn, conn.execute("SELECT MAX(id) FROM checks").fetchone()[0], "a user's")


def test_available_now_keeps_past_dates_and_own_site_rows_stay():
    t = "2026-11-25"
    assert rc.same_value("available_from", "2026-11-04", "2026-11-25", t)
    assert not rc.same_value("available_from", "2026-12-04", "2026-11-25", t)
    ev = [{"source": "direct", "obs": {"price": None, "price_basis": None, "data": '{"beds": 1}'}}]
    assert rc.plan_checks({}, ev, "beds", 2, t)[0] == []
