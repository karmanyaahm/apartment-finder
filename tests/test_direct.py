"""Direct-site parsers on hand-made fixtures shaped like the 2026-11-24 pages."""

from __future__ import annotations

import json

import pytest

from aptfinder.intake import direct
from aptfinder.store.keys import place_key

SITES = {
    "avalon-mountain-view": {"id": "avalon-mountain-view", "platform": "avalon", "url": "https://a.example/av/"},
    "landsby": {"id": "landsby", "platform": "jd_floorplans", "url": "https://l.example/floorplans/",
                "name": "Landsby", "address": "100 Aspen Way, Mountain View, CA 94040"},
    "the-dean": {"id": "the-dean", "platform": "prometheus", "url": "https://p.example/the-dean"},
    "sevens": {"id": "sevens", "platform": "photos", "url": "https://s.example/", "name": "Sevens",
               "address": "777 W Middlefield Rd, Mountain View, CA 94043"},
}


@pytest.fixture(autouse=True)
def fake_sites(monkeypatch):
    monkeypatch.setattr(direct, "sites", lambda: SITES)


def page(url, scripts):
    return {"url": url, "requested": url, "scripts": scripts}


def test_avalon_units_match_benchmark_shape():
    g = {"name": "Avalon Mountain View", "address": {"addressLine1": "1600 Villa Street", "city": "Mountain View",
                                                    "state": "CA", "zip": "94041"},
         "coordinates": {"latitude": 37.39, "longitude": -122.08},
         "units": [{"unitName": "174", "bedroomNumber": 3, "bathroomNumber": 2, "squareFeet": 1056,
                    "floorPlan": {"name": "C1", "lowResolution": "/floorplans/ca049/ca049-c1.png/128/96",
                                  "highResolution": "/floorplans/ca049/ca049-c1.png/1024/768"},
                    "availableDateUnfurnished": "2026-11-25T05:00:00+00:00",
                    "startingAtPricesUnfurnished": {"leaseTerm": 15, "prices": {"price": 5320, "totalPrice": 5480}},
                    "availableDateFurnished": "2026-11-25T05:00:00+00:00",
                    "startingAtPricesFurnished": {"leaseTerm": 15, "prices": {"price": 6570}}}]}
    item = page("https://a.example/av/", [{"type": "application/javascript", "id": "fusion-metadata",
                                           "text": "window.Fusion=window.Fusion||{};Fusion.globalContent="
                                                   + json.dumps(g) + ";Fusion.x=1;"}])
    p, pf = direct.parse(item, {})
    assert p.source_id == "avalon-mountain-view:u:174" and p.price == 5320 and p.price_basis == "month"
    # AvalonBay's own furnished option on the same unit is a separate offer
    assert pf.source_id == "avalon-mountain-view:uf:174" and pf.price == 6570 and pf.data.furnished is True
    assert pf.data.term == "long" and pf.data.extra["offer"] == "Furnished+"
    assert (p.data.beds, p.data.baths, p.data.sqft, p.data.floor_plan) == (3, 2, 1056, "C1")
    assert p.data.available_from == "2026-11-25"
    # the unit's drawing, on AvalonBay's media host (as the page's own <img alt="Floorplan"> shows it)
    assert p.data.extra["floorplan_image"] == pf.data.extra["floorplan_image"] == \
        "https://resource.avalonbay.com/floorplans/ca049/ca049-c1.png/1024/768"
    assert place_key(p.data.address, p.data.city, None, None) == place_key("1600 Villa St, Mountain View, CA 94041",
                                                                           None, None, None)
    assert direct.area_of(item, ["avalon-mountain-view"]) == "avalon-mountain-view"


def test_jd_floorplans_uses_config_address_when_page_has_none():
    d = {"floorplans": [{"id": 1, "title": "3A.1", "slug": "3a-1", "bedrooms": "3", "bathrooms": "2",
                         "square_feet": "1248", "rent_min": "", "rent_max": "", "availability_count": 0,
                         "available_date": False}]}
    item = page("https://l.example/floorplans/", [{"type": "application/json", "id": "jd-fp-data-script-app",
                                                    "text": json.dumps(d)}])
    (p,) = direct.parse(item, {})
    assert p.price is None and p.data.available_from is None  # rent loads via JS: unknown, not zero
    assert (p.data.beds, p.data.baths, p.data.sqft) == (3, 2, 1248)
    assert p.data.address == "100 Aspen Way, Mountain View, CA 94040"


def test_prometheus_page_gives_the_building_not_plans_from_drawings():
    apd = {"name": "The Dean", "fullAddress": "458 San Antonio Rd, Mountain View, CA 94040", "city": "Mountain View",
           "latLong": {"lat": 37.40, "lon": -122.11},
           "floorplanSvg": {"Plan 3A": "https://x/2023_DEANO_1196sqft_32CA_3B2BR_3A.svg",
                            "Plan A": "https://x/2023_DEANO_519sqft_STC_Studio_A.svg"},
           "floorplanPng": {"Plan 3A": "https://x/3A-2x.png"},
           "webContents": {"fields": {"galleryImages": [{"src": "https://img/1.jpg"}]}}}
    nd = {"props": {"pageProps": {"apartmentPageData": apd}}}
    item = page("https://p.example/the-dean", [{"type": "application/json", "id": "__NEXT_DATA__",
                                                "text": json.dumps(nd)}])
    (p,) = direct.parse(item, {})  # one row for the building: a drawing is not an offered plan
    assert p.data.kind == "site_info" and p.data.beds is None and p.price is None
    assert p.data.address == "458 San Antonio Rd, Mountain View, CA 94040" and p.data.extra["images"] == ["https://img/1.jpg"]
    # the drawings by plan name, for the plans a render reads from this site (never plans of their own)
    assert p.data.extra["plan_drawings"] == {"Plan 3A": "https://x/2023_DEANO_1196sqft_32CA_3B2BR_3A.svg",
                                             "Plan A": "https://x/2023_DEANO_519sqft_STC_Studio_A.svg"}


