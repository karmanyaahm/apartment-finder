from aptfinder.intake.render import check_plans

PAGE = ("Park Place | 851 Church Street, Mountain View, CA 94041 | Floor Plans\nNorth 2CR\n2 Beds - 2 Baths\n"
        "965 sq. ft.\n** Utilities Included\nStarting at $6,145\nAvailable Now\nSouth 1AR\n1 Bed - 1 Bath\n745 sq. ft.\n"
        "Starting at $4,500\nAvailable Dec 6\nThe Dean | Starting at $4,073 | Beds : Studio - 3")


def test_plans_are_kept_only_with_what_the_page_states():
    got = check_plans([
        {"name": "North 2CR", "beds": 2, "baths": 2, "sqft": 965, "price": 6145, "available": "Available Now"},
        {"name": "South 1AR", "beds": 1, "baths": 1, "sqft": 745, "price": 4600, "available": "Available Dec 6"},
        {"name": "North 3A", "beds": 3, "baths": 2, "sqft": 1100, "price": 7000},  # not on the page
    ], PAGE)
    assert [p["name"] for p in got] == ["North 2CR", "South 1AR"]
    assert got[0] == {"name": "North 2CR", "beds": 2.0, "baths": 2.0, "sqft": 965.0, "price": 6145.0,
                      "available": "Available Now"}
    assert "price" not in got[1] and got[1]["sqft"] == 745.0  # $4,600 is not on the page: dropped


# ---------------------------------------------------------------- outcomes, failure vs not (classification)
from aptfinder.intake.apify import classify_failure, failed_requests, log_errors, same_url  # noqa: E402
from aptfinder.intake.render import (MAX_UNIT_PAGES, PAGE_FUNCTION, last_outcomes, select, site_outcome,  # noqa: E402
                                     site_text)

ERROR_ITEM = {"#error": True, "#debug": {"requestId": "x", "url": "https://blocked.example/", "method": "GET",
                                         "retryCount": 2, "statusCode": 403,
                                         "errorMessages": ["Request blocked - received 403 status code.\n  at ..."]}}


def test_failure_classification():
    assert classify_failure(403, []) == "blocked" and classify_failure(429, []) == "blocked"
    assert classify_failure(None, ["Request blocked - received 403 status code."]) == "blocked"
    assert classify_failure(None, ["page.goto: Timeout 120000ms exceeded."]) == "timeout"
    assert classify_failure(None, ["pageFunction timed out after 240 seconds"]) == "timeout"
    assert classify_failure(None, ["page.goto: net::ERR_TIMED_OUT at https://sevensmv.com/"]) == "timeout"  # real, 11-24
    assert classify_failure(None, ["net::ERR_NAME_NOT_RESOLVED"]) == "error"
    assert failed_requests([ERROR_ITEM, {"site": "a", "text": "x"}]) == [
        {"url": "https://blocked.example/", "status": 403, "messages": ["Request blocked - received 403 status code."]}]
    assert same_url("http://www.Sevensmv.com/", "https://sevensmv.com") and not same_url("https://a.com/x", "https://a.com/y")


def test_log_lines_name_the_failure_of_a_url():
    log = ("2026-11-24T10:00:01.000Z INFO  PlaywrightCrawler: Starting the crawler.\n"
           "2026-11-24T10:00:09.000Z WARN  PlaywrightCrawler: Reclaiming failed request back to the list or queue. "
           "Request blocked - received 429 status code. {\"id\":\"a\",\"url\":\"https://essex.example/montclaire\","
           "\"retryCount\":1}\n"
           "2026-11-24T10:00:30.000Z ERROR PlaywrightCrawler: Request failed and reached maximum retries. "
           "page.goto: Timeout 120000ms exceeded. {\"id\":\"b\",\"url\":\"https://slow.example/\",\"retryCount\":2}\n")
    status, msgs = log_errors(log, "https://essex.example/montclaire/")
    assert status == 429 and len(msgs) == 1 and classify_failure(status, msgs) == "blocked"
    status, msgs = log_errors(log, "https://slow.example")
    assert status is None and classify_failure(status, msgs) == "timeout"
    assert log_errors(log, "https://fine.example/") == (None, [])


