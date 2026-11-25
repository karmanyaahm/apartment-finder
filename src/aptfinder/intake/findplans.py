"""Find a building's floor plans and prices when its own site gave nothing usable (Verify's own-site list).

  aptf-intake find-plans --place KEY [--place KEY ...] [--max-apify-usd 0.6] [--max-claude-usd 1.0]

Two ways, cheapest first, compared in one pilot:

* **search** (`search_first`): web search "<building> <city> floor plans" through headless `claude -p` with WebSearch
  as its only tool (the user's subscription, no Apify credit; discover-sites' mechanism); the first result that is
  not an aggregator, not a form, and on the building's own host (a host a known site of the place uses) or naming
  the building (`discover.name_tokens`) is rendered.
* **click** (`click_agent`): from the building's known site (or the search pick's home page when none is known),
  render the page, give Haiku its visible text plus its links and buttons, and let it pick ONE next step: follow a
  link, click a named button or tab in place (never apply, tour, contact, schedule, log in or submit: AGENTS.md
  rule 7; the page function refuses those names and submit buttons), read this page, or stop (no plans online,
  only a contact form). At most MAX_HOPS hops a site. Every hop (URL, choice, reason) goes in the raw file.

Pages are rendered through Apify's `apify/playwright-scraper` (datacenter proxy), one actor run per round for every
site still open, with a page function (AGENT_PAGE) that waits for the page, reads the text of every frame (plan
widgets live in iframes), lists links and buttons, and takes a screenshot (JPEG, first SHOT_MAX_PX pixels). The
final page of each way is read by render's reader (`render.ask` + `render.check_plans`: a plan counts only if its
name, price and sqft are on the page), which also reads who may rent (`render.page_restriction`). When the text
gives no priced plan, Haiku reads the screenshot (vision, tiles of TILE_PX); plans read only from the picture are
marked `from_screenshot` (they cannot be checked against the text).

Pilot only: writes a raw file under data/raw/findplans/ and a report; stores no observations (the user decides the
full run after the pilot)."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from .. import settings
from .apify import browser_proxy, run_actor, run_record, settle
from .discover import SECRET_ENV, Blocker, is_aggregator, is_form, name_tokens
from .render import ACTOR, ask, check_plans, page_restriction

MAX_HOPS = 3
SHOT_MAX_PX = 6000  # screenshot height cap
TILE_PX = 1500  # screenshot tiles sent to Haiku
MAX_TILES = 4
USD_PER_PAGE = 0.004  # datacenter playwright page with a screenshot, estimate before the run (real charge recorded)
NEVER = r"apply|tour|contact|schedule|lease\s*now|submit|sign\s*(in|up)|log\s*in|book|call|email|text\s+us|chat|reserve|deposit|pay\b|portal|resident"

AGENT_PAGE = r"""async function pageFunction(context) {
  const { page, request, response } = context;
  const ud = request.userData || {};
  const NEVER = new RegExp(__NEVER__, 'i');
  const PRICE = /\$\s?\d[\d,]{2,}/;
  try { await page.waitForLoadState('networkidle', { timeout: 20000 }); } catch (e) {}
  const frameText = async () => {
    const out = [];
    for (const f of page.frames()) {
      try {
        const t = await f.evaluate(() => document.body ? document.body.innerText : '');
        if (t && t.trim().length > 40) out.push({ url: f.url(), main: f === page.mainFrame(), text: t });
      } catch (e) {}
    }
    return out;
  };
  // wait up to 10 s for a price to show anywhere (widgets fill in late)
  for (let i = 0; i < 5; i++) {
    if ((await frameText()).some((f) => PRICE.test(f.text))) break;
    await page.waitForTimeout(2000);
  }
  const clicked = [];
  let refused = null;
  if (ud.click) {
    const want = ud.click.trim().toLowerCase();
    if (NEVER.test(want)) { refused = 'refused by name: ' + ud.click; }
    else {
      let done = false;
      for (const f of page.frames()) {
        if (done) break;
        for (const el of await f.$$('a, button, [role=button], [role=tab], li, summary, label')) {
          const t = ((await el.innerText().catch(() => '')) || '').trim().replace(/\s+/g, ' ');
          if (!t || t.toLowerCase() !== want) continue;
          const bad = await el.evaluate((e) => !!(e.closest('form') && (e.type === 'submit' || e.getAttribute('type') === 'submit'))).catch(() => true);
          if (bad) { refused = 'refused: a form submit button'; continue; }
          if (!(await el.isVisible().catch(() => false))) continue;
          try { await el.click({ timeout: 4000 }); clicked.push(t); done = true; } catch (e) {}
          break;
        }
      }
      if (done) {
        try { await page.waitForLoadState('networkidle', { timeout: 10000 }); } catch (e) {}
        for (let i = 0; i < 4; i++) {
          if ((await frameText()).some((f) => PRICE.test(f.text))) break;
          await page.waitForTimeout(2000);
        }
      } else if (!refused) { refused = 'not found on the page: ' + ud.click; }
    }
  }
  await page.evaluate(() => window.scrollTo(0, document.body ? document.body.scrollHeight : 0)).catch(() => {});
  await page.waitForTimeout(1500);
  const frames = await frameText();
  const priced = frames.filter((f) => !f.main && PRICE.test(f.text));
  const main = frames.find((f) => f.main);
  const rest = frames.filter((f) => !f.main && !PRICE.test(f.text));
  const text = [...priced.map((f) => '[frame ' + f.url + ']\n' + f.text.slice(0, 20000)),
                main ? main.text : '', ...rest.map((f) => '[frame ' + f.url + ']\n' + f.text.slice(0, 5000))].join('\n\n');
  const links = [], buttons = [];
  for (const f of page.frames()) {
    const ls = await f.$$eval('a[href]', (as) => as.map((a) => [(a.innerText || a.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 80), a.href])).catch(() => []);
    for (const [t, h] of ls) if (h && !h.startsWith('javascript') && !h.startsWith('mailto') && !h.startsWith('tel')) links.push([t, h]);
    const bs = await f.$$eval('button, [role=button], [role=tab], summary', (es) => es.map((e) => (e.innerText || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 60))).catch(() => []);
    for (const b of bs) if (b) buttons.push(b);
  }
  const iframes = page.frames().filter((f) => f !== page.mainFrame()).map((f) => f.url()).filter((u) => u && u !== 'about:blank');
  let shot = null;
  if (ud.shot) {
    try {
      const h = await page.evaluate(() => Math.min(document.documentElement.scrollHeight, __SHOT_MAX__));
      await page.setViewportSize({ width: 1280, height: 900 });
      const buf = await page.screenshot({ type: 'jpeg', quality: 55, fullPage: true, clip: { x: 0, y: 0, width: 1280, height: h } });
      shot = buf.toString('base64');
    } catch (e) { shot = null; }
  }
  const st = response ? (typeof response.status === 'function' ? response.status() : response.status) : null;
  return { key: ud.key || null, requested: request.url, url: page.url(), status: st, title: await page.title(),
           clicked, refused, text: text.slice(0, 80000), links: links.slice(0, 250), buttons: [...new Set(buttons)].slice(0, 80),
           iframes, shot };
}""".replace("__NEVER__", json.dumps(NEVER)).replace("__SHOT_MAX__", str(SHOT_MAX_PX))


def _claude(argv: list[str], prompt: str, timeout: int = 600, stdin: str | None = None) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(argv + ([prompt] if prompt else []), capture_output=True, text=True, cwd=cwd, env=env,
                              timeout=timeout, input=stdin)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    try:
        out = json.loads(proc.stdout) if not stdin else [json.loads(ln) for ln in lines]
    except ValueError:
        raise Blocker(f"claude gave no JSON (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}")
    if isinstance(out, list):
        out = next((x for x in reversed(out) if x.get("type") == "result"), {})
    if out.get("is_error") or out.get("subtype") != "success":
        raise Blocker(f"claude call failed: {out.get('subtype')} {str(out.get('result'))[:200]}")
    return out


BASE = ["--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence"]

# --- search ------------------------------------------------------------------------------------------------------
SEARCH_SYSTEM = ("Run exactly one web search with the query you are given and report its results in order, as they "
                 "came back: url and title of each (up to 8). Do not judge or skip any. Answer in the requested JSON.")
SEARCH_SCHEMA = {"type": "object", "required": ["results"], "properties": {"results": {"type": "array", "items": {
    "type": "object", "required": ["url", "title"],
    "properties": {"url": {"type": "string"}, "title": {"type": "string"}}}}}}


def web_search(query: str, model: str = "haiku") -> tuple[list[dict[str, str]], float]:
    argv = ["claude", "-p", "--system-prompt", SEARCH_SYSTEM, "--tools", "WebSearch", "--allowedTools", "WebSearch",
            *BASE, "--model", model, "--output-format", "json", "--json-schema", json.dumps(SEARCH_SCHEMA),
            "--max-budget-usd", "0.10"]
    out = _claude(argv, f"Query: {query}")
    searches = sum(u.get("webSearchRequests") or 0 for u in (out.get("modelUsage") or {}).values())
    if not searches:
        raise Blocker("the search answer used no web search")
    return (out.get("structured_output") or {}).get("results") or [], float(out.get("total_cost_usd") or 0)


def _host(url: str) -> str:
    return urlsplit(url or "").netloc.lower().split(":")[0].removeprefix("www.")


def pick_result(results: list[dict[str, str]], name: str, own_hosts: set[str], city: str = ""
                ) -> tuple[str | None, str]:
    """The first search result on the building's own host, or naming the building in its host and path; never an
    aggregator or a form. Words of the city are not the building's ('Palo Alto Plaza' is no paloalto.gov page)."""
    toks = [t for t in name_tokens(name) if t not in city.lower()]
    for i, r in enumerate(results):
        u = r.get("url") or ""
        if not u.startswith("http") or is_aggregator(u) or is_form(u):
            continue
        h = _host(u)
        in_host = bool(toks) and all(t in re.sub(r"[^a-z0-9]", "", h) for t in toks)
        if h in own_hosts and (in_host or not toks):
            return u, f"result {i + 1}: on the building's known host {h}"
        if h in own_hosts and all(t in re.sub(r"[^a-z0-9]", "", urlsplit(u).path.lower()) for t in toks):
            # a manager's host shared by several buildings (caprop.com): only the page naming this one
            return u, f"result {i + 1}: the building's page on its manager's host {h}"
        # the host, or the host and path, name the building (prometheusapartments.com/.../harken); another
        # site's namesake page (ashtonwoods.com/austin/.../colton, a Salem 'Encore') is caught after rendering:
        # the page must name the building's city (`names_city`)
        if toks and all(t in re.sub(r"[^a-z0-9]", "", h) for t in toks):
            return u, f"result {i + 1}: the host names the building ({', '.join(toks)})"
        if toks and all(t in re.sub(r"[^a-z0-9]", "", h + urlsplit(u).path.lower()) for t in toks):
            return u, f"result {i + 1}: the URL names the building ({', '.join(toks)})"
    return None, "no result on the building's own host or naming it"


