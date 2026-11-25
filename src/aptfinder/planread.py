"""Baths read from an own-site plan's floor-plan drawing, for plan rows whose own site states no baths (Indigo).

  python -m aptfinder.planread [--dry-run] [--max-usd 0.5] [--workers 4] [--limit N]

Targets: active `direct` plan rows with known beds and unknown baths (after the user's corrections) that no other
source's same plan fills (`plans.inherit_baths`, applied first), and whose own drawing is in the photo store
(`extra.floorplan_image`, else an own-site `plan_drawings` label naming the plan; aptfinder.media). Each stored
drawing is sent once to Claude Haiku (headless `claude -p`, no tools, the image as a base64 block over
stream-json, under no file name, so a URL like 'plan-2d-...-2x2' cannot leak into the answer), which reports
bedrooms, full and half baths and what it saw. The answer is kept in the run's `counts.readings`, keyed by the
stored file's sha256 (runs are the log; a re-render that keeps the drawing keeps the reading), and applied at read
time by `plans.inherit_baths` only when it is a floor plan, confident, its bedrooms equal the row's, and full baths
are 1 to beds + 2 (baths = full + half / 2, Zillow's convention). The row's stored data is never changed, so a
user's correction of the baths (a `checks` row on the unknown value) keeps winning.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Callable

from . import settings
from .plans import inherit_baths, match_drawing, site_drawings
from .store import connect, finish_run, start_run

COMPONENT = "planread"
VERSION = 1  # prompt / schema version: readings of another version are asked again
SCHEMA = {
    "type": "object",
    "properties": {
        "is_floor_plan": {"type": "boolean", "description": "the image is a floor plan drawing of one home"},
        "bedrooms": {"type": ["integer", "null"], "description": "0 for a studio; null if it cannot be told"},
        "full_baths": {"type": ["integer", "null"], "description": "bathrooms with a shower or tub"},
        "half_baths": {"type": ["integer", "null"], "description": "bathrooms with only a toilet and sink"},
        "confident": {"type": "boolean", "description": "true only if every count is clearly readable"},
        "seen": {"type": "string", "description": "the labels and rooms you counted, e.g. '2 BD / 2 BA' or "
                                                  "'Bedroom 1, Bedroom 2, Bath 1 (tub), Bath 2 (shower)'"},
    },
    "required": ["is_floor_plan", "bedrooms", "full_baths", "half_baths", "confident", "seen"],
}
SYSTEM = ("You read apartment floor plan drawings. Count only what the drawing shows: rooms labelled bedroom (or "
          "BR, BD, Bed) and bathrooms (Bath, BA, a room with a toilet). A bathroom with a tub or shower is full; with "
          "only a toilet and sink it is half. A label like '2 BD / 2 BA' or '2x2' printed on the drawing counts. "
          "Dens, offices, lofts, closets and laundry are not bedrooms or baths. If the image is not a floor plan of "
          "one home, or you cannot read the counts, say so: set confident false. Answer in the requested JSON.")
PROMPT = "How many bedrooms, full bathrooms and half bathrooms does this floor plan show? Say what you saw."
SECRET_ENV = ("APIFY_TOKEN", "GOOGLE_MAPS_API_KEY")


class Blocker(RuntimeError):
    """A Claude call failed (not logged in, budget, no answer): stop and report (AGENTS.md rule 9)."""


def accept(ans: dict[str, Any] | None, beds: float | None) -> dict[str, Any] | None:
    """{baths, beds, seen} from a reading that is a confident floor plan agreeing with the row's beds, else None."""
    if not isinstance(ans, dict) or beds is None or ans.get("is_floor_plan") is not True or ans.get("confident") is not True:
        return None
    b, full, half = ans.get("bedrooms"), ans.get("full_baths"), ans.get("half_baths") or 0
    if not all(isinstance(x, int) and not isinstance(x, bool) for x in (b, full, half)) or b != beds:
        return None
    if not 1 <= full <= beds + 2 or not 0 <= half <= 2:
        return None
    return {"baths": full + half / 2, "beds": b, "seen": str(ans.get("seen") or "")[:500]}


