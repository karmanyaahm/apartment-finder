"""One site, one entry: config.duplicate_sites and the readers that skip and retire duplicate entries.

The cases are the real ones of 2026-11-25: `park-place` (hand-listed) and `park-place-2` (discovered, the same URL
with a trailing slash, community 4500556); three discovered entries for Spruce (one URL, community 3323021)."""

import json

from aptfinder import settings
from aptfinder.config import duplicate_sites, load_sites, site_key
from aptfinder.intake.prometheus import API, read
from aptfinder.intake.render import last_outcomes, mark_duplicates
from aptfinder.store import append_observation, finish_run, start_run

PARK = "https://prometheusapartments.com/ca/mountain-view-apartments/park-place"
SPRUCE = "https://prometheusapartments.com/ca/sunnyvale-apartments/spruce/"
DAY = __import__("datetime").date(2026, 12, 1)


def _unit(n: int) -> dict:
    return {"rent": "3500", "bestRent": "3400", "floorPlanName": "Plan A", "bedrooms": "1", "bathrooms": "1",
            "area": "700", "unitNumber": str(100 + n), "madeReadyDate": "12/1/2026", "unitHoldStatus": "false"}


def _config(tmp_path, monkeypatch, ids: dict[str, str] | None = None):
    import shutil

    cfg = tmp_path / "cfg"
    shutil.copytree(settings.ROOT / "config.example", cfg)
    (cfg / "direct_sites.toml").write_text(
        f'[[site]]\nid = "park-place"\nname = "Park Place"\nplatform = "prometheus"\nurl = "{PARK}"\n')
    rows = [("park-place-2", "Park Place", PARK + "/"), ("applewood-spruce-apartments", "Spruce", SPRUCE),
            ("pepperwood-spruce-apartments", "Spruce", SPRUCE), ("655-south-fair-oaks-avenue", "Spruce", SPRUCE)]
    (cfg / "discovered_sites.toml").write_text("".join(
        f'\n[[found]]\nid = "{i}"\nname = "{n}"\nplatform = "prometheus"\nurl = "{u}"\nplace_key = "pk-{i}"\n'
        for i, n, u in rows))
    monkeypatch.setattr(settings, "CONFIG_DIR", cfg)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
    (tmp_path / "prometheus_ids.json").write_text(json.dumps(ids or {}))


