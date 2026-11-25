"""Render buildings' own websites in a real browser and read their offered plans and prices with Claude Haiku.

  aptf-intake render [--site ID] [--retry-failed] [--dry-run]

Many community sites load floor plans, prices and availability with JavaScript, which the plain page fetch
(`direct`, cheerio) never sees. This step fetches each site in the site list (`config.load_sites`) through Apify's
`apify/playwright-scraper`: it waits for the page, clicks "view more" / "load more" / "show all" style expanders (never
anything that applies, books, contacts or signs in: AGENTS.md rule 7), scrolls to the bottom, and also opens the
site's own floor-plans page once, and up to MAX_UNIT_PAGES per-plan unit lists ("View apartments"), all inside the
community's own path. Avalon sites are skipped: their page data already gives every unit exactly.

Each site's outcome is recorded in the run's counts (`areas['<site>#render']`): `ok`, `no_plans`, or a transient
failure (`blocked`, `timeout`, `empty`, `not_returned`, `error`, `not_read`) with the HTTP status and the
scraper's message; `dead` when the free precheck (`precheck.py`: DNS, one plain GET) stopped the site
before Apify; `duplicate` (with `duplicate_of`) for an entry that is the same site as an earlier one, written by
`mark_duplicates` in a run of its own before any fetch, which retires the entry's rows (it is never fetched). A run over every site picks transient failures first, then sites never rendered, then those older than
STALE_DAYS; `ok` and `no_plans` are done until then.

Each site's rendered text goes to headless `claude -p --model haiku` (no tools) with a fixed schema of plans and
units. An answer counts only if what it states is on the page: the plan or unit name, the price (as written, with or
without the comma) and the sqft must appear in the text; anything else is dropped. Rows are stored as source `direct`
(the building's own site), tagged with the area `<site>#render`, so the next render of a site retires its plans that
are no longer offered without touching the site's other rows (photos).

The same call reads who may rent the community (`restriction`, extract's values: income, senior, ...), kept only with
its verbatim quote from the pages (`extract.check_restriction`), or 'none'; it is stored on every plan row of the
site as `extra.page_restriction` {value, evidence, url}, which the place's restriction pooling reads beside extract's
reading. `aptf-intake render-restriction` reads it again from the raw page text of sites already rendered (no Apify).
Parking for the community's residents (extract's `parking` object, `extract.check_parking`) is read the same way and
stored as `extra.page_parking`; `render-restriction --parking` reads only that for sites already rendered.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import date, datetime, timezone
from typing import Any

from .. import identity, settings
from ..config import PRECHECK_SKIP, load_sites
from ..store import append_observation, finish_run, start_run
from ..store.models import ObservationData
from .apify import (RESIDENTIAL_USD_PER_PAGE, browser_proxy, classify_failure, failed_requests, log_errors,
                    run_actor, run_record, same_url, settle)
from ..plans import match_drawing
from .direct import plan_drawings
from .discover import SECRET_ENV, Blocker
from .extract import PARKING, RESTRICTION, check_parking, check_restriction
from .lease import _avail
from .precheck import precheck_many

ACTOR = "apify/playwright-scraper"
SKIP_PLATFORMS = {"avalon",  # exact units from the page data already
                  "prometheus"}  # read from Prometheus's availability JSON instead (intake/prometheus.py)

MAX_UNIT_PAGES = 5  # per site: per-plan "view apartments" pages followed (about $0.003 a page)
PAGES_PER_SITE = 2 + MAX_UNIT_PAGES  # start page, its floor-plans page, the plans' unit pages
PAGE_FUNCTION = r"""async function pageFunction(context) {
  const { page, request, response } = context;
  const MORE = /^(show|load|view|see)\s+(more|all)\b|more\s+(floor\s*plans|units|apartments|results)|view\s+all\s+(floor\s*plans|units|availab)|^show\s+unavailable|^all\s+floor\s*plans$/i;
  const NEVER = /apply|tour|contact|schedule|lease\s*now|submit|sign\s*(in|up)|log\s*in|book|call|email|text\s+us|chat|reserve|deposit/i;
  const UNITS = /^(view|see|show)\s+(available\s+)?(apartments|units|homes)\b|^available\s+(units|apartments)\b|^view\s+availability$/i;
  const MAX_UNITS = __MAX_UNIT_PAGES__;
  try { await page.waitForLoadState('networkidle', { timeout: 20000 }); } catch (e) {}
  const clicked = [];
  for (let round = 0; round < 10; round++) {
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await page.waitForTimeout(1200);
    let n = 0;
    for (const el of await page.$$('button, [role=button], a')) {
      const t = ((await el.innerText().catch(() => '')) || '').trim().replace(/\s+/g, ' ');
      if (!t || t.length > 40 || !MORE.test(t) || NEVER.test(t)) continue;
      if (!(await el.isVisible().catch(() => false))) continue;
      const href = await el.getAttribute('href').catch(() => null);
      if (href && !href.startsWith('#') && !href.startsWith('javascript')) continue;
      try { await el.click({ timeout: 3000 }); clicked.push(t); n++; await page.waitForTimeout(1500); } catch (e) {}
    }
    if (!n) break;
  }
  const ud = request.userData;
  const kind = ud.kind || 'start';
  const here = page.url().split('#')[0];
  // stay inside this community's own pages: on an operator's site (greystar.com/<community>/...) a same-site link
  // can lead to another building (Central Park at Whisman's page linked to Encasa in Sunnyvale, 2026-11-24).
  // The scope is the start page's (origin + first path segment), handed down to the pages it opens.
  let scope = ud.scope;
  if (!scope) {
    const u = new URL(here);
    const seg = u.pathname.split('/').filter(Boolean)[0];
    scope = u.origin + (seg ? '/' + seg : '');
  }
  const inScope = (h) => h.startsWith(scope) && !NEVER.test(h.slice(scope.length)) && h.split('#')[0] !== here;
  const followed = [];
  const extra = [];
  // a plan's own unit list ("VIEW APARTMENTS", "View units", "Available units"): a link is opened as a page, at
  // most MAX_UNITS per site (the start page hands what is left to the floor-plans page); a button without a link
  // is clicked in place and the dialog it opens is read
  let unitsLeft = kind === 'start' ? MAX_UNITS : (ud.unitsLeft || 0);
  if (kind !== 'units') {
    const seen = new Set();
    for (const el of await page.$$('a, button, [role=button]')) {
      if (unitsLeft <= 0) break;
      const t = ((await el.innerText().catch(() => '')) || '').trim().replace(/\s+/g, ' ');
      if (!t || t.length > 40 || !UNITS.test(t) || NEVER.test(t)) continue;
      const href = await el.evaluate((a) => a.href || null).catch(() => null);
      if (href && !href.startsWith('javascript') && href.split('#')[0] !== here) {
        const h = href.split('#')[0];
        if (seen.has(h) || !inScope(h)) continue;
        seen.add(h);
        await context.enqueueRequest({ url: h, userData: { site: ud.site, kind: 'units', scope, from: here, label: t } });
        followed.push(h); unitsLeft--;
      } else if (await el.isVisible().catch(() => false)) {
        try {
          await el.click({ timeout: 3000 }); await page.waitForTimeout(2000); unitsLeft--; clicked.push(t);
          const dlg = await page.$$eval('[role=dialog], [aria-modal=true], .modal.show, .modal.in',
                                        (ds) => ds.map((d) => d.innerText || '').filter(Boolean).join('\n'));
          if (dlg) extra.push(dlg.slice(0, 8000));
          await page.keyboard.press('Escape').catch(() => {}); await page.waitForTimeout(500);
        } catch (e) {}
      }
    }
  }
  if (kind === 'start') {
    const links = await page.$$eval('a[href]', (as) => as.map((a) => [a.href, (a.innerText || '').trim()]));
    const fp = links.find(([h, t]) => inScope(h) && /floor-?plans|floorplans|availability/i.test(h + ' ' + t)
                                      && !followed.includes(h.split('#')[0]));
    if (fp) {
      const h = fp[0].split('#')[0];
      await context.enqueueRequest({ url: h, userData: { site: ud.site, kind: 'floorplans', scope, unitsLeft, from: here } });
      followed.push(h);
    }
  }
  const text = await page.evaluate(() => document.body ? document.body.innerText : '');
  // images with their alt text and class, for floor plan drawings (render.page_drawings keeps only those whose
  // alt, class or URL says floor plan, and a plan gets one only when exactly one label names it)
  const images = await page.$$eval('img', (ims) => ims.map((i) => ({
    src: i.currentSrc || i.src || i.getAttribute('data-src') || '', alt: i.alt || '', cls: i.className || '' }))
    .filter((i) => i.src && !i.src.startsWith('data:')).slice(0, 300)).catch(() => []);
  return { site: ud.site || null, kind, child: kind !== 'start', requested: request.url, url: page.url(), images,
           status: response ? (typeof response.status === 'function' ? response.status() : response.status) : null,
           title: await page.title(), clicked, followed, from: ud.from || null, label: ud.label || null,
           text: (text + (extra.length ? '\n\n[opened in place]\n' + extra.join('\n\n') : '')).slice(0, 80000) };
}""".replace("__MAX_UNIT_PAGES__", str(MAX_UNIT_PAGES))

SYSTEM = ("You read an apartment community's own website (rendered page text) and list the floor plans or units it "
          "currently offers. Use only what the text states; never infer. Copy names, prices, square feet and "
          "availability exactly as written. Skip other communities shown on the page (e.g. 'nearby' or 'other "
          "properties'). Also say who may rent this community (restriction) and its parking. Answer in the requested JSON.")
# who may rent: extract's field, worded for a whole website (a limit must hold for this community as a whole)
PAGE_RESTRICTION = {**RESTRICTION, "description": RESTRICTION["description"] + (
    " The limit must hold for this community as a whole: 'This community is income restricted', 'an affordable "
    "housing community', 'income limits', 'AMI', 'tax credit', 'subsidized family housing', 'senior community "
    "62+', 'project-based Section 8'. none when the pages describe the community and set no such limit; also none "
    "for a few BMR units in an otherwise market-rate building, a menu or link label ('Below-Market Rates'), other "
    "properties of the same operator, 'Section 8 accepted' and equal housing notices.")}
# parking: extract's object, worded for a community's site (its residents' parking, often a fee list)
PAGE_PARKING = {**PARKING, "description": PARKING["description"] + (
    " Read it for a resident of this community: 'Garage parking $75/month', 'one assigned covered space per "
    "apartment', 'Parking: Surface lot, free'. Ignore parking of other properties of the operator.")}
_S = {"type": ["string", "null"]}
SCHEMA = {"type": "object", "required": ["plans", "restriction"], "properties": {
  "restriction": PAGE_RESTRICTION, "parking": PAGE_PARKING, "plans": {"type": "array", "items": {
    "type": "object", "required": ["name"],
    "properties": {
        "name": {**_S, "description": "plan or unit name as written, e.g. 'North 2CR', 'Plan B2', 'Unit 204'"},
        "unit": {**_S, "description": "a specific unit number if the text gives one"},
        "beds": {"type": ["number", "null"], "description": "0 for a studio"},
        "baths": {"type": ["number", "null"]},
        "sqft": {"type": ["number", "null"]},
        "price": {"type": ["number", "null"], "description": "monthly rent, the lowest shown ('starting at')"},
        "available": {**_S, "description": "as written, e.g. 'Available Now', 'Available Dec 6'"},
        "furnished": {"type": ["boolean", "null"]},
    }}}}}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _num_on_page(n: float, text: str) -> bool:
    i = int(round(n))
    return any(f in text for f in (f"{i:,}", str(i)))


def check_plans(answer: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Keep a plan only if its name is on the page; keep its price and sqft only if those numbers are on the page."""
    t = f" {_norm(text)} "
    out = []
    for p in answer:
        name = (p.get("name") or "").strip()
        if not name or f" {_norm(name)} " not in t:
            continue
        q = {"name": name}
        if isinstance(p.get("beds"), (int, float)) and 0 <= p["beds"] <= 8:
            q["beds"] = float(p["beds"])
        if isinstance(p.get("baths"), (int, float)) and 0 < p["baths"] <= 8:
            q["baths"] = float(p["baths"])
        for k, lo, hi in (("price", 300, 50000), ("sqft", 100, 10000)):
            v = p.get(k)
            if isinstance(v, (int, float)) and lo <= v <= hi and _num_on_page(v, text):
                q[k] = float(v)
        if (u := (p.get("unit") or "").strip()) and f" {_norm(u)} " in t:
            q["unit"] = u
        if isinstance(p.get("available"), str) and _norm(p["available"]) and _norm(p["available"]) in _norm(text):
            q["available"] = p["available"]
        if isinstance(p.get("furnished"), bool):
            q["furnished"] = p["furnished"]
        out.append(q)
    return out


