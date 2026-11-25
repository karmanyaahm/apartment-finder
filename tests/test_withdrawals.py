"""Off the market (view withdrawals): the user's 'no longer for rent' mark and Zillow detail pages that say removed
or sold. Newest evidence wins; only a fresh scrape (not an extract or a reparse) or a FOR_RENT page brings it back."""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from aptfinder import verify
from aptfinder.intake import zillow_detail as zd
from aptfinder.store import append_observation, connect, finish_run, init_db, start_run
from aptfinder.web.app import create_app

from test_zillow_detail import FOR_SALE, UNIT, _zillow, fake  # noqa: F401  (fixture)

REMOVED = {  # an OTHER page whose newest event is 'Listing removed' (2105-style pages without an event stay items)
    **UNIT, "home_status": "OTHER",
    "price_history": [{"date": "2026-11-24", "event": "Listing removed", "price": 40000},
                      {"date": "2026-11-01", "event": "Listed for rent", "price": 40000}]}
SOLD = {"zillow_id": 466640639, "home_status": "RECENTLY_SOLD", "price_history": []}


@pytest.fixture
def clock(monkeypatch):
    """A clock one second per call, so 'newer' is never a tie within a test."""
    t0 = datetime(2026, 11, 25, tzinfo=timezone.utc)
    tick = itertools.count()
    monkeypatch.setattr("aptfinder.store.db.now_iso",
                        lambda: (t0 + timedelta(seconds=next(tick))).isoformat(timespec="seconds"))


def _active(conn, sid):
    return conn.execute("SELECT active, gone_by FROM listings WHERE source = 'zillow' AND source_id = ?",
                        (sid,)).fetchone()


def _rescrape(conn, sid, **params):
    run = start_run(conn, "intake", "zillow", params)
    append_observation(conn, run, "zillow", sid, {"address": "x", "beds": 6, "extra": {"zpid": int(sid.split(":")[0])}},
                       url="https://x", price=40000, price_basis="month")
    finish_run(conn, run, "ok", {})


def test_removed_or_sold_pages_retire_without_an_item_ambiguous_ones_stay(conn, fake, clock, monkeypatch):
    pages = {"466640639": SOLD, "122197342": REMOVED, "124743044": FOR_SALE}
    from aptfinder.intake.apify import ActorRun

    monkeypatch.setattr("aptfinder.intake.apify.run_actor", lambda actor, ri, **kw: ActorRun(
        actor, "r", "SUCCEEDED", [pages[z] for z in ri["zpids"]], 0.0, {}, "u"))
    _zillow(conn)
    zd.fetch(conn, max_usd=1.0)
    assert tuple(_active(conn, "122197342")) == (0, "zillow_detail")
    assert tuple(_active(conn, "466640639:1bd")) == (0, "zillow_detail")  # a building's rows share the page
    assert tuple(_active(conn, "124743044")) == (1, None)  # FOR_SALE with no removal: the user decides
    assert [i["listings"][0]["source_id"] for i in verify.stale_queue(conn)] == ["124743044"]
    # an extract or a reparse re-appends old data: proves nothing
    _rescrape(conn, "122197342", reparse_of="data/raw/zillow/run1.json")
    run = start_run(conn, "extract", "zillow", {})
    append_observation(conn, run, "zillow", "122197342", {"beds": 6}, price=40000, price_basis="month")
    finish_run(conn, run, "ok", {})
    assert _active(conn, "122197342")["active"] == 0
    # a fresh scrape that sees it again: back on the list
    _rescrape(conn, "122197342", scope="targeted")
    assert tuple(_active(conn, "122197342")) == (1, None)


def test_user_mark_and_undo_are_appended_checks(conn, fake, clock):
    _zillow(conn)
    zd.fetch(conn, max_usd=1.0)
    sid = "124743044"
    assert verify.stale_queue(conn)
    c = TestClient(create_app(conn.execute("PRAGMA database_list").fetchone()["file"]))
    n0 = conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
    assert c.post("/api/listings/gone", json={"source": "zillow", "source_id": sid, "queue": "conflict"}).status_code == 200
    assert tuple(_active(conn, sid)) == (0, "user")
    assert verify.stale_queue(conn) == []  # answered: the item left Verify
    row = conn.execute("SELECT * FROM checks ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["field"], row["verdict"], row["queue"], row["true_value"]) == ("active", "wrong", "conflict", '"gone"')
    # not a field reading: no accuracy tally, no sample requeue, no current check
    assert not [k for k in verify.accuracy(conn) if k[1] == "active"]
    assert not conn.execute("SELECT 1 FROM current_checks WHERE field = 'active'").fetchone()
    # the drawer shows it with its date
    pk = conn.execute("SELECT place_key FROM listings WHERE source_id = ?", (sid,)).fetchone()[0]
    if pk:
        lst = next(x for x in c.get(f"/api/places/{pk}").json()["listings"] if x["source_id"] == sid)
        assert lst["gone_by"] == "user" and lst["gone_at"].startswith("2026-11-25") and not lst["active"]
    # undo appends a reversing check
    assert c.post("/api/listings/gone", json={"source": "zillow", "source_id": sid, "gone": False}).status_code == 200
    assert tuple(_active(conn, sid)) == (1, None)
    assert conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0] == n0 + 2
    # marked gone again, then a fresh scrape sees it: newest evidence wins
    c.post("/api/listings/gone", json={"source": "zillow", "source_id": sid})
    assert _active(conn, sid)["active"] == 0
    _rescrape(conn, sid, scope="targeted")
    assert _active(conn, sid)["active"] == 1
    assert c.post("/api/listings/gone", json={"source": "zillow", "source_id": "nope"}).status_code == 404
