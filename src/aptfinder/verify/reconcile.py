"""Reconcile Verify conflicts with Sonnet (`aptf-verify reconcile`).

For each open conflict (goal-relevant ones first, then the rest by place), Sonnet reads what we hold for every
listing in it (stored fields, their history, the listing's text) and decides each disputed field, quoting the text
that states the value. Fields it cannot settle from stored text send the listings' current pages through Apify
(`apify/playwright-scraper`, text only: no clicks, no links followed, no forms), and Sonnet decides again with them.

An answer is accepted only by code, never on Sonnet's word: confidence 'high', a quote that is a verbatim substring of
the evidence it names, the value written in that quote, and on a page listing several prices a quote that also names
the unit's beds or plan. Accepted answers are appended as `checks` rows by author `reconcile:sonnet` (queue
'conflict', note: quote, URL, model, confidence, reason), one per listing of the conflict (correct where the listing
states the value, wrong with the true value elsewhere), so the conflict leaves Verify like a hand-checked one. A
field where any listing already has a current check by the user is never touched; a later user check wins (newest
check). These rows are not hand checks: `verify.accuracy` and the sample queue read the user's only.

Not handled: address text conflicts (an LLM never overrides an address), Zillow 'no longer for rent?' items (no gone
marks from a machine), a price on a nightly or weekly listing.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Callable

from .. import settings
from ..store import append_check, finish_run, start_run
from . import CHECK_FIELD, all_conflicts

AUTHOR = "reconcile:sonnet"
MODEL = "sonnet"
ACTOR = "apify/playwright-scraper"
DC_USD_PER_PAGE = 0.004  # datacenter playwright page, compute only (render: about $0.003)
# hosts that answered 403 to apify/playwright-scraper through both the datacenter and the US residential proxy
# (pilot 2026-11-25: 3 Zillow homedetails pages, 3 Apartments.com pages); never sent
BLOCKED_HOSTS = ("zillow.com", "apartments.com")
FETCH_FIELDS = {"price", "beds", "baths", "sqft", "available_from"}  # worth a current page
NOT_HANDLED = {"address"}
MAX_TEXT = 6000
MAX_PAGE = 14000

PAGE_FUNCTION = r"""async function pageFunction(context) {
  const { page, request, response } = context;
  try { await page.waitForLoadState('networkidle', { timeout: 15000 }); } catch (e) {}
  const main = await page.evaluate(() => document.body ? document.body.innerText : '');
  const frames = [];
  for (const f of page.frames()) {
    if (f === page.mainFrame()) continue;
    try {
      const t = await f.evaluate(() => document.body ? document.body.innerText : '');
      if (t && /\$\s?\d[\d,]{2,}/.test(t)) frames.push('[frame ' + f.url() + ']\n' + t.slice(0, 15000));
    } catch (e) {}
  }
  return { requested: request.url, url: page.url(), title: await page.title(),
           status: response ? (typeof response.status === 'function' ? response.status() : response.status) : null,
           text: [main, ...frames].join('\n\n').slice(0, 60000) };
}"""

SYSTEM = (
    "You reconcile conflicting facts about rental listings. Several sources (or a source and its own text) disagree "
    "on a field of one apartment, plan or room. You get every listing's stored fields with when each value was seen, "
    "its text, and sometimes its current web page. Decide each disputed field: the value that is true now, or "
    "undecided. Rules: use only what the evidence states, never guess; quote, verbatim and short (one line, under 200 "
    "characters), the text that states the value, copied exactly from the evidence you name (a text block E, a "
    "stored page block S or a current page block P, never the stored-fields line); a newer page or observation "
    "supersedes an older one (each stored page gives its fetch date); watch for a per-room price "
    "vs a whole-unit rent, a price range or 'starting at' price of a plan vs one unit, '1.5 baths' vs '1 bath + "
    "powder room', a building page listing several plans (quote the line of this unit's plan), and remarks that "
    "contradict structured fields. Price is the monthly rent in dollars. Dates are YYYY-MM-DD ('available now' is "
    "today's date). confidence 'high' only when the quote states the value for this very unit or room. If the "
    "evidence does not settle a field, answer undecided and say whether the listing's current page would likely "
    "settle it (need_page).")

SCHEMA = {"type": "object", "required": ["decisions"], "properties": {"decisions": {"type": "array", "items": {
    "type": "object", "required": ["field", "status", "confidence", "reason"],
    "properties": {
        "field": {"type": "string", "description": "the disputed field, as named in the question"},
        "status": {"type": "string", "enum": ["decided", "undecided"]},
        "value": {"description": "the true value: a number, true/false, 'YYYY-MM-DD', or 'room' for kind"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {"type": ["string", "null"], "description": "id of the evidence block quoted, e.g. 'E2', 'S1' or 'P1'"},
        "quote": {"type": ["string", "null"], "description": "verbatim from that block"},
        "reason": {"type": "string", "description": "one sentence"},
        "need_page": {"type": "boolean"}}}}}}


# --- what is disputed --------------------------------------------------------------------------------------------

def item_fields(item: dict[str, Any]) -> list[str]:
    """The check fields Sonnet decides for one conflict ('price' for price_month). A too-cheap listing may also be a
    room in shared housing (kind)."""
    fs = [CHECK_FIELD.get(f, f) for f in item["fields"] if f not in NOT_HANDLED]
    if item.get("cheap"):
        fs.append("kind")
    return list(dict.fromkeys(fs))


def item_kind(item: dict[str, Any]) -> str:
    return ("cheap" if item.get("cheap") else "stale" if item.get("stale") else "text" if item.get("text")
            else "pending" if item.get("pending_render") else "cross")


def user_checked(conn) -> set[tuple[str, str, str]]:
    """(source, source_id, field) with a current check whose newest row is the user's."""
    return {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT cc.source, cc.source_id, cc.field FROM current_checks cc JOIN checks c ON c.id = cc.check_id "
        "WHERE c.author NOT LIKE 'reconcile:%'")}


