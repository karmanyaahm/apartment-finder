from __future__ import annotations

from aptfinder import identity
from aptfinder.identity.ranges import AUTHOR, norm_name
from aptfinder.store import append_annotation, append_link, append_observation, start_run

RANGE = "1222-1244 University Dr, Menlo Park, CA 94025"


def obs(conn, run, source, sid, **data):
    return append_observation(conn, run, source, sid, data, price=data.pop("price", None), price_basis="month")


def links(conn):
    return {r["source_id"]: (r["place_key"], r["unit_key"], r["author"])
            for r in conn.execute("SELECT * FROM current_links")}


def _gardens(conn, name="University Gardens", operator=None, number="1226", zip_=" 94025", city="Menlo Park", tag=""):
    r = start_run(conn, "intake", "rentcafe")
    obs(conn, r, "rentcafe", "plan:2", address=RANGE, property_name="University Gardens", floor_plan="2 Bed x 1.5 Bath",
        beds=2, price=4495, kind="apartment", lat=37.4512, lon=-122.1877, operator="Acme")
    obs(conn, r, "rentcafe", "plan:3", address=RANGE, property_name="University Gardens", floor_plan="3 Bed x 3 Bath",
        beds=3, price=5695, kind="apartment", lat=37.4512, lon=-122.1877)
    for src in ("zillow", "craigslist"):  # one townhouse listed twice
        obs(conn, r, src, f"{src}-{number}{tag}", address=f"{number} University Dr, {city}, CA{zip_}", property_name=name,
            operator=operator, beds=3, price=5695, kind="apartment", lat=37.4511, lon=-122.1876)


def test_townhouses_join_the_range_each_its_own_unit_idempotently(conn):
    _gardens(conn)
    r = start_run(conn, "intake", "zillow")
    obs(conn, r, "zillow", "z1228", address="1228 University Dr, Menlo Park, CA 94025",
        property_name="The University Gardens Apartments", beds=3, price=5695, kind="apartment")
    c = identity.run(conn)
    assert c["range_merged_places"] == 2 and c["range_merged_listings"] == 3
    ln = links(conn)
    kept = ln["plan:2"][0]
    assert ln["zillow-1226"] == ln["craigslist-1226"] == (kept, f"{kept}/1226", AUTHOR)
    assert ln["z1228"][:2] == (kept, f"{kept}/1228")  # same beds and price, a different townhouse
    assert ln["plan:3"][1] == f"{kept}/3bedx3bath"
    assert conn.execute("SELECT n_units FROM places WHERE place_key = ?", (kept,)).fetchone()[0] == 4
    n = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    assert identity.run(conn)["linked"] == 0 and conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == n


def test_shared_operator_is_enough(conn):
    _gardens(conn, name="Townhouse for rent", operator="ACME")
    identity.run(conn)
    assert links(conn)["zillow-1226"][0] == links(conn)["plan:2"][0]


def test_a_range_alone_does_not_merge(conn):
    _gardens(conn, name="Sunny 3bd townhouse")
    identity.run(conn)
    assert links(conn)["zillow-1226"][0] != links(conn)["plan:2"][0]


def test_outside_the_range_or_another_zip_or_city_does_not_merge(conn):
    for i, kw in enumerate(({"number": "1246"}, {"number": "1220"}, {"zip_": " 94301"}, {"zip_": ""},
                            {"city": "Palo Alto"})):
        _gardens(conn, tag=str(i), **kw)
    identity.run(conn)
    ln = links(conn)
    kept = ln["plan:2"][0]
    assert all(pk != kept for sid, (pk, _, _) in ln.items() if not sid.startswith("plan:"))


def test_unit_named_in_the_address_is_kept_after_the_number(conn):
    _gardens(conn, number="1230 #B")
    identity.run(conn)
    kept = links(conn)["plan:2"][0]
    assert links(conn)["zillow-1230 #B"][1] == f"{kept}/1230-b"


def test_not_the_same_building_and_a_user_link_win(conn):
    _gardens(conn)
    identity.run(conn)
    kept = links(conn)["plan:2"][0]
    reason = conn.execute("SELECT reason FROM current_links WHERE source_id = 'zillow-1226'").fetchone()[0]
    merged = reason.split(": ")[-1].split(" merged into ")[0]
    append_link(conn, "user", "zillow", "zillow-1226", "u_x", "u_x", "manual split")
    identity.run(conn)
    assert links(conn)["zillow-1226"][0] == "u_x"
    append_annotation(conn, "user", "place_pair", "|".join(sorted((kept, merged))), "same_building", comment="no")
    identity.run(conn)
    assert links(conn)["craigslist-1226"][0] == merged


def test_norm_name():
    assert norm_name("The University Gardens Apartments") == norm_name("university gardens") == "university gardens"
    assert norm_name("Apartments") is None


def test_a_place_merged_by_hand_is_left_alone(conn):
    from aptfinder.store.keys import place_key

    _gardens(conn)
    town = place_key("1226 University Dr, Menlo Park, CA 94025", None, None, None)
    # you merged the range place into the townhouse's place by hand (Palo Alto Plaza on the live data): moving the
    # townhouse's rule listings to the range place would leave the hand-linked plans behind
    for sid in ("plan:2", "plan:3"):
        append_link(conn, "user", "rentcafe", sid, town, f"{town}/{sid}", "manual merge")
    c = identity.run(conn)
    assert "range_merged_places" not in c
    assert {pk for pk, _, _ in links(conn).values()} == {town}
