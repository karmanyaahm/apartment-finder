"""Parse eval/benchmark.csv (the user's hand-built snapshot of Nov 23, 2026) into tiers."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import settings

BENCH = settings.ROOT / "eval" / "benchmark.csv"
GEOCODED = settings.ROOT / "eval" / "benchmark_geocoded.csv"


@dataclass
class BenchRow:
    tier: str          # zillow | community | ruled_out
    name: str          # full "Listing" cell, or community name for ruled_out
    city: str | None = None
    beds: float | None = None
    baths: float | None = None
    rent: float | None = None
    walk_caltrain: float | None = None
    station: str | None = None
    drive_meta: float | None = None
    url: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def _num(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"[\d,]+(\.\d+)?", text)
    return float(m.group(0).replace(",", "")) if m else None


def load_benchmark(path: Path = BENCH) -> list[BenchRow]:
    rows: list[BenchRow] = []
    tier = "zillow"
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        for cells in reader:
            first = cells[0].strip()
            if first.startswith("---"):
                low = first.lower()
                tier = "community" if "community" in low else tier
                continue
            if first.startswith("Notes:"):
                break
            if not first:
                continue
            d = dict(zip(header, cells))
            rows.append(BenchRow(
                tier=tier, name=first, city=d.get("City") or None, beds=_num(d.get("Beds")),
                baths=_num(d.get("Baths")), rent=_num(d.get("Total rent/mo")),
                walk_caltrain=_num(d.get("Walk to Caltrain (min)")),
                station=d.get("Nearest Caltrain station") or None,
                drive_meta=_num(d.get("Drive to Meta HQ, no traffic (min)")), url=d.get("Link") or None,
            ))
        # ruled-out communities are named in the notes below the table
        rest = " ".join(c[0] for c in reader if c)
    m = re.search(r"no 3-bedroom floor plans at all:\s*([^.]+)\.", rest)
    if m:
        for name in m.group(1).split(","):
            rows.append(BenchRow(tier="ruled_out", name=name.strip(), extra={"why": "no 3BR plans"}))
    m = re.search(r"((?:The )?[A-Z]\w+) and (\w+) have 3BR plans but sit a 30\+ min walk", rest)
    if m:
        for name in m.groups():
            rows.append(BenchRow(tier="ruled_out", name=name.strip(), extra={"why": "30+ min walk"}))
    return rows


def load_geocodes(path: Path = GEOCODED) -> dict[str, tuple[float, float]]:
    if not path.exists():
        return {}
    with open(path, newline="") as f:
        return {r["name"]: (float(r["lat"]), float(r["lon"])) for r in csv.DictReader(f) if r["lat"]}
