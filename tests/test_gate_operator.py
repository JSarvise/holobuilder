"""A run-state gate carries a comparison OPERATOR (`gte` | `lte`), not just a
threshold.

A resolver that hardcoded `actual >= threshold` for every dimension, or a
recon import parser (`skills.py`) that matched only the `_gte-` suffix, would
let any `life_lte-*` trigger fall through to an UNKNOWN condition and score
nothing. That is safe only *by accident*: the game's master enum defines
`LIVE_SKILL_TRIGGER_TYPE_LIFE_LTE = 11` with thresholds 500/700/950 ("When
LIFE is 500/700/950 or less,") for comeback / desperation skills — the exact
INVERSE of `>=`. Extending the parser to `lte` without teaching the resolver
the operator would flip a never-fires gate into an always-fires bonus
(perfect-play `life=1000 >= 950` is true), overstating those skills' EV — a
silent correctness failure.

No shipped card uses LIFE_LTE yet (no references across the skill-level
tables and `data/cards.json`), so this is preventive: it closes the trap
before the first LTE card lands. `gte` is the default so every existing gate
is unchanged.
"""

from __future__ import annotations

import pytest

from hbuilder.domain.card import GateCondition
from hbuilder.domain.gates import GatePredicate, RunState, predicate_from_condition
from hbuilder.domain.schema import validate_skill_slot

from conftest import make_card


def _agg():
    from hbuilder.domain.gates import aggregate

    return aggregate(tuple(make_card(f"c{i}") for i in range(6)))


# --- The regression the trap would cause -----------------------------------


@pytest.mark.parametrize(
    "operator,life,threshold,should_fire",
    [
        # LTE: fires only when LIFE is AT OR BELOW the threshold.
        ("lte", 1000, 950, False),  # <-- the trap: perfect play must NOT fire it
        ("lte", 950, 950, True),    # boundary is inclusive
        ("lte", 500, 700, True),    # comfortably below
        ("lte", 701, 700, False),   # one above
        # GTE: unchanged behaviour, fires at or above.
        ("gte", 1000, 950, True),
        ("gte", 949, 950, False),
        ("gte", 950, 950, True),
    ],
)
def test_run_state_operator_direction(operator, life, threshold, should_fire):
    predicate = GatePredicate(
        dimension="run_state", value="life", threshold=threshold, operator=operator
    )
    resolution = predicate.resolve(_agg(), RunState(life=life, combo=100))
    assert resolution.fired is should_fire, (
        f"a LIFE {operator} gate with threshold {threshold} against "
        f"life={life} must {'fire' if should_fire else 'not fire'}"
    )
    assert resolution.actual_count == life


def test_operator_defaults_to_gte():
    """Every gate without an explicit `operator` must keep its
    `>=` behaviour untouched."""
    predicate = GatePredicate(dimension="run_state", value="life", threshold=950)
    assert predicate.operator == "gte"
    assert predicate.resolve(_agg(), RunState(life=1000, combo=100)).fired is True


def test_predicate_from_condition_carries_operator():
    condition = GateCondition(
        kind="run_state", target="life", count=950, operator="lte"
    )
    predicate = predicate_from_condition(condition)
    assert predicate.operator == "lte"
    assert predicate.resolve(_agg(), RunState(life=1000, combo=100)).fired is False


def test_gate_condition_operator_defaults_to_gte():
    condition = GateCondition(kind="run_state", target="life", count=600)
    assert condition.operator == "gte"


# --- Schema: operator is validated, and only on run_state ------------------


def _slot(**condition_extra):
    return {
        "family": "score_support",
        "effect_type": "score",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 50.0,
        "condition": {
            "kind": "run_state",
            "target": "life",
            "count": 950,
            **condition_extra,
        },
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }


def test_schema_accepts_valid_operator():
    assert validate_skill_slot(_slot(operator="lte"), card_id="x", slot_kind="ACTIVE") == []
    assert validate_skill_slot(_slot(operator="gte"), card_id="x", slot_kind="ACTIVE") == []


def test_schema_rejects_unknown_operator():
    errors = validate_skill_slot(_slot(operator="ne"), card_id="x", slot_kind="ACTIVE")
    assert any("operator" in e for e in errors), errors


def test_schema_run_state_operator_optional():
    """Omitting operator is valid — it defaults to gte."""
    assert validate_skill_slot(_slot(), card_id="x", slot_kind="ACTIVE") == []


def test_schema_rejects_operator_on_type_condition():
    slot = {
        "family": "score_support",
        "effect_type": "score",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 50.0,
        "condition": {"kind": "type", "target": "Cute", "count": 2, "operator": "lte"},
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    errors = validate_skill_slot(slot, card_id="x", slot_kind="ACTIVE")
    assert any("operator" in e for e in errors), errors
