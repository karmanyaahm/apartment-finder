"""aptf-identity: append links for listings whose identity decision is new or changed."""

from __future__ import annotations

import argparse
import json

from .. import settings
from ..store import connect, finish_run, init_db, start_run
from . import run


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aptf-identity")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "normalize", "duplicates"],
                    help="run: link listings (default); normalize: read new address strings with Haiku, then run; "
                         "duplicates: list suspected duplicate buildings")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    conn = connect(args.db)
    init_db(conn)
    if args.cmd == "duplicates":
        from .addresses import suspected_duplicates

        print(json.dumps(suspected_duplicates(conn), indent=1))
        return
    if args.cmd == "normalize":
        from .addresses import normalize

        res = normalize(conn, limit=args.limit, dry_run=args.dry_run)
        print(json.dumps(res, indent=1))
        if args.dry_run:
            return
    rid = start_run(conn, "identity")
    counts = run(conn)
    finish_run(conn, rid, "ok", counts)
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