def goal_relevance_map(conn, items: list[dict[str, Any]]) -> dict[int, list[str]]:
    """index in `items` -> the goals under which the conflict is shown (verify.goal_relevance)."""
    from ..filter import best_trips, evaluate_all, goal_inputs
    from ..filter import goals as goalstore
    from . import goal_relevance

    located = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT place_key, lat, lon FROM places WHERE lat IS NOT NULL")}
    trips = best_trips(conn, located)
    res, inp = evaluate_all(conn, trips=trips), goal_inputs(conn, trips=trips)
    rows = {(r["source"], r["source_id"]): r for r in inp["listings"]}
    out: dict[int, list[str]] = {}
    for gid, g in goalstore.effective_goals(conn).goals.items():
        lres = res["listings"].get(gid, {})
        for i, c in enumerate(items):
            if goal_relevance(c, g.rules, lres, rows, inp["low"], people=g.people) == "shown":
                out.setdefault(i, []).append(gid)
    return out


def select(conn, goal: str | None = None, relevance: dict[int, list[str]] | None = None,
           kinds: set[str] | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Open conflicts to reconcile, goal-relevant first (those of `goal` when given), then the rest by place.
    -> (items, skipped counts by reason). Each item gains `decide` (its fields) and `goals`."""
    items = all_conflicts(conn)
    rel = relevance if relevance is not None else goal_relevance_map(conn, items)
    mine = user_checked(conn)
    out, skipped = [], {"stale": 0, "address_only": 0, "user_checked": 0}
    for i, it in enumerate(items):
        if it.get("stale"):
            skipped["stale"] += 1
            continue
        fs = [f for f in item_fields(it)
              if not any((l["source"], l["source_id"], f) in mine for l in it["listings"])]
        if not item_fields(it):
            skipped["address_only"] += 1
            continue
        if not fs:
            skipped["user_checked"] += 1
            continue
        if kinds and item_kind(it) not in kinds:
            continue
        out.append({**it, "decide": fs, "goals": rel.get(i, []), "kind_tag": item_kind(it)})
    first = (lambda c: goal not in c["goals"]) if goal else (lambda c: not c["goals"])
    out.sort(key=lambda c: (first(c), c["place_key"] or "", c.get("unit_key") or ""))
    return out, skipped


# --- evidence ----------------------------------------------------------------------------------------------------

class Texts:
    """A listing's text as `extract` reads it (description, amenities, raw allowlisted lines, Zillow's page)."""

    def __init__(self, stored: bool = False) -> None:
        from .stored import Stored

        self.raws: dict[str, Any] = {}
        self.zpages: dict[str, Any] | None = None
        self.stored = Stored() if stored else None  # pages we hold (verify/stored.py)

    def __call__(self, source: str, obs: dict[str, Any], data: dict[str, Any]) -> str:
        from ..intake.extract import RAW_SOURCES, RawIndex, entry_text, raw_lines

        lines = None
        try:
            if source == "zillow":
                if self.zpages is None:
                    from ..intake.zillow_detail import load_pages

                    self.zpages = load_pages()
                page = self.zpages.get(str((data.get("extra") or {}).get("zpid")))
                lines = raw_lines(source, page, ":" in obs["source_id"]) if page else None
            elif source in RAW_SOURCES:
                idx = self.raws.setdefault(source, RawIndex(source))
                item = idx.item(obs.get("raw_path"), obs["source_id"])
                lines = raw_lines(source, item) if item else None
        except Exception:  # an unreadable raw file: the stored text alone
            lines = None
        return entry_text(data, lines, own=True)[:MAX_TEXT]


STORED = ["price", "price_basis", "beds", "baths", "sqft", "available_from", "available_to", "furnished",
          "private_bath", "kind", "term", "floor_plan", "unit", "property_name", "address", "title"]


def stored_value(obs: dict[str, Any], field: str) -> Any:
    """As `POST /api/checks` takes it: the observation's price column, else its data field."""
    return obs["price"] if field == "price" else json.loads(obs["data"]).get(field)


