"""Read facts out of a listing's whole entry with a small model (Claude Haiku through headless `claude -p`, no
tools), several calls in parallel.

  aptf-intake extract craigslist|zumper|...|all [--workers 8] [--limit N] [--max-usd 1] [--dry-run]

Every source (`TEXT_SOURCES`); a row counts as having text when it has a name or title (a RentCafe building or an
own-site plan with no description is still read for its restriction, below). The entry sent is the listing's title, name, description and
amenity list as stored, plus, for sources that keep more in their raw item than the parser stores (Apartments.com's
'about' text, fees, utilities and parking; Zumper's full description, pet policy and fees; Zillow's detail page by zpid),
those raw fields, found by re-parsing the listing's raw file (`raw_path`). Unit-specific values (price, unit) are
left out, so a building's units send the same text and are read once. Answers are in a fixed JSON schema:

- address, building name and website, kept only if they appear in the text (street part / name / URL verbatim);
- building and unit facts (gym, pool, laundry, parking, EV charging, air conditioning, dishwasher, cats, dogs,
  furnished, private bath, utilities included, minimum stay, lease term, application / admin / monthly fees), each
  with a short evidence quote that must appear verbatim in the text (a fee's number must also be in its quote),
  else the fact is dropped. Unknown stays null; absence is never inferred;
- who may rent (`restriction`): income | senior | veterans | student | employer | other, kept only with a verbatim
  quote, or 'none' when the entry sets no such limit ('Section 8 welcome' is none). A claimed limit whose quote is
  not in the text is dropped (unknown). A RentCafe building under rentcafe.com/housing/ read as 'none' stays unknown:
  that section holds income-restricted and market-rate buildings alike (Alta Torre $880, 6tenEast $3,841).

The result is a new observation of the same listing (append-only; run component `extract`): facts the source left
empty are filled (address, name, furnished, private bath; website, minimum stay and utilities in `extra`), positive
amenities are added to `amenities` in plain words ('Gym / fitness center (read from text)'), so goal rules and the
place's amenity pooling see them, and every checked fact with its quote is kept in `extra.extracted.facts`. Text
that contradicts a stated address / furnished / private bath goes to `extra.extracted.conflicts` (Verify).

Calls run in a thread pool (`workers`), each a separate `claude -p`; the database and the cache are written only
in the main thread. Answers are cached by schema version, model and text in `data/extract_cache.json`, so reading
an unchanged entry again (after the next scrape, or a reparse) costs nothing. A blocked call stops new calls; the
answers already paid for are applied, then the blocker is raised.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

from .. import identity, settings
from ..store import COMMIT_EVERY, append_observation, finish_run, start_run
from ..store.models import ObservationData
from .discover import SECRET_ENV, Blocker, clean_url

TEXT_SOURCES = ("craigslist", "furnishedfinder", "airbnb", "apartments_com", "zillow", "zumper", "padmapper",
                "rentcafe", "direct", "google_places", "portfolio")  # every source: a name alone can state a limit
RAW_SOURCES = ("apartments_com", "zumper")  # raw item holds text the parser does not store
VERSION = 3  # schema version: answers and 'read' marks of an older schema are read again
# a listing has text to read: a description, an amenity list, or a name or title as stored (shared with the Pipeline
# panel); a name alone is read for who may rent ('The Laurel at Mountain View (55+)')
HAS_TEXT_SQL = ("(length(trim(COALESCE(json_extract(data, '$.description'), ''))) >= 40 "
                "OR length(trim(COALESCE(json_extract(data, '$.property_name'), json_extract(data, '$.title'), ''))) > 0 "
                "OR json_array_length(COALESCE(json_extract(data, '$.amenities'), '[]')) > 0 "
                "OR json_extract(data, '$.extra.required_monthly_fees') IS NOT NULL)")
READ_SQL = f"COALESCE(json_extract(data, '$.extra.extracted.v'), 1) >= {VERSION}"
UNREAD_SQL = f"(json_extract(data, '$.extra.extracted') IS NULL OR NOT {READ_SQL})"
MAX_TEXT = 5000
TEXT_EXTRAS = ("pets", "laundry", "housing_type", "required_monthly_fees", "rent_special")  # source extras sent as text
CACHE = "extract_cache.json"
SYSTEM = ("You extract facts from rental listing text. Use only what the text states; never guess or infer from "
          "typical listings, and never infer that something is absent because it is not mentioned. Answer null for "
          "anything the text does not say. Every non-null fact needs `evidence`: a short exact quote (a few words) "
          "copied character for character from the text. Answer in the requested JSON.")


def _fact(value: dict[str, Any], desc: str) -> dict[str, Any]:
    return {"type": ["object", "null"], "description": desc, "required": ["value", "evidence"],
            "properties": {"value": value, "evidence": {"type": "string"}}}


_B = {"type": "boolean"}
FACTS: dict[str, dict[str, Any]] = {
    "gym": _fact(_B, "the building has a gym or fitness center"),
    "pool": _fact(_B, "swimming pool"),
    "in_unit_laundry": _fact(_B, "washer and dryer inside the unit"),
    "shared_laundry": _fact(_B, "shared / on-site laundry room"),
    "parking": _fact({"type": "string", "enum": ["garage", "covered", "lot", "street", "none"]},
                     "kind of parking offered (garage, covered/carport, open lot, street only, none)"),
    "ev_charging": _fact(_B, "electric vehicle charging"),
    "air_conditioning": _fact(_B, "air conditioning"),
    "dishwasher": _fact(_B, "dishwasher"),
    "cats_allowed": _fact(_B, "cats allowed (false if the text says no cats / no pets)"),
    "dogs_allowed": _fact(_B, "dogs allowed (false if the text says no dogs / no pets)"),
    "furnished": _fact(_B, "the unit is rented furnished"),
    "private_bath": _fact(_B, "true only if the renter's bathroom is not shared; false if shared"),
    "utilities_included": _fact({"type": "array", "items": {"type": "string"}},
                                "which utilities are included in the rent, e.g. ['water', 'trash']"),
    "min_stay_days": _fact({"type": "integer"}, "minimum stay or lease, in days"),
    "lease_term_months": _fact({"type": "integer"}, "lease length offered, in months"),
    "application_fee": _fact({"type": "number"}, "application fee in dollars"),
    "admin_fee": _fact({"type": "number"}, "administration fee in dollars"),
    "monthly_fees": _fact({"type": "number"}, "mandatory monthly fees beyond rent, in dollars, total"),
}
RESTRICTIONS = ("income", "senior", "veterans", "student", "employer", "other", "none")
RESTRICTION = {
    "type": "object", "required": ["value", "evidence"],
    "description": "who may rent this home, if the entry limits it: income (income-restricted, affordable housing, BMR, "
                   "below market rate, income limits apply, tax credit / LIHTC, Section 8 or voucher holders only, "
                   "project-based vouchers / PBV), "
                   "senior (55+, 62+, seniors only), veterans (veterans only, VASH), student (students only), employer "
                   "(only employees of a company or institution), other (any other rule on who may live there: "
                   "disability, supportive or transitional housing, members only, one gender only). none when the "
                   "entry sets no such limit, the usual rental. Not limits (none): accepting Section 8 or vouchers "
                   "('Section 8 welcome'); a minimum income to qualify ('income 3x rent', 'income requirements' about "
                   "earning enough), which is ordinary screening, unlike a maximum income; house or booking rules "
                   "(21+, no children, vaccinated guests, no smoking or drugs, reviews needed, 1 person max); being "
                   "near a campus or an employer. evidence: the exact words that state the limit, copied character "
                   "for character; empty for none.",
    "properties": {"value": {"type": "string", "enum": list(RESTRICTIONS)}, "evidence": {"type": "string"}}}
_STR = {"type": ["string", "null"]}
SCHEMA = {"type": "object", "required": ["results"], "properties": {"results": {"type": "array", "items": {
    "type": "object", "required": ["key", "restriction"],
    "properties": {
        "key": {"type": "string"},
        "address": {**_STR, "description": "street address of the rental as written, e.g. '231 Grant Ave, "
                                           "Palo Alto, CA 94306'; null if the text gives none"},
        "property_name": {**_STR, "description": "name of the apartment community or building, if named"},
        "website": {**_STR, "description": "a URL in the text for the property or its manager"},
        "facts": {"type": "object", "properties": FACTS},
        "restriction": RESTRICTION,
    }}}}}
MONEY = ("application_fee", "admin_fee", "monthly_fees")
AMENITY = {"gym": "Gym / fitness center", "pool": "Pool", "in_unit_laundry": "In-unit laundry",
           "shared_laundry": "Shared laundry", "ev_charging": "EV charging", "air_conditioning": "Air conditioning",
           "dishwasher": "Dishwasher", "cats_allowed": "Cats allowed", "dogs_allowed": "Dogs allowed"}
FROM_TEXT = " (read from text)"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def _fact_ok(name: str, f: Any, t: str) -> bool:
    """A fact counts only with a quote found verbatim in the text (normalized); a fee's number must be in it."""
    if not isinstance(f, dict) or f.get("value") is None:
        return False
    q = _norm(f.get("evidence") or "")
    if len(q) < 3 or f" {q} " not in f" {t} ":
        return False
    v = f["value"]
    kind = FACTS[name]["properties"]["value"]
    if kind["type"] == "boolean":
        return isinstance(v, bool)
    if kind["type"] == "string":
        return v in kind["enum"]
    if kind["type"] == "array":
        return isinstance(v, list) and bool(v) and all(isinstance(x, str) for x in v)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        return False
    if name in MONEY:
        return v > 0 and str(int(v)) in re.sub(r"\D", "", f.get("evidence") or "")
    return 0 < v <= 3650


