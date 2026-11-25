"""Stored pages as reconciler evidence (`verify/reconcile.py`): every raw page we already hold for a listing, with
the date it was fetched, so Sonnet can settle a conflict without a live fetch (Zillow and Apartments.com answer
Apify 403, and Apify credit is nearly gone).

Per listing, by source:

- every source with a parser (`intake.ADAPTERS`): the listing's raw item in its observation's `raw_path` (the
  actor's output: Apartments.com, Zumper, PadMapper, RentCafe, Craigslist, Airbnb, Furnished Finder, ...), found by
  re-running the parser (`extract.RawIndex`);
- Zillow: its newest detail page (`data/raw/zillow_detail/`, by zpid) and building page (`data/raw/zillow_building/`).
  A building's bedroom row (`<zpid>:Nbd`) gets only the detail page's building lines (`detail_lines(building=True)`:
  the page is one representative unit's, so never its beds or price) and the building page's plans;
- own sites (`direct`, `<site>:r:<plan>`): the rendered page text of the site (`data/raw/render/`, `site_text`), or
  the Prometheus availability JSON of the site (`data/raw/direct/run*_prometheus_json.json`).

A raw item is written out as plain lines, one per field, and one line per list element ('rentals[3]: Beds: 1;
BedsText: 1 Bed; Rent: 3095; ...'), so a quote can hold a unit's name and its value together. Keys that are no
statement about this listing are left out: photos, links, ids, reviews, schools, nearby or similar listings, other
listings, price history and Zestimates (a verbatim quote from them would pass the quote test). The fetch date is
the raw file's run (`runs.started_at`), its own `fetched_at`, or a date in its name; never the listing's last
observation (rows are re-observed from old files).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import settings

MAX_BLOCK = 14000
MAX_LINE = 1500
# keys (any depth) that never state a fact about this listing, or are noise
NOISE = re.compile(
    r"(image|photo|picture|thumbnail|logo|video|tour|media|gallery|carousel|icon|url|link|href|review|rating|school|"
    r"nearby|similar|neighbo|transit|walkscore|zestimate|history|tax|mortgage|agent|broker|contact|phone|email|"
    r"token|^id$|_id$|ids$|key$|^ck$|seo|breadcrumb|activelistings|dil|_group$|_level$|parent_display|aspect|"
    r"amenity_groups|_on$|typename|orientation|accessibility|embed|hours|^lat|^lng|^lon|latitude|longitude|geo|attachment|disclaimer|onboard|stripe|screen|variant|"
    r"pricingtype|matchtype|specialtytype|monetary|rentinfotype|unitlevel|isnew|has[a-z]*count|count$)", re.I)


# inside a list element (one plan, unit or room of many) also the lists every element repeats
NESTED_NOISE = re.compile(r"amenit|feature|listing_location|feed_name|currency|city_state", re.I)


def _scalar(v: Any) -> str | None:
    if v is None or v == "" or v is False or isinstance(v, (dict, list)):
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    if not s or re.match(r"https?://", s):
        return None
    return s


def _inline(v: Any, nested: bool = False) -> str:
    """A nested value on one line: 'k: v; k2: v2', lists joined with ' | '. `nested`: an element of a list."""
    if isinstance(v, dict):
        parts = []
        for k, x in v.items():
            if NOISE.search(str(k)) or (nested and NESTED_NOISE.search(str(k))):
                continue
            s = _inline(x)
            if s:
                parts.append(f"{k}: {s}" if isinstance(x, (str, int, float, bool)) else f"{k}: [{s}]")
        return "; ".join(parts)
    if isinstance(v, list):
        if v and all(isinstance(x, int) and not isinstance(x, bool) for x in v):
            return ""  # feature codes ([7, 4, 16]), no statement
        return " | ".join(s for x in v if (s := _inline(x)))
    return _scalar(v) or ""


def item_lines(item: dict[str, Any]) -> list[str]:
    """A raw item as plain lines (see the module doc)."""
    out: list[str] = []
    for k, v in item.items():
        if NOISE.search(str(k)):
            continue
        if isinstance(v, list) and any(isinstance(x, dict) for x in v):
            out += [f"{k}[{i}]: {s}"[:MAX_LINE] for i, x in enumerate(v) if (s := _inline(x, True))]
        elif isinstance(v, dict):
            for k2, x in v.items():
                if NOISE.search(str(k2)):
                    continue
                if isinstance(x, list) and any(isinstance(y, dict) for y in x):
                    out += [f"{k}.{k2}[{i}]: {s}"[:MAX_LINE] for i, y in enumerate(x) if (s := _inline(y, True))]
                elif s := _inline(x):
                    out.append(f"{k}.{k2}: {s}"[:MAX_LINE])
        elif s := _inline(v):
            out.append(f"{k}: {s}"[:MAX_LINE])
    return out


def mine_first(lines: list[str], data: dict[str, Any]) -> list[str]:
    """The lines naming this listing's unit or floor plan first (a building's item lists every unit, and the block
    is capped), the rest in order."""
    names = [str(x) for x in (data.get("unit"), data.get("floor_plan")) if x and len(str(x)) >= 2]
    if not names:
        return lines
    hit = [ln for ln in lines if any(n in ln for n in names)]
    return hit + [ln for ln in lines if ln not in hit]


def _resolve(path: str | None) -> Path | None:
    from ..intake.extract import _resolve as resolve

    return resolve(path)


def file_date(conn, path: Path | str | None, data: dict[str, Any] | None = None) -> str | None:
    """When a raw file was fetched: its own `fetched_at`/`date`, its run's start, or a date in its name."""
    if data:
        for k in ("fetched_at", "date"):
            if isinstance(data.get(k), str) and re.match(r"\d{4}-\d\d-\d\d", data[k]):
                return data[k][:10]
    name = Path(str(path or "")).name
    m = re.match(r"run(\d+)", name)
    if m:
        r = conn.execute("SELECT started_at FROM runs WHERE id = ?", (int(m.group(1)),)).fetchone()
        if r and r[0]:
            return str(r[0])[:10]
    m = re.search(r"(\d{4}-\d\d-\d\d)", name)
    return m.group(1) if m else None


