"""Baths an own site leaves out: borrowed from the same plan on another source, else read from the plan's own
drawing (aptfinder.planread); a hand-set value always wins."""

from __future__ import annotations

import json

from aptfinder import identity, planread
from aptfinder.config import Goal, GoalsConfig, load_goals
from aptfinder.filter import evaluate_all
from aptfinder.filter.rules import CHECK, PASS
from aptfinder.plans import inherit_baths
from aptfinder.store import append_check, append_observation, finish_run, start_run

HERE = dict(place_key="p1", active=1)


def _row(source, sid, **f):
    return {"source": source, "source_id": sid, **HERE, "kind": "apartment", **f}


def test_borrow_needs_same_beds_and_size_or_a_real_plan_name_and_agreement():
    rows = [
        _row("direct", "realm:2d", beds=2, baths=None, sqft=1052, floor_plan="Plan 2d"),
        _row("rentcafe", "plan:1", beds=2, baths=2, sqft=1052, floor_plan="2x2B"),
        _row("rentcafe", "plan:other", beds=2, baths=1, sqft=900, floor_plan="2x1"),  # another size: not this plan
        # generic names name no plan: '2 BR' on the own site and Zillow's '2bd' never match by name
        _row("direct", "x:2-br", beds=2, baths=None, sqft=None, floor_plan="2 BR"),
        _row("zillow", "9:2bd", beds=2, baths=1, sqft=None, floor_plan="2bd"),
        # a distinctive plan name matches without a size
        _row("direct", "x:ultramarine", beds=3, baths=None, sqft=None, floor_plan="ULTRAMARINE"),
        _row("apartments_com", "a:m:ultramarine", beds=3, baths=2, sqft=1300, floor_plan="Ultramarine"),
        # candidates that disagree: left unknown and reported
        _row("direct", "x:1a", beds=1, baths=None, sqft=700, floor_plan="1A"),
        _row("zumper", "z1", beds=1, baths=1, sqft=705),
        _row("apartments_com", "a1", beds=1, baths=1.5, sqft=695),
        # a furnished stay of the same size is another offer; an inactive listing does not count
        _row("direct", "x:3c", beds=3, baths=None, sqft=1200, floor_plan="3C"),
        _row("blueground", "b1", beds=3, baths=3, sqft=1200, term="short"),
        {**_row("zillow", "old", beds=3, baths=2, sqft=1200), "active": 0},
    ]
    dis = inherit_baths(rows)
    by = {r["source_id"]: r for r in rows}
    assert by["realm:2d"]["baths"] == 2
    assert by["realm:2d"]["baths_from"] == {"how": "borrowed", "source": "rentcafe", "source_id": "plan:1", "n": 1}
    assert by["x:2-br"]["baths"] is None
    assert by["x:ultramarine"]["baths"] == 2
    assert by["x:1a"]["baths"] is None and "baths_from" not in by["x:1a"]
    assert dis == [{"source_id": "x:1a", "place_key": "p1", "values": {1: ["zumper|z1"], 1.5: ["apartments_com|a1"]}}]
    assert by["x:3c"]["baths"] is None


def test_a_drawing_reading_fills_only_what_borrowing_left():
    rows = [_row("direct", "pp:studio", beds=0, baths=None, sqft=None, floor_plan="Studio Floor Plan")]
    inherit_baths(rows, {("direct", "pp:studio"): {"baths": 1, "seen": "Bath", "file": "abc"}})
    assert rows[0]["baths"] == 1 and rows[0]["baths_from"]["how"] == "drawing"


def test_accept_only_a_confident_plan_that_agrees_with_the_rows_beds():
    ok = {"is_floor_plan": True, "bedrooms": 2, "full_baths": 2, "half_baths": 1, "confident": True, "seen": "2 BD"}
    assert planread.accept(ok, 2.0) == {"baths": 2.5, "beds": 2, "seen": "2 BD"}
    assert planread.accept(ok, 3) is None  # beds disagree
    assert planread.accept({**ok, "confident": False}, 2) is None
    assert planread.accept({**ok, "is_floor_plan": False}, 2) is None
    assert planread.accept({**ok, "full_baths": None}, 2) is None
    assert planread.accept({**ok, "full_baths": 0}, 2) is None
    assert planread.accept(ok, None) is None


def _obs(conn, source, rows):
    run = start_run(conn, "intake", source, {"scope": "full"})
    for sid, data, price in rows:
        append_observation(conn, run, source, sid, data, url="https://x", price=price, price_basis="month")
    finish_run(conn, run, "ok", {})


def _size_goal() -> GoalsConfig:
    return GoalsConfig(goals={"g": Goal(label="g", rules=[{"type": "size_options", "options": [
        {"beds": 3, "min_baths": 2}, {"beds": 4, "min_baths": 3}]}], nice=[])}, verify=load_goals().verify)


