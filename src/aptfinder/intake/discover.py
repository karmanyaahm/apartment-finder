"""Find apartment communities' own websites with a local, headless Claude Code that may only web-search.

  aptf-intake discover-sites [--limit 40] [--dry-run]

Buildings come from the `places` view: a property name, inside some station's walk zone (`areas.in_walk_zone`), no community site yet and not
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
from ..areas import in_walk_zone
from ..config import (CHECK_LABEL, CHECK_RETRY, PRECHECK_RECHECK_DAYS, PRECHECK_SKIP, check_outcome, last_checks,
                      latest_found, load_areas, load_sites, site_key)
from ..store import finish_run, start_run

FILE = "discovered_sites.toml"
SECRET_ENV = ("APIFY_TOKEN", "GOOGLE_MAPS_API_KEY")
# Listing platforms, aggregators, social sites and virtual-tour hosts: a URL on one (or a subdomain) is never a
# building's own site. Operator portfolio domains (greystar.com, avaloncommunities.com, udr.com, ...) and
# rentcafewebsite.com community sites are not here; nor are leasing short-link hosts (apt-rental.com,
# *.leasehomenow.com, ...), which redirect to the building's page and are judged where they land.
AGGREGATOR_HOSTS = {
    "apartments.com", "zillow.com", "trulia.com", "hotpads.com", "rent.com", "rentcafe.com", "apartmentlist.com",
    "apartmentguide.com", "zumper.com", "padmapper.com", "craigslist.org", "craigslist.com", "realtor.com",
    "redfin.com", "forrent.com", "corporatehousing.com", "forrentuniversity.com", "yellowpages.com", "apartmentfinder.com", "apartmenthomeliving.com", "rentals.com", "rentable.co",
    "homes.com", "move.com", "liveapartments.com", "apartmentratings.com", "niche.com", "airbnb.com", "vrbo.com",
    "furnishedfinder.com", "blueground.com", "vrent.com", "turbotenant.com", "hemlane.com",
    "matterport.com", "ricoh360.com",
    "yelp.com", "facebook.com", "instagram.com", "google.com", "linkedin.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "nextdoor.com", "bbb.org", "mapquest.com", "yahoo.com", "bing.com", "wikipedia.org"}
ACTION_PATH = re.compile(r"/(book|schedule|tour|apply|application|contact|lease-now|virtual-tour|self-guided|"
                         r"guest-card|email)", re.I)
SPACED_TLDS = {"com", "net", "org", "us", "io", "co", "apartments", "homes", "rentals", "properties", "biz", "info"}
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
    if not p.netloc or re.search(r"\s", p.netloc):
        # a host written with spaces for dots in listing text ("www heritagerealtysanmateo com"): joined when it
        # is only plain labels ending in a known TLD ("x dot com" is not); anything else is not a URL
        labels = p.netloc.lower().split()
        if len(labels) < 2 or labels[-1] not in SPACED_TLDS or "dot" in labels \
                or not all(re.fullmatch(r"[a-z0-9-]+", x) for x in labels):
            return None, f"malformed host {p.netloc!r}"
        p = p._replace(netloc=".".join(labels))
        url = urlunsplit(p)
    if is_aggregator(url):
        return None, f"aggregator or social host {p.netloc.lower().removeprefix('www.')}"
    if keep_query:
        return urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, "")), ""
    path = ACTION_PATH.split(p.path)[0] if ACTION_PATH.search(p.path) else p.path
    path = path if path.endswith("/") or "." in path.rsplit("/", 1)[-1] else path + "/"
    return urlunsplit((p.scheme, p.netloc, path, "", "")), ""


def is_form(url: str | None) -> bool:
    """A tour, apply, contact or lead form (`/guest-card/`, `/emailus.cfm`): never a building's site, even when it
    shows the building's address. A listing's short link often lands on one."""
    return bool(url and ACTION_PATH.search(urlsplit(url).path))


def is_aggregator(url: str | None) -> bool:
    """A URL on a listing platform, aggregator, social or tour host (AGGREGATOR_HOSTS): never a building's own
    site. Craigslist and Zumper links to vrent.com or turbotenant.com single-listing pages are the usual case."""
    host = urlsplit(url or "").netloc.lower().split(":")[0]
    return any(host == h or host.endswith("." + h) for h in AGGREGATOR_HOSTS)


# words that do not tell one building from another (a name made only of these never vouches for a site)
GENERIC_NAME_WORDS = {
    "the", "apartments", "apartment", "apts", "apt", "and", "homes", "home", "residences", "residence", "living",
    "village", "park", "place", "plaza", "court", "courts", "gardens", "garden", "manor", "tower", "towers", "house",
    "station", "square", "center", "north", "south", "east", "west", "studio", "bedroom", "bedrooms", "bath",
    "baths", "townhome", "townhomes", "community", "communities", "luxury", "street", "avenue", "boulevard",
    "affordable",
    "senior", "family", "lofts", "flat", "suites", "terrace", "commons", "view", "vista", "oaks", "creek"}


def name_tokens(name: str | None) -> list[str]:
    """The building name's distinctive words (4+ letters, not GENERIC_NAME_WORDS): 'The Heltsley' -> ['heltsley']."""
    words = re.findall(r"[a-z0-9]+", (name or "").split(",")[0].lower())
    return [w for w in words if len(w) >= 4 and w not in GENERIC_NAME_WORDS and not w.isdigit()]


