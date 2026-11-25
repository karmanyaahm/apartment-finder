"""filter: evaluates goals over the derived views and trips at read time. Writes nothing."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from typing import Any

from ..config import Goal, GoalsConfig, load_goals, match_operators, operator_restriction
from ..plans import inherit_baths, inherit_prices, is_own_priced
from ..verify import accuracy, low_accuracy
from .building import building_note, building_units_by_place, place_building_units
from .rules import (CHECK, FAIL, NO_LISTINGS, PASS, RANK, Ctx, combine, commute_trips, evaluate, fact, in_net,
                    parking_cost)

PROVIDER_PREF = {"google": 0, "knn": 1, "estimate": 9}


class _Trips(defaultdict):
    """A third-party stay place (`<building>~<operator>`) is at its building's address: it reads the building's
    trips instead of having its own routed (identity.stay_key). Where the stay also has old trips of its own
    (estimates appended before the stay rule), the building's trip wins for each (point, mode) it has, so the
    stay's estimate never hides the building's Google or KNN time; the stay's own fill only what the building lacks.
    """

    def get(self, key, default=None):
        base = key.split("~", 1)[0] if key and "~" in key else None
        if base in self:
            return {**self[key], **self[base]} if key in self else self[base]
        if key in self:
            return self[key]
        return default


def best_trips(conn: sqlite3.Connection, located: dict[str, tuple[float, float]] | None = None
               ) -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    """place_key -> (point_id, mode) -> latest trip from the most trusted provider with a time, among trips routed from
    the place's present position (trips.is_current): the place's own Google trip, else a KNN range from nearby places' Google trips (filter.knn), else the straight-line estimate, else (no
    estimate: transit) the middle of the nearest Google times at any distance (knn.far_estimate). `located`: place_key
    -> (lat, lon) of the located places, already read (the places view is computed over everything)."""
    from ..trips import is_current
    from .knn import add_knn, cells

    out: dict[str, dict[tuple[str, str], dict[str, Any]]] = _Trips(dict)
    rows = conn.execute("""
        SELECT * FROM (SELECT t.*, ROW_NUMBER() OVER (
            PARTITION BY place_key, point_id, mode, provider ORDER BY id DESC) rn FROM trips t) WHERE rn = 1
    """).fetchall()
    if located is None:
        located = {pk: (la, lo) for pk, la, lo in
                   conn.execute("SELECT place_key, lat, lon FROM places WHERE lat IS NOT NULL")}
    for r in rows:
        if not is_current(r, located.get(r["place_key"])):
            continue  # routed from an old position (or before origins were kept): estimate or KNN until re-routed
        key = (r["point_id"], r["mode"])
        cur = out[r["place_key"]].get(key)
        # a trip with a time beats one without (Google found no route: the estimate, or KNN, decides instead)
        if cur is None or ((r["minutes"] is None, PROVIDER_PREF.get(r["provider"], 5))
                           < (cur["minutes"] is None, PROVIDER_PREF.get(cur["provider"], 5))):
            out[r["place_key"]][key] = dict(r)
    sel = cells(conn)
    if sel:
        add_knn(out, located, sel)
    return out


def nearest_by_set(trips: dict[tuple[str, str], dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """(set, mode) -> the fastest trip to any point of that set."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for (point_id, mode), t in trips.items():
        if t["minutes"] is None:
            continue
        key = (point_id.split(":", 1)[0], mode)
        if key not in out or t["minutes"] < out[key]["minutes"]:
            out[key] = t
    return out


def place_annotations(conn: sqlite3.Connection) -> dict[str, dict[str, dict[str, Any]]]:
    """place_key -> attribute -> latest annotation (any author, any goal: a rating is a fact about the building,
    shared by every goal; old rows may still carry a goal_id, which nothing reads). Its `rating` is the latest
    rating of that attribute, so a comment typed after a rating does not hide it."""
    out: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in conn.execute("SELECT * FROM annotations WHERE entity_type = 'place' ORDER BY id"):
        a, prev = dict(r), out[r["entity_key"]].get(r["attribute"].lower())
        if a["rating"] is None and prev:
            a["rating"] = prev["rating"]
        out[r["entity_key"]][r["attribute"].lower()] = a
    return out


