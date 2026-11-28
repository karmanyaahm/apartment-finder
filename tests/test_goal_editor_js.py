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
    m = re.search(rf"^(?:async )?function {name}\(.*?^\}}$", src, re.M | re.S)
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


def _sort_order(goal: dict, key: str = "ppb") -> list[str]:
    """app.js sorted() over the three cards the user saw (lease goal, people 3): their labels in order."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    places = [
        {"label": "3BED & 2BATH", "goals": {"lease": {"offer": {"price_month": 3664, "price_per_bed": 1221, "beds": 3}}}},
        {"label": "227 Church St", "goals": {"lease": {"offer": {"price_month": 5200, "price_per_bed": 1300, "beds": 4}}}},
        {"label": "Palmetto", "goals": {"lease": {"offer": {"price_month": 3995, "price_per_bed": 1332, "beds": 3}}}},
    ]
    src = APP_JS.read_text()
    script = (f"const S = {{ goal: 'lease', draft: {json.dumps(goal)}, goalsData: {{ goals: [] }} }};\n"
              f"const $ = () => ({{ value: {json.dumps(key)} }});\nconst RANK = {{}};\n"
              "const goalOffer = (p) => (S.goal !== 'all' && p.goals[S.goal]?.offer) || null;\n"
              "const savedGoal = () => null;\n"
              f"{_function(src, 'goalPeople')}\n{_function(src, 'goalTerm')}\n{_function(src, 'sorted')}\n"
              f"console.log(JSON.stringify(sorted({json.dumps(places)}).map((p) => p.label)));")
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_price_sort_is_per_person_under_a_goal_with_people():
    # the card shows the offer's price ÷ people, so the sort orders by that: 1,221 < 1,332 < 1,733
    goal = {"id": "lease", "rules": [], "nice": [], "people": 3}
    assert _sort_order(goal) == ["3BED & 2BATH", "Palmetto", "227 Church St"]
    # no people: price / bed (1,221 < 1,300 < 1,332)
    assert _sort_order({**goal, "people": None}) == ["3BED & 2BATH", "227 Church St", "Palmetto"]


def _sqft_order(goal_id: str, key: str, people=None) -> list[str]:
    """app.js sorted() by sqft: under a goal the offer's sqft (the unit the card prices), under All max_sqft."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    places = [
        {"label": "small offer, big unit", "max_sqft": 2400, "goals": {"lease": {"offer": {"price_month": 3000, "sqft": 700}}}},
        {"label": "no sqft", "max_sqft": None, "goals": {"lease": {"offer": {"price_month": 3100, "sqft": None}}}},
        {"label": "big offer", "max_sqft": 1279, "goals": {"lease": {"offer": {"price_month": 5000, "sqft": 1279}}}},
        {"label": "no offer", "max_sqft": 900, "goals": {"lease": {}}},
    ]
    goal = {"id": "lease", "rules": [], "nice": [], "people": people}
    script = (f"const S = {{ goal: {json.dumps(goal_id)}, draft: {json.dumps(goal)}, goalsData: {{ goals: [] }} }};\n"
              f"const $ = () => ({{ value: {json.dumps(key)} }});\nconst RANK = {{}};\n"
              "const goalOffer = (p) => (S.goal !== 'all' && p.goals[S.goal]?.offer) || null;\n"
              "const savedGoal = () => null;\n"
              f"{_function(APP_JS.read_text(), 'goalPeople')}\n{_function(APP_JS.read_text(), 'goalTerm')}\n"
              f"{_function(APP_JS.read_text(), 'sorted')}\n"
              f"console.log(JSON.stringify(sorted({json.dumps(places)}).map((p) => p.label)));")
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_sqft_sort_reads_the_goals_offer_and_puts_unknown_last():
    # under a goal: the offer's sqft (1,279 > 700), not the place's largest unit; no sqft or no offer last
    assert _sqft_order("lease", "sqft")[:2] == ["big offer", "small offer, big unit"]
    assert set(_sqft_order("lease", "sqft")[2:]) == {"no sqft", "no offer"}
    # sqft per person with people 3: the same unit's sqft ÷ 3; no people falls back to plain sqft
    assert _sqft_order("lease", "sqftpp", people=3)[:2] == ["big offer", "small offer, big unit"]
    assert _sqft_order("lease", "sqftpp")[:2] == ["big offer", "small offer, big unit"]
    # All: the place's largest known unit
    assert _sqft_order("all", "sqft") == ["small offer, big unit", "big offer", "no offer", "no sqft"]