def listing_evidence(conn, l: dict[str, Any], fields: list[str], texts: Texts) -> dict[str, Any]:
    obs = dict(conn.execute("SELECT * FROM observations WHERE id = ?", (l["obs_id"],)).fetchone())
    data = json.loads(obs["data"])
    stored = {k: (obs["price"] if k == "price" else obs["price_basis"] if k == "price_basis" else data.get(k))
              for k in STORED}
    hist = []
    for h in conn.execute("SELECT observed_at, price, data FROM obs_history WHERE source = ? AND source_id = ? "
                          "ORDER BY observed_at DESC, id DESC LIMIT 6", (l["source"], l["source_id"])):
        d = json.loads(h["data"])
        vals = {f: (h["price"] if f == "price" else d.get(f)) for f in fields}
        if not hist or hist[-1][1] != vals:
            hist.append((h["observed_at"][:10], vals))
    seen = conn.execute("SELECT MIN(observed_at), MAX(observed_at) FROM observations WHERE source = ? AND "
                        "source_id = ?", (l["source"], l["source_id"])).fetchone()
    return {"source": l["source"], "source_id": l["source_id"], "obs": obs, "url": obs["url"],
            "stored": {k: v for k, v in stored.items() if v not in (None, "", [])},
            "history": hist, "first_seen": seen[0][:10], "last_seen": seen[1][:10],
            "text": texts(l["source"], obs, data),
            "stored_pages": texts.stored(conn, l["source"], obs) if texts.stored else []}


def stored_blocks(ev: list[dict[str, Any]]) -> list[tuple[str, int, dict[str, Any]]]:
    """(block id 'S1'.., listing index (E<i>), stored page) for every stored page of the conflict's listings."""
    out = []
    for i, e in enumerate(ev, 1):
        for sp in e.get("stored_pages") or []:
            out.append((f"S{len(out) + 1}", i, sp))
    return out


def build_prompt(item: dict[str, Any], ev: list[dict[str, Any]], pages: dict[str, dict[str, Any]],
                 today: str) -> tuple[str, dict[str, str]]:
    """The question and the evidence blocks; -> (prompt, {block id: text that may be quoted})."""
    blocks: dict[str, str] = {}
    parts = [f"Today is {today}. Disputed fields: {', '.join(item['decide'])}."]
    if item.get("cheap"):
        parts.append(f"Why: {item['reason']}. Decide 'price' (the monthly rent of what this listing offers) and "
                     "'kind' ('room' only if the listing offers a room in shared housing, not the whole unit); "
                     "'beds' only if the stated count is wrong.")
    if item.get("text"):
        parts.append(f"The source's structured field says one thing; its own text was read as {item['text']}.")
    if item.get("asked"):
        parts.append("The user asked for these fields of this listing to be read again: its stored value may be "
                     "wrong (a date or condition stated only in the title or text).")
    for i, e in enumerate(ev, 1):
        bid = f"E{i}"
        blocks[bid] = e["text"]
        hist = "; ".join(f"{d}: {json.dumps(v)}" for d, v in e["history"])
        parts.append(f"### {bid}: listing {e['source']} {e['source_id']}  url {e['url']}\n"
                     f"seen {e['first_seen']} to {e['last_seen']}\nstored fields: {json.dumps(e['stored'])}\n"
                     f"values of the disputed fields over time (newest first): {hist}\n"
                     f"text:\n{e['text'] or '(none)'}")
    for bid, i, sp in stored_blocks(ev):  # pages we already hold, each with its fetch date
        blocks[bid] = sp["text"]
        parts.append(f"### {bid}: {sp['label']} for listing E{i} (fetched {sp.get('fetched') or 'date unknown'})"
                     f"\n{sp['text']}")
    for j, (url, p) in enumerate(pages.items(), 1):
        bid = f"P{j}"
        blocks[bid] = p["text"]
        parts.append(f"### {bid}: current page of {url} (fetched {p['fetched']}, title {p.get('title')!r})\n"
                     f"{p['text'][:MAX_PAGE]}")
    parts.append(f"Answer one decision per disputed field: {', '.join(item['decide'])}.")
    return "\n\n".join(parts), blocks


# --- Sonnet ------------------------------------------------------------------------------------------------------