# ------------------------------------------------------------------------------------------------ what to read
def _rows(conn) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT source, source_id, place_key, active, kind, term, furnished, beds, baths, sqft, floor_plan, data "
        "FROM listings WHERE place_key IN (SELECT place_key FROM listings WHERE source = 'direct' AND active = 1 "
        "AND baths IS NULL AND kind IS NOT 'site_info')")]


def _drawing(r: dict[str, Any], drawings: dict[str, str]) -> str | None:
    """The row's own drawing URL: its `floorplan_image`, else the own site's drawing whose label names the plan."""
    u = (json.loads(r["data"]).get("extra") or {}).get("floorplan_image")
    return u if isinstance(u, str) and u.startswith("http") else match_drawing(r.get("floor_plan"), drawings)


def _files(conn, urls: list[str]) -> dict[str, str]:
    """url -> the stored file's sha256 (aptfinder.media; the latest OK row of each URL)."""
    out: dict[str, str] = {}
    for i in range(0, len(urls), 500):
        part = urls[i:i + 500]
        for u, f in conn.execute(
                f"SELECT url, COALESCE(same_as, sha256) FROM media WHERE status = 200 AND sha256 IS NOT NULL "
                f"AND url IN ({','.join('?' * len(part))}) ORDER BY id", part):
            out[u] = f
    return out


def stored_readings(conn) -> dict[str, dict[str, Any]]:
    """sha256 -> the latest answer of this VERSION, from the `planread` runs' counts."""
    out: dict[str, dict[str, Any]] = {}
    for (c,) in conn.execute(f"SELECT counts FROM runs WHERE component = '{COMPONENT}' AND counts IS NOT NULL "
                             "ORDER BY id"):
        rd = (json.loads(c) or {}).get("readings") or {}
        out.update({k: v for k, v in rd.items() if isinstance(v, dict) and v.get("v") == VERSION})
    return out


def targets(conn) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rows still without baths after borrowing, with their drawing's stored file (None: not stored / none)."""
    rows = _rows(conn)
    if not rows:
        return [], {"rows": 0}
    lacking = [r for r in rows if r["source"] == "direct" and r["active"] and r["baths"] is None
               and r["kind"] != "site_info"]
    disagree = inherit_baths(rows)  # fills `baths` in place on the borrowable rows
    borrowed = [r for r in lacking if r.get("baths_from")]
    left = [r for r in lacking if r["baths"] is None]
    by_place: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["kind"] == "site_info":
            by_place.setdefault(r["place_key"], []).append({"source": r["source"], "data": json.loads(r["data"])})
    for r in left:
        r["drawing"] = _drawing(r, site_drawings(by_place.get(r["place_key"], [])))
    files = _files(conn, sorted({r["drawing"] for r in left if r.get("drawing")}))
    for r in left:
        r["file"] = files.get(r.get("drawing") or "")
    return left, {"lacking": len(lacking), "no_beds": sum(r["beds"] is None for r in lacking),
                  "borrowed": borrowed, "disagree": disagree,
                  "drawing_not_stored": sum(bool(r.get("drawing")) and not r["file"] for r in left),
                  "no_drawing": sum(not r.get("drawing") for r in left)}


def readings(conn) -> dict[tuple[str, str], dict[str, Any]]:
    """(source, source_id) -> accepted reading {baths, beds, seen, file}, for `plans.inherit_baths` at read time."""
    stored = stored_readings(conn)
    if not stored:
        return {}
    left, _ = targets(conn)
    out = {}
    for r in left:
        if r.get("file") and (a := accept(stored.get(r["file"]), r["beds"])):
            out[(r["source"], r["source_id"])] = {**a, "file": r["file"]}
    return out


# ------------------------------------------------------------------------------------------------ claude
def argv(model: str, max_usd: float) -> list[str]:
    return ["claude", "-p", "--system-prompt", SYSTEM, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--disable-slash-commands", "--model", model, "--no-session-persistence", "--input-format",
            "stream-json", "--output-format", "stream-json", "--verbose", "--json-schema", json.dumps(SCHEMA),
            "--max-budget-usd", f"{max_usd:.2f}"]


