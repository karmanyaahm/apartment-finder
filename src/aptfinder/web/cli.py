"""aptf-web: serve the dashboard on 127.0.0.1 only."""

from __future__ import annotations

import argparse

import uvicorn

from .. import settings
from ..config import load_redactions
from .app import create_app


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="aptf-web")
    ap.add_argument("--db", default=str(settings.DB_PATH))
    ap.add_argument("--seed", action="store_true", help="serve data/seed.db (demo data)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--redact", action="store_true",
                    help="read-only view with the strings in config/redact.toml replaced (for screenshots)")
    args = ap.parse_args(argv)
    db = str(settings.SEED_DB_PATH) if args.seed else args.db
    redact = load_redactions() if args.redact else None
    if args.redact and not redact:
        ap.error("--redact needs a [replace] table in config/redact.toml")
    # Bound to localhost: the app has write endpoints and no auth (Cloudflare Access before any tunnel).
    uvicorn.run(create_app(db, redact), host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