def click_starts(picks: list[str | None], known: str | None, own_hosts: set[str]) -> list[str]:
    """Where the click agent starts, in order: a search pick on a host the building's known sites use, then the
    known site, then the other picks. A pick named only by the name (a Salem 'Encore' on prospectportal.com,
    themartingroup.com for The Martin; pilot 2026-11-25) never comes before a site the building is known by."""
    own = [u for u in picks if u and _host(u) in own_hosts]
    other = [u for u in picks if u and _host(u) not in own_hosts]
    return list(dict.fromkeys(own + ([known] if known else []) + other))


def names_city(page: dict[str, Any], city: str) -> bool:
    return not city or city.lower() in ((page.get("title") or "") + " " + (page.get("text") or "")).lower()


GOOGLE_ACTOR = "apify/google-search-scraper"  # $0.0025 a results page (BRONZE), Apify needs a $0.50 charge cap


def google_search(queries: list[str]) -> tuple[dict[str, list[dict[str, str]]], dict[str, Any]]:
    """{query: [{url, title}]} for every query in one Apify run (first results page each)."""
    res = run_actor(GOOGLE_ACTOR, {"queries": "\n".join(queries), "resultsPerPage": 10, "maxPagesPerQuery": 1,
                                   "countryCode": "us", "languageCode": "en", "mobileResults": False},
                    max_items=len(queries) + 5, max_charge_usd=0.5, timeout_s=900)
    out: dict[str, list[dict[str, str]]] = {}
    for it in res.items:
        q = ((it.get("searchQuery") or {}).get("term") or "").strip()
        out[q] = [{"url": r.get("url") or "", "title": r.get("title") or ""} for r in it.get("organicResults") or []]
    return out, run_record(res)