# the restriction alone, for `reread_restrictions` (pages already rendered: their plans are stored)
RX_SYSTEM = ("You read an apartment community's own website (rendered page text) and say who may rent there. Use only "
             "what the text states; never infer. Answer in the requested JSON.")
RX_SCHEMA = {"type": "object", "required": ["restriction"], "properties": {"restriction": PAGE_RESTRICTION}}
# parking alone, for `reread_restrictions(parking=True)`
PK_SYSTEM = ("You read an apartment community's own website (rendered page text) and say what parking its residents "
             "get. Use only what the text states; never infer. Answer in the requested JSON.")
PK_SCHEMA = {"type": "object", "required": ["parking"], "properties": {"parking": PAGE_PARKING}}


def argv(model: str, max_usd: float, system: str = SYSTEM, schema: dict[str, Any] = SCHEMA) -> list[str]:
    return ["claude", "-p", "--system-prompt", system, "--tools", "", "--strict-mcp-config", "--setting-sources",
            "", "--disable-slash-commands", "--model", model, "--no-session-persistence", "--output-format", "json",
            "--json-schema", json.dumps(schema), "--max-budget-usd", f"{max_usd:.2f}"]


def _call(name: str, text: str, model: str, max_usd: float, system: str, schema: dict[str, Any]
          ) -> tuple[dict[str, Any], float]:
    prompt = f"Community: {name}\n\nRendered page text:\n{text}"
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(argv(model, max_usd, system, schema) + [prompt], capture_output=True, text=True,
                              cwd=cwd, env=env, timeout=600)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise Blocker(f"claude gave no JSON (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}")
    if out.get("is_error") or out.get("subtype") != "success" or not out.get("structured_output"):
        raise Blocker(f"claude call failed: {out.get('subtype')} {str(out.get('result'))[:200]}")
    return out["structured_output"], float(out.get("total_cost_usd") or 0)


