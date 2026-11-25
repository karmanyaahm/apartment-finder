"""Operator portfolio pages: the generic Haiku answer is kept only as far as the page shows it. The fixture is a trimmed
copy of the UDR Mountain View page from the 2026-11-24 trial, with two answers changed to fail the checks."""

from __future__ import annotations

from aptfinder.config import Area
from aptfinder.intake import ADAPTERS, portfolio

# --------------------------------------------------------------------------------------------- portfolio
UDR_PAGE = {
    "operator": "UDR", "city": "Mountain View", "status": 200, "title": "Mountain View Apartments | UDR",
    "url": "https://www.udr.com/san-francisco-bay-area-apartments/mountain-view",
    "text": "Mountain View Apartments for Rent " + "x " * 120 + "Birch Creek 575 S. Rengstorff Avenue Mountain View, "
            "CA 94040 1-2 Bedrooms Verve 1984 West El Camino Real Mountain View, CA 94040",
    "links": [["https://www.udr.com/san-francisco-bay-area-apartments/mountain-view/birch-creek/", "Birch Creek"],
              ["https://www.udr.com/san-francisco-bay-area-apartments/mountain-view/verve/", "Verve"]],
    "page_state": "ok", "model": "haiku",
    "communities": [
        {"name": "Birch Creek", "address": "575 S. Rengstorff Avenue", "city": "Mountain View, CA 94040",
         "url": "https://www.udr.com/san-francisco-bay-area-apartments/mountain-view/birch-creek/"},
        {"name": "Verve", "address": "1984 W El Camino Real", "city": "Mountain View",  # address not as written
         "url": "https://www.udr.com/made-up-link"},  # link not on the page
        {"name": "Birch Grove", "address": None, "city": None, "url": None},  # not on the page
    ]}


def test_portfolio_keeps_only_what_the_page_shows():
    out = ADAPTERS["portfolio"].parse(UDR_PAGE, {})
    assert [p.data.property_name for p in out] == ["Birch Creek", "Verve"]
    b, v = out
    assert b.data.address == "575 S. Rengstorff Avenue, Mountain View, CA 94040" and b.data.zip == "94040"
    assert b.data.operator == "UDR" and b.data.kind == "site_info"
    assert b.data.extra["website"] == "https://www.udr.com/san-francisco-bay-area-apartments/mountain-view/birch-creek/"
    assert b.source_id == "udr.com/san-francisco-bay-area-apartments/mountain-view/birch-creek"
    assert v.data.address is None and v.data.extra["website"] is None and v.data.operator is None
    assert v.source_id == "udr:verve"


def test_portfolio_blocked_and_empty_pages():
    assert portfolio.page_state({"status": 403, "text": "Access denied"}) == "blocked"
    assert portfolio.page_state({"status": 429, "text": "x" * 500}) == "blocked"
    assert portfolio.page_state({"status": 404, "text": "Not found"}) == "empty"
    assert ADAPTERS["portfolio"].parse({**UDR_PAGE, "page_state": "failed"}, {}) is None
    assert ADAPTERS["portfolio"].parse({**UDR_PAGE, "communities": []}, {}) == []


def test_portfolio_input_is_every_operator_by_city():
    cfg = {"operators": [{"name": "A", "url": "https://a.example/{slug}-apartments"},
                         {"name": "B", "url": "https://b.example/s/{slug}-ca"}]}
    inp = portfolio.build_input(cfg, [Area(kind="city", id="Mountain View", label="")])
    assert [r["url"] for r in inp["startUrls"]] == ["https://a.example/mountain-view-apartments",
                                                     "https://b.example/s/mountain-view-ca"]
    assert inp["startUrls"][0]["userData"] == {"operator": "A", "city": "Mountain View"}