# --- the click agent's choice ------------------------------------------------------------------------------------
NAV_SYSTEM = (
    "You help find an apartment community's floor plans and rents on its own website. You get one rendered page: "
    "its text, its links (text -> URL) and its buttons/tabs. Choose ONE next step: 'read' if this page already "
    "shows floor plans with rents (or units with rents); 'follow' a link (give its URL exactly as listed) that most "
    "likely leads to floor plans, pricing or availability of THIS community; 'click' a button or tab (give its text "
    "exactly as listed) that reveals floor plans or prices on this page (e.g. 'SEARCH BY FLOOR PLANS', 'Floor "
    "Plans', 'View all'); or 'stop' when the site says rents are not online ('call for details', waitlist, "
    "affordable housing lottery) or only offers a contact form. Never choose anything that applies, schedules a "
    "tour, contacts, logs in, pays or submits a form. Also never a link to another community. Answer in the "
    "requested JSON.")
NAV_SCHEMA = {"type": "object", "required": ["action", "target", "reason"], "properties": {
    "action": {"type": "string", "enum": ["read", "follow", "click", "stop"]},
    "target": {"type": ["string", "null"], "description": "the link URL or the button text, null for read/stop"},
    "reason": {"type": "string", "description": "a few words, quoting the page where possible"}}}