def ask(name: str, text: str, model: str, max_usd: float) -> tuple[list[dict[str, Any]], Any, float, Any]:
    """(plans, restriction as answered, cost, parking as answered)."""
    out, cost = _call(name, text, model, max_usd, SYSTEM, SCHEMA)
    return out.get("plans") or [], out.get("restriction"), cost, out.get("parking")


def ask_restriction(name: str, text: str, model: str, max_usd: float) -> tuple[Any, float]:
    out, cost = _call(name, text, model, max_usd, RX_SYSTEM, RX_SCHEMA)
    return out.get("restriction"), cost


def ask_parking(name: str, text: str, model: str, max_usd: float) -> tuple[Any, float]:
    out, cost = _call(name, text, model, max_usd, PK_SYSTEM, PK_SCHEMA)
    return out.get("parking"), cost


def page_restriction(rx: Any, text: str, url: str) -> dict[str, Any] | None:
    """The page's answer, checked like extract's (a limit needs its quote on the page), with the site's URL."""
    got = check_restriction(rx, text)
    return {**got, "url": url} if got else None


def page_parking(pk: Any, text: str, url: str) -> dict[str, Any] | None:
    """The page's parking, checked like extract's (`check_parking`: every value backed by a quote on the page)."""
    got = check_parking(pk, text)
    return {**got, "url": url} if got else None


