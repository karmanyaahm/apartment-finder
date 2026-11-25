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