def goal_inputs(conn: sqlite3.Connection, cfg: GoalsConfig | None = None, trips: dict | None = None) -> dict[str, Any]:
    """Everything a goal is judged on, read once: the offer rows (amenities pooled per place), trips, place
    annotations, low-accuracy source fields and the places whose own site prices offers. Reused by evaluate_all
    for every goal and by the dashboard's draft preview (evaluate_goal on one unsaved goal). `trips`: a best_trips
    result already in hand (the dashboard's cached one), read only."""
    cfg = cfg or load_goals()
    trips = trips if trips is not None else best_trips(conn)
    anns = place_annotations(conn)
    low = low_accuracy(accuracy(conn), cfg.verify.min_samples, cfg.verify.min_accuracy)
    # site_info rows (a community website's photos and link) are not offers: no goal applies to them
    # nor are a community site's plans without a price (Prometheus plan drawings, Landsby's plan list): the plan
    # exists, but no unit in it is known to be offered, so it shows in the drawer and does not count for goals
    # (those unpriced own-site plans are kept aside as `unpriced`: they decide nothing, but a plan that fits the goal
    # there makes the place 'no listings' rather than a fail; place_outcome)
    listings, unpriced = [], []
    for r in conn.execute("SELECT * FROM listings WHERE kind IS NOT 'site_info'"):
        (unpriced if r["source"] == "direct" and r["price"] is None else listings).append(dict(r))
    inherit_prices(listings)  # a price-less plan row takes the same plan's price from another source
    # an own-site plan row without baths borrows them from the same plan on another source, else from its own
    # floor-plan drawing as read by aptfinder.planread (lower trust: the reason names where they came from)
    from ..planread import readings as plan_readings

    inherit_baths(listings + unpriced, plan_readings(conn))
    listing_operators(listings + unpriced)  # who rents each offer out (a `not_operator` rule reads it)
    # own website wins: where the building's own site prices an offer, only its active priced rows decide the
    # place's goals; other sources' listings there still get a result (shown in the drawer) but decide nothing
    own = {r["place_key"] for r in listings if r["place_key"] and is_own_priced(r)}
    # amenities belong to the building: every listing at a place is judged on all its sources' amenity lists
    # (an own-site plan row has none; Apartments.com lists the building's "Fitness Center")
    # so is a limit on who may rent ('restriction', read by `aptf-intake extract` and from an own site's pages by
    # render): a limit any listing or site_info row states ('PBV - Belovida (VASH)') holds for every listing at the
    # place; otherwise each listing's own reading ('none'), or None when it has not been read (unknown: an `equals
    # restriction none` rule says needs_check). Your answer in Verify (annotation place / restriction) beats both.
    # The place's operator (`config.operator_restriction`: its own site's domain or the operator field) fills in
    # below a quoted limit: an affordable-housing one says income (unless a page there says market-rate in so many
    # words), over a listing's none; a market-rate one says none where nothing was read (unknown). Nothing read at
    # all: assumed none (`ASSUMED_NONE`, the user's decision 2026-11-25: no limit unless one is stated), so an
    # `equals restriction none` rule passes; only a stated limit, an affordable operator or your answer restricts.
    # parking: a row's own reading (extract's, then its site's pages) first, field by field; what it leaves unknown
    # comes from a building statement at the place (an own site's pages, a building-level source: `parking_pool`)
    pooled: dict[str, set[str]] = {}
    rx_of: dict[tuple[str, str], dict[str, Any]] = {}
    limits: dict[str, list[dict[str, Any]]] = {}
    op_income: dict[str, dict[str, Any]] = {}
    op_market: dict[str, dict[str, Any]] = {}
    market: set[str] = set()
    pk_of: dict[tuple[str, str], list[dict[str, Any]]] = {}
    pk_place: dict[str, list[dict[str, Any]]] = {}
    places: set[str] = set()  # every place, site_info-only ones included: a place with no offer is 'no listings'
    gone: dict[tuple[str, str], str] = {}  # a row its source marks unavailable (unavailable_label)
    min_stays: dict[tuple[str, str], tuple[float, str]] = {}  # the source's stated minimum stay (min_stay)
    for r in conn.execute("SELECT source, source_id, place_key, amenities, url, operator, floor_plan, "
                          "json_extract(data, '$.extra.availability') AS avail, "
                          "json_extract(data, '$.extra.available_label') AS avail_label, "
                          "json_extract(data, '$.extra.website') AS website, "
                          "json_extract(data, '$.extra.extracted.restriction') AS rx, "
                          "json_extract(data, '$.extra.page_restriction') AS prx, "
                          "json_extract(data, '$.extra.extracted.parking') AS pkx, "
                          "json_extract(data, '$.extra.page_parking') AS ppk, "
                          "json_extract(data, '$.extra.min_stay_days') AS msd, "
                          "json_extract(data, '$.extra.minimum_stay_days') AS mnsd, "
                          "json_extract(data, '$.extra.min_lease_days') AS mld, "
                          "json_extract(data, '$.extra.min_lease_months') AS mlm FROM listings"):
        if r["place_key"]:
            places.add(r["place_key"])
        ms = min_stay(r["msd"], r["mnsd"], r["mld"], r["mlm"])
        if ms:
            min_stays[(r["source"], r["source_id"])] = ms
        why = unavailable_label(r["avail"], r["avail_label"], r["floor_plan"])
        if why:
            gone[(r["source"], r["source_id"])] = why
        own_pk, building = row_parking(json.loads(r["pkx"]) if r["pkx"] else None,
                                       json.loads(r["ppk"]) if r["ppk"] else None, r["source"], r["source_id"])
        if own_pk:
            pk_of[(r["source"], r["source_id"])] = own_pk
        if building and r["place_key"]:
            pk_place.setdefault(r["place_key"], []).extend(building)
        pk, am = r["place_key"], r["amenities"]
        if pk and am and am != "[]":
            pooled.setdefault(pk, set()).update(json.loads(am) if isinstance(am, str) else am)
        rx = row_restriction(json.loads(r["rx"]) if r["rx"] else None, json.loads(r["prx"]) if r["prx"] else None,
                             r["source"])
        if rx:
            rx_of[(r["source"], r["source_id"])] = rx
            if pk and rx.get("value") not in (None, "none"):
                limits.setdefault(pk, []).append(rx)
            if pk and rx.get("value") == "none" and rx.get("evidence"):
                market.add(pk)
        op = pk and operator_restriction(own_site_urls(r["source"], r["url"], r["website"]), [r["operator"]])
        if op:
            (op_market if op["value"] == "none" else op_income).setdefault(pk, op)
    # how many units the building has (filter.building: a stated count, a house type, or a floor from the units and
    # plans one source lists): a place-level fact every listing there is judged on, like pooled amenities
    bu = building_units_by_place(conn)
    for r in listings + unpriced:
        r["unavailable"] = gone.get((r["source"], r["source_id"]))
        r["min_stay_days"], r["min_stay_evidence"] = min_stays.get((r["source"], r["source_id"]), (None, None))
        b = bu.get(r["place_key"] or "")
        r["building_units"] = b["value"] if b else None
        r["building_units_floor"] = bool(b and b["floor"])
        r["building_units_evidence"] = building_note(b)
        r["is_apartment_building"] = b["apartment_building"] if b else None  # filter.building, 5+ units or the type
        r["is_apartment_building_evidence"] = b["apartment_evidence"] if b else None
        if r["place_key"] in pooled:
            own_am = json.loads(r["amenities"]) if isinstance(r.get("amenities"), str) else (r.get("amenities") or [])
            r["amenities"] = sorted(pooled[r["place_key"]] | set(own_am))
        mine = rx_of.get((r["source"], r["source_id"])) or {}
        rx = mine if mine.get("value") not in (None, "none") else (limits.get(r["place_key"]) or [mine])[0]
        if rx.get("value") in (None, "none") and r["place_key"] in op_income and r["place_key"] not in market:
            rx = op_income[r["place_key"]]
        elif rx.get("value") is None and r["place_key"] in op_market:
            rx = op_market[r["place_key"]]
        if rx.get("value") is None:
            rx = ASSUMED_NONE
        rx = annotated_restriction(anns.get(r["place_key"] or "", {})) or rx
        r["restriction"] = rx.get("value")
        r["restriction_evidence"] = restriction_note(rx)
        # the ordered readings stay on the row: a parking_cost rule reads one reading whole (rules.parking_cost)
        r["parking_readings"] = (pk_of.get((r["source"], r["source_id"]), []) +
                                 sorted(pk_place.get(r["place_key"] or "", []), key=lambda x: not x["page"]))
        r.update(parking_fields(r["parking_readings"]))
    return {"listings": listings, "unpriced": unpriced, "places": places, "trips": trips, "anns": anns, "low": low,
            "own": own}


