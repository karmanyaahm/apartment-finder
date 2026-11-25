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
    real = webapp.goal_inputs

    def goal_inputs(conn, *a, **k):  # a recompute that takes 3 s once `slow` is set
        if slow.is_set():
            time.sleep(3)
        return real(conn, *a, **k)

    monkeypatch.setattr(webapp, "goal_inputs", goal_inputs)
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

    real = webapp.goal_inputs

    def goal_inputs(conn, *a, **k):  # the new process's rebuild is slow
        time.sleep(3)
        return real(conn, *a, **k)

    monkeypatch.setattr(webapp, "goal_inputs", goal_inputs)
    t = time.perf_counter()
    c2 = TestClient(webapp.create_app(db, background=True))  # a restart after the edit
    r = c2.get("/api/places")
    assert time.perf_counter() - t < 1.0, "the restarted app waited for its rebuild"
    assert r.json() == before and r.headers.get("X-Updating") == "places_body"  # last run's list, flagged


def _counting(monkeypatch, *names):
    """Wrap webapp's functions `names` with a call counter: {name: calls}."""
    calls = dict.fromkeys(names, 0)
    for n in names:
        real = getattr(webapp, n)

        def wrapped(*a, _n=n, _real=real, **k):
            calls[_n] += 1
            return _real(*a, **k)

        monkeypatch.setattr(webapp, n, wrapped)
    return calls


def _lease(places):
    return {p["place_key"]: p["goals"]["lease"]["status"] for p in places if "lease" in p["goals"]}


def test_a_goal_save_rejudges_the_goals_without_a_full_rebuild(tmp_path, monkeypatch):
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    monkeypatch.setattr(webapp, "WATCH_EVERY_S", 3600)  # only the save itself rebuilds anything
    calls = _counting(monkeypatch, "best_trips", "goal_inputs")
    c = TestClient(webapp.create_app(db, background=True))
    before = _lease(c.get("/api/places").json())
    assert set(before.values()) != {"pass"}
    c.get("/api/pipeline")  # the start's queue done
    built = dict(calls)
    r = c.put("/api/goals/lease", json={"label": "Lease, anything", "rules": [], "nice": []})
    assert r.status_code == 200
    after = c.get("/api/places")  # the save answered once the list was rebuilt: fresh at once
    assert "X-Updating" not in after.headers
    assert set(_lease(after.json()).values()) == {"pass"}
    assert calls == built, "a goal save rebuilt the trips or goal inputs"
    # the draft preview reads the same cached inputs: nothing rebuilt either
    assert c.post("/api/goals/preview", json={"label": "x", "rules": []}).status_code == 200
    assert calls == built


def test_a_goal_save_uses_out_of_date_inputs_as_they_are(tmp_path, monkeypatch):
    """New scraped rows made the inputs out of date (an intake run on the box): the save still re-judges only the
    goals over the last trips and inputs, and the list stays flagged for the worker's usual rebuild."""
    from aptfinder.store import connect, start_run

    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    monkeypatch.setattr(webapp, "WATCH_EVERY_S", 3600)
    monkeypatch.setattr(webapp, "SCRAPE_REFRESH_S", 0)
    calls = _counting(monkeypatch, "best_trips", "goal_inputs")
    c = TestClient(webapp.create_app(db, background=True))
    c.get("/api/places")
    c.get("/api/pipeline")  # waits for the start's queue (Pipeline is queued last) before the scrape below
    built = dict(calls)
    w = connect(db)
    start_run(w, "intake", "zillow")
    w.commit()
    assert c.put("/api/goals/lease", json={"label": "Lease, anything", "rules": []}).status_code == 200
    assert calls == built, "a goal save rebuilt the trips or goal inputs"
    after = c.get("/api/places")
    assert "X-Updating" in after.headers  # still out of date on the scrape: the worker rebuilds it as usual
    assert set(_lease(after.json()).values()) == {"pass"}


def test_a_rebuild_started_before_a_goal_save_does_not_put_the_old_goals_back(tmp_path, monkeypatch):
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    monkeypatch.setattr(webapp, "WATCH_EVERY_S", 3600)
    slow = threading.Event()
    real_gzip = webapp.gzip.compress

    class SlowGzip:  # the worker's list rebuild stalls after judging the old goals
        @staticmethod
        def compress(data, *a, **k):
            if slow.is_set() and threading.current_thread().name.startswith("aptf-recompute"):
                time.sleep(2)
            return real_gzip(data, *a, **k)

    monkeypatch.setattr(webapp, "gzip", SlowGzip)
    c = TestClient(webapp.create_app(db, background=True))
    pk = c.get("/api/places").json()[0]["place_key"]
    slow.set()
    assert c.post("/api/annotations", json={"entity_key": pk, "attribute": "overall", "rating": 4}).status_code == 200
    c.get("/api/places")  # starts the worker's rebuild (old goals)
    time.sleep(0.5)
    assert c.put("/api/goals/lease", json={"label": "Lease, anything", "rules": []}).status_code == 200
    assert set(_lease(c.get("/api/places").json()).values()) == {"pass"}
    time.sleep(2.5)  # the old rebuild has finished by now
    slow.clear()
    assert set(_lease(c.get("/api/places").json()).values()) == {"pass"}
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:  # then the list catches up with the rating too
        r = c.get("/api/places")
        if "X-Updating" not in r.headers:
            break
        time.sleep(0.2)
    else:
        raise AssertionError("the rebuilt list never arrived")
    places = r.json()
    assert set(_lease(places).values()) == {"pass"}
    assert next(p for p in places if p["place_key"] == pk)["rating"] == 4
