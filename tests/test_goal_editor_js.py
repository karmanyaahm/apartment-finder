"""The goal editor's "+ add" menu (app.js ruleTemplates) against the backend's rule shapes: every rule type the
backend evaluates can be added from the menu, and every template is a rule the backend accepts. The fields and values
inside a rule's inputs come from GET /api/goals (numeric_fields, equals_fields, known_fields, aggs, amenities)."""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from aptfinder.filter.rules import EQUALS_FIELDS, RULE_TYPES, validate_rule

APP_JS = pathlib.Path(__file__).resolve().parents[1] / "src/aptfinder/web/static/app.js"


def _function(src: str, name: str) -> str:
    m = re.search(rf"^function {name}\(.*?^\}}$", src, re.M | re.S)
    assert m, f"no function {name} in app.js"
    return m.group(0)


@pytest.fixture(scope="module")
def templates() -> list[list]:
    if not shutil.which("node"):
        pytest.skip("node not installed")
    meta = {"default_target": "commute_targets:Meta HQ", "targets": ["commute_targets:Meta HQ"],
            "point_sets": {"commute_targets": {"label": "targets"}, "caltrain_stations": {"label": "Caltrain"}}}
    script = (f"const S = {{ meta: {json.dumps(meta)} }};\nconst $ = () => ({{ value: 'total' }});\n"
              f"{_function(APP_JS.read_text(), 'ruleTemplates')}\nconsole.log(JSON.stringify(ruleTemplates()));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_every_rule_type_has_an_add_entry(templates):
    assert set(RULE_TYPES) - {r["type"] for _, r in templates} == set()


def test_every_add_entry_is_a_valid_rule(templates):
    for label, rule in templates:
        err = validate_rule(rule, modes=["walk", "bike", "drive"], points={"commute_targets:Meta HQ"},
                            sets={"commute_targets", "caltrain_stations"})
        assert err is None, f"{label}: {err}"


def test_every_equals_field_is_reachable(templates):
    # an equals rule's field select lists equals_fields, so one equals template reaches every field; the restriction
    # preset is there because 'no limit' is what goals ask for
    assert any(r["type"] == "equals" for _, r in templates)
    assert {"field": "restriction", "type": "equals", "value": "none"} in [r for _, r in templates]
    assert "none" in EQUALS_FIELDS["restriction"]


def test_editor_inputs_read_the_options_from_api_goals():
    body = _function(APP_JS.read_text(), "ruleInputs")
    for key in ("numeric_fields", "equals_fields", "known_fields", "aggs", "amenities", "operators"):
        assert f"G.{key}" in body, f"ruleInputs does not read {key} from api/goals"
    # the 'count in net commute' box shows where /api/goals lists in_net as an optional key of the rule type
    assert 'G.types[r.type]?.optional?.includes("in_net")' in body


def test_net_total_needs_every_counted_time():
    # a total with a counted trip missing is needs check (v null), never a smaller sum; mean uses the known trips
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = APP_JS.read_text()
    p = {"goals": {"g": {"commute": [[10, False], [None, False], [20, True]]}}}
    out = {}
    for agg in ("total", "mean"):
        script = (f"const S = {{ goal: 'g' }};\nconst $ = () => ({{ value: '{agg}' }});\n"
                  f"{_function(src, 'aggregate')}\n{_function(src, 'netCommute')}\n"
                  f"console.log(JSON.stringify(netCommute({json.dumps(p)})));")
        out[agg] = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out["total"]["v"] is None and (out["total"]["known"], out["total"]["n"]) == (2, 3)
    assert out["mean"]["v"] == 15