def nav_prompt(name: str, page: dict[str, Any], tried: list[str]) -> str:
    seen, links = set(), []
    for t, h in page.get("links") or []:
        h = h.split("#")[0]
        if h in seen or not t:
            continue
        seen.add(h)
        links.append(f"- {t} -> {h}")
    return (f"Community: {name}\nPage: {page.get('url')}\nTitle: {page.get('title')}\n"
            f"Already tried: {', '.join(tried) or 'nothing'}\n\nLinks:\n" + "\n".join(links[:150])
            + "\n\nButtons and tabs:\n" + "\n".join(f"- {b}" for b in (page.get("buttons") or [])[:60])
            + f"\n\nPage text (start):\n{(page.get('text') or '')[:6000]}")


def choose(name: str, page: dict[str, Any], tried: list[str], model: str = "haiku") -> tuple[dict[str, Any], float]:
    argv = ["claude", "-p", "--system-prompt", NAV_SYSTEM, "--tools", "", *BASE, "--model", model,
            "--output-format", "json", "--json-schema", json.dumps(NAV_SCHEMA), "--max-budget-usd", "0.10"]
    out = _claude(argv, nav_prompt(name, page, tried))
    return out.get("structured_output") or {}, float(out.get("total_cost_usd") or 0)


def check_choice(c: dict[str, Any], page: dict[str, Any]) -> dict[str, Any]:
    """Refuse a step rule 7 forbids, or a link the page does not have."""
    import re as _re

    t = (c.get("target") or "").strip()
    # a button by its name; a link by discover's form detector (a path segment that applies, tours, contacts...),
    # not NEVER over the whole URL: 'floor-plans-and-virtual-tours' is no tour booking (pilot 2026-11-25)
    if c.get("action") == "click" and _re.search(NEVER, t, _re.I):
        return {"action": "stop", "target": None, "reason": f"refused (rule 7): {t}"}
    if c.get("action") == "follow":
        hrefs = {h.split("#")[0] for _, h in page.get("links") or []}
        if t.split("#")[0] not in hrefs:
            t2 = urljoin(page.get("url") or "", t)
            if t2.split("#")[0] not in hrefs:
                return {"action": "stop", "target": None, "reason": f"chose a link not on the page: {t}"}
            c = {**c, "target": t2}
        if is_form(c["target"]):
            return {"action": "stop", "target": None, "reason": f"refused: a form ({t})"}
    return c


