"""Gate coverage: overlapping Type/Generation gates, dual-generation
membership, threshold boundary, absent target, member-order invariance.

Card fixtures are built in-memory here, independent of `data/cards.json`.
"""

from __future__ import annotations

import inspect
import itertools
import typing

import pytest

from hbuilder.domain.card import Card, GateCondition, SkillSlot, Stats
from hbuilder.domain.gates import (
    GatePredicate,
    GateResolution,
    RunState,
    TeamAggregates,
    aggregate,
    predicate_from_condition,
    resolve_team_gates,
)
from hbuilder.domain.schema import CARD_TYPES, GENERATIONS
from hbuilder.domain.team import MEMBER_COUNT, Team

_STATS = Stats(performance=100, technique=100, sense=100)

# A fixed RunState fixture value for every test that doesn't itself
# exercise run-state resolution — composition-gate tests are provably
# independent of it.
_RUN_STATE = RunState(life=1000, combo=100)


def _skill(
    kind: str,
    family: str = "score_support",
    condition: GateCondition | None = None,
    target: object = None,
    override: object = None,
    rider: object = None,
) -> SkillSlot:
    # This builds a SkillSlot directly, bypassing card.py's JSON
    # null-to-UNKNOWN conversion — `condition=None` here is always the
    # structurally-ungated Python-level value (the same value
    # `{"kind": "ungated"}` on disk parses into), never JSON null/UNKNOWN.
    # `target` follows the identical contract — `target=None` is always the
    # structurally team-wide value, never JSON null/UNKNOWN. So do
    # `override`/`rider` — `override=None`/`rider=None` is always the
    # structurally "no second effect" value, never JSON null/UNKNOWN.
    return SkillSlot(
        kind=kind,
        family=family,
        effect_type="score",
        duration=None,
        interval=None,
        probability_tier=None,
        magnitude=10,
        condition=condition,
        target=target,
        override=override,
        rider=rider,
    )


def _card(
    card_id: str,
    *,
    generations: frozenset[str] = frozenset({"1期生"}),
    type_: str = "Cute",
    special: SkillSlot | None = None,
    active: SkillSlot | None = None,
    passive: SkillSlot | None = None,
    costume_skill: SkillSlot | None = None,
) -> Card:
    return Card(
        id=card_id,
        member=f"Member {card_id}",
        generations=generations,
        type=type_,
        rarity="SSR",
        costume_name=f"Costume {card_id}",
        stats=_STATS,
        skills=(special, active, passive),
        costume_skill=costume_skill,
    )


# --- Dual-generation overlap ---------------------------------------------


def test_dual_generation_card_fires_two_generation_gates():
    cards = [
        _card("dual_01", generations=frozenset({"1期生", "ゲーマーズ"})),
        _card("c2", generations=frozenset({"1期生"})),
        _card("c3", generations=frozenset({"ゲーマーズ"})),
        _card("c4", generations=frozenset({"5期生"})),
        _card("c5", generations=frozenset({"5期生"})),
        _card("c6", generations=frozenset({"5期生"})),
    ]
    agg = aggregate(cards)

    kisei_res = GatePredicate(dimension="generation", value="1期生", threshold=2).resolve(agg, _RUN_STATE)
    gamers_res = GatePredicate(
        dimension="generation", value="ゲーマーズ", threshold=2
    ).resolve(agg, _RUN_STATE)

    assert kisei_res.fired
    assert kisei_res.actual_count == 2
    assert gamers_res.fired
    assert gamers_res.actual_count == 2


