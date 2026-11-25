"""A free reachability check of a building's own site from this machine, before any Apify run.

  aptf-intake precheck [--site ID] [--dry-run]   # every site in the list by default

User-approved (2026-11-25) exception to "every listing source goes through Apify": per site one DNS lookup of
the host and one plain GET (normal browser User-Agent, redirects followed, at most READ_BYTES of the body read and
never stored or parsed for listing data). Outcomes:

- `dead`: the host does not resolve (NXDOMAIN, or a CNAME to a name that does not resolve: EAI_NONAME / EAI_NODATA).
- `challenge`: a Cloudflare bot challenge ('Just a moment...'): a `cf-mitigated: challenge` header, or a 403/429/503
  from `server: cloudflare` whose first bytes carry the challenge page's markers.
- `proceed`: anything else, including a temporary DNS failure, a timeout, a TLS or connection error or a bare 403
  (Apify's browser may still get through; unknown never rejects).

`render` and `promote-sites` skip `dead` and `challenge` sites (no Apify run) and record the outcome like any
other (`config.PRECHECK_SKIP`); the standalone command records only those two, so it never overwrites a real
render outcome.
"""

from __future__ import annotations

import re
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TIMEOUT_S = 12.0
READ_BYTES = 16384
_DEAD_ERRNO = {getattr(socket, n) for n in ("EAI_NONAME", "EAI_NODATA") if hasattr(socket, n)}
_CF_BODY = re.compile(r"Just a moment\.\.\.|cf-chl|challenge-platform|cf_chl_opt", re.I)


def resolve(host: str, getaddrinfo: Callable = socket.getaddrinfo) -> str | None:
    """None if the host resolves (or the lookup failed for a reason that proves nothing), else the DNS error."""
    try:
        getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        if e.errno in _DEAD_ERRNO:
            return f"DNS: {host}: {e.strerror or e}"
    except (UnicodeError, OSError):
        pass
    return None


def is_challenge(status: int, headers: httpx.Headers, body: str) -> bool:
    if (headers.get("cf-mitigated") or "").lower() == "challenge":
        return True
    return ("cloudflare" in (headers.get("server") or "").lower() and status in (403, 429, 503)
            and bool(_CF_BODY.search(body)))


def precheck(url: str, *, getaddrinfo: Callable = socket.getaddrinfo,
             client: httpx.Client | None = None) -> dict[str, Any]:
    """{outcome: dead | challenge | proceed, http_status, error, final_url} for one site URL."""
    host = urlsplit(url).hostname or ""
    if not host:
        return {"outcome": "proceed", "http_status": None, "error": "no host in the URL"}
    if dns := resolve(host, getaddrinfo):
        return {"outcome": "dead", "http_status": None, "error": dns}
    own = client is None
    c = client or httpx.Client(follow_redirects=True, timeout=TIMEOUT_S,
                               headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8"})
    try:
        with c.stream("GET", url) as r:
            buf = b""
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) >= READ_BYTES:
                    break
            body = buf[:READ_BYTES].decode("utf-8", "replace")
            final = str(r.url)
            if is_challenge(r.status_code, r.headers, body):
                return {"outcome": "challenge", "http_status": r.status_code, "final_url": final,
                        "error": f"Cloudflare challenge (HTTP {r.status_code}"
                                 + (", cf-mitigated" if r.headers.get("cf-mitigated") else "") + ")"}
            # a final host that does not resolve (a redirect to a dead domain) raises ConnectError below
            return {"outcome": "proceed", "http_status": r.status_code, "final_url": final, "error": None}
    except httpx.HTTPError as e:
        return {"outcome": "proceed", "http_status": None, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        if own:
            c.close()


def precheck_many(urls: dict[str, str], workers: int = 16, **kw) -> dict[str, dict[str, Any]]:
    """{id: url} -> {id: precheck result}, a few at a time."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = dict(zip(urls, ex.map(lambda u: precheck(u, **kw), urls.values())))
    return res


def run(conn, *, sites: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Precheck every site in the list (or the ones named) and record the `dead` and `challenge` ones: as the
    site's render outcome (a run of its own, `params.render` and `precheck`, scope `targeted`, cost 0) and, for a
    discovered site whose address check is not `ok`, as its `[[check]]`. Nothing is sent to Apify."""
    from datetime import datetime, timezone

    from ..config import last_checks, load_sites
    from ..store import finish_run, start_run
    from .discover import _records, append

    all_sites = load_sites()
    pick = {sid: s["url"] for sid, s in all_sites.items() if not sites or sid in sites}
    res = precheck_many(pick)
    skip = {sid: r for sid, r in res.items() if r["outcome"] in ("dead", "challenge")}
    out: dict[str, Any] = {"checked": len(res),
                           "counts": {k: sum(r["outcome"] == k for r in res.values())
                                      for k in ("dead", "challenge", "proceed")},
                           "dead": {sid: [pick[sid], r["error"]] for sid, r in skip.items() if r["outcome"] == "dead"},
                           "challenge": {sid: [pick[sid], r["error"]] for sid, r in skip.items()
                                         if r["outcome"] == "challenge"},
                           "proceed_errors": {sid: [pick[sid], r["error"]] for sid, r in res.items()
                                              if r["outcome"] == "proceed" and r["error"]}}
    if dry_run or not skip:
        return out
    areas = {f"{sid}#render": {"state": "failed", "items": 0, "outcome": r["outcome"],
                               "http_status": r.get("http_status"), "error": r["error"], "pages": 0}
             for sid, r in skip.items()}
    run_id = start_run(conn, "intake", "direct", {"render": True, "precheck": True, "scope": "targeted",
                                                  "areas": list(areas)})
    finish_run(conn, run_id, "ok", {"areas": areas, "observations": 0}, est_cost=0.0)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    checks = last_checks(_records())
    rows = [precheck_check(sid, r, now, run_id) for sid, r in skip.items()
            if all_sites[sid].get("verify_address") and (checks.get(sid) or {}).get("outcome") != "ok"
            and not (checks.get(sid) or {}).get("ok")]
    if rows:
        append("check", rows)
    out.update(run_id=run_id, checks_written=len(rows))
    return out


def precheck_check(sid: str, r: dict[str, Any], at: str, run_id: int | None) -> dict[str, Any]:
    """A `[[check]]` row for a site the precheck stopped (outcome dead or challenge)."""
    from ..config import CHECK_LABEL

    return {"id": sid, "ok": False, "at": at, "run_id": run_id, "outcome": r["outcome"],
            "http_status": r.get("http_status"), "error": r["error"],
            "reason": f"precheck: {CHECK_LABEL[r['outcome']]}"}