# --- reading -----------------------------------------------------------------------------------------------------
VISION_SYSTEM = ("You read screenshots of an apartment community's own website and list the floor plans or units "
                 "shown with their rents. Copy names, prices, square feet and availability exactly as shown; never "
                 "infer. Skip other communities. Answer in the requested JSON.")


def tiles(b64: str) -> list[str]:
    from PIL import Image

    im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    out = []
    for y in range(0, min(im.height, TILE_PX * MAX_TILES), TILE_PX):
        b = io.BytesIO()
        im.crop((0, y, im.width, min(im.height, y + TILE_PX))).save(b, "JPEG", quality=70)
        out.append(base64.b64encode(b.getvalue()).decode())
    return out


def read_screenshot(name: str, b64: str, model: str = "haiku") -> tuple[list[dict[str, Any]], float]:
    from .render import SCHEMA

    schema = {"type": "object", "required": ["plans"], "properties": {"plans": SCHEMA["properties"]["plans"]}}
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": t}}
               for t in tiles(b64)]
    content.append({"type": "text", "text": f"Community: {name}. List its floor plans or units with rents."})
    msg = {"type": "user", "message": {"role": "user", "content": content}}
    argv = ["claude", "-p", "--system-prompt", VISION_SYSTEM, "--tools", "", *BASE, "--model", model,
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(schema), "--max-budget-usd", "0.20"]
    out = _claude(argv, "", stdin=json.dumps(msg) + "\n")
    plans = []  # the same bounds as the text check (a $394,240 house price is no rent)
    for p in (out.get("structured_output") or {}).get("plans") or []:
        if not p.get("name"):
            continue
        for k, lo, hi in (("price", 300, 50000), ("sqft", 100, 10000)):
            if not (isinstance(p.get(k), (int, float)) and lo <= p[k] <= hi):
                p[k] = None
        plans.append(p)
    return plans, float(out.get("total_cost_usd") or 0)


def other_address(plan_name: str, address: str | None) -> bool:
    """A plan named by a street address that is not the building's ('1055 Laurel Street' on Noel Oaks' page: the
    manager's vacancies widget lists the whole portfolio; pilot 2026-11-25)."""
    m = re.match(r"\s*(\d{2,})\s+([A-Za-z]+)", plan_name or "")
    if not m or not address or m.group(2).lower() in {"bed", "beds", "bedroom", "bedrooms", "bath", "baths", "br",
                                                       "ba", "sq", "sqft", "sf", "month", "months", "unit", "units"}:
        return False
    # the street's name only: a building spans numbers (Laurel Court, '1041 Laurel St #1055', lists '1055 Laurel
    # Street')
    return m.group(2).lower() not in re.findall(r"[a-z]+", address.split(",")[0].lower())


def read_page(name: str, page: dict[str, Any], model: str = "haiku", address: str | None = None
              ) -> tuple[dict[str, Any], float]:
    """`_read_page`, a failure kept as the site's reading (one site's error never loses the pages already paid);
    plans named by another street address are dropped (listed in `other_buildings`)."""
    try:
        res, cost = _read_page(name, page, model)
    except Exception as e:  # noqa: BLE001
        return {"error": f"reading failed: {e!r}"[:300]}, 0.0
    other = [p for p in res.get("plans") or [] if other_address(p.get("name"), address)]
    if other:
        res = {**res, "plans": [p for p in res["plans"] if p not in other],
               "other_buildings": [p.get("name") for p in other]}
    return res, cost


