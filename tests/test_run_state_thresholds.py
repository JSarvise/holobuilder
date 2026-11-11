"""Run-state gates carry their own numeric threshold.

A flag-shaped run-state gate, with the threshold inside the flag NAME
(`life_at_or_above_600`) and the condition's `count` pinned to `1`, can
express exactly one threshold per dimension — and two different LIFE
thresholds occur on real cards, on
different skills: 600 ("When LIFE is 600 or higher, Score UP 120%") and 1000
(`ライフ1000以上で発動率が45%UP`). The threshold is therefore a per-skill
parameter, and the run state carries the numeric value it is compared against.

This file pins that comparison, and that `RunState` carries exactly the
dimensions `RUN_STATE_DIMENSIONS` names.
"""

from __future__ import annotations

import dataclasses

import pytest
from support.builders import make_card

from hbuilder.domain.gates import GatePredicate, RunState
from hbuilder.domain.schema import RUN_STATE_DIMENSIONS, validate_skill_slot
from hbuilder.domain.scoring_config import load_scoring_config


def _aggregates_for(cards):
    from hbuilder.domain.gates import aggregate

    return aggregate(tuple(cards))


# --- Behaviour: the threshold is per-skill, not global ---------------------


@pytest.mark.parametrize(
    "life,threshold,should_fire",
    [
        (1000, 600, True),  # perfect play clears the lenient gate
        (1000, 1000, True),  # ...and the strict one, at the boundary
        (999, 1000, False),  # one short of the strict gate
        (600, 600, True),  # boundary is inclusive (>=)
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
        "coercion — the breakdown shows the user what the gate was "
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
        "removing the `count != 1` pin must not also remove the generic `count >= 1` floor"
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
