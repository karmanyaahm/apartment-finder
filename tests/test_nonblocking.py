"""The served dashboard (create_app(background=True)) never makes a request wait for a whole-database recompute:
while one runs, the list answers from its last snapshot (flagged X-Updating) and writes go through."""

import threading
import time

from fastapi.testclient import TestClient

import aptfinder.web.app as webapp
from aptfinder.store.seed import seed_db


def test_requests_answer_while_a_recompute_runs(tmp_path, monkeypatch):
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    slow = threading.Event()
    real = webapp.evaluate_all

    def evaluate_all(conn, *a, **k):  # a recompute that takes 3 s once `slow` is set
        if slow.is_set():
            time.sleep(3)
        return real(conn, *a, **k)

    monkeypatch.setattr(webapp, "evaluate_all", evaluate_all)
    c = TestClient(webapp.create_app(db, background=True))
    first = c.get("/api/places")  # the first snapshot (built at start; this request waits for it only)
    assert first.status_code == 200 and "X-Updating" not in first.headers
    pk = first.json()[0]["place_key"]

    slow.set()
    assert c.post("/api/annotations", json={"entity_key": pk, "attribute": "overall", "rating": 4}).status_code == 200
    times, flagged = [], 0
    t_end = time.monotonic() + 2.0  # well inside the 3 s recompute the edit started
    while time.monotonic() < t_end:
        t = time.perf_counter()
        r = c.get("/api/places")
        t2 = time.perf_counter()
        w = c.post("/api/annotations", json={"entity_key": pk, "attribute": "note", "comment": "x"})
        times += [t2 - t, time.perf_counter() - t2]
        assert r.status_code == 200 and w.status_code == 200
        flagged += "X-Updating" in r.headers
    assert flagged, "the list never said it was updating"
    assert max(times) < 0.3, f"a request took {max(times):.2f} s during the recompute"

    slow.clear()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:  # the worker finishes and the next answer is fresh, with the new rating
        r = c.get("/api/places")
        if "X-Updating" not in r.headers and next(p for p in r.json() if p["place_key"] == pk)["rating"] == 4:
            break
        time.sleep(0.2)
    else:
        raise AssertionError("the rebuilt list never arrived")


def test_a_restart_answers_from_the_snapshots_on_disk(tmp_path, monkeypatch):
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    c = TestClient(webapp.create_app(db, background=True))
    before = c.get("/api/places").json()
    deadline = time.monotonic() + 10
    while not list((tmp_path / "cache" / "seed").glob("*.pkl")) and time.monotonic() < deadline:
        time.sleep(0.1)
    pk = before[0]["place_key"]
    assert c.post("/api/annotations", json={"entity_key": pk, "attribute": "overall", "rating": 5}).status_code == 200

    real = webapp.evaluate_all

    def evaluate_all(conn, *a, **k):  # the new process's rebuild is slow
        time.sleep(3)
        return real(conn, *a, **k)

    monkeypatch.setattr(webapp, "evaluate_all", evaluate_all)
    t = time.perf_counter()
    c2 = TestClient(webapp.create_app(db, background=True))  # a restart after the edit
    r = c2.get("/api/places")
    assert time.perf_counter() - t < 1.0, "the restarted app waited for its rebuild"
    assert r.json() == before and r.headers.get("X-Updating") == "places_body"  # last run's list, flagged