# What a source says when a plan has nothing to rent: Apartments.com's "0 Available units" and its "No availability"
# plan name, an own site's "Unavailable on selected date", "Call for details", "Contact for Availability",
# "WAITLIST CLOSED". Structured fields only, never a description ("parking is currently unavailable").
UNAVAILABLE = re.compile(r"^0 available|^no availab|unavailable|waitlist|wait list|call for|contact for|sold out", re.I)


def min_stay(min_stay_days: Any, minimum_stay_days: Any, min_lease_days: Any, min_lease_months: Any
             ) -> tuple[float, str] | None:
    """(days, which field) of the shortest stay the source allows, from its structured fields only: `min_stay_days`
    (Airbnb, Furnished Finder, Zillow, Craigslist parsers), Furnished Finder's `minimum_stay_days`, Zumper's
    `min_lease_days`, Apartments.com's `min_lease_months` (x 30). Haiku's `lease_term_months` is 'the lease length
    offered', not a minimum, so it is not read. None when none is stated."""
    for v, name, mult in ((min_stay_days, "min_stay_days", 1), (minimum_stay_days, "minimum_stay_days", 1),
                          (min_lease_days, "min_lease_days", 1), (min_lease_months, "min_lease_months", 30)):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return float(v) * mult, name
    return None