def check_answer(ans: dict[str, Any], text: str) -> dict[str, Any]:
    """Only facts the text supports: verbatim street address, name and URL; every other fact with a verbatim
    quote. Returns flat values plus `facts` {name: {value, evidence}}."""
    out: dict[str, Any] = {}
    t = _norm(text)
    addr = (ans.get("address") or "").strip()
    street = addr.split(",")[0].strip()
    if re.match(r"^\d+\s+\S", street) and f" {_norm(street)} " in f" {t} ":
        out["address"] = addr
    name = (ans.get("property_name") or "").strip()
    if name and _norm(name) and _norm(name) in t:
        out["property_name"] = name
    url = (ans.get("website") or "").strip()
    if url and url.lower().rstrip("/").split("://")[-1].removeprefix("www.") in text.lower():
        cleaned, _ = clean_url(url if url.startswith("http") else f"https://{url}", keep_query=True)
        if cleaned:
            out["website"] = cleaned
    facts = {k: {"value": f["value"], "evidence": f["evidence"]}
             for k, f in (ans.get("facts") or {}).items() if k in FACTS and _fact_ok(k, f, t)}
    if facts:
        out["facts"] = facts
    rx = check_restriction(ans.get("restriction"), text)
    if rx:
        out["restriction"] = rx
    return out


