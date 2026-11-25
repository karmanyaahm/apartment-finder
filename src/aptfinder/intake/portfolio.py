"""Operator portfolios: the big landlords' own city pages, listing their communities, as buildings (`site_info`).

  aptf-intake run portfolio [--area ID] [--dry-run]

Each operator in `[portfolio] operators` (config/sources.toml) has a URL template per city (`{slug}` =
'mountain-view'). Every operator x city page is fetched through Apify's `apify/playwright-scraper` (one run for all
pages; AGENTS.md rule 6): it waits for the page, clicks 'load more' / 'view all' style expanders (never apply, tour,
contact, sign in: rule 7), scrolls, and returns the page text and every link on it.

One generic reader for every operator, no per-site parser: headless Claude Haiku (`claude -p`, no tools) lists the
communities the page shows (name, street address, city, link), and its answer is written into the raw item before the
raw file is saved, so `reparse` re-applies the checks for free. The checks (`parse`) keep a community only if its name
is on the page; its address only if the street part is on the page; its link only if that exact link is on the page
and on the operator's own host. The community link is `extra.website` (a lead for `promote-sites`) and its path is the
source_id, so a community shown on two city pages is one row.

A page that answers 404 or shows no community gives nothing; a 403 / 429 / captcha page, or one the actor never
returned, is unparsed, named in the run warnings and makes that page's city partial (`failed_cities`; a blocker for
that operator, not worked around); the run's other cities are complete.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlsplit

from ..store.models import ObservationData
from .apify import ActorRun, browser_proxy, run_actor
from .base import Adapter, Parsed
from .direct import SITE_INFO
from .discover import SECRET_ENV, Blocker

ACTOR = "apify/playwright-scraper"

PAGE_FUNCTION = r"""async function pageFunction(context) {
  const { page, request, response } = context;
  const MORE = /^(show|load|view|see)\s+(more|all)\b|more\s+(communities|properties|apartments|results)|^view\s+all/i;
  const NEVER = /apply|tour|contact|schedule|lease\s*now|submit|sign\s*(in|up)|log\s*in|book|call|email|text\s+us|chat|reserve|deposit/i;
  try { await page.waitForLoadState('networkidle', { timeout: 20000 }); } catch (e) {}
  const clicked = [];
  for (let round = 0; round < 6; round++) {
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
  const links = await page.$$eval('a[href]', (as) => as.map((a) => [a.href, (a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 120)]));
  const text = await page.evaluate(() => document.body ? document.body.innerText : '');
  return { operator: request.userData.operator, city: request.userData.city, requested: request.url, url: page.url(),
           status: response ? (typeof response.status === 'function' ? response.status() : response.status) : null,
           title: await page.title(), clicked, links: links.slice(0, 1500), text: text.slice(0, 80000) };
}"""

SYSTEM = ("You read a property management company's web page (rendered text and its links) and list every apartment "
          "community the page shows as a result or card. Use only what the text states; never infer. Copy names and "
          "addresses exactly as written, and pick each community's link from the given link list (the link to that "
          "community's own page). Skip navigation, blog posts, job listings and anything that is not a community. "
          "Answer in the requested JSON.")
_S = {"type": ["string", "null"]}
SCHEMA = {"type": "object", "required": ["communities"], "properties": {"communities": {"type": "array", "items": {
    "type": "object", "required": ["name"],
    "properties": {
        "name": {**_S, "description": "community name as written"},
        "address": {**_S, "description": "street address as written (number and street), if shown"},
        "city": {**_S, "description": "city as written, if shown"},
        "url": {**_S, "description": "the community's link, copied exactly from the link list"},
    }}}}}

BLOCKED = re.compile(r"access denied|attention required|verify you are (a )?human|captcha|request blocked|"
                     r"too many requests|unusual traffic", re.IGNORECASE)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _host(u: str) -> str:
    return re.sub(r"^www\.", "", urlsplit(u).netloc.lower())


def _key(u: str) -> str:
    """A link without scheme, www, query, fragment or trailing slash: the same community from two pages matches."""
    p = urlsplit(u)
    return f"{_host(u)}{p.path.rstrip('/').lower()}"


def page_state(item: dict[str, Any]) -> str:
    """ok | empty (404 or nothing on it) | blocked (403, 429, captcha) for one fetched page."""
    st = item.get("status")
    if st in (403, 429) or (st and st >= 500) or BLOCKED.search((item.get("title") or "") + " " + (item.get("text") or "")[:2000]):
        return "blocked"
    if st == 404 or len((item.get("text") or "").strip()) < 200:
        return "empty"
    return "ok"


def ask(operator: str, item: dict[str, Any], model: str, max_usd: float) -> tuple[list[dict[str, Any]], float]:
    host = _host(item.get("url") or item.get("requested") or "")
    links = sorted({(h.split("#")[0], t) for h, t in item.get("links") or [] if _host(h) == host and t})[:500]
    prompt = (f"Operator: {operator}\nPage: {item.get('url')}\n\nLinks on the page (url | text):\n"
              + "\n".join(f"{h} | {t}" for h, t in links)
              + f"\n\nRendered page text:\n{(item.get('text') or '')[:24000]}")
    argv = ["claude", "-p", "--system-prompt", SYSTEM, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--disable-slash-commands", "--model", model, "--no-session-persistence", "--output-format", "json",
            "--json-schema", json.dumps(SCHEMA), "--max-budget-usd", f"{max_usd:.2f}"]
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(argv + [prompt], capture_output=True, text=True, cwd=cwd, env=env, timeout=600)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise Blocker(f"claude gave no JSON (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}")
    if out.get("is_error") or out.get("subtype") != "success" or not out.get("structured_output"):
        raise Blocker(f"claude call failed: {out.get('subtype')} {str(out.get('result'))[:200]}")
    return out["structured_output"].get("communities") or [], float(out.get("total_cost_usd") or 0)


def _slug(city: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")


def build_input(cfg: dict[str, Any], areas: list, residential: bool = False) -> dict[str, Any]:
    """Pages of the operators on one proxy: `proxy = "residential"` in an operator's config entry puts its pages
    in the residential run (operators that refuse Apify's datacenter proxy), the rest go through the default."""
    ops = [op for op in cfg["operators"] if (op.get("proxy") == "residential") == residential]
    reqs = [{"url": op["url"].format(slug=_slug(a.id)), "userData": {"operator": op["name"], "city": a.id}}
            for a in areas for op in ops]
    return {"startUrls": reqs, "pageFunction": PAGE_FUNCTION, **browser_proxy(residential),
            "maxRequestsPerCrawl": len(reqs), "maxRequestRetries": 1, "headless": True, "pageLoadTimeoutSecs": 90,
            "pageFunctionTimeoutSecs": 150, "waitUntil": "domcontentloaded"}


def fetch(cfg: dict[str, Any], areas: list, cap_each: int, max_usd: float, conn) -> ActorRun:
    run: ActorRun | None = None
    for residential in (False, True):  # one actor run per proxy (a run has one proxy configuration)
        inp = build_input(cfg, areas, residential)
        if not inp["startUrls"]:
            continue
        part = run_actor(cfg["actor"], inp, max_items=len(inp["startUrls"]), max_charge_usd=max_usd,
                         timeout_s=1800, memory_mb=cfg.get("memory_mb", 2048))
        got = {it.get("requested") for it in part.items}
        for r in inp["startUrls"]:  # a page the actor gave up on: keep a stub so it shows as failed, not as nothing
            if r["url"] not in got:
                part.items.append({**r["userData"], "requested": r["url"], "url": r["url"], "status": None,
                                   "page_state": "failed"})
        for it in part.items:
            it["proxy"] = "residential" if residential else "datacenter"
        if run is None:
            run = part
        else:
            run.items += part.items
            run.cost_usd = round((run.cost_usd or 0.0) + (part.cost_usd or 0.0), 4)
            run.usage_usd = round((run.usage_usd or 0.0) + (part.usage_usd or 0.0), 4)
            run.usage = {k: round((run.usage or {}).get(k, 0.0) + (part.usage or {}).get(k, 0.0), 4)
                         for k in {*(run.usage or {}), *(part.usage or {})}}
            run.run_id = f"{run.run_id},{part.run_id}"
            if part.status != "SUCCEEDED":
                run.status = part.status
    if run is None:
        raise ValueError("no operator pages to fetch (config/sources.toml [portfolio] operators)")
    claude_usd = 0.0
    for it in run.items:
        if it.get("page_state") == "failed":
            continue
        it["page_state"] = page_state(it)
        if it["page_state"] != "ok":
            continue
        if claude_usd >= cfg.get("max_claude_usd", 0.5):
            it["communities_error"] = "Claude budget for this run reached"
            continue
        try:
            it["communities"], c = ask(it.get("operator") or "", it, cfg.get("model", "haiku"), 0.3)
        except (Blocker, subprocess.TimeoutExpired) as e:  # keep the paid page: the raw file is written after this
            it["communities_error"] = f"claude: {e}"[:300]
            continue
        it["claude_usd"] = c
        it["model"] = cfg.get("model", "haiku")
        claude_usd += c
    run.cost_usd = round((run.cost_usd or 0.0) + claude_usd, 4)
    return run


def failed_cities(items: list[dict[str, Any]]) -> list[str]:
    """The cities with a page that was blocked, never returned or not read: only those are partial (a failed Irvine
    Company page for Stanford says nothing about Sunnyvale). Read from the raw items, so `reparse --covers auto`
    recomputes it for free."""
    return sorted({it.get("city") for it in items
                   if (it.get("page_state") in ("blocked", "failed") or it.get("communities_error")) and it.get("city")})


def check(item: dict[str, Any]) -> list[dict[str, Any]]:
    """The model's communities, kept only as far as the page shows them (see module doc)."""
    text = f" {_norm(item.get('text') or '')} "
    host = _host(item.get("url") or item.get("requested") or "")
    links = {_key(h): h.split("#")[0] for h, _ in item.get("links") or [] if _host(h) == host}
    page = _key(item.get("url") or "")
    out = []
    for c in item.get("communities") or []:
        name = (c.get("name") or "").strip()
        if not name or f" {_norm(name)} " not in text:
            continue
        q: dict[str, Any] = {"name": name}
        addr = (c.get("address") or "").strip()
        street = addr.split(",")[0]
        if re.match(r"\d", street) and f" {_norm(street)} " in text:
            q["street"] = street
        city = (c.get("city") or "").strip()
        if city and f" {_norm(city)} " in text:
            m = re.match(r"([^,]+?)(?:,\s*(?:CA|California))?(?:\s*,?\s*(\d{5}))?$", city)
            q["city"] = m.group(1).strip() if m else city.split(",")[0].strip()
            if m and m.group(2):
                q["zip"] = m.group(2)
        url = (c.get("url") or "").strip()
        if url and _key(url) in links and _key(url) != page:
            q["url"] = links[_key(url)]
        out.append(q)
    return out


def parse(item: dict[str, Any], cfg: dict[str, Any]) -> list[Parsed] | None:
    state = item.get("page_state") or page_state(item)
    if state in ("blocked", "failed") or item.get("communities_error"):
        return None  # counted as unparsed and warned about; never credited
    op = item.get("operator")
    out = []
    for c in check(item):
        city = c.get("city") or None
        addr = (f"{c['street']}, {city or item.get('city')}, CA {c.get('zip') or ''}".strip()
                if c.get("street") else None)
        sid = _key(c["url"]) if c.get("url") else f"{_slug(op or '')}:{_slug(c['name'])}"
        out.append(Parsed(
            source_id=sid, url=c.get("url") or item.get("url"), price=None, price_basis=None,
            # the operator only when the community links to the operator's own site: Avalon's Mountain View page
            # also shows Reserve at Mountain View, linked to equityapartments.com (2026-11-24)
            data=ObservationData(address=addr, city=city, zip=c.get("zip"), kind=SITE_INFO, property_name=c["name"],
                                 operator=op if c.get("url") else None,
                                 extra={"website": c.get("url"), "operator_page": item.get("url"), "listed_by": op,
                                        "searched_city": item.get("city"), "model": item.get("model"),
                                        "note": "operator portfolio page: the building and its link; not an offer"})))
    return out


def area_of(item: dict[str, Any], ids: list[str]) -> str | None:
    return item.get("city") if item.get("city") in ids else None


ADAPTER = Adapter(name="portfolio", build_input=build_input, parse=parse, area_kind="city", batch=True,
                  area_of=area_of, required=(), fetch=fetch, area_failures=failed_cities)