def unavailable_label(availability: str | None, label: str | None, floor_plan: str | None) -> str | None:
    """The source's own words when it says this plan has nothing to rent (UNAVAILABLE), else None."""
    for v in (availability, label, floor_plan):
        if isinstance(v, str) and UNAVAILABLE.search(v.strip()):
            return v.strip()
    return None


def listing_operators(listings: list[dict[str, Any]]) -> None:
    """Set each row's `operators`, {configured operator name: how this offer names it}, per listing (never pooled
    per place: Blueground reposts one unit of a large complex). What names the lister (`config.match_operators`,
    names exact any case, or an own-site domain): the operator field; Apartments.com's management company; Zumper's
    brokerage; a PadMapper row's Zumper twin (the same listing id, one company); a Zillow unit's detail page broker
    or agent (raw page, `zillow_detail.page_listers`; a building's bedroom rows `<zpid>:Nbd` are not read, their
    page is one representative unit's). Nothing found: {} (a not_operator rule passes)."""
    from ..intake.zillow_detail import page_listers

    def names(r: dict[str, Any], d: dict[str, Any]) -> list[tuple[str, str]]:
        ex = d.get("extra") or {}
        got = [(r.get("operator"), "operator field"), (ex.get("management_company"), "management company"),
               (ex.get("brokerage"), "brokerage")]
        return [(str(n), f"{how} '{n}' ({r['source']})") for n, how in got if n]

    data = {id(r): (json.loads(r["data"]) if isinstance(r.get("data"), str) else r.get("data") or {}) for r in listings}
    zumper = {r["source_id"]: names(r, data[id(r)]) for r in listings if r["source"] == "zumper"}
    listers = page_listers() if any(r["source"] == "zillow" for r in listings) else {}
    for r in listings:
        d = data[id(r)]
        cands = names(r, d)
        if r["source"] == "padmapper":
            cands += [(n, f"{how}, same listing as Zumper {r['source_id']}") for n, how in zumper.get(r["source_id"], [])]
        if r["source"] == "zillow" and ":" not in r["source_id"]:
            z = str((d.get("extra") or {}).get("zpid") or r["source_id"])
            cands += [(n, f"Zillow page lister '{n}'") for n in listers.get(z, [])]
        ops: dict[str, str] = {}
        for n, how in cands:
            for o in match_operators([], [n]):
                ops.setdefault(o["name"], how)
        urls = own_site_urls(r["source"], r.get("url"), (d.get("extra") or {}).get("website"))
        for o in match_operators(urls):
            ops.setdefault(o["name"], f"own site {urls[0]}")
        r["operators"] = ops


