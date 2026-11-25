"""README screenshots from a running dashboard, ideally the redacted one (`aptf-web --redact --port 8767`).

  uv run --no-project --python 3.12 --with playwright python -I scripts/screenshots.py docs/screenshots \
      http://127.0.0.1:8767/ <showcase place_key> <units place_key>

Read-only: only clicks that open views. Check every image for private strings before committing it;
the README uses JPEGs (`convert x.png -quality 82 x.jpg`).
"""

import sys
import time

from playwright.sync_api import sync_playwright

OUT, URL, SHOWCASE, UNITS = sys.argv[1:5]


def goal(page, g):
    page.click(f'#goal-switch button[data-goal="{g}"]')
    time.sleep(1.5)


def drawer(page, pk, section=None):
    page.evaluate("(pk) => openPlace(pk)", pk)
    page.wait_for_selector("#drawer:not(.hidden) #drawer-body *", timeout=30000)
    time.sleep(3)  # photos and plan images
    if section:
        page.evaluate("""(t) => { const h = [...document.querySelectorAll('#drawer h3, #drawer h4, #drawer summary')]
                                    .find((e) => e.textContent.includes(t)); if (h) h.scrollIntoView(); }""", section)
        time.sleep(1.5)


with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_context(viewport={"width": 1600, "height": 1000}, device_scale_factor=1).new_page()
    page.goto(URL)
    page.wait_for_selector(".card", state="attached", timeout=120000)
    time.sleep(4)  # map tiles

    goal(page, "lease")
    page.uncheck("#todo")
    page.evaluate("map.setView([37.398, -122.085], 14)")  # Mountain View and San Antonio walk zones
    time.sleep(4)
    page.screenshot(path=f"{OUT}/1-overview.png")

    drawer(page, SHOWCASE)
    page.screenshot(path=f"{OUT}/2-building.png")
    drawer(page, UNITS)
    page.evaluate("""() => { const d = [...document.querySelectorAll('#drawer details')]
                              .find((e) => e.querySelector('summary').textContent.includes('Units')); d.open = true; }""")
    drawer(page, UNITS, "Floor plans")
    page.screenshot(path=f"{OUT}/3-units.png")
    drawer(page, UNITS, "Commute")
    page.screenshot(path=f"{OUT}/4-commute.png")
    page.keyboard.press("Escape")

    goal(page, "temp")
    page.uncheck('.st[value="fail"]')
    time.sleep(1.5)
    page.screenshot(path=f"{OUT}/5-temp-goal.png")

    page.click("#verify-btn")
    page.wait_for_selector("#verify:not(.hidden) #verify-body *", timeout=30000)
    time.sleep(3)
    page.screenshot(path=f"{OUT}/6-verify.png")
    browser.close()