def ask(jpeg: bytes, model: str, max_usd: float) -> tuple[dict[str, Any], float]:
    msg = {"type": "user", "message": {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.b64encode(jpeg).decode()}},
        {"type": "text", "text": PROMPT}]}}
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(argv(model, max_usd), input=json.dumps(msg) + "\n", capture_output=True, text=True,
                              cwd=cwd, env=env, timeout=300)
    res = None
    for line in proc.stdout.splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "result":
            res = o
    if res is None:
        raise Blocker(f"claude gave no result (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:300]}")
    if res.get("is_error") or res.get("subtype") != "success" or not res.get("structured_output"):
        raise Blocker(f"claude call failed: {res.get('subtype')} {str(res.get('result'))[:200]}")
    return res["structured_output"], float(res.get("total_cost_usd") or 0)


def run(conn, *, dry_run: bool = False, max_usd: float = 0.5, model: str = "haiku", limit: int | None = None,
        ask_fn: Callable[[bytes, str, float], tuple[dict[str, Any], float]] | None = None) -> dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor

    from .media import media_path

    left, info = targets(conn)
    stored = stored_readings(conn)
    files = sorted({r["file"] for r in left if r.get("file") and r["beds"] is not None})
    todo = [f for f in files if f not in stored][:limit]
    plan = {"direct_rows_without_baths": info.get("lacking", 0), "without_beds": info.get("no_beds", 0),
            "borrowed": len(info.get("borrowed", [])), "disagreements": len(info.get("disagree", [])),
            "still_without": len(left), "with_stored_drawing": sum(bool(r.get("file")) for r in left),
            "drawing_not_stored": info.get("drawing_not_stored", 0), "no_drawing": info.get("no_drawing", 0),
            "drawings_read_before": len(files) - len([f for f in files if f not in stored]), "to_ask": len(todo)}
    examples = {"borrowed_rows": [{k: r[k] for k in ("source_id", "beds", "baths", "sqft", "floor_plan", "baths_from")}
                             for r in info.get("borrowed", [])],
                "disagree_rows": info.get("disagree", []),
                "to_read_rows": [{k: r.get(k) for k in ("source_id", "beds", "floor_plan", "drawing", "file")}
                            for r in left if r.get("file")]}
    if dry_run or not todo:
        return {**plan, **examples}
    run_id = start_run(conn, COMPONENT, "direct", {"model": model, "files": len(todo), "version": VERSION})
    got: dict[str, Any] = {}
    spent, err = 0.0, None
    one = ask_fn or ask
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {}
        for f in todo:
            if spent >= max_usd:
                break
            futs[pool.submit(one, media_path(f).read_bytes(), model, min(0.1, max_usd))] = f
        for fut, f in futs.items():
            try:
                ans, cost = fut.result()
            except (Blocker, subprocess.TimeoutExpired, OSError) as e:
                err = err or e
                continue
            spent += cost
            got[f] = {**ans, "v": VERSION, "model": model}
    accepted = {(r["source"], r["source_id"]): a for r in left if r.get("file") in got
                and (a := accept(got[r["file"]], r["beds"]))}
    counts = {**plan, "asked": len(got), "accepted_rows": len(accepted), "readings": got}
    finish_run(conn, run_id, "error" if err else "ok", counts, est_cost=round(spent, 4),
               error=str(err)[:500] if err else None)
    if err:
        raise Blocker(f"{err} (readings before it were stored: {len(got)})") from err
    return {"run_id": run_id, "claude_usd": round(spent, 4), **counts, **examples,
            "accepted": [{"source_id": k[1], **v} for k, v in accepted.items()]}


def main(argv_: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m aptfinder.planread", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="counts and examples only, no Claude call")
    ap.add_argument("--max-usd", type=float, default=0.5, help="cap on Claude spend for this command")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--limit", type=int, help="read at most N drawings")
    args = ap.parse_args(argv_)
    conn = connect(str(settings.DB_PATH))
    try:
        res = run(conn, dry_run=args.dry_run, max_usd=args.max_usd, model=args.model, limit=args.limit)
    except Blocker as e:
        raise SystemExit(f"BLOCKED: planread {e}")
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    sys.exit(main())