def ask(prompt: str, model: str = MODEL, max_usd: float = 0.5) -> tuple[list[dict[str, Any]], float]:
    from ..intake.discover import SECRET_ENV, Blocker
    from ..intake.render import argv

    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(argv(model, max_usd, SYSTEM, SCHEMA), input=prompt, capture_output=True, text=True,
                              cwd=cwd, env=env, timeout=600)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise Blocker(f"claude gave no JSON (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}")
    if out.get("is_error") or out.get("subtype") != "success" or not out.get("structured_output"):
        raise Blocker(f"claude call failed: {out.get('subtype')} {str(out.get('result'))[:200]}")
    return out["structured_output"].get("decisions") or [], float(out.get("total_cost_usd") or 0)


# --- acceptance (code, not Sonnet's word) -------------------------------------------------------------------------

def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace(" ", " ")).strip().lower()


MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
BED_WORD = r"(?:bd|br|bed|beds|bedroom|bedrooms|bdrm|bdrms|b)\b"


def _num_forms(n: float) -> set[str]:
    forms = {f"{int(round(n)):,}", str(int(round(n)))}
    if n != int(n):
        forms |= {f"{n:,.2f}", f"{n:.2f}", f"{n:g}"}
    return forms


def value_in_quote(field: str, value: Any, quote: str, today: str) -> bool:
    """The quote writes the value: the number (price, sqft), the count next to a bed / bath word, a date's month
    or 'now'. Booleans and kind have no written form to test; their quote must still be verbatim."""
    q = _ws(quote)
    if field in ("price", "sqft"):
        return isinstance(value, (int, float)) and any(re.search(rf"(?<![\d.,]){re.escape(f)}(?![\d,]|\.\d)", q)
                                                       for f in _num_forms(float(value)))
    if field == "beds":
        if not isinstance(value, (int, float)):
            return False
        if value == 0:
            return "studio" in q
        return bool(re.search(rf"(?<![\d.]){int(value)}\s*-?\s*{BED_WORD}", q))
    if field == "baths":
        if not isinstance(value, (int, float)):
            return False
        bath = r"\s*-?\s*(?:ba|bath|baths|bathroom|bathrooms)\b"
        if re.search(rf"(?<![\d.]){re.escape(f'{value:g}')}{bath}", q):
            return True
        whole = int(value)
        return value != whole and bool(re.search(rf"(?<![\d.]){whole}\s*(?:1/2|½|and a half)", q) or (
            re.search(rf"(?<![\d.]){whole}{bath}", q) and re.search(r"half|powder", q)))
    if field == "available_from":
        try:
            d = date.fromisoformat(str(value))
        except ValueError:
            return False
        if d.isoformat() <= today and re.search(r"\b(now|immediately|today)\b", q):
            return True
        return MONTHS[d.month - 1] in q or f"{d.month}/{d.day}" in q or d.isoformat() in q
    if field == "kind":
        return value == "room"
    return isinstance(value, bool)


def _priced_lines(text: str) -> int:
    return len({m for m in re.findall(r"\$\s?(\d{1,2},?\d{3})", text or "") if int(m.replace(",", "")) >= 500})


def names_unit(quote: str, ev: dict[str, Any]) -> bool:
    """The quote says which unit it is about: its beds, plan name or unit number."""
    q = _ws(quote)
    s = ev["stored"]
    if re.search(BED_WORD, q) or "studio" in q:
        return True
    return any(x and _ws(str(x)) in q for x in (s.get("floor_plan"), s.get("unit")))


def accept(d: dict[str, Any], item: dict[str, Any], ev: list[dict[str, Any]], blocks: dict[str, str],
           today: str, owners: dict[str, list[dict[str, Any]]] | None = None) -> tuple[bool, str]:
    """(accepted, why not) for one of Sonnet's decisions."""
    f, v = d.get("field"), d.get("value")
    if f not in item["decide"]:
        return False, f"field {f!r} not disputed"
    if d.get("status") != "decided" or v is None:
        return False, "undecided"
    if d.get("confidence") != "high":
        return False, f"confidence {d.get('confidence')}"
    bid, quote = d.get("evidence") or "", d.get("quote") or ""
    if bid not in blocks or len(_ws(quote)) < 3:
        return False, "no quote"
    if _ws(quote) not in _ws(blocks[bid]):
        return False, f"quote not verbatim in {bid}"
    if f in ("furnished", "private_bath") and not isinstance(v, bool):
        return False, "not a boolean"
    if f == "kind" and v != "room":
        return False, "kind other than room"
    if not value_in_quote(f, v, quote, today):
        return False, "value not written in the quote"
    if item.get("text") and f in item["text"]:  # a text conflict: one of the two readings
        cands = {json.dumps(item["text"][f])} | {json.dumps(e["stored"].get(f)) for e in ev}
        if json.dumps(v) not in cands and not (f == "price" and isinstance(v, (int, float))):
            return False, "a third value"
    if f in ("price", "sqft") and _priced_lines(blocks[bid]) >= 3 and not any(names_unit(quote, e) for e in ev):
        return False, "page lists several prices and the quote names no unit"
    if not item.get("text") and not item.get("cheap") and len(ev) > 1:
        # a cross conflict won by a source quoting itself proves nothing about the other source: refused for facts
        # that do not age (beds, baths, sqft); a price only from a current page when every loser was last seen a
        # week or more ago (a newer page supersedes an older observation)
        own = (owners or {}).get(bid) or []
        if own and all(same_value(f, stored_value(e["obs"], f), v, today) for e in own):
            if f != "price":
                return False, "the winning source quoting itself"
            losers = [e for e in ev if not same_value(f, stored_value(e["obs"], f), v, today)]
            week = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
            if not bid.startswith("P") or any(e["last_seen"] > week for e in losers):
                return False, "the winning source quoting itself"
    return True, ""


