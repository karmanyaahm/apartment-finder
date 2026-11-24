"""Minimal Apify REST client: start an actor run with hard caps, wait, fetch items and the real cost.

Spend is capped by Apify itself (maxTotalChargeUsd for pay-per-event actors, maxItems otherwise), so a
misbehaving actor cannot overspend even if our own accounting is wrong.
"""

from __future__ import annotations

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


def _client() -> httpx.Client:
    token = settings.secret("APIFY_TOKEN")
    if not token:
        raise SystemExit("APIFY_TOKEN is not set in .env")
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=60)


def run_actor(actor: str, run_input: dict[str, Any], *, max_items: int, max_charge_usd: float,
              timeout_s: int = 900, poll_s: int = 10, memory_mb: int | None = None) -> ActorRun:
    actor_path = actor.replace("/", "~")
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
            run = c.get(f"/actor-runs/{run['id']}").json()["data"]
        if run["status"] in ("READY", "RUNNING"):
            c.post(f"/actor-runs/{run['id']}/abort")
            run = c.get(f"/actor-runs/{run['id']}").json()["data"]
        items = c.get(f"/datasets/{run['defaultDatasetId']}/items",
                      params={"clean": "true", "format": "json"}).json()
    return ActorRun(
        actor=actor, run_id=run["id"], status=run["status"], items=items,
        cost_usd=run.get("usageTotalUsd"), charged_events=run.get("chargedEventCounts"),
        console_url=f"https://console.apify.com/actors/runs/{run['id']}",
    )