def test_one_card_satisfies_overlapping_type_and_generation_gates():
    cards = [
        _card("overlap_01", type_="Cute", generations=frozenset({"1期生"})),
        _card("c2", type_="Cute", generations=frozenset({"5期生"})),
        _card("c3", type_="Pure", generations=frozenset({"1期生"})),
        _card("c4", type_="Happy", generations=frozenset({"3期生"})),
        _card("c5", type_="Happy", generations=frozenset({"4期生"})),
        _card("c6", type_="Pure", generations=frozenset({"2期生"})),
    ]
    agg = aggregate(cards)

    type_res = GatePredicate(dimension="type", value="Cute", threshold=2).resolve(agg, _RUN_STATE)
    gen_res = GatePredicate(dimension="generation", value="1期生", threshold=2).resolve(agg, _RUN_STATE)

    # "overlap_01" is counted toward both: Cute (with c2) and 1期生 (with c3).
    assert type_res.fired
    assert type_res.actual_count == 2
    assert gen_res.fired
    assert gen_res.actual_count == 2


# --- Absent target and threshold boundary --------------------------------


def test_gate_target_absent_from_team_resolves_to_zero():
    cards = [_card(f"c{i}", type_="Cute", generations=frozenset({"1期生"})) for i in range(6)]
    agg = aggregate(cards)

    res = GatePredicate(dimension="type", value="Pure", threshold=1).resolve(agg, _RUN_STATE)

    assert res.fired is False
    assert res.actual_count == 0


def test_threshold_fires_at_exactly_two_and_not_at_one():
    cards = [
        _card("a", type_="Cute"),
        _card("b", type_="Cute"),
        _card("c", type_="Pure"),
        _card("d", type_="Pure"),
        _card("e", type_="Pure"),
        _card("f", type_="Happy"),
    ]
    agg = aggregate(cards)

    res_at_2 = GatePredicate(dimension="type", value="Cute", threshold=2).resolve(agg, _RUN_STATE)
    assert res_at_2.fired
    assert res_at_2.actual_count == 2

    res_at_3 = GatePredicate(dimension="type", value="Cute", threshold=3).resolve(agg, _RUN_STATE)
    assert not res_at_3.fired
    assert res_at_3.actual_count == 2

    res_happy_threshold_2 = GatePredicate(
        dimension="type", value="Happy", threshold=2
    ).resolve(agg, _RUN_STATE)
    assert not res_happy_threshold_2.fired
    assert res_happy_threshold_2.actual_count == 1


# --- Aggregate count invariants -------------------------------------------


def _sample_team() -> Team:
    leader = _card("leader_01", type_="Happy", generations=frozenset({"5期生"}))
    members = (
        _card("m1", type_="Cute", generations=frozenset({"1期生", "ゲーマーズ"})),
        _card("m2", type_="Cute", generations=frozenset({"1期生"})),
        _card("m3", type_="Pure", generations=frozenset({"ゲーマーズ"})),
        _card("m4", type_="Happy", generations=frozenset({"5期生"})),
        _card("m5", type_="Pure", generations=frozenset({"2期生"})),
    )
    return Team(leader=leader, members=members)


def test_type_counts_sum_to_the_five_members_never_six():
    """Gates count the 5 members. The leader is an outfit
    slot outside them and contributes nothing to these counts, so the total is
    MEMBER_COUNT even when the leader's own type differs."""
    team = _sample_team()
    agg = aggregate(team.members)
    assert sum(agg.type_counts.values()) == MEMBER_COUNT


def test_generation_counts_sum_equals_five_plus_multi_generation_members():
    team = _sample_team()
    agg = aggregate(team.members)
    multi_generation_cards = sum(1 for card in team.members if len(card.generations) > 1)
    assert sum(agg.generation_counts.values()) == MEMBER_COUNT + multi_generation_cards


# --- Member-order invariance ----------------------------------------------