# labels of entry lines that are no description: a name, a title, an amenity list or a source extra says nothing about
# who may rent, so 'none' read from them alone is unknown (Villa Montgomery's plans '1 Bedroom/1 bath', $1,012)
_THIN = ("Title: ", "Name: ", "Amenities: ", *(f"{k.replace('_', ' ').capitalize()}: " for k in TEXT_EXTRAS))
MIN_DESCRIBED = 40  # normalized characters of other text before a 'none' counts


def described(text: str) -> bool:
    """True when the entry has real text (a description, an about text, a detail page) beyond its title, name,
    amenity list and source extras: text that would state a limit on who may rent if there were one."""
    rest = " ".join(line for line in text.splitlines() if not line.startswith(_THIN))
    return len(_norm(rest)) >= MIN_DESCRIBED


def check_restriction(rx: Any, text: str) -> dict[str, Any] | None:
    """{value, evidence} of a limit whose quote is in `text`; {value: 'none'} only when `text` is `described`;
    else None (unknown). Shared by extract and render's page reading."""
    if not isinstance(rx, dict) or rx.get("value") not in RESTRICTIONS:
        return None
    if rx["value"] == "none":
        return {"value": "none"} if described(text) else None
    q = _norm(rx.get("evidence") or "")  # a limit needs its quote ('55+' -> '55')
    t = _norm(text)
    # whole words, except a long quote may touch a word the scrape glued on ('Educator HousingAt least one ...')
    if len(q) >= 2 and (f" {q} " in f" {t} " or (len(q) >= 20 and q in t)):
        return {"value": rx["value"], "evidence": rx["evidence"]}
    return None


