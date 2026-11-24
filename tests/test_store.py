from __future__ import annotations

import sqlite3

import pytest

from aptfinder.store import (
    append_annotation,
    append_check,
    append_link,
    append_observation,
    append_trip,
    finish_run,
    init_db,
    start_run,
)
from aptfinder.store.keys import parse_address, place_key, unit_key


def _obs(conn, run, source="zillow", sid="1", price=5000.0, observed_at=None, **data):
    base = {"address": "49 Showers Dr Apt 3, Mountain View, CA 94040", "beds": 3, "baths": 2,
            "lat": 37.40, "lon": -122.11}
    base.update(data)
    return append_observation(conn, run, source, sid, base, price=price, price_basis="month",
                              observed_at=observed_at)


def _link(conn, source="zillow", sid="1", author="addr_unit", pk="p_a", uk="p_a/3"):
    return append_link(conn, author, source, sid, pk, uk, "test")


@pytest.mark.parametrize("table,where", [
    ("observations", "1=1"), ("links", "1=1"), ("trips", "1=1"), ("checks", "1=1"),
    ("annotations", "1=1"),
])
def test_log_tables_reject_update_and_delete(conn, table, where):
    run = start_run(conn, "intake", "zillow")
    oid = _obs(conn, run)
    _link(conn)
    tid = append_trip(conn, "p_a", "commute_targets:x", "walk", 10, 800, [], None, "fake")
    append_check(conn, "user", "sample", "price", "correct", observation_id=oid, stored_value=5000)
    append_annotation(conn, "user", "place", "p_a", "overall", rating=4)
    assert tid
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"UPDATE {table} SET id = id WHERE {where}")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"DELETE FROM {table} WHERE {where}")


def test_runs_close_out_once_only(conn):
    run = start_run(conn, "intake", "zillow", {"scope": "full"})
    finish_run(conn, run, "ok", {"n": 1})
    with pytest.raises(sqlite3.IntegrityError, match="closed out once"):
        finish_run(conn, run, "error")
    run2 = start_run(conn, "intake", "zillow")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE runs SET source = 'other' WHERE id = ?", (run2,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM runs")


def test_init_is_idempotent(conn):
    init_db(conn)
    init_db(conn)


def test_views_reflect_newer_observation(conn):
    r1 = start_run(conn, "intake", "zillow")
    _obs(conn, r1, price=5000, observed_at="2026-11-01T00:00:00+00:00")
    _link(conn)
    r2 = start_run(conn, "intake", "zillow")
    _obs(conn, r2, price=4800, observed_at="2026-11-02T00:00:00+00:00")
    row = conn.execute("SELECT * FROM listings").fetchone()
    assert row["price"] == 4800
    assert row["n_obs"] == 2 and row["n_versions"] == 2
    assert row["price_per_bed"] == pytest.approx(1600)
    assert conn.execute("SELECT COUNT(*) FROM obs_history").fetchone()[0] == 2


def test_obs_history_collapses_unchanged(conn):
    for day in (1, 2, 3):
        r = start_run(conn, "intake", "zillow")
        _obs(conn, r, observed_at=f"2026-11-0{day}T00:00:00+00:00")
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM obs_history").fetchone()[0] == 1


def test_views_reflect_newer_link_and_user_wins(conn):
    r = start_run(conn, "intake", "zillow")
    _obs(conn, r)
    _link(conn, pk="p_a", uk="p_a")
    assert conn.execute("SELECT place_key FROM listings").fetchone()[0] == "p_a"
    _link(conn, author="user", pk="p_b", uk="p_b")
    assert conn.execute("SELECT place_key FROM listings").fetchone()[0] == "p_b"
    # a later rule row does not undo the user's decision
    _link(conn, author="addr_unit", pk="p_c", uk="p_c")
    assert conn.execute("SELECT place_key FROM listings").fetchone()[0] == "p_b"


def test_active_flag_follows_latest_full_run(conn):
    r1 = start_run(conn, "intake", "zillow", {"scope": "full"})
    _obs(conn, r1, sid="1")
    _obs(conn, r1, sid="2")
    finish_run(conn, r1)
    r2 = start_run(conn, "intake", "zillow", {"scope": "full"})
    _obs(conn, r2, sid="1")
    finish_run(conn, r2)
    active = dict(conn.execute("SELECT source_id, active FROM listings").fetchall())
    assert active == {"1": 1, "2": 0}
    # a targeted run does not mark others inactive
    r3 = start_run(conn, "intake", "zillow", {"scope": "targeted"})
    _obs(conn, r3, sid="2")
    finish_run(conn, r3)
    active = dict(conn.execute("SELECT source_id, active FROM listings").fetchall())
    assert active == {"1": 1, "2": 1}


