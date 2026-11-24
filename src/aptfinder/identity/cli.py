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
    args = ap.parse_args(argv)
    conn = connect(args.db)
    init_db(conn)
    rid = start_run(conn, "identity")
    counts = run(conn)
    finish_run(conn, rid, "ok", counts)
    print(json.dumps(counts))


if __name__ == "__main__":
    main()