def _read_page(name: str, page: dict[str, Any], model: str = "haiku") -> tuple[dict[str, Any], float]:
    """Plans (checked against the text), who may rent, and the screenshot reading when the text gives no price."""
    text = f"[page {page.get('url')}]\n{(page.get('text') or '')[:24000]}"
    answer, rx, cost, *_ = ask(name, text, model, 0.3)  # (plans, restriction, cost[, parking])
    plans = check_plans(answer, text)
    res: dict[str, Any] = {"plans": plans, "restriction": page_restriction(rx, text, page.get("url") or ""),
                           "from": "text"}
    if not any(p.get("price") for p in plans) and page.get("shot"):
        vp, vc = read_screenshot(name, page["shot"], model)
        cost += vc
        if any(p.get("price") for p in vp):
            res.update(plans=[{**p, "from_screenshot": True} for p in vp], **{"from": "screenshot"})
    return res, cost


# --- the pilot ---------------------------------------------------------------------------------------------------
class Pager:
    """Renders pages through Apify in rounds, caching by (URL, click); tracks the real charge and the cap."""

    def __init__(self, max_usd: float):
        self.max_usd, self.spent, self.cache, self.runs = max_usd, 0.0, {}, []

    def fetch(self, reqs: list[tuple[str, str | None]]) -> None:
        todo = [r for r in dict.fromkeys(reqs) if r not in self.cache]
        if not todo:
            return
        est = USD_PER_PAGE * len(todo)
        if self.spent + est > self.max_usd:
            raise Blocker(f"pilot Apify cap: spent ${self.spent:.3f} + estimated ${est:.3f} > ${self.max_usd:.2f}")
        keys = {f"r{i}": r for i, r in enumerate(todo)}
        res = run_actor(ACTOR, {"startUrls": [{"url": u, "userData": {"key": k, "click": c, "shot": True}}
                                              for k, (u, c) in keys.items()],
                                "pageFunction": AGENT_PAGE, **browser_proxy(False),
                                "maxRequestsPerCrawl": len(todo), "maxRequestRetries": 1, "headless": True,
                                "pageLoadTimeoutSecs": 90, "pageFunctionTimeoutSecs": 150,
                                "waitUntil": "domcontentloaded"},
                        max_items=2 * len(todo), max_charge_usd=max(0.05, self.max_usd - self.spent),
                        timeout_s=1500, memory_mb=2048, clean=False)
        rec = run_record(res)
        self.runs.append(rec)
        self.spent += res.usage_usd or 0.0
        got = {it.get("key"): it for it in res.items if it.get("key")}
        for k, r in keys.items():
            it = got.get(k)
            if it is None:
                err = next((i for i in res.items if i.get("#error") and (i.get("#debug") or {}).get("url") == r[0]), None)
                msg = ((err or {}).get("#debug") or {}).get("errorMessages") or ["not returned"]
                it = {"url": r[0], "error": str(msg[-1])[:300], "text": "", "links": [], "buttons": []}
            self.cache[r] = it

    def get(self, url: str, click: str | None = None) -> dict[str, Any]:
        return self.cache[(url, click)]


def _priced(res: dict[str, Any] | None) -> bool:
    return bool(res and any(p.get("price") for p in res.get("plans") or []))