ERROR_PAGE = re.compile(r"\b(404|not found|bad gateway|error|forbidden|access denied|just a moment)\b", re.I)


def vouched(site: dict[str, Any], item: dict[str, Any], website_hosts: set[str] | frozenset = frozenset()
            ) -> str | None:
    """Why a page whose text lacks the street address is still the building's own (the address often sits only in
    a footer image or on the contact page), or None. Never for an error page ('404 Not Found', 'Rentcafe Error')
    or a page with almost no text. Then: you gave the URL in Verify; or its host is the website Google Places
    gives for this place, on a host no other place's Places website uses (`place_website_hosts`: a shared
    operator host such as midpen-housing.org or cbw-properties.com names no one building), and the site did not
    come from Google Places itself (that would vouch for itself); or every distinctive word of the building's name
    is in the page's host and path, or in its title ('The Heltsley' on theheltsley.com), and the page names the
    building's city. A generic name ('Park Plaza Apartments') never vouches that way."""
    title = item.get("title") or ""
    if ERROR_PAGE.search(title) or len((item.get("text") or "").strip()) < 200 or is_aggregator(item.get("url")):
        return None
    if str(site.get("found_by", "")).startswith("you"):
        return "you added this URL"
    url = item.get("url") or site.get("url") or ""
    host = urlsplit(url).netloc.lower().split(":")[0].removeprefix("www.")
    if host and host in website_hosts and not str(site.get("found_by", "")).startswith("google_places"):
        return f"Google Places gives this place's website on {host}"
    toks = name_tokens(site.get("name"))
    # the name rules also need the building's city on the page: rentvillaoaks.com (a 'Villa Oaks' in Santa Rosa)
    # is not Sunnyvale's Villa Oaks
    parts = [p.strip() for p in (site.get("address") or "").split(",")]
    city = parts[1].lower() if len(parts) > 2 else None
    # ...outside JSON blobs: rentvillaoaks.com's text carries its operator's portfolio JSON, Sunnyvale buildings
    # included
    text = item.get("text") or ""
    for _ in range(4):
        text = re.sub(r"\{[^{}]*\}", " ", text)
    if not toks or not city or city not in (title + " " + text).lower():
        return None
    where = re.sub(r"[^a-z0-9]", "", host + urlsplit(url).path.lower())
    if all(t in where for t in toks):
        return f"the URL names the building ({', '.join(toks)})"
    if all(re.search(rf"\b{t}\b", title.lower()) for t in toks):
        return f"the page title names the building ({title.strip()[:80]})"
    return None


def place_website_hosts(conn) -> dict[str, set[str]]:
    """place_key -> hosts of the websites Google Places gives for it (`extra.website` of google_places rows), only
    hosts that no other place's Places website uses (an operator's shared host names no one building)."""
    by_host: dict[str, set[str]] = {}
    for r in conn.execute("SELECT DISTINCT place_key, json_extract(data, '$.extra.website') AS w FROM listings "
                          "WHERE source = 'google_places' AND place_key IS NOT NULL "
                          "AND json_extract(data, '$.extra.website') IS NOT NULL"):
        h = urlsplit(r["w"]).netloc.lower().split(":")[0].removeprefix("www.")
        if h and not is_aggregator(r["w"]):
            by_host.setdefault(h, set()).add(r["place_key"])
    out: dict[str, set[str]] = {}
    for h, pks in by_host.items():
        if len(pks) == 1:
            out.setdefault(next(iter(pks)), set()).add(h)
    return out