def row_restriction(extracted: dict[str, Any] | None, page: dict[str, Any] | None,
                    source: str) -> dict[str, Any] | None:
    """One row's reading of who may rent: a limit from extract's reading or from its own site's pages (render,
    `extra.page_restriction`, source '<source> page') first, then either's 'none'; None when neither read it."""
    got = [({**x, "source": src}) for x, src in ((extracted, source), (page, f"{source} page"))
           if isinstance(x, dict) and x.get("value")]
    return next((x for x in got if x["value"] != "none"), got[0] if got else None)


ASSUMED_NONE = {"value": "none", "evidence": "no limit stated — assumed none", "source": "assumed"}


def own_site_urls(source: str, url: str | None, website: str | None) -> list[str]:
    """The URLs a row gives for its building's own site: a direct or portfolio row's page, and any `extra.website`
    (Google Places' website, extract's website read from a listing's text)."""
    return [u for u in ((url if source in ("direct", "portfolio") else None), website) if u]


# sources whose text describes a whole building or community, so their parking holds for every unit there; a
# Craigslist, Airbnb, Zumper or PadMapper post (or a Zillow unit) speaks for its own home only
BUILDING_SOURCES = {"apartments_com", "rentcafe", "direct", "google_places", "portfolio"}
PARKING_FIELDS = ("parking_included", "parking_spots", "parking_usd_month")