def test_aggregates_are_invariant_under_member_permutation():
    leader = _card("leader_01", type_="Happy", generations=frozenset({"5期生"}))
    base_members = [
        _card("m1", type_="Cute", generations=frozenset({"1期生", "ゲーマーズ"})),
        _card("m2", type_="Cute", generations=frozenset({"1期生"})),
        _card("m3", type_="Pure", generations=frozenset({"ゲーマーズ"})),
        _card("m4", type_="Happy", generations=frozenset({"5期生"})),
        _card("m5", type_="Pure", generations=frozenset({"2期生"})),
    ]
    assert len(base_members) == MEMBER_COUNT

    def fired_gate_set(agg: TeamAggregates) -> frozenset[tuple[str, str]]:
        fired = set()
        for value in CARD_TYPES:
            if GatePredicate(dimension="type", value=value, threshold=2).resolve(agg, _RUN_STATE).fired:
                fired.add(("type", value))
        for value in GENERATIONS:
            if GatePredicate(
                dimension="generation", value=value, threshold=2
            ).resolve(agg, _RUN_STATE).fired:
                fired.add(("generation", value))
        return frozenset(fired)

    baseline_team = Team(leader=leader, members=tuple(base_members))
    baseline_agg = aggregate(baseline_team.members)
    baseline_fired = fired_gate_set(baseline_agg)

    permutations = list(itertools.permutations(base_members))
    assert len(permutations) == 120

    for perm in permutations:
        team = Team(leader=leader, members=perm)
        agg = aggregate(team.members)
        assert agg == baseline_agg
        assert fired_gate_set(agg) == baseline_fired


# --- resolve_team_gates ----------------------------------------------------


def test_resolve_team_gates_returns_one_resolved_skill_per_instance():
    condition = GateCondition(kind="type", target="Cute", count=2)
    gated_skill = _skill("ACTIVE", condition=condition)
    ungated_skill = _skill("PASSIVE")
    leader = _card("carrier_01", type_="Cute", active=gated_skill, passive=ungated_skill)
    # Two Cute among the FIVE members, which is what the gate now counts.
    members = (
        leader,
        _card("m1", type_="Cute"),
        *(_card(f"m{i}", type_="Pure") for i in range(2, MEMBER_COUNT)),
    )
    team = Team(leader=_card("outfit_01", type_="Pure"), members=members)

    resolved = resolve_team_gates(team, _RUN_STATE)
    assert len(resolved) == len(team.effective_skill_instances())

    gated = [r for r in resolved if r.slot_kind == "ACTIVE" and r.card_id == leader.id]
    assert len(gated) == 1
    assert isinstance(gated[0].gate, GateResolution)
    assert gated[0].gate.fired
    assert gated[0].gate.actual_count == 2  # leader + m1, both Cute

    ungated = [r for r in resolved if r.slot_kind == "PASSIVE" and r.card_id == leader.id]
    assert len(ungated) == 1
    assert ungated[0].gate is None


def test_predicate_from_condition_maps_kind_target_count():
    condition = GateCondition(kind="generation", target="5期生", count=3)
    predicate = predicate_from_condition(condition)
    assert predicate.dimension == "generation"
    assert predicate.value == "5期生"
    assert predicate.threshold == 3


# --- Run-state gate resolution ------------------------------------------------


def test_run_state_gated_skill_fires_when_the_value_meets_its_threshold():
    condition = GateCondition(kind="run_state", target="combo", count=100)
    gated_skill = _skill("ACTIVE", condition=condition)
    # The carrier must be a MEMBER: a leader's own ACTIVE slot is never
    # emitted, so a skill parked there would resolve to
    # nothing and this test would pass for the wrong reason.
    leader = _card("carrier_01", active=gated_skill)
    members = (leader, *(_card(f"m{i}") for i in range(2, MEMBER_COUNT + 1)))
    team = Team(leader=_card("outfit_01"), members=members)

    on_state = RunState(life=1000, combo=100)
    resolved = resolve_team_gates(team, on_state)
    gated = next(r for r in resolved if r.card_id == leader.id and r.slot_kind == "ACTIVE")

    assert gated.gate.fired
    # The real run-state value, not a 1/0 coercion. Boundary case: the
    # threshold is inclusive, so combo == 100 satisfies `combo >= 100`.
    assert gated.gate.actual_count == on_state.combo