def same_value(field: str, a: Any, b: Any, today: str | None = None) -> bool:
    """Equal values; two availability dates on or before today are the same fact ('available now')."""
    if field == "available_from" and today and isinstance(a, str) and isinstance(b, str):
        return a == b or (a[:10] <= today and b[:10] <= today)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) < 0.5
    return a == b


def plan_checks(item: dict[str, Any], ev: list[dict[str, Any]], field: str, value: Any, today: str | None = None
                ) -> tuple[list[dict[str, Any]], str]:
    """The checks rows settling `field` across the conflict's listings, or ([], why not). A building's own-site
    row (`direct`) is never corrected by a machine: there the conflict is more likely two plans grouped as one unit."""
    rows = []
    for e in ev:
        if field == "price" and e["obs"]["price_basis"] not in ("month", None):
            return [], f"{e['source']} prices by the {e['obs']['price_basis']}"
        stored = stored_value(e["obs"], field)
        ok = stored is not None and same_value(field, stored, value, today)
        if not ok and e["source"] == "direct":
            return [], "would correct the own site's row"
        rows.append({"e": e, "stored": stored, "verdict": "correct" if ok else "wrong",
                     "true": None if ok else value})
    return rows, ""


# --- fetch -------------------------------------------------------------------------------------------------------

