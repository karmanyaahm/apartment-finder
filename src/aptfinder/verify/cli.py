"""aptf-verify: accuracy report and queue sizes. Checks themselves are made in the dashboard."""

from __future__ import annotations

import argparse

from .. import settings
from ..store import connect, init_db
from . import accuracy, agreement, conflict_queue, sample_queue


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aptf-verify")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    conn = connect(args.db)
    init_db(conn)
    print(f"conflict queue: {len(conflict_queue(conn))}   sample queue: {len(sample_queue(conn, args.seed))}")
    print("\naccuracy (hand-checked, 95% Wilson interval):")
    for (src, field), a in sorted(accuracy(conn).items()):
        print(f"  {src:28s} {field:16s} {a['correct']:3d}/{a['n']:<3d}  {a['acc']:5.0%}  "
              f"[{a['lo']:.0%}, {a['hi']:.0%}]")
    print("\ncross-source agreement:")
    for field, a in agreement(conn).items():
        if a["compared"]:
            print(f"  {field:16s} {a['agree']}/{a['compared']}")


if __name__ == "__main__":
    main()
