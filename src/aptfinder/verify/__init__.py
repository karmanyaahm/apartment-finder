"""verify: sample and conflict queues, accuracy per source and field from `checks`."""

from __future__ import annotations

import json
import math
import random
import re
import sqlite3
from urllib.parse import urlsplit
from collections import defaultdict
from typing import Any

CONFLICT_FIELDS = ["price_month", "beds", "baths", "sqft", "available_from"]
# conflict field -> the stored field a check verifies
CHECK_FIELD = {"price_month": "price"}


def is_named_building(property_name: str | None, address: str | None) -> bool:
    """A named apartment community: a property name that is not an address (so not a house or a single unit),
    and an address. The set the no-website queue and the dashboard's Pipeline panel count over."""
    import re

    name = (property_name or "").strip()
    return bool(name) and address is not None and not re.match(r"^\d", name)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def accuracy(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """{(source, field): {correct, n, acc, lo, hi}}; cannot_tell is excluded. One vote per distinct fact:
    the latest check per (listing, field, checked value), so re-checking an unchanged value in a later
    run does not count twice, while a value the source later changed is a new fact.
    Trips are keyed as ('trip:<provider>:<mode>', 'minutes')."""
    rows = conn.execute("""
        SELECT src, field, verdict FROM (
            SELECT COALESCE(o.source, 'trip:' || t.provider || ':' || t.mode) AS src, c.field, c.verdict,
                   ROW_NUMBER() OVER (PARTITION BY
                       COALESCE(o.source || '|' || o.source_id, 'trip|' || c.trip_id), c.field, c.stored_value
                       ORDER BY c.id DESC) AS rn
            FROM checks c
            LEFT JOIN observations o ON o.id = c.observation_id
            LEFT JOIN trips t ON t.id = c.trip_id
        ) WHERE rn = 1 AND verdict != 'cannot_tell'
    """).fetchall()
    tally: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        t = tally[(r["src"], r["field"])]
        t[1] += 1
        t[0] += r["verdict"] == "correct"
    out = {}
    for key, (k, n) in tally.items():
        lo, hi = wilson(k, n)
        out[key] = {"correct": k, "n": n, "acc": k / n, "lo": lo, "hi": hi}
    return out


def low_accuracy(acc: dict, min_samples: int, min_accuracy: float) -> set[tuple[str, str]]:
    return {key for key, a in acc.items() if a["n"] >= min_samples and a["acc"] < min_accuracy}


def current_checks(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, str]]:
    """(source, source_id) -> {field: verdict} for checks still valid against the latest observation."""
    out: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for r in conn.execute("SELECT source, source_id, field, verdict FROM current_checks"):
        out[(r["source"], r["source_id"])][r["field"]] = r["verdict"]
    return out


