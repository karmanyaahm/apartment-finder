"""Site discovery: the trimmed claude invocation, result checks, URL cleaning, address check."""

from __future__ import annotations

import pytest

from aptfinder.intake import direct
from aptfinder.intake.discover import Blocker, check_result, claude_argv, clean_url, platform_of


def test_argv_is_trimmed_and_search_only():
    a = claude_argv("sonnet", 0.5)
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in a
    assert a[a.index("--tools") + 1] == "WebSearch" and a[a.index("--allowedTools") + 1] == "WebSearch"
    assert a[a.index("--setting-sources") + 1] == "" and "--bare" not in a


def ok_out(**kw):
    out = {"subtype": "success", "is_error": False, "permission_denials": [],
           "modelUsage": {"m": {"webSearchRequests": 2}},
           "structured_output": {"results": [{"key": "p1", "url": "https://x.com/", "note": ""}]}}
    out.update(kw)
    return out


def test_check_result_refuses_untrustworthy_calls():
    assert check_result(ok_out())[0]["key"] == "p1"
    for bad in (ok_out(permission_denials=[{"tool": "WebSearch"}]), ok_out(structured_output=None),
                ok_out(modelUsage={"m": {"webSearchRequests": 0}}), ok_out(subtype="error_max_budget_usd")):
        with pytest.raises(Blocker):
            check_result(bad)


def test_clean_url():
    assert clean_url("https://sevensmv.com/?utm_source=x") == ("https://sevensmv.com/", "")
    u, _ = clean_url("https://www.avaloncommunities.com/california/mountain-view-apartments/avalon-towers-on-the-"
                     "peninsula/book-a-tour/CA064")
    assert u == "https://www.avaloncommunities.com/california/mountain-view-apartments/avalon-towers-on-the-peninsula/"
    assert clean_url("https://www.apartments.com/x/abc/")[0] is None
    assert clean_url(None)[0] is None
    assert platform_of(u) == "avalon" and platform_of("https://sevensmv.com/") == "photos"


def test_address_on_page():
    page = {"text": "Welcome home. 2400 West El Camino Real, Mountain View", "scripts": []}
    assert direct.address_on_page(page, "2400 W El Camino Real, Mountain View, CA 94040") is True
    assert direct.address_on_page(page, "100 Moffett Blvd, Mountain View, CA 94043") is False
    assert direct.address_on_page({"scripts": []}, "2400 W El Camino Real") is None


def test_a_weaker_models_miss_does_not_block_a_stronger_search(conn, tmp_path, monkeypatch):
    import shutil

    from aptfinder import identity, settings
    from aptfinder.intake.discover import candidates
    from aptfinder.store import append_observation, start_run

    shutil.copytree(settings.ROOT / "config.example", tmp_path / "cfg")
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path / "cfg")
    run = start_run(conn, "intake", "apartments_com", {})
    append_observation(conn, run, "apartments_com", "h1", {
        "property_name": "Harken", "address": "100 View St, Mountain View, CA 94041", "lat": 37.3947, "lon": -122.0763})
    identity.run(conn)
    (tmp_path / "cfg" / "discovered_sites.toml").write_text(
        '[[miss]]\nplace_key = "%s"\nmodel = "haiku"\nreason = "no site found"\n'
        % conn.execute("SELECT place_key FROM places").fetchone()[0])
    assert [c["name"] for c in candidates(conn, "sonnet")] == ["Harken"]
    assert candidates(conn, "haiku") == []


def test_named_building_without_website_is_queued_and_your_url_is_promoted(conn, tmp_path, monkeypatch):
    import shutil

    from aptfinder import identity, settings, verify
    from aptfinder.intake.discover import promote
    from aptfinder.store import append_annotation, append_observation, start_run

    shutil.copytree(settings.ROOT / "config.example", tmp_path / "cfg")
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path / "cfg")
    run = start_run(conn, "intake", "apartments_com", {})
    append_observation(conn, run, "apartments_com", "h1", {
        "property_name": "Harken", "address": "100 View St, Mountain View, CA 94041", "lat": 37.3947, "lon": -122.0763})
    append_observation(conn, run, "zillow", "z1", {  # a house: an address as its name, not queued
        "property_name": "12 Elm St", "address": "12 Elm St, Mountain View, CA 94041", "lat": 37.395, "lon": -122.077})
    identity.run(conn)
    (item,) = verify.own_site_queue(conn)
    assert item["name"] == "Harken" and item["reason"] == "no_website" and item["urls"] == []
    append_annotation(conn, "user", "place", item["place_key"], "website", comment="https://harken.example/?utm=x")
    (item,) = verify.own_site_queue(conn)  # your URL shows at once, pending its check
    assert item["reason"] == "not_checked" and item["urls"][0]["status"] == "pending"
    assert "pending check" in item["urls"][0]["label"]
    (site,) = promote(conn, limit=10, dry_run=True)["sites"]
    assert site == ("Harken", "https://harken.example/?utm=x")
    # dismissed ("no plans online, fine"): gone until you add another URL
    append_annotation(conn, "user", "place", item["place_key"], "own_site", comment="dismissed: no plans online")
    assert verify.own_site_queue(conn) == []