def row_parking(extracted: dict[str, Any] | None, page: dict[str, Any] | None, source: str, source_id: str
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(this row's parking readings, those that pool to its place). Each reading is the checked `parking` object
    plus `source` ('direct page' for an own site's pages) and `page`. A page reading always pools (a community's
    site); extract's reading pools when the source describes a building (`BUILDING_SOURCES`, Zillow building rows
    '<zpid>:2bd')."""
    got = [{**x, "source": src, "page": is_page} for x, src, is_page in
           ((extracted, source, False), (page, f"{source} page", True)) if isinstance(x, dict) and x.get("evidence")]
    building = source in BUILDING_SOURCES or (source == "zillow" and ":" in source_id)
    return got, [x for x in got if x["page"] or building]


def parking_values(p: dict[str, Any]) -> dict[str, Any]:
    """The rule fields of one parking reading. included: True when a space comes with the rent at no extra charge,
    False when the text says no parking, street only or paid, None when the cost is not stated. spots: the stated
    number (0 for no parking). usd_month: 0 when included, the price when paid, else None."""
    if p.get("offered") == "no":
        return {"parking_included": False, "parking_spots": p.get("spots") or 0, "parking_usd_month": None}
    if p.get("offered") != "yes":
        return dict.fromkeys(PARKING_FIELDS)
    cost = p.get("cost")
    return {"parking_included": True if cost == "included" else False if cost == "paid" else None,
            "parking_spots": p.get("spots"),
            "parking_usd_month": 0 if cost == "included" else p.get("usd_month") if cost == "paid" else None}


def parking_note(p: dict[str, Any]) -> str:
    """'"Garage parking $75/month" (direct page)': the quotes and their source, for reasons and hovers."""
    return f'"{" | ".join(p.get("evidence") or [])}" ({p.get("source")})'


def parking_fields(readings: list[dict[str, Any]]) -> dict[str, Any]:
    """Each parking rule field from the first reading that knows it (the row's own first, then its place's
    building statements), with that reading's quote as `<field>_evidence`; None (unknown) when none does."""
    out: dict[str, Any] = dict.fromkeys(PARKING_FIELDS)
    for p in readings:
        for k, v in parking_values(p).items():
            if out[k] is None and v is not None:
                out[k], out[f"{k}_evidence"] = v, parking_note(p)
    return out


def place_parking(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The place's parking for its card: the most informative reading of any listing there (cost stated, then a
    number of spaces, then offered yes / no; an own site's pages first on a tie), or None when none says anything.
    `rows`: listings with `data` parsed."""
    best, rank = None, None
    for x in rows:
        extra = (x.get("data") or {}).get("extra") or {}
        mine, _ = row_parking((extra.get("extracted") or {}).get("parking"), extra.get("page_parking"),
                              x["source"], x["source_id"])
        for p in mine:
            r = (p.get("cost") is not None or p.get("offered") == "no", p.get("spots") is not None,
                 p.get("offered") is not None, p["page"])
            if rank is None or r > rank:
                best, rank = p, r
    return best


def annotated_restriction(anns: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Your answer on who may rent a place (Verify: annotation place / restriction, comment the value), else None."""
    a = anns.get("restriction")
    v = (a.get("comment") or "").strip().lower() if a else ""
    return {"value": v, "evidence": "marked in Verify", "source": "you"} if v else None


def restriction_note(rx: dict[str, Any] | None) -> str | None:
    """'"Income restricted" (zillow)': the quote that states a limit and its source, for reasons and hovers."""
    if not rx or rx.get("value") in (None, "none"):
        return None
    return f'"{rx.get("evidence") or ""}" ({rx.get("source")})'


def place_restriction(rows: list[dict[str, Any]], anns: dict[str, dict[str, Any]] | None = None
                      ) -> dict[str, Any] | None:
    """The limit on who may rent that any of a place's listings states ({value, evidence, source}), else an
    affordable-housing operator's (`config.operator_restriction`) unless a page there says market-rate, else a
    'none' with its basis (a page's market-rate quote, a market-rate operator), else None (nothing stated: the goals
    assume none); your Verify answer (`anns`: the place's latest annotations) first. `rows`: listings with `data`
    parsed. The card shows a badge only for a limit."""
    mark = annotated_restriction(anns or {})
    if mark:
        return mark if mark["value"] != "none" else None
    op, market = None, None
    for x in rows:
        extra = (x.get("data") or {}).get("extra") or {}
        rx = row_restriction((extra.get("extracted") or {}).get("restriction"), extra.get("page_restriction"),
                             x["source"])
        if rx and rx["value"] != "none":
            return rx
        market = market or (rx if rx and rx.get("evidence") else None)
        got = operator_restriction(own_site_urls(x["source"], x.get("url"), extra.get("website")), [x.get("operator")])
        op = op if op and op["value"] != "none" else got or op
    if op and op["value"] != "none" and not market:
        return op
    return market or (op if op and op["value"] == "none" else None)


def evaluate_goal(inp: dict[str, Any], goal: Goal) -> tuple[dict[tuple[str, str], Any], dict[str, Any]]:
    """(listing results, place results) of one goal over goal_inputs. A listing is pass, needs_check or fail; a place
    is that too, or `no_listings` (place_outcome)."""
    listings, trips, anns, low, own = inp["listings"], inp["trips"], inp["anns"], inp["low"], inp["own"]
    commute = net_rules(goal)[0]  # the terms of a net_commute rule
    # a parking_cost rule (must or nice): each result carries its cost for the cards and the sort
    pc = next((r for r in [*goal.rules, *goal.nice] if r["type"] == "parking_cost"), None)
    lres: dict[tuple[str, str], Any] = {}
    by_place: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    unpriced = inp.get("unpriced") or []
    for i, row in enumerate([*listings, *unpriced]):
        pk = row["place_key"]
        t = trips.get(pk, {})
        ctx = Ctx(trips=t, nearest=nearest_by_set(t), annotations=anns.get(pk, {}),
                  untrusted={f for (src, f) in low if src == row["source"]}, commute_rules=commute,
                  people=goal.people)
        # (status, text, rule type): the type lets callers set aside commute rules (routing_candidates)
        reasons = [(*evaluate(r, row, ctx), r["type"]) for r in goal.rules]
        nice = [(*evaluate(r, row, ctx), r["type"]) for r in goal.nice]
        # an unpriced own-site plan (goal_inputs) is an own-site row: it decides wherever it is, but only whether the
        # place is 'no listings' or a fail (place_outcome); it has no listing result
        decides = pk not in own or is_own_priced(row) or i >= len(listings)
        # the attribute each non-pass rule judges (rules.fact), in reasons order: cards mark it red or amber
        fails = [{"s": s, **fact(r, row, goal.people)} for r, (s, _, _) in zip(goal.rules, reasons) if s != PASS]
        res = {"status": combine([s for s, _, _ in reasons]), "reasons": reasons, "fails": fails, "nice": nice,
               **({"parking_cost": parking_cost(row.get("parking_readings") or [], pc["spots"])} if pc else {}),
               "nice_score": sum(s == PASS for s, _, _ in nice), "source": row["source"],
               "source_id": row["source_id"], "unit_key": row["unit_key"], "place_key": pk,
               "decides": decides, "own_site": pk in own, "active": bool(row.get("active"))}
        if i < len(listings):
            lres[(row["source"], row["source_id"])] = res
        if pk is not None and decides:
            by_place[pk].append((res, row))
    pres = {}
    for pk in (inp.get("places") or by_place):
        rs = by_place.get(pk, [])
        pres[pk] = place_outcome(rs, pk)
        o = goal_offer(rs)
        if o:
            pres[pk] = {**pres[pk], "offer": o}
    return lres, pres


def goal_offer(rs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any] | None:
    """The offer a place's card shows for a goal: among its deciding priced rows (active ones where any is active),
    the cheapest of the best status (a pass, else needs_check, else any): {price_month, price_per_bed, beds, baths,
    source, source_id}. None without a priced row. Not the place's cheapest unit of any size (a 1bd's $3,799 under
    a 3bd goal)."""
    if any(res["active"] for res, _ in rs):
        rs = [(res, row) for res, row in rs if res["active"]]
    priced_rows = [(res, row) for res, row in rs if row.get("price_month") is not None]
    if not priced_rows:
        return None
    res, row = min(priced_rows, key=lambda x: (RANK[x[0]["status"]], x[1]["price_month"]))
    return {"price_month": row["price_month"], "price_per_bed": row.get("price_per_bed"), "beds": row.get("beds"),
            "baths": row.get("baths"), "source": row["source"], "source_id": row["source_id"]}


NO_OFFERS = "no offers known"


def place_outcome(rs: list[tuple[dict[str, Any], dict[str, Any]]], pk: str) -> dict[str, Any]:
    """A place's result for a goal from its deciding (listing result, row) pairs. A plan is a row with beds or a price
    known (a building row with neither, a RentCafe or Google Places entry, offers nothing); an offer is a plan with
    a price that its source does not mark unavailable. Some offer does not fail: the best offer (pass, then
    needs_check, then the most nice-to-haves). Only active rows count where any is active. Else some plan does not fail (beds/baths pass or unknown and no other
    rule fails) but none of those is an offer: `no_listings`, `why` 'no_price' or 'unavailable' (every fitting plan
    is marked unavailable). Else plans that all fail: the best of them, a fail. No plan at all: `no_listings`,
    `why` 'none' (nothing known for rent). Never a rejection: the place stays, in its own bucket."""
    def best(xs: list[dict[str, Any]]) -> dict[str, Any]:
        return min(xs, key=lambda r: (RANK[r["status"]], -r["nice_score"]))

    if any(res["active"] for res, _ in rs):  # active listings decide; gone ones only where nothing is active
        rs = [(res, row) for res, row in rs if res["active"]]
    plans = [(res, row) for res, row in rs if row.get("beds") is not None or priced(row)]
    fit = [(res, row) for res, row in plans if res["status"] != FAIL]
    offers = [res for res, row in fit if priced(row) and not row.get("unavailable")]
    if offers:
        return best(offers)
    if fit:
        gone = [row["unavailable"] for _, row in fit if row.get("unavailable")]
        why = "unavailable" if len(gone) == len(fit) else "no_price"
        text = (f'fitting plan unavailable ("{gone[0]}")' if why == "unavailable"
                else f"no price for a fitting plan ({len(fit)} fit{'s' if len(fit) == 1 else ''})")
        b = best([res for res, _ in fit])
        return {**b, "status": NO_LISTINGS, "why": why,
                "reasons": [(NO_LISTINGS, text, "listings"), *b["reasons"]]}
    if plans:
        return best([res for res, _ in plans])
    return {"status": NO_LISTINGS, "why": "none", "reasons": [(NO_LISTINGS, NO_OFFERS, "listings")], "fails": [],
            "nice": [], "nice_score": 0, "source": None, "source_id": None, "unit_key": None, "place_key": pk,
            "decides": True, "own_site": False}


def priced(row: dict[str, Any]) -> bool:
    return row.get("price_month") is not None or row.get("price") is not None


def net_rules(goal: Goal) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(counted, unticked) commute rules of a goal's net commute (rules.in_net): counted = the must-haves not
    unticked, then the nice-to-haves ticked; unticked = the must-haves whose box was unticked (shown, not counted)."""
    must = [r for r in goal.rules if r["type"] in COMMUTE_RULES]
    nice = [r for r in goal.nice if r["type"] in COMMUTE_RULES]
    return ([r for r in must if in_net(r, True)] + [r for r in nice if in_net(r, False)],
            [r for r in must if not in_net(r, True)])


def place_commute_terms(goal: Goal, trips: dict[str, Any], pk: str) -> list[dict[str, Any]]:
    """The net-commute terms of a place for a goal: the trip behind each of its counted commute rules
    (net_rules, rules.commute_trips; a term is its `minutes`, an estimate when its provider is estimate or knn), then
    the trip of each unticked must-have, marked `off` (listed on hover as not counted, never in the number)."""
    t = trips.get(pk, {})
    counted, off = net_rules(goal)
    nearest = nearest_by_set(t)
    return commute_trips(counted, t, nearest) + [{**d, "off": True} for d in commute_trips(off, t, nearest)]


def evaluate_all(conn: sqlite3.Connection, cfg: GoalsConfig | None = None, trips: dict | None = None) -> dict[str, Any]:
    """{'listings': {goal: {(source, source_id): result}}, 'places': {goal: {place_key: result}}}.
    A result is {status, reasons: [(status, text, type)], fails: [{s, f, v, ...}] (rules.fact of each non-pass
    rule, in reasons order), nice: [(status, text, type)], nice_score}. `cfg` defaults to the effective goal set
    (goals.toml overlaid by the dashboard's goal edits, filter.goals.effective_goals)."""
    from .goals import effective_goals

    cfg = cfg or effective_goals(conn)
    inp = goal_inputs(conn, cfg, trips)
    out: dict[str, Any] = {"listings": {}, "places": {}}
    for gid, goal in cfg.goals.items():
        out["listings"][gid], out["places"][gid] = evaluate_goal(inp, goal)
    return out


# The goal editor's amenity presets (GET /api/goals `amenities`, made into amenity rules): an amenity holds when an
# entry of a listing's amenity list contains one of its words and none of its exclusions ("Dishwasher" is not laundry, "no parking" is
# not parking). Haiku's text reading adds entries such as "In-unit laundry (read from text)".
AMENITY_FLAGS: dict[str, tuple[list[str], list[str]]] = {
    "gym": (["gym", "fitness"], []),
    "in_unit_laundry": (["in-unit laundry", "in unit laundry", "washer/dryer", "washer and dryer", "in unit",
                         "in-home washer"], ["hookup", "no laundry"]),
    "parking": (["parking", "garage", "carport"], ["no parking"]),
    "pets": (["pets allowed", "pet friendly", "dogs allowed", "cats allowed"], []),
}


COMMUTE_RULES = {"commute_any", "near"}
ROUTING_RULES = COMMUTE_RULES | {"net_commute"}  # rules a paid route can still change


def routing_candidates(conn: sqlite3.Connection, cfg: GoalsConfig | None = None,
                       res: dict[str, Any] | None = None) -> dict[str, set[str]]:
    """place_key -> goals for which some listing there can still pass on everything except commute rules:
    the only places worth paid routing (a 2-bedroom over budget fails whatever its commute). Listings that do
    not decide their place (an aggregator's at a place whose own site prices offers) nominate nothing. `res`: an
    `evaluate_all` result already in hand."""
    res = res or evaluate_all(conn, cfg)
    out: dict[str, set[str]] = {}
    for gid, listings in res["listings"].items():
        for r in listings.values():
            pk = r.get("place_key")
            if pk and r.get("decides", True) and not any(
                    s == FAIL and t not in ROUTING_RULES for s, _, t in r["reasons"]):
                out.setdefault(pk, set()).add(gid)
    return out


__all__ = ["place_building_units", "evaluate_all", "evaluate_goal", "goal_inputs", "place_restriction", "place_parking", "routing_candidates", "AMENITY_FLAGS", "best_trips", "nearest_by_set", "PASS", "FAIL", "CHECK", "NO_LISTINGS"]
