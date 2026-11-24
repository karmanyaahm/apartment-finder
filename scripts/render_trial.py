"""Trial: fetch community pages through Apify's browser actor (apify/playwright-scraper), so their JavaScript runs;
expand everything ("show more", "load more", "view all"), scroll to the bottom, follow the site's own floor-plans
link once, and save what each rendered page shows. Read-only: nothing goes into the database.

  uv run python scripts/render_trial.py <out.json> <url> [<url> ...]

Never clicks anything that applies, books, contacts or signs in (AGENTS.md rule 7).
"""

import json
import sys

from aptfinder.intake.apify import run_actor

PAGE_FUNCTION = r"""async function pageFunction(context) {
  const { page, request, response } = context;
  const MORE = /^(show|load|view|see)\s+(more|all)\b|more\s+(floor\s*plans|units|apartments|results)|view\s+all\s+(floor\s*plans|units|availab)|^show\s+unavailable|^all\s+floor\s*plans$/i;
  const NEVER = /apply|tour|contact|schedule|lease\s*now|submit|sign\s*(in|up)|log\s*in|book|call|email|text\s+us|chat|reserve|deposit/i;
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
      if (href && !href.startsWith('#') && !href.startsWith('javascript')) continue;  // links are not expanders
      try { await el.click({ timeout: 3000 }); clicked.push(t); n++; await page.waitForTimeout(1500); } catch (e) {}
    }
    if (!n) break;
  }
  // the site's own floor-plans page, once per start page
  if (!request.userData.child) {
    const links = await page.$$eval('a[href]', (as) => as.map((a) => [a.href, (a.innerText || '').trim()]));
    const here = page.url().split('#')[0];
    const origin = new URL(here).origin;
    const fp = links.find(([h, t]) => h.startsWith(origin) && /floor-?plans|floorplans|availability/i.test(h + ' ' + t)
                                      && !/apply|tour|contact/i.test(h) && h.split('#')[0] !== here);
    if (fp) await context.enqueueRequest({ url: fp[0], userData: { child: true, parent: request.url } });
  }
  const text = await page.evaluate(() => document.body ? document.body.innerText : '');
  return { requested: request.url, parent: request.userData.parent || null, url: page.url(),
           status: response ? (typeof response.status === 'function' ? response.status() : response.status) : null,
           title: await page.title(), clicked, text: text.slice(0, 80000) };
}"""

out, urls = sys.argv[1], sys.argv[2:]
run = run_actor("apify/playwright-scraper",
                {"startUrls": [{"url": u} for u in urls], "pageFunction": PAGE_FUNCTION,
                 "proxyConfiguration": {"useApifyProxy": True}, "maxRequestsPerCrawl": 2 * len(urls),
                 "maxRequestRetries": 2, "headless": True,
                 "pageLoadTimeoutSecs": 120, "pageFunctionTimeoutSecs": 180, "waitUntil": "domcontentloaded"},
                max_items=2 * len(urls), max_charge_usd=0.10, timeout_s=900, memory_mb=2048)
json.dump({"run": run.run_id, "status": run.status, "cost_usd": run.cost_usd, "console": run.console_url,
           "items": run.items}, open(out, "w"), indent=1)
print(json.dumps({"status": run.status, "cost_usd": run.cost_usd, "items": len(run.items),
                  "pages": [(i.get("requested"), i.get("status"), len(i.get("text") or ""), i.get("clicked"))
                            for i in run.items]}, indent=1))
