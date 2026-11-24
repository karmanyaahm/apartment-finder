from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from aptfinder import pipeline, verify
from aptfinder.intake import discover
from aptfinder.store import append_annotation, connect
from aptfinder.store.seed import seed_db
from aptfinder.web.app import create_app

STAGES = ["configure", "intake", "identity", "extract", "websites", "sites", "estimates", "anchors", "google",
          "verify", "filter"]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "seed.db"
    seed_db(path, with_trips=True)
    return path


def test_api_pipeline_shape(db):
    d = TestClient(create_app(db)).get("/api/pipeline").json()
    assert [s["id"] for s in d["stages"]] == STAGES
    for s in d["stages"]:
        assert s["state"] in ("done", "partial", "todo", "stale", "blocked")
        assert isinstance(s["commands"], list) and s["summary"]
    assert d["active"] == ["mountain_view"] and d["stale_days"] == 7
    # the seed's intake runs predate areas: every active area-source pair is still to do, with its command
    intake = next(s for s in d["stages"] if s["id"] == "intake")
    assert intake["state"] == "todo"
    assert "nix run .#intake -- run zillow --chunk mountain_view" in intake["commands"]
    row = next(r for r in d["intake"]["rows"] if r["area_id"] == "94043")
    assert row["active"] and set(row["cells"]) == set(d["intake"]["sources"]["zip"])
    assert any(not r["active"] for r in d["intake"]["rows"])  # other chunks are listed, dimmed
    est = next(s for s in d["stages"] if s["id"] == "estimates")
    assert est["state"] == "done"  # the seed computes estimates for every place
    assert next(s for s in d["stages"] if s["id"] == "verify")["counts"]["conflicts"] >= 1
    assert set(next(s for s in d["stages"] if s["id"] == "filter")["counts"]) == {"temp", "lease"}


def test_place_status_and_staleness(db):
    conn = connect(db)
    pk = conn.execute("SELECT place_key FROM places WHERE property_name LIKE '%Showers%'").fetchone()[0]
    fresh = pipeline.place_status(conn, pk, now=datetime(2026, 11, 25, tzinfo=timezone.utc))
    assert {s["source"] for s in fresh["sources"]} == {"zillow", "apartments_com"}
    assert all(s["state"] == "done" for s in fresh["sources"])
    assert fresh["website"]["state"] == "not_searched" and fresh["trips"] == {"estimate": fresh["trips"]["estimate"]}
    later = pipeline.place_status(conn, pk, now=datetime(2026, 12, 10, tzinfo=timezone.utc))
    assert all(s["state"] == "stale" for s in later["sources"])
    # the drawer carries it
    d = TestClient(create_app(db)).get(f"/api/places/{pk}").json()
    assert d["pipeline"]["website"]["label"] == "not searched"


def test_website_states_and_no_website_count(db, monkeypatch):
    conn = connect(db)
    keys = [r[0] for r in conn.execute("SELECT place_key FROM places ORDER BY place_key")]
    a, b, c, e = keys[0], keys[1], keys[2], keys[3]
    monkeypatch.setattr(discover, "_records", lambda config_dir=None: {
        "miss": [{"place_key": a, "model": "sonnet", "note": "nothing"}],
        "found": [{"id": "x", "place_key": b, "url": "https://x.example", "found_by": "claude"},
                  {"id": "y", "place_key": c, "url": "https://y.example", "found_by": "claude"}],
        "check": [{"id": "x", "ok": False, "reason": "page not fetched"},
                  {"id": "y", "ok": False, "reason": "page does not show the street address"}]})
    append_annotation(conn, "user", "place", e, "website", comment="none")
    ws = pipeline.website_status(conn)
    assert ws[a]["state"] == "no_site" and ws[a]["model"] == "sonnet"
    assert ws[b]["state"] == "blocked" and ws[c]["state"] == "rejected"
    assert ws[e]["state"] == "none_confirmed"
    assert pipeline.website_status(conn, b) == {b: ws[b]}
    d = pipeline.overview(conn)
    nv = next(s for s in d["stages"] if s["id"] == "verify")
    assert nv["counts"]["no_website"] == len(verify.no_website_queue(conn))