def test_unknown_page_is_unparsed():
    assert direct.parse(page("https://other.example/", []), {}) is None


def test_photos_site_keeps_photos_drops_logos_and_is_not_an_offer(conn):
    from aptfinder.filter import evaluate_all
    from aptfinder.store import append_observation, start_run

    imgs = [{"src": "https://cdn.example/og.jpg", "from": "meta"},
            {"src": "/img/logo.png", "alt": "Sevens logo", "from": "img"},
            {"src": "/img/small.jpg", "srcset": "/img/a-800.jpg 800w, /img/a-1600.jpg 1600w", "from": "img"},
            {"src": "/img/thumb.jpg", "w": "120", "from": "img"},
            {"src": "data:image/gif;base64,xx", "from": "img"},
            {"src": "/img/pool.webp", "cls": "gallery-item", "from": "style"},
            {"src": "https://cdn.example/w_598/v1/pool.webp", "from": "img"},
            {"src": "https://cdn.example/c_fill,h_80,w_80/v1/tiny.jpg", "from": "img"},
            {"src": "https://cdn.example/q_auto,f_auto/v2/front.jpg", "srcset": "w_998/v2/front.jpg", "from": "img"}]
    item = {**page("https://s.example/", []), "images": imgs}
    (p,) = direct.parse(item, {})
    assert p.data.extra["images"] == ["https://cdn.example/og.jpg", "https://s.example/img/a-1600.jpg",
                                      "https://s.example/img/pool.webp", "https://cdn.example/q_auto,f_auto/v2/front.jpg"]
    assert p.data.kind == "site_info" and p.price is None and p.source_id == "sevens:site"
    run = start_run(conn, "intake", "direct")
    append_observation(conn, run, "direct", p.source_id, p.data, url=p.url)
    res = evaluate_all(conn)
    assert all(not res["listings"][g] for g in res["listings"])  # no goal looks at it


# trimmed real image candidates (run 158 and run 83, 2026-11-24)
SOFIA = [{"src": "https://cdn.prod.website-files.com/6a91/6b04_imgi_19_alicia.png", "alt": "2x2-1044",
          "cls": "floor-plan-image", "from": "img"},
         {"src": "https://cdn.prod.website-files.com/6a91/6b04_imgi_7_clara-770(2).png", "alt": "1x1-770",
          "cls": "floor-plan-image", "from": "img"},
         {"src": "https://cdn.prod.website-files.com/6a91/6b04_imgi_6_daniela(4).png", "alt": "1x1-763",
          "cls": "floor-plan-image", "from": "img"}]
SELO = [{"src": "https://www.deanzaproperties.com/wp-content/smush-webp/2021/11/selo-UNIT-G-floor-plan-300x249.png.webp",
         "alt": "selo UNIT G floor plan", "cls": "lazyload", "from": "img"},
        {"src": "https://www.deanzaproperties.com/wp-content/smush-webp/2021/11/selo-UNIT-G-floor-plan-609x506.png.webp",
         "cls": "no_lazy", "from": "img"}]
NOT_DRAWINGS = [  # a room photo captioned with the amenity, a missing drawing, decorations
    {"src": "https://d1uty3jitgnf7i.cloudfront.net/wp-content/uploads/2022/01/2ModelHomes_P15-_TheMarston-600x400.jpg",
     "alt": "Open Floor Plan at The Marston by Windsor, 825 Marshall Street, CA", "cls": "object-cover", "from": "img"},
    {"src": "https://assets.rentsync.com/vrent/images/floorplans/1794976395614_400_2_Bed_1_Bath_Type_A_FLOOR_PLAN_COMING_SOON.JPG",
     "alt": "696 Towle Way", "cls": "information__image", "from": "img"},
    {"src": "https://cdn.prod.website-files.com/6a26/6a26_element-placeholder.svg", "alt": "Placeholder icon",
     "cls": "floorplan-element", "from": "img"},
    {"src": "https://cdn.prod.website-files.com/6a26/6a27_Arbora-models-18_k8skmg.avif", "cls": "floorplans-banner",
     "from": "style"},
    {"src": "https://cdn.example/pool.jpg", "alt": "Pool", "from": "img"}]


def test_photos_site_keeps_floor_plan_drawings_by_label():
    from aptfinder.plans import match_drawing

    item = {**page("https://s.example/", []), "images": SOFIA + SELO + NOT_DRAWINGS}
    (p,) = direct.parse(item, {})
    d = p.data.extra["plan_drawings"]
    assert sorted(d.values()) == sorted([i["src"] for i in SOFIA] + [SELO[1]["src"]])  # the larger copy
    assert "selo UNIT G floor plan selo-UNIT-G-floor-plan.png.webp" in d  # the small copy's alt kept
    # a plan gets a drawing only when exactly one label names it
    assert match_drawing("1x1-770", d) == SOFIA[1]["src"]
    assert match_drawing("UNIT G", d) == SELO[1]["src"]
    assert match_drawing("1x1", d) is None  # two drawings: 1x1-770 and 1x1-763
    assert match_drawing("Studio", d) is None and match_drawing("A", d) is None
    # nothing that is not a drawing: no plan_drawings at all
    (q,) = direct.parse({**page("https://s.example/", []), "images": NOT_DRAWINGS}, {})
    assert "plan_drawings" not in q.data.extra