def test_reload_keeps_showing_the_draft_preview():
    # the X-Updating re-poll (loadPlaces every 5 s) once rendered the saved goal's results and then the draft's again
    # when the preview answered, so the counts flipped each poll; now every render after a reload shows the draft
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = APP_JS.read_text()
    script = f"""
const S = {{ draft: {{ id: 'lease', rules: [1] }}, preview: null, places: [] }};
const goalBody = (g) => ({{ rules: g.rules }});
const draftDirty = () => true;
const $ = () => ({{ scrollTop: 0 }});
const api = async () => [{{ place_key: 'a', goals: {{ lease: 'saved' }} }}];
const renders = []; let previews = 0;
const renderList = () => renders.push(S.places[0].goals.lease);
const previewDraft = () => {{ previews++; }};
const goalDiff = () => null;
{_function(src, 'applyGoalResults')}
{_function(src, 'loadPlaces')}
(async () => {{
  await loadPlaces();                       // no preview of this draft yet: nothing rendered, preview asked
  S.preview = {{ gid: 'lease', body: JSON.stringify(goalBody(S.draft)), places: {{ a: 'draft' }} }};
  await loadPlaces();                       // the last preview shows at once over the fresh places
  S.draft.rules = [2];
  await loadPlaces();                       // an edited draft the preview no longer matches: not rendered
  console.log(JSON.stringify({{ renders, previews }}));
}})();
"""
    out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
    assert out == {"renders": ["draft"], "previews": 3}


# ---- the draft diff: places an unsaved edit cuts (blue) or adds (pink)
LEASE_RULES = [{"field": "beds", "type": "min", "value": 3}, {"field": "baths", "type": "min", "value": 2},
               {"in_net": False, "max": 21, "mode": "walk", "set": "caltrain_stations", "type": "near"}]


def _node(src_names: list[str], body: str, prelude: str = "") -> object:
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = APP_JS.read_text()
    m = re.search(r"^const okStatus = .*$", src, re.M)
    assert m, "no okStatus in app.js"
    script = prelude + "\n" + m.group(0) + "\n" + "\n".join(_function(src, n) for n in src_names) + "\n" + body
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_diff_kind_cut_and_added():
    pairs = [["pass", "fail"], ["needs_check", "no_listings"], ["fail", "pass"], ["no_listings", "needs_check"],
             ["pass", "needs_check"], ["fail", "no_listings"], [None, "pass"], ["pass", None]]
    out = _node(["diffKind"], f"console.log(JSON.stringify({json.dumps(pairs)}.map(([a, b]) => diffKind(a, b))));")
    assert out == ["cut", "cut", "added", "added", None, None, "added", "cut"]


def test_changed_rules_are_the_edited_ones():
    tight = [*LEASE_RULES[:2], {**LEASE_RULES[2], "max": 18}]
    out = _node(["changedRules"], f"""const saved = {{ rules: {json.dumps(LEASE_RULES)} }};
      const ppp = {{ field: "price_per_person", type: "max", value: 2500 }};
      console.log(JSON.stringify([changedRules({{ rules: {json.dumps(tight)} }}, saved),
        changedRules({{ rules: saved.rules }}, saved),
        changedRules({{ rules: [ppp], people: 3 }}, {{ rules: [ppp], people: 2 }})]));""")
    assert out == [[2], [], [0]]