def platform_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    return next((v for k, v in PLATFORMS.items() if host.endswith(k)), "photos")


def _records(config_dir: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    import tomllib

    path = (config_dir or settings.CONFIG_DIR) / FILE
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        rec = tomllib.load(f)
    return {**rec, "found": latest_found(rec)} if "found" in rec else rec


MODEL_RANK = {"haiku": 1, "sonnet": 2, "opus": 3}


def _rank(model: str | None) -> int:
    return next((v for k, v in MODEL_RANK.items() if k in (model or "").lower()), 0)


def scraped_places(conn) -> set[str]:
    """Places with own-site (`direct`) rows, leaving out rows read from an aggregator page (vrent.com,
    turbotenant.com, ...): such a place still has no own site."""
    return {r[0] for r in conn.execute("SELECT DISTINCT place_key, url FROM listings WHERE source = 'direct' "
                                       "AND place_key IS NOT NULL") if not is_aggregator(r[1])}


def candidates(conn, model: str = "sonnet") -> list[dict[str, Any]]:
    """Named buildings in a station walk zone with no community site yet, not searched before by this model or a
    stronger one (a weaker model's "no site" does not block a stronger search); most listings first."""
    rec = _records()
    run_model = {r["id"]: json.loads(r["params"] or "{}").get("model")
                 for r in conn.execute("SELECT id, params FROM runs WHERE component = 'discover'")}
    def searched_well(r: dict[str, Any]) -> bool:
        return _rank(r.get("model") or run_model.get(r.get("run_id"))) >= _rank(model)
    seen = {r.get("place_key") for r in rec.get("miss", []) if searched_well(r)}
    seen |= {r.get("place_key") for r in rec.get("found", [])
             if str(r.get("found_by", "")).startswith("claude") and searched_well(r)}
    have = {s.get("place_key") for s in load_sites(duplicates=True).values()}  # passed or not yet checked
    have |= scraped_places(conn)
    out = []
    for r in conn.execute("SELECT place_key, property_name, address, lat, lon, n_listings FROM places "
                          "WHERE property_name IS NOT NULL AND address IS NOT NULL ORDER BY n_listings DESC"):
        if in_walk_zone(r["lat"], r["lon"]) and r["place_key"] not in seen | have:
            out.append({"key": r["place_key"], "name": html.unescape(r["property_name"]).strip(),
                        "address": r["address"]})
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


def discover(conn, *, limit: int, batch: int, model: str, max_usd: float,
             dry_run: bool = False, fetch: bool = True) -> dict[str, Any]:
    cands = candidates(conn, model)[:limit]
    est = round(0.025 * len(cands) / max(1, batch) * min(batch, 3), 2)  # trial: ~$0.024 per one-building call
    if dry_run or not cands:
        return {"buildings": [f'{c["name"]} | {c["address"]}' for c in cands], "estimated_usd": est}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    every = load_sites(duplicates=True)
    taken = set(every) | {r["id"] for r in _records().get("found", [])}
    # a URL already in the list (or found before) is the same site (config.duplicate_sites): never a second entry
    urls = {site_key(r["url"]): r["id"] for r in _records().get("found", [])} | {
        site_key(s["url"]): sid for sid, s in every.items()}
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
            base = {"place_key": c["key"], "name": c["name"], "address": c["address"],
                    "found_at": now, "run_id": run, "model": model, "note": (a.get("note") or "")[:300]}
            if url and site_key(url) in urls:
                url, why = None, f"same site as {urls[site_key(url)]} ({url})"
            if url:
                sid = _slug(c["name"], taken)
                taken.add(sid)
                urls[site_key(url)] = sid
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


def retryable(recheck: bool = False) -> list[str]:
    """Found sites whose latest check did not fetch the page (blocked, timed out, empty): fetched
    again by `promote-sites`. `recheck`: also those the precheck stopped (dead domains) over
    PRECHECK_RECHECK_DAYS ago (`--retry-failed` never takes them)."""
    rec = _records()
    last = last_checks(rec)
    dup = {sid for sid, s in load_sites(duplicates=True).items() if s.get("duplicate_of")}  # never fetched

    def due(c: dict[str, Any]) -> bool:
        oc = check_outcome(c)
        if oc in PRECHECK_SKIP:
            return recheck and _age_days(c.get("at")) > PRECHECK_RECHECK_DAYS
        return oc in CHECK_RETRY

    return [f["id"] for f in rec.get("found", []) if not is_aggregator(f["url"]) and f["id"] not in dup
            and f["id"] in last and due(last[f["id"]])]


def _age_days(ts: str | None) -> float:
    if not ts:
        return float("inf")
    t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


def promote(conn, *, limit: int, dry_run: bool = False, fetch: bool = True,
            retry_failed: bool = False, browser: bool = False, residential: bool = False) -> dict[str, Any]:
    """Websites that listings already name (Apartments.com's website field, URLs read from Craigslist text by
    `extract`) become `[[found]]` sites without a search, then go through the same Apify fetch and address check
    as searched ones. One proposal per place: the URL most of its listings give; every URL you added in Verify
    is a proposal of its own, beside any site the place already has. Found sites whose page was not
    fetched last time (blocked, timed out) are fetched again with them; `retry_failed`: only those."""
    from collections import Counter

    retry = retryable(recheck=not retry_failed)
    if retry_failed:
        if dry_run or not retry or not fetch:
            return {"retry": retry}
        return {"retry": retry, "fetch": fetch_and_check(conn, retry, browser=browser, residential=residential)}
    rec = _records()
    proposed = {r.get("place_key") for r in rec.get("found", []) if not is_aggregator(r["url"])}
    every = load_sites(duplicates=True)
    have = {s.get("place_key") for s in every.values()} | proposed | scraped_places(conn)
    by_place: dict[str, Counter] = {}
    info: dict[str, Any] = {}
    for r in conn.execute(
            "SELECT l.place_key, l.source, json_extract(l.data, '$.extra.website') AS website, p.property_name, "
            "p.address, p.lat, p.lon FROM listings l JOIN places p ON p.place_key = l.place_key "
            "WHERE l.active = 1 AND l.source <> 'direct' AND p.address IS NOT NULL "
            "AND json_extract(l.data, '$.extra.website') IS NOT NULL"):
        if r["place_key"] in have or is_aggregator(r["website"]) or not in_walk_zone(r["lat"], r["lon"]):
            continue
        by_place.setdefault(r["place_key"], Counter())[(r["website"], r["source"])] += 1
        info[r["place_key"]] = r
    # every website you added in Verify (annotation `website` on the place) is a site of its own, even when the
    # place already has one: a place can have several sources; only a URL already found or listed is skipped
    # a URL already found or listed is the same site (config.site_key): never proposed again
    norm = site_key
    known = {norm(f["url"]) for f in rec.get("found", [])} | {norm(s["url"]) for s in every.values()}
    yours = []
    for r in conn.execute(
            "SELECT a.entity_key AS place_key, a.comment AS website, p.property_name, p.address, p.lat, p.lon "
            "FROM annotations a JOIN places p ON p.place_key = a.entity_key "
            "WHERE a.entity_type = 'place' AND a.attribute = 'website' AND a.comment LIKE 'http%' "
            "AND p.address IS NOT NULL ORDER BY a.id"):
        url = clean_url(r["website"], keep_query=True)[0] or r["website"]
        if norm(url) in known or is_aggregator(url) or not in_walk_zone(r["lat"], r["lon"]):
            continue
        known.add(norm(url))
        by_place.pop(r["place_key"], None)  # your URL outranks a listing's link for the same place
        yours.append((r["place_key"], url, r))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    taken = set(every) | {r["id"] for r in rec.get("found", [])}
    rows = []
    picks = [(pk, *c.most_common(1)[0], info[pk]) for pk, c in by_place.items()]
    kept = []
    for p in picks:  # a listing's website that is already a site (or another place's pick this pass) is skipped
        if norm(p[1][0]) not in known:
            known.add(norm(p[1][0]))
            kept.append(p)
    picks = kept
    for pk, (url, source), n, r in ([(pk, (url, "you"), 1, r) for pk, url, r in yours] + picks)[:limit]:
        name = html.unescape(r["property_name"] or r["address"]).strip()
        sid = _slug(name, taken)
        taken.add(sid)
        rows.append({"id": sid, "place_key": pk, "name": name, "address": r["address"],
                     "found_at": now, "platform": platform_of(url), "url": url,
                     "found_by": "you (Verify)" if source == "you" else f"{source} listing",
                     "note": "typed in Verify" if source == "you" else f"website field of {n} listing(s)"})
    if dry_run or not (rows or retry):
        return {"sites": [(r["name"], r["url"]) for r in rows], "retry": retry}
    if rows:
        append("found", rows)
    res: dict[str, Any] = {"proposed": len(rows), "retry": retry}
    if fetch:
        res["fetch"] = fetch_and_check(conn, [r["id"] for r in rows] + retry, browser=browser,
                                       residential=residential)
    return res


# One page per site in a real browser, for the address check only (sites that refuse the cheap fetch). The item
# has the fields `direct.address_on_page` reads: the loaded URL, the page text and its ld+json scripts.
BROWSER_CHECK_PAGE = r"""async function pageFunction(context) {
  const { page, request, response } = context;
  await page.waitForTimeout(2500);
  const d = await page.evaluate(() => ({
    title: document.title,
    text: document.body ? document.body.innerText.replace(/\s+/g, ' ').slice(0, 100000) : '',
    scripts: [...document.querySelectorAll('script[type="application/ld+json"]')]
      .map((x) => ({ type: 'application/ld+json', id: x.id || '', text: x.textContent.slice(0, 20000) })),
  }));
  const st = response ? (typeof response.status === 'function' ? response.status() : response.status) : null;
  return { url: page.url(), requested: request.url, status: st, site: (request.userData || {}).site || null,
           images: [], ...d };
}"""


def _browser_fetch(conn, ids: list[str], residential: bool) -> tuple[int, float, list[dict[str, Any]], list[str]]:
    """The check pages through the browser actor (and the residential proxy if asked). Recorded as an intake run
    of source `direct` with `sites` (not `areas`), so it is not coverage."""
    from . import direct
    from .apify import RESIDENTIAL_USD_PER_PAGE, browser_proxy, run_actor, run_record, settle
    from .render import ACTOR

    sites = direct.sites()
    reqs = [{"url": sites[i]["url"], "userData": {"site": i}} for i in ids if i in sites]
    proxy = "residential" if residential else "datacenter"
    run_id = start_run(conn, "intake", "direct", {"check": True, "browser": True, "scope": "targeted", "proxy": proxy,
                                                  "sites": [r["userData"]["site"] for r in reqs], "actor": ACTOR})
    try:
        res = run_actor(ACTOR, {"startUrls": reqs, "pageFunction": BROWSER_CHECK_PAGE, **browser_proxy(residential),
                                "maxRequestsPerCrawl": len(reqs), "maxRequestRetries": 2, "headless": True,
                                "pageLoadTimeoutSecs": 90, "pageFunctionTimeoutSecs": 90,
                                "waitUntil": "domcontentloaded"},
                        max_items=2 * len(reqs), max_charge_usd=1.0, timeout_s=1800, memory_mb=2048, clean=False,
                        with_log=True, est_usd=RESIDENTIAL_USD_PER_PAGE * len(reqs) if residential else None)
    except Exception as e:  # the credit guard's refusal or an Apify error: close the run, then re-raise
        finish_run(conn, run_id, "error", {}, error=repr(e)[:500])
        raise
    raw = settings.RAW_DIR / "direct" / f"run{run_id}_browsercheck.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps({"actor": ACTOR, "apify_run": res.run_id, "status": res.status, "proxy": proxy,
                               "cost_usd": res.cost_usd, "items": res.items, "log": res.log}))
    items = [i for i in res.items if not i.get("#error")]
    recs = [run_record(res)]
    finish_run(conn, run_id, "ok", {"items": len(items), "pages": len(reqs), "apify_usd": settle(recs),
                                    "apify_runs": recs}, est_cost=res.cost_usd)
    return run_id, res.cost_usd or 0.0, items, [res.log or ""]


