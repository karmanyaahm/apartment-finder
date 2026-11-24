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