def amenities_from(facts: dict[str, Any]) -> list[str]:
    """Positive facts in plain words, for the amenity rules (`match` words) and the place's pooled list."""
    out = [AMENITY[k] + FROM_TEXT for k in AMENITY if (facts.get(k) or {}).get("value") is True]
    park = (facts.get("parking") or {}).get("value")
    if park and park != "none":
        out.append(f"Parking: {park}{FROM_TEXT}")
    return out


# stored fields a text reading can contradict (all checkable in Verify)
CONFLICT_FIELDS = ("address", "furnished", "private_bath")


def _disagrees(field: str, stated: Any, text: Any) -> bool:
    if field == "address":  # same street part = same address, however the rest is written
        return _norm(str(stated).split(",")[0]) != _norm(str(text).split(",")[0])
    return stated != text


# --- the entry text ---------------------------------------------------------------------------------------------

def _flat(v: Any) -> str:
    if v is None or v == "" or v == [] or v == {}:
        return ""
    if isinstance(v, dict):
        return ", ".join(f"{k}: {s}" for k, x in v.items() if (s := _flat(x)) and not str(k).startswith("_"))
    if isinstance(v, list):
        return "; ".join(s for x in v if (s := _flat(x)))
    return str(v).strip()


def raw_lines(source: str, item: dict[str, Any], building: bool = False) -> list[str]:
    """Allowlisted raw fields as 'label: value' lines. Never reviews, schools, transit or other buildings.
    `building`: a Zillow page read for a building's bedroom row (no unit-level flags)."""
    out: list[tuple[str, Any]] = []
    if source == "apartments_com":
        md = item.get("moreDetails") or {}
        groups = [f"{g.get('name')}: {_flat(g.get('amenities'))}" for c in md.get("amenities") or []
                  if isinstance(c, dict) for g in c.get("groups") or [] if isinstance(g, dict)]
        out = [("About", md.get("desc") or item.get("description")), ("Lease terms", md.get("leaseTerms")),
               ("One-time fees", [f"{e.get('amount')} {e.get('type')}" for e in md.get("onetimeExpenses") or []]),
               ("Monthly fees", [f"{e.get('amount')} {e.get('type')}" for e in md.get("recurringExpenses") or []]),
               ("Utilities included", md.get("includedUtilities")),
               ("Parking", [p.get("desc") for p in md.get("parkingPolicies") or [] if isinstance(p, dict)]),
               ("Fees", item.get("fees")), ("Pet fees", item.get("petFees")),
               ("Parking fees", item.get("parkingFees")), ("Special", item.get("moveInSpecialText")),
               ("Amenities by group", groups)]
    elif source == "zumper":
        out = [("About", item.get("detail_entity_data_description")),
               ("Summary", item.get("detail_entity_data_supplemental_content_description")),
               ("Overview", item.get("detail_entity_data_supplemental_content_property_overview")),
               ("Pet policy", item.get("detail_entity_data_pet_policy")),
               ("Fees", item.get("detail_entity_data_fees")), ("Deposits", item.get("detail_entity_data_deposits")),
               ("Parking", item.get("detail_entity_data_parking_data")),
               ("Specials", item.get("detail_entity_data_specials")),
               ("Unit features", item.get("amenity_tags")), ("Building features", item.get("building_amenity_tags"))]
    elif source == "zillow":  # the listing's detail page (intake/zillow_detail.py), never its unit's beds or price
        from .zillow_detail import detail_lines

        out = detail_lines(item, building)
    return [f"{k}: {s}" for k, v in out if (s := _flat(v))]


def entry_text(data: dict[str, Any], raw: list[str] | None = None) -> str:
    """Title, name, description, amenities and source extras, then raw lines; capped at MAX_TEXT."""
    extra = data.get("extra") or {}
    am = [a for a in data.get("amenities") or [] if not str(a).endswith(FROM_TEXT)]
    lines = [f"Title: {data['title']}" if data.get("title") else "",
             f"Name: {data['property_name']}" if data.get("property_name") else "",
             (data.get("description") or "").strip()[:3000],
             f"Amenities: {'; '.join(am)}" if am else "",
             *(f"{k.replace('_', ' ').capitalize()}: {_flat(extra[k])}" for k in TEXT_EXTRAS
               if _flat(extra.get(k))),
             *(raw or [])]
    seen, out = set(), []
    for line in lines:  # a raw field may repeat a stored one
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return "\n".join(out)[:MAX_TEXT]


