"""Find apartment communities' own websites with a local, headless Claude Code that may only web-search.

  aptf-intake discover-sites --chunk mountain_view [--limit 40] [--dry-run]

Buildings come from the `places` view: a property name, inside the chunk, no community site yet and not
searched before. Claude (`claude -p`, the only tool WebSearch: it never fetches a page from this machine) proposes
a URL or null per building. A proposal is cleaned (no query string, no tour/apply/contact path, no aggregator or
social host) and appended to config/discovered_sites.toml as `[[found]]` (misses as `[[miss]]`, so reruns skip
them). Then the page is fetched through Apify like any community site, and it counts only if it shows the
building's street address (`direct.address_on_page`); a page without the address is appended as `[[check]] outcome = "rejected"` and
the site is dropped; a page not fetched (blocked, timed out, empty) is a retryable outcome and is fetched again
by the next `promote-sites`. Each Claude call is a `runs` row (component `discover`) with its cost.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .. import settings
from ..areas import chunk_of
from ..config import CHECK_LABEL, CHECK_RETRY, check_outcome, last_checks, load_areas, load_sites
from ..store import finish_run, start_run

FILE = "discovered_sites.toml"
SECRET_ENV = ("APIFY_TOKEN", "GOOGLE_MAPS_API_KEY")
AGGREGATORS = re.compile(
    r"(^|\.)(apartments|zillow|trulia|hotpads|rent|rentcafe|apartmentlist|apartmentguide|zumper|padmapper|"
    r"craigslist|realtor|redfin|forrent|apartmentfinder|apartmenthomeliving|yelp|facebook|instagram|google|"
    r"linkedin|twitter|x|youtube|tiktok|nextdoor|bbb|mapquest|yahoo|bing|airbnb|furnishedfinder|blueground|"
    r"rentable|rentals|homes|move|liveapartments|apartmentratings|niche|wikipedia)\.(com|org|net)$", re.I)
ACTION_PATH = re.compile(r"/(book|schedule|tour|apply|application|contact|lease-now|virtual-tour|self-guided|"
                         r"guest-card|email)", re.I)
PLATFORMS = {"avaloncommunities.com": "avalon", "prometheusapartments.com": "prometheus"}

SYSTEM = ("You find the official website of apartment communities with web search. The official website is the "
          "community's own site or its page on the owner/manager's site (e.g. avaloncommunities.com, udr.com, "
          "essexapartmenthomes.com), never a listing aggregator (apartments.com, zillow, rent.com, rentcafe "
          "listing pages, apartmentlist, zumper, yelp, ...). Give the community's home page, not a tour, "
          "application or contact page. If you are not confident the site is for this exact building at this "
          "address, answer null: a wrong site is worse than none. Answer in the requested JSON.")
SCHEMA = {"type": "object", "required": ["results"], "properties": {"results": {"type": "array", "items": {
    "type": "object", "required": ["key", "url", "note"],
    "properties": {"key": {"type": "string"}, "url": {"type": ["string", "null"]}, "note": {"type": "string"}}}}}}


class Blocker(RuntimeError):
    """The search step cannot be trusted (not logged in, tool denied, no search made): stop, record nothing."""


def claude_argv(model: str, max_usd: float) -> list[str]:
    """The fixed, trimmed invocation: only WebSearch, no MCP servers, no user/project settings or memory, no
    skills, no saved session. Without the trimming one call cost about $0.25 instead of $0.02 (trial
    2026-11-24). Not `--bare`: it skips the login."""
    return ["claude", "-p", "--system-prompt", SYSTEM, "--tools", "WebSearch", "--allowedTools", "WebSearch",
            "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands", "--model", model,
            "--no-session-persistence", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
            "--max-budget-usd", f"{max_usd:.2f}"]


def check_result(out: dict[str, Any]) -> list[dict[str, Any]]:
    """The call's answers, or Blocker. `subtype` alone is not enough: a not-logged-in call and a call whose
    search was denied both reported success, the latter with a guessed URL."""
    usage = out.get("modelUsage") or {}
    searches = sum(u.get("webSearchRequests") or 0 for u in usage.values())
    if out.get("is_error") or out.get("subtype") != "success":
        raise Blocker(f"claude call failed: {out.get('subtype')} {out.get('errors') or out.get('result')}")
    if out.get("permission_denials"):
        raise Blocker(f"web search was denied: {out['permission_denials']}")
    if not out.get("structured_output"):
        raise Blocker(f"no structured answer (not logged in?): {str(out.get('result'))[:200]}")
    if searches == 0:
        raise Blocker("the answer used no web search; refusing a guessed URL")
    return out["structured_output"].get("results") or []


def clean_url(url: str | None, keep_query: bool = False) -> tuple[str | None, str]:
    """(url, why-not): no query or fragment, path cut before a tour/apply/contact segment, aggregators refused.
    `keep_query` keeps a link whole (a redirect link names its building in the query) for fetching; the page it
    lands on is cleaned and recorded by `fetch_and_check`."""
    if not url or not url.startswith("http"):
        return None, "no url"
    p = urlsplit(url.strip())
    host = p.netloc.lower().removeprefix("www.")
    if AGGREGATORS.search(host):
        return None, f"aggregator or social host {host}"
    if keep_query:
        return urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, "")), ""
    path = ACTION_PATH.split(p.path)[0] if ACTION_PATH.search(p.path) else p.path
    path = path if path.endswith("/") or "." in path.rsplit("/", 1)[-1] else path + "/"
    return urlunsplit((p.scheme, p.netloc, path, "", "")), ""


def is_form(url: str | None) -> bool:
    """A tour, apply, contact or lead form (`/guest-card/`, `/emailus.cfm`): never a building's site, even when it
    shows the building's address. A listing's short link often lands on one."""
    return bool(url and ACTION_PATH.search(urlsplit(url).path))


def platform_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    return next((v for k, v in PLATFORMS.items() if host.endswith(k)), "photos")


def _records(config_dir: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    import tomllib

    path = (config_dir or settings.CONFIG_DIR) / FILE
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


MODEL_RANK = {"haiku": 1, "sonnet": 2, "opus": 3}


def _rank(model: str | None) -> int:
    return next((v for k, v in MODEL_RANK.items() if k in (model or "").lower()), 0)


def candidates(conn, chunks: list[str], model: str = "sonnet") -> list[dict[str, Any]]:
    """Named buildings in the chunks with no community site yet, not searched before by this model or a
    stronger one (a weaker model's "no site" does not block a stronger search); most listings first."""
    rec = _records()
    run_model = {r["id"]: json.loads(r["params"] or "{}").get("model")
                 for r in conn.execute("SELECT id, params FROM runs WHERE component = 'discover'")}
    def searched_well(r: dict[str, Any]) -> bool:
        return _rank(r.get("model") or run_model.get(r.get("run_id"))) >= _rank(model)
    seen = {r.get("place_key") for r in rec.get("miss", []) if searched_well(r)}
    seen |= {r.get("place_key") for r in rec.get("found", [])
             if str(r.get("found_by", "")).startswith("claude") and searched_well(r)}
    have = {s.get("place_key") for s in load_sites().values()}  # passed or not yet checked
    have |= {r["place_key"] for r in conn.execute("SELECT DISTINCT place_key FROM listings WHERE source = 'direct'")}
    out = []
    for r in conn.execute("SELECT place_key, property_name, address, lat, lon, n_listings FROM places "
                          "WHERE property_name IS NOT NULL AND address IS NOT NULL ORDER BY n_listings DESC"):
        ch = chunk_of(r["lat"], r["lon"])
        if ch in chunks and r["place_key"] not in seen | have:
            out.append({"key": r["place_key"], "name": html.unescape(r["property_name"]).strip(),
                        "address": r["address"], "chunk": ch})
    return out


def ask(batch: list[dict[str, Any]], model: str, max_usd: float) -> tuple[list[dict[str, Any]], float]:
    prompt = ("Find the official website of each apartment community below. Return one result per key.\n"
              + "\n".join(f'- key {b["key"]}: "{b["name"]}", {b["address"]}' for b in batch))
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(claude_argv(model, max_usd) + [prompt], capture_output=True, text=True, cwd=cwd,
                              env=env, timeout=600)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise Blocker(f"claude gave no JSON (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}")
    return check_result(out), float(out.get("total_cost_usd") or 0)


def _toml_value(v: Any) -> str:
    return json.dumps(v) if not isinstance(v, bool) else str(v).lower()


def append(kind: str, rows: list[dict[str, Any]], config_dir: Path | None = None) -> None:
    """Append-only: new tables at the end of config/discovered_sites.toml, never edits of earlier ones."""
    path = (config_dir or settings.CONFIG_DIR) / FILE
    if not path.exists():
        path.write_text("# Machine-appended by `aptf-intake discover-sites` (see src/aptfinder/intake/discover.py).\n"
                        "# [[found]] a proposed site, [[miss]] searched with no site, [[check]] a fetch of its page:\n"
                        "# outcome ok, rejected (no address on it) or not fetched (retried). Delete a [[found]] to drop it.\n")
    with open(path, "a") as f:
        for r in rows:
            f.write(f"\n[[{kind}]]\n" + "".join(f"{k} = {_toml_value(v)}\n" for k, v in r.items() if v is not None))


def _slug(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "site"
    sid, i = base, 2
    while sid in taken:
        sid, i = f"{base}-{i}", i + 1
    return sid


def discover(conn, chunks: list[str], *, limit: int, batch: int, model: str, max_usd: float,
             dry_run: bool = False, fetch: bool = True) -> dict[str, Any]:
    cands = candidates(conn, chunks, model)[:limit]
    est = round(0.025 * len(cands) / max(1, batch) * min(batch, 3), 2)  # trial: ~$0.024 per one-building call
    if dry_run or not cands:
        return {"buildings": [f'{c["name"]} | {c["address"]}' for c in cands], "estimated_usd": est}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    taken = set(load_sites()) | {r["id"] for r in _records().get("found", [])}
    spent, found, missed = 0.0, [], []
    for i in range(0, len(cands), batch):
        if spent >= max_usd:
            break
        group = cands[i:i + batch]
        run = start_run(conn, "discover", "claude-websearch", {"model": model, "places": [c["key"] for c in group]})
        try:
            answers, cost = ask(group, model, min(1.0, max_usd - spent + 0.05))
        except (Blocker, subprocess.TimeoutExpired) as e:
            finish_run(conn, run, "error", error=str(e))
            raise Blocker(str(e)) from e
        spent += cost
        by_key = {a["key"]: a for a in answers}
        f_rows, m_rows = [], []
        for c in group:
            a = by_key.get(c["key"]) or {"url": None, "note": "no answer for this key"}
            url, why = clean_url(a.get("url"))
            base = {"place_key": c["key"], "name": c["name"], "address": c["address"], "chunk": c["chunk"],
                    "found_at": now, "run_id": run, "model": model, "note": (a.get("note") or "")[:300]}
            if url:
                sid = _slug(c["name"], taken)
                taken.add(sid)
                f_rows.append({"id": sid, **base, "platform": platform_of(url), "url": url,
                               "found_by": f"claude -p WebSearch ({model})"})
            else:
                m_rows.append({**base, "reason": why if a.get("url") else "no site found"})
        append("found", f_rows)
        append("miss", m_rows)
        finish_run(conn, run, "ok", {"found": len(f_rows), "missed": len(m_rows)}, est_cost=cost)
        found += f_rows
        missed += m_rows
    res: dict[str, Any] = {"searched": len(found) + len(missed), "found": len(found), "missed": len(missed),
                           "claude_usd": round(spent, 4), "sites": [(r["name"], r["url"]) for r in found]}
    if fetch and found:
        res["fetch"] = fetch_and_check(conn, [r["id"] for r in found])
    return res


def retryable(chunks: list[str]) -> list[str]:
    """Found sites in the chunks whose latest check did not fetch the page (blocked, timed out, empty): fetched
    again by `promote-sites`."""
    rec = _records()
    last = last_checks(rec)
    return [f["id"] for f in rec.get("found", [])
            if f.get("chunk") in chunks and f["id"] in last and check_outcome(last[f["id"]]) in CHECK_RETRY]


def promote(conn, chunks: list[str], *, limit: int, dry_run: bool = False, fetch: bool = True,
            retry_failed: bool = False) -> dict[str, Any]:
    """Websites that listings already name (Apartments.com's website field, URLs read from Craigslist text by
    `extract`) become `[[found]]` sites without a search, then go through the same Apify fetch and address check
    as searched ones. One proposal per place: the URL most of its listings give; every URL you added in Verify
    is a proposal of its own, beside any site the place already has. Found sites whose page was not
    fetched last time (blocked, timed out) are fetched again with them; `retry_failed`: only those."""
    from collections import Counter

    retry = retryable(chunks)
    if retry_failed:
        if dry_run or not retry or not fetch:
            return {"retry": retry}
        return {"retry": retry, "fetch": fetch_and_check(conn, retry)}
    rec = _records()
    proposed = {r.get("place_key") for r in rec.get("found", [])}
    have = {s.get("place_key") for s in load_sites().values()} | proposed
    have |= {r["place_key"] for r in conn.execute("SELECT DISTINCT place_key FROM listings WHERE source = 'direct'")}
    by_place: dict[str, Counter] = {}
    info: dict[str, Any] = {}
    for r in conn.execute(
            "SELECT l.place_key, l.source, json_extract(l.data, '$.extra.website') AS website, p.property_name, "
            "p.address, p.lat, p.lon FROM listings l JOIN places p ON p.place_key = l.place_key "
            "WHERE l.active = 1 AND l.source <> 'direct' AND p.address IS NOT NULL "
            "AND json_extract(l.data, '$.extra.website') IS NOT NULL"):
        if r["place_key"] in have or chunk_of(r["lat"], r["lon"]) not in chunks:
            continue
        by_place.setdefault(r["place_key"], Counter())[(r["website"], r["source"])] += 1
        info[r["place_key"]] = r
    # every website you added in Verify (annotation `website` on the place) is a site of its own, even when the
    # place already has one: a place can have several sources; only a URL already found or listed is skipped
    def norm(u: str) -> str:
        return (clean_url(u, keep_query=True)[0] or u).rstrip("/").lower()

    known = {norm(f["url"]) for f in rec.get("found", [])} | {norm(s["url"]) for s in load_sites().values()}
    yours = []
    for r in conn.execute(
            "SELECT a.entity_key AS place_key, a.comment AS website, p.property_name, p.address, p.lat, p.lon "
            "FROM annotations a JOIN places p ON p.place_key = a.entity_key "
            "WHERE a.entity_type = 'place' AND a.attribute = 'website' AND a.comment LIKE 'http%' "
            "AND p.address IS NOT NULL ORDER BY a.id"):
        url = clean_url(r["website"], keep_query=True)[0] or r["website"]
        if norm(url) in known or chunk_of(r["lat"], r["lon"]) not in chunks:
            continue
        known.add(norm(url))
        by_place.pop(r["place_key"], None)  # your URL outranks a listing's link for the same place
        yours.append((r["place_key"], url, r))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    taken = set(load_sites()) | {r["id"] for r in rec.get("found", [])}
    rows = []
    picks = [(pk, *c.most_common(1)[0], info[pk]) for pk, c in by_place.items()]
    for pk, (url, source), n, r in ([(pk, (url, "you"), 1, r) for pk, url, r in yours] + picks)[:limit]:
        name = html.unescape(r["property_name"] or r["address"]).strip()
        sid = _slug(name, taken)
        taken.add(sid)
        rows.append({"id": sid, "place_key": pk, "name": name, "address": r["address"],
                     "chunk": chunk_of(r["lat"], r["lon"]), "found_at": now, "platform": platform_of(url), "url": url,
                     "found_by": "you (Verify)" if source == "you" else f"{source} listing",
                     "note": "typed in Verify" if source == "you" else f"website field of {n} listing(s)"})
    if dry_run or not (rows or retry):
        return {"sites": [(r["name"], r["url"]) for r in rows], "retry": retry}
    if rows:
        append("found", rows)
    res: dict[str, Any] = {"proposed": len(rows), "retry": retry}
    if fetch:
        res["fetch"] = fetch_and_check(conn, [r["id"] for r in rows] + retry)
    return res


def fetch_and_check(conn, ids: list[str]) -> dict[str, Any]:
    """Fetch the new sites through Apify (the `direct` source) and record whether each page is the building's."""
    from . import ADAPTERS, direct
    from .base import run_source

    direct.sites.cache_clear()
    _, areas = load_areas()
    pick = [a for a in areas if a.kind == "site" and a.id in ids]
    res = run_source(conn, ADAPTERS["direct"], pick)
    items, apify_runs = [], []
    for f in (settings.RAW_DIR / "direct").glob(f"run{res.run_id}_*.json"):
        blob = json.loads(f.read_text())
        items += blob.get("items") or []
        apify_runs += [blob["apify_run"]] if blob.get("apify_run") else []
    sites = direct.sites()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    checks, seen = [], set()
    for it in items:
        s = direct._site_for(it)
        if not s or s["id"] not in ids:
            continue
        seen.add(s["id"])
        form = is_form(it.get("url"))  # the page it landed on, before `clean_url` cuts the form off
        ok = False if form else direct.address_on_page(it, s.get("address"))
        n = len(direct.PARSERS[s["platform"]](it, s)) if ok else 0
        # where the link landed after redirects; later scrapes go there directly
        final, _ = clean_url(it.get("url"))
        moved = final if final and final.rstrip("/") != s["url"].rstrip("/") else None
        checks.append({"id": s["id"], "ok": bool(ok), "at": now, "run_id": res.run_id,
                       "outcome": "ok" if ok else "rejected" if ok is False else "empty",
                       "final_url": moved, "platform": platform_of(moved) if moved and ok else None,
                       "reason": ("address shown; rows: %d" % n) if ok else
                       f"landed on a contact or tour form: {it.get('url')}" if form else
                       ("page does not show the street address" if ok is False else "page has no text")})
    missing = [i for i in ids if i not in seen and i in sites]
    log = ""
    if missing and apify_runs:  # why the page did not come back: the run log names the URL and the error
        from .apify import run_log

        log = "\n".join(run_log(r) for r in apify_runs)
    for i in missing:
        checks.append({"id": i, "ok": False, "at": now, "run_id": res.run_id, **unfetched(sites[i]["url"], log)})
    append("check", checks)
    direct.sites.cache_clear()
    return {"run_id": res.run_id, "apify_usd": res.cost_usd, "ok": [c["id"] for c in checks if c["ok"]],
            "rejected": {c["id"]: c["reason"] for c in checks if c["outcome"] == "rejected"},
            "retry": {c["id"]: c["reason"] for c in checks if c["outcome"] in CHECK_RETRY}}


def unfetched(url: str, log: str) -> dict[str, Any]:
    """A check for a page that did not come back: retryable, with the HTTP status and error the log gives."""
    from .apify import classify_failure, log_errors

    status, msgs = log_errors(log, url)
    if not msgs:
        return {"outcome": "not_fetched", "reason": "page not fetched"}
    oc = classify_failure(status, msgs)
    return {"outcome": oc, "http_status": status, "error": msgs[-1],
            "reason": f"page not fetched: {CHECK_LABEL[oc]}" + (f" (HTTP {status})" if status else "")}
