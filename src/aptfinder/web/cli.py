"""aptf-web: serve the dashboard on 127.0.0.1 only."""

from __future__ import annotations

import argparse

import uvicorn

from .. import settings
from .app import create_app


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aptf-web")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    ap.add_argument("--seed", action="store_true", help="serve data/seed.db (demo data)")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    db = str(settings.SEED_DB_PATH) if args.seed else args.db
    # Bound to localhost: the app has write endpoints and no auth (Cloudflare Access before any tunnel).
    uvicorn.run(create_app(db), host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
