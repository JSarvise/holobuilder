"""Run-state gates carry their own numeric threshold.

A flag-shaped run-state gate, with the threshold inside the flag NAME
(`life_at_or_above_600`) and the condition's `count` pinned to `1`, can
express exactly one threshold per dimension — and two different LIFE
thresholds occur on real cards, on
different skills: 600 ("When LIFE is 600 or higher, Score UP 120%") and 1000
(`ライフ1000以上で発動率が45%UP`). The threshold is therefore a per-skill
parameter, and the run state carries the numeric value it is compared against.

The behavioural half of this file pins that comparison. The structural half
pins the thing that would otherwise rot silently: `RUN_STATE_DIMENSIONS` has
three readers, and if one of them (e.g. `cli.py`) hardcoded the dimension
names instead of iterating the constant, a change to the dimensions would adapt
in two renderers and keep emitting the old shape in the third — producing
*plausible* output, never an error. That is the failure class of a constant
most call sites read through and one does not, which
`tests/test_unknown_condition_is_data_missing.py` guards one layer down. A behaviour test cannot catch it, because the wrong
output is well-formed; only a structural walk can.

Follows the AST-walk convention of `tests/test_source_of_truth.py`: a walk over
real syntax, never a string search of the source text, so a comment or
docstring that merely mentions a dimension name cannot fool it.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from hbuilder.domain.card import GateCondition
from hbuilder.domain.gates import GatePredicate, RunState
from hbuilder.domain.schema import RUN_STATE_DIMENSIONS, validate_skill_slot
from hbuilder.domain.scoring_config import load_scoring_config

from conftest import DEFAULT_RUN_STATE, make_card, make_skill

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "src" / "hbuilder"

# Every module that renders `assumed_run_state` for a human or a machine.
RENDERERS = (
    PACKAGE_ROOT / "cli.py",
    PACKAGE_ROOT / "output" / "json_output.py",
    PACKAGE_ROOT / "output" / "assumptions.py",
)

# The pre-revision flag names. These must not survive anywhere as identifiers
# or as literal strings in executable positions.
RETIRED_FLAG_NAMES = ("combo_100_reached", "life_at_or_above_600")


def _aggregates_for(cards):
    from hbuilder.domain.gates import aggregate

    return aggregate(tuple(cards))


# --- Behaviour: the threshold is per-skill, not global ---------------------


@pytest.mark.parametrize(
    "life,threshold,should_fire",
    [
        (1000, 600, True),    # perfect play clears the lenient gate
        (1000, 1000, True),   # ...and the strict one, at the boundary
        (999, 1000, False),   # one short of the strict gate
        (600, 600, True),     # boundary is inclusive (>=)
        (599, 600, False),
    ],
)
def test_run_state_gate_compares_against_its_own_threshold(life, threshold, should_fire):
    predicate = GatePredicate(dimension="run_state", value="life", threshold=threshold)
    agg = _aggregates_for([make_card(f"c{i}") for i in range(6)])
    resolution = predicate.resolve(agg, RunState(life=life, combo=100))

    assert resolution.fired is should_fire, (
        f"a LIFE gate with threshold {threshold} against life={life} must "
        f"{'fire' if should_fire else 'not fire'}. The threshold is a "
        "per-skill parameter carried by the condition's `count`, not a "
        "constant folded into a flag name"
    )
    assert resolution.actual_count == life, (
        "actual_count must report the real run-state value, not a 1/0 "
        "coercion — the breakdown shows the owner what the gate was "
        "compared against (SCOR-08)"
    )


def test_two_life_thresholds_coexist_on_one_run_state():
    """The case the boolean encoding could not represent at all."""
    agg = _aggregates_for([make_card(f"c{i}") for i in range(6)])
    run_state = RunState(life=800, combo=100)

    lenient = GatePredicate(dimension="run_state", value="life", threshold=600)
    strict = GatePredicate(dimension="run_state", value="life", threshold=1000)

    assert lenient.resolve(agg, run_state).fired is True
    assert strict.resolve(agg, run_state).fired is False, (
        "two skills with different LIFE thresholds must resolve "
        "independently against one run state. A per-dimension boolean "
        "forced them to share a single answer, which is why the encoding "
        "changed"
    )


# --- Schema: the count != 1 pin is gone ------------------------------------


@pytest.mark.parametrize("threshold", [1, 100, 600, 1000, 1200])
def test_schema_accepts_any_positive_run_state_threshold(threshold):
    record = {
        "family": "score_support",
        "effect_type": "score",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 50.0,
        "condition": {"kind": "run_state", "target": "life", "count": threshold},
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    errors = validate_skill_slot(record, "PASSIVE", "test_card")
    count_errors = [e for e in errors if e.startswith("condition.count")]
    assert count_errors == [], (
        f"a run_state condition with count={threshold} must validate. The "
        "old `count must be exactly 1` pin assumed run-state gates were "
        f"flag-shaped; it would reject every attested threshold. Got: "
        f"{count_errors}"
    )


def test_schema_still_rejects_a_non_positive_threshold():
    record = {
        "family": "score_support",
        "effect_type": "score",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 50.0,
        "condition": {"kind": "run_state", "target": "life", "count": 0},
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    errors = validate_skill_slot(record, "PASSIVE", "test_card")
    assert any(e.startswith("condition.count") for e in errors), (
        "removing the `count != 1` pin must not also remove the generic "
        "`count >= 1` floor"
    )


# --- Config: numeric, and booleans rejected loudly -------------------------


def test_run_state_dimensions_match_the_dataclass_fields():
    assert tuple(f.name for f in dataclasses.fields(RunState)) == RUN_STATE_DIMENSIONS


def test_shipped_config_run_state_is_numeric():
    cfg = load_scoring_config()
    for dim in RUN_STATE_DIMENSIONS:
        value = getattr(cfg.run_state, dim)
        assert isinstance(value, int) and not isinstance(value, bool), (
            f"assumed_run_state.{dim} must load as a real int, not a bool. "
            "`bool` is a subclass of `int`, so a stale `true` would load as "
            "1 and silently stop every gate above threshold 1 from firing"
        )


def test_a_boolean_run_state_value_is_rejected_rather_than_coerced():
    from hbuilder.domain.scoring_config import validate_scoring_config

    errors = validate_scoring_config({"assumed_run_state": {"life": True, "combo": 100}})
    assert any("life" in e for e in errors), (
        "a pre-revision boolean must be rejected by name, not coerced to 1. "
        "Silent coercion is the migration failure this check exists to stop"
    )


# --- Structural: no renderer may hardcode dimension names ------------------


def _string_constants(tree: ast.AST) -> list[str]:
    """Every string constant in an executable position — docstrings and
    comments excluded, so prose naming a retired flag cannot trip this."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