def _resolve(path: str | None) -> Path | None:
    """A raw path as stored: absolute here, relative to the repo, or from another copy ('.../data/raw/...')."""
    if not path:
        return None
    cands = [Path(path), settings.ROOT / path]
    if "data/raw/" in path:
        cands.append(settings.RAW_DIR / path.split("data/raw/", 1)[1])
    return next((c for c in cands if c.is_file()), None)


class RawIndex:
    """source_id -> raw item, by re-running the source's parser over each raw file once."""

    def __init__(self, source: str):
        self.source, self.files = source, {}

    def item(self, raw_path: str | None, source_id: str) -> dict[str, Any] | None:
        p = _resolve(raw_path)
        if p is None:
            return None
        if p not in self.files:
            from . import ADAPTERS
            from .base import load_sources

            adapter, cfg, idx = ADAPTERS[self.source], load_sources().get(self.source, {}), {}
            for it in json.loads(p.read_text()).get("items") or []:
                try:
                    parsed = adapter.parse(it, cfg) if isinstance(it, dict) else None
                except Exception:  # an old file in another shape: no raw text for its rows
                    parsed = None
                for q in (parsed if isinstance(parsed, list) else [parsed] if parsed else []):
                    idx.setdefault(q.source_id, it)
            self.files[p] = idx
        return self.files[p].get(source_id)


# --- claude -----------------------------------------------------------------------------------------------------

def argv(model: str, max_usd: float) -> list[str]:
    """Trimmed like `discover.claude_argv`, with no tools at all; the prompt goes on stdin."""
    return ["claude", "-p", "--system-prompt", SYSTEM, "--tools", "", "--strict-mcp-config", "--setting-sources",
            "", "--disable-slash-commands", "--model", model, "--no-session-persistence", "--output-format", "json",
            "--json-schema", json.dumps(SCHEMA), "--max-budget-usd", f"{max_usd:.2f}"]


def ask(batch: list[dict[str, Any]], model: str, max_usd: float) -> tuple[list[dict[str, Any]], float]:
    prompt = "Extract the facts for each listing below. Return one result per key.\n\n" + "\n\n".join(
        f"### key {b['key']}\n{b['text']}" for b in batch)
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(argv(model, max_usd), input=prompt, capture_output=True, text=True, cwd=cwd, env=env,
                              timeout=600)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise Blocker(f"claude gave no JSON (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}")
    if out.get("is_error") or out.get("subtype") != "success":
        raise Blocker(f"claude call failed: {out.get('subtype')} {out.get('errors') or out.get('result')}")
    if not out.get("structured_output"):
        raise Blocker(f"no structured answer (not logged in?): {str(out.get('result'))[:200]}")
    return out["structured_output"].get("results") or [], float(out.get("total_cost_usd") or 0)


def ask_parallel(groups: list[list[dict[str, Any]]], ask_one: Callable[[list[dict[str, Any]], float], tuple],
                 workers: int, max_usd: float,
                 on_answer: Callable[[list[dict[str, Any]], list[dict[str, Any]]], None]) -> dict[str, Any]:
    """Run `ask_one(group, per-call cap)` for each group, at most `workers` at a time. New calls stop once the
    spend reaches `max_usd` or any call raises (the first error is returned; calls in flight are finished and
    kept). `on_answer` runs in this (the calling) thread."""
    state = {"spent": 0.0, "calls": 0, "error": None}
    todo = iter(groups)
    n = max(1, workers)
    with ThreadPoolExecutor(max_workers=n) as pool:
        inflight: dict[Any, list[dict[str, Any]]] = {}

        def fill() -> None:
            while state["error"] is None and state["spent"] < max_usd and len(inflight) < n:
                g = next(todo, None)
                if g is None:
                    return
                inflight[pool.submit(ask_one, g, min(0.5, max(0.05, max_usd - state["spent"])))] = g

        fill()
        while inflight:
            finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for f in finished:
                g = inflight.pop(f)
                try:
                    answers, cost = f.result()
                except (Blocker, subprocess.TimeoutExpired, OSError) as e:
                    state["error"] = state["error"] or e
                    continue
                state["spent"] += cost
                state["calls"] += 1
                on_answer(g, answers)
            fill()
    return {**state, "left": sum(1 for _ in todo)}


