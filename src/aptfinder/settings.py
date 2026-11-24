"""Paths and environment. Everything else reads locations from here."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(os.environ.get("APTF_ROOT", Path(__file__).resolve().parents[2]))
load_dotenv(ROOT / ".env")

# Your own config lives in config/ (gitignored); without it the committed config.example/ is used.
CONFIG_DIR = Path(os.environ.get("APTF_CONFIG") or (ROOT / "config" if (ROOT / "config").is_dir() else ROOT / "config.example"))
DATA_DIR = Path(os.environ.get("APTF_DATA", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = Path(os.environ.get("APTF_DB", DATA_DIR / "apartments.db"))
SEED_DB_PATH = DATA_DIR / "seed.db"


def secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None
