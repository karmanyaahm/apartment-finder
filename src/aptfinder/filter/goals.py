"""The effective goal set: config/goals.toml is the seed, overlaid by goals edited in the dashboard.

An edit is an annotations row: entity_type 'goal', entity_key the goal id, attribute 'definition', comment the
JSON {label, rules, nice, people?}; a delete is the same row with {"deleted": true} (a tombstone). The latest row per id
wins. Seed goals keep their order; goals created in the dashboard follow in the order they were first saved."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from ..config import Goal, GoalsConfig, load_goals
from .rules import validate_rule

GOAL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
RESERVED_IDS = {"all"}  # the dashboard's goal switch uses it for "every goal"


def goal_edits(conn: sqlite3.Connection) -> dict[str, dict[str, Any] | None]:
    """goal id -> its latest edit ({label, rules, nice}), or None when the latest row deletes it."""
    out: dict[str, dict[str, Any] | None] = {}
    for key, comment in conn.execute("SELECT entity_key, comment FROM annotations WHERE entity_type = 'goal' "
                                     "AND attribute = 'definition' ORDER BY id"):
        d = json.loads(comment)
        out[key] = None if d.get("deleted") else d
    return out


def effective_goals(conn: sqlite3.Connection, cfg: GoalsConfig | None = None) -> GoalsConfig:
    """goals.toml (or `cfg`) with every dashboard edit applied; `verify` always comes from the file."""
    cfg = cfg or load_goals()
    goals = dict(cfg.goals)
    for gid, d in goal_edits(conn).items():
        if d is None:
            goals.pop(gid, None)
        else:
            goals[gid] = Goal(label=d["label"], rules=d["rules"], nice=d["nice"], people=d.get("people"))
    return GoalsConfig(goals=goals, verify=cfg.verify)


def validate_goal(body: Any, *, modes: list[str], points: set[str], sets: set[str]) -> dict[str, Any]:
    """{label, rules, nice, people?} from a dashboard request, or ValueError naming the first problem. `people`
    (people sharing the rent, an integer >= 1) is kept only when given; null or absent means the goal sets none."""
    if not isinstance(body, dict):
        raise ValueError("a goal must be an object")
    label = body.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("a goal needs a name (label)")
    out: dict[str, Any] = {"label": label.strip()}
    for part in ("rules", "nice"):
        rules = body.get(part, [])
        if not isinstance(rules, list):
            raise ValueError(f"{part} must be a list")
        for i, r in enumerate(rules):
            err = validate_rule(r, modes=modes, points=points, sets=sets)
            if err:
                raise ValueError(f"{part} #{i + 1}: {err}")
        out[part] = rules
    people = body.get("people")
    if people is not None:
        if not isinstance(people, int) or isinstance(people, bool) or people < 1:
            raise ValueError("people must be a whole number of at least 1")
        out["people"] = people
    return out


def check_id(gid: str) -> None:
    if not GOAL_ID.match(gid or "") or gid in RESERVED_IDS:
        raise ValueError("goal id: 1 to 40 of a-z, 0-9, _ and -, starting with a letter or digit, not 'all'")
