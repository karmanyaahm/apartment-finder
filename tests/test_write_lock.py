"""Short write transactions: another connection can write within about a second while a long job runs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from aptfinder.intake import base
from aptfinder.intake.base import Adapter, Parsed
from aptfinder.store import begin_write, connect, init_db
from aptfinder.store.models import ObservationData


def _probe(db, job_thread) -> float:
    """INSERT from a second connection (1 s busy timeout) until the job ends; the longest wait."""
    w = sqlite3.connect(db, timeout=1.0, isolation_level=None)
    worst = 0.0
    while job_thread.is_alive():
        t = time.monotonic()
        w.execute("INSERT INTO api_calls (ts, provider, mode, ok) VALUES ('t', 'probe', 'probe', 1)")
        worst = max(worst, time.monotonic() - t)
        time.sleep(0.02)
    w.close()
    return worst


def test_a_slow_reparse_commits_in_batches(tmp_path, monkeypatch):
    """100 items at 30 ms each, the parse between the inserts: one transaction would hold the lock 3 s. (Batch of
    1 here: a writer that holds the lock most of the time starves a waiter, whose busy handler polls.)"""
    db = tmp_path / "t.db"
    init_db(connect(db))
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps({"items": [{"id": str(i)} for i in range(100)]}))

    def slow_parse(item, cfg):
        time.sleep(0.03)
        return Parsed(item["id"], None, ObservationData(address=f"{item['id']} A St, Palo Alto"), price=3000.0,
                      price_basis="month")

    monkeypatch.setattr(base, "COMMIT_EVERY", 1)
    monkeypatch.setattr(base, "load_sources", lambda: {})
    adapter = Adapter("fake", build_input=lambda c, a: {}, parse=slow_parse)
    out = {}
    job = threading.Thread(target=lambda: out.update(base.reparse(connect(db), adapter, str(raw),
                                                                  rebuild_identity=False)))
    job.start()
    time.sleep(0.05)
    worst = _probe(db, job)
    job.join()
    assert out["observations"] == 100
    assert worst < 0.8, f"a second writer waited {worst:.2f} s"


def test_render_commits_each_site_before_the_next_claude_call(conn, tmp_path, monkeypatch):
    from aptfinder import settings
    from aptfinder.intake import render
    from aptfinder.intake.apify import ActorRun

    sites = {s: {"url": f"https://{s}.example/", "name": s} for s in ("a", "b", "c")}
    open_during_ask = []

    def ask(name, text, model, cap):
        open_during_ask.append(conn.in_transaction)  # a pending write here would hold the lock for the whole call
        return {}, 0.0

    monkeypatch.setattr(render, "load_sites", lambda: sites)
    monkeypatch.setattr(render, "run_actor", lambda *a, **k: ActorRun(
        "x", "r1", "SUCCEEDED", [{"site": s, "url": v["url"], "text": "Plan A $3,000 700 sqft"}
                                 for s, v in sites.items()], 0.0, {}, "u"))
    monkeypatch.setattr(render, "site_outcome", lambda *a: {"outcome": "read"})
    monkeypatch.setattr(render, "ask", ask)
    monkeypatch.setattr(render, "check_plans", lambda answer, text: [
        {"name": "A", "unit": None, "beds": 1, "baths": 1, "sqft": 700, "price": 3000, "available": None}])
    monkeypatch.setattr(render, "_site_address", lambda conn, sid, s: (f"1 {sid} St, Palo Alto", None))
    monkeypatch.setattr(render.identity, "run", lambda conn: {})
    monkeypatch.setattr(settings, "RAW_DIR", tmp_path / "raw")
    render.render(conn, sites=list(sites))
    assert open_during_ask == [False, False, False]
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE source = 'direct'").fetchone()[0] == 3


def test_begin_write_says_it_is_waiting_then_proceeds(tmp_path):
    db = tmp_path / "t.db"
    init_db(connect(db))
    holder = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    told = []
    threading.Timer(1.5, lambda: holder.execute("COMMIT")).start()
    c = connect(db)
    begin_write(c, on_wait=lambda: told.append(True))
    c.execute("INSERT INTO api_calls (ts, provider, mode, ok) VALUES ('t', 'x', 'y', 1)")
    c.commit()
    assert told == [True]
