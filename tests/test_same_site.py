from __future__ import annotations

from aptfinder import identity
from aptfinder.identity.same_site import AUTHOR, building_site_key, is_generic_page
from aptfinder.store import append_annotation, append_link, append_observation, start_run


def obs(conn, run, source, sid, **data):
    return append_observation(conn, run, source, sid, data, price=data.pop("price", None), price_basis="month")


def places(conn):
    return {r["place_key"]: r["n_listings"] for r in conn.execute("SELECT place_key, n_listings FROM places")}


def test_building_site_key_drops_homepages_portfolio_pages_and_aggregators():
    assert building_site_key("https://www.udr.com/san-francisco-bay-area-apartments/mountain-view/verve/") \
        == "udr.com/san-francisco-bay-area-apartments/mountain-view/verve"
    assert building_site_key("http://www.zell.com/") is None
    assert building_site_key("https://caprop.com") is None
    assert building_site_key("https://www.zillow.com/b/verve-mountain-view-ca/") is None
    assert building_site_key("https://www.essexapartmenthomes.com/apartments/mountain-view") is None
    assert building_site_key("https://x.com/communities/ca/mountain-view-apartments") is None
    assert not is_generic_page("/apartments/sunnyvale/windsor-ridge")


def _two(conn, website_a, website_b, b_addr="1984 W El Camino Real, Mountain View, CA", lon_b=-122.0999):
    r = start_run(conn, "intake", "zumper")
    for i in range(3):
        obs(conn, r, "zumper", f"a{i}", address="2020 W El Camino Real, Mountain View, CA", property_name="Verve",
            lat=37.4, lon=-122.1, beds=i, price=3000 + i, extra={"website": website_a})
    obs(conn, r, "google_places", "b", address=b_addr, property_name="Verve", lat=37.4, lon=lon_b,
        extra={"website": website_b})


def test_same_building_site_under_100m_merges_into_the_bigger_place_idempotently(conn):
    url = "https://www.udr.com/san-francisco-bay-area-apartments/mountain-view/verve/"
    _two(conn, url, url)
    c = identity.run(conn)
    assert c["auto_merged_places"] == 1 and c["auto_merged_listings"] == 1
    assert len(places(conn)) == 1 and list(places(conn).values()) == [4]
    row = conn.execute("SELECT author, reason FROM current_links WHERE source_id = 'b'").fetchone()
    assert row["author"] == AUTHOR and "udr.com" in row["reason"]
    n = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    c2 = identity.run(conn)
    assert c2["linked"] == 0 and conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == n


def test_bare_homepage_does_not_merge(conn):
    _two(conn, "http://www.zell.com/", "http://zell.com")
    identity.run(conn)
    assert len(places(conn)) == 2


def test_far_apart_does_not_merge(conn):
    url = "https://www.udr.com/san-francisco-bay-area-apartments/mountain-view/verve/"
    _two(conn, url, url, lon_b=-122.098)  # ~180 m
    identity.run(conn)
    assert len(places(conn)) == 2


def test_not_the_same_building_undoes_the_merge_and_split_wins(conn):
    url = "https://www.udr.com/san-francisco-bay-area-apartments/mountain-view/verve/"
    _two(conn, url, url)
    identity.run(conn)
    assert len(places(conn)) == 1
    reason = conn.execute("SELECT reason FROM current_links WHERE source_id = 'b'").fetchone()[0]
    b, a = reason.split(": ")[-1].split(" merged into ")
    append_annotation(conn, "user", "place_pair", "|".join(sorted((a, b))), "same_building", comment="no")
    identity.run(conn)
    assert len(places(conn)) == 2
    # a user split still wins over the rule
    append_link(conn, "user", "zumper", "a0", "u_x", "u_x", "manual split")
    identity.run(conn)
    assert conn.execute("SELECT place_key FROM current_links WHERE source_id = 'a0'").fetchone()[0] == "u_x"