def test_every_url_you_add_is_a_site_of_its_own(conn, tmp_path, monkeypatch):
    """A place can have several sources: a second URL never replaces the first."""
    import shutil

    from aptfinder import identity, settings, verify
    from aptfinder.config import load_sites
    from aptfinder.intake.discover import append, promote
    from aptfinder.store import append_annotation, append_observation, start_run

    shutil.copytree(settings.ROOT / "config.example", tmp_path / "cfg")
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path / "cfg")
    run = start_run(conn, "intake", "apartments_com", {})
    append_observation(conn, run, "apartments_com", "h1", {
        "property_name": "Harken", "address": "100 View St, Mountain View, CA 94041", "lat": 37.3947, "lon": -122.0763})
    identity.run(conn)
    pk = conn.execute("SELECT place_key FROM places").fetchone()[0]
    append("found", [{"id": "harken", "place_key": pk, "name": "Harken", "url": "https://harken.example/",
                      "found_by": "claude-sonnet"}])
    append("check", [{"id": "harken", "ok": False, "outcome": "blocked", "http_status": 403,
                      "reason": "page not fetched: blocked (HTTP 403)", "at": "2026-11-24T10:00:00+00:00"}])
    (item,) = verify.own_site_queue(conn)
    assert item["reason"] == "all_failed" and "HTTP 403" in item["urls"][0]["label"]
    for u in ("https://harken.example/", "https://op.example/harken/floorplans", "https://op.example/harken/units"):
        append_annotation(conn, "user", "place", pk, "website", comment=u)
    got = promote(conn, limit=10, dry_run=True)["sites"]
    assert got == [("Harken", "https://op.example/harken/floorplans"), ("Harken", "https://op.example/harken/units")]
    promote(conn, limit=10, fetch=False)
    assert sorted(s["url"] for s in load_sites().values() if s.get("place_key") == pk) == [
        "https://harken.example/", "https://op.example/harken/floorplans", "https://op.example/harken/units"]
    # a form is named as the reason, even beside a site that failed
    append("check", [{"id": "harken-2", "ok": False, "outcome": "rejected",
                      "reason": "landed on a contact or tour form: https://op.example/harken/guest-card/"}])
    (item,) = verify.own_site_queue(conn)
    assert item["reason"] == "form" and len(item["urls"]) == 3


FORMS = ('[[found]]\nid = "station-245"\nplace_key = "p_s"\nurl = "http://station245.prospectportal.com/su/6ks5m6"\n'
         'platform = "photos"\naddress = "245 Bush St, Mountain View, CA 94041"\n'
         '[[found]]\nid = "woodside-place"\nplace_key = "p_w"\nurl = "https://amc.touraptnow.com/rip8j3"\n'
         'platform = "photos"\naddress = "881 Woodside Pl, Mountain View, CA 94040"\n')


def test_a_listing_link_that_lands_on_a_lead_form_is_not_a_site(tmp_path, monkeypatch):
    """Station 245's and Woodside Place's Craigslist short links redirect to a guest card and an email-us form,
    which show the address: the address check passed, and the form was scraped as the building's site."""
    import json
    import shutil
    from types import SimpleNamespace

    from aptfinder import settings
    from aptfinder.config import load_sites
    from aptfinder.intake import base
    from aptfinder.intake.discover import fetch_and_check, is_form

    landed = {"station-245": "https://station245.prospectportal.com/mountain-view/station-245/guest-card/",
              "woodside-place": "https://amc.touraptnow.com/emailus.cfm?pid=1234"}
    assert all(is_form(u) for u in landed.values()) and not is_form("https://sevensmv.com/")
    # ok checks stored before this rule are read as rejected
    (tmp_path / "discovered_sites.toml").write_text(FORMS + "".join(
        f'[[check]]\nid = "{i}"\nok = true\nfinal_url = "{u.split("?")[0]}"\n' for i, u in landed.items()))
    assert load_sites(tmp_path) == {}
    # a new check records the rejection
    shutil.copytree(settings.ROOT / "config.example", tmp_path / "cfg")
    (tmp_path / "cfg" / "discovered_sites.toml").write_text(FORMS)
    (tmp_path / "raw" / "direct").mkdir(parents=True)
    (tmp_path / "raw" / "direct" / "run7_x.json").write_text(json.dumps({"items": [
        {"site": "station-245", "url": landed["station-245"], "text": "Station 245, 245 Bush Street", "scripts": []},
        {"site": "woodside-place", "url": landed["woodside-place"], "text": "881 Woodside Place", "scripts": []}]}))
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(base, "run_source", lambda conn, adapter, areas: SimpleNamespace(run_id=7, cost_usd=0.0))
    res = fetch_and_check(None, ["station-245", "woodside-place"])
    assert res["ok"] == [] and all(r.startswith("landed on a contact or tour form") for r in res["rejected"].values())
    assert set(res["rejected"]) == {"station-245", "woodside-place"}
    assert "station-245" not in load_sites() and "woodside-place" not in load_sites()


