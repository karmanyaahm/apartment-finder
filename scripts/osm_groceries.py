"""Generate config/points/grocery_any.csv and grocery_chains.csv from OpenStreetMap (Overpass API).

Run once and commit the output:  env -u PYTHONPATH uv run python scripts/osm_groceries.py
Reference data only (not a listing site), so a direct fetch is allowed.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aptfinder import settings  # noqa: E402
from aptfinder.config import load_area  # noqa: E402

OVERPASS = "https://overpass-api.de/api/interpreter"
SHOPS = "supermarket|grocery|greengrocer"
CHAINS = {
    "Walmart": ("walmart",),
    "Safeway": ("safeway",),
    "Whole Foods": ("whole foods",),
    "Trader Joe's": ("trader joe",),
}


def chain_of(tags: dict[str, str]) -> str | None:
    text = " ".join(tags.get(k, "") for k in ("brand", "name", "operator")).lower()
    for chain, needles in CHAINS.items():
        if any(n in text for n in needles):
            return chain
    return None


def main() -> None:
    s, w, n, e = load_area().bbox
    query = f"""
    [out:json][timeout:60];
    (
      node["shop"~"^({SHOPS})$"]({s},{w},{n},{e});
      way["shop"~"^({SHOPS})$"]({s},{w},{n},{e});
    );
    out center tags;
    """
    resp = httpx.post(OVERPASS, data={"data": query}, timeout=90,
                      headers={"User-Agent": "apartment-finder/0.1 (personal research script)"})
    resp.raise_for_status()
    rows = []
    for el in resp.json()["elements"]:
        tags = el.get("tags", {})
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        name = tags.get("name") or tags.get("brand")
        if not (lat and lon and name):
            continue
        addr = " ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")]))
        rows.append({
            "name": name, "lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "shop": tags.get("shop", ""),
            "chain": chain_of(tags) or "", "address": addr, "city": tags.get("addr:city", ""),
            "osm_id": f"{el['type']}/{el['id']}",
        })
    rows.sort(key=lambda r: (r["name"], r["osm_id"]))
    # names must be unique within a set (point_id = set:name)
    seen: dict[str, int] = {}
    for r in rows:
        seen[r["name"]] = seen.get(r["name"], 0) + 1
    for r in rows:
        if seen[r["name"]] > 1:
            r["name"] = f"{r['name']} ({r['address'] or r['osm_id']})"
    out_dir = settings.CONFIG_DIR / "points"
    cols = ["set", "name", "lat", "lon", "shop", "chain", "address", "city", "osm_id"]
    for set_name, subset in (("grocery_any", rows), ("grocery_chains", [r for r in rows if r["chain"]])):
        with open(out_dir / f"{set_name}.csv", "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=cols)
            wr.writeheader()
            for r in subset:
                wr.writerow({"set": set_name, **r})
        print(f"{set_name}: {len(subset)} points")


if __name__ == "__main__":
    main()