@pytest.mark.parametrize("path", RENDERERS, ids=lambda p: p.name)
def test_no_renderer_hardcodes_a_run_state_dimension_name(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    literals = set(_string_constants(tree))
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    offenders = [d for d in RUN_STATE_DIMENSIONS if d in literals]
    assert offenders == [], (
        f"{path.name} names run-state dimension(s) {offenders} as a string "
        "literal instead of iterating RUN_STATE_DIMENSIONS. Every renderer "
        "must read the constant: this is exactly how cli.py drifted, and the "
        "drift is invisible at behaviour level because the hardcoded "
        "renderer keeps producing well-formed output"
    )

    retired = [f for f in RETIRED_FLAG_NAMES if f in literals or f in attributes]
    assert retired == [], (
        f"{path.name} still references retired flag name(s) {retired}. These "
        "were replaced by numeric dimensions; a survivor means a renderer "
        "was not migrated"
    )


def test_no_module_in_the_package_still_references_a_retired_flag_name():
    survivors = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        literals = set(_string_constants(tree))
        attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for flag in RETIRED_FLAG_NAMES:
            if flag in literals or flag in attributes or flag in names:
                survivors.append(f"{path.relative_to(PACKAGE_ROOT)}:{flag}")
    assert survivors == [], (
        f"retired run-state flag names survive in executable positions: "
        f"{survivors}"
    )
