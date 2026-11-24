"""Loaders for the human-edited files in config/. Read at startup; never written by code."""

from __future__ import annotations

import csv
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from . import settings


class Point(BaseModel):
    set: str
    name: str
    lat: float
    lon: float
    extra: dict[str, str] = Field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.set}:{self.name}"


class PointSetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = None
    modes: list[str] = ["walk"]
    k: int = 3  # route to the k nearest points; 0 means every point in the set
    prefilter_km: float | None = None  # skip Google routing beyond this straight-line distance
    max_km: dict[str, float] = Field(default_factory=dict)  # per-mode straight-line limit for paid routing
    paid: bool = True  # False: this set only ever gets free straight-line estimates


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    rules: list[dict[str, Any]] = []
    nice: list[dict[str, Any]] = []


class VerifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_samples: int = 5
    min_accuracy: float = 0.8
    sample_fields: list[str] = ["price", "beds", "baths", "sqft", "available_from", "address"]


class GoalsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goals: dict[str, Goal]
    verify: VerifyConfig = VerifyConfig()


class AreaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cities: list[str]
    bbox: list[float]  # south, west, north, east


def _toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_goals(config_dir: Path | None = None) -> GoalsConfig:
    return GoalsConfig.model_validate(_toml((config_dir or settings.CONFIG_DIR) / "goals.toml"))


def load_area(config_dir: Path | None = None) -> AreaConfig:
    return AreaConfig.model_validate(_toml((config_dir or settings.CONFIG_DIR) / "area.toml"))


def load_point_sets(config_dir: Path | None = None) -> dict[str, PointSetConfig]:
    raw = _toml((config_dir or settings.CONFIG_DIR) / "point_sets.toml")
    return {k: PointSetConfig.model_validate(v) for k, v in raw.items()}


def load_caltrain(config_dir: Path | None = None) -> dict[str, Any]:
    return _toml((config_dir or settings.CONFIG_DIR) / "caltrain.toml")


def load_points(config_dir: Path | None = None) -> dict[str, list[Point]]:
    """Every config/points/*.csv, keyed by set name. Columns set,name,lat,lon plus free columns."""
    out: dict[str, list[Point]] = {}
    for path in sorted(((config_dir or settings.CONFIG_DIR) / "points").glob("*.csv")):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if not row.get("name"):
                    continue
                base = {k: row.pop(k) for k in ("set", "name", "lat", "lon")}
                p = Point(set=base["set"] or path.stem, name=base["name"], lat=float(base["lat"]),
                          lon=float(base["lon"]), extra={k: v for k, v in row.items() if v})
                out.setdefault(p.set, []).append(p)
        out.setdefault(path.stem, [])
    return out


class Area(BaseModel):
    """One intake area. kind: zip (Zillow, Apartments.com, Craigslist), city (Furnished Finder),
    box (Airbnb: a station's walk-zone bounding box)."""
    kind: str
    id: str
    label: str
    chunks: list[str]
    city: str | None = None
    bbox: list[float] | None = None
    slug: str | None = None  # box: Airbnb place slug for the search URL (default Palo-Alto--CA)


def load_sites(config_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Community websites: hand-listed ones (config/direct_sites.toml) plus ones found by `aptf-intake
    discover-sites` (config/discovered_sites.toml, machine-appended). A discovered site is left out once a
    `[[check]]` failed it, or when a hand entry has the same id, URL or place; it carries `verify_address`."""
    d = config_dir or settings.CONFIG_DIR
    out: dict[str, dict[str, Any]] = {}
    if (d / "direct_sites.toml").exists():
        out = {s["id"]: s for s in _toml(d / "direct_sites.toml").get("site", [])}
    if (d / "discovered_sites.toml").exists():
        raw = _toml(d / "discovered_sites.toml")
        failed = {c["id"] for c in raw.get("check", []) if not c.get("ok")}
        urls = {s["url"].rstrip("/") for s in out.values()}
        places = {s.get("place_key") for s in out.values()} - {None}
        for s in raw.get("found", []):
            if s["id"] in failed or s["id"] in out or s["url"].rstrip("/") in urls or s.get("place_key") in places:
                continue
            out[s["id"]] = {**s, "verify_address": True}
    return out


def load_areas(config_dir: Path | None = None) -> tuple[list[str], list[Area]]:
    raw = _toml((config_dir or settings.CONFIG_DIR) / "areas.toml")
    out = [Area(kind="zip", id=z["code"], label=f"{z['code']} {z['city']}", chunks=z["chunks"], city=z["city"])
           for z in raw.get("zip", [])]
    out += [Area(kind="city", id=c["name"], label=c["name"], chunks=c["chunks"], city=c["name"])
            for c in raw.get("city", [])]
    out += [Area(kind="box", id=b["id"], label=f"{b['station']} station zone", chunks=[b["chunk"]], bbox=b["bbox"],
                 slug=b.get("slug"))
            for b in raw.get("box", [])]
    out += [Area(kind="site", id=d["id"], label=d.get("name", d["id"]), chunks=[d["chunk"]] if d.get("chunk") else [])
            for d in load_sites(config_dir).values()]
    return raw.get("chunks", []), out
