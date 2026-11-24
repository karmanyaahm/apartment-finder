"""Headless Chrome smoke test of the dashboard. Start `aptf-web --seed` first, then:
   uv run --no-project --python 3.12 --with playwright python -I scripts/browser_check.py <screenshot_dir>
Writes a rating and a comment to the seed db (rebuild with `aptf-db seed --fresh`)."""
import sys
import time

from playwright.sync_api import sync_playwright

OUT = sys.argv[1]
URL = "http://127.0.0.1:8765/"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto(URL)
    page.wait_for_selector(".card")
    time.sleep(4)  # tiles
    print("cards:", page.locator(".card").count(), "| count:", page.inner_text("#count"))
    print("status:", page.inner_text("#status-strip")[:300])
    page.screenshot(path=f"{OUT}/1_main.png")
    tiles = page.evaluate("document.querySelectorAll('.leaflet-tile-loaded').length")
    print("tiles loaded:", tiles)

    # goal switch
    page.click("#goal-switch button[data-goal=lease]")
    print("lease chips:", page.locator(".card").count())
    page.click("#goal-switch button[data-goal=temp]")
    page.click("#goal-switch button[data-goal=all]")

    # drawer on Showers place
    page.locator(".card", has_text="Showers").first.click()
    page.wait_for_selector("#drawer:not(.hidden) h2")
    time.sleep(0.5)
    page.screenshot(path=f"{OUT}/2_drawer.png", full_page=False)
    page.locator("#drawer").screenshot(path=f"{OUT}/2b_drawer_full.png")
    for i, frac in enumerate((0.45, 1.0)):
        page.evaluate(f"d => d.scrollTop = d.scrollHeight * {frac}", page.locator("#drawer").element_handle())
        time.sleep(0.3)
        page.locator("#drawer").screenshot(path=f"{OUT}/2c_drawer_lower{i}.png")
    page.evaluate("d => d.scrollTop = 0", page.locator("#drawer").element_handle())
    # rate overall 4
    page.locator('#drawer .attr[data-attr=overall] button[data-rate="4"]').click()
    time.sleep(1)
    print("stars after rating:", page.locator("#drawer h2 .stars").count())
    page.locator("#drawer .attr[data-attr=gym] input").fill("SEED test: gym looked small")
    page.locator("#drawer .attr[data-attr=gym] form button").click()
    time.sleep(1)
    print("gym log:", page.inner_text("#drawer .attr[data-attr=gym] .log")[:120])
    page.click("[data-close=drawer]")

    # verify mode
    page.click("#verify-btn")
    page.wait_for_selector("#verify-body h2")
    page.screenshot(path=f"{OUT}/3_verify.png")
    print("verify title:", page.inner_text("#verify-body h2"))
    page.click("[data-close=verify]")

    # offline reload: SW must be active and have cached tiles + shell + api
    page.reload()
    page.wait_for_selector(".card")
    time.sleep(3)
    print("sw controller:", page.evaluate("!!navigator.serviceWorker.controller"))
    ctx.set_offline(True)
    page.reload()
    try:
        page.wait_for_selector(".card", timeout=10000)
        time.sleep(2)
        print("OFFLINE cards:", page.locator(".card").count(),
              "tiles:", page.evaluate("document.querySelectorAll('.leaflet-tile-loaded').length"))
    except Exception as e:
        print("OFFLINE FAILED", e)
    page.screenshot(path=f"{OUT}/4_offline.png")
    ctx.set_offline(False)

    # mobile width
    m = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True)
    mp = m.new_page()
    mp.goto(URL)
    mp.wait_for_selector(".card")
    time.sleep(2)
    print("mobile scrollWidth:", mp.evaluate("document.documentElement.scrollWidth"))
    mp.screenshot(path=f"{OUT}/5_mobile.png")
    print("console errors:", errors[:10])
    browser.close()