def fetch(urls: list[str], *, residential: bool, max_usd: float) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Current text of each URL, one page each, through Apify. -> ({url: {text, title, status, fetched}}, run info
    with the real charge)."""
    from ..intake.apify import (RESIDENTIAL_USD_PER_PAGE, browser_proxy, failed_requests, run_actor, run_record,
                                same_url)

    n = len(urls)
    res = run_actor(ACTOR, {"startUrls": [{"url": u} for u in urls], "pageFunction": PAGE_FUNCTION,
                            **browser_proxy(residential), "maxRequestsPerCrawl": n, "maxRequestRetries": 1,
                            "headless": True, "pageLoadTimeoutSecs": 60, "pageFunctionTimeoutSecs": 90,
                            "waitUntil": "domcontentloaded", "maxConcurrency": 5},
                    max_items=2 * n, max_charge_usd=max_usd, timeout_s=900, memory_mb=2048, clean=False,
                    est_usd=RESIDENTIAL_USD_PER_PAGE * n if residential else None)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out, statuses = {}, {}
    for it in res.items:
        d = it.get("#debug") or {}
        u = next((x for x in urls if any(same_url(x, y) for y in (it.get("requested"), it.get("url"),
                                                                    d.get("url"), d.get("loadedUrl")) if y)), None)
        if u:
            statuses[u] = it.get("status") or d.get("statusCode")
        if it.get("#error") or not it.get("text"):
            continue
        if u and len(it["text"].strip()) > 200 and (it.get("status") or 200) < 400:
            out[u] = {"text": it["text"], "title": it.get("title"), "status": it.get("status"),
                      "final_url": it.get("url"), "fetched": now}
    info = {**run_record(res), "pages": n, "read": len(out), "residential": residential, "statuses": statuses,
            "failed": [f"{f['url']}: {f['status']} {(f['messages'] or ['?'])[-1][:120]}" for f in
                       failed_requests(res.items)],
            "items": res.items}
    return out, info


# --- the run -----------------------------------------------------------------------------------------------------

def _decide(conn, items, pages_by_url, texts, today, model, ask_fn, workers, log) -> list[dict[str, Any]]:
    """Ask Sonnet about each item (with any fetched pages of its listings); -> one result per item."""
    jobs = []
    for it in items:
        ev = [listing_evidence(conn, l, it["decide"], texts) for l in it["listings"]]
        pages = {e["url"]: pages_by_url[e["url"]] for e in ev if e["url"] in pages_by_url}
        prompt, blocks = build_prompt(it, ev, pages, today)
        jobs.append((it, ev, pages, prompt, blocks))

    def one(job):
        try:
            return ask_fn(job[3])
        except Exception as e:  # noqa: BLE001 - one failed call leaves that item undecided
            return e
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        answers = list(pool.map(one, jobs))
    out = []
    for (it, ev, pages, prompt, blocks), ans in zip(jobs, answers):
        r = {"item": it, "ev": ev, "pages": list(pages), "blocks": blocks, "cost": 0.0, "decisions": [],
             "error": None}
        if isinstance(ans, Exception):
            r["error"] = str(ans)[:300]
            log(f"  ! {it['place_key']} {it['decide']}: {r['error']}")
        else:
            decisions, r["cost"] = ans
            owners = {f"E{i}": [e] for i, e in enumerate(ev, 1)} | {
                bid: [ev[i - 1]] for bid, i, _ in stored_blocks(ev)} | {
                f"P{j}": [e for e in ev if e["url"] == u] for j, u in enumerate(pages, 1)}
            for d in decisions:
                ok, why = accept(d, it, ev, blocks, today, owners)
                r["decisions"].append({**d, "accepted": ok, "why_not": why})
            if it.get("cheap"):  # a real price closes a too-cheap item: only when Sonnet is sure what is offered
                kind = next((d for d in r["decisions"] if d.get("field") == "kind"), None)
                if not kind or kind.get("confidence") != "high" or kind.get("status") != "decided":
                    for d in r["decisions"]:
                        if d.get("field") == "price" and d["accepted"]:
                            d["accepted"], d["why_not"] = False, "unsure whether a room or the whole unit"
        out.append(r)
    return out


def write_checks(conn, r: dict[str, Any], model: str, run_id: int | None, queue: str = "conflict"
                 ) -> list[dict[str, Any]]:
    """Append the checks of every accepted decision of one item (one transaction per field)."""
    written = []
    mine = user_checked(conn)  # read again: the user may have answered meanwhile
    by_bid = {f"E{i}": e["url"] for i, e in enumerate(r["ev"], 1)} | {f"P{j}": u for j, u in enumerate(r["pages"], 1)} | {
        bid: r["ev"][i - 1]["url"] for bid, i, _ in stored_blocks(r["ev"])}
    stored_of = {bid: f"{sp['label']} (fetched {sp.get('fetched')})" for bid, _, sp in stored_blocks(r["ev"])}
    for d in r["decisions"]:
        if not d["accepted"]:
            continue
        rows, why = plan_checks(r["item"], r["ev"], d["field"], d["value"], date.today().isoformat())
        if not rows or any((x["e"]["source"], x["e"]["source_id"], d["field"]) in mine for x in rows):
            d["accepted"], d["why_not"] = False, why or "user answered meanwhile"
            continue
        note = json.dumps({"quote": d["quote"], "evidence": d["evidence"], "url": by_bid.get(d["evidence"]),
                           "model": model, "confidence": d["confidence"], "reason": d["reason"], "run": run_id,
                           **({"stored": stored_of[d["evidence"]]} if d["evidence"] in stored_of else {})})
        for x in rows:
            o = x["e"]["obs"]
            conn.execute(
                "INSERT INTO checks (ts, author, queue, observation_id, field, stored_value, verdict, true_value, "
                "note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), AUTHOR, queue, o["id"], d["field"],
                 json.dumps(x["stored"]), x["verdict"],
                 json.dumps(x["true"]) if x["verdict"] == "wrong" else None, note))
        conn.commit()
        written.append({"field": d["field"], "value": d["value"], "listings": len(rows)})
    return written


def reconcile(conn, *, goal: str | None = None, limit: int = 10, max_usd: float = 0.0, dry_run: bool = False,
              model: str = MODEL, workers: int = 4, residential: bool = True, skip: int = 0,
              ask_fn: Callable[[str], tuple[list, float]] | None = None, fetch_fn: Callable | None = None,
              relevance: dict[int, list[str]] | None = None, out_path: Path | None = None,
              kinds: set[str] | None = None, stored: bool = True,
              log: Callable[[str], None] = print) -> dict[str, Any]:
    """Reconcile up to `limit` open conflicts (from `skip`). `max_usd`: Apify cap for current pages (0: stored
    evidence only). `stored`: also the raw pages we hold for each listing (`verify/stored.py`). `dry_run`: decide
    and report, write nothing."""
    from ..intake.apify import RESIDENTIAL_USD_PER_PAGE, credit

    today = date.today().isoformat()
    items, skipped = select(conn, goal, relevance, kinds)
    todo = items[skip:skip + limit]
    ask_fn = ask_fn or (lambda p: ask(p, model))
    texts = Texts(stored=stored)
    run_id = None if dry_run else start_run(conn, "verify", "reconcile", {
        "model": model, "goal": goal, "limit": limit, "skip": skip, "max_apify_usd": max_usd,
        "residential": residential, "kinds": sorted(kinds or []), "stored_pages": stored})
    log(f"reconcile: {len(todo)} of {len(items)} open conflicts (skipped: {skipped}); Apify cap ${max_usd:.2f}")
    results = _decide(conn, todo, {}, texts, today, model, ask_fn, workers, log)
    # second pass: fields still open that a current page could settle
    apify, pages_by_url, gone = {"usd": 0.0, "runs": []}, {}, set()
    want = [r for r in results if not r["error"] and any(
        not d["accepted"] and d.get("field") in FETCH_FIELDS for d in r["decisions"])]
    urls = list(dict.fromkeys(e["url"] for r in want for e in r["ev"] if e["url"] and not any(
        (urlsplit(e["url"]).hostname or "").endswith(h) for h in BLOCKED_HOSTS)))
    if urls and max_usd > 0:
        spent = 0.0
        for proxy in (["datacenter", "residential"] if residential else ["datacenter"]):
            left = [u for u in urls if u not in pages_by_url]
            if proxy == "residential":  # not pages the datacenter fetch found gone (404 / 410)
                left = [u for u in left if u not in gone]
            per = RESIDENTIAL_USD_PER_PAGE if proxy == "residential" else DC_USD_PER_PAGE
            cr = credit()
            room = min(max_usd - spent, (cr["left"] or 0) - 0.30)
            n = max(0, min(len(left), int(room / per)))
            log(f"fetch ({proxy}): {len(left)} pages wanted, {n} within ${room:.2f} (credit left ${cr['left']}, "
                f"~${per}/page)")
            if not n:
                continue
            got, info = (fetch_fn or fetch)(left[:n], residential=proxy == "residential",
                                            max_usd=min(room, per * n + 0.05))
            pages_by_url.update(got)
            gone |= {u for u, st in info.get("statuses", {}).items() if st in (404, 410)}
            raw = settings.RAW_DIR / "reconcile"
            raw.mkdir(parents=True, exist_ok=True)
            (raw / f"run{run_id or 'dry'}-{proxy}-{datetime.now():%H%M%S}.json").write_text(json.dumps(info))
            # the run's charge; residential traffic may post after the run, so at least the estimate
            cost = max(info.get("usd") or 0, per * n if proxy == "residential" else 0)
            spent += cost
            apify["usd"] += cost
            apify["runs"].append({k: v for k, v in info.items() if k != "items"})
            log(f"fetch ({proxy}): read {info['read']} of {n}; Apify charge ${info.get('usd') or 0:.3f}"
                + (f" + residential traffic billed later, est ${per * n:.2f}" if proxy == "residential" else "")
                + f"; failed: {info['failed'][:6]}")
        again = [r["item"] for r in want if any(e["url"] in pages_by_url for e in r["ev"])]
        if again:
            redo = {id(r["item"]): r for r in _decide(conn, again, pages_by_url, texts, today, model, ask_fn,
                                                       workers, log)}
            for i, r in enumerate(results):
                new = redo.get(id(r["item"]))
                if new:  # keep first-pass acceptances, take the page pass for the rest
                    kept = {d["field"]: d for d in r["decisions"] if d["accepted"]}
                    new["decisions"] = list(kept.values()) + [d for d in new["decisions"]
                                                             if d.get("field") not in kept]
                    new["cost"] += r["cost"]
                    results[i] = new
    stats = {"examined": len(results), "resolved": 0, "partly": 0, "undecided": 0, "errors": 0,
             "fields_accepted": 0, "fields_accepted_by_block": {}, "fields_rejected": {}, "sonnet_usd": 0.0,
             "by_goal": {}}
    out_f = open(out_path, "a") if out_path else None
    for r in results:
        stats["sonnet_usd"] += r["cost"]
        written = [] if dry_run else write_checks(conn, r, model, run_id)
        acc = [d for d in r["decisions"] if d["accepted"]]
        fields_done = {d["field"] for d in acc}
        closes = ({"price"} & fields_done or {"kind"} & fields_done) if r["item"].get("cheap") \
            else fields_done >= set(r["item"]["decide"])  # a too-cheap item leaves on its price (or kind room)
        state = ("error" if r["error"] else "resolved" if closes
                 else "partly" if fields_done else "undecided")
        stats["errors" if state == "error" else state] += 1
        stats["fields_accepted"] += len(acc)
        for d in acc:
            b = (d.get("evidence") or "?")[:1]
            stats["fields_accepted_by_block"][b] = stats["fields_accepted_by_block"].get(b, 0) + 1
        for d in r["decisions"]:
            if not d["accepted"]:
                stats["fields_rejected"][d["why_not"]] = stats["fields_rejected"].get(d["why_not"], 0) + 1
        for g in r["item"]["goals"] or ["(no goal)"]:
            b = stats["by_goal"].setdefault(g, {"examined": 0, "resolved": 0, "partly": 0, "undecided": 0})
            b["examined"] += 1
            if state in b:
                b[state] += 1
        rec = {"place_key": r["item"]["place_key"], "unit_key": r["item"].get("unit_key"),
               "kind": r["item"]["kind_tag"], "goals": r["item"]["goals"], "fields": r["item"]["decide"],
               "state": state, "error": r["error"], "pages": r["pages"], "written": written,
               "stored": [{"block": bid, "listing": f"E{i}", "label": sp["label"], "fetched": sp.get("fetched"),
                           "chars": len(sp["text"])} for bid, i, sp in stored_blocks(r["ev"])],
               "listings": [{"source": e["source"], "source_id": e["source_id"], "url": e["url"],
                             "values": {f: stored_value(e["obs"], f) for f in r["item"]["decide"]},
                             "last_seen": e["last_seen"]} for e in r["ev"]],
               "decisions": [{k: d.get(k) for k in ("field", "status", "value", "confidence", "evidence", "quote",
                                                    "reason", "need_page", "accepted", "why_not")}
                             for d in r["decisions"]]}
        if out_f:
            out_f.write(json.dumps(rec, default=str) + "\n")
        log(json.dumps({k: rec[k] for k in ("place_key", "kind", "goals", "state")}) + "  " + "; ".join(
            f"{d['field']}={d.get('value')!r} [{d.get('confidence')}] {'OK' if d['accepted'] else 'no: ' + d['why_not']}"
            f" {d.get('evidence')}: {(d.get('quote') or '')[:90]!r}" for d in rec["decisions"]))
    if out_f:
        out_f.close()
    stats["sonnet_usd"] = round(stats["sonnet_usd"], 3)
    stats["apify_usd_est"] = round(apify["usd"], 3)
    stats["apify_runs"] = apify["runs"]
    if texts.stored:
        stats["stored_missing"] = texts.stored.missing
    if run_id is not None:
        finish_run(conn, run_id, "ok", {k: v for k, v in stats.items()}, est_cost=round(apify["usd"], 4))
    stats["run_id"] = run_id
    return stats


def retract(conn, check_id: int, why: str) -> int:
    """Withdraw one of the reconciler's checks: a newer `reconcile:` row on the same observation and field, verdict
    cannot_tell, stored value null. Where the listing states a value, null matches nothing, so the current_checks
    view holds no valid check for the listing and field and the conflict reopens; where it states none, the row is
    the current check and, being no 'wrong', removes the override. Append-only: the retracted row stays. Refused
    for a user's check."""
    c = conn.execute("SELECT * FROM checks WHERE id = ?", (check_id,)).fetchone()
    if c is None or not c["author"].startswith("reconcile:"):
        raise ValueError(f"check {check_id} is not a reconciler check")
    return append_check(conn, c["author"], "conflict", c["field"], "cannot_tell", observation_id=c["observation_id"],
                        stored_value=None, note=json.dumps({"retracts": check_id, "why": why}))


