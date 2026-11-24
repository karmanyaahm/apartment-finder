"""aptf-filter: print goal outcomes per place."""

from __future__ import annotations

import argparse

from .. import settings
from ..store import connect, init_db
from . import evaluate_all


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aptf-filter")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    ap.add_argument("--goal", help="only this goal")
    ap.add_argument("--status", choices=["pass", "fail", "needs_check"])
    args = ap.parse_args(argv)
    conn = connect(args.db)
    init_db(conn)
    res = evaluate_all(conn)
    addr = {r["place_key"]: r["address"] or r["place_key"] for r in conn.execute("SELECT * FROM places")}
    for gid, places in res["places"].items():
        if args.goal and gid != args.goal:
            continue
        print(f"\n== {gid}")
        for pk, r in sorted(places.items(), key=lambda kv: kv[1]["status"]):
            if args.status and r["status"] != args.status:
                continue
            print(f"  {r['status']:12s} {addr.get(pk, pk)[:60]}  [{r['source']}]")
            for s, text, *_ in r["reasons"]:
                if s != "pass":
                    print(f"      {s}: {text}")


if __name__ == "__main__":
    main()