def _line(p: dict[str, Any]) -> str:
    beds = "?" if p.get("beds") is None else f"{p['beds']:g}"
    price = f"${p['price']:,.0f}" if p.get("price") else "no price"
    return f"{p['name']}: {beds} bd, {p.get('sqft') or '?'} sqft, {price}, {p.get('available') or ''}".rstrip(", ")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "plan"


def _site_address(conn, sid: str, site: dict[str, Any]) -> tuple[str | None, str | None]:
    """The address identity knows the building by: from this site's earlier rows, else the site list."""
    r = conn.execute("SELECT address, place_key FROM listings WHERE source = 'direct' AND address IS NOT NULL "
                     "AND (source_id LIKE ? OR json_extract(data, '$.extra.site') = ?) LIMIT 1",
                     (f"{sid}:%", sid)).fetchone()
    return (r["address"], r["place_key"]) if r else (site.get("address"), None)


STALE_DAYS = 7
DONE = {"ok", "no_plans"}  # done until stale
TRANSIENT = {"blocked", "timeout", "empty", "not_returned", "error", "not_read"}  # picked again first
STATE_OF = {"ok": "complete", "no_plans": "empty"}  # area state (retires old plans); any other outcome: failed
MIN_TEXT = 200  # rendered text shorter than this is `empty`
PAGE_BUDGET = {"start": 12000, "floorplans": 12000, "units": 6000}  # characters of each page sent to Haiku
OUTCOME_LABEL = {"ok": "plans found", "no_plans": "rendered, no plans offered", "blocked": "blocked",
                 "timeout": "timed out", "empty": "rendered almost no text", "not_returned": "no page returned",
                 "error": "failed", "not_read": "not read (Claude budget reached)",
                 "dead": "site dead (DNS)",  # precheck.py
                 "duplicate": "duplicate"}  # mark_duplicates: 'duplicate of <canonical id>'


def describe(o: dict[str, Any]) -> str:
    """'blocked (HTTP 403) on 11-24, will retry'."""
    s = OUTCOME_LABEL.get(o["outcome"], o["outcome"])
    if o["outcome"] == "duplicate" and o.get("duplicate_of"):
        s += f" of {o['duplicate_of']}"
    if o.get("http_status"):
        s += f" (HTTP {o['http_status']})"
    if o.get("at"):
        s += f" on {o['at'][5:10]}"
    return s + (", will retry" if o["outcome"] in TRANSIENT else
                ", rechecked after 7 days" if o["outcome"] in PRECHECK_SKIP else "")


def last_outcomes(conn) -> dict[str, dict[str, Any]]:
    """site id -> its latest render outcome {outcome, at, run_id, http_status, error, plans}. Runs before outcomes
    were recorded: state complete -> ok, empty -> no_plans, partial -> not_read, failed -> not_returned."""
    old = {"complete": "ok", "empty": "no_plans", "partial": "not_read", "failed": "not_returned"}
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT id, started_at, counts FROM runs WHERE component = 'intake' AND source = 'direct' "
                          "AND json_extract(params, '$.render') AND counts IS NOT NULL ORDER BY id"):
        for area, a in (json.loads(r["counts"]).get("areas") or {}).items():
            if not area.endswith("#render"):
                continue
            oc = a.get("outcome") or old.get(a.get("state"), "not_returned")
            out[area[: -len("#render")]] = {"outcome": oc, "at": r["started_at"], "run_id": r["id"],
                                            "http_status": a.get("http_status"), "error": a.get("error"),
                                            "plans": a.get("items"),
                                            **({"duplicate_of": a["duplicate_of"]} if a.get("duplicate_of") else {})}
    return out