class Stored:
    """Loads each raw file once for a whole reconcile run. `__call__(conn, source, obs)` -> blocks
    [{"label", "text", "fetched"}] for one listing; misses are counted in `self.missing` by source."""

    def __init__(self) -> None:
        self.raws: dict[str, Any] = {}
        self.files: dict[Path, Any] = {}
        self.zpages: dict[str, Any] | None = None
        self.zbuild: dict[str, Any] | None = None
        self.zpage_files: dict[str, str] = {}
        self.missing: dict[str, int] = {}

    def _json(self, p: Path) -> Any:
        if p not in self.files:
            self.files[p] = json.loads(p.read_text())
        return self.files[p]

    def _miss(self, what: str) -> None:
        self.missing[what] = self.missing.get(what, 0) + 1

    def __call__(self, conn, source: str, obs: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            if source == "zillow":
                return self._zillow(conn, obs)
            if source == "direct" and ":r:" in obs["source_id"]:
                return self._own_site(conn, obs)
            return self._raw_item(conn, source, obs)
        except Exception as e:  # noqa: BLE001 - an unreadable file: no stored page for this listing
            self._miss(f"{source}: {type(e).__name__}")
            return []

    def _raw_item(self, conn, source: str, obs: dict[str, Any]) -> list[dict[str, Any]]:
        from ..intake import ADAPTERS
        from ..intake.extract import RawIndex

        if source not in ADAPTERS:
            return []
        p = _resolve(obs.get("raw_path"))
        if p is None:
            self._miss(f"{source}: raw file missing")
            return []
        item = self.raws.setdefault(source, RawIndex(source)).item(str(p), obs["source_id"])
        if item is None:
            self._miss(f"{source}: item not in its raw file")
            return []
        lines = mine_first(item_lines(item), json.loads(obs["data"]))
        return [{"label": f"stored {source} item, {p.name}", "fetched": file_date(conn, p),
                 "text": "\n".join(lines)[:MAX_BLOCK]}] if lines else []

    def _zillow(self, conn, obs: dict[str, Any]) -> list[dict[str, Any]]:
        from ..intake.zillow_building import load_buildings, plans
        from ..intake.zillow_detail import detail_lines, load_pages, raw_files, zpid_of

        if self.zpages is None:
            self.zpages, self.zbuild = load_pages(), load_buildings()
            for f in raw_files():  # zpid -> its newest detail file (load_pages keeps the newest page)
                for it in json.loads(f.read_text()).get("items") or []:
                    z = zpid_of(it) if isinstance(it, dict) else None
                    if z:
                        self.zpage_files[z] = f.name
        zpid = str((json.loads(obs["data"]).get("extra") or {}).get("zpid") or obs["source_id"].split(":")[0])
        building = ":" in obs["source_id"]
        out = []
        page = self.zpages.get(zpid)
        if page:
            fname = self.zpage_files.get(zpid)
            if building:
                lines = [f"{k}: {v}" for k, v in detail_lines(page, building=True) if v]
            else:
                lines = item_lines(page)
            if lines:
                out.append({"label": f"stored Zillow detail page of zpid {zpid}"
                            + (" (one representative unit's page; building lines only)" if building else ""),
                            "fetched": file_date(conn, fname), "text": "\n".join(lines)[:MAX_BLOCK]})
        else:
            self._miss("zillow: no detail page")
        b = (self.zbuild or {}).get(zpid)
        if b:
            lines = [f"plan {p['name']}: beds {p['beds']}; baths {p['baths']}; sqft {p['sqft']}; price {p['price']}; "
                     f"units {p['units']}; available {p['available']}" for p in plans(b)]
            name = _inline({k: b.get(k) for k in ("building_name", "address", "description") if b.get(k)})
            out.append({"label": f"stored Zillow building page of zpid {zpid}", "fetched": (b.get("_fetched_at") or "")[:10]
                        or None, "text": "\n".join(([name] if name else []) + lines)[:MAX_BLOCK]})
        elif building:
            self._miss("zillow: no building page")
        return out

    def _own_site(self, conn, obs: dict[str, Any]) -> list[dict[str, Any]]:
        from ..intake.render import site_text

        site = obs["source_id"].split(":r:")[0]
        p = _resolve(obs.get("raw_path"))
        if p is None:
            self._miss("direct: raw file missing")
            return []
        data = self._json(p)
        if "sites" in data:  # Prometheus availability JSON
            rec = (data.get("sites") or {}).get(site) or {}
            units = rec.get("units")
            if not units:
                self._miss("direct: site not in its Prometheus file")
                return []
            lines = [f"unit[{i}]: {s}"[:MAX_LINE] for i, u in enumerate(units) if (s := _inline(u, True))]
            return [{"label": f"stored Prometheus availability JSON of {site}, {p.name}",
                     "fetched": file_date(conn, p, data), "text": "\n".join(lines)[:MAX_BLOCK]}]
        its = [i for i in data.get("items") or [] if not i.get("#error") and i.get("site") == site]
        if not its:
            self._miss("direct: site not in its render file")
            return []
        return [{"label": f"stored rendered own-site pages of {site}, {p.name}", "fetched": file_date(conn, p, data),
                 "text": site_text(its)[:MAX_BLOCK]}]
