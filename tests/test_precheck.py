"""The free own-site precheck: a dead domain or a Cloudflare challenge is recorded and never sent to Apify."""

import json
import socket

import httpx
import pytest

from aptfinder.intake import precheck as pc


def _dns_ok(host, port, **kw):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port))]


def _dns_nx(host, port, **kw):
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


def _client(status=200, headers=None, body="<html><title>Sevens</title>245 Bush Street</html>"):
    return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, headers=headers or {},
                                                                                  text=body)))


@pytest.mark.real_precheck
def test_dns_failure_is_dead_and_nothing_is_fetched():
    def boom(req):
        raise AssertionError("no GET for a dead domain")

    r = pc.precheck("https://www.velocityls.com/", getaddrinfo=_dns_nx,
                    client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert r["outcome"] == "dead" and "www.velocityls.com" in r["error"]


@pytest.mark.real_precheck
def test_a_temporary_dns_failure_proves_nothing():
    def again(host, port, **kw):
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    assert pc.precheck("https://x.example/", getaddrinfo=again, client=_client())["outcome"] == "proceed"


@pytest.mark.real_precheck
def test_cloudflare_challenge_page():
    page = "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>cf_chl_opt</body></html>"
    r = pc.precheck("https://barkley.example/", getaddrinfo=_dns_ok,
                    client=_client(403, {"server": "cloudflare"}, page))
    assert r["outcome"] == "challenge" and r["http_status"] == 403
    r = pc.precheck("https://b.example/", getaddrinfo=_dns_ok, client=_client(403, {"cf-mitigated": "challenge"}, ""))
    assert r["outcome"] == "challenge"


@pytest.mark.real_precheck
def test_a_malformed_host_is_not_dead():
    r = pc.precheck("https://www heritagerealtysanmateo com/", getaddrinfo=_dns_nx, client=_client())
    assert r["outcome"] == "proceed" and "malformed host" in r["error"]


@pytest.mark.real_precheck
def test_a_normal_site_or_a_bare_403_proceeds():
    assert pc.precheck("https://sevensmv.com/", getaddrinfo=_dns_ok, client=_client())["outcome"] == "proceed"
    # Cloudflare's bot script on a normal 200 page is not a challenge; a plain 403 may still pass in a browser
    ok = _client(200, {"server": "cloudflare"}, '<script src="/cdn-cgi/challenge-platform/scripts/x.js"></script>')
    assert pc.precheck("https://a.example/", getaddrinfo=_dns_ok, client=ok)["outcome"] == "proceed"
    assert pc.precheck("https://a.example/", getaddrinfo=_dns_ok,
                       client=_client(403, {"server": "nginx"}, "Forbidden"))["outcome"] == "proceed"


def _stub(monkeypatch, outcomes):
    monkeypatch.setattr(pc, "precheck", lambda url, **kw: {"outcome": outcomes.get(url, "proceed"),
                                                           "http_status": None, "error": "DNS: stub"})


def test_render_skips_dead_sites_and_retry_failed_leaves_them(conn, monkeypatch, tmp_path):
    """A dead domain is recorded and not sent to Apify; a Cloudflare challenge to the plain GET is not a reason to
    skip (Apify's browser got through on every such site)."""
    from aptfinder.config import load_sites
    from aptfinder.intake import render
    from aptfinder.intake.apify import ActorRun

    sites = load_sites()
    a, b = [sid for sid, s in sites.items() if s.get("platform") not in render.SKIP_PLATFORMS][:2]
    _stub(monkeypatch, {sites[a]["url"]: "dead", sites[b]["url"]: "challenge"})
    sent = []

    def fake(actor, inp, **kw):
        sent.extend(u["userData"]["site"] for u in inp["startUrls"])
        return ActorRun(actor=actor, run_id="r1", status="SUCCEEDED", cost_usd=0.0, charged_events=None,
                        console_url="", items=[], log="")

    monkeypatch.setattr(render, "run_actor", fake)
    monkeypatch.setattr(render.settings, "RAW_DIR", tmp_path)
    monkeypatch.setattr(render.identity, "run", lambda conn: None)
    res = render.render(conn, sites=[a, b])
    assert sent == [b] and res["outcomes"][a]["outcome"] == "dead"
    last = render.last_outcomes(conn)
    assert last[a]["outcome"] == "dead" and last[b]["outcome"] == "not_returned"
    assert "site dead (DNS)" in render.describe(last[a]) and last[a]["error"] == "DNS: stub"
    # --retry-failed never picks it; a full run does not either until it is 7 days old
    assert render.select([a], last, retry_failed=True) == []
    assert render.select([a], last) == []
    old = {k: {**v, "at": "2020-01-01T00:00:00+00:00"} for k, v in last.items()}
    assert [s for s, _ in render.select([a], old)] == [a]  # rechecked after a week (the precheck again)
    run = json.loads(conn.execute("SELECT counts FROM runs ORDER BY id LIMIT 1").fetchone()[0])
    assert run["areas"][f"{a}#render"]["state"] == "failed"  # retires nothing


def test_promote_retry_skips_precheck_failures_and_rechecks_weekly(tmp_path, monkeypatch):
    import shutil

    from aptfinder import settings
    from aptfinder.config import load_sites
    from aptfinder.intake.discover import retryable

    shutil.copytree(settings.ROOT / "config.example", tmp_path / "cfg")
    found = "".join(f'[[found]]\nid = "{i}"\nplace_key = "p_{i}"\nname = "{i}"\naddress = "1 Main St"\n'
                    f'url = "https://{i}.example/"\nplatform = "generic"\nfound_by = "x"\n\n' for i in "abc")
    (tmp_path / "cfg" / "discovered_sites.toml").write_text(
        found + '[[check]]\nid = "a"\nok = false\noutcome = "dead"\nat = "2099-01-01T00:00:00+00:00"\n\n'
        '[[check]]\nid = "b"\nok = false\noutcome = "challenge"\nat = "2020-01-01T00:00:00+00:00"\n\n'
        '[[check]]\nid = "c"\nok = false\noutcome = "blocked"\nat = "2099-01-01T00:00:00+00:00"\n')
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path / "cfg")
    assert retryable() == ["c"]  # --retry-failed
    assert retryable(recheck=True) == ["c"]  # a plain promote: the dead one is not a week old
    (tmp_path / "cfg" / "discovered_sites.toml").write_text(
        (tmp_path / "cfg" / "discovered_sites.toml").read_text().replace("2099", "2020", 1))
    assert retryable(recheck=True) == ["a", "c"]
    assert {"a", "b", "c"} <= set(load_sites())  # a dead site is not rejected: it can come back


