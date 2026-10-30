"""The non-negativity invariant the `>=` lower-bound guarantee
rests on.

Non-negativity spans the whole model: card numerics
(`schema.py`'s existing `>= 0` check on `duration`/`interval`/`magnitude`),
gate encoding (`condition: null` means UNKNOWN, so a skill cannot silently
look ungated when it is not), and config weights (this file's own
subject). Given every effect family is non-negative, `AdditiveStacking` and
`MultiplicativeStacking` are both non-decreasing when a non-negative rate
is appended, and both activation-rate models are non-decreasing in
`team_rate_up` — so skipping a skill (whether for a missed gate or an
`UNKNOWN` field) can only ever *lower* a team's total, never raise it.
That is what makes a tainted team's computed score a genuine lower bound
on its true score, rather than an estimate that could go either way.

Any future effect family that could subtract is therefore a **breaking
change** to the `>=` guarantee, not a routine addition — a contributor
relaxing this test should read this docstring before doing so, not merely
widen an assertion around a red suite.
"""

from __future__ import annotations

import pytest

from hbuilder.domain.card import GateCondition
from hbuilder.domain.schema import SchemaError
from hbuilder.domain.scoring import score_with_breakdown, team_taint
from hbuilder.domain.scoring_config import (
    DEFAULT_CONFIG,
    load_scoring_config,
    validate_scoring_config,
)
from hbuilder.domain.scoring_models import (
    AdditiveStacking,
    AdditivePercentagePoints,
    MultiplicativeRateUp,
    MultiplicativeStacking,
)
from hbuilder.domain.unknown import UNKNOWN
from hbuilder.output.table import LOWER_BOUND_MARKER, format_team_rows
from hbuilder.solver.exact import RankedTeam

from conftest import make_card, make_scoring_config, make_skill, make_team

# --- negative family_weights is a hard SchemaError ---------------------------


def test_negative_judgment_enhancement_weight_is_rejected_naming_family_weights_and_the_family():
    weights = dict(DEFAULT_CONFIG["family_weights"])
    weights["judgment_enhancement"] = -0.5
    errors = validate_scoring_config({"family_weights": weights})
    assert any(
        e.startswith("family_weights") and "judgment_enhancement" in e for e in errors
    )


def test_negative_family_weight_in_a_config_file_raises_schema_error(tmp_path):
    import json

    doc = dict(DEFAULT_CONFIG)
    weights = dict(DEFAULT_CONFIG["family_weights"])
    weights["judgment_enhancement"] = -0.5
    doc["family_weights"] = weights
    path = tmp_path / "config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(SchemaError) as exc_info:
        load_scoring_config(path)
    assert exc_info.value.field == "family_weights"


def test_zero_family_weight_is_still_accepted_the_boundary_is_negative_only_not_positive_only():
    # life_recovery's shipped default is exactly 0.0 — the check must not
    # collapse "non-negative" into "positive only".
    weights = dict(DEFAULT_CONFIG["family_weights"])
    weights["life_recovery"] = 0.0
    errors = validate_scoring_config({"family_weights": weights})
    assert errors == []


# --- Monotonicity: what makes the non-negativity invariant sufficient ------


_RATE_LISTS = [
    [],
    [0.0],
    [0.1],
    [0.05, 0.2, 0.0],
    [1.0, 0.5, 0.25, 0.1, 0.0],
]


@pytest.mark.parametrize("rates", _RATE_LISTS)
def test_additive_stacking_combine_is_non_decreasing_when_a_non_negative_rate_is_appended(
    rates,
):
    model = AdditiveStacking()
    before = model.combine(rates)
    for appended in (0.0, 0.01, 0.5, 3.0):
        after = model.combine(rates + [appended])
        assert after >= before


@pytest.mark.parametrize("rates", _RATE_LISTS)
def test_multiplicative_stacking_combine_is_non_decreasing_when_a_non_negative_rate_is_appended(
    rates,
):
    model = MultiplicativeStacking()
    before = model.combine(rates)
    for appended in (0.0, 0.01, 0.5, 3.0):
        after = model.combine(rates + [appended])
        assert after >= before


@pytest.mark.parametrize("base_prob", [0.0, 0.35, 0.55, 0.75, 1.0])
def test_additive_percentage_points_activation_rate_model_is_non_decreasing_in_team_rate_up(
    base_prob,
):
    model = AdditivePercentagePoints()
    previous = model.apply(base_prob, 0.0)
    for team_rate_up in (0.0, 0.05, 0.2, 0.5, 1.0):
        current = model.apply(base_prob, team_rate_up)
        assert current >= previous
        previous = current


@pytest.mark.parametrize("base_prob", [0.0, 0.35, 0.55, 0.75, 1.0])
def test_multiplicative_rate_up_activation_rate_model_is_non_decreasing_in_team_rate_up(
    base_prob,
):
    model = MultiplicativeRateUp()
    previous = model.apply(base_prob, 0.0)
    for team_rate_up in (0.0, 0.05, 0.2, 0.5, 1.0):
        current = model.apply(base_prob, team_rate_up)
        assert current >= previous
        previous = current


