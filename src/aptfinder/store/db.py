"""Connection, init, and the only functions that write to the log tables."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .keys import content_hash
from .models import ObservationData
from .schema import ddl


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(ddl())
    conn.commit()


def _j(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def start_run(conn: sqlite3.Connection, component: str, source: str | None = None,
              params: dict[str, Any] | None = None, started_at: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, component, source, params) VALUES (?, ?, ?, ?)",
        (started_at or now_iso(), component, source, _j(params or {})),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str = "ok",
               counts: dict[str, Any] | None = None, est_cost: float | None = None,
               error: str | None = None, finished_at: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ?, counts = ?, est_cost = ?, error = ? WHERE id = ?",
        (finished_at or now_iso(), status, _j(counts or {}), est_cost, error, run_id),
    )
    conn.commit()


def append_observation(conn: sqlite3.Connection, run_id: int, source: str, source_id: str,
                       data: ObservationData | dict[str, Any], *, url: str | None = None,
                       price: float | None = None, price_basis: str | None = None,
                       query_checkin: str | None = None, query_checkout: str | None = None,
                       raw_path: str | None = None, observed_at: str | None = None,
                       commit: bool = True) -> int:
    if not isinstance(data, ObservationData):
        data = ObservationData.model_validate(data)
    d = data.model_dump(exclude_none=True)
    h = content_hash(d, price, price_basis, query_checkin, query_checkout)
    cur = conn.execute(
        """INSERT INTO observations (run_id, observed_at, source, source_id, url, content_hash, price,
               price_basis, query_checkin, query_checkout, data, raw_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, observed_at or now_iso(), source, source_id, url, h, price, price_basis,
         query_checkin, query_checkout, _j(d), raw_path),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def append_link(conn: sqlite3.Connection, author: str, source: str, source_id: str,
                place_key: str, unit_key: str, reason: str | None = None,
                commit: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO links (ts, author, source, source_id, place_key, unit_key, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), author, source, source_id, place_key, unit_key, reason),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def append_trip(conn: sqlite3.Connection, place_key: str, point_id: str, mode: str,
                minutes: float | None, distance_m: float | None, legs: list[dict[str, Any]],
                depart_assumption: str | None, provider: str, commit: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO trips (computed_at, place_key, point_id, mode, minutes, distance_m, legs, "
        "depart_assumption, provider) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), place_key, point_id, mode, minutes, distance_m, _j(legs), depart_assumption, provider),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def append_check(conn: sqlite3.Connection, author: str, queue: str, field: str, verdict: str, *,
                 observation_id: int | None = None, trip_id: int | None = None,
                 stored_value: Any = None, true_value: Any = None, note: str | None = None) -> int:
    if verdict not in ("correct", "wrong", "cannot_tell"):
        raise ValueError(f"bad verdict {verdict!r}")
    cur = conn.execute(
        "INSERT INTO checks (ts, author, queue, observation_id, trip_id, field, stored_value, verdict, "
        "true_value, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), author, queue, observation_id, trip_id, field, _j(stored_value), verdict,
         _j(true_value) if verdict == "wrong" else None, note),
    )
    conn.commit()
    return cur.lastrowid


def append_annotation(conn: sqlite3.Connection, author: str, entity_type: str, entity_key: str,
                      attribute: str, *, goal_id: str | None = None, rating: int | None = None,
                      comment: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO annotations (ts, author, entity_type, entity_key, attribute, goal_id, rating, comment) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), author, entity_type, entity_key, attribute, goal_id, rating, comment),
    )
    conn.commit()
    return cur.lastrowid
