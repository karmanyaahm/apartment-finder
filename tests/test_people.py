"""A goal's `people` (sharing the rent) and the derived price_per_person = price_month / people."""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from aptfinder.filter.goals import validate_goal
from aptfinder.filter.rules import CHECK, FAIL, PASS, Ctx, evaluate, fact, validate_rule
from aptfinder.store import connect
from aptfinder.store.seed import seed_db
from aptfinder.verify import goal_relevance, rule_fields
from aptfinder.web.app import create_app

RULE = {"type": "max", "field": "price_per_person", "value": 2500}
REFS = {"modes": ["walk"], "points": set(), "sets": set()}


def test_four_bedrooms_for_three_people():
    row = {"price_month": 7500, "price": 7500, "beds": 4, "price_per_bed": 1875}
    s, why = evaluate(RULE, row, Ctx(people=3))
    assert s == PASS and "2,500 = 7,500 / 3" in why
    assert evaluate(RULE, {**row, "price_month": 7501}, Ctx(people=3))[0] == FAIL
    assert evaluate({**RULE, "strict": True}, row, Ctx(people=3))[0] == FAIL
    assert evaluate(RULE, {**row, "price_month": None}, Ctx(people=3))[0] == CHECK
    assert evaluate(RULE, row, Ctx(people=3, untrusted={"price"}))[0] == CHECK
    assert fact(RULE, {**row, "price_month": 9000}, 3) == {"f": "price_per_person", "v": 3000, "n": 3, "lim": 2500}


def test_no_people_is_needs_check_naming_it():
    s, why = evaluate(RULE, {"price_month": 1000}, Ctx())
    assert s == CHECK and "goal has no 'people'" in why
    assert evaluate({"type": "known", "field": "price_per_person"}, {"price_month": 1000}, Ctx())[0] == CHECK
    assert validate_rule(RULE, **REFS) is None  # a valid rule: the goal decides


@pytest.mark.parametrize("people", [0, -1, 2.5, True, "3"])
def test_people_validation(people):
    with pytest.raises(ValueError, match="people"):
        validate_goal({"label": "x", "rules": [], "nice": [], "people": people}, **REFS)


def test_people_absent_stays_absent():
    assert "people" not in validate_goal({"label": "x", "rules": [], "people": None}, **REFS)
    assert validate_goal({"label": "x", "people": 3}, **REFS)["people"] == 3


def test_people_round_trip_and_equivalence(tmp_path):
    db = tmp_path / "seed.db"
    seed_db(db, with_trips=True)
    c = TestClient(create_app(db))
    g = next(g for g in c.get("/api/goals").json()["goals"] if g["id"] == "lease")
    assert g["people"] is None
    old = {**g, "rules": [*g["rules"], {"type": "max", "field": "price_month", "value": 7500}]}
    new = {**g, "people": 3, "rules": [*g["rules"], {"type": "max", "field": "price_per_person", "value": 2500}]}
    st = lambda body: {k: v["status"] for k, v in c.post("/api/goals/preview", json=body).json()["places"].items()}
    assert st(old) == st(new)
    assert c.put("/api/goals/lease", json=new).status_code == 200
    g2 = next(g for g in c.get("/api/goals").json()["goals"] if g["id"] == "lease")
    assert g2["people"] == 3 and g2["rules"][-1]["field"] == "price_per_person"
    stored = json.loads(connect(db).execute("SELECT comment FROM annotations WHERE entity_type = 'goal'").fetchone()[0])
    assert stored["people"] == 3
    # without people the same rule is needs_check, never a fail
    nop = {**new, "people": None}
    assert set(st(nop).values()) <= {"needs_check", "fail"}
    assert all(v != "pass" for v in st(nop).values())


def test_verify_relevance_reads_price_per_person():
    assert rule_fields(RULE) >= {"price_month", "price"}
    item = {"fields": ["price_month"], "listings": [{"source": "a", "source_id": "1"}]}
    rules = [RULE]
    row = {"source": "a", "source_id": "1", "price_month": 7000, "price": 7000, "price_basis": "month"}
    other = {"source": "b", "source_id": "2", "price_month": 8000, "price": 8000, "price_basis": "month"}
    item["listings"].append({"source": "b", "source_id": "2"})
    lres = {("a", "1"): {"reasons": [(PASS, "", "max")]}, ("b", "2"): {"reasons": [(FAIL, "", "max")]}}
    rows = {("a", "1"): row, ("b", "2"): other}
    # $7,000 vs $8,000 for 3 people straddles $2,500 each: the conflict matters
    assert goal_relevance(item, rules, lres, rows, people=3) == "shown"


APP_JS = pathlib.Path(__file__).resolve().parents[1] / "src/aptfinder/web/static/app.js"


def test_editor_preset_only_with_people():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    import re
    fn = re.search(r"^function ruleTemplates\(.*?^\}$", APP_JS.read_text(), re.M | re.S).group(0)
    meta = {"default_target": "t:x", "targets": ["t:x"], "point_sets": {"s": {}}}
    out = {}
    for people in (None, 3):
        script = (f"const S = {{ meta: {json.dumps(meta)}, draft: {json.dumps({'people': people})} }};\n"
                  f"const $ = () => ({{ value: 'total' }});\n{fn}\nconsole.log(JSON.stringify(ruleTemplates()));")
        out[people] = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True,
                                                check=True).stdout)
    has = lambda ts: any(r.get("field") == "price_per_person" for _, r in ts)
    assert not has(out[None]) and has(out[3])