def test_fetch_and_check_records_precheck_and_skips_apify(tmp_path, monkeypatch):
    import shutil

    from aptfinder import settings
    from aptfinder.config import check_outcome, last_checks
    from aptfinder.intake import base
    from aptfinder.intake.discover import _records, fetch_and_check

    shutil.copytree(settings.ROOT / "config.example", tmp_path / "cfg")
    (tmp_path / "cfg" / "discovered_sites.toml").write_text(
        '[[found]]\nid = "gone"\nplace_key = "p_g"\nname = "Gone"\naddress = "1 Main St"\n'
        'url = "https://gone.example/"\nplatform = "generic"\nfound_by = "x"\n')
    monkeypatch.setattr(settings, "CONFIG_DIR", tmp_path / "cfg")
    _stub(monkeypatch, {"https://gone.example/": "dead"})
    monkeypatch.setattr(base, "run_source", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Apify")))
    res = fetch_and_check(None, ["gone"])
    assert res["run_id"] is None and "gone" in res["precheck"]
    c = last_checks(_records())["gone"]
    assert check_outcome(c) == "dead" and c["error"] == "DNS: stub"


def test_own_site_list_reason_and_label(conn, monkeypatch):
    from aptfinder import verify

    assert verify.OWN_SITE_REASONS["site_dead"] == "site dead (DNS)"
    renders = {"s": {"outcome": "dead", "at": "2026-11-25T01:00:00+00:00", "error": "DNS: s.example: not known"}}
    monkeypatch.setattr("aptfinder.config.load_sites",
                        lambda: {"s": {"id": "s", "url": "https://s.example/", "place_key": "p1"}})
    urls = verify.own_site_urls(conn, "p1", sites={"s": {"id": "s", "url": "https://s.example/", "place_key": "p1"}},
                                rec={}, renders=renders, inputs={"scraped": {}, "listing_sites": {}})
    assert urls[0]["status"] == "dead" and urls[0]["label"].startswith("site dead (DNS) on 11-25")
    assert "s.example: not known" in urls[0]["label"]