def pilot(targets: list[dict[str, Any]], *, max_apify_usd: float = 0.6, max_claude_usd: float = 2.0,
          model: str = "haiku", search: bool = True) -> dict[str, Any]:
    """targets: [{key, name, city, start (known own-site URL or None), own_hosts}]. Returns the per-site report;
    writes data/raw/findplans/pilot<ts>.json with every page's text, links, hops and readings (no screenshots)."""
    pager, claude = Pager(max_apify_usd), 0.0
    out: dict[str, dict[str, Any]] = {t["key"]: {"name": t["name"], "start": t.get("start"), "search": {},
                                                 "google": {}, "click": {"hops": []}} for t in targets}
    # 1. search (no Apify); `search=False`: the click agent from the known site only
    for t in (targets if search else []):
        q = f"{t['name']} {t['city']} floor plans"
        try:
            results, c = web_search(q, model)
        except Blocker as e:
            out[t["key"]]["search"] = {"query": q, "error": str(e)}
            continue
        claude += c
        url, why = pick_result(results, t["name"], t["own_hosts"], t["city"])
        out[t["key"]]["search"] = {"query": q, "results": results[:5], "pick": url, "why": why, "claude_usd": c}
    # 1b. the same queries through Google (Apify), one run for all
    queries = {t["key"]: f"{t['name']} {t['city']} floor plans" for t in targets}
    try:
        if not search:
            raise Blocker("search off (--no-search)")
        g, grec = google_search(list(queries.values()))
        pager.runs.append(grec)
        pager.spent += grec.get("usd") or 0.0
    except Exception as e:  # noqa: BLE001 (reported, the other arms go on)
        g, grec = {}, {"error": str(e)}
    for t in targets:
        results = g.get(queries[t["key"]], [])
        url, why = pick_result(results, t["name"], t["own_hosts"], t["city"])
        out[t["key"]]["google"] = {"query": queries[t["key"]], "results": results[:5], "pick": url, "why": why}
    # 2. round 1: the search picks and the click agent's start pages. Search first: the click agent starts from
    # the Google pick, else Claude's, else the known site; a start page that fails falls back to the next one
    # (pilot 2026-11-25: Noel Oaks' known URL with a trailing slash was not returned, its Google pick loaded; a
    # search pick's home page led the agent off into an operator's Texas pages)
    state: dict[str, dict[str, Any]] = {}
    trusted: dict[str, set[str]] = {}
    for t in targets:
        o = out[t["key"]]
        starts = click_starts([o["google"].get("pick"), o["search"].get("pick")], t.get("start"), t["own_hosts"])
        trusted[t["key"]] = {u for u in starts if u == t.get("start") or _host(u) in t["own_hosts"]}
        o["click"]["starts"] = starts
        if starts:
            o["click"]["start"] = starts[0]
            state[t["key"]] = {"url": starts[0], "click": None, "tried": [], "done": False, "starts": starts[1:]}
    pager.fetch([(o[a]["pick"], None) for o in out.values() for a in ("search", "google") if o[a].get("pick")]
                + [(s["url"], None) for s in state.values()])
    city = {t["key"]: t["city"] for t in targets}
    for _ in range(2):  # a failed start, or one that does not name the city: the next start page
        moved = False
        for k, s in state.items():
            pg = pager.get(s["url"])
            # a pick named only by the name (not on a host the building is known by) must name its city
            bad = (pg.get("error") or "no text") if (pg.get("error") or not pg.get("text")) else \
                (None if s["url"] in trusted[k] or names_city(pg, city[k]) else f"does not name {city[k]}")
            if not bad:
                continue
            out[k]["click"]["hops"].append({"url": s["url"], "action": "stop",
                                            "reason": f"start page failed: {str(bad)[:120]}"})
            if s["starts"]:
                s["url"] = s["starts"].pop(0)
                moved = True
            else:
                s["done"] = True
        if not moved:
            break
        pager.fetch([(s["url"], None) for s in state.values()])
    # the search arms: read their page (one reading per URL, shared by both arms)
    reads: dict[str, dict[str, Any]] = {}
    for t in targets:
        o = out[t["key"]]
        for arm in ("search", "google"):
            pick = o[arm].get("pick")
            if not pick:
                continue
            pg = pager.get(pick)
            if pg.get("error") or not pg.get("text"):
                o[arm]["read"] = {"error": pg.get("error") or "no text"}
                continue
            if not names_city(pg, t["city"]):
                o[arm]["read"] = {"error": f"the page does not name {t['city']}: another building of that name"}
                continue
            if pick not in reads:
                r, c = read_page(t["name"], pg, model, t.get("address"))
                claude += c
                reads[pick] = r
            o[arm]["read"] = reads[pick]
            o[arm]["final_url"] = pg.get("url")
    # 3. the click agent: up to MAX_HOPS choices, one Apify run per round
    names = {t["key"]: t["name"] for t in targets}
    addrs = {t["key"]: t.get("address") for t in targets}
    for hop in range(MAX_HOPS + 1):
        nxt = []
        for k, s in state.items():
            if s["done"]:
                continue
            pg = pager.get(s["url"], s["click"])
            hops = out[k]["click"]["hops"]
            if pg.get("error") or not pg.get("text"):
                hops.append({"url": s["url"], "click": s["click"], "action": "stop",
                             "reason": f"page failed: {pg.get('error') or 'no text'}"})
                s["done"] = True
                continue
            if claude >= max_claude_usd:
                hops.append({"url": s["url"], "action": "stop", "reason": "Claude cap reached"})
                s["done"] = True
                continue
            if hop == MAX_HOPS:
                c = {"action": "read", "target": None, "reason": f"hop limit ({MAX_HOPS}) reached"}
            else:
                c, cc = choose(names[k], pg, s["tried"], model)
                claude += cc
                c = check_choice(c, pg)
            hops.append({"url": pg.get("url"), "click": s["click"], "clicked": pg.get("clicked"),
                         "refused": pg.get("refused"), **c})
            if c["action"] in ("read", "stop"):
                if c["action"] == "read":
                    r, rc = read_page(names[k], pg, model, addrs[k])
                    claude += rc
                    out[k]["click"]["read"] = r
                out[k]["click"]["final_url"] = pg.get("url")
                s["done"] = True
                continue
            s["tried"].append(c["target"])
            if c["action"] == "follow":
                s["url"], s["click"] = c["target"].split("#")[0], None
            else:
                s["url"], s["click"] = pg.get("requested") or s["url"], c["target"]
            nxt.append((s["url"], s["click"]))
        if not nxt:
            break
        try:
            pager.fetch(nxt)
        except Blocker as e:
            for k, s in state.items():
                if not s["done"]:
                    out[k]["click"]["hops"].append({"action": "stop", "reason": str(e)})
                    s["done"] = True
            break
    apify = settle(pager.runs)
    raw_dir = settings.RAW_DIR / "findplans"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"pilot{int(time.time())}.json"
    pages = [{"url": u, "click": c, **{k: v for k, v in p.items() if k != "shot"}, "shot": bool(p.get("shot"))}
             for (u, c), p in pager.cache.items()]
    path.write_text(json.dumps({"actor": ACTOR, "apify_runs": pager.runs, "apify_usd": apify, "claude_usd": claude,
                                "sites": out, "pages": pages}, default=str))
    return {"raw": str(path), "apify_usd": apify, "claude_usd": round(claude, 4), "pages": len(pager.cache),
            "sites": out, "found": {k: {a: _priced(o[a].get("read")) for a in ("search", "google", "click")}
                                    for k, o in out.items()}}