def test_residential_operators_get_their_own_run(monkeypatch):
    from aptfinder.intake.apify import ActorRun

    cfg = {"actor": "x", "operators": [{"name": "A", "url": "https://a.example/{slug}"},
                                       {"name": "B", "url": "https://b.example/{slug}", "proxy": "residential"}],
           "max_claude_usd": 0}
    area = [Area(kind="city", id="Mountain View", label="")]
    assert [r["url"] for r in portfolio.build_input(cfg, area)["startUrls"]] == ["https://a.example/mountain-view"]
    res = portfolio.build_input(cfg, area, residential=True)
    assert [r["url"] for r in res["startUrls"]] == ["https://b.example/mountain-view"]
    assert res["proxyConfiguration"]["apifyProxyGroups"] == ["RESIDENTIAL"] and "preNavigationHooks" in res
    runs = []

    def fake(actor, inp, **kw):
        runs.append(inp["proxyConfiguration"])
        return ActorRun(actor=actor, run_id=str(len(runs)), status="SUCCEEDED", items=[], cost_usd=0.01,
                        charged_events=None, console_url="")

    monkeypatch.setattr(portfolio, "run_actor", fake)
    run = portfolio.fetch(cfg, area, 0, 1.0, None)
    assert len(runs) == 2 and runs[0] == {"useApifyProxy": True}
    assert {it["proxy"] for it in run.items} == {"datacenter", "residential"} and run.cost_usd == 0.02
    # pages the actor never returned: only their city is partial, never the whole run (r320, 2026-11-25)
    assert not run.truncated and portfolio.failed_cities(run.items) == ["Mountain View"]


# r320 (2026-11-25): one Irvine Company page for Stanford failed and all 8 cities were marked partial
def _pages():
    sv = {**UDR_PAGE, "city": "Palo Alto", "operator": "UDR"}
    stanford_ok = {**UDR_PAGE, "city": "Stanford", "operator": "UDR", "communities": [],
                   "url": "https://www.udr.com/x/stanford", "status": 404, "page_state": "empty"}
    stanford_failed = {"operator": "Irvine Company", "city": "Stanford", "requested": "https://irvine.example/stanford",
                       "url": "https://irvine.example/stanford", "status": None, "page_state": "failed"}
    return [sv, stanford_ok, stanford_failed]


def test_a_failed_page_makes_only_its_city_partial(monkeypatch, tmp_path):
    from aptfinder.intake import base
    from aptfinder.intake.apify import ActorRun
    from aptfinder.store import connect, init_db

    assert portfolio.failed_cities(_pages()) == ["Stanford"]
    monkeypatch.setattr(base, "account_remaining_usd", lambda: None)
    monkeypatch.setattr(base.settings, "RAW_DIR", tmp_path / "raw")
    fake = lambda *a, **kw: ActorRun(actor="x", run_id="r", status="SUCCEEDED", items=_pages(),  # noqa: E731
                                     cost_usd=0.01, charged_events=None, console_url="")
    monkeypatch.setattr(ADAPTERS["portfolio"], "fetch", fake)
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    areas = [Area(kind="city", id="Palo Alto", label=""), Area(kind="city", id="Stanford", label="")]
    res = base.run_source(conn, ADAPTERS["portfolio"], areas, rebuild_identity=False)
    assert {a: v["state"] for a, v in res.counts["areas"].items()} == {"Palo Alto": "complete", "Stanford": "partial"}


def test_reparse_covers_auto_recomputes_each_city_for_free(tmp_path):
    import json

    from aptfinder.intake.base import coverage, reparse
    from aptfinder.store import connect, init_db

    raw = tmp_path / "run320.json"
    raw.write_text(json.dumps({"actor": "apify/playwright-scraper", "areas": ["Palo Alto", "Stanford", "Los Altos"],
                               "items": _pages()}))
    conn = connect(tmp_path / "t.db")
    init_db(conn)
    reparse(conn, ADAPTERS["portfolio"], str(raw), covers="auto", rebuild_identity=False)
    cov = {k[1]: v["state"] for k, v in coverage(conn).items() if k[0] == "portfolio"}
    assert cov == {"Palo Alto": "complete", "Stanford": "partial", "Los Altos": "empty"}