def _checked_trips(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT DISTINCT trip_id FROM checks WHERE trip_id IS NOT NULL")}


def _round_robin(groups: dict[str, list[Any]], seed: int) -> list[Any]:
    rng = random.Random(seed)
    lists = []
    for key in sorted(groups):
        items = list(groups[key])
        rng.shuffle(items)
        lists.append(items)
    out = []
    while any(lists):
        for lst in lists:
            if lst:
                out.append(lst.pop())
    return out


def sample_queue(conn: sqlite3.Connection, seed: int = 0, include_trips: bool = True) -> list[dict[str, Any]]:
    """Unchecked items drawn evenly across sources (round robin), repeatable for a given seed.
    Listings are grouped by source; routed trips by provider and mode. Straight-line estimates are
    never sampled: they are labelled guesses, and checking them by hand measures nothing."""
    checked = current_checks(conn)
    ever: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in conn.execute("SELECT DISTINCT o.source, o.source_id, c.field FROM checks c "
                          "JOIN observations o ON o.id = c.observation_id"):
        ever[(r["source"], r["source_id"])].add(r["field"])
    groups: dict[str, list[Any]] = defaultdict(list)
    for r in conn.execute("SELECT source, source_id, obs_id, place_key FROM listings WHERE kind IS NOT 'site_info' "
                          "ORDER BY source, source_id"):
        key = (r["source"], r["source_id"])
        # in the queue if never checked, or if a field that was checked has since changed
        if key not in ever or not ever[key] <= set(checked.get(key, {})):
            groups[r["source"]].append({"kind": "listing", "source": r["source"], "source_id": r["source_id"],
                                        "obs_id": r["obs_id"], "place_key": r["place_key"]})
    if include_trips:
        tchecked = _checked_trips(conn)
        for r in conn.execute("SELECT id, place_key, point_id, mode, provider FROM trips "
                              "WHERE minutes IS NOT NULL AND provider != 'estimate' ORDER BY id"):
            if r["id"] not in tchecked:
                groups[f"trip:{r['provider']}:{r['mode']}"].append(
                    {"kind": "trip", "trip_id": r["id"], "place_key": r["place_key"], "point_id": r["point_id"],
                     "mode": r["mode"], "provider": r["provider"]})
    return _round_robin(groups, seed)


def _differs(field: str, a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    if field == "price_month":
        return abs(a - b) > 0.01 * max(abs(a), abs(b))
    return a != b


def pending_render(conn: sqlite3.Connection) -> dict[str, str]:
    """place_key -> reason, for places with an own site (any `direct` row, retired ones too) that has not been
    rendered yet (`aptf-intake render`: no direct row with extra.platform = 'render'). Avalon sites never need a
    render: their page data gives exact units. A pending render will replace the own-site plans, so a conflict
    there is likely to settle without a hand check."""
    from ..config import load_sites

    avalon = {sid for sid, s in load_sites().items() if s.get("platform") == "avalon"}
    out = {}
    for r in conn.execute("""
            SELECT place_key, GROUP_CONCAT(DISTINCT json_extract(data, '$.extra.site')) AS sites,
                   SUM(json_extract(data, '$.extra.platform') IN ('render', 'avalon')) AS done
            FROM listings WHERE source = 'direct' AND place_key IS NOT NULL GROUP BY place_key"""):
        sites = sorted(set((r["sites"] or "").split(",")) - {""})
        if r["done"] or any(s in avalon for s in sites):
            continue
        out[r["place_key"]] = f"own site {', '.join(sites) or '(unnamed)'} not rendered yet"
    return out


def conflict_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Units with active listings from more than one source that disagree on a field (a retired listing never
    makes a conflict). A conflict drops out once every disagreeing field has a current check on every listing
    (see the current_checks view). Conflicts at a place whose own site awaits a render carry
    `pending_render: True` and a `pending_reason`, and come after the actionable ones. A place whose own website
    prices an offer has no conflicts: the own site is the truth there, and disagreements among other sources
    decide nothing (`plans.own_site_places`)."""
    from ..plans import own_site_places

    own = own_site_places(conn)
    by_unit: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM listings WHERE unit_key IS NOT NULL AND active = 1 "
                          "AND kind IS NOT 'site_info'"):
        by_unit[r["unit_key"]].append(r)
    checked = current_checks(conn)
    pending = pending_render(conn)
    out = []
    for uk, rows in sorted(by_unit.items()):
        if len({r["source"] for r in rows}) < 2 or rows[0]["place_key"] in own:
            continue
        fields = []
        for f in CONFLICT_FIELDS:
            vals = [r[f] for r in rows]
            if any(_differs(f, a, b) for i, a in enumerate(vals) for b in vals[i + 1:]):
                cf = CHECK_FIELD.get(f, f)
                if not all(cf in checked.get((r["source"], r["source_id"]), {}) for r in rows):
                    fields.append(f)
        if fields:
            pk = rows[0]["place_key"]
            out.append({"kind": "conflict", "unit_key": uk, "place_key": pk, "fields": fields,
                        "pending_render": pk in pending, "pending_reason": pending.get(pk),
                        "listings": [{"source": r["source"], "source_id": r["source_id"], "obs_id": r["obs_id"]}
                                     for r in rows]})
    return sorted(out, key=lambda c: c["pending_render"])  # stable: actionable first, unit order within


def all_conflicts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every open conflict in Verify order: actionable cross-source ones, then text conflicts (a render does not
    settle those), then cross-source ones waiting for an own-site render."""
    cross = conflict_queue(conn)
    return ([c for c in cross if not c["pending_render"]] + text_conflict_queue(conn)
            + [c for c in cross if c["pending_render"]])


def text_conflict_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Listings whose text (read by `aptf-intake extract`) contradicts a value the source stated: the source's
    value is stored, the text's is shown beside it as '<source> (text)'. Drops out once each such field has a
    current check on the listing. None at a place whose own website prices an offer: there other sources'
    listings decide nothing, so their text is moot."""
    from ..plans import own_site_places

    own = own_site_places(conn)
    checked = current_checks(conn)
    out = []
    for r in conn.execute("SELECT source, source_id, obs_id, place_key, "
                          "json_extract(data, '$.extra.extracted.conflicts') AS c FROM listings "
                          "WHERE json_extract(data, '$.extra.extracted.conflicts') IS NOT NULL ORDER BY source_id"):
        if r["place_key"] in own and r["source"] != "direct":
            continue
        text = json.loads(r["c"])
        open_ = {f: v for f, v in text.items() if f not in checked.get((r["source"], r["source_id"]), {})}
        if open_:
            out.append({"kind": "conflict", "unit_key": None, "place_key": r["place_key"], "fields": sorted(open_),
                        "pending_render": False, "text": open_, "text_label": f"{r['source']} (text)",
                        "listings": [{"source": r["source"], "source_id": r["source_id"], "obs_id": r["obs_id"]}]})
    return out


OWN_SITE_REASONS = {  # reason -> label, in the order an item takes the first that applies
    "no_website": "no website known",
    "form": "a site is a contact, tour or lead form",
    "address_failed": "a site's page does not show the building's address",
    "no_plans": "own site read, but no floor plans or prices found",
    "all_failed": "every site failed to load (blocked, timed out, empty or not returned)",
    "not_checked": "website known, not checked yet",
}


SCRAPED_SQL = ("SELECT place_key, substr(source_id, 1, instr(source_id, ':') - 1) AS site, MAX(last_seen) AS seen, "
               "MIN(url) AS url, SUM(active AND price IS NOT NULL AND kind IS NOT 'site_info') AS priced "
               "FROM listings WHERE source = 'direct' {where} GROUP BY 1, 2")
LISTING_SITES_SQL = ("SELECT DISTINCT place_key, json_extract(data, '$.extra.website') AS w, source FROM listings "
                     "WHERE source <> 'direct' AND json_extract(data, '$.extra.website') IS NOT NULL {where}")


def _by_place(rows) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["place_key"], []).append(dict(r))
    return out


def own_site_inputs(conn: sqlite3.Connection, place_key: str | None = None) -> dict[str, Any]:
    """The two `listings` reads own_site_urls needs, for one place or for all at once (`listings` is a view, so
    one query per place costs a full view build each: 282 places took 90 s)."""
    where, args = ("AND place_key = ?", (place_key,)) if place_key else ("", ())
    return {"scraped": _by_place(conn.execute(SCRAPED_SQL.format(where=where), args)),
            "listing_sites": _by_place(conn.execute(LISTING_SITES_SQL.format(where=where), args))}


def own_site_urls(conn: sqlite3.Connection, place_key: str, *, sites: dict | None = None, rec: dict | None = None,
                  renders: dict | None = None, inputs: dict | None = None) -> list[dict[str, Any]]:
    """Every URL tried or known for a building's own site, each with its last outcome: `status` is form, rejected,
    no_plans, priced, failed or pending, `label` says what happened and when. Several URLs per place are normal:
    the hand list, sites found by search or named by listings, and any you added in Verify."""
    from ..config import CHECK_RETRY, check_outcome, last_checks, load_sites
    from ..intake.discover import _records, clean_url, is_aggregator, is_form
    from ..intake.render import TRANSIENT, describe, last_outcomes

    sites = load_sites() if sites is None else sites
    rec = _records() if rec is None else rec
    renders = last_outcomes(conn) if renders is None else renders
    checks = last_checks(rec)
    inputs = own_site_inputs(conn, place_key) if inputs is None else inputs
    scraped = {r["site"]: r for r in inputs["scraped"].get(place_key, [])}
    out: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    listing_hosts: dict[str, dict[str, Any]] = {}

    def add(url: str, via: str, sid: str | None = None, check: dict | None = None) -> None:
        key = (clean_url(url)[0] or url).rstrip("/").lower().replace("://www.", "://").replace("http://", "https://")
        if key in seen_urls or is_aggregator(url):  # a vrent.com / turbotenant.com page is not an own site
            return
        seen_urls.add(key)
        e: dict[str, Any] = {"url": url, "via": via, "site": sid}
        final = (check or {}).get("final_url")
        rd, sc = renders.get(sid or ""), scraped.get(sid or "")
        if is_form(url) or is_form(final) or str((check or {}).get("reason", "")).startswith("landed on a contact"):
            e.update(status="form", label=f"contact/tour form{f' (landed on {final})' if final else ''}, dropped")
        elif check and check_outcome(check) == "rejected":
            e.update(status="rejected", label=f"address check failed on {str(check.get('at'))[5:10]}: "
                                              f"{check.get('reason')}")
        elif sc and sc["priced"]:
            e.update(status="priced", label="priced offers")
        elif rd and rd["outcome"] in TRANSIENT:
            e.update(status="failed", label=f"render {describe(rd)}")
        elif rd or sc:
            e.update(status="no_plans", label=(f"render {describe(rd)}".replace("plans found", "plans found, none priced")
                                               if rd else "")
                     + (f"{'; ' if rd else ''}page scraped {str(sc['seen'])[5:10]}: photos/link only" if sc else ""))
        elif check and check_outcome(check) in CHECK_RETRY:
            e.update(status="failed", label=f"address check: page not fetched ({check_outcome(check)}"
                                            f"{', HTTP %s' % check['http_status'] if check.get('http_status') else ''}) "
                                            f"on {str(check.get('at'))[5:10]}, will retry")
        elif via.startswith("you"):
            e.update(status="pending", label="pending check (the next promote-sites fetches it, then render)")
        elif sid:
            e.update(status="pending", label="not fetched yet (the next promote-sites / render fetches it)")
        else:
            host = urlsplit(url).netloc.lower().removeprefix("www.")
            if host in listing_hosts:  # listings' short links to one host: one row, counted
                listing_hosts[host]["n"] += 1
                listing_hosts[host]["label"] = f"named by {listing_hosts[host]['n']} listings' links to {host}, not fetched"
                return
            e.update(status="pending", label="named by a listing, not fetched", n=1)
            listing_hosts[host] = e
        out.append(e)

    for sid, s in sites.items():
        if s.get("place_key") == place_key or sid in scraped:
            add(s["url"], "site list" if not s.get("verify_address") else s.get("found_by") or "found", sid,
                checks.get(sid))
    for sid in scraped:
        if sid not in sites:
            add(scraped[sid]["url"] or sid, "earlier scrape", sid)
    for f in rec.get("found", []):
        if f.get("place_key") == place_key:
            add(f["url"], f.get("found_by") or "found", f["id"], checks.get(f["id"]))
    for r in conn.execute("SELECT comment, ts FROM annotations WHERE entity_type = 'place' AND attribute = 'website' "
                          "AND entity_key = ? AND comment LIKE 'http%' ORDER BY id", (place_key,)):
        add(r["comment"], f"you, {r['ts'][5:10]}")
    for r in inputs["listing_sites"].get(place_key, []):
        add(r["w"], f"{r['source']} listing")
    return out


def own_site_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Named buildings in a station walk zone whose own site gives no usable offer (no active priced `direct` row), each
    with one reason (OWN_SITE_REASONS) and every URL tried with its last outcome. An item leaves the list by itself
    once a site yields priced offers, or when you dismiss it (annotation `own_site`, newer than any URL you added)
    or say it has no website (annotation `website` = none)."""
    from ..areas import in_walk_zone
    from ..config import load_sites
    from ..intake.discover import _records
    from ..intake.render import last_outcomes

    from ..intake.discover import is_aggregator

    priced = {r[0] for r in conn.execute(
        "SELECT DISTINCT place_key, url FROM listings WHERE source = 'direct' AND active = 1 AND price IS NOT NULL "
        "AND kind IS NOT 'site_info' AND place_key IS NOT NULL") if not is_aggregator(r[1])}
    last_ann: dict[tuple[str, str], dict[str, Any]] = {}
    for r in conn.execute("SELECT entity_key, attribute, comment, id FROM annotations WHERE entity_type = 'place' "
                          "AND attribute IN ('website', 'own_site') ORDER BY id"):
        last_ann[(r["entity_key"], r["attribute"])] = dict(r)
    sites, rec, renders, inputs = load_sites(), _records(), last_outcomes(conn), own_site_inputs(conn)
    out = []
    for r in conn.execute("SELECT place_key, property_name, address, lat, lon, n_listings FROM places "
                          "WHERE property_name IS NOT NULL AND address IS NOT NULL ORDER BY n_listings DESC"):
        pk, name = r["place_key"], r["property_name"].strip()
        if pk in priced or not is_named_building(name, r["address"]) or not in_walk_zone(r["lat"], r["lon"]):
            continue
        web, dis = last_ann.get((pk, "website")), last_ann.get((pk, "own_site"))
        if dis and (not web or dis["id"] > web["id"]):
            continue  # you dismissed it, and added no URL since
        urls = own_site_urls(conn, pk, sites=sites, rec=rec, renders=renders, inputs=inputs)
        st = {u["status"] for u in urls}
        if not urls:
            if web and (web["comment"] or "").strip().lower() == "none":
                continue  # you said: no website
            reason = "no_website"
        else:
            reason = next(k for k, s in (("form", "form"), ("address_failed", "rejected"), ("no_plans", "no_plans"),
                                         ("all_failed", "failed"), ("not_checked", "pending")) if s in st)
        out.append({"kind": "own_site", "place_key": pk, "name": name, "address": r["address"],
                    "listings": r["n_listings"], "reason": reason, "reason_label": OWN_SITE_REASONS[reason],
                    "urls": urls})
    return out


DUP_SITE_MAX_M = 600.0  # one own website: the same building's places sit within ~570 m (Central Park at Whisman)
DUP_SITE_MAX_PLACES = 6  # a URL more places share is a manager's site (rentbt.com: 17), not one building's
DUP_NAME_MAX_M = 200.0
DUP_WEIGHT = {"website": 3, "address": 2, "name": 2}
DUP_REASONS = {"address": "similar address", "website": "same own website", "name": "same name"}
_NAME_FILLER = {"the", "apartments", "apartment", "apts", "apt", "homes", "residences", "community"}


def pair_key(a: str, b: str) -> str:
    """The annotation key of a pair of places: both keys, sorted, joined by '|'."""
    return "|".join(sorted((a, b)))


def _site_key(url: str | None) -> str | None:
    """A website as host + path: lowercase, no scheme, www., query, fragment, trailing slash or spaces."""
    u = re.sub(r"\s+", "", url or "").lower()
    if not u:
        return None
    s = urlsplit(u if "://" in u else "https://" + u)
    host = s.netloc.removeprefix("www.")
    return (host + s.path.rstrip("/")) if "." in host else None


def _name_key(name: str | None) -> str | None:
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split() if w not in _NAME_FILLER]
    return " ".join(words) if words and not words[0][0].isdigit() else None


def duplicate_queue(conn: sqlite3.Connection, places: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Pairs of places that are probably one building, each with its reasons, strongest evidence first:
    `address` (identity.addresses.suspected_duplicates: same number, street 85%+ alike, within 150 m), `website`
    (the same own website, host and path, within DUP_SITE_MAX_M: a `direct` site scraped at both, or the website
    a listing names) and `name` (the same property name within DUP_NAME_MAX_M). Third-party stay places (`~`) and
    places you split off (`u_`) are left out. Bucketed by number, URL and name, never all pairs. A pair leaves once
    you merge it (one place is gone) or answer 'not the same building' (annotation place_pair / 'a|b' /
    same_building = no). `places`: rows of the places view already read."""
    from ..commute.geo import haversine_m
    from ..identity.addresses import suspected_duplicates

    if places is None:
        places = [dict(r) for r in conn.execute("SELECT * FROM places")]
    by_key = {p["place_key"]: p for p in places if "~" not in p["place_key"]
              and not p["place_key"].startswith("u_") and p.get("lat") is not None}
    answered = {}
    for r in conn.execute("SELECT entity_key, comment FROM annotations WHERE entity_type = 'place_pair' "
                          "AND attribute = 'same_building' ORDER BY id"):
        answered[r["entity_key"]] = (r["comment"] or "").strip().lower()
    pairs: dict[str, dict[str, Any]] = {}

    def add(a: str, b: str, kind: str, detail: str) -> None:
        if a == b or a not in by_key or b not in by_key:
            return
        k = pair_key(a, b)
        if answered.get(k) == "no":
            return
        e = pairs.setdefault(k, {"pair_key": k, "a": min(a, b), "b": max(a, b), "reasons": []})
        if kind not in {x["kind"] for x in e["reasons"]}:
            e["reasons"].append({"kind": kind, "label": DUP_REASONS[kind], "detail": detail})

    def metres(a: str, b: str) -> float:
        pa, pb = by_key[a], by_key[b]
        return haversine_m((pa["lat"], pa["lon"]), (pb["lat"], pb["lon"]))

    for d in suspected_duplicates(conn, places=list(by_key.values())):
        add(d["a_key"], d["b_key"], "address", f"{d['a']} / {d['b']} ({round(d['similarity'] * 100)}% alike)")
    sites: dict[str, set[str]] = defaultdict(set)
    inputs = own_site_inputs(conn)
    for pk, rows in inputs["scraped"].items():
        for r in rows:
            if r["site"]:
                sites["site " + r["site"]].add(pk)
    for pk, rows in inputs["listing_sites"].items():
        for r in rows:
            if (k := _site_key(r["w"])):
                sites[k].add(pk)
    for url, pks in sites.items():
        ks = sorted(k for k in pks if k in by_key)
        if len(ks) > DUP_SITE_MAX_PLACES:
            continue
        for i, a in enumerate(ks):
            for b in ks[i + 1:]:
                if metres(a, b) <= DUP_SITE_MAX_M:
                    own = url.startswith("site ")
                    add(a, b, "website", f"own site {url[5:]} scraped at both" if own else url)
    names: dict[str, list[str]] = defaultdict(list)
    for pk, p in by_key.items():
        if (n := _name_key(p.get("property_name"))):
            names[n].append(pk)
    for n, ks in names.items():
        for i, a in enumerate(ks):
            for b in ks[i + 1:]:
                if metres(a, b) <= DUP_NAME_MAX_M:
                    add(a, b, "name", by_key[a]["property_name"])
    out = []
    for e in pairs.values():
        pa, pb = by_key[e["a"]], by_key[e["b"]]
        e["metres"] = round(metres(e["a"], e["b"]))
        e["score"] = sum(DUP_WEIGHT[r["kind"]] for r in e["reasons"])
        e["places"] = [{"place_key": p["place_key"], "name": p.get("property_name"), "address": p.get("address"),
                        "n_listings": p.get("n_listings") or 0,
                        "sources": sorted(set((p.get("sources") or "").split(",")) - {""})}
                       for p in (pa, pb)]
        out.append(e)
    return sorted(out, key=lambda e: (-e["score"], e["metres"], e["pair_key"]))


def agreement(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Cross-source agreement per field over multi-source units (free signal, no manual work)."""
    by_unit: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM listings WHERE unit_key IS NOT NULL AND active = 1 "
                          "AND kind IS NOT 'site_info'"):
        by_unit[r["unit_key"]].append(r)
    out = {f: {"agree": 0, "compared": 0} for f in CONFLICT_FIELDS}
    for rows in by_unit.values():
        if len({r["source"] for r in rows}) < 2:
            continue
        for f in CONFLICT_FIELDS:
            vals = [r[f] for r in rows if r[f] is not None]
            if len(vals) < 2:
                continue
            out[f]["compared"] += 1
            out[f]["agree"] += not any(_differs(f, a, b) for i, a in enumerate(vals) for b in vals[i + 1:])
    return out


def stored_value(row: sqlite3.Row, field: str) -> Any:
    """Value of a field in an observation row (price column or data JSON)."""
    if field == "price":
        return row["price"]
    return json.loads(row["data"]).get(field)