# --- cache --------------------------------------------------------------------------------------------------------

def _cache_path() -> Path:
    return settings.DATA_DIR / CACHE


def load_cache() -> dict[str, Any]:
    p = _cache_path()
    return json.loads(p.read_text()) if p.exists() else {}


def save_cache(new: dict[str, Any]) -> None:
    """Merge `new` into the file under a lock (parallel intake runs each extract), replacing it atomically."""
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cur = load_cache()
        cur.update(new)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur))
        os.replace(tmp, p)


# --- the run ------------------------------------------------------------------------------------------------------

def _pending(conn, source: str, reread: bool = False, pages: set[str] | None = None,
             id_like: str | None = None) -> list[dict[str, Any]]:
    """Active listings with text not read under the current schema; with `reread`, also those already read (their
    answers come from the cache). A listing already read starts from its scraped observation under the reading.
    `pages`: zpids with a Zillow detail page, which is text even when the stored row has none. `id_like`: only
    source_ids matching this SQL LIKE pattern (e.g. '%:%', a Zillow building's bedroom rows)."""
    has_text = HAS_TEXT_SQL.replace('data', 'lo.data')
    if pages:
        has_text = (f"({has_text} OR CAST(json_extract(lo.data, '$.extra.zpid') AS TEXT) IN "
                    "(SELECT value FROM json_each(?)))")
    rows = conn.execute(
        "SELECT lo.* FROM latest_obs lo JOIN listings l ON l.source = lo.source AND l.source_id = lo.source_id "
        f"WHERE lo.source = ? AND l.active = 1 AND (? OR {UNREAD_SQL.replace('data', 'lo.data')}) "
        f"AND {has_text} AND (? IS NULL OR lo.source_id LIKE ?) ORDER BY lo.source_id",
        (source, reread, *([json.dumps(sorted(pages))] if pages else []), id_like, id_like)).fetchall()
    out = []
    for r in rows:
        if (json.loads(r["data"]).get("extra") or {}).get("extracted") is not None:
            r = conn.execute(
                "SELECT * FROM observations WHERE source = ? AND source_id = ? "
                "AND json_extract(data, '$.extra.extracted') IS NULL ORDER BY id DESC LIMIT 1",
                (source, r["source_id"])).fetchone() or r
        out.append({"row": dict(r), "data": json.loads(r["data"])})
    return out