def test_run_state_gated_skill_does_not_fire_when_the_value_is_below_its_threshold():
    condition = GateCondition(kind="run_state", target="combo", count=100)
    gated_skill = _skill("ACTIVE", condition=condition)
    # The carrier must be a MEMBER: a leader's own ACTIVE slot is never
    # emitted, so a skill parked there would resolve to
    # nothing and this test would pass for the wrong reason.
    leader = _card("carrier_01", active=gated_skill)
    members = (leader, *(_card(f"m{i}") for i in range(2, MEMBER_COUNT + 1)))
    team = Team(leader=_card("outfit_01"), members=members)

    off_state = RunState(life=0, combo=0)
    resolved = resolve_team_gates(team, off_state)
    gated = next(r for r in resolved if r.card_id == leader.id and r.slot_kind == "ACTIVE")

    assert not gated.gate.fired
    assert gated.gate.actual_count == 0


def test_run_state_gate_resolution_is_independent_of_team_composition():
    condition = GateCondition(kind="run_state", target="life", count=600)
    gated_skill = _skill("PASSIVE", condition=condition)
    run_state = RunState(life=1000, combo=0)

    # The carrier is a MEMBER in both teams (a leader's PASSIVE is not
    # emitted). The point of the test: two teams whose
    # type/generation composition could not differ more must still resolve
    # this run-state gate identically, because run state is team-independent.
    leader_a = _card("carrier_a", type_="Cute", generations=frozenset({"1期生"}), passive=gated_skill)
    team_a = Team(
        leader=_card("outfit_a"),
        members=(
            leader_a,
            *(
                _card(f"a{i}", type_="Cute", generations=frozenset({"1期生"}))
                for i in range(2, MEMBER_COUNT + 1)
            ),
        ),
    )

    leader_b = _card("carrier_b", type_="Happy", generations=frozenset({"5期生"}), passive=gated_skill)
    team_b = Team(
        leader=_card("outfit_b"),
        members=(
            leader_b,
            *(
                _card(f"b{i}", type_="Pure" if i % 2 else "Happy", generations=frozenset({"holoX"}))
                for i in range(2, MEMBER_COUNT + 1)
            ),
        ),
    )

    resolved_a = resolve_team_gates(team_a, run_state)
    resolved_b = resolve_team_gates(team_b, run_state)

    gate_a = next(r for r in resolved_a if r.card_id == "carrier_a").gate
    gate_b = next(r for r in resolved_b if r.card_id == "carrier_b").gate

    assert gate_a.fired and gate_b.fired
    # The run-state value itself, identical for both teams — that is the
    # point of the test: a run_state gate reads the play-through snapshot,
    # never the assembled team. `actual_count` reports the real run-state
    # value (not a 1/0 coercion) so the breakdown can show what the
    # threshold was compared against.
    assert gate_a.actual_count == gate_b.actual_count == run_state.life


def test_activation_rate_up_active_skill_gated_on_combo_resolves_fired_under_default_run_state():
    condition = GateCondition(kind="run_state", target="combo", count=100)
    rate_up_skill = _skill("ACTIVE", family="activation_rate_up", condition=condition)
    leader = _card("carrier_01", active=rate_up_skill)
    members = (leader, *(_card(f"m{i}") for i in range(2, MEMBER_COUNT + 1)))
    team = Team(leader=_card("outfit_01"), members=members)

    default_run_state = RunState(life=1000, combo=100)
    resolved = resolve_team_gates(team, default_run_state)
    gated = next(r for r in resolved if r.card_id == leader.id and r.slot_kind == "ACTIVE")

    assert gated.family == "activation_rate_up"
    assert gated.gate.fired


# --- Structural: resolve takes TeamAggregates and RunState only, no Card --


def test_resolve_takes_aggregates_and_run_state_only_never_a_card():
    sig = inspect.signature(GatePredicate.resolve)
    params = [p for name, p in sig.parameters.items() if name != "self"]
    assert len(params) == 2

    hints = typing.get_type_hints(GatePredicate.resolve)
    assert hints[params[0].name] is TeamAggregates
    assert hints[params[1].name] is RunState
    assert Card not in hints.values()