def mark_duplicates(conn) -> dict[str, str]:
    """Retire the rows of every site-list entry that is the same site as an earlier one (`config.duplicate_sites`):
    one run of its own (`params.render`, scope partial, status ok, cost 0) whose outcome for `<entry>#render` is
    `duplicate` with `duplicate_of`, state `empty`, so its older render rows go inactive. Only entries not already
    marked as a duplicate of the same canonical entry. Run before every render and Prometheus read; the readers
    never fetch a duplicate. Returns {entry id: canonical id} of the entries marked now."""
    last = last_outcomes(conn)
    dups = {sid: s["duplicate_of"] for sid, s in load_sites(duplicates=True).items() if s.get("duplicate_of")}
    new = {sid: c for sid, c in dups.items()
           if (last.get(sid) or {}).get("outcome") != "duplicate" or last[sid].get("duplicate_of") != c}
    if not new:
        return {}
    run = start_run(conn, "intake", "direct", {"areas": [f"{sid}#render" for sid in new], "render": True,
                                               "scope": "partial", "duplicates": True})
    finish_run(conn, run, "ok", {"areas": {f"{sid}#render": {
        "state": "empty", "items": 0, "outcome": "duplicate", "duplicate_of": c, "http_status": None, "error": None}
        for sid, c in new.items()}, "observations": 0}, est_cost=0.0)
    return new


def _age_days(ts: str) -> float:
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400


def select(candidates: list[str], last: dict[str, dict[str, Any]], *, retry_failed: bool = False) -> list[tuple[str, str]]:
    """[(site, why)] in the order to render: transient failures first, then never rendered, then stale; a site
    whose last outcome is ok or no_plans and younger than STALE_DAYS is done. `retry_failed`: only the failures."""
    fail, never, stale = [], [], []
    for sid in candidates:
        o = last.get(sid)
        if o is None:
            never.append((sid, "never rendered"))
        elif o["outcome"] in TRANSIENT:
            fail.append((sid, f"retry: {describe(o)}"))
        elif _age_days(o["at"]) > STALE_DAYS:
            stale.append((sid, f"stale: {describe(o)}"))
    return fail if retry_failed else fail + never + stale


def site_outcome(start_url: str, pages: list[dict[str, Any]], failed: list[dict[str, Any]],
                 log: str | None) -> dict[str, Any]:
    """Outcome of one site's fetch before reading: `read` (pages to read), or a failure with the HTTP status and
    the scraper's message. `pages`: the site's items; `failed`: failed requests of the whole run (`#error` items)."""
    start = [p for p in pages if p.get("kind", "start" if not p.get("child") else "") == "start"]
    if start:
        status = start[0].get("status")
        text = "".join(p.get("text") or "" for p in pages)
        if status in (403, 429):
            return {"outcome": "blocked", "http_status": status, "error": f"HTTP {status}"}
        if len(text.strip()) < MIN_TEXT:
            return {"outcome": "empty", "http_status": status, "error": f"{len(text.strip())} characters of text"}
        return {"outcome": "read", "http_status": status}
    f = next((f for f in failed if same_url(f["url"], start_url)), None)
    if f:
        return {"outcome": classify_failure(f["status"], f["messages"]), "http_status": f["status"],
                "error": (f["messages"] or [None])[-1]}
    status, msgs = log_errors(log, start_url)
    if msgs:
        return {"outcome": classify_failure(status, msgs), "http_status": status, "error": msgs[-1]}
    return {"outcome": "not_returned", "http_status": None, "error": "the actor returned no item for this page"}


def page_drawings(pages: list[dict[str, Any]]) -> dict[str, str]:
    """{label: URL} of the floor plan drawings on a site's rendered pages (their `images`; raw files from before
    2026-11-25 have none). A URL is only ever one the page itself showed."""
    out: dict[str, str] = {}
    for p in pages:
        out.update(plan_drawings(p))
    return out


def site_text(pages: list[dict[str, Any]]) -> str:
    """The text Haiku reads for one site: the start page, its floor-plans page, then at most MAX_UNIT_PAGES unit
    pages, each cut to its budget so unit lists are not crowded out by a long start page."""
    order = {"start": 0, "floorplans": 1, "units": 2}
    kind = lambda p: p.get("kind") or ("floorplans" if p.get("child") else "start")  # noqa: E731
    ps = sorted(pages, key=lambda p: order.get(kind(p), 3))
    units = [p for p in ps if kind(p) == "units"][:MAX_UNIT_PAGES]
    ps = [p for p in ps if kind(p) != "units"] + units
    return "\n\n".join(f"[page {p['url']}]\n{(p.get('text') or '')[:PAGE_BUDGET.get(kind(p), 6000)]}" for p in ps)


