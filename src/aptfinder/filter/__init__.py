"""filter: evaluates goals over the derived views and trips at read time. Writes nothing."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from ..config import GoalsConfig, load_goals
from ..verify import accuracy, low_accuracy
from .rules import CHECK, FAIL, PASS, RANK, Ctx, combine, evaluate

PROVIDER_PREF = {"google": 0, "estimate": 9}


def best_trips(conn: sqlite3.Connection) -> dict[str, dict[tuple[str, str], dict[str, Any]]]:
    """place_key -> (point_id, mode) -> latest trip from the most trusted provider."""
    out: dict[str, dict[tuple[str, str], dict[str, Any]]] = defaultdict(dict)
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
    listings = [dict(r) for r in conn.execute("SELECT * FROM listings")]
    out: dict[str, Any] = {"listings": {}, "places": {}}
    for gid, goal in cfg.goals.items():
        lres, pres = {}, {}
        for row in listings:
            pk = row["place_key"]
            t = trips.get(pk, {})
            ctx = Ctx(trips=t, nearest=nearest_by_set(t), annotations=anns.get(pk, {}),
                      untrusted={f for (src, f) in low if src == row["source"]})
            reasons = [evaluate(r, row, ctx) for r in goal.rules]
            nice = [evaluate(r, row, ctx) for r in goal.nice]
            res = {"status": combine([s for s, _ in reasons]), "reasons": reasons, "nice": nice,
                   "nice_score": sum(s == PASS for s, _ in nice), "source": row["source"],
                   "source_id": row["source_id"], "unit_key": row["unit_key"]}
            lres[(row["source"], row["source_id"])] = res
            if pk is None:
                continue
            best = pres.get(pk)
            if best is None or (RANK[res["status"]], -res["nice_score"]) < (RANK[best["status"]], -best["nice_score"]):
                pres[pk] = res
        out["listings"][gid] = lres
        out["places"][gid] = pres
    return out


__all__ = ["evaluate_all", "best_trips", "nearest_by_set", "PASS", "FAIL", "CHECK"]
