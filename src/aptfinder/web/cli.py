"""aptf-web: serve the dashboard on 127.0.0.1 only."""

from __future__ import annotations

import argparse

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

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
    ap.add_argument("--base-path", default=settings.secret("APTF_BASE_PATH") or "",
                    help="serve everything under this path, e.g. a secret /SomeLongPhrase for a tunnel "
                         "(default: APTF_BASE_PATH from .env)")
    args = ap.parse_args(argv)
    db = str(settings.SEED_DB_PATH) if args.seed else args.db
    redact = load_redactions() if args.redact else None
    if args.redact and not redact:
        ap.error("--redact needs a [replace] table in config/redact.toml")
    # Bound to localhost: the app has write endpoints and no auth (Cloudflare Access before any tunnel).
    app = create_app(db, redact)
    base = "/" + args.base_path.strip("/") if args.base_path.strip("/") else ""
    if base:  # only <base>/... is served; everything else is 404
        outer = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        outer.add_api_route(base, lambda: RedirectResponse(base + "/"), include_in_schema=False)
        outer.mount(base, app)
        app = outer
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
