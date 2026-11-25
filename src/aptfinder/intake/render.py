"""Render buildings' own websites in a real browser and read their offered plans and prices with Claude Haiku.

  aptf-intake render --chunk mountain_view [--site ID] [--dry-run]

Many community sites load floor plans, prices and availability with JavaScript, which the plain page fetch
(`direct`, cheerio) never sees. This step fetches each site in the site list (`config.load_sites`) through Apify's
`apify/playwright-scraper`: it waits for the page, clicks "view more" / "load more" / "show all" style expanders (never
anything that applies, books, contacts or signs in: AGENTS.md rule 7), scrolls to the bottom, and also opens the
site's own floor-plans page once, and up to MAX_UNIT_PAGES per-plan unit lists ("View apartments"), all inside the
community's own path. Avalon sites are skipped: their page data already gives every unit exactly.

Each site's rendered text goes to headless `claude -p --model haiku` (no tools) with a fixed schema of plans and
units. An answer counts only if what it states is on the page: the plan or unit name, the price (as written, with or
without the comma) and the sqft must appear in the text; anything else is dropped. Rows are stored as source `direct`
(the building's own site), tagged with the area `<site>#render`, so the next render of a site retires its plans that
are no longer offered without touching the site's other rows (photos).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import date
from typing import Any

from .. import identity, settings
from ..areas import chunk_of
from ..config import load_sites
from ..store import append_observation, finish_run, start_run
from ..store.models import ObservationData
from .apify import run_actor
from .discover import SECRET_ENV, Blocker
from .lease import _avail

ACTOR = "apify/playwright-scraper"
SKIP_PLATFORMS = {"avalon"}  # exact units from the page data already

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
  return { site: ud.site || null, kind, child: kind !== 'start', requested: request.url, url: page.url(),
           status: response ? (typeof response.status === 'function' ? response.status() : response.status) : null,
           title: await page.title(), clicked, followed, from: ud.from || null, label: ud.label || null,
           text: (text + (extra.length ? '\n\n[opened in place]\n' + extra.join('\n\n') : '')).slice(0, 80000) };
}""".replace("__MAX_UNIT_PAGES__", str(MAX_UNIT_PAGES))

SYSTEM = ("You read an apartment community's own website (rendered page text) and list the floor plans or units it "
          "currently offers. Use only what the text states; never infer. Copy names, prices, square feet and "
          "availability exactly as written. Skip other communities shown on the page (e.g. 'nearby' or 'other "
          "properties'). Answer in the requested JSON.")