def render(conn, *, sites: list[str] | None = None, model: str = "haiku",
           max_claude_usd: float = 1.0, max_apify_usd: float = 1.0, dry_run: bool = False,
           retry_failed: bool = False, residential: bool = False) -> dict[str, Any]:
    all_sites = load_sites(duplicates=True)
    dups = {sid: s["duplicate_of"] for sid, s in all_sites.items() if s.get("duplicate_of")}
    marked = {} if dry_run else mark_duplicates(conn)
    cands = []
    for sid, s in all_sites.items():
        if s.get("platform") in SKIP_PLATFORMS:
            continue
        if (sites and sid not in sites) or sid in dups:  # a duplicate is never fetched (mark_duplicates)
            continue
        cands.append(sid)
    last = last_outcomes(conn)
    # sites named with --site are rendered whatever their last outcome
    order = [(sid, "asked for") for sid in cands] if sites else select(cands, last, retry_failed=retry_failed)
    pick = {sid: all_sites[sid] for sid, _ in order}
    if dry_run or not pick:
        return {"sites": [(sid, all_sites[sid]["url"], why) for sid, why in order],
                "done": {sid: describe(last[sid]) for sid in cands if sid not in pick and sid in last},
                "duplicates": {sid: f"duplicate of {c}" for sid, c in dups.items()}, "marked_duplicate": marked,
                "estimated_apify_usd": round(0.003 * PAGES_PER_SITE * len(pick), 2),
                "estimated_claude_usd": round(0.006 * len(pick), 2)}
    # the free precheck (DNS, one plain GET): a dead domain is recorded, not sent to Apify
    pre = precheck_many({sid: s["url"] for sid, s in pick.items()})
    stopped = {sid: r for sid, r in pre.items() if r["outcome"] in PRECHECK_SKIP}
    areas = {f"{sid}#render": sid for sid in pick}
    per_area = {f"{sid}#render": {"state": "failed", "items": 0, "outcome": r["outcome"],
                                  "http_status": r.get("http_status"), "error": r["error"], "pages": 0}
                for sid, r in stopped.items()}
    report: dict[str, Any] = {sid: describe({**r, "at": None}) + f": {r['error']}" for sid, r in stopped.items()}
    pick = {sid: s for sid, s in pick.items() if sid not in stopped}
    run = start_run(conn, "intake", "direct", {"areas": list(areas), "render": True, "scope": "partial",
                                               "actor": ACTOR, "model": model,
                                               "proxy": "residential" if residential else "datacenter"})
    if not pick:
        finish_run(conn, run, "ok", {"areas": per_area, "observations": 0}, est_cost=0.0)
        return {"run_id": run, "apify_usd": 0.0, "claude_usd": 0.0, "observations": 0, "sites": report,
                "outcomes": {k[: -len("#render")]: a for k, a in per_area.items()}, "marked_duplicate": marked}
    n_req = PAGES_PER_SITE * len(pick)
    try:
        res = run_actor(ACTOR, {"startUrls": [{"url": s["url"], "userData": {"site": sid}} for sid, s in pick.items()],
                                "pageFunction": PAGE_FUNCTION, **browser_proxy(residential),
                                "maxRequestsPerCrawl": n_req, "maxRequestRetries": 2, "headless": True,
                                "pageLoadTimeoutSecs": 120, "pageFunctionTimeoutSecs": 240,
                                "waitUntil": "domcontentloaded"},
                        max_items=2 * n_req, max_charge_usd=max_apify_usd, timeout_s=1800, memory_mb=2048,
                        clean=False, with_log=True,
                        est_usd=RESIDENTIAL_USD_PER_PAGE * n_req if residential else None)
    except Exception as e:  # close the run, then re-raise
        finish_run(conn, run, "error", {}, error=repr(e)[:500])
        raise
    paid = {"apify_usd": res.usage_usd, "apify_runs": [run_record(res)]}  # Apify's real charge, in counts
    raw_dir = settings.RAW_DIR / "render"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"run{run}.json"
    raw_path.write_text(json.dumps({"actor": ACTOR, "apify_run": res.run_id, "status": res.status,
                                    "cost_usd": res.cost_usd, "items": res.items, "log": res.log}))
    failed = failed_requests(res.items)
    pages: dict[str, list[dict[str, Any]]] = {}
    for it in res.items:
        if not it.get("#error") and it.get("site") in pick:
            pages.setdefault(it["site"], []).append(it)
    spent, n_obs = 0.0, 0
    try:
        for sid, s in pick.items():
            its = pages.get(sid, [])
            o = site_outcome(s["url"], its, failed, res.log)
            followed = {u for i in its for u in i.get("followed") or []}
            info = {"http_status": o.get("http_status"), "error": o.get("error"), "pages": len(its),
                    "unit_pages": sum(i.get("kind") == "units" for i in its),
                    "pages_failed": [f"{f['url']}: {(f['messages'] or ['?'])[-1]}" for f in failed
                                     if any(same_url(f["url"], u) for u in followed)]}
            if o["outcome"] != "read":
                per_area[f"{sid}#render"] = {"state": "failed", "items": 0, "outcome": o["outcome"], **info}
                report[sid] = describe({**o, "at": None})
                continue
            if spent >= max_claude_usd:
                per_area[f"{sid}#render"] = {"state": "failed", "items": 0, "outcome": "not_read", **info}
                report[sid] = "Claude budget reached"
                continue
            text = site_text(its)
            answer, rx, cost, pk = ask(s.get("name") or sid, text, model, min(0.3, max_claude_usd - spent + 0.05))
            spent += cost
            plans = check_plans(answer, text)
            addr, _ = _site_address(conn, sid, s)
            url = next((i["url"] for i in its if i.get("child")), its[0]["url"])
            prx = page_restriction(rx, text, s["url"])
            ppk = page_parking(pk, text, s["url"])
            drawings = page_drawings(its)
            for p in plans:
                drawing = match_drawing(p.get("unit"), drawings) or match_drawing(p["name"], drawings)
                key = _slug(p.get("unit") or p["name"])
                avail = _avail(p["available"], date.today()) if p.get("available") else None
                data = ObservationData(address=addr, kind="apartment", term="long", property_name=s.get("name"),
                                       floor_plan=p["name"], unit=p.get("unit"), beds=p.get("beds"),
                                       baths=p.get("baths"), sqft=p.get("sqft"), available_from=avail,
                                       furnished=p.get("furnished"),
                                       extra={"site": sid, "platform": "render", "area": f"{sid}#render",
                                              "available_label": p.get("available"), "model": model,
                                              "clicked": sorted({c for i in its for c in i.get("clicked") or []}),
                                              **({"floorplan_image": drawing} if drawing else {}),
                                              **({"page_restriction": prx} if prx else {}),
                                              **({"page_parking": ppk} if ppk else {})})
                append_observation(conn, run, "direct", f"{sid}:r:{key}", data, url=url, price=p.get("price"),
                                   price_basis="month" if p.get("price") else None, raw_path=str(raw_path),
                                   commit=False)
                n_obs += 1
            conn.commit()  # this site's rows, before the next site's Claude call: never hold the write lock across it
            oc = "ok" if plans else "no_plans"
            per_area[f"{sid}#render"] = {"state": STATE_OF[oc], "items": len(plans), "outcome": oc, **info}
            report[sid] = [_line(p) for p in plans] if plans else "no plans on the page"
            if prx and plans:
                report[sid].append(f"who may rent: {prx['value']}"
                                   + (f' "{prx["evidence"]}"' if prx.get("evidence") else ""))
    except (Blocker, subprocess.TimeoutExpired) as e:
        conn.commit()
        finish_run(conn, run, "error", {"areas": per_area, "observations": n_obs, **paid, "apify_usd": settle(paid["apify_runs"])},
                   est_cost=round((res.cost_usd or 0) + spent, 4), error=str(e))
        raise Blocker(str(e)) from e
    except Exception as e:  # close the run (its commit keeps the sites read so far), then re-raise
        finish_run(conn, run, "error", {"areas": per_area, "observations": n_obs, **paid, "apify_usd": settle(paid["apify_runs"])},
                   est_cost=round((res.cost_usd or 0) + spent, 4), error=repr(e)[:500])
        raise
    conn.commit()
    finish_run(conn, run, "ok", {"areas": per_area, "observations": n_obs, "items": len(res.items), **paid, "apify_usd": settle(paid["apify_runs"])},
               est_cost=round((res.cost_usd or 0) + spent, 4))
    identity.run(conn)
    return {"run_id": run, "apify_usd": res.cost_usd, "claude_usd": round(spent, 4), "observations": n_obs,
            "marked_duplicate": marked,
            "outcomes": {sid: {k: v for k, v in a.items() if k != "state"} for sid, a in
                         ((k[: -len("#render")], a) for k, a in per_area.items())},
            "sites": report}