def fetch_and_check(conn, ids: list[str], *, browser: bool = False, residential: bool = False) -> dict[str, Any]:
    """Fetch the new sites through Apify (the `direct` source) and record whether each page is the building's.
    `browser`: through the browser actor instead (sites that refuse the cheap fetch), `residential` also through
    the residential proxy; one page per site, for the check only."""
    from . import ADAPTERS, direct
    from .base import run_source
    from .precheck import precheck_check, precheck_many

    direct.sites.cache_clear()
    # the free precheck first: a dead domain gets its check now and no Apify fetch
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pre = precheck_many({i: direct.sites()[i]["url"] for i in ids if i in direct.sites()})
    stopped = [precheck_check(i, r, now, None) for i, r in pre.items() if r["outcome"] in PRECHECK_SKIP]
    if stopped:
        append("check", stopped)
    ids = [i for i in ids if i not in {c["id"] for c in stopped}]
    pre_out = {c["id"]: c["reason"] + f": {c['error']}" for c in stopped}
    if not ids:
        direct.sites.cache_clear()
        return {"run_id": None, "apify_usd": 0.0, "ok": [], "rejected": {}, "retry": {}, "precheck": pre_out}
    items, apify_runs, logs = [], [], []
    if browser or residential:
        run_id, cost, items, logs = _browser_fetch(conn, ids, residential)

        class _R:  # the fields the rest of this function reads from a run
            pass

        res = _R()
        res.run_id, res.cost_usd = run_id, cost
    else:
        areas = load_areas()
        pick = [a for a in areas if a.kind == "site" and a.id in ids]
        res = run_source(conn, ADAPTERS["direct"], pick)
        for f in (settings.RAW_DIR / "direct").glob(f"run{res.run_id}_*.json"):
            blob = json.loads(f.read_text())
            items += blob.get("items") or []
            apify_runs += [blob["apify_run"]] if blob.get("apify_run") else []
    sites = direct.sites()
    hosts = place_website_hosts(conn) if conn is not None else {}
    checks, seen = [], set()
    for it in items:
        s = direct._site_for(it)
        if not s or s["id"] not in ids:
            continue
        seen.add(s["id"])
        form = is_form(it.get("url"))  # the page it landed on, before `clean_url` cuts the form off
        ok = False if form else direct.address_on_page(it, s.get("address"))
        why = None  # the address is not on the page, but the page is still the building's (`vouched`)
        if ok is False and not form and (why := vouched(s, it, hosts.get(s.get("place_key"), set()))):
            ok = True
        try:
            n = len(direct.PARSERS[s["platform"]](it, s)) if ok else 0
        except Exception:  # a browser-check item lacks fields some platform parsers read; the check stands
            n = 0
        # where the link landed after redirects; later scrapes go there directly
        final, _ = clean_url(it.get("url"))
        moved = final if final and final.rstrip("/") != s["url"].rstrip("/") else None
        checks.append({"id": s["id"], "ok": bool(ok), "at": now, "run_id": res.run_id,
                       "outcome": "ok" if ok else "rejected" if ok is False else "empty",
                       "final_url": moved, "platform": platform_of(moved) if moved and ok else None,
                       "reason": (f"{why}; rows: {n}" if why else "address shown; rows: %d" % n) if ok else
                       f"landed on a contact or tour form: {it.get('url')}" if form else
                       ("page does not show the street address" if ok is False else "page has no text")})
    missing = [i for i in ids if i not in seen and i in sites]
    log = ""
    if missing and apify_runs:  # why the page did not come back: the run log names the URL and the error
        from .apify import run_log

        log = "\n".join(run_log(r) for r in apify_runs)
    elif missing and logs:
        log = "\n".join(logs)
    for i in missing:
        checks.append({"id": i, "ok": False, "at": now, "run_id": res.run_id, **unfetched(sites[i]["url"], log)})
    append("check", checks)
    direct.sites.cache_clear()
    return {"run_id": res.run_id, "apify_usd": res.cost_usd, "ok": [c["id"] for c in checks if c["ok"]],
            "rejected": {c["id"]: c["reason"] for c in checks if c["outcome"] == "rejected"},
            "retry": {c["id"]: c["reason"] for c in checks if c["outcome"] in CHECK_RETRY}, "precheck": pre_out}


def unfetched(url: str, log: str) -> dict[str, Any]:
    """A check for a page that did not come back: retryable, with the HTTP status and error the log gives."""
    from .apify import classify_failure, log_errors

    status, msgs = log_errors(log, url)
    if not msgs:
        return {"outcome": "not_fetched", "reason": "page not fetched"}
    oc = classify_failure(status, msgs)
    return {"outcome": oc, "http_status": status, "error": msgs[-1],
            "reason": f"page not fetched: {CHECK_LABEL[oc]}" + (f" (HTTP {status})" if status else "")}
