"""Minimal Apify REST client: start an actor run with hard caps, wait, fetch items and the real cost.

Every actor run passes the credit guard first (`guard`: the account's real credit left, from /users/me/limits,
minus `spend.reserve_usd`), and its charge cap is cut to what the guard allows. Apify enforces
`maxTotalChargeUsd` only for pay-per-event actors, so `run_actor` also aborts a run whose `usageTotalUsd`
reaches the cap while it polls (platform-billed actors such as apify/playwright-scraper).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .. import settings

API = "https://api.apify.com/v2"


@dataclass
class ActorRun:
    actor: str
    run_id: str
    status: str
    items: list[dict[str, Any]]
    cost_usd: float | None
    charged_events: dict[str, int] | None
    console_url: str
    truncated: bool = False  # the source said there was more than it returned: every area of the run is partial
    truncated_areas: list[str] | None = None  # ... or only these areas (Places: a box whose query hit 60 results)
    usage_usd: float | None = None  # Apify's real charge (usageTotalUsd) as read when the run ended; summed by
    # composite fetches. Residential proxy traffic is billed later, so this can still be short (decision log)
    usage: dict[str, float] | None = None  # usageUsd breakdown (non-zero items), when Apify gives one
    log: str | None = None  # the run's log, when asked for (`with_log`): why requests failed
    # set by a fetch that judges its own completeness (Airbnb's split box search): True when the search ended on its
    # own everywhere, replacing the item-cap test (a split search returns more items than one search's cap)
    complete: bool | None = None


def _client() -> httpx.Client:
    token = settings.secret("APIFY_TOKEN")
    if not token:
        raise SystemExit("APIFY_TOKEN is not set in .env")
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=60)


DATACENTER = {"useApifyProxy": True}
# Apify's residential group, US exits: for sites that refuse the datacenter proxy (403/429 even in a browser).
# Billed by traffic, so pages through it skip images, fonts and video (RESIDENTIAL_HOOKS).
RESIDENTIAL = {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"], "apifyProxyCountry": "US"}
RESIDENTIAL_HOOKS = """[async ({ page }) => {
  await page.route(/\\.(png|jpe?g|gif|webp|avif|svg|ico|woff2?|ttf|otf|mp4|webm|m3u8)(\\?|$)/i, (r) => r.abort());
}]"""


def browser_proxy(residential: bool) -> dict[str, Any]:
    """Proxy fields of a web/playwright-scraper input: the datacenter default, or the residential group with
    heavy files blocked."""
    if not residential:
        return {"proxyConfiguration": DATACENTER}
    return {"proxyConfiguration": RESIDENTIAL, "preNavigationHooks": RESIDENTIAL_HOOKS}


def _get_json(c, path: str, tries: int = 6, **kw) -> Any:
    """GET an Apify API path as JSON, retrying a hiccup (5xx, an empty or non-JSON body) instead of losing the run:
    a Craigslist run (2026-11-25) crashed on one empty status response while the actor kept going."""
    for i in range(tries):
        try:
            r = c.get(path, **kw)
            if r.status_code < 500:
                return r.json()
        except (ValueError, httpx.TransportError):
            if i == tries - 1:
                raise
        time.sleep(min(30, 3 * 2 ** i))
    raise RuntimeError(f"Apify API kept failing for {path}")


class SpendRefused(RuntimeError):
    """The credit guard refused an Apify run (credit left minus reserve is too small, or unknown)."""


def credit() -> dict[str, float | None]:
    """The account's real Apify credit now: the monthly limit, the usage this cycle, what is left, and our
    `spend.reserve_usd`. `left` is None when the API does not say."""
    from .base import load_sources

    reserve = float(load_sources().get("spend", {}).get("reserve_usd", 0.0))
    with _client() as c:
        d = _get_json(c, "/users/me/limits").get("data", {})
    limit = (d.get("limits") or {}).get("maxMonthlyUsageUsd")
    used = (d.get("current") or {}).get("monthlyUsageUsd")
    left = None if limit is None or used is None else round(limit - used, 4)
    return {"limit": limit, "used": None if used is None else round(used, 4), "left": left, "reserve": reserve}


def guard(max_charge_usd: float) -> float:
    """The charge cap a run may have: `max_charge_usd`, cut to (credit left - reserve). Raises SpendRefused when
    that is under $0.02 or the credit is unknown, so (credit left - cap) >= reserve for every run started."""
    cr = credit()
    if cr["left"] is None:
        raise SpendRefused("Apify did not report the credit left (GET /v2/users/me/limits); no run started")
    room = cr["left"] - cr["reserve"]
    if room < 0.02:
        raise SpendRefused(f"Apify credit left ${cr['left']:.2f} minus reserve ${cr['reserve']:.2f} leaves "
                           f"${room:.2f}; no run started")
    return round(min(max_charge_usd, room), 4)


def _usage(run: dict[str, Any]) -> dict[str, float]:
    return {k: round(v, 4) for k, v in (run.get("usageUsd") or {}).items() if v and round(v, 4)}


def run_actor(actor: str, run_input: dict[str, Any], *, max_items: int, max_charge_usd: float,
              timeout_s: int = 900, poll_s: int = 10, memory_mb: int | None = None, clean: bool = True,
              with_log: bool = False) -> ActorRun:
    """`clean=False` keeps hidden fields and empty items: Apify's scrapers (web/playwright-scraper) push a failed
    request as an item with only `#error` and `#debug` (url, statusCode, errorMessages), which `clean` drops.
    `with_log` also fetches the run's log."""
    actor_path = actor.replace("/", "~")
    max_charge_usd = guard(max_charge_usd)
    with _client() as c:
        params: dict[str, Any] = {"maxItems": max_items, "maxTotalChargeUsd": max_charge_usd}
        if memory_mb:
            params["memory"] = memory_mb
        r = c.post(f"/acts/{actor_path}/runs", json=run_input, params=params)
        if r.status_code >= 400:
            err = r.json().get("error", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
            raise RuntimeError(f"Apify refused to start {actor}: HTTP {r.status_code} "
                               f"{err.get('type', '')}: {err.get('message', r.text[:300])}")
        run = r.json()["data"]
        deadline = time.time() + timeout_s
        while run["status"] in ("READY", "RUNNING") and time.time() < deadline:
            time.sleep(poll_s)
            run = _get_json(c, f"/actor-runs/{run['id']}")["data"]
            if (run.get("usageTotalUsd") or 0) >= max_charge_usd and run["status"] in ("READY", "RUNNING"):
                break  # over its cap: Apify does not stop a platform-billed actor itself
        if run["status"] in ("READY", "RUNNING"):
            c.post(f"/actor-runs/{run['id']}/abort")
            run = _get_json(c, f"/actor-runs/{run['id']}")["data"]
        items = _get_json(c, f"/datasets/{run['defaultDatasetId']}/items",
                          params={"clean": "true" if clean else "false", "format": "json"})
        log = c.get(f"/actor-runs/{run['id']}/log").text if with_log else None
    return ActorRun(
        actor=actor, run_id=run["id"], status=run["status"], items=items,
        cost_usd=run.get("usageTotalUsd"), charged_events=run.get("chargedEventCounts"),
        console_url=f"https://console.apify.com/actors/runs/{run['id']}", log=log,
        usage_usd=run.get("usageTotalUsd"), usage=_usage(run),
    )


def account_remaining_usd() -> float | None:
    """Monthly Apify credit left (limit minus usage this cycle); None if the API doesn't say."""
    return credit()["left"]


def run_record(run: ActorRun) -> dict[str, Any]:
    """One Apify run as stored in a run's counts (`apify_runs`): id, status, real charge, its breakdown."""
    return {"run": run.run_id, "status": run.status, "usd": run.usage_usd, **({"usage": run.usage} if run.usage else {})}


def run_log(run_id: str) -> str:
    """The plain-text log of an actor run (why its requests failed)."""
    with _client() as c:
        return c.get(f"/actor-runs/{run_id}/log").text


def same_url(a: str | None, b: str | None) -> bool:
    """URLs equal up to scheme, a leading 'www.', a trailing slash and a fragment (the crawler normalizes)."""
    def n(u: str) -> str:
        u = u.split("#", 1)[0].strip().lower()
        u = re.sub(r"^https?://(www\.)?", "", u)
        return u.rstrip("/")
    return bool(a and b) and n(a) == n(b)


def classify_failure(status: int | None, messages: list[str]) -> str:
    """Why a page did not come back, from its HTTP status and the scraper's error messages: `blocked` (HTTP 403 or
    429, or the crawler's 'Request blocked'), `timeout`, else `error`."""
    text = " ".join(m or "" for m in messages)
    if status in (403, 429) or re.search(r"request blocked|\b(403|429)\b|captcha|access denied", text, re.I):
        return "blocked"
    if re.search(r"time[d_\s]*out", text, re.I):  # "Timeout 120000ms", "timed out", net::ERR_TIMED_OUT
        return "timeout"
    return "error"


def failed_requests(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Requests Apify's web/playwright scrapers gave up on: with `clean=False` each is an item carrying `#error`
    and `#debug` (url, statusCode, errorMessages). -> [{url, status, messages}]."""
    out = []
    for it in items:
        if it.get("#error"):
            d = it.get("#debug") or {}
            out.append({"url": d.get("url") or d.get("loadedUrl"), "status": d.get("statusCode"),
                        "messages": [str(m).split("\n", 1)[0][:300] for m in d.get("errorMessages") or []]})
    return out


def log_errors(log: str | None, url: str) -> tuple[int | None, list[str]]:
    """The run log's warning and error lines about one URL -> (HTTP status if a line names one, messages)."""
    msgs, status = [], None
    for line in (log or "").splitlines():
        if not re.search(r"\b(WARN|ERROR)\b", line) or not any(same_url(u, url) for u in _URL.findall(line)):
            continue
        msgs.append(re.sub(r"\s+", " ", line.strip())[:300])
        if m := re.search(r"\b(?:status(?: code)?|HTTP)[:\s]+(\d{3})\b|received (\d{3})", line, re.I):
            status = int(m.group(1) or m.group(2))
    return status, msgs


_URL = re.compile(r"https?://[^\s\"'<>)]+")
