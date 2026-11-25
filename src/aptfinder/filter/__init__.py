"""filter: evaluates goals over the derived views and trips at read time. Writes nothing."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from ..config import GoalsConfig, load_goals
from ..plans import inherit_prices, is_own_priced
from ..verify import accuracy, low_accuracy
from .rules import CHECK, FAIL, PASS, RANK, Ctx, combine, evaluate

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


def evaluate_all(conn: sqlite3.Connection, cfg: GoalsConfig | None = None) -> dict[str, Any]:
    """{'listings': {goal: {(source, source_id): result}}, 'places': {goal: {place_key: result}}}.
    A result is {status, reasons: [(status, text)], nice: [(status, text)], nice_score}."""
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
    out: dict[str, Any] = {"listings": {}, "places": {}}
    for gid, goal in cfg.goals.items():
        lres, pres = {}, {}
        for row in listings:
            pk = row["place_key"]
            t = trips.get(pk, {})
            ctx = Ctx(trips=t, nearest=nearest_by_set(t), annotations=anns.get(pk, {}),
                      untrusted={f for (src, f) in low if src == row["source"]})
            # (status, text, rule type): the type lets callers set aside commute rules (routing_candidates)
            reasons = [(*evaluate(r, row, ctx), r["type"]) for r in goal.rules]
            nice = [(*evaluate(r, row, ctx), r["type"]) for r in goal.nice]
            decides = pk not in own or is_own_priced(row)
            res = {"status": combine([s for s, _, _ in reasons]), "reasons": reasons, "nice": nice,
                   "nice_score": sum(s == PASS for s, _, _ in nice), "source": row["source"],
                   "source_id": row["source_id"], "unit_key": row["unit_key"], "place_key": pk,
                   "decides": decides, "own_site": pk in own}
            lres[(row["source"], row["source_id"])] = res
            if pk is None or not decides:
                continue
            best = pres.get(pk)
            if best is None or (RANK[res["status"]], -res["nice_score"]) < (RANK[best["status"]], -best["nice_score"]):
                pres[pk] = res
        out["listings"][gid] = lres
        out["places"][gid] = pres
    return out


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


__all__ = ["evaluate_all", "routing_candidates", "best_trips", "nearest_by_set", "PASS", "FAIL", "CHECK"]
