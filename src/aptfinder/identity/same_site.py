"""Automatic merge of obvious duplicate buildings: the same building-specific own website, under 100 m apart.

Run inside `identity.run` on the rule decisions (before they are compared with the links already written), so the
merge is recomputed from the same evidence every run: idempotent, new data gets it, and a pair you answer 'not the
same building' (annotation place_pair / 'a|b' / same_building = no) goes back to its own places on the next run.
A merged listing's link is a rule row with author AUTHOR; a user link (split, drawer merge) still wins over it.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

AUTHOR = "auto-merge:same-site-100m"
MAX_M = 100.0
MAX_PLACES = 6  # a URL more places share is a manager's site, not one building's (as verify.DUP_SITE_MAX_PLACES)
NO_PRECISE = {"airbnb"}  # offsets its map pins on purpose

# a path that names a list of buildings, not one: '/apartments', '/communities/ca/mountain-view', '/search?...'
PORTFOLIO_WORDS = {
    "apartments", "apartment", "communities", "community", "properties", "property", "locations", "location",
    "rentals", "rental", "listings", "listing", "search", "portfolio", "residential", "availability", "available",
    "our-communities", "find-apartments", "find-a-home", "apartments-for-rent", "for-rent", "homes", "buildings",
    "ca", "california", "us", "en", "en-us", "city", "cities", "neighborhoods", "neighborhood", "vacancies",
}
CITY_SLUGS = {
    "palo-alto", "mountain-view", "sunnyvale", "menlo-park", "redwood-city", "san-mateo", "san-carlos", "belmont",
    "burlingame", "millbrae", "san-bruno", "south-san-francisco", "san-francisco", "santa-clara", "san-jose",
    "los-altos", "cupertino", "east-palo-alto", "atherton", "foster-city", "san-francisco-bay-area", "bay-area",
}


def _slug(seg: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", seg.lower()).strip("-")


def is_generic_page(path: str) -> bool:
    """A site's homepage (path '/' or empty) or an operator's portfolio or city page: every path segment is a
    list word or a city ('/apartments/ca/mountain-view', '/mountain-view-apartments', '/communities')."""
    segs = [_slug(s) for s in path.split("/") if s.strip()]
    if not segs:
        return True
    for s in segs:
        bare = re.sub(r"-(apartments?|rentals?|ca|homes)$", "", s)
        bare = re.sub(r"^(apartments?|rentals?)-(in-)?", "", bare)
        if s not in PORTFOLIO_WORDS and bare not in PORTFOLIO_WORDS and bare not in CITY_SLUGS:
            return False
    return True


def building_site_key(url: str | None) -> str | None:
    """config.site_key of a URL that names one building, else None: no aggregator or listing host
    (discover.is_aggregator), no bare homepage (a property manager's covers many buildings: zell.com, caprop.com),
    no portfolio or city page."""
    from ..config import site_key
    from ..intake.discover import is_aggregator

    u = (url or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    if is_aggregator(u):
        return None
    try:
        p = urlsplit(u)
        k = site_key(u)
    except ValueError:
        return None
    if "." not in p.netloc or is_generic_page(p.path):
        return None
    return k


def _pair_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


def _ok_key(pk: str | None) -> bool:
    return bool(pk) and "~" not in pk and not pk.startswith("u_")


def candidates(conn: sqlite3.Connection, decisions: dict, rows: list, geocodes: dict,
               sites: dict | None = None, rec: dict | None = None) -> dict[str, Any]:
    """Pairs of rule places with the same building site within MAX_M. Returns {'pairs': [...], 'skipped': [...]}:
    `skipped` lists URLs shared by places within MAX_M that the generic-page / aggregator / manager-site rule
    dropped (for the dry run)."""
    from ..commute.geo import haversine_m
    from ..config import check_outcome, last_checks, load_sites
    from ..intake.discover import _records

    sites = load_sites(duplicates=True) if sites is None else sites
    rec = _records() if rec is None else rec
    pts: dict[str, list[tuple[float, float]]] = defaultdict(list)
    precise: dict[str, list[tuple[float, float]]] = defaultdict(list)
    n_list: dict[str, int] = defaultdict(int)
    info: dict[str, dict[str, Any]] = {}
    urls: dict[str, dict[str, str]] = defaultdict(dict)  # raw url -> {place: via}
    for r in rows:
        d = decisions.get((r["source"], r["source_id"]))
        if not d or not _ok_key(d[1]):
            continue
        pk = d[1]
        if r["kind"] != "site_info":
            n_list[pk] += 1
        i = info.setdefault(pk, {"address": None, "name": None})
        i["address"] = i["address"] or r["address"]
        i["name"] = i["name"] or r["property_name"]
        if r["lat"] is not None and r["lon"] is not None:
            pts[pk].append((r["lat"], r["lon"]))
            g = geocodes.get(r["address"]) if r["address"] else None
            if g and not g.get("fallback") and r["source"] not in NO_PRECISE:
                precise[pk].append((r["lat"], r["lon"]))
        if r["source"] == "direct":
            sid = str(r["source_id"]).split(":", 1)[0]
            u = (sites.get(sid) or {}).get("url") or r["url"]
            if u:
                urls[u].setdefault(pk, f"own site {sid} scraped")
        elif r["website"]:
            urls[r["website"]].setdefault(pk, f"named by a {r['source']} listing")
    rejected = {i for i, c in last_checks(rec).items() if check_outcome(c) == "rejected"}
    canon: dict[str, str] = {}  # building site key -> the canonical site-list entry's place
    for sid, s in sites.items():
        if s.get("place_key"):
            urls[s["url"]].setdefault(s["place_key"], f"site list {sid}")
            if not s.get("duplicate_of") and (k := building_site_key(s["url"])):
                canon.setdefault(k, s["place_key"])
    for f in rec.get("found", []):
        if f.get("place_key") and f.get("id") not in rejected:
            urls[f["url"]].setdefault(f["place_key"], f"found {f.get('id')}")

    centre = {pk: (sum(p[0] for p in v) / len(v), sum(p[1] for p in v) / len(v)) for pk, v in pts.items()}

    def metres(a: str, b: str) -> float:
        if precise.get(a) and precise.get(b):
            return min(haversine_m(x, y) for x in precise[a] for y in precise[b])
        return haversine_m(centre[a], centre[b])

    answered = {}
    for r in conn.execute("SELECT entity_key, comment FROM annotations WHERE entity_type = 'place_pair' "
                          "AND attribute = 'same_building' ORDER BY id"):
        answered[r["entity_key"]] = (r["comment"] or "").strip().lower()

    from ..config import site_key

    by_key: dict[str, dict[str, str]] = defaultdict(dict)  # building site key -> {place: url}
    plain: dict[str, dict[str, str]] = defaultdict(dict)  # any site key (for the skipped report)
    for u, pks in urls.items():
        k = building_site_key(u)
        for pk in pks:
            if pk not in centre:
                continue
            if k:
                by_key[k].setdefault(pk, u)
            else:
                try:
                    plain[site_key(u if "://" in u else "https://" + u)].setdefault(pk, u)
                except ValueError:
                    pass
    pairs, skipped, seen = [], [], set()
    for k, pks in by_key.items():
        ks = sorted(pks)
        for i, a in enumerate(ks):
            for b in ks[i + 1:]:
                if (pr := _pair_key(a, b)) in seen or (m := metres(a, b)) >= MAX_M:
                    continue
                if len(ks) > MAX_PLACES:
                    skipped.append({"url": pks[a], "why": f"shared by {len(ks)} places", "a": a, "b": b,
                                    "metres": round(m)})
                    continue
                if answered.get(pr) == "no":
                    skipped.append({"url": pks[a], "why": "you answered not the same building", "a": a, "b": b,
                                    "metres": round(m)})
                    continue
                seen.add(pr)
                pairs.append({"a": a, "b": b, "metres": round(m, 1), "url": pks[a], "key": k,
                              "precise": bool(precise.get(a) and precise.get(b)),
                              "canonical": canon.get(k), "via": [urls[pks[a]].get(a), urls[pks[b]].get(b)]})
    for k, pks in plain.items():
        ks = sorted(pks)
        for i, a in enumerate(ks):
            for b in ks[i + 1:]:
                if (m := metres(a, b)) < MAX_M and _pair_key(a, b) not in seen:
                    skipped.append({"url": pks[a], "why": "homepage, portfolio page or aggregator", "a": a, "b": b,
                                    "metres": round(m)})
    return {"pairs": pairs, "skipped": skipped, "info": info, "n_listings": dict(n_list)}


def merge_map(pairs: list[dict[str, Any]], n_listings: dict[str, int],
              prev_kept: set[str] | frozenset = frozenset()) -> dict[str, tuple[str, dict[str, Any]]]:
    """{merged place: (kept place, the pair that joined it)}. Pairs join into groups (union-find); each group keeps
    the place it kept last run, else the canonical site-list entry's place, else the one with more listings."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    for p in pairs:
        parent[find(p["a"])] = find(p["b"])
    groups: dict[str, list[str]] = defaultdict(list)
    for x in list(parent):
        groups[find(x)].append(x)
    canon = {p["canonical"] for p in pairs if p.get("canonical")}
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for members in groups.values():
        kept = min(members, key=lambda x: (x not in prev_kept, x not in canon, -n_listings.get(x, 0), x))
        for x in members:
            if x != kept:
                via = next(p for p in pairs if x in (p["a"], p["b"]))
                out[x] = (kept, via)
    return out


def apply(decisions: dict, mapping: dict[str, tuple[str, dict[str, Any]]]) -> int:
    """Re-point the rule decisions of merged places at the kept place (in place); each listing keeps its unit,
    '<merged>/<unit>' -> '<kept>/<unit>', as the drawer's merge does. Returns the listings moved."""
    n = 0
    for key, d in decisions.items():
        if not d or d[1] not in mapping:
            continue
        pk, uk = d[1], d[2]
        kept, via = mapping[pk]
        unit = kept + uk[len(pk):] if uk and uk.startswith(pk) else kept
        decisions[key] = (AUTHOR, kept, unit,
                          f"same own website {via['key']}, {via['metres']:.0f} m apart: {pk} merged into {kept}")
        n += 1
    return n
