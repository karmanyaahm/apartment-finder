"""filter: evaluates goals over the derived views and trips at read time. Writes nothing."""

from __future__ import annotations

import json

import sqlite3
from collections import defaultdict
from typing import Any

from ..config import Goal, GoalsConfig, load_goals, operator_restriction
from ..plans import inherit_prices, is_own_priced
from ..verify import accuracy, low_accuracy
from .rules import CHECK, FAIL, PASS, RANK, Ctx, combine, commute_trips, evaluate, fact

PROVIDER_PREF = {"google": 0, "knn": 1, "estimate": 9}


class _Trips(defaultdict):
    """A third-party stay place (`<building>~<operator>`) is at its building's address: it reads the building's
    trips instead of having its own routed (identity.stay_key)."""

    def get(self, key, default=None):
        if key in self:
            return self[key]
        if key and "~" in key and key.split("~", 1)[0] in self:
            return self[key.split("~", 1)[0]]
        return default


def best_trips(conn: sqlite3.Connection) -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    """place_key -> (point_id, mode) -> latest trip from the most trusted provider: the place's own Google trip, else
    a KNN range from nearby places' Google trips (filter.knn), else the straight-line estimate."""
    from .knn import add_knn, cells

    out: dict[str, dict[tuple[str, str], dict[str, Any]]] = _Trips(dict)
    rows = conn.execute("""
        SELECT * FROM (SELECT t.*, ROW_NUMBER() OVER (
            PARTITION BY place_key, point_id, mode, provider ORDER BY id DESC) rn FROM trips t) WHERE rn = 1
    """).fetchall()
    for r in rows:
        key = (r["point_id"], r["mode"])
        cur = out[r["place_key"]].get(key)
        if cur is None or PROVIDER_PREF.get(r["provider"], 5) < PROVIDER_PREF.get(cur["provider"], 5):
            out[r["place_key"]][key] = dict(r)
    sel = cells(conn)
    if sel:
        located = {pk: (la, lo) for pk, la, lo in conn.execute("SELECT place_key, lat, lon FROM places WHERE lat IS NOT NULL")}
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


def goal_inputs(conn: sqlite3.Connection, cfg: GoalsConfig | None = None) -> dict[str, Any]:
    """Everything a goal is judged on, read once: the offer rows (amenities pooled per place), trips, place
    annotations, low-accuracy source fields and the places whose own site prices offers. Reused by evaluate_all
    for every goal and by the dashboard's draft preview (evaluate_goal on one unsaved goal)."""
    cfg = cfg or load_goals()
    trips = best_trips(conn)
    anns = place_annotations(conn)
    low = low_accuracy(accuracy(conn), cfg.verify.min_samples, cfg.verify.min_accuracy)
    # site_info rows (a community website's photos and link) are not offers: no goal applies to them
    # nor are a community site's plans without a price (Prometheus plan drawings, Landsby's plan list): the plan
    # exists, but no unit in it is known to be offered, so it shows in the drawer and does not count for goals
    listings = [dict(r) for r in conn.execute("SELECT * FROM listings WHERE kind IS NOT 'site_info' "
                                              "AND NOT (source = 'direct' AND price IS NULL)")]
    inherit_prices(listings)  # a price-less plan row takes the same plan's price from another source
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
    # words), over a listing's none; a market-rate one says none where nothing was read (unknown).
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
    for r in conn.execute("SELECT source, source_id, place_key, amenities, url, operator, "
                          "json_extract(data, '$.extra.website') AS website, "
                          "json_extract(data, '$.extra.extracted.restriction') AS rx, "
                          "json_extract(data, '$.extra.page_restriction') AS prx, "
                          "json_extract(data, '$.extra.extracted.parking') AS pkx, "
                          "json_extract(data, '$.extra.page_parking') AS ppk FROM listings"):
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
    for r in listings:
        if r["place_key"] in pooled:
            own_am = json.loads(r["amenities"]) if isinstance(r.get("amenities"), str) else (r.get("amenities") or [])
            r["amenities"] = sorted(pooled[r["place_key"]] | set(own_am))
        mine = rx_of.get((r["source"], r["source_id"])) or {}
        rx = mine if mine.get("value") not in (None, "none") else (limits.get(r["place_key"]) or [mine])[0]
        if rx.get("value") in (None, "none") and r["place_key"] in op_income and r["place_key"] not in market:
            rx = op_income[r["place_key"]]
        elif rx.get("value") is None and r["place_key"] in op_market:
            rx = op_market[r["place_key"]]
        rx = annotated_restriction(anns.get(r["place_key"] or "", {})) or rx
        r["restriction"] = rx.get("value")
        r["restriction_evidence"] = restriction_note(rx)
        r.update(parking_fields(pk_of.get((r["source"], r["source_id"]), []) +
                                sorted(pk_place.get(r["place_key"] or "", []), key=lambda x: not x["page"])))
    return {"listings": listings, "trips": trips, "anns": anns, "low": low, "own": own}


