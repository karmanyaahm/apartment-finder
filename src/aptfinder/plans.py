"""Floor plan identity across sources, shared by the filter and the dashboard.

Two listings are the same plan when they are at the same place with the same beds, baths and size (to 10 sqft)
and the same kind of offer (a lease, a furnished lease such as AvalonBay Furnished+, or a furnished short stay
such as Blueground's units inside a building).
Without a size, the plan name decides.
"""

from __future__ import annotations

import re
from typing import Any


def plan_key(f: dict[str, Any]) -> str:
    beds, baths = f.get("beds"), f.get("baths")
    size = f"{'studio' if beds == 0 else (f'{beds:g}bd' if beds is not None else '?bd')}/" \
           f"{f'{baths:g}' if baths is not None else '?'}ba"
    # three offers never share a row: an ordinary lease, a furnished lease (e.g. AvalonBay Furnished+ on the
    # same unit at a higher rent), and a furnished short stay (Blueground and its listing template)
    offer = "stay" if f.get("term") == "short" else "furnished" if f.get("furnished") else "lease"
    if f.get("sqft"):
        return f"{offer}|{size}|{round(f['sqft'] / 10) * 10:.0f}sqft"
    return f"{offer}|{size}|{(f.get('floor_plan') or '').strip().lower()}"


def inherit_prices(rows: list[dict[str, Any]]) -> None:
    """In place: a listing without a monthly price takes the lowest price another listing gives for the same
    plan at the same place (e.g. a community site's plan without prices, priced on Apartments.com).
    Sets `price_from` so the reason can say where the number came from."""
    best: dict[tuple[str, str], tuple[float, str]] = {}
    for r in rows:
        if r.get("place_key") and r.get("price_month"):
            k = (r["place_key"], plan_key(r))
            if k not in best or r["price_month"] < best[k][0]:
                best[k] = (r["price_month"], r["source"])
    for r in rows:
        if r.get("place_key") and not r.get("price_month"):
            hit = best.get((r["place_key"], plan_key(r)))
            if hit:
                r["price_month"], r["price_from"] = hit
                if r.get("beds"):
                    r["price_per_bed"] = hit[0] / r["beds"]


def is_own_priced(r: dict[str, Any]) -> bool:
    """An active, priced offer from the building's own website (`direct`, not a `site_info` photo/link row)."""
    return (r.get("source") == "direct" and bool(r.get("active")) and r.get("price") is not None
            and r.get("kind") != "site_info")


def own_site_places(conn) -> set[str]:
    """Places whose own website currently prices at least one offer. There the own site is the truth: goals are
    judged on its rows alone, and other sources' listings are shown as 'also listed' (design.md, 'own website
    wins')."""
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT place_key FROM listings WHERE source = 'direct' AND active = 1 AND price IS NOT NULL "
        "AND kind IS NOT 'site_info' AND place_key IS NOT NULL")}


# ------------------------------------------------------------------------------------------ floor plan drawings
def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower()))


def match_drawing(name: str | None, drawings: dict[str, str]) -> str | None:
    """The drawing whose label names this plan, when exactly one does: `drawings` is {label: URL} (a page's alt
    text and file name, or a site's own plan-name map). '1x1-770' names only '1x1-770 ... .png', not '1x1-763';
    a name two or more labels share (e.g. '1x1') gets nothing."""
    n = _norm(name or "")
    if len(n) < 2:
        return None
    hits = {u for label, u in drawings.items() if f" {n} " in f" {_norm(label)} "}
    return hits.pop() if len(hits) == 1 else None


def plan_image(ls: list[dict[str, Any]], drawings: dict[str, str], skip: tuple[str, ...] = ()) -> str | None:
    """One drawing for a plan (its listings `ls`, as dicts with `source` and `data`): the own website's first
    (a row's `extra.floorplan_image`, else a drawing of the site's pages whose label names the plan), then any
    other source's `extra.floorplan_image`. URLs on hosts in `skip` (they refuse hotlinking) are never shown."""
    def ok(u: Any) -> bool:
        return isinstance(u, str) and u.startswith("http") and not any(h in u for h in skip)

    own = [l for l in ls if l["source"] == "direct"]
    for l in own:
        if ok(u := (l["data"].get("extra") or {}).get("floorplan_image")):
            return u
    for l in own + [l for l in ls if l["source"] != "direct"]:
        if ok(u := match_drawing(l["data"].get("floor_plan") or (l.get("fields") or {}).get("floor_plan"), drawings)):
            return u
    for l in ls:
        if ok(u := (l["data"].get("extra") or {}).get("floorplan_image")):
            return u
    return None


def site_drawings(listings: list[dict[str, Any]]) -> dict[str, str]:
    """{label: URL} from a place's own-site `site_info` rows (`extra.plan_drawings`; older rows kept a list of
    names only, which carries no URL)."""
    out: dict[str, str] = {}
    for l in listings:
        d = (l["data"].get("extra") or {}).get("plan_drawings") if l["source"] == "direct" else None
        if isinstance(d, dict):
            out.update({k: v for k, v in d.items() if isinstance(v, str)})
    return out