def test_wrong_check_overrides_until_source_changes(conn):
    r1 = start_run(conn, "intake", "zillow")
    oid = _obs(conn, r1, price=5000, observed_at="2026-11-01T00:00:00+00:00")
    append_check(conn, "user", "sample", "price", "wrong", observation_id=oid,
                 stored_value=5000, true_value=5200)
    append_check(conn, "user", "sample", "baths", "wrong", observation_id=oid,
                 stored_value=2, true_value=2.5)
    row = conn.execute("SELECT price, baths, overridden FROM listings").fetchone()
    assert row["price"] == 5200 and row["baths"] == 2.5
    assert set(row["overridden"].split(",")) == {"price", "baths"}

    # same value re-observed: the correction still applies
    r2 = start_run(conn, "intake", "zillow")
    _obs(conn, r2, price=5000, observed_at="2026-11-02T00:00:00+00:00")
    assert conn.execute("SELECT price FROM listings").fetchone()[0] == 5200

    # source changes price: correction lapses, new scraped value shown; baths override persists
    r3 = start_run(conn, "intake", "zillow")
    _obs(conn, r3, price=4900, observed_at="2026-11-03T00:00:00+00:00")
    row = conn.execute("SELECT price, baths FROM listings").fetchone()
    assert row["price"] == 4900 and row["baths"] == 2.5


def test_later_correct_check_cancels_wrong(conn):
    r1 = start_run(conn, "intake", "zillow")
    oid = _obs(conn, r1, price=5000)
    append_check(conn, "user", "sample", "price", "wrong", observation_id=oid, stored_value=5000,
                 true_value=5200)
    append_check(conn, "user", "sample", "price", "correct", observation_id=oid, stored_value=5000)
    assert conn.execute("SELECT price FROM listings").fetchone()[0] == 5000


def test_places_group_listings(conn):
    r = start_run(conn, "intake", "zillow")
    _obs(conn, r, source="zillow", sid="1", price=5000)
    _obs(conn, r, source="apartments", sid="9", price=5100)
    _link(conn, source="zillow", sid="1", pk="p_a", uk="p_a/3")
    _link(conn, source="apartments", sid="9", pk="p_a", uk="p_a/3")
    place = conn.execute("SELECT * FROM places").fetchone()
    assert place["n_listings"] == 2 and place["min_price_month"] == 5000
    assert set(place["sources"].split(",")) == {"zillow", "apartments"}
    assert conn.execute("SELECT n_listings FROM units").fetchone()[0] == 2


def test_stay_price_normalized_to_month(conn):
    r = start_run(conn, "intake", "airbnb")
    append_observation(conn, r, "airbnb", "a1", {"beds": 1}, price=3100, price_basis="stay",
                       query_checkin="2026-12-01", query_checkout="2027-01-01")
    pm = conn.execute("SELECT price_month FROM listings").fetchone()[0]
    assert pm == pytest.approx(3100 * 30.4375 / 31)


def test_address_parsing_and_keys():
    a = parse_address("49 Showers Dr Floor 1-ID1622, Mountain View, CA 94040")
    assert a == {"street": "49 showers dr", "unit": "1-id1622", "city": "mountain view"}
    b = parse_address("Avalon Mountain View (AvalonBay), 1600 Villa St, Mountain View, CA 94041 - units 1")
    assert b["street"] == "1600 villa st" and b["city"] == "mountain view"
    c = parse_address("825 Menlo Ave APT F, Menlo Park, CA 94025")
    assert c["unit"] == "f" and c["street"] == "825 menlo ave"
    assert parse_address("147 South Mathilda Avenue, Sunnyvale")["street"] == "147 s mathilda ave"
    k1 = place_key("825 Menlo Ave APT F, Menlo Park, CA 94025", None, None, None)
    k2 = place_key("825 Menlo Avenue #G", "Menlo Park", None, None)
    assert k1 == k2
    assert unit_key(k1, "825 Menlo Ave APT F, Menlo Park", None, None) == f"{k1}/f"
    assert place_key(None, None, 37.40001, -122.1) == place_key(None, None, 37.40004, -122.10002)
    assert place_key(None, None, None, None) is None


def test_init_db_is_noop_when_schema_unchanged(conn):
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    assert v != 0
    conn.execute("PRAGMA user_version = 1")
    init_db(conn)  # changed version -> rebuild, restores the hash
    assert conn.execute("PRAGMA user_version").fetchone()[0] == v
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0


def test_address_without_comma_before_the_city_gets_the_same_key():
    from aptfinder.store.keys import parse_address, place_key

    a = "234 Escuela Avenue Mountain View, CA 94040"
    b = "234 Escuela Avenue, Mountain View, CA 94040"
    assert place_key(a, None, None, None) == place_key(b, None, None, None)
    assert parse_address(a) == {"street": "234 escuela ave", "unit": None, "city": "mountain view"}
    assert parse_address("273 Mountain View Ave, Mountain View, CA")["street"] == "273 mountain view ave"
    assert parse_address("400 Avenue A Apt 3, Palo Alto")["street"] == "400 ave a"  # 'Avenue' is the name here


def test_address_city_beats_the_listing_city_field_and_unit_and_range_quirks():
    from aptfinder.store.keys import place_key

    k = lambda a, c=None: place_key(a, c, None, None)  # noqa: E731
    assert k("1095 W El Camino Real , Sunnyvale, CA 94087", "mountain view") == k("1095 West El Camino Real, Sunnyvale, CA")
    assert k("1870 California St, 11, Mountain View, CA") == k("1870 California St APT 11, Mountain View, CA")
    assert k("881-881 E El Camino Real, Mountain View, CA") == k("881 E El Camino Real, Mountain View, CA")
    assert k("4180-4190 Byron St, Palo Alto") != k("4180 Byron St, Palo Alto")  # a real range stays a range
    assert k("850 Calderon Ave", "mountain view") == k("850 Calderon Ave, Mountain View, CA")  # city field as fallback
