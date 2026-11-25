"""Where each stage of the pipeline stands, computed on read from the logs and config (nothing stored): what has
run, what is partial, what has never run, what is stale (older than STALE_DAYS), what is blocked, and the command
that would run the rest. Feeds the dashboard's Pipeline panel (`/api/pipeline`) and the drawer's Pipeline section
(`place_status`). Intake covers every area of each source's kind; the website, own-site and Google stages count the
places inside some station's walk zone (`areas.in_walk_zone`)."""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from .areas import in_walk_zone
from .config import CHECK_LABEL, CHECK_RETRY, check_outcome, load_areas, load_points, load_sites
from .verify import is_named_building

STALE_DAYS = 7
from .intake.extract import HAS_TEXT_SQL, READ_SQL, TEXT_SOURCES  # listings whose entry Haiku reads
WEB_STATES = {  # website state -> label, in display order
    "priced": "own site, priced units",
    "scraped": "own site scraped, no prices",
    "listed": "in the site list, not scraped yet",
    "known": "URL known, not fetched yet",
    "blocked": "page not fetched yet (blocked or failed; retried)",
    "rejected": "found, page lacks the address",
    "no_site": "searched, no site found",
    "none_confirmed": "you said: no website",
    "not_searched": "not searched",
}
INTAKE = "nix run .#intake --"
COMMUTE = "nix run .#commute --"