def test_site_outcome_failure_vs_not():
    text = "Plan A1 1 bed 700 sq ft $3,000 " * 20
    ok = site_outcome("https://a.example/", [{"site": "a", "kind": "start", "status": 200, "text": text}], [], None)
    assert ok["outcome"] == "read"
    empty = site_outcome("https://a.example/", [{"site": "a", "kind": "start", "status": 200, "text": "Loading"}],
                         [], None)
    assert empty["outcome"] == "empty" and empty["error"] == "7 characters of text"
    blocked = site_outcome("https://blocked.example", [], failed_requests([ERROR_ITEM]), None)
    assert blocked == {"outcome": "blocked", "http_status": 403, "error": "Request blocked - received 403 status code."}
    log = "ERROR PlaywrightCrawler: Request failed. page.goto: Timeout 120000ms exceeded. {\"url\":\"https://t.example/\"}"
    assert site_outcome("https://t.example/", [], [], log)["outcome"] == "timeout"
    assert site_outcome("https://n.example/", [], [], "")["outcome"] == "not_returned"


def test_site_text_puts_unit_pages_in_with_their_own_budget():
    pages = [{"kind": "units", "url": f"https://a.example/plan/{i}", "text": f"Unit {i}0{i} $4,{i}00"} for i in range(7)]
    pages += [{"kind": "start", "url": "https://a.example/", "text": "S" * 50000},
              {"kind": "floorplans", "url": "https://a.example/floorplans", "text": "Plan B2"}]
    t = site_text(pages)
    assert t.index("[page https://a.example/]") < t.index("floorplans") < t.index("/plan/0")
    assert t.count("[page ") == 2 + MAX_UNIT_PAGES and "Unit 404 $4,400" in t and "/plan/6" not in t
    assert "S" * 12001 not in t  # the start page is cut to its budget


def test_page_function_follows_plan_unit_links_within_scope_and_never_actions():
    assert "const MAX_UNITS = 5;" in PAGE_FUNCTION and "__MAX_UNIT_PAGES__" not in PAGE_FUNCTION
    assert "kind: 'units', scope" in PAGE_FUNCTION and "NEVER.test(h.slice(scope.length))" in PAGE_FUNCTION
    import re
    units = re.compile(r"^(view|see|show)\s+(available\s+)?(apartments|units|homes)\b|^available\s+(units|apartments)\b"
                       r"|^view\s+availability$", re.I)
    assert units.pattern.replace("\\\\", "\\") in PAGE_FUNCTION.replace("\\\\", "\\")
    for t in ("VIEW APARTMENTS", "View units", "Available units", "View available units"):
        assert units.search(t), t
    for t in ("Apply now", "Schedule a tour", "View all floor plans"):
        assert not units.search(t), t


def _render_run(conn, areas, started="2026-11-24T10:00:00+00:00"):
    import json

    conn.execute("INSERT INTO runs (started_at, finished_at, component, source, params, counts, status) "
                 "VALUES (?, ?, 'intake', 'direct', ?, ?, 'ok')",
                 (started, started, json.dumps({"render": True, "areas": list(areas)}), json.dumps({"areas": areas})))


def test_retry_selection(conn):
    _render_run(conn, {"old#render": {"state": "complete", "items": 3}, "b#render": {"state": "failed"}},
                "2026-11-01T10:00:00+00:00")
    _render_run(conn, {"b#render": {"state": "failed", "outcome": "blocked", "http_status": 403},
                       "ok#render": {"state": "complete", "outcome": "ok", "items": 4},
                       "np#render": {"state": "empty", "outcome": "no_plans"},
                       "t#render": {"state": "failed", "outcome": "timeout"}},
                "2099-01-01T00:00:00+00:00")
    last = last_outcomes(conn)
    assert last["old"]["outcome"] == "ok" and last["b"]["outcome"] == "blocked" and last["b"]["http_status"] == 403
    got = select(["ok", "never", "old", "np", "b", "t"], last)
    assert [s for s, _ in got] == ["b", "t", "never", "old"]  # failures first, then never, then stale
    assert got[0][1].startswith("retry: blocked (HTTP 403)") and "will retry" in got[0][1]
    assert [s for s, _ in select(["ok", "never", "b", "t"], last, retry_failed=True)] == ["b", "t"]