def reread_restrictions(conn, *, sites: list[str] | None = None, model: str = "haiku", max_usd: float = 1.0,
                        workers: int = 4, dry_run: bool = False, parking: bool = False) -> dict[str, Any]:
    """Who may rent, read again from the raw page text of sites already rendered (no Apify): for each site with
    active rendered plan rows (`<site>:r:%`), the pages of its newest raw file go to Haiku (`ask_restriction`);
    each plan row whose reading changed gets a new observation, its scraped data plus `extra.page_restriction`, or
    without it when the answer is now unknown (`cleared`; append-only; run
    component `render_restriction`), then the cached extract answers are applied over them (free), so the plan
    rows' own readings stay current. `sites`: only these. A site whose raw file is gone is reported, not read.
    `parking`: read the community's parking instead (`ask_parking`, stored as `extra.page_parking`; run component
    `render_parking`); the plan row's other page readings stay, as its newest scraped observation carries them."""
    from concurrent.futures import ThreadPoolExecutor

    from .extract import extract

    rows: dict[str, list[Any]] = {}
    for r in conn.execute("SELECT * FROM listings WHERE source = 'direct' AND active = 1 AND source_id LIKE '%:r:%' "
                          "ORDER BY source_id"):
        sid = r["source_id"].split(":r:")[0]
        if not sites or sid in sites:
            rows.setdefault(sid, []).append(r)
    texts: dict[str, tuple[str, str]] = {}
    report: dict[str, Any] = {}
    files: dict[str, Any] = {}
    all_sites = load_sites(duplicates=True)
    for sid, rs in rows.items():
        raw = max((x["raw_path"] for x in rs if x["raw_path"]), default=None,
                  key=lambda p: int(re.sub(r"\D", "", p.rsplit("/", 1)[-1]) or 0))
        path = raw and next((c for c in (raw, settings.RAW_DIR / "render" / raw.rsplit("/", 1)[-1])
                             if os.path.isfile(c)), None)
        if not path:
            report[sid] = f"raw file missing: {raw}"
            continue
        if path not in files:
            files[path] = json.loads(open(path).read()).get("items") or []
        its = [i for i in files[path] if not i.get("#error") and i.get("site") == sid]
        if not its:
            report[sid] = f"no pages of this site in {raw}"
            continue
        texts[sid] = (site_text(its), (all_sites.get(sid) or {}).get("url") or its[0]["url"])
    plan = {"sites": len(rows), "to_read": len(texts), "rows": sum(len(rows[s]) for s in texts),
            "estimated_claude_usd": round(0.004 * len(texts), 2)}
    if dry_run or not texts:
        return {**plan, "report": report}
    key, comp = ("page_parking", "render_parking") if parking else ("page_restriction", "render_restriction")
    run = start_run(conn, comp, "direct", {"model": model, "sites": len(texts)})
    spent, n_obs, counts = 0.0, 0, {}
    err = None

    def one(sid: str) -> tuple[str, Any, float]:
        name = (all_sites.get(sid) or {}).get("name") or rows[sid][0]["property_name"] or sid
        rx, cost = (ask_parking if parking else ask_restriction)(name, texts[sid][0], model, 0.2)
        return sid, rx, cost

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = []
            for sid in texts:
                futs.append(pool.submit(one, sid))
            for f in futs:  # in order; the database is written only here, in this thread
                try:
                    sid, rx, cost = f.result()
                except (Blocker, subprocess.TimeoutExpired, OSError) as e:
                    err = err or e
                    continue
                spent += cost
                text, url = texts[sid]
                prx = (page_parking if parking else page_restriction)(rx, text, url)
                if parking:
                    v = f"{prx['offered'] or '?'}/{prx['cost'] or 'unspecified'}" if prx else "unknown"
                    report[sid] = v + (f" spots={prx['spots']} ${prx['usd_month']} {prx['kinds']} "
                                       f"{' | '.join(prx['evidence'])}" if prx else "")
                else:
                    v = prx["value"] if prx else "unknown"
                    report[sid] = f"{v}" + (f': "{prx["evidence"]}"' if prx and prx.get("evidence") else "")
                counts[v] = counts.get(v, 0) + 1
                for r in rows[sid]:
                    base = conn.execute(  # the scraped observation, not an extract reading
                        "SELECT * FROM observations WHERE source = 'direct' AND source_id = ? "
                        "AND json_extract(data, '$.extra.extracted') IS NULL ORDER BY id DESC LIMIT 1",
                        (r["source_id"],)).fetchone() or r
                    d = json.loads(base["data"])
                    extra = dict(d.get("extra") or {})
                    if extra.get(key) == prx:
                        continue  # unchanged: nothing to append
                    if prx:
                        extra[key] = prx
                    else:  # unknown now: an earlier reading the stricter check rejects is dropped
                        del extra[key]
                        counts["cleared"] = counts.get("cleared", 0) + 1
                    d["extra"] = extra
                    append_observation(conn, run, "direct", r["source_id"], ObservationData(**d), url=base["url"],
                                       price=base["price"], price_basis=base["price_basis"],
                                       raw_path=base["raw_path"], commit=False)
                    n_obs += 1
                conn.commit()
                if spent >= max_usd:
                    err = err or Blocker(f"--max-usd {max_usd} spent")
                    for g in futs:
                        g.cancel()
    except Exception as e:
        conn.commit()
        finish_run(conn, run, "error", {**plan, "observations": n_obs, ("parking" if parking else "restriction"): counts},
                   est_cost=round(spent, 4), error=repr(e)[:500])
        raise
    finish_run(conn, run, "error" if err else "ok", {**plan, "observations": n_obs, ("parking" if parking else "restriction"): counts},
               est_cost=round(spent, 4), error=str(err)[:500] if err else None)
    ex = extract(conn, "direct", cached_only=True, rebuild_identity=False, id_like="%:r:%") if n_obs else None
    identity.run(conn)
    return {"run_id": run, "claude_usd": round(spent, 4), "observations": n_obs, ("parking" if parking else "restriction"): counts,
            "extract_cached": {k: ex.get(k) for k in ("listings", "observations", "restriction")} if ex else None,
            "error": str(err) if err else None, "report": report}
