"""Phase A/B board-ceiling cut (`domain/board_ceiling.py`).

The end-to-end soundness (bound never violated, top-K preserved) is measured on
4000 real teams by `bench/bench_board_ceiling.py`; these tests pin the two
things a unit test can pin deterministically: the bound is a true upper bound
and is monotone (phase B), and the ceiling actually reads the board's
leader-scope and activation-rate channels (phase A)."""

from __future__ import annotations

from hbuilder.domain import board
from hbuilder.domain.board_ceiling import (
    HolomemCeiling,
    holomem_ceilings,
    unit_score_upper_bound,
    top_n_candidates,
)

from conftest import make_card

K = 2.03734


def _zero_ceiling() -> HolomemCeiling:
    return HolomemCeiling(
        member_power=0.0, leader_scope=0.0, song_rate=0.0,
        effect_up=0.0, act_rate=0.0, cooldown=0.0,
    )


def test_upper_bound_reduces_to_the_no_board_score_when_ceilings_are_zero():
    # With no board to add, the bound is exactly OverallPower * (1+bonus) * K.
    ceilings = {"A": _zero_ceiling(), "B": _zero_ceiling()}
    ub = unit_score_upper_bound(
        overall_power_no_board=10_000.0,
        score_bonus_rate_no_board=500.0,
        member_holomems=("A",),
        leader_holomem="B",
        ceilings=ceilings,
        k_constant=K,
    )
    assert ub == 10_000.0 * (1.0 + 500.0 / 1000.0) * K


def test_upper_bound_is_an_upper_bound_and_is_monotone():
    base = unit_score_upper_bound(
        overall_power_no_board=10_000.0, score_bonus_rate_no_board=500.0,
        member_holomems=("A",), leader_holomem="B",
        ceilings={"A": _zero_ceiling(), "B": _zero_ceiling()}, k_constant=K,
    )
    # A member that brings board power and an active boost can only raise it.
    richer = HolomemCeiling(
        member_power=2_000.0, leader_scope=0.0, song_rate=30.0,
        effect_up=40.0, act_rate=0.2, cooldown=0.1,
    )
    higher = unit_score_upper_bound(
        overall_power_no_board=10_000.0, score_bonus_rate_no_board=500.0,
        member_holomems=("A",), leader_holomem="B",
        ceilings={"A": richer, "B": _zero_ceiling()}, k_constant=K,
    )
    assert higher > base
    # And leader-scope only counts once (the single leader), never per member.
    richer_leader = HolomemCeiling(
        member_power=0.0, leader_scope=5_000.0, song_rate=0.0,
        effect_up=0.0, act_rate=0.0, cooldown=0.0,
    )
    both_lead = unit_score_upper_bound(
        overall_power_no_board=10_000.0, score_bonus_rate_no_board=500.0,
        member_holomems=("A",), leader_holomem="B",
        ceilings={"A": richer_leader, "B": richer_leader}, k_constant=K,
    )
    # power gains exactly one 5000 leader_scope, not two.
    assert both_lead == (10_000.0 + 5_000.0) * (1.0 + 500.0 / 1000.0) * K


def test_top_n_candidates_picks_the_highest_bounds():
    bounds = [(0, 100.0), (1, 900.0), (2, 300.0), (3, 700.0), (4, 50.0)]
    assert set(top_n_candidates(bounds, 2)) == {1, 3}
    assert set(top_n_candidates(bounds, 3)) == {1, 3, 2}


_ACT = ("skill_tree_effect-live_active_skill_activation_probability_up_permil_up"
        "-60-skill_tree_effect_target-skill_tree_character-always")
_LEADER_STAT = ("skill_tree_effect-all_parameter_up-50-skill_tree_effect_target"
                "-all-set_live_leader")


def test_ceiling_reads_the_leader_scope_and_activation_rate_channels():
    model = board.board_from_mapping(
        {
            "characters": {
                "Hero": [
                    {"node": "B-001", "effects": [_LEADER_STAT], "connect_multiplier": 2.0},
                    {"node": "B-002", "effects": [_ACT], "connect_multiplier": 1.5},
                ]
            }
        }
    )
    hero = make_card("card-hero-5-uniq-0000-00", member="Hero")
    ceilings = holomem_ceilings(model, {"Hero": hero}, max_team_stat_sum=100_000.0)
    c = ceilings["Hero"]
    # leader-scope stat node -> a positive leader_scope, scaled by connect (2.0).
    assert c.leader_scope > 0.0
    # activation-rate node -> a positive act_rate, scaled by connect (1.5): 60*1.5 permil.
    assert c.act_rate > 0.0
    assert abs(c.act_rate - 0.060 * 1.5) < 1e-9
