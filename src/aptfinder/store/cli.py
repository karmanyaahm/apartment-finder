"""aptf-db: initialize the database, show counts, write seed data."""

from __future__ import annotations

import argparse

from .. import settings
from .db import connect, init_db
from .schema import LOG_TABLES


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aptf-db")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create tables, triggers and views (idempotent)")
    sub.add_parser("stats", help="row counts per log table and view")
    seed = sub.add_parser("seed", help="write demo observations (defaults to data/seed.db)")
    seed.add_argument("--fresh", action="store_true", help="delete the seed db file first")
    args = ap.parse_args(argv)

    if args.cmd == "seed":
        from .seed import seed_db

        db = args.db if args.db != str(settings.DB_PATH) else str(settings.SEED_DB_PATH)
        seed_db(db, fresh=args.fresh)
        print(f"seeded {db}")
        return

    conn = connect(args.db)
    init_db(conn)
    if args.cmd == "init":
        print(f"initialized {args.db}")
    elif args.cmd == "stats":
        for name in LOG_TABLES + ["listings", "units", "places", "operators"]:
            n = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            print(f"{name:14s} {n}")


if __name__ == "__main__":
    main()