_S = {"type": ["string", "null"]}
SCHEMA = {"type": "object", "required": ["plans"], "properties": {"plans": {"type": "array", "items": {
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


def argv(model: str, max_usd: float) -> list[str]:
    return ["claude", "-p", "--system-prompt", SYSTEM, "--tools", "", "--strict-mcp-config", "--setting-sources",
            "", "--disable-slash-commands", "--model", model, "--no-session-persistence", "--output-format", "json",
            "--json-schema", json.dumps(SCHEMA), "--max-budget-usd", f"{max_usd:.2f}"]


def ask(name: str, text: str, model: str, max_usd: float) -> tuple[list[dict[str, Any]], float]:
    prompt = f"Community: {name}\n\nRendered page text:\n{text}"
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(argv(model, max_usd) + [prompt], capture_output=True, text=True, cwd=cwd, env=env,
                              timeout=600)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise Blocker(f"claude gave no JSON (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}")
    if out.get("is_error") or out.get("subtype") != "success" or not out.get("structured_output"):
        raise Blocker(f"claude call failed: {out.get('subtype')} {str(out.get('result'))[:200]}")
    return out["structured_output"].get("plans") or [], float(out.get("total_cost_usd") or 0)


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


PAGE_BUDGET = {"start": 12000, "floorplans": 12000, "units": 6000}  # characters of each page sent to Haiku


def site_text(pages: list[dict[str, Any]]) -> str:
    """The text Haiku reads for one site: the start page, its floor-plans page, then at most MAX_UNIT_PAGES unit
    pages, each cut to its budget so unit lists are not crowded out by a long start page."""
    order = {"start": 0, "floorplans": 1, "units": 2}
    kind = lambda p: p.get("kind") or ("floorplans" if p.get("child") else "start")  # noqa: E731
    ps = sorted(pages, key=lambda p: order.get(kind(p), 3))
    units = [p for p in ps if kind(p) == "units"][:MAX_UNIT_PAGES]
    ps = [p for p in ps if kind(p) != "units"] + units
    return "\n\n".join(f"[page {p['url']}]\n{(p.get('text') or '')[:PAGE_BUDGET.get(kind(p), 6000)]}" for p in ps)


def render(conn, chunks: list[str], *, sites: list[str] | None = None, model: str = "haiku",
           max_claude_usd: float = 1.0, max_apify_usd: float = 1.0, dry_run: bool = False) -> dict[str, Any]:
    all_sites = load_sites()
    pick = {}
    for sid, s in all_sites.items():
        if s.get("platform") in SKIP_PLATFORMS:
            continue
        if sites and sid not in sites:
            continue
        if not sites and s.get("chunk") not in chunks:
            r = conn.execute("SELECT lat, lon FROM listings WHERE source = 'direct' AND lat IS NOT NULL AND "
                             "source_id LIKE ? LIMIT 1", (f"{sid}:%",)).fetchone()
            if not r or chunk_of(r["lat"], r["lon"]) not in chunks:
                continue
        pick[sid] = s
    if dry_run or not pick:
        return {"sites": [(sid, s["url"]) for sid, s in pick.items()],
                "estimated_apify_usd": round(0.003 * PAGES_PER_SITE * len(pick), 2), "estimated_claude_usd": round(0.004 * len(pick), 2)}
    areas = {f"{sid}#render": sid for sid in pick}
    run = start_run(conn, "intake", "direct", {"areas": list(areas), "render": True, "scope": "partial",
                                               "actor": ACTOR, "model": model})
    res = run_actor(ACTOR, {"startUrls": [{"url": s["url"], "userData": {"site": sid}} for sid, s in pick.items()],
                            "pageFunction": PAGE_FUNCTION, "proxyConfiguration": {"useApifyProxy": True},
                            "maxRequestsPerCrawl": PAGES_PER_SITE * len(pick), "maxRequestRetries": 2,
                            "headless": True, "pageLoadTimeoutSecs": 120, "pageFunctionTimeoutSecs": 240,
                            "waitUntil": "domcontentloaded"},
                    max_items=PAGES_PER_SITE * len(pick), max_charge_usd=max_apify_usd, timeout_s=1800, memory_mb=2048)
    raw_dir = settings.RAW_DIR / "render"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"run{run}.json"
    raw_path.write_text(json.dumps({"actor": ACTOR, "apify_run": res.run_id, "status": res.status,
                                    "cost_usd": res.cost_usd, "items": res.items}))
    pages: dict[str, list[dict[str, Any]]] = {}
    for it in res.items:
        if it.get("site") in pick:
            pages.setdefault(it["site"], []).append(it)
    spent, per_area, n_obs, report = 0.0, {}, 0, {}
    try:
        for sid, s in pick.items():
            its = pages.get(sid, [])
            text = site_text(its)
            if not its or len((text or "").strip()) < 200:
                per_area[f"{sid}#render"] = {"state": "failed", "items": 0}
                report[sid] = "page not rendered (blocked or empty)"
                continue
            if spent >= max_claude_usd:
                per_area[f"{sid}#render"] = {"state": "partial", "items": 0}
                report[sid] = "Claude budget reached"
                continue
            answer, cost = ask(s.get("name") or sid, text, model, min(0.3, max_claude_usd - spent + 0.05))
            spent += cost
            plans = check_plans(answer, text)
            addr, _ = _site_address(conn, sid, s)
            url = next((i["url"] for i in its if i.get("child")), its[0]["url"])
            for p in plans:
                key = _slug(p.get("unit") or p["name"])
                avail = _avail(p["available"], date.today()) if p.get("available") else None
                data = ObservationData(address=addr, kind="apartment", term="long", property_name=s.get("name"),
                                       floor_plan=p["name"], unit=p.get("unit"), beds=p.get("beds"),
                                       baths=p.get("baths"), sqft=p.get("sqft"), available_from=avail,
                                       furnished=p.get("furnished"),
                                       extra={"site": sid, "platform": "render", "area": f"{sid}#render",
                                              "available_label": p.get("available"), "model": model,
                                              "clicked": sorted({c for i in its for c in i.get("clicked") or []})})
                append_observation(conn, run, "direct", f"{sid}:r:{key}", data, url=url, price=p.get("price"),
                                   price_basis="month" if p.get("price") else None, raw_path=str(raw_path),
                                   commit=False)
                n_obs += 1
            per_area[f"{sid}#render"] = {"state": "complete" if plans else "empty", "items": len(plans)}
            report[sid] = [_line(p) for p in plans] if plans else "no plans on the page"
    except (Blocker, subprocess.TimeoutExpired) as e:
        conn.commit()
        finish_run(conn, run, "error", {"areas": per_area, "observations": n_obs},
                   est_cost=round((res.cost_usd or 0) + spent, 4), error=str(e))
        raise Blocker(str(e)) from e
    conn.commit()
    finish_run(conn, run, "ok", {"areas": per_area, "observations": n_obs, "items": len(res.items)},
               est_cost=round((res.cost_usd or 0) + spent, 4))
    identity.run(conn)
    return {"run_id": run, "apify_usd": res.cost_usd, "claude_usd": round(spent, 4), "observations": n_obs,
            "sites": report}
