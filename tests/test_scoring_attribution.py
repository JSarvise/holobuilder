"""Coverage of the three attribution states (applied,
gate_not_met, data_missing) on `SkillContribution`, contribution ordering
against `Team.effective_skill_instances()`, `SkillContribution`/
`ScoreBreakdown`'s declared field shape, `team_taint`'s union-over-cards
completeness indicator, and the fast-path/full-attribution parity guarantee
across all four model combinations plus a run-state flip.

Test names state the exact behavior each one pins, following
`tests/test_gates.py`'s convention. Card/skill/team fixtures come from
`tests/conftest.py`; the shipped six-card fixture team (`nene_01` as
leader) comes from `tests/fixtures/cards.json` via `load_cards`/`build_team`,
mirroring `tests/test_scoring_ev.py`'s own end-to-end check.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import re
import textwrap
from pathlib import Path

import pytest

import hbuilder.domain.scoring as scoring_module
from hbuilder.domain.card import EffectTarget, GateCondition, SlotOverride, SlotRider, Stats
from hbuilder.domain.gates import RunState
from hbuilder.domain.loader import load_cards
from hbuilder.domain.schema import TEAM_WIDE_TARGET_KIND
from hbuilder.domain.scoring import (
    CONTRIBUTION_STATES,
    ScoreBreakdown,
    SkillContribution,
    score_fast,
    score_with_breakdown,
    team_taint,
)
from hbuilder.domain.scoring_config import load_scoring_config
from hbuilder.domain.scoring_models import ACTIVATION_RATE_MODELS, STACKING_MODELS
from hbuilder.domain.team import build_team
from hbuilder.domain.unknown import UNKNOWN

from conftest import FIXTURE_CARDS_PATH, make_card, make_scoring_config, make_skill, make_team

_FIXTURE_MEMBER_IDS = ["fubuki_01", "mio_01", "matsuri_01", "polka_01", "lamy_01"]


def _shipped_fixture_team():
    cards = load_cards(FIXTURE_CARDS_PATH)
    return build_team(cards, "nene_01", _FIXTURE_MEMBER_IDS)


# --- SkillContribution, ScoreBreakdown, and the three attribution states ---


def test_ungated_active_skill_produces_applied_contribution_with_nonzero_rate_and_unset_gate_fields():
    skill = make_skill(
        "ACTIVE", duration=10.0, interval=10.0, probability_tier="Medium", magnitude=25.0
    )
    leader = make_card("leader_ungated", active=skill)
    members = [make_card(f"m_ungated_{i}") for i in range(5)]
    team = make_team([*members, leader])
    cfg = make_scoring_config(probability_tiers={"Low": 1.0, "Medium": 1.0, "High": 1.0})

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "leader_ungated" and c.slot_kind == "ACTIVE"
    )

    assert contribution.state == "applied"
    assert contribution.rate > 0.0
    assert contribution.gate_dimension is None
    assert contribution.gate_value is None
    assert contribution.gate_threshold is None
    assert contribution.gate_actual_count is None
    assert contribution.gate_fired is None


def test_gated_skill_one_member_short_produces_gate_not_met_with_actual_count_one_below_threshold():
    condition = GateCondition(kind="type", target="Cute", count=2)
    gated_skill = make_skill("PASSIVE", magnitude=20.0, condition=condition)
    leader = make_card("leader_gate_short", type_="Cute", passive=gated_skill)
    members = [make_card(f"m_short_{i}", type_="Pure") for i in range(5)]
    team = make_team([*members, leader])
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "leader_gate_short" and c.slot_kind == "PASSIVE"
    )

    assert contribution.state == "gate_not_met"
    assert contribution.rate == 0.0
    assert contribution.gate_actual_count == 1
    assert contribution.gate_threshold == 2
    assert contribution.gate_fired is False


def test_gate_at_exact_threshold_produces_applied_state_with_actual_count_equal_to_threshold():
    condition = GateCondition(kind="type", target="Cute", count=2)
    gated_skill = make_skill("PASSIVE", magnitude=20.0, condition=condition)
    # Exactly two Cute AMONG THE FIVE MEMBERS -- the carrier plus one. The
    # outfit slot is Pure and outside the five, so it cannot supply the second
    # Cute.
    carrier = make_card("leader_gate_exact", type_="Cute", passive=gated_skill)
    members = [carrier, make_card("m_exact_0", type_="Cute")] + [
        make_card(f"m_exact_{i}", type_="Pure") for i in range(1, 4)
    ]
    team = make_team([make_card("outfit_01", type_="Pure"), *members])
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "leader_gate_exact" and c.slot_kind == "PASSIVE"
    )

    assert contribution.state == "applied"
    assert contribution.gate_actual_count == 2
    assert contribution.gate_threshold == 2
    assert contribution.gate_fired is True
    assert contribution.rate == pytest.approx(0.20)


def test_skill_with_unknown_magnitude_produces_data_missing_state_naming_the_field():
    skill = make_skill(
        "ACTIVE", duration=10.0, interval=10.0, probability_tier="Medium", magnitude=UNKNOWN
    )
    leader = make_card("leader_unknown_mag", active=skill)
    members = [make_card(f"m_unknown_{i}") for i in range(5)]
    team = make_team([*members, leader])
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "leader_unknown_mag" and c.slot_kind == "ACTIVE"
    )

    assert contribution.state == "data_missing"
    assert contribution.rate == 0.0
    assert contribution.missing_field == "magnitude"


def test_team_of_six_skill_less_cards_produces_empty_contributions_and_total_equals_base_stat_sum():
    cards = [make_card(f"blank_{i}") for i in range(6)]
    team = make_team(cards)
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)

    assert breakdown.contributions == ()
    assert breakdown.total_score == pytest.approx(float(team.base_stat_sum()))


def test_contributions_appear_in_effective_skill_instances_order():
    team = _shipped_fixture_team()
    cfg = load_scoring_config()

    breakdown = score_with_breakdown(team, cfg)

    instances = team.effective_skill_instances()
    assert [c.card_id for c in breakdown.contributions] == [i.card_id for i in instances]
    assert [c.slot_kind for c in breakdown.contributions] == [i.slot_kind for i in instances]
    # The activation_rate_up skill (fubuki_01's ACTIVE) is folded into
    # team_rate_up and never joins a pool, but it must still appear in the
    # breakdown at its own ordered position — dropping it would
    # make the two orderings above diverge in length.
    assert len(breakdown.contributions) == len(instances)


def test_a_card_leading_and_also_a_member_carries_both_sources_exactly_once():
    """The two roles are independent: a card may wear the outfit AND be one of
    the five. When it does, its costume entry carries source 'costume' and its
    normal slots carry source 'normal' — which arises only when the leader
    really is also a member."""
    cards = load_cards(FIXTURE_CARDS_PATH)
    # nene_01 both wears the outfit and stands among the five.
    team = build_team(cards, "nene_01", ["nene_01", *_FIXTURE_MEMBER_IDS[:4]])
    cfg = load_scoring_config()

    breakdown = score_with_breakdown(team, cfg)

    costume_contribution = next(
        c for c in breakdown.contributions if c.card_id == "nene_01" and c.slot_kind == "costume"
    )
    normal_contributions = [
        c for c in breakdown.contributions if c.card_id == "nene_01" and c.slot_kind != "costume"
    ]

    assert costume_contribution.source == "costume"
    assert normal_contributions, "as a MEMBER it still contributes its normal slots"
    assert all(c.source == "normal" for c in normal_contributions)


def test_a_leader_outside_the_five_contributes_its_outfit_and_nothing_else():
    """The complement of the test above: when the outfit card is not among the
    members, only its costume entry appears."""
    team = _shipped_fixture_team()
    cfg = load_scoring_config()

    breakdown = score_with_breakdown(team, cfg)

    nene = [c for c in breakdown.contributions if c.card_id == "nene_01"]
    assert [c.slot_kind for c in nene] == ["costume"]


def test_team_taint_over_shipped_fixture_names_mio_01_and_its_active_magnitude_field():
    team = _shipped_fixture_team()

    taint = team_taint(team)

    assert any("mio_01" in entry and "magnitude" in entry for entry in taint)


def test_team_taint_returns_empty_for_a_team_of_fully_populated_cards():
    cards = [make_card(f"full_{i}") for i in range(6)]
    team = make_team(cards)

    assert team_taint(team) == ()


def test_life_recovery_contribution_present_with_computed_rate_despite_zero_family_weight():
    life_skill = make_skill("PASSIVE", family="life_recovery", effect_type="life", magnitude=12.0)
    leader = make_card("life_leader", passive=life_skill)
    members = [make_card(f"life_member_{i}") for i in range(5)]
    team = make_team([*members, leader])
    cfg = make_scoring_config()
    assert cfg.family_weights["life_recovery"] == 0.0

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "life_leader" and c.slot_kind == "PASSIVE"
    )

    assert contribution.state == "applied"
    assert contribution.rate == pytest.approx(0.12)
    assert contribution.weight == 0.0
    assert contribution.weighted_rate == 0.0


def test_every_contribution_state_is_one_of_the_declared_contribution_states():
    team = _shipped_fixture_team()
    cfg = load_scoring_config()

    breakdown = score_with_breakdown(team, cfg)

    assert set(CONTRIBUTION_STATES) == {"applied", "gate_not_met", "data_missing"}
    assert all(c.state in CONTRIBUTION_STATES for c in breakdown.contributions)


def test_skill_contribution_and_score_breakdown_carry_the_full_declared_field_set():
    field_names = {f.name for f in dataclasses.fields(SkillContribution)}
    assert {
        "card_id",
        "slot_kind",
        "source",
        "family",
        "effect_type",
        "state",
        "pool",
        "rate",
        "weight",
        "weighted_rate",
        "missing_field",
        "gate_dimension",
        "gate_value",
        "gate_threshold",
        "gate_actual_count",
        "gate_fired",
        "target_kind",
        "target_card_ids",
        "override_fired",
        "rider_family",
        "rider_effect_type",
        "rider_magnitude",
    } == field_names

    breakdown_fields = {f.name for f in dataclasses.fields(ScoreBreakdown)}
    assert {
        "total_score",
        "base_stat_sum",
        "effective_stat_sum",
        "combined_stat_rate",
        "combined_score_rate",
        "combined_support_rate",
        # The ACTIVE Score-UP pool, exposed for the board-plan valuer to price
        # the active-pool nodes (skill_effect_up / activation_rate / cooldown).
        "active_pool",
        "board_score_rate",
        # The board's POWER half, the other side of `board_score_rate`. The
        # total is `(effective_stat_sum + board_power) * (1 + score rate) *
        # (1 + board score rate)`, so a breakdown without it can only recover
        # the largest single line in the panel by dividing the total back out.
        "board_power",
        "team_rate_up",
        "contributions",
        "incomplete_skills",
        "stacking_model_name",
        "activation_rate_model_name",
        "active_stacking_model_name",
        "run_state",
        # Slot order: which rule scored the stat
        # pool, and the deck slot order that rule chose.
        "slot_order_rule",
        "member_order",
        # The two terms the game's Unit Score Details panel lists and the
        # ranking omits. Declared here so a field added without a decision
        # fails this guard rather than appearing silently.
        "memory_bonus",
        "member_power_up",
        # The deck-screen Overall Power stat block (MemberParameter + Outfit +
        # Passive) summed with the server's per-(card, attribute) float32
        # ceilings, distinct from the float64 `effective_stat_sum` the ranking
        # uses. A DISPLAY quantity, never a summand of `total_score`.
        "overall_power_stat_sum",
    } == breakdown_fields


# --- target_kind/target_card_ids attribution --------------------------------


def test_targeted_stat_up_contribution_reports_target_kind_and_ascending_target_card_ids():
    target = EffectTarget(kind="type", value="Cute", count=2)
    skill = make_skill(
        "PASSIVE", family="stat_up", effect_type="performance", magnitude=10.0, target=target
    )
    leader = make_card(
        "target_leader",
        type_="Cute",
        stats=Stats(performance=150, technique=100, sense=100),
        passive=skill,
    )
    member_b = make_card(
        "target_member_b", type_="Cute", stats=Stats(performance=200, technique=100, sense=100)
    )
    member_c = make_card(
        "target_member_c", type_="Cute", stats=Stats(performance=90, technique=100, sense=100)
    )
    fillers = [make_card(f"target_filler_{i}", type_="Pure") for i in range(2)]
    # Three Cute among the five members (carrier + b + c); the outfit card is
    # Pure and outside them, so it is neither a target nor a gate contributor.
    team = make_team([make_card("target_outfit", type_="Pure"), leader, member_b, member_c, *fillers])
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "target_leader" and c.slot_kind == "PASSIVE"
    )

    assert contribution.state == "applied"
    assert contribution.target_kind == "type"
    # TopNByBuffedStat ranks by descending performance: member_b(200) >
    # leader(150) > member_c(90) -- the top 2 of 3 qualifying Cute cards,
    # sorted ascending by id for deterministic output.
    assert contribution.target_card_ids == ("target_leader", "target_member_b")


def test_team_wide_target_stat_contribution_reports_whole_team_marker_and_lists_all_six_card_ids():
    skill = make_skill("PASSIVE", family="stat_up", effect_type="performance", magnitude=10.0)
    leader = make_card("allteam_leader", passive=skill)
    members = [make_card(f"allteam_member_{i}") for i in range(5)]
    team = make_team([*members, leader])
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "allteam_leader" and c.slot_kind == "PASSIVE"
    )

    assert contribution.state == "applied"
    assert contribution.target_kind == TEAM_WIDE_TARGET_KIND
    assert contribution.target_card_ids == tuple(sorted(card.id for card in team.members))


def test_data_missing_target_contribution_reports_none_for_both_target_fields():
    skill = make_skill(
        "PASSIVE", family="stat_up", effect_type="performance", magnitude=10.0, target=UNKNOWN
    )
    leader = make_card("unknown_target_leader", passive=skill)
    members = [make_card(f"unknown_target_member_{i}") for i in range(5)]
    team = make_team([*members, leader])
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c
        for c in breakdown.contributions
        if c.card_id == "unknown_target_leader" and c.slot_kind == "PASSIVE"
    )

    assert contribution.state == "data_missing"
    assert contribution.missing_field == "target"
    assert contribution.target_kind is None
    assert contribution.target_card_ids is None


def test_score_pool_contribution_carries_no_target_fields():
    # `score_support` is not a score-pool family -- it is a
    # multiplier on the ACTIVE pool and DOES resolve a target. The family
    # that still lands in the score pool is `conditional_score_up`.
    skill = make_skill("PASSIVE", family="conditional_score_up", effect_type="score", magnitude=10.0)
    leader = make_card("scorepool_leader", passive=skill)
    members = [make_card(f"scorepool_member_{i}") for i in range(5)]
    team = make_team([*members, leader])
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "scorepool_leader" and c.slot_kind == "PASSIVE"
    )

    assert contribution.pool == "score"
    assert contribution.target_kind is None
    assert contribution.target_card_ids is None


def test_pool_none_activation_rate_up_contribution_carries_no_target_fields():
    skill = make_skill(
        "ACTIVE",
        family="activation_rate_up",
        effect_type="activation_rate",
        duration=8.0,
        interval=10.0,
        probability_tier="Medium",
        magnitude=15.0,
    )
    leader = make_card("rateup_leader", active=skill)
    members = [make_card(f"rateup_member_{i}") for i in range(5)]
    team = make_team([*members, leader])
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "rateup_leader" and c.slot_kind == "ACTIVE"
    )

    assert contribution.pool == "none"
    assert contribution.target_kind is None
    assert contribution.target_card_ids is None


def test_gate_not_met_stat_contribution_carries_no_target_fields():
    condition = GateCondition(kind="type", target="Cute", count=3)
    skill = make_skill(
        "PASSIVE",
        family="stat_up",
        effect_type="performance",
        magnitude=10.0,
        condition=condition,
    )
    leader = make_card("gate_short_leader", type_="Cute", passive=skill)
    members = [make_card(f"gate_short_member_{i}", type_="Pure") for i in range(5)]
    team = make_team([*members, leader])
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c
        for c in breakdown.contributions
        if c.card_id == "gate_short_leader" and c.slot_kind == "PASSIVE"
    )

    assert contribution.state == "gate_not_met"
    assert contribution.target_kind is None
    assert contribution.target_card_ids is None


# --- override_fired attribution ---------------------------------------------


def _override_active_skill(magnitude: float, override_magnitude: float):
    return make_skill(
        "ACTIVE",
        duration=20.0,
        interval=20.0,
        probability_tier="Medium",
        magnitude=magnitude,
        override=SlotOverride(
            magnitude=override_magnitude,
            condition=GateCondition(kind="type", target="Pure", count=2),
        ),
    )


def _override_team(skill, *, pure_count: int):
    """`pure_count` Pure cards AMONG THE FIVE MEMBERS, which is what the
    override gate counts. The carrier is a member (a leader's ACTIVE is
    not emitted) and is Happy, so it never supplies a Pure itself; the
    outfit slot is Happy and outside the five for the same reason."""
    carrier = make_card("override_attrib_leader", type_="Happy", active=skill)
    types = ["Pure"] * pure_count + ["Cute"] * (4 - pure_count)
    members = [carrier] + [
        make_card(f"override_attrib_member_{i}", type_=types[i]) for i in range(4)
    ]
    return make_team([make_card("override_outfit", type_="Happy"), *members])


def test_override_fired_is_true_when_the_override_gate_fired():
    skill = _override_active_skill(magnitude=35.0, override_magnitude=70.0)
    team = _override_team(skill, pure_count=2)
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "override_attrib_leader"
    )

    assert contribution.override_fired is True


def test_override_fired_is_false_when_the_override_gate_did_not_fire():
    skill = _override_active_skill(magnitude=35.0, override_magnitude=70.0)
    team = _override_team(skill, pure_count=0)
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "override_attrib_leader"
    )

    assert contribution.override_fired is False


def test_override_fired_is_none_when_the_slot_carries_no_override():
    skill = make_skill(
        "ACTIVE", duration=20.0, interval=20.0, probability_tier="Medium", magnitude=35.0
    )
    team = _override_team(skill, pure_count=2)
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "override_attrib_leader"
    )

    assert contribution.override_fired is None


# --- rider_family/rider_effect_type/rider_magnitude attribution -------------


def _rider_special_skill():
    return make_skill(
        "SPECIAL",
        duration=10.0,
        magnitude=145.0,
        rider=SlotRider(
            family="activation_rate_up",
            effect_type="activation_rate",
            magnitude=55.0,
            condition=GateCondition(kind="run_state", target="life", count=1000),
            target=None,
        ),
    )


def _rider_team(skill):
    leader = make_card("rider_attrib_leader", special=skill)
    members = [make_card(f"rider_attrib_member_{i}") for i in range(5)]
    return make_team([*members, leader])


def test_rider_fields_are_populated_when_the_slot_carries_a_rider():
    team = _rider_team(_rider_special_skill())
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "rider_attrib_leader"
    )

    assert contribution.rider_family == "activation_rate_up"
    assert contribution.rider_effect_type == "activation_rate"
    assert contribution.rider_magnitude == 55.0


def test_rider_fields_are_all_none_when_the_slot_carries_no_rider():
    skill = make_skill("SPECIAL", duration=10.0, magnitude=145.0)
    team = _rider_team(skill)
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    contribution = next(
        c for c in breakdown.contributions if c.card_id == "rider_attrib_leader"
    )

    assert contribution.rider_family is None
    assert contribution.rider_effect_type is None
    assert contribution.rider_magnitude is None


def test_a_riders_attribution_fields_never_change_rate_weight_or_weighted_rate():
    """Attribution-only. A rider-carrying contribution's rate/weight/
    weighted_rate must equal the same slot's without a rider -- the rider
    fields describe the row, they do not perturb it."""
    with_rider = score_with_breakdown(_rider_team(_rider_special_skill()), make_scoring_config())
    without_rider = score_with_breakdown(
        _rider_team(make_skill("SPECIAL", duration=10.0, magnitude=145.0)), make_scoring_config()
    )
    c_with = next(c for c in with_rider.contributions if c.card_id == "rider_attrib_leader")
    c_without = next(c for c in without_rider.contributions if c.card_id == "rider_attrib_leader")

    assert c_with.rate == c_without.rate
    assert c_with.weight == c_without.weight
    assert c_with.weighted_rate == c_without.weighted_rate


# --- fast/attribution parity across all four model combinations -------------


def _cfg_for(stacking_key: str, activation_rate_key: str, *, run_state: RunState | None = None):
    cfg = load_scoring_config()
    overrides: dict = {
        "stacking_model": STACKING_MODELS[stacking_key],
        "activation_rate_model": ACTIVATION_RATE_MODELS[activation_rate_key],
    }
    if run_state is not None:
        overrides["run_state"] = run_state
    return dataclasses.replace(cfg, **overrides)


def test_fast_and_breakdown_totals_agree_under_additive_stacking_with_pp_activation_rate():
    team = _shipped_fixture_team()
    cfg = _cfg_for("additive", "pp")
    assert score_fast(team, cfg) == pytest.approx(score_with_breakdown(team, cfg).total_score)


def test_fast_and_breakdown_totals_agree_under_additive_stacking_with_multiplicative_activation_rate():
    team = _shipped_fixture_team()
    cfg = _cfg_for("additive", "multiplicative")
    assert score_fast(team, cfg) == pytest.approx(score_with_breakdown(team, cfg).total_score)


def test_fast_and_breakdown_totals_agree_under_multiplicative_stacking_with_pp_activation_rate():
    team = _shipped_fixture_team()
    cfg = _cfg_for("multiplicative", "pp")
    assert score_fast(team, cfg) == pytest.approx(score_with_breakdown(team, cfg).total_score)


def test_fast_and_breakdown_totals_agree_under_multiplicative_stacking_with_multiplicative_activation_rate():
    team = _shipped_fixture_team()
    cfg = _cfg_for("multiplicative", "multiplicative")
    assert score_fast(team, cfg) == pytest.approx(score_with_breakdown(team, cfg).total_score)


def test_fast_and_breakdown_totals_agree_for_a_team_carrying_all_three_attribution_states():
    condition = GateCondition(kind="type", target="Cute", count=2)
    gate_not_met_skill = make_skill(
        "PASSIVE", family="score_support", effect_type="score", magnitude=15.0, condition=condition
    )
    data_missing_skill = make_skill(
        "ACTIVE",
        family="score_support",
        effect_type="score",
        duration=10.0,
        interval=10.0,
        probability_tier="Medium",
        magnitude=UNKNOWN,
    )
    applied_skill = make_skill("PASSIVE", family="score_support", effect_type="score", magnitude=10.0)

    leader = make_card(
        "mixed_leader", type_="Pure", passive=gate_not_met_skill, active=data_missing_skill
    )
    member_applied = make_card("mixed_member_applied", type_="Pure", passive=applied_skill)
    fillers = [make_card(f"mixed_filler_{i}", type_="Pure") for i in range(3)]
    # The mixed carrier is a MEMBER so all three states are actually emitted;
    # every member is Pure, so the Cute>=2 gate is genuinely not met.
    team = make_team([make_card("mixed_outfit", type_="Pure"), leader, member_applied, *fillers])
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    states = {c.state for c in breakdown.contributions}
    assert states == {"applied", "gate_not_met", "data_missing"}
    assert score_fast(team, cfg) == pytest.approx(breakdown.total_score)


def test_fast_and_breakdown_totals_agree_when_run_state_flips_a_gate_off_and_the_total_changes():
    condition = GateCondition(kind="run_state", target="combo", count=100)
    skill = make_skill("PASSIVE", family="score_support", effect_type="score", magnitude=30.0, condition=condition)
    leader = make_card("runstate_leader", passive=skill)
    # A `score_support` skill multiplies the ACTIVE pool, so the team
    # needs an ACTIVE score contribution for flipping its gate to move the
    # total at all. Without one the support has nothing to raise and both
    # run states score the bare stat sum -- correct behaviour, and it would
    # make this test pass vacuously if the team were left as it was.
    # `make_team` takes cards[0] as the LEADER, whose own normal slots are
    # never emitted, so the ACTIVE has to sit at index
    # 1 or later to be a member at all.
    members = [
        make_card("runstate_member_0"),
        make_card(
            "runstate_member_1",
            active=make_skill(
                "ACTIVE",
                family="conditional_score_up",
                effect_type="score",
                duration=10.0,
                interval=10.0,
                probability_tier="Medium",
                magnitude=20.0,
            ),
        )
    ] + [make_card(f"runstate_member_{i}") for i in range(2, 5)]
    team = make_team([*members, leader])

    on_state = RunState(life=1000, combo=100)
    off_state = RunState(life=0, combo=0)
    cfg_on = make_scoring_config(run_state=on_state)
    cfg_off = make_scoring_config(run_state=off_state)

    total_on_fast = score_fast(team, cfg_on)
    total_on_breakdown = score_with_breakdown(team, cfg_on).total_score
    total_off_fast = score_fast(team, cfg_off)
    total_off_breakdown = score_with_breakdown(team, cfg_off).total_score

    assert total_on_fast == pytest.approx(total_on_breakdown)
    assert total_off_fast == pytest.approx(total_off_breakdown)
    assert total_on_fast != pytest.approx(total_off_fast)


# --- Breakdown reconstruction: the totals are arithmetically auditable ------


def test_breakdown_score_pool_contributions_recombine_to_combined_score_rate():
    """`combined_score_rate` is not a bare combine of the score pool's own
    weighted rates. Every Score UP collapses into ONE aggregate
    (`SkillScoreUpRatio` = `(1 + B) x max_ACTIVE`), where `B` is itself a max
    per bucket over the boost population — the `score_support` rows **plus**
    every SPECIAL Score UP, which is a `ScoreUpBoostSkillEffect`.
    Reconstructing it therefore needs BOTH
    pools plus each row's slot kind, and the point this pins is that the
    breakdown still carries everything required to do the reconstruction from
    the outside: `pool`, `slot_kind`, `effect_type` and `weighted_rate`.

    ⚠ It also pins what a reader must NOT do: `sum(score rows)` is not
    `combined_score_rate`, because the pool is a maximum."""
    team = _shipped_fixture_team()
    cfg = load_scoring_config()
    breakdown = score_with_breakdown(team, cfg)

    support = [c for c in breakdown.contributions if c.pool == "support"]
    assert support, "the shipped fixture must exercise the support pool"

    score_up = [
        c
        for c in breakdown.contributions
        if c.pool == "score"
        and c.effect_type == "score"
        and c.slot_kind in ("ACTIVE", "SPECIAL")
    ]
    # The SPECIAL collapse: a SPECIAL Score UP is a `ScoreUpBoostSkillEffect`,
    # so the boost population is the support rows PLUS every SPECIAL Score UP,
    # and the ratio population is the ACTIVE Score UPs alone.
    ratio_rows = [c for c in score_up if c.slot_kind != "SPECIAL"]
    boost_rows = support + [c for c in score_up if c.slot_kind == "SPECIAL"]

    # `B`: max per bucket over the boost rows, then the two maxima added. A row's
    # own `weighted_rate` is already `magnitude x occupancy`, and the fixture has
    # at most one live row per bucket, so the rates can be maximised directly.
    boost = max(
        (c.weighted_rate for c in boost_rows if c.slot_kind != "SPECIAL"), default=0.0
    ) + max(
        (c.weighted_rate for c in boost_rows if c.slot_kind == "SPECIAL"), default=0.0
    )
    assert boost == pytest.approx(breakdown.combined_support_rate)

    other = [
        c.weighted_rate
        for c in breakdown.contributions
        if c.pool == "score" and c not in score_up
    ]
    pool = sum(c.weighted_rate * (1.0 + boost) for c in ratio_rows)
    recombined = cfg.stacking_model.combine([*other, pool])

    assert recombined == pytest.approx(breakdown.combined_score_rate)
    if len(score_up) > 1:
        naive = cfg.stacking_model.combine(
            [c.weighted_rate for c in breakdown.contributions if c.pool == "score"]
        )
        assert naive != pytest.approx(breakdown.combined_score_rate)


def test_breakdown_stat_pool_contributions_recombine_to_combined_stat_rate():
    team = _shipped_fixture_team()
    cfg = load_scoring_config()
    breakdown = score_with_breakdown(team, cfg)

    stat_weighted_rates = [c.weighted_rate for c in breakdown.contributions if c.pool == "stat"]
    recombined = cfg.stacking_model.combine(stat_weighted_rates)

    assert recombined == pytest.approx(breakdown.combined_stat_rate)


def test_total_score_equals_effective_stat_sum_times_one_plus_combined_score_rate():
    team = _shipped_fixture_team()
    cfg = load_scoring_config()
    breakdown = score_with_breakdown(team, cfg)

    assert breakdown.total_score == pytest.approx(
        breakdown.effective_stat_sum * (1.0 + breakdown.combined_score_rate)
    )


# --- Structural: neither entry point can grow a second implementation ------


def _sole_call_target(func) -> str:
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert len(calls) == 1, f"expected exactly one call in {func.__name__}, found {len(calls)}"
    call = calls[0]
    assert isinstance(call.func, ast.Name), f"{func.__name__} does not call a bare name"
    return call.func.id


def test_score_fast_body_contains_exactly_one_call_targeting_the_shared_driver():
    assert _sole_call_target(scoring_module.score_fast) == "_score_team"


def test_score_with_breakdown_body_contains_exactly_one_call_targeting_the_shared_driver():
    assert _sole_call_target(scoring_module.score_with_breakdown) == "_score_team"


def test_neither_entry_point_calls_resolve_team_gates_or_ev_functions_directly():
    fast_source = inspect.getsource(scoring_module.score_fast)
    breakdown_source = inspect.getsource(scoring_module.score_with_breakdown)

    assert "resolve_team_gates" not in fast_source
    assert "resolve_team_gates" not in breakdown_source
    assert "EV_FUNCTIONS" not in fast_source
    assert "EV_FUNCTIONS" not in breakdown_source


def test_fold_activation_rate_up_and_ev_functions_are_referenced_from_their_two_shared_drivers_only():
    source = Path(scoring_module.__file__).read_text(encoding="utf-8")

    # _fold_activation_rate_up has exactly two legitimate call sites:
    # _score_team's pass 1 (the per-team driver) and
    # score_card_set's shared prefix (the set-level batch driver) — both
    # are shared drivers, not a duplicated implementation, since each is
    # itself the *sole* place gate resolution plus EV arithmetic happens
    # for its own entry points (the fast/attribution parity tests for the
    # former, test_batch_api_parity.py's bit-identical sweep for the latter).
    # A THIRD call site would mean a private copy leaked into some other
    # function. EV_FUNCTIONS is subscripted
    # only from _resolve_skill's dispatch, which score_card_set reuses
    # rather than re-dispatching itself. The negative lookbehinds exclude
    # the symbol's own `def ...(` declaration line and any
    # backtick-quoted docstring mention, counting only actual
    # call/subscript sites.
    assert len(re.findall(r"(?<!def )_fold_activation_rate_up\(", source)) == 2
    assert len(re.findall(r"(?<!`)EV_FUNCTIONS\[", source)) == 1
