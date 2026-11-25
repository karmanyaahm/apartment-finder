"""filter: evaluates goals over the derived views and trips at read time. Writes nothing."""

from __future__ import annotations

import json

import sqlite3
from collections import defaultdict
from typing import Any

from ..config import Goal, GoalsConfig, load_goals
from ..plans import inherit_prices, is_own_priced
from ..verify import accuracy, low_accuracy
from .rules import CHECK, FAIL, PASS, RANK, Ctx, combine, commute_terms, evaluate, fact

PROVIDER_PREF = {"google": 0, "estimate": 9}


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
    """place_key -> (point_id, mode) -> latest trip from the most trusted provider."""
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
    """place_key -> attribute -> latest annotation (any author)."""
    out: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in conn.execute("SELECT * FROM annotations WHERE entity_type = 'place' ORDER BY id"):
        out[r["entity_key"]][r["attribute"].lower()] = dict(r)
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
    pooled: dict[str, set[str]] = {}
    for pk, am in conn.execute("SELECT place_key, amenities FROM listings WHERE place_key IS NOT NULL "
                               "AND amenities IS NOT NULL AND amenities != '[]'"):
        pooled.setdefault(pk, set()).update(json.loads(am) if isinstance(am, str) else am)
    for r in listings:
        if r["place_key"] in pooled:
            own_am = json.loads(r["amenities"]) if isinstance(r.get("amenities"), str) else (r.get("amenities") or [])
            r["amenities"] = sorted(pooled[r["place_key"]] | set(own_am))
    return {"listings": listings, "trips": trips, "anns": anns, "low": low, "own": own}


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


def place_commute_terms(goal: Goal, trips: dict[str, Any], pk: str) -> list[tuple[float | None, bool]]:
    """The net-commute terms of a place for a goal (rules.commute_terms over its hard commute rules)."""
    t = trips.get(pk, {})
    return commute_terms([r for r in goal.rules if r["type"] in COMMUTE_RULES], t, nearest_by_set(t))


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
                    s == FAIL and t not in COMMUTE_RULES for s, _, t in r["reasons"]):
                out.setdefault(pk, set()).add(gid)
    return out


__all__ = ["evaluate_all", "evaluate_goal", "goal_inputs", "routing_candidates", "amenity_flags", "AMENITY_FLAGS", "best_trips", "nearest_by_set", "PASS", "FAIL", "CHECK"]