def test_browser_check_reads_the_rendered_page(tmp_path, monkeypatch, conn):
    """A site that refuses the cheap fetch is checked in the browser actor (residential proxy if asked); the
    rendered text is what the address check reads, and the run is not coverage."""
    import shutil

    from aptfinder import settings
    from aptfinder.intake import apify, discover
    from aptfinder.intake.apify import ActorRun
    from aptfinder.intake.base import coverage

    shutil.copytree(settings.ROOT / "config.example", tmp_path / "cfg")
    (tmp_path / "cfg" / "discovered_sites.toml").write_text(
        '[[found]]\nid = "station-245"\nplace_key = "p_x"\nname = "Station 245"\naddress = "245 Bush St, Mountain View"\n'
        'url = "https://station245.prospectportal.com/mountain-view/station-245/"\nplatform = "generic"\n'
        'found_by = "google_places listing"\n')
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
    seen = {}

    def fake(actor, inp, **kw):
        seen.update(inp)
        return ActorRun(actor=actor, run_id="r1", status="SUCCEEDED", cost_usd=0.01, charged_events=None,
                        console_url="", items=[{"site": "station-245", "requested": inp["startUrls"][0]["url"],
                                                "url": inp["startUrls"][0]["url"], "status": 200, "scripts": [],
                                                "text": "Station 245 Apartments 245 Bush Street Mountain View"}])

    monkeypatch.setattr(apify, "run_actor", fake)
    res = discover.fetch_and_check(conn, ["station-245"], residential=True)
    assert res["ok"] == ["station-245"]
    assert seen["proxyConfiguration"]["apifyProxyGroups"] == ["RESIDENTIAL"] and seen["maxRequestsPerCrawl"] == 1
    assert not [k for k in coverage(conn) if k[0] == "direct"]


def test_aggregator_hosts_are_never_own_sites():
    from aptfinder.intake.discover import is_aggregator

    for u in ("https://www.vrent.com/residential/533-bryant-street/02", "https://rental.turbotenant.com/p/x/1",
              "https://www.zillow.com/b/x/", "https://sfbay.craigslist.org/pen/apa/d/x.html",
              "https://www.rentcafe.com/apartments/ca/mountain-view/", "https://my.matterport.com/show/?m=abc"):
        assert is_aggregator(u) and clean_url(u)[0] is None, u
    for u in ("https://central-park-1.rentcafewebsite.com/", "https://www.greystar.com/x", "https://udr.com/x",
              "https://www.avaloncommunities.com/x", "https://www.essexapartmenthomes.com/x",
              "https://www.prometheusapartments.com/x", "https://www.equityapartments.com/x",
              "https://www.irvinecompanyapartments.com/x", "https://www.mvapartments.com/",
              "https://www.rentbalmoral.com/", "https://www.apt-rental.com/?n=x", None):
        assert not is_aggregator(u), u