def test_cut_by_names_the_edited_rule_not_the_other_fails():
    # live case (2026-11-25, lease, walk to Caltrain 21 -> 18): the place passed; under the draft every plan fails, so
    # a 1 bd / 1 ba listing decides and beds and baths fail too. The cut is the walk rule.
    d = {"status": "fail", "reasons": ["beds >= 3: is 1", "baths >= 2: is 1", "walk to caltrain_stations <= 18: 19 min"],
         "fails": [{"s": "fail", "i": 0, "f": "beds"}, {"s": "fail", "i": 1, "f": "baths"},
                   {"s": "fail", "i": 2, "f": "near"}]}
    out = _node(["cutReasons"], f"console.log(JSON.stringify(cutReasons({json.dumps(d)}, {{ reasons: [] }}, [2])));")
    assert out == ["walk to caltrain_stations <= 18: 19 min"]
    # no changed rule fails (a cut to no listings, or results without `i`): the reasons the saved goal did not give
    nl = {"status": "no_listings", "reasons": ["no price for a fitting plan (1 fits)", "private bath ?: unknown"],
          "fails": [{"s": "needs_check", "i": 0, "f": "private_bath"}]}
    out = _node(["cutReasons"], f"console.log(JSON.stringify(cutReasons({json.dumps(nl)}, "
                                f"{{ reasons: ['private bath ?: unknown'] }}, [1])));")
    assert out == ["no price for a fitting plan (1 fits)"]


def _filtered(diff: dict | None, only: str | None, ticks: list[str]) -> list[str]:
    places = [{"place_key": "a", "label": "A", "n_active": 1, "goals": {"lease": {"status": "fail"}},
               "saved": {"lease": {"status": "pass"}}},
              {"place_key": "b", "label": "B", "n_active": 1, "goals": {"lease": {"status": "pass"}},
               "saved": {"lease": {"status": "pass"}}},
              {"place_key": "c", "label": "C", "n_active": 1, "goals": {"lease": {"status": "fail"}},
               "saved": {"lease": {"status": "fail"}}},
              {"place_key": "d", "label": "D", "n_active": 1, "goals": {"lease": {"status": "pass"}},
               "saved": {"lease": {"status": "fail"}}}]
    prelude = (f"const S = {{ goal: 'lease', places: {json.dumps(places)}, diff: {json.dumps(diff)}, only: {json.dumps(only)} }};\n"
               "const RANK = { pass: 0, needs_check: 1, no_listings: 2, fail: 3 };\n"
               f"const $ = (s) => (s === '#q' ? {{ value: '' }} : {{ checked: false }});\n"
               f"const $$ = () => {json.dumps(ticks)}.map((v) => ({{ value: v, checked: true }}));")
    return _node(["placeStatus", "filtered"], "console.log(JSON.stringify(filtered().map((p) => p.place_key)));",
                 prelude)


def test_cut_places_stay_listed_and_show_only_cut_or_added():
    diff = {"kinds": {"a": "cut", "d": "added"}, "why": {"a": ["x"], "d": ["y"]}, "cut": 1, "added": 1}
    assert _filtered(diff, None, ["pass", "needs_check"]) == ["a", "b", "d"]  # fail unticked: the cut one stays
    assert _filtered(diff, None, ["fail"]) == ["a", "c", "d"]  # pass unticked: the added one stays
    assert _filtered(diff, "cut", ["pass", "needs_check", "fail"]) == ["a"]  # "show only these" (cut)
    assert _filtered(diff, "added", ["pass", "needs_check", "fail"]) == ["d"]  # "show only these" (added)
    assert _filtered(None, "cut", ["pass", "needs_check"]) == ["b", "d"]  # no draft diff: the toggle does nothing


def test_added_by_names_the_loosened_rule():
    # loosening walk to Caltrain 21 -> 24: the place failed the saved walk rule (index 2); the saved rule the draft
    # changed is what added it, and the line quotes the saved goal's reason
    out = _node(["changedRules", "cutReasons"], f"""const saved = {{ rules: {json.dumps(LEASE_RULES)} }};
      const draft = {{ rules: {json.dumps([*LEASE_RULES[:2], {**LEASE_RULES[2], "max": 24}])} }};
      const s = {{ status: "fail", reasons: ["walk to caltrain_stations <= 21: 23 min"],
                   fails: [{{ s: "fail", i: 2, f: "near" }}] }};
      console.log(JSON.stringify(cutReasons(s, {{ status: "pass", reasons: [] }}, changedRules(saved, draft))));""")
    assert out == ["walk to caltrain_stations <= 21: 23 min"]
