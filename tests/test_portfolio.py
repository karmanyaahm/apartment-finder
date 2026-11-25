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