def test_same_url_is_one_site_and_the_first_entry_in_load_order_is_canonical(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    assert site_key(PARK) == site_key(PARK + "/") == site_key("https://www.PrometheusApartments.com"
                                                              "/ca/mountain-view-apartments/park-place/#plans")
    assert sorted(load_sites()) == ["applewood-spruce-apartments", "park-place"]
    every = load_sites(duplicates=True)
    assert {sid: s["duplicate_of"] for sid, s in every.items() if s.get("duplicate_of")} == {
        "park-place-2": "park-place", "pepperwood-spruce-apartments": "applewood-spruce-apartments",
        "655-south-fair-oaks-avenue": "applewood-spruce-apartments"}


def test_redirect_links_that_differ_only_in_their_query_are_two_sites():
    sites = {"a": {"url": "https://www.lease-apts.com/?b=100-main-st"}, "b": {"url": "https://www.lease-apts.com/?b=9-oak-ave"},
             "c": {"url": "https://lease-apts.com/?b=9-oak-ave"}}
    assert duplicate_sites(sites) == {"c": "b"}


def test_same_prometheus_community_with_different_urls_is_one_site():
    sites = {"a": {"url": "https://prometheusapartments.com/ca/x/a", "platform": "prometheus"},
             "b": {"url": "https://prometheusapartments.com/ca/x/b-other-name", "platform": "prometheus"},
             "c": {"url": "https://c.example/", "platform": "photos"}}
    assert duplicate_sites(sites) == {}
    assert duplicate_sites(sites, {"a": "4500556", "b": "4500556", "c": "4500556"}) == {"b": "a"}


def test_the_reader_asks_once_per_building_and_retires_the_duplicates_rows(conn, tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {"park-place": "4500556", "park-place-2": "4500556",
                                    "applewood-spruce-apartments": "3323021", "pepperwood-spruce-apartments": "3323021",
                                    "655-south-fair-oaks-avenue": "3323021"})
    # the earlier read wrote every unit once per entry: 21 x 2 for Park Place, 25 x 3 for Spruce
    r0 = start_run(conn, "intake", "direct", {"render": True, "scope": "partial"})
    areas = {}
    for sid, n in (("park-place", 21), ("park-place-2", 21), ("applewood-spruce-apartments", 25),
                   ("pepperwood-spruce-apartments", 25), ("655-south-fair-oaks-avenue", 25)):
        for i in range(n):
            append_observation(conn, r0, "direct", f"{sid}:r:{100 + i}", {"unit": str(100 + i), "extra": {
                "area": f"{sid}#render", "site": sid}}, price=3400, price_basis="month")
        areas[f"{sid}#render"] = {"state": "complete", "outcome": "ok", "items": n}
    finish_run(conn, r0, "ok", {"areas": areas})

    park, spruce = API.format(id="4500556", date="2026-12-01"), API.format(id="3323021", date="2026-12-01")
    calls: list[str] = []

    def get(url, headers):
        calls.append(url)
        return 200, json.dumps([_unit(i) for i in range(21 if url == park else 25)])

    res = read(conn, get=get, delay=0, today=DAY)
    assert sorted(calls) == sorted([park, spruce])  # one request per building
    assert res["sites"]["park-place-2"] == "duplicate of park-place"
    assert res["sites"]["pepperwood-spruce-apartments"] == "duplicate of applewood-spruce-apartments"
    active = dict(conn.execute("SELECT substr(source_id, 1, instr(source_id, ':') - 1), SUM(active) FROM listings "
                               "GROUP BY 1").fetchall())
    assert active == {"park-place": 21, "park-place-2": 0, "applewood-spruce-apartments": 25,
                      "pepperwood-spruce-apartments": 0, "655-south-fair-oaks-avenue": 0}
    last = last_outcomes(conn)
    assert last["park-place-2"]["outcome"] == "duplicate" and last["park-place-2"]["duplicate_of"] == "park-place"
    # marked once: the next read writes no second mark
    n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    read(conn, get=get, delay=0, today=DAY)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == n_runs + 1  # the read's own run only


def test_a_duplicate_is_retired_even_when_the_reads_run_is_refused(conn, tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch, {"park-place": "4500556", "park-place-2": "4500556"})
    r0 = start_run(conn, "intake", "direct", {"render": True, "scope": "partial"})
    for sid in ("park-place", "park-place-2"):
        append_observation(conn, r0, "direct", f"{sid}:r:101", {"extra": {"area": f"{sid}#render"}}, price=3400,
                           price_basis="month")
    finish_run(conn, r0, "ok", {"areas": {}})
    read(conn, sites=["park-place", "park-place-2"], delay=0, today=DAY,
         get=lambda url, h: (403, '{"error":{"code":"challenge","message":"x"}}'))
    active = dict(conn.execute("SELECT source_id, active FROM listings").fetchall())
    assert active == {"park-place:r:101": 1, "park-place-2:r:101": 0}  # refused: the canonical keeps its rows
    assert last_outcomes(conn)["park-place"]["outcome"] == "blocked"
    assert mark_duplicates(conn) == {}  # already marked


def test_discover_never_adds_a_url_already_in_the_list(conn, tmp_path, monkeypatch):
    from aptfinder import identity
    from aptfinder.intake import discover

    _config(tmp_path, monkeypatch)
    run = start_run(conn, "intake", "apartments_com", {})
    append_observation(conn, run, "apartments_com", "p1", {
        "property_name": "Park Place Apartments", "address": "100 View St, Mountain View, CA 94041",
        "lat": 37.3947, "lon": -122.0763})
    identity.run(conn)
    monkeypatch.setattr(discover, "ask", lambda group, model, usd: (
        [{"key": c["key"], "url": PARK + "/?utm=x"} for c in group], 0.01))
    res = discover.discover(conn, limit=5, batch=1, model="haiku", max_usd=1.0, fetch=False)
    assert res["found"] == 0 and res["missed"] == 1
    (miss,) = discover._records()["miss"]
    assert miss["reason"].startswith("same site as park-place")


def test_the_pipeline_lists_duplicate_entries_collapsed(conn, tmp_path, monkeypatch):
    from aptfinder.pipeline import overview

    _config(tmp_path, monkeypatch)
    mark_duplicates(conn)
    ov = overview(conn)
    (st,) = [s for s in ov["stages"] if s["id"] == "sites"]
    assert "3 duplicate entries collapsed" in st["summary"] and "park-place-2 = park-place" in st["summary"]
    assert st["counts"].get("todo") == 2  # only the two canonical entries count
    row = next(r for r in ov["sites"] if r["id"] == "park-place-2")
    assert row["render"]["label"] == "duplicate of park-place"