def _days(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 86400


def _fresh(ts: str | None, now: datetime) -> str:
    """done, or stale when older than STALE_DAYS; todo when never."""
    if not ts:
        return "todo"
    d = _days(ts, now)
    return "stale" if d is not None and d > STALE_DAYS else "done"


def _roll(states: list[str]) -> str:
    """A stage's state from its parts' states."""
    s = set(states)
    if not s or s == {"todo"}:
        return "todo"
    if s == {"done"}:
        return "done"
    if s <= {"done", "stale"}:
        return "stale"
    if s <= {"done", "stale", "blocked"}:
        return "blocked"
    return "partial"


def _runs(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """(component, source) -> last ok run (id, started_at), number of runs and total est. cost."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in conn.execute(
            "SELECT component, COALESCE(source, '') AS source, COUNT(*) AS n, ROUND(COALESCE(SUM(est_cost), 0), 4) "
            "AS cost, MAX(CASE WHEN status = 'ok' THEN id END) AS last_ok, "
            "MAX(CASE WHEN status = 'ok' THEN started_at END) AS last_ok_at, MAX(id) AS last_id "
            "FROM runs GROUP BY component, COALESCE(source, '')"):
        out[(r["component"], r["source"])] = dict(r)
    for v in out.values():
        last = conn.execute("SELECT status, error FROM runs WHERE id = ?", (v["last_id"],)).fetchone()
        v["last_status"], v["last_error"] = last["status"], last["error"]
    return out


def _run_info(runs: dict, component: str, source: str = "") -> dict[str, Any]:
    r = runs.get((component, source))
    if not r:
        return {"last_run": None, "runs": 0, "cost": 0.0}
    return {"last_run": r["last_ok_at"], "last_run_id": r["last_ok"], "runs": r["n"], "cost": r["cost"],
            "last_status": r["last_status"], "last_error": r["last_error"]}


def website_status(conn: sqlite3.Connection, place_key: str | None = None) -> dict[str, dict[str, Any]]:
    """place_key -> {state, detail, url?}; state is a key of WEB_STATES. A place not in the result has not been
    searched. `place_key` limits the queries to one place (the drawer). A URL on an aggregator host (vrent.com,
    turbotenant.com, ...) is not a website here."""
    from .intake.discover import _records, is_aggregator

    w, args = ("", ()) if place_key is None else (" AND place_key = ?", (place_key,))
    mine = (lambda pk: True) if place_key is None else (lambda pk: pk == place_key)
    out: dict[str, dict[str, Any]] = {}
    rec = _records()
    checks = {c["id"]: c for c in rec.get("check", [])}  # the last check of a site wins
    for m in rec.get("miss", []):
        if mine(m.get("place_key")):
            out[m["place_key"]] = {"state": "no_site", "model": m.get("model") or "?",
                                   "detail": f"searched ({m.get('model') or '?'}): {m.get('note') or 'no site found'}"}
    for pk, url in conn.execute(
            "SELECT place_key, MIN(json_extract(data, '$.extra.website')) FROM listings WHERE place_key IS NOT NULL "
            "AND json_extract(data, '$.extra.website') IS NOT NULL" + w + " GROUP BY place_key", args):
        if is_aggregator(url):
            continue
        out[pk] = {"state": "known", "url": url, "detail": "named by a listing; promote-sites fetches it"}
    for s in load_sites().values():
        if s.get("place_key") and mine(s["place_key"]):
            out[s["place_key"]] = {"state": "listed", "url": s["url"], "detail": "in the site list, not scraped yet"}
    for r in conn.execute(
            "SELECT entity_key, comment FROM annotations WHERE entity_type = 'place' AND attribute = 'website'"
            + w.replace("place_key", "entity_key") + " ORDER BY id", args):
        if (r["comment"] or "").strip().lower() == "none":
            out[r["entity_key"]] = {"state": "none_confirmed", "detail": "you said it has no website"}
        else:
            out[r["entity_key"]] = {"state": "known", "url": r["comment"],
                                    "detail": "you gave the website; promote-sites fetches it"}
    # a found site's latest check: rejected (fetched, no address) or not fetched yet (blocked, timed out: retried)
    for f in rec.get("found", []):
        c = checks.get(f["id"])
        if not (mine(f.get("place_key")) and c) or is_aggregator(f["url"]) \
                or out.get(f["place_key"], {}).get("state") == "none_confirmed":
            continue
        oc = check_outcome(c)
        if oc == "rejected":
            out[f["place_key"]] = {"state": "rejected", "url": f["url"],
                                   "detail": f"{c.get('reason')} ({f.get('found_by')})"}
        elif oc in CHECK_RETRY:
            out[f["place_key"]] = {"state": "blocked", "url": f["url"], "outcome": oc,
                                   "detail": f"{check_label(c)} ({f.get('found_by')})"}
    for r in conn.execute(
            "SELECT place_key, MAX(last_seen) AS seen, SUM(price IS NOT NULL) AS priced, MIN(url) AS url "
            "FROM listings WHERE source = 'direct' AND place_key IS NOT NULL" + w + " GROUP BY place_key", args):
        if is_aggregator(r["url"]):
            continue
        out[r["place_key"]] = {"state": "priced" if r["priced"] else "scraped", "url": r["url"], "seen": r["seen"],
                               "detail": "own site gives units with prices" if r["priced"]
                               else "own site scraped: plans, photos or link, no prices"}
    return out


def render_outcomes(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """site id -> its latest render outcome (`intake.render.last_outcomes`) with a label ('blocked (HTTP 403) on
    11-24, will retry'), `retry` (a transient failure) and `stale` (done, but older than STALE_DAYS)."""
    from .intake.render import TRANSIENT, describe, last_outcomes

    out = {}
    for sid, o in last_outcomes(conn).items():
        out[sid] = {**o, "label": describe(o), "retry": o["outcome"] in TRANSIENT,
                    "stale": o["outcome"] not in TRANSIENT and _fresh(o["at"], datetime.now(timezone.utc)) == "stale"}
    return out


def site_checks() -> dict[str, dict[str, Any]]:
    """site id -> the latest address check of a discovered site (`promote-sites`), with its outcome and a label."""
    from .intake.discover import _records
    from .config import last_checks

    return {sid: {"outcome": check_outcome(c), "at": c.get("at"), "http_status": c.get("http_status"),
                  "label": check_label(c)} for sid, c in last_checks(_records()).items()}


def check_label(c: dict[str, Any]) -> str:
    """'blocked (HTTP 403) on 11-24, will retry'."""
    oc = check_outcome(c)
    return (CHECK_LABEL[oc] + (f" (HTTP {c['http_status']})" if c.get("http_status") else "")
            + (f" on {str(c['at'])[5:10]}" if c.get("at") else "") + (", will retry" if oc in CHECK_RETRY else ""))


RENDER_SKIP = {"avalon"}  # intake.render.SKIP_PLATFORMS: the page data gives every unit


def _current_points() -> set[str]:
    return {p.id for pts in load_points().values() for p in pts}


def overview(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    from . import verify
    from .filter import evaluate_all, routing_candidates
    from .intake import ADAPTERS
    from .intake.base import coverage, skipped
    from .trips import google_usage

    now = now or datetime.now(timezone.utc)
    areas = load_areas()
    runs = _runs(conn)
    places = [dict(r) for r in conn.execute("SELECT place_key, property_name, address, lat, lon FROM places")]
    in_zone = {p["place_key"] for p in places if in_walk_zone(p["lat"], p["lon"])}
    stages: list[dict[str, Any]] = []

    def stage(sid: str, label: str, state: str, summary: str, commands: list[str], **extra: Any) -> None:
        stages.append({"id": sid, "label": label, "state": state, "summary": summary, "commands": commands, **extra})

    # 1. configure
    from .filter.goals import effective_goals

    goals = effective_goals(conn).goals
    sites = load_sites()
    kinds = Counter(a.kind for a in areas)
    stage("configure", "Configure", "done",
          f"{kinds['zip']} ZIPs, {kinds['city']} cities, {kinds['box']} station boxes, {len(sites)} sites, "
          f"{len(goals)} goals", ["edit config/*.toml"],
          counts={"areas": dict(kinds), "sites": len(sites), "goals": list(goals)})

    # 2. intake: per area kind, area x source, like `aptf-intake coverage`
    cov = coverage(conn)
    srcs = {k: [s for s, ad in ADAPTERS.items() if ad.area_kind == k and not skipped(s)] for k in ("zip", "city", "box")}
    rows, todo_cmds = [], {}
    for kind, ss in srcs.items():
        for a in areas:
            if a.kind != kind:
                continue
            cells = {}
            for s in ss:
                c = cov.get((s, a.id))
                if not c:
                    st = "todo"
                elif c["state"] == "partial":
                    st = "partial"
                else:
                    st = _fresh(c["date"], now)
                cells[s] = {"state": st, "date": c["date"] if c else None, "run_id": c["run_id"] if c else None,
                            "items": c.get("items") if c else None, "empty": bool(c) and c["state"] == "empty"}
                if st != "done":
                    todo_cmds.setdefault(f"{INTAKE} run {s}" + (" --reload" if st == "stale" else ""), None)
            rows.append({"area": a.label, "area_id": a.id, "kind": kind, "cells": cells})
    all_cells = [c["state"] for r in rows for c in r["cells"].values()]
    n = Counter(all_cells)
    src_rows = []
    for s in ADAPTERS:
        info = _run_info(runs, "intake", s)
        src_rows.append({"source": s, "area_kind": ADAPTERS[s].area_kind, **info})
    stage("intake", "Intake (aggregators)", _roll(all_cells),
          f"{n['done']} done, {n['stale']} stale, {n['partial']} partial, {n['todo']} todo "
          f"of {len(all_cells)} area-source pairs", list(todo_cmds),
          counts=dict(n), cost=round(sum(r["cost"] for r in src_rows if r["source"] != "direct"), 2))

    # 3. identity (intake runs it inline too, so its state comes from the data, not its run rows): an unlinked
    # listing is left to do only if identity's own rule would place it now
    from .identity import decide

    n_list = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    unlinked = conn.execute(
        "SELECT source, source_id, address, unit, city, floor_plan, property_name, kind, lat, lon, beds, price, term, "
        "operator FROM listings WHERE place_key IS NULL").fetchall()
    left = sum(1 for r in unlinked if decide(r) is not None)
    stage("identity", "Identity (same building, same unit)", "partial" if left else "done",
          f"{n_list - len(unlinked)} of {n_list} listings placed; {len(unlinked) - left} cannot be placed "
          "(no usable address or coordinates)" + (f"; {left} placeable, not linked yet" if left else ""),
          ["nix run .#identity"] if left else [],
          counts={"listings": n_list, "unlinked": len(unlinked), "placeable": left}, **_run_info(runs, "identity"))

    # 3b. addresses geocoded and suspected duplicate buildings (identity/addresses.py)
    from .identity.addresses import load_geocodes, suspected_duplicates

    geo = load_geocodes()
    addrs = {r[0] for r in conn.execute("SELECT DISTINCT address FROM listings WHERE address IS NOT NULL AND address != ''")}
    done_ = sum(1 for a in addrs if a in geo)
    precise = sum(1 for a in addrs if a in geo and not geo[a].get("fallback"))
    dups = suspected_duplicates(conn)
    stage("duplicates", "Addresses geocoded and duplicate buildings",
          "partial" if (dups or done_ < len(addrs)) else "done",
          f"{done_} of {len(addrs)} address strings geocoded by Google ({precise} placed precisely; the rest keep "
          f"the parser's reading); {len(dups)} suspected duplicate buildings (same number, similar street, "
          "within 150 m); new addresses are geocoded after each intake run (`--no-followup` skips)",
          (["nix run .#identity -- geocode"] if done_ < len(addrs) else [])
          + (["nix run .#identity -- duplicates"] if dups else []),
          duplicates=dups[:20])

    # 4. text reading (Haiku reads each listing's whole entry); mirrors extract._pending (active, has text, read
    # under the current schema)
    ex_rows, ex_states = [], []
    for s in TEXT_SOURCES:
        r = conn.execute(
            f"SELECT COUNT(*) AS n, SUM(json_extract(data, '$.extra.extracted') IS NOT NULL AND {READ_SQL}) AS read "
            f"FROM listings WHERE source = ? AND active = 1 AND {HAS_TEXT_SQL}", (s,)).fetchone()
        n_, read = r["n"] or 0, r["read"] or 0
        st = "done" if n_ == read else ("todo" if not read else "partial")
        ex_states.append(st)
        ex_rows.append({"source": s, "listings": n_, "read": read, "state": st, **_run_info(runs, "extract", s)})
    ex_cmds = [f"{INTAKE} extract {r['source']}" for r in ex_rows if r["state"] != "done"]
    stage("extract", "Text reading (Haiku)", _roll(ex_states),
          "; ".join(f"{r['source']} {r['read']} read, {r['listings'] - r['read']} to read" for r in ex_rows),
          ([f"{INTAKE} extract all"] if len(ex_cmds) > 1 else []) + ex_cmds,
          rows=ex_rows, cost=round(sum(r["cost"] for r in ex_rows), 2))

    # 5. website finding, over named buildings (verify.is_named_building)
    ws = website_status(conn)
    named = [p for p in places if is_named_building(p["property_name"], p["address"])]
    for p in named:
        p["web"] = ws.get(p["place_key"], {}).get("state", "not_searched")
    all_c = Counter(p["web"] for p in named)
    act_named = [p for p in named if p["place_key"] in in_zone]
    act_c = Counter(p["web"] for p in act_named)
    no_site_models = Counter(ws[p["place_key"]].get("model") for p in named if p["web"] == "no_site")
    cmds = ([f"{INTAKE} promote-sites"] if act_c["known"] else []) + (
        [f"{INTAKE} run direct"] if act_c["listed"] else []) + (
        [f"{INTAKE} discover-sites --limit 40"] if act_c["not_searched"] else [])
    runnable = act_c["not_searched"] + act_c["known"] + act_c["listed"]
    found = act_c["priced"] + act_c["scraped"]
    web_state = ("done" if not act_named else "todo" if act_c["not_searched"] == len(act_named)
                 else "partial" if runnable else "blocked" if act_c["blocked"] else "done")
    stage("websites", "Website finding", web_state,
          f"walk zones: {found} of {len(act_named)} named buildings have their own site scraped; "
          f"{act_c['not_searched']} not searched, {act_c['blocked']} blocked",
          cmds + (["blocked sites need a browser-rendering actor or residential proxies (your decision)"]
                  if act_c["blocked"] else []),
          labels=WEB_STATES, by_state=dict(all_c), zone_by_state=dict(act_c), named=len(named),
          zone_named=len(act_named), no_site_models=dict(no_site_models),
          **_run_info(runs, "discover", "claude-websearch"))

    # 6. own-site scrapes, one row per site in the list
    seen = {r["site"]: dict(r) for r in conn.execute(
        "SELECT substr(source_id, 1, instr(source_id, ':') - 1) AS site, MAX(observed_at) AS last FROM observations "
        "WHERE source = 'direct' GROUP BY 1")}
    stats = {r["site"]: dict(r) for r in conn.execute(
        "SELECT substr(source_id, 1, instr(source_id, ':') - 1) AS site, COUNT(*) AS rows, SUM(active) AS active, "
        "SUM(price IS NOT NULL) AS priced, SUM(kind = 'site_info') AS info FROM listings WHERE source = 'direct' "
        "GROUP BY 1")}
    renders = render_outcomes(conn)
    checks = site_checks()
    site_rows = []
    for sid, s in sites.items():
        last = (seen.get(sid) or {}).get("last")
        st = stats.get(sid) or {}
        rd = renders.get(sid)
        if s.get("platform") in RENDER_SKIP:
            rd = {"outcome": "skipped", "label": "not needed (page data gives every unit)"}
        site_rows.append({"id": sid, "name": s.get("name") or sid, "url": s["url"], "platform": s.get("platform"),
                          "last": last, "state": _fresh(last, now),
                          "rows": st.get("rows") or 0, "priced": st.get("priced") or 0,
                          "info_only": bool(st.get("rows")) and st.get("info") == st.get("rows"),
                          "discovered": bool(s.get("verify_address")),
                          "render": rd, "check": checks.get(sid),
                          "command": f"{INTAKE} run direct --area {sid}" + (" --reload" if last else "")})
    order = {"todo": 0, "stale": 1, "done": 2}
    site_rows.sort(key=lambda r: (order[r["state"]], r["last"] or "", r["name"].lower()))
    sc = Counter(r["state"] for r in site_rows)
    cmds = []
    if sc["todo"]:
        cmds.append(f"{INTAKE} run direct")
    if sc["stale"]:
        cmds.append(f"{INTAKE} run direct --reload")
    # Prometheus sites are read from their availability JSON (`aptf-intake prometheus`), never rendered
    rend = [r for r in site_rows if r["platform"] != "prometheus"]
    if any((r["render"] or {}).get("retry") for r in rend):
        cmds.append(f"{INTAKE} render --retry-failed")
    if any(not r["render"] or r["render"].get("stale") for r in rend):
        cmds.append(f"{INTAKE} render")
    if any(not r["render"] or r["render"].get("retry") or r["render"].get("stale") for r in site_rows
           if r["platform"] == "prometheus"):
        cmds.append(f"{INTAKE} prometheus")
    rc = Counter((r["render"] or {"outcome": "never"})["outcome"] for r in site_rows)
    stage("sites", "Own-site scrapes", _roll([r["state"] for r in site_rows]),
          f"{sc['done']} fresh, {sc['stale']} stale, {sc['todo']} never scraped "
          f"of {len(site_rows)} sites; rendered: "
          + (", ".join(f"{n} {k}" for k, n in rc.most_common()) or "none"), cmds,
          counts=dict(sc), render_counts=dict(rc), **_run_info(runs, "intake", "direct"))

    # 7-9. commute: trips under the current point ids only (renamed points leave old rows behind)
    points = _current_points()
    provs: dict[str, set[str]] = defaultdict(set)
    for pk, pt, prov in conn.execute("SELECT DISTINCT place_key, point_id, provider FROM trips"):
        if pt in points:
            provs[pk].add(prov)
    # compute_all skips a stay place (`<building>~<operator>`) whose building is a place: it reads the building's
    located = {p["place_key"] for p in places if p["lat"] is not None}
    routable = {pk for pk in located if pk.split("~", 1)[0] == pk or pk.split("~", 1)[0] not in located}
    est = sum(1 for pk in routable if "estimate" in provs.get(pk, ()))
    stage("estimates", "Commute estimates (free)",
          "done" if est == len(routable) else "todo" if not est else "partial",
          f"{est} of {len(routable)} places have straight-line estimates (added after each intake run)",
          [] if est == len(routable) else [f"{COMMUTE} compute"], **_run_info(runs, "commute", "estimate"))

    pts = load_points()
    stations, targets = pts.get("caltrain_stations", []), pts.get("commute_targets", [])
    routed_legs: set[tuple] = set()
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'commute_legs'").fetchone():
        routed_legs = {tuple(r) for r in conn.execute(
            "SELECT o_lat, o_lon, d_lat, d_lon FROM commute_legs WHERE mode = 'bike' AND provider != 'estimate' "
            "AND found = 1")}
    anchors = [{"station": s.name, "target": t.name,
                "routed": (round(s.lat, 4), round(s.lon, 4), round(t.lat, 4), round(t.lon, 4)) in routed_legs}
               for s in stations for t in targets]
    na = sum(a["routed"] for a in anchors)
    stage("anchors", "Station -> target bike legs (Google, once)",
          "done" if anchors and na == len(anchors) else "todo" if not na else "partial",
          f"{na} of {len(anchors)} station x target legs routed",
          [] if na == len(anchors) else [f"{COMMUTE} --provider google anchors --dry-run",
                                          f"{COMMUTE} --provider google anchors"], anchors=anchors)

    res = evaluate_all(conn)
    cands: set[str] = set()
    for pk in routing_candidates(conn, res=res):  # routed as its building, like `compute --candidates`
        base = pk.split("~", 1)[0]
        cands.add(base if base in located else pk)
    act_cands = sorted(pk for pk in cands if pk in in_zone and pk in located)
    g_done = sum(1 for pk in act_cands if "google" in provs.get(pk, ()))
    usage = google_usage(conn)
    from . import settings

    key = bool(settings.secret("GOOGLE_MAPS_API_KEY"))
    capped = usage["calls"] >= usage["cap"]
    g_state = ("done" if g_done == len(act_cands) else "blocked" if capped or not key
               else "todo" if not g_done else "partial")
    stage("google", "Google routing (candidates in station walk zones)", g_state,
          f"{g_done} of {len(act_cands)} candidate places routed; {sum('google' in v for v in provs.values())} places "
          f"routed in all; {usage['calls']}/{usage['cap']} calls this month"
          + ("; GOOGLE_MAPS_API_KEY not set" if not key else "") + ("; monthly cap reached" if capped else ""),
          [] if g_done == len(act_cands) else [f"{COMMUTE} --provider google compute --candidates --dry-run",
                                               f"{COMMUTE} --provider google compute --candidates"],
          usage=usage, candidates=len(act_cands), routed=g_done, cost=usage["est_cost_usd"],
          **{k: v for k, v in _run_info(runs, "commute", "google").items() if k != "cost"})

    # 10. verify
    n_checks = conn.execute("SELECT COUNT(*) FROM checks").fetchone()[0]
    open_conflicts = verify.all_conflicts(conn)
    conflicts = len(open_conflicts)
    pending = sum(c["pending_render"] for c in open_conflicts)  # an own-site render will likely settle these
    # the own-site queue: named buildings in a walk zone whose own site gave no usable offer
    no_web = len(verify.own_site_queue(conn))  # named buildings whose own site gave nothing usable
    n_dup = len(verify.duplicate_queue(conn))  # pairs of places that are probably one building
    stage("verify", "Verify (hand checks)",
          "done" if not conflicts and not no_web and not n_dup else "todo" if not n_checks else "partial",
          f"{n_checks} checks made; {conflicts} open conflicts ({pending} waiting for an own-site render); "
          f"{n_dup} possible duplicate buildings; {no_web} named buildings whose own site gave no usable offer",
          ["Verify button in this dashboard"] if conflicts or no_web or n_dup else [],
          counts={"checks": n_checks, "conflicts": conflicts, "conflicts_pending_render": pending,
                  "duplicates": n_dup, "own_site": no_web})

    # 11. filter (computed on every read)
    per_goal = {g: dict(Counter(r["status"] for r in pres.values())) for g, pres in res["places"].items()}
    stage("filter", "Filter (goals, live)", "done",
          "; ".join(f"{g}: {c.get('pass', 0)} pass, {c.get('needs_check', 0)} check, {c.get('fail', 0)} fail"
                    for g, c in per_goal.items()),
          [f"nix run .#filter -- --goal {g} --status pass" for g in per_goal], counts=per_goal)

    # photos: our own copies of listing photos and floor-plan drawings (aptfinder.media)
    from .media import status as media_status

    ms = media_status(conn)
    full = f"; media store full ({ms['max_bytes'] / 1e9:.1f} GB)" if ms["full"] else ""
    stage("photos", "Photos", "blocked" if ms["full"] else "todo" if not ms["downloaded"] else
          "done" if not ms["to_fetch"] else "partial",
          f"{ms['downloaded']} downloaded ({ms['files']} files after dedupe), {ms['failed']} failed, "
          f"{ms['to_fetch']} to fetch; {ms['disk_bytes'] / 1e9:.2f} of {ms['max_bytes'] / 1e9:.1f} GB{full}",
          ["nix run .#media -- fetch", "nix run .#media -- fetch --retry-failed"],
          counts={k: ms[k] for k in ("downloaded", "files", "failed", "to_fetch", "disk_bytes", "full")})

    stuck = [dict(r) for r in conn.execute(
        "SELECT id, component, source, started_at FROM runs WHERE status = 'running' ORDER BY id")
        if (_days(r["started_at"], now) or 0) > 1 / 24]  # running for over an hour
    return {"now": now.isoformat(timespec="seconds"), "stale_days": STALE_DAYS,
            "stages": stages, "intake": {"sources": srcs, "rows": rows, "runs": src_rows}, "sites": site_rows,
            "stuck_runs": stuck}


def place_status(conn: sqlite3.Connection, place_key: str, now: datetime | None = None,
                 web: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """One place: each source's last sighting (stale after STALE_DAYS), its own website, whether its text was
    read, and whether its commute times are estimates or Google routes. A stay place (`<building>~<operator>`)
    takes the website and trips of its building."""
    now = now or datetime.now(timezone.utc)
    base = place_key.split("~", 1)[0]
    sources = []
    for r in conn.execute(
            "SELECT source, MAX(last_seen) AS seen, SUM(active) AS active, COUNT(*) AS n, "
            "SUM(json_extract(data, '$.extra.extracted') IS NOT NULL) AS read FROM listings WHERE place_key = ? "
            "GROUP BY source ORDER BY source", (place_key,)):
        sources.append({"source": r["source"], "seen": r["seen"], "listings": r["n"], "active": r["active"] or 0,
                        "state": _fresh(r["seen"], now),
                        "text_read": (r["read"] or 0) if r["source"] in TEXT_SOURCES else None})
    # `web`: website_status for every place, computed once by the caller (the dashboard caches it)
    ws = (web if web is not None else website_status(conn, base)).get(
        base, {"state": "not_searched", "detail": "no website looked for yet"})
    points = _current_points()
    trips = Counter()
    for pk, pt, prov in conn.execute(
            "SELECT DISTINCT place_key, point_id, provider FROM trips WHERE place_key IN (?, ?)", (place_key, base)):
        if pt in points:
            trips[prov] += 1
    # the building's own site(s): the latest render and address check of each, failure or not
    sids = {sid for sid, s in load_sites().items() if s.get("place_key") == base}
    sids |= {r[0] for r in conn.execute(
        "SELECT DISTINCT substr(source_id, 1, instr(source_id, ':') - 1) FROM listings WHERE source = 'direct' "
        "AND place_key = ?", (base,))}
    renders, checks = render_outcomes(conn), site_checks()
    sites = [{"id": sid, "render": renders.get(sid), "check": checks.get(sid)} for sid in sorted(sids - {""})]
    return {"sources": sources, "website": {**ws, "label": WEB_STATES[ws["state"]]}, "trips": dict(trips),
            "sites": sites, "stale_days": STALE_DAYS}