def test_a_failed_render_retires_nothing(conn):
    """Only ok (state complete) and no_plans (state empty) prove a plan is gone; a failure proves nothing."""
    from aptfinder.store import append_observation, finish_run, start_run

    r1 = start_run(conn, "intake", "direct", {"render": True, "areas": ["s#render"], "scope": "partial"})
    append_observation(conn, r1, "direct", "s:r:a1", {"beds": 1, "extra": {"area": "s#render", "platform": "render"}},
                       price=3000, price_basis="month")
    finish_run(conn, r1, "ok", {"areas": {"s#render": {"state": "complete", "outcome": "ok", "items": 1}}})
    r2 = start_run(conn, "intake", "direct", {"render": True, "areas": ["s#render"], "scope": "partial"})
    finish_run(conn, r2, "ok", {"areas": {"s#render": {"state": "failed", "outcome": "blocked", "items": 0}}})
    assert conn.execute("SELECT active FROM listings").fetchone()[0] == 1
    r3 = start_run(conn, "intake", "direct", {"render": True, "areas": ["s#render"], "scope": "partial"})
    finish_run(conn, r3, "ok", {"areas": {"s#render": {"state": "empty", "outcome": "no_plans", "items": 0}}})
    assert conn.execute("SELECT active FROM listings").fetchone()[0] == 0


# ---------------------------------------------------------------- discovered sites: not fetched is not rejected
def test_a_page_not_fetched_is_retried_only_a_missing_address_rejects(tmp_path, monkeypatch):
    from aptfinder import settings
    from aptfinder.config import check_outcome, load_sites
    from aptfinder.intake.discover import retryable, unfetched

    d = tmp_path / "cfg"
    d.mkdir()
    found = "".join(f'[[found]]\nid = "{i}"\nurl = "https://{i}.example/"\nplace_key = "p_{i}"\n\n'
                    for i in ("old403", "blocked", "noaddr", "again"))
    checks = ('[[check]]\nid = "old403"\nok = false\nreason = "page not fetched"\n\n'  # written before outcomes
              '[[check]]\nid = "blocked"\nok = false\noutcome = "blocked"\nhttp_status = 403\n'
              'reason = "page not fetched: blocked (HTTP 403)"\n\n'
              '[[check]]\nid = "noaddr"\nok = false\nreason = "page does not show the street address"\n\n'
              '[[check]]\nid = "again"\nok = false\nreason = "page not fetched"\n\n'
              '[[check]]\nid = "again"\nok = true\noutcome = "ok"\nreason = "address shown; rows: 2"\n')
    (d / "discovered_sites.toml").write_text(found + checks)
    monkeypatch.setattr(settings, "CONFIG_DIR", d)
    assert set(load_sites()) == {"old403", "blocked", "again"}  # only the missing address drops a site
    assert check_outcome({"ok": False, "reason": "page has no text"}) == "empty"
    assert retryable() == ["old403", "blocked"]  # the latest check of 'again' is ok
    assert unfetched("https://x.example/", "")["outcome"] == "not_fetched"
    log = 'WARN CheerioCrawler: Request blocked - received 403 status code. {"url":"https://x.example/"}'
    assert unfetched("https://x.example", log) | {"error": None} == {
        "outcome": "blocked", "http_status": 403, "error": None, "reason": "page not fetched: blocked (HTTP 403)"}


def test_render_plans_get_a_drawing_only_from_the_page_images():
    from aptfinder.intake.render import page_drawings
    from aptfinder.plans import match_drawing

    pages = [{"url": "https://www.sofiaaptliving.com/", "images": [
        {"src": "https://cdn.prod.website-files.com/6a91/6b04_imgi_7_clara-770(2).png", "alt": "1x1-770",
         "cls": "floor-plan-image"},
        {"src": "https://cdn.prod.website-files.com/6a91/6b04_imgi_6_daniela(4).png", "alt": "1x1-763",
         "cls": "floor-plan-image"},
        {"src": "https://cdn.prod.website-files.com/6a91/kitchen.jpg", "alt": "1x1-770 kitchen", "cls": "gallery"}]},
        {"url": "https://www.sofiaaptliving.com/floorplans", "text": "old raw file: no images"}]
    d = page_drawings(pages)
    assert match_drawing("1x1-770", d) == "https://cdn.prod.website-files.com/6a91/6b04_imgi_7_clara-770(2).png"
    assert "https://cdn.prod.website-files.com/6a91/kitchen.jpg" not in d.values()  # a room photo, not a drawing
    assert "images" in PAGE_FUNCTION and "alt" in PAGE_FUNCTION
