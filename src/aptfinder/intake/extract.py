"""Read facts out of listing text with a small model (Claude Haiku through headless `claude -p`, no tools).

  aptf-intake extract craigslist [--limit 200] [--dry-run]

Some sources keep facts only in prose: a Craigslist post's address, building name and website sit in its
description; Furnished Finder rooms say "en-suite" or "shared bath" only in text. For each active listing of the
source whose latest observation has not been read yet, the title and description go to the model in batches; it
answers in a fixed JSON schema and may say null. Answers are checked before use:

- an address counts only if its street part (before the first comma, starting with a number) appears verbatim in
  the text, and a website only if it appears verbatim and survives `discover.clean_url`;
- only fields the parser left empty are filled; nothing the source stated is overwritten.

The result is appended as a new observation of the same listing (append-only; `extra.extracted` names the filled
fields, the model and the run), in a run with component `extract`, so the dashboard and identity use it like any
other row and a later scrape simply supersedes it. Answers are cached by text in `data/extract_cache.json`, so
re-reading after a new scrape of unchanged text costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from typing import Any

from .. import identity, settings
from ..store import append_observation, finish_run, start_run
from ..store.models import ObservationData
from .discover import SECRET_ENV, Blocker, clean_url

SOURCES = ("craigslist", "furnishedfinder")
CACHE = "extract_cache.json"
SYSTEM = ("You extract facts from rental listing text. Use only what the text states; never guess or infer from "
          "typical listings. Answer null for anything the text does not say. Answer in the requested JSON.")
_FACT = {"type": ["string", "null"]}
SCHEMA = {"type": "object", "required": ["results"], "properties": {"results": {"type": "array", "items": {
    "type": "object", "required": ["key"],
    "properties": {
        "key": {"type": "string"},
        "address": {**_FACT, "description": "street address of the rental as written, e.g. '231 Grant Ave, "
                                            "Palo Alto, CA 94306'; null if the text gives none"},
        "property_name": {**_FACT, "description": "name of the apartment community or building, if named"},
        "website": {**_FACT, "description": "a URL in the text for the property or its manager"},
        "furnished": {"type": ["boolean", "null"]},
        "private_bath": {"type": ["boolean", "null"], "description": "true only if the renter's bathroom is "
                                                                     "not shared; false if shared"},
        "min_stay_days": {"type": ["integer", "null"], "description": "minimum stay or lease in days"},
        "utilities_included": {"type": ["boolean", "null"]},
    }}}}}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def check_answer(ans: dict[str, Any], text: str) -> dict[str, Any]:
    """Only facts the text supports: verbatim street address and URL; typed values otherwise."""
    out: dict[str, Any] = {}
    t = _norm(text)
    addr = (ans.get("address") or "").strip()
    street = addr.split(",")[0].strip()
    if re.match(r"^\d+\s+\S", street) and f" {_norm(street)} " in f" {t} ":
        out["address"] = addr
    name = (ans.get("property_name") or "").strip()
    if name and _norm(name) and _norm(name) in t:
        out["property_name"] = name
    url = (ans.get("website") or "").strip()
    if url and url.lower().rstrip("/").split("://")[-1].removeprefix("www.") in text.lower():
        cleaned, _ = clean_url(url if url.startswith("http") else f"https://{url}", keep_query=True)
        if cleaned:
            out["website"] = cleaned
    for k in ("furnished", "private_bath", "utilities_included"):
        if isinstance(ans.get(k), bool):
            out[k] = ans[k]
    if isinstance(ans.get("min_stay_days"), int) and 0 < ans["min_stay_days"] <= 3650:
        out["min_stay_days"] = ans["min_stay_days"]
    return out


def argv(model: str, max_usd: float) -> list[str]:
    """Trimmed like `discover.claude_argv`, with no tools at all."""
    return ["claude", "-p", "--system-prompt", SYSTEM, "--tools", "", "--strict-mcp-config", "--setting-sources",
            "", "--disable-slash-commands", "--model", model, "--no-session-persistence", "--output-format", "json",
            "--json-schema", json.dumps(SCHEMA), "--max-budget-usd", f"{max_usd:.2f}"]


def ask(batch: list[dict[str, Any]], model: str, max_usd: float) -> tuple[list[dict[str, Any]], float]:
    prompt = "Extract the facts for each listing below. Return one result per key.\n\n" + "\n\n".join(
        f"### key {b['key']}\nTitle: {b['title']}\n{b['text']}" for b in batch)
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(argv(model, max_usd) + [prompt], capture_output=True, text=True, cwd=cwd, env=env,
                              timeout=600)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise Blocker(f"claude gave no JSON (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}")
    if out.get("is_error") or out.get("subtype") != "success":
        raise Blocker(f"claude call failed: {out.get('subtype')} {out.get('errors') or out.get('result')}")
    if not out.get("structured_output"):
        raise Blocker(f"no structured answer (not logged in?): {str(out.get('result'))[:200]}")
    return out["structured_output"].get("results") or [], float(out.get("total_cost_usd") or 0)


def _pending(conn, source: str, reread: bool = False) -> list[dict[str, Any]]:
    """Active listings not read yet; with `reread`, also those already read (their answers come from the cache,
    so applying a changed check costs nothing). A reread starts from the observation before the extraction."""
    rows = conn.execute(
        "SELECT lo.* FROM latest_obs lo JOIN listings l ON l.source = lo.source AND l.source_id = lo.source_id "
        "WHERE lo.source = ? AND l.active = 1 AND (? OR json_extract(lo.data, '$.extra.extracted') IS NULL) "
        "ORDER BY lo.source_id", (source, reread)).fetchall()
    if reread:  # the scraped observation under an extracted one
        rows = [conn.execute(
            "SELECT * FROM observations WHERE source = ? AND source_id = ? "
            "AND json_extract(data, '$.extra.extracted') IS NULL ORDER BY id DESC LIMIT 1",
            (source, r["source_id"])).fetchone() or r for r in rows]
    out = []
    for r in rows:
        d = json.loads(r["data"])
        text = (d.get("description") or "").strip()
        if len(text) >= 40:
            out.append({"row": dict(r), "data": d, "title": d.get("title") or "", "text": text[:2500]})
    return out


def extract(conn, source: str, *, limit: int, batch: int, model: str, max_usd: float,
            dry_run: bool = False, reread: bool = False) -> dict[str, Any]:
    if source not in SOURCES:
        raise SystemExit(f"extract supports {', '.join(SOURCES)}")
    todo = _pending(conn, source, reread)[:limit]
    cache_path = settings.DATA_DIR / CACHE
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    for t in todo:
        t["hash"] = hashlib.sha1(f"{model}\n{t['title']}\n{t['text']}".encode()).hexdigest()
    ask_list = [t for t in todo if t["hash"] not in cache]
    if dry_run:
        return {"listings": len(todo), "cached": len(todo) - len(ask_list), "to_ask": len(ask_list),
                "calls": -(-len(ask_list) // batch)}
    run = start_run(conn, "extract", source, {"model": model, "listings": len(todo)})
    spent, n_calls = 0.0, 0
    try:
        for i in range(0, len(ask_list), batch):
            if spent >= max_usd:
                break
            group = ask_list[i:i + batch]
            keyed = [{"key": str(j), "title": g["title"], "text": g["text"]} for j, g in enumerate(group)]
            answers, cost = ask(keyed, model, min(0.5, max_usd - spent + 0.05))
            spent, n_calls = spent + cost, n_calls + 1
            by_key = {a.get("key"): a for a in answers}
            for j, g in enumerate(group):
                cache[g["hash"]] = by_key.get(str(j)) or {}
            cache_path.write_text(json.dumps(cache))
    except (Blocker, subprocess.TimeoutExpired) as e:
        finish_run(conn, run, "error", {"calls": n_calls}, est_cost=round(spent, 4), error=str(e))
        raise Blocker(str(e)) from e
    filled: dict[str, int] = {}
    n_obs = 0
    for t in todo:
        if t["hash"] not in cache:
            continue  # budget ran out before this one
        facts = check_answer(cache[t["hash"]], f"{t['title']}\n{t['text']}")
        d = dict(t["data"])
        extra = dict(d.get("extra") or {})
        got = []
        for k, v in facts.items():
            if k in ("website", "min_stay_days", "utilities_included"):
                if extra.get(k) is None:
                    extra[k] = v
                    got.append(k)
            elif d.get(k) is None:
                if k == "address" and "," not in v and d.get("city"):
                    v = f"{v}, {str(d['city']).title()}"  # "850 Calderon Ave" -> "..., Mountain View"
                d[k] = v
                got.append(k)
        extra["extracted"] = {"fields": got, "model": model, "run_id": run}
        d["extra"] = extra
        r = t["row"]
        append_observation(conn, run, source, r["source_id"], ObservationData(**d), url=r["url"], price=r["price"],
                           price_basis=r["price_basis"], query_checkin=r["query_checkin"],
                           query_checkout=r["query_checkout"], raw_path=r["raw_path"], commit=False)
        n_obs += 1
        for k in got:
            filled[k] = filled.get(k, 0) + 1
    conn.commit()
    counts = {"listings": len(todo), "asked": len(ask_list), "calls": n_calls, "observations": n_obs,
              "filled": filled}
    finish_run(conn, run, "ok", counts, est_cost=round(spent, 4))
    identity.run(conn)
    return {"run_id": run, "claude_usd": round(spent, 4), **counts}
