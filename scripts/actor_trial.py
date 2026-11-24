"""Trial an Apify actor with hard caps and summarize its output fields. Raw output goes to data/raw/trials/.

  uv run python scripts/actor_trial.py <actor> '<json input>' --max-items 20 --max-usd 0.10
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aptfinder import settings  # noqa: E402
from aptfinder.intake.apify import run_actor  # noqa: E402


def walk(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        yield (prefix + "[]", "list")
        for v in obj[:3]:
            yield from walk(v, prefix + "[]")
    else:
        yield (prefix, obj)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("actor")
    ap.add_argument("input")
    ap.add_argument("--max-items", type=int, default=20)
    ap.add_argument("--max-usd", type=float, default=0.10)
    a = ap.parse_args()
    res = run_actor(a.actor, json.loads(a.input), max_items=a.max_items, max_charge_usd=a.max_usd)
    out = settings.RAW_DIR / "trials"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    path = out / f"{a.actor.replace('/', '~')}_{stamp}.json"
    path.write_text(json.dumps({"actor": a.actor, "input": json.loads(a.input), "run_id": res.run_id,
                                "status": res.status, "cost_usd": res.cost_usd,
                                "charged_events": res.charged_events, "items": res.items}, indent=1))
    print(f"{a.actor}: status={res.status} items={len(res.items)} cost=${res.cost_usd} events={res.charged_events}")
    print(f"raw: {path}")
    filled: Counter = Counter()
    example: dict = {}
    for it in res.items:
        for k, v in walk(it):
            if v not in (None, "", [], {}):
                filled[k] += 1
                example.setdefault(k, v)
    for k in sorted(filled):
        print(f"  {filled[k]:3d}/{len(res.items)}  {k:50s} {str(example[k])[:70]!r}")


if __name__ == "__main__":
    main()