def row_restriction(extracted: dict[str, Any] | None, page: dict[str, Any] | None,
                    source: str) -> dict[str, Any] | None:
    """One row's reading of who may rent: a limit from extract's reading or from its own site's pages (render,
    `extra.page_restriction`, source '<source> page') first, then either's 'none'; None when neither read it."""
    got = [({**x, "source": src}) for x, src in ((extracted, source), (page, f"{source} page"))
           if isinstance(x, dict) and x.get("value")]
    return next((x for x in got if x["value"] != "none"), got[0] if got else None)


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
    affordable-housing operator's (`config.operator_restriction`) unless a page there says market-rate, else None;
    your Verify answer (`anns`: the place's latest annotations) first. `rows`: listings with `data` parsed."""
    mark = annotated_restriction(anns or {})
    if mark:
        return mark if mark["value"] != "none" else None
    op, market = None, False
    for x in rows:
        extra = (x.get("data") or {}).get("extra") or {}
        rx = row_restriction((extra.get("extracted") or {}).get("restriction"), extra.get("page_restriction"),
                             x["source"])
        if rx and rx["value"] != "none":
            return rx
        market = market or bool(rx and rx.get("evidence"))
        got = operator_restriction(own_site_urls(x["source"], x.get("url"), extra.get("website")), [x.get("operator")])
        op = op if op and op["value"] != "none" else got or op
    return op if op and op["value"] != "none" and not market else None


def evaluate_goal(inp: dict[str, Any], goal: Goal) -> tuple[dict[tuple[str, str], Any], dict[str, Any]]:
    """(listing results, place results) of one goal over goal_inputs."""
    listings, trips, anns, low, own = inp["listings"], inp["trips"], inp["anns"], inp["low"], inp["own"]
    commute = [r for r in goal.rules if r["type"] in COMMUTE_RULES]  # the terms of a net_commute rule
    lres: dict[tuple[str, str], Any] = {}
    pres: dict[str, Any] = {}
    for row in listings:
        pk = row["place_key"]
        t = trips.get(pk, {})
        ctx = Ctx(trips=t, nearest=nearest_by_set(t), annotations=anns.get(pk, {}),
                  untrusted={f for (src, f) in low if src == row["source"]}, commute_rules=commute)
        # (status, text, rule type): the type lets callers set aside commute rules (routing_candidates)
        reasons = [(*evaluate(r, row, ctx), r["type"]) for r in goal.rules]
        nice = [(*evaluate(r, row, ctx), r["type"]) for r in goal.nice]
        decides = pk not in own or is_own_priced(row)
        # the attribute each non-pass rule judges (rules.fact), in reasons order: cards mark it red or amber
        fails = [{"s": s, **fact(r, row)} for r, (s, _, _) in zip(goal.rules, reasons) if s != PASS]
        res = {"status": combine([s for s, _, _ in reasons]), "reasons": reasons, "fails": fails, "nice": nice,
               "nice_score": sum(s == PASS for s, _, _ in nice), "source": row["source"],
               "source_id": row["source_id"], "unit_key": row["unit_key"], "place_key": pk,
               "decides": decides, "own_site": pk in own}
        lres[(row["source"], row["source_id"])] = res
        if pk is None or not decides:
            continue
        best = pres.get(pk)
        if best is None or (RANK[res["status"]], -res["nice_score"]) < (RANK[best["status"]], -best["nice_score"]):
            pres[pk] = res
    return lres, pres


def place_commute_terms(goal: Goal, trips: dict[str, Any], pk: str) -> list[dict[str, Any]]:
    """The net-commute terms of a place for a goal: the trip behind each of its hard commute rules
    (rules.commute_trips; a term is its `minutes`, an estimate when its provider is estimate or knn)."""
    t = trips.get(pk, {})
    return commute_trips([r for r in goal.rules if r["type"] in COMMUTE_RULES], t, nearest_by_set(t))


def evaluate_all(conn: sqlite3.Connection, cfg: GoalsConfig | None = None) -> dict[str, Any]:
    """{'listings': {goal: {(source, source_id): result}}, 'places': {goal: {place_key: result}}}.
    A result is {status, reasons: [(status, text, type)], fails: [{s, f, v, ...}] (rules.fact of each non-pass
    rule, in reasons order), nice: [(status, text, type)], nice_score}. `cfg` defaults to the effective goal set
    (goals.toml overlaid by the dashboard's goal edits, filter.goals.effective_goals)."""
    from .goals import effective_goals

    cfg = cfg or effective_goals(conn)
    inp = goal_inputs(conn, cfg)
    out: dict[str, Any] = {"listings": {}, "places": {}}
    for gid, goal in cfg.goals.items():
        out["listings"][gid], out["places"][gid] = evaluate_goal(inp, goal)
    return out


# The list's "require" checkboxes: an amenity holds when any entry of the place's pooled amenity lists contains one of
# its words (the amenity rule's matching) and none of its exclusions ("Dishwasher" is not laundry, "no parking" is
# not parking). Haiku's text reading adds entries such as "In-unit laundry (read from text)".
AMENITY_FLAGS: dict[str, tuple[list[str], list[str]]] = {
    "gym": (["gym", "fitness"], []),
    "in_unit_laundry": (["in-unit laundry", "in unit laundry", "washer/dryer", "washer and dryer", "in unit",
                         "in-home washer"], ["hookup", "no laundry"]),
    "parking": (["parking", "garage", "carport"], ["no parking"]),
    "pets": (["pets allowed", "pet friendly", "dogs allowed", "cats allowed"], []),
}


def amenity_flags(amenities: list[str] | None) -> dict[str, bool | None]:
    """name -> True / False from a place's pooled amenity entries; None for every flag when nothing is listed."""
    if not amenities:
        return {k: None for k in AMENITY_FLAGS}
    low = [a.lower() for a in amenities]
    return {k: any(any(w in a for w in words) and not any(x in a for x in excl) for a in low)
            for k, (words, excl) in AMENITY_FLAGS.items()}


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


__all__ = ["evaluate_all", "evaluate_goal", "goal_inputs", "place_restriction", "place_parking", "routing_candidates", "amenity_flags", "AMENITY_FLAGS", "best_trips", "nearest_by_set", "PASS", "FAIL", "CHECK"]