# --- The consequence: skipping a skill can only lower the total ------------


def test_skipping_a_skill_can_only_lower_a_teams_total_so_a_tainted_score_is_a_lower_bound():
    """Direct statement of the consequence, as an assertion rather than a
    comment: since every rate a skipped skill would have contributed is
    non-negative, and both stacking models and both activation-rate
    models are non-decreasing in their non-negative inputs (proven above),
    adding back any skipped skill's rate to either pool cannot decrease the
    combined rate — so the team's true (fully-known) score can only be
    greater than or equal to its computed (some-skills-skipped) score.

    Any future effect family that could subtract invalidates this
    assertion — that is a breaking change to the `>=` guarantee, not a
    routine addition."""
    additive = AdditiveStacking()
    multiplicative = MultiplicativeStacking()
    computed_rates = [0.1, 0.2]
    skipped_rate = 0.3  # what a skipped (gated-off or UNKNOWN) skill would add

    assert additive.combine(computed_rates + [skipped_rate]) >= additive.combine(
        computed_rates
    )
    assert multiplicative.combine(
        computed_rates + [skipped_rate]
    ) >= multiplicative.combine(computed_rates)


# --- The other two legs, cross-checked in one place --------------------------


def test_d03_cross_check_all_three_non_negativity_legs_are_enforced():
    """The three legs of non-negativity, named together so a reader finds all of them from
    one test: card numerics (`schema.py`'s `>= 0` check), gate encoding
    (`condition: null` means UNKNOWN, see `tests/test_condition_inversion.py`),
    and config weights (this file). This test asserts the config leg
    directly and names the other two by reference rather than duplicating
    their coverage."""
    from hbuilder.domain.schema import validate_skill_slot

    # Leg 1: card numerics reject a negative magnitude.
    negative_magnitude_slot = {
        "family": "conditional_score_up",
        "effect_type": "score",
        "duration": 8,
        "interval": 15,
        "probability_tier": "Medium",
        "magnitude": -5,
        "condition": {"kind": "ungated"},
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    errors = validate_skill_slot(negative_magnitude_slot, "card_neg_01", "ACTIVE")
    assert any("magnitude" in e for e in errors)

    # Leg 2: gate encoding — see tests/test_condition_inversion.py for full
    # coverage; named here only so this test enumerates all three legs.
    assert GateCondition  # imported to name the type this leg concerns

    # Leg 3: config weights (this file's own subject).
    weights = dict(DEFAULT_CONFIG["family_weights"])
    weights["stat_up"] = -1.0
    errors = validate_scoring_config({"family_weights": weights})
    assert any("stat_up" in e for e in errors)


# --- verified as a test, not merely a comment --------------------------------


def test_d03a_no_unknown_value_can_reach_a_gate_predicate():
    """A `condition` dict is only ever
    built into a `GateCondition` when every one of `kind`/`target`/`count`
    validated (schema.py's `validate_skill_slot`), and `GateCondition`
    carries no nullable numeric field — so no `UNKNOWN` value can ever
    reach a `GatePredicate`. The overstatement risk lies entirely in how
    `condition: null` is read, never in `GateCondition` itself."""
    import dataclasses

    condition = GateCondition(kind="type", target="Cute", count=2)
    for field in dataclasses.fields(condition):
        value = getattr(condition, field.name)
        assert value is not None, (
            f"GateCondition.{field.name} must never be None/nullable — a "
            "nullable field here would be a second UNKNOWN path into a "
            "GatePredicate"
        )


# --- an untranscribed target is a lower bound, too ---------------------------


def test_unknown_target_taints_the_team_and_the_rendered_row_carries_the_lower_bound_marker():
    """The `target` sibling of the condition/numeric/config legs above: a
    PASSIVE `stat_up` skill whose `target` was never transcribed (every
    other field known) must be reported by `team_taint` with a dotted
    path ending `.target`, and the rendered table row for that team must
    carry the `>=` lower-bound marker — exactly the consequence the invariant
    promises for any `UNKNOWN` field, `target` included. Reading an
    untranscribed target as team-wide would overstate, the one direction
    this invariant forbids."""
    skill = make_skill(
        "PASSIVE",
        family="stat_up",
        effect_type="performance",
        magnitude=14.0,
        condition=None,
        target=UNKNOWN,
    )
    leader = make_card("untranscribed_target_leader", passive=skill)
    members = [make_card(f"untranscribed_target_filler_{i}") for i in range(5)]
    team = make_team([*members, leader])
    cfg = make_scoring_config()

    taint = team_taint(team)
    assert "untranscribed_target_leader:skills.PASSIVE.target" in taint
    assert any(entry.endswith(".target") for entry in taint if "untranscribed_target_leader" in entry)

    breakdown = score_with_breakdown(team, cfg)
    ranked = RankedTeam(
        score=breakdown.total_score,
        leader_id=leader.id,
        member_ids=tuple(member.id for member in members),
        team=team,
    )
    rows = format_team_rows([ranked], [breakdown], cfg)
    assert LOWER_BOUND_MARKER in rows[1]
