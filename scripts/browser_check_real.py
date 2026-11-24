"""Headless Chrome check of the real-data dashboard (read-only: no ratings or checks are written).

  uv run --no-project --python 3.12 --with playwright python -I scripts/browser_check_real.py <shot dir> [url]
"""

import sys
import time

from playwright.sync_api import sync_playwright

OUT = sys.argv[1]
URL = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8766/"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    t = time.time()
    page.goto(URL)
    page.wait_for_selector(".card", timeout=60000)
    print(f"first cards after {time.time() - t:.1f}s; count: {page.inner_text('#count')}")
    time.sleep(3)
    print("todo chips:", page.locator(".chip.todo").count(), "| zone circles:",
          page.evaluate("document.querySelectorAll('path.leaflet-interactive, path').length"))
    page.screenshot(path=f"{OUT}/real_1_main.png")
    page.uncheck("#todo")
    print("without TODO areas:", page.inner_text("#count"))
    page.check("#todo")
    page.fill("#q", "Avalon Mountain View")
    time.sleep(0.5)
    page.locator(".card").first.click()
    page.wait_for_selector("#drawer:not(.hidden) h2")
    time.sleep(2)
    print("drawer:", page.inner_text("#drawer h2"))
    print("photos:", page.locator("#drawer .media img").count(),
          "| loaded:", page.evaluate("[...document.querySelectorAll('#drawer .media img')].filter(i => i.naturalWidth > 0).length"))
    rows = page.locator("#drawer h3:has-text('Floor plans') + div table tr")
    print("plan rows:", rows.count() - 1)
    for i in range(1, min(rows.count(), 6)):
        print("   ", rows.nth(i).inner_text().replace("\t", " | ")[:140])
    page.locator("#drawer").screenshot(path=f"{OUT}/real_2_drawer.png")
    page.click("[data-close=drawer]")
    page.fill("#q", "The Dean")
    time.sleep(0.5)
    page.locator(".card").first.click()
    page.wait_for_selector("#drawer:not(.hidden) h2")
    time.sleep(4)
    print("drawer:", page.inner_text("#drawer h2"), "| photos loaded:",
          page.evaluate("[...document.querySelectorAll('#drawer .media img')].filter(i => i.naturalWidth > 0).length"),
          "| plan images loaded:", page.evaluate("[...document.querySelectorAll('#drawer .plan-img')].filter(i => i.naturalWidth > 0).length"))
    page.locator("#drawer").screenshot(path=f"{OUT}/real_3_dean.png")
    page.click("[data-close=drawer]")
    for name in ("Sevens", "Fayette Arms"):  # photos from the community's own website (platform "photos")
        page.fill("#q", name)
        time.sleep(0.5)
        page.locator(".card").first.click()
        page.wait_for_selector("#drawer:not(.hidden) h2")
        time.sleep(4)
        print("drawer:", page.inner_text("#drawer h2"), "| photos:", page.locator("#drawer .media img").count(),
              "| loaded:", page.evaluate("[...document.querySelectorAll('#drawer .media img')].filter(i => i.naturalWidth > 0).length"),
              "| first from own site:", page.evaluate("(document.querySelector('#drawer .media img') || {}).src || ''")[:60])
        page.locator("#drawer").screenshot(path=f"{OUT}/real_5_{name.split()[0].lower()}.png")
        page.click("[data-close=drawer]")
    page.fill("#q", "")
    page.click("#verify-btn")
    page.wait_for_selector("#verify-body h2", timeout=60000)
    print("verify:", page.inner_text("#verify-body h2"))
    page.screenshot(path=f"{OUT}/real_4_verify.png")
    print("console errors:", errors[:8])
    browser.close()