def test_a_vrent_link_is_not_promoted_and_its_place_goes_back_to_no_website(conn, tmp_path, monkeypatch):
    """Craigslist posts link vrent.com single-listing pages; those were promoted, checked and rendered as the
    building's own site. They are dropped everywhere, and the building is searchable again."""
    import shutil

    from aptfinder import identity, settings, verify
    from aptfinder.config import load_sites
    from aptfinder.intake.discover import append, candidates, promote
    from aptfinder.store import append_annotation, append_observation, start_run

    shutil.copytree(settings.ROOT / "config.example", tmp_path / "cfg")
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path / "cfg")
    run = start_run(conn, "intake", "craigslist", {})
    vrent = "https://www.vrent.com/residential/100-view-street/02"
    append_observation(conn, run, "craigslist", "c1", {
        "property_name": "Harken", "address": "100 View St, Mountain View, CA 94041", "lat": 37.3947,
        "lon": -122.0763, "extra": {"website": vrent}})
    identity.run(conn)
    pk = conn.execute("SELECT place_key FROM places").fetchone()[0]
    assert promote(conn, limit=10, dry_run=True)["sites"] == []
    append_annotation(conn, "user", "place", pk, "website", comment="https://rental.turbotenant.com/p/harken/1")
    assert promote(conn, limit=10, dry_run=True)["sites"] == []
    # one promoted earlier and scraped (a priced row from the vrent page): dropped from the site list
    append("found", [{"id": "harken", "place_key": pk, "name": "Harken", "url": vrent, "platform": "photos",
                      "found_by": "craigslist listing"}])
    append_observation(conn, run, "direct", "harken:r:02", {
        "address": "100 View St, Mountain View, CA 94041", "lat": 37.3947, "lon": -122.0763}, price=3000, url=vrent)
    identity.run(conn)
    assert "harken" not in load_sites()
    assert conn.execute("SELECT place_key FROM listings WHERE source = 'direct'").fetchone()[0] == pk
    (item,) = verify.own_site_queue(conn)
    assert item["reason"] == "no_website" and item["urls"] == []
    assert [c["name"] for c in candidates(conn, "sonnet")] == ["Harken"]
    # a real link from another listing is still promoted
    append_observation(conn, run, "craigslist", "c2", {
        "property_name": "Harken", "address": "100 View St, Mountain View, CA 94041", "lat": 37.3947,
        "lon": -122.0763, "extra": {"website": "https://harken.example/"}})
    identity.run(conn)
    assert promote(conn, limit=10, dry_run=True)["sites"] == [("Harken", "https://harken.example/")]


def test_load_sites_drops_aggregator_urls(tmp_path):
    from aptfinder.config import load_sites

    (tmp_path / "direct_sites.toml").write_text(
        '[[site]]\nid = "z"\nurl = "https://www.zillow.com/b/x/"\nplatform = "photos"\n'
        '[[site]]\nid = "sevens"\nurl = "https://sevensmv.com/"\nplatform = "photos"\n')
    (tmp_path / "discovered_sites.toml").write_text(
        '[[found]]\nid = "v"\nplace_key = "p_v"\nurl = "https://www.vrent.com/residential/x/1"\nplatform = "photos"\n'
        '[[found]]\nid = "r"\nplace_key = "p_r"\nurl = "https://short.example/abc"\nplatform = "photos"\n'
        '[[found]]\nid = "ok"\nplace_key = "p_ok"\nurl = "https://ok.example/"\nplatform = "photos"\n'
        '[[check]]\nid = "r"\nok = true\nfinal_url = "https://rental.turbotenant.com/p/x/1"\n')
    assert sorted(load_sites(tmp_path)) == ["ok", "sevens"]


def test_clean_url_joins_a_host_written_with_spaces():
    # listing text hides a link as "www heritagerealtysanmateo com"; extract reads it verbatim
    assert clean_url("https://www heritagerealtysanmateo com/")[0] == "https://www.heritagerealtysanmateo.com/"
    assert clean_url("https://WWW CMPMI COM/", keep_query=True)[0] == "https://www.cmpmi.com/"
    # anything else with a space (or no host) is not a URL
    for bad in ("https://Stayluxlife dot com/", "https://sfcityrents . com/", "http gofivestarpm com rentals",
                "https://www srecompany com availability/", "https my matterport com show ?m=x"):
        assert clean_url(bad)[0] is None, bad
    assert clean_url("https://sevensmv.com/")[0] == "https://sevensmv.com/"


def test_load_sites_takes_the_latest_found_row_of_an_id(tmp_path):
    from aptfinder.config import load_sites

    (tmp_path / "discovered_sites.toml").write_text(
        '[[found]]\nid = "g2"\nplace_key = "p_g2"\nurl = "https://www heritage com/"\nplatform = "photos"\n'
        '[[found]]\nid = "ok"\nplace_key = "p_ok"\nurl = "https://ok.example/"\nplatform = "photos"\n'
        '[[found]]\nid = "g2"\nplace_key = "p_g2"\nurl = "https://www.heritage.com/"\nplatform = "photos"\n')
    sites = load_sites(tmp_path)
    assert list(sites) == ["g2", "ok"] and sites["g2"]["url"] == "https://www.heritage.com/"