def extract(conn, source: str, *, limit: int = 100000, batch: int = 10, model: str = "haiku", max_usd: float = 1.0,
            workers: int = 8, dry_run: bool = False, reread: bool = False, cached_only: bool = False,
            ask_fn: Callable | None = None, rebuild_identity: bool = True,
            id_like: str | None = None, recheck_none: bool = False) -> dict[str, Any]:
    """Read the whole entry of every unread active listing of `source`. `cached_only`: apply cached answers, ask
    nothing (after a reparse). `ask_fn` replaces `ask` (tests). `rebuild_identity=False`: the caller rebuilds it.
    `id_like`: only source_ids matching this SQL LIKE pattern. `recheck_none`: only listings whose current reading
    says restriction 'none' from a text that is not `described` (cached answers applied again, free)."""
    if recheck_none:
        reread = cached_only = True
    if source not in TEXT_SOURCES:
        raise SystemExit(f"extract supports {', '.join(TEXT_SOURCES)} or all")
    pages = None
    if source == "zillow":  # detail pages, by zpid (a building's bedroom rows share one)
        from .zillow_detail import load_pages

        pages = load_pages()
    todo = _pending(conn, source, reread, set(pages or ()), id_like)[:limit]
    raws = RawIndex(source) if source in RAW_SOURCES else None
    raw_missing = 0
    for t in todo:
        lines = None
        if pages is not None:
            page = pages.get(str((t["data"].get("extra") or {}).get("zpid")))
            lines = raw_lines(source, page, ":" in t["row"]["source_id"]) if page else None  # <zpid>:Nbd
        elif raws is not None:
            item = raws.item(t["row"]["raw_path"], t["row"]["source_id"])
            raw_missing += item is None
            lines = raw_lines(source, item) if item else None
        t["text"] = entry_text(t["data"], lines)
        t["hash"] = hashlib.sha1(f"v{VERSION}\n{model}\n{t['text']}".encode()).hexdigest()
    if recheck_none:
        said_none = {r[0] for r in conn.execute(
            "SELECT source_id FROM listings WHERE source = ? "
            "AND json_extract(data, '$.extra.extracted.restriction.value') = 'none'", (source,))}
        todo = [t for t in todo if t["row"]["source_id"] in said_none and not described(t["text"])]
    cache = load_cache()
    uniq: dict[str, str] = {}
    for t in todo:
        if t["hash"] not in cache:
            uniq.setdefault(t["hash"], t["text"])
    plan = {"source": source, "listings": len(todo), "texts": len({t["hash"] for t in todo}), "to_ask": len(uniq),
            "calls_planned": -(-len(uniq) // batch), "raw_missing": raw_missing}
    if dry_run or not todo or (cached_only and len(uniq) == plan["texts"]):
        return plan
    if cached_only:
        uniq = {}
    run = start_run(conn, "extract", source, {"model": model, "listings": len(todo), "workers": workers,
                                              "schema": VERSION, "cached_only": cached_only})
    items = list(uniq.items())
    groups = [[{"key": str(j), "hash": h, "text": x} for j, (h, x) in enumerate(items[i:i + batch])]
              for i in range(0, len(items), batch)]
    one = ask_fn or ask

    def on_answer(group: list[dict[str, Any]], answers: list[dict[str, Any]]) -> None:
        by_key = {str(a.get("key")): a for a in answers if isinstance(a, dict)}
        new = {g["hash"]: by_key.get(g["key"]) or {} for g in group}
        cache.update(new)
        save_cache(new)

    n_obs = 0
    try:  # the Claude calls write nothing to the database; the answers are applied after them
        res = ask_parallel(groups, lambda g, cap: one([{"key": x["key"], "text": x["text"]} for x in g], model, cap),
                           workers, max_usd, on_answer)
        filled: dict[str, int] = {}
        facts_n: dict[str, int] = {}
        restr: dict[str, int] = {}
        for t in todo:
            if t["hash"] not in cache:
                continue  # budget ran out, or a blocker stopped the calls, before this one
            got_ = check_answer(cache[t["hash"]], t["text"])
            facts = got_.pop("facts", {})
            rx = got_.pop("restriction", None)
            d = dict(t["data"])
            extra = dict(d.get("extra") or {})
            if rx == {"value": "none"} and source == "rentcafe" and extra.get("vertical") == "housing":
                rx = None  # rentcafe.com/housing/ mixes income-restricted and market-rate buildings: unknown
            if rx:
                restr[rx["value"]] = restr.get(rx["value"], 0) + 1
            flat = {**got_, **{k: facts[k]["value"] for k in ("furnished", "private_bath", "min_stay_days",
                                                              "utilities_included") if k in facts}}
            got, conflicts = [], {}
            for k, v in flat.items():
                stated = d.get(k)
                if k in CONFLICT_FIELDS and stated is not None and _disagrees(k, stated, v):
                    conflicts[k] = v  # the source's value stays; the text's goes to the Verify conflicts
                    continue
                if k in ("website", "min_stay_days", "utilities_included"):
                    if extra.get(k) is None:
                        extra[k] = v
                        got.append(k)
                elif d.get(k) is None:
                    if k == "address" and "," not in v and d.get("city"):
                        v = f"{v}, {str(d['city']).title()}"  # "850 Calderon Ave" -> "..., Mountain View"
                    d[k] = v
                    got.append(k)
            add = [a for a in amenities_from(facts) if a not in (d.get("amenities") or [])]
            if add:  # only positives; a listing with no list keeps none (unknown, never 'not listed')
                d["amenities"] = list(d.get("amenities") or []) + add
                got.append("amenities")
            extra["extracted"] = {"v": VERSION, "fields": got, "model": model, "run_id": run, "facts": facts,
                                  **({"restriction": rx} if rx else {}),
                                  **({"conflicts": conflicts} if conflicts else {})}
            d["extra"] = extra
            r = t["row"]
            append_observation(conn, run, source, r["source_id"], ObservationData(**d), url=r["url"],
                               price=r["price"], price_basis=r["price_basis"], query_checkin=r["query_checkin"],
                               query_checkout=r["query_checkout"], raw_path=r["raw_path"], commit=False)
            n_obs += 1
            if n_obs % COMMIT_EVERY == 0:
                conn.commit()
            for k in got:
                filled[k] = filled.get(k, 0) + 1
            for k in facts:
                facts_n[k] = facts_n.get(k, 0) + 1
        conn.commit()
    except Exception as e:  # close the run (its commit keeps the rows appended so far)
        finish_run(conn, run, "error", {**plan, "observations": n_obs}, error=repr(e)[:500])
        raise
    err = res["error"]
    counts = {**plan, "calls": res["calls"], "observations": n_obs, "filled": filled, "facts": facts_n,
              "restriction": restr,
              "not_asked": sum(1 for t in todo if t["hash"] not in cache)}
    finish_run(conn, run, "error" if err else "ok", counts, est_cost=round(res["spent"], 4),
               error=str(err)[:500] if err else None)
    if n_obs and rebuild_identity:
        identity.run(conn)
    if err:
        raise Blocker(f"{source}: {err} (answers before it were applied: {n_obs} listings)") from err
    return {"run_id": run, "claude_usd": round(res["spent"], 4), **counts}


def after_intake(conn, source: str, observations: int, *, skip: bool = False, followup: bool = True,
                 geocode: bool = True, db_path: str | None = None, **kw) -> dict[str, Any] | None:
    """The core-pipeline hook: after an intake run (or a reparse, `cached_only`) wrote rows, read a text source's
    rows (every new scraped row supersedes the listing's reading; unchanged text comes from the cache for free),
    then the free follow-up steps (`after_steps`) for any source, even when the reading is blocked."""
    if not observations:
        return None
    try:
        # identity is rebuilt once per run, by after_steps (--no-followup skips it with the other follow-up steps)
        return None if skip or source not in TEXT_SOURCES else extract(conn, source, rebuild_identity=False, **kw)
    finally:
        if followup:
            print(after_steps(conn, db_path or str(settings.DB_PATH), geocode=geocode), flush=True)


def _geocode_new(conn) -> str:
    """Google geocoding of address strings not in the cache (capped by `geocode`; within the free tier)."""
    from ..identity.addresses import geocode

    plan = geocode(conn, dry_run=True)
    if not plan["to_geocode"]:
        return f"geocoded 0 (cached {plan['cached']})"
    if not settings.secret("GOOGLE_MAPS_API_KEY"):
        return f"geocoding skipped, no GOOGLE_MAPS_API_KEY ({plan['to_geocode']} new addresses)"
    try:
        res = geocode(conn)
    except SystemExit as e:  # the API refused: say so, the free steps still run
        return f"geocoding BLOCKED: {e}"
    left = plan["to_geocode"] - res["calls"]
    return (f"geocoded {res['calls']} (cached {plan['cached']}"
            + (f"; {left} left at the cap, {res['used_this_month']}/{res['monthly_cap']} this month" if left else "")
            + ")")


def _identity(conn) -> str:
    rid = start_run(conn, "identity")
    counts = identity.run(conn)
    finish_run(conn, rid, "ok", counts)
    return f"identity rebuilt ({counts.get('linked', 0)} links)"


def _estimates(conn, db_path: str) -> str:
    from ..trips import estimate_missing

    res = estimate_missing(conn, db_path)
    return f"estimates for {res['places_done']} new places ({res['new_trips']} trips)" + (
        f", stopped: {res['error']}" if res["error"] else "")


def after_steps(conn, db_path: str, *, geocode: bool = True) -> str:
    """Free steps after every intake run that wrote rows: geocode new address strings (not on a reparse), rebuild
    identity, straight-line commute estimates for places without any. One summary line."""
    parts = [_geocode_new(conn)] if geocode else []
    parts.append(_identity(conn))
    parts.append(_estimates(conn, db_path))
    return "after intake: " + ", ".join(parts)