def reconcile_listing(conn, source: str, source_id: str, fields: list[str], *, model: str = MODEL,
                      dry_run: bool = False, ask_fn: Callable[[str], tuple[list, float]] | None = None,
                      stored: bool = True, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Sonnet reads chosen fields of one listing at the user's request (no conflict needed), under the same
    acceptance as conflicts; an accepted answer is a `reconcile:sonnet` check in queue 'adhoc' (a wrong verdict with
    the true value overrides the stored one while the source's value is unchanged). Refused on a field the user
    checked. -> {decisions, written}."""
    row = conn.execute("SELECT obs_id, place_key, unit_key FROM listings WHERE source = ? AND source_id = ?",
                       (source, source_id)).fetchone()
    if row is None:
        raise ValueError(f"no listing {source}:{source_id}")
    mine = user_checked(conn)
    fs = [f for f in fields if (source, source_id, f) not in mine]
    item = {"place_key": row["place_key"], "unit_key": row["unit_key"], "fields": fs, "decide": fs, "goals": [],
            "kind_tag": "asked", "asked": True,
            "listings": [{"source": source, "source_id": source_id, "obs_id": row["obs_id"]}]}
    if not fs:
        return {"decisions": [], "written": [], "skipped": "the user checked every field"}
    (r,) = _decide(conn, [item], {}, Texts(stored=stored), date.today().isoformat(), model,
                   ask_fn or (lambda p: ask(p, model)), 1, log)
    run_id = None if dry_run else start_run(conn, "verify", "reconcile", {"model": model, "listing":
                                                                         f"{source}:{source_id}", "fields": fs})
    written = [] if dry_run else write_checks(conn, r, model, run_id, queue="adhoc")
    out = {"decisions": [{k: d.get(k) for k in ("field", "value", "confidence", "evidence", "quote", "reason",
                                                "accepted", "why_not")} for d in r["decisions"]],
           "written": written, "error": r["error"], "sonnet_usd": round(r["cost"], 4), "run_id": run_id}
    if run_id is not None:
        finish_run(conn, run_id, "error" if r["error"] else "ok", {"written": written}, est_cost=0.0)
    return out