REDIRECT_HOSTS = {"livewres.com", "livecreativejuices.com", "greystar.leasehomenow.com",
                  "legacypartners.aptfeatures.com"}  # listings' short-link hosts: never a start page or own host
START_RANK = {"no_plans": 1, "failed": 1, "pending": 2, "rejected": 3}


def targets(conn, *, places: list[str] | None = None, names: list[str] | None = None,
            limit: int | None = None) -> list[dict[str, Any]]:
    """Items of Verify's own-site list as pilot targets: the start page is a URL you added, else a site that
    loaded, else one not fetched yet, else one whose address check failed (never a listing's short-link host or a
    form); own hosts are the hosts of every URL known for it except rejected ones."""
    from ..verify import own_site_queue

    out = []
    for i in own_site_queue(conn):
        if (places or names) and i["place_key"] not in (places or []) and i["name"] not in (names or []):
            continue
        host = lambda u: _host(u["url"])  # noqa: E731
        urls = [u for u in i["urls"] if u["status"] != "form" and not is_aggregator(u["url"])
                and host(u) not in REDIRECT_HOSTS]
        urls.sort(key=lambda u: 0 if u["via"].startswith("you") else START_RANK.get(u["status"], 5))
        parts = [p.strip() for p in i["address"].split(",")]
        out.append({"key": i["place_key"], "name": i["name"].split(",")[0], "reason": i["reason"],
                    "address": i["address"],
                    "city": parts[1] if len(parts) > 2 else "", "start": urls[0]["url"] if urls else None,
                    "own_hosts": {host(u) for u in urls if u["status"] != "rejected"}})
        if limit and len(out) >= limit:
            break
    return out