def test_filter_uses_borrowed_and_drawing_baths_and_a_hand_value_wins(conn, tmp_path, monkeypatch):
    from aptfinder import media, settings

    monkeypatch.setattr(settings, "MEDIA_DIR", tmp_path / "media")
    at = dict(address="1 Plan St, Mountain View, CA", lat=37.4, lon=-122.08, kind="apartment")
    img = "https://own.example/plans/c.png"
    _obs(conn, "direct", [
        ("s:r:b", {**at, "beds": 3, "sqft": 1210, "floor_plan": "B"}, 6000),
        ("s:r:c", {**at, "beds": 3, "floor_plan": "C", "extra": {"floorplan_image": img}}, 6100),
        ("s:r:d", {**at, "beds": 3, "floor_plan": "D"}, 6200),
    ])
    _obs(conn, "apartments_com", [("a:m:b", {**at, "beds": 3, "baths": 2, "sqft": 1200, "floor_plan": "B1"}, 6050)])
    identity.run(conn)
    # the drawing of plan C is in the photo store
    sha = "c" * 64
    conn.execute("INSERT INTO media (fetched_at, url, kind, status, sha256) VALUES ('t', ?, 'plan', 200, ?)", (img, sha))
    media.media_path(sha).parent.mkdir(parents=True)
    media.media_path(sha).write_bytes(b"jpeg")
    conn.commit()
    dry = planread.run(conn, dry_run=True)
    assert (dry["direct_rows_without_baths"], dry["borrowed"], dry["still_without"], dry["with_stored_drawing"],
            dry["to_ask"]) == (3, 1, 2, 1, 1)
    asked = []

    def stub(jpeg, model, cap):
        asked.append(jpeg)
        return {"is_floor_plan": True, "bedrooms": 3, "full_baths": 2, "half_baths": 1, "confident": True,
                "seen": "Bedroom 1-3, Bath 1, Bath 2, Powder"}, 0.001

    res = planread.run(conn, ask_fn=stub)
    assert asked == [b"jpeg"] and res["accepted_rows"] == 1
    assert planread.run(conn, ask_fn=stub)["to_ask"] == 0  # read once, kept in the run's counts
    out = evaluate_all(conn, _size_goal())["listings"]["g"]
    assert out[("direct", "s:r:b")]["status"] == PASS
    assert "baths from apartments_com" in out[("direct", "s:r:b")]["reasons"][0][1]
    assert out[("direct", "s:r:c")]["status"] == PASS
    assert "floor plan drawing" in out[("direct", "s:r:c")]["reasons"][0][1]
    assert out[("direct", "s:r:d")]["status"] == CHECK  # nothing to borrow, no drawing: still a check
    # the user sets plan B's baths by hand (the drawer's 'set ba': a check on the unknown value): it wins
    obs = conn.execute("SELECT obs_id FROM listings WHERE source_id = 's:r:b'").fetchone()[0]
    append_check(conn, "user", "adhoc", "baths", "wrong", observation_id=obs, stored_value=None, true_value=1)
    out = evaluate_all(conn, _size_goal())["listings"]["g"]
    assert "is 3bd/1ba" in out[("direct", "s:r:b")]["reasons"][0][1]
    assert "baths from" not in out[("direct", "s:r:b")]["reasons"][0][1]
    # and filling a value the source left empty is no accuracy vote against the source
    from aptfinder.verify import accuracy

    assert ("direct", "baths") not in accuracy(conn)
    assert json.loads(conn.execute("SELECT counts FROM runs WHERE component = 'planread'").fetchone()[0])["readings"][sha]["v"] == 1


def test_borrow_from_a_zillow_building_rows_named_plan():
    """Zillow's bedroom row ('3bd') names no plan itself, but its building page's plans do (`extra.building_plans`):
    an own-site plan of the same name and beds takes that plan's baths (Indigo ULTRAMARINE, 2026-11-25)."""
    plans = [{"name": "Ultramarine", "beds": 3, "baths": 2, "sqft": 1481, "price": 7031},
             {"name": "Cobalt", "beds": 3, "baths": 2.5, "sqft": 1600, "price": 8000}]
    rows = [
        _row("direct", "i:ultramarine", beds=3, baths=None, sqft=None, floor_plan="ULTRAMARINE"),
        _row("direct", "i:azure", beds=3, baths=None, sqft=None, floor_plan="AZURE"),  # no plan of that name
        _row("direct", "i:3-br", beds=3, baths=None, sqft=None, floor_plan="3 BR"),  # generic: never by name
        _row("zillow", "9:3bd", beds=3, baths=None, sqft=None, floor_plan="3bd",
             data=json.dumps({"extra": {"building_plans": plans}})),
    ]
    inherit_baths(rows)
    by = {r["source_id"]: r for r in rows}
    assert by["i:ultramarine"]["baths"] == 2
    assert by["i:ultramarine"]["baths_from"] == {"how": "borrowed", "source": "zillow", "source_id": "9:3bd", "n": 1}
    assert by["i:azure"]["baths"] is None and by["i:3-br"]["baths"] is None
    # the drawer's rows carry `data` parsed
    drawer = [{**_row("direct", "i:u", floor_plan="Ultramarine"), "fields": {"beds": 3, "baths": None,
                                                                            "floor_plan": "Ultramarine", "kind": "apartment"}},
              {**_row("zillow", "9:3bd"), "data": {"extra": {"building_plans": plans}},
               "fields": {"beds": 3, "baths": 2, "floor_plan": "3bd", "kind": "apartment"}}]
    inherit_baths(drawer, fields=lambda r: r["fields"])
    assert drawer[0]["fields"]["baths"] == 2
