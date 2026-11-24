"""aptf-verify: accuracy report and queue sizes. Checks themselves are made in the dashboard.

  aptf-verify                                         # queue sizes, accuracy, agreement
  aptf-verify samples --source zumper --n 5 --seed 7  # random rows of one source to check by hand on its page
"""

from __future__ import annotations

import argparse
import json
import random

from .. import settings
from ..store import connect, init_db
from . import accuracy, agreement, conflict_queue, sample_queue


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aptf-verify")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    ap.add_argument("--seed", type=int, default=0)
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("samples", help="random rows of one source, with the page to open and every stored field")
    sp.add_argument("--source", required=True)
    sp.add_argument("--n", type=int, default=5)
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--all", action="store_true", help="include inactive (retired) listings")
    args = ap.parse_args(argv)
    conn = connect(args.db)
    init_db(conn)
    if args.cmd == "samples":
        print_samples(conn, args.source, args.n, args.seed, args.all)
        return
    print(f"conflict queue: {len(conflict_queue(conn))}   sample queue: {len(sample_queue(conn, args.seed))}")
    print("\naccuracy (hand-checked, 95% Wilson interval):")
    for (src, field), a in sorted(accuracy(conn).items()):
        print(f"  {src:28s} {field:16s} {a['correct']:3d}/{a['n']:<3d}  {a['acc']:5.0%}  "
              f"[{a['lo']:.0%}, {a['hi']:.0%}]")
    print("\ncross-source agreement:")
    for field, a in agreement(conn).items():
        if a["compared"]:
            print(f"  {field:16s} {a['agree']}/{a['compared']}")


FIELDS = ["property_name", "operator", "address", "lat", "lon", "unit", "floor_plan", "beds", "baths", "sqft", "price", "price_basis",
          "available_from", "kind", "term", "furnished"]
EXTRA = ["website", "phone", "rating", "rating_count", "business_status", "price_range", "beds_range", "units_offered",
         "provider_url", "padmapper_url", "operator_page"]


def samples(conn, source: str, n: int, seed: int, include_inactive: bool = False) -> list[dict]:
    """n rows of a source drawn with random.Random(seed) from its listings sorted by source_id, so the same seed
    picks the same rows on any copy of the database that holds the same listings."""
    rows = [dict(r) for r in conn.execute(
        "SELECT l.*, o.data AS obs_data FROM listings l JOIN observations o ON o.id = l.obs_id WHERE l.source = ? "
        + ("" if include_inactive else "AND l.active ") + "ORDER BY l.source_id", (source,))]
    pick = random.Random(seed).sample(rows, min(n, len(rows)))
    out = []
    for r in pick:
        extra = (json.loads(r["obs_data"]) or {}).get("extra") or {}
        # the page to open: the provider's own page for this row (Google Maps place, Zumper/PadMapper listing,
        # operator community page)
        url = (extra.get("google_maps_uri") or (extra.get("website") if source == "portfolio" else None)
               or r["url"] or extra.get("operator_page"))
        fields = {k: r.get(k) for k in FIELDS}
        fields.update({f"extra.{k}": extra.get(k) for k in EXTRA if k in extra})
        out.append({"source_id": r["source_id"], "open": url, "place_key": r["place_key"], "fields": fields})
    return out


def print_samples(conn, source: str, n: int, seed: int, include_inactive: bool = False) -> None:
    total = conn.execute("SELECT COUNT(*) FROM listings WHERE source = ?" + ("" if include_inactive else " AND active"),
                         (source,)).fetchone()[0]
    print(f"{source}: {n} of {total} rows, seed {seed} (random.Random(seed).sample over rows sorted by source_id)")
    for i, s in enumerate(samples(conn, source, n, seed, include_inactive), 1):
        print(f"\n{i}. {s['source_id']}\n   open: {s['open']}")
        for k, v in s["fields"].items():
            print(f"   {k:22s} {'(missing)' if v is None or v == [] else v}")


if __name__ == "__main__":
    main()
