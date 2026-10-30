"""Coverage of both `StackingModel` implementations,
both `ActivationRateModel` implementations, the two name-to-instance
registries, and the four-combination matrix that proves switching either
unverified interpretation actually changes a team's total.

Test names state the exact behavior or defect each one pins, following
`tests/test_gates.py`'s convention. Card/skill/team fixtures come from
`tests/conftest.py`.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

import hbuilder.domain.scoring as scoring_module
from hbuilder.domain.scoring import score_fast
from hbuilder.domain.scoring_models import (
    ACTIVATION_RATE_MODELS,
    STACKING_MODELS,
    AdditivePercentagePoints,
    AdditiveStacking,
    MultiplicativeRateUp,
    MultiplicativeStacking,
)

from conftest import make_card, make_scoring_config, make_skill, make_team

# --- Stacking model: combine ------------------------------------------------


def test_additive_stacking_combine_of_empty_list_is_exactly_zero():
    assert AdditiveStacking().combine([]) == 0.0


def test_multiplicative_stacking_combine_of_empty_list_is_exactly_zero():
    assert MultiplicativeStacking().combine([]) == 0.0


def test_additive_stacking_combine_of_single_rate_returns_it_unchanged():
    assert AdditiveStacking().combine([0.10]) == 0.10


def test_multiplicative_stacking_combine_of_single_rate_returns_it_approx():
    assert MultiplicativeStacking().combine([0.10]) == pytest.approx(0.10)


def test_additive_stacking_combine_of_pair_is_zero_point_two_four():
    assert AdditiveStacking().combine([0.10, 0.14]) == pytest.approx(0.24)


def test_multiplicative_stacking_combine_of_pair_is_zero_point_two_five_four():
    assert MultiplicativeStacking().combine([0.10, 0.14]) == pytest.approx(0.254)


def test_neither_combine_returns_a_multiplier_adding_one_reproduces_d15():
    # The worked numbers: 1.24 additive, 1.254 multiplicative. Both
    # `combine` implementations return the *combined rate*, not the
    # multiplier — the caller adds 1.0.
    assert 1.0 + AdditiveStacking().combine([0.10, 0.14]) == pytest.approx(1.24)
    assert 1.0 + MultiplicativeStacking().combine([0.10, 0.14]) == pytest.approx(1.254)


# --- Activation-rate model: apply -------------------------------------------


def test_additive_percentage_points_apply_of_zero_rate_up_returns_base_unchanged():
    assert AdditivePercentagePoints().apply(0.55, 0.0) == 0.55


def test_multiplicative_rate_up_apply_of_zero_rate_up_returns_base_unchanged():
    assert MultiplicativeRateUp().apply(0.55, 0.0) == 0.55


def test_additive_percentage_points_apply_of_fifteen_pp_is_seventy_percent():
    assert AdditivePercentagePoints().apply(0.55, 0.15) == pytest.approx(0.70)


def test_multiplicative_rate_up_apply_of_fifteen_percent_is_sixty_three_point_two_five():
    assert MultiplicativeRateUp().apply(0.55, 0.15) == pytest.approx(0.6325)


def test_both_apply_implementations_clamp_at_exactly_one():
    assert AdditivePercentagePoints().apply(0.75, 0.55) == 1.0
    assert MultiplicativeRateUp().apply(0.75, 0.55) == 1.0


# --- Registries --------------------------------------------------------------


def test_stacking_models_registry_maps_additive_and_multiplicative_by_name():
    assert set(STACKING_MODELS) == {"additive", "multiplicative"}
    assert all(key == model.name for key, model in STACKING_MODELS.items())


def test_activation_rate_models_registry_maps_pp_and_multiplicative_by_name():
    assert set(ACTIVATION_RATE_MODELS) == {"pp", "multiplicative"}
    assert all(key == model.name for key, model in ACTIVATION_RATE_MODELS.items())


def test_multiplicative_rate_up_never_divides_by_one_hundred():
    source = Path(scoring_module.__file__).parent.joinpath(
        "scoring_models.py"
    ).read_text(encoding="utf-8")
    assert not re.search(r"/\s*100", source)


def test_scoring_module_never_compares_a_model_name_string_for_equality():
    source = Path(scoring_module.__file__).read_text(encoding="utf-8")
    assert not re.search(r"==\s*['\"](additive|multiplicative|pp)['\"]", source)


# --- Four-combination matrix ------------------------------------------------
#
# One identical team, scored four times through `score_fast` with the four
# pairings of stacking model x activation-rate model. Built so that:
#   - `team_rate_up` (folded from member1's activation_rate_up skill) is
#     identical across all four combinations: `apply(base_prob, 0.0)` is
#     exactly `base_prob` under both activation-rate models by construction,
#     so the fold itself never depends on which model is selected.
#   - The leader's ACTIVE `score_support` skill's own probability *does*
#     depend on which activation-rate model receives `team_rate_up`.
#   - Two PASSIVE `score_support` skills contribute the exact worked pair
#     (0.10, 0.14), so the stacking model's own choice is independently
#     exercised on top of the activation-rate choice.


def _four_combo_team():
    """The ACTIVE score skill must be on a MEMBER. Parked on the leader it is
    not emitted at all, and with no ACTIVE in the score
    pool the activation-rate axis has nothing to act on — the four model
    combinations would collapse to two and the test would pass while pinning
    half of what it claims to."""
    active_scorer = make_card(
        "member_active_01",
        active=make_skill(
            "ACTIVE",
            # An ACTIVE that grants score is `conditional_score_up` — every
            # real ACTIVE slot is, and none is `score_support`. Using
            # `score_support` here would route the skill into the support
            # pool, leaving the score pool empty and collapsing all four model
            # combinations to the bare stat sum.
            family="conditional_score_up",
            effect_type="score",
            duration=10.0,
            interval=10.0,
            probability_tier="Medium",
            magnitude=20.0,
        ),
    )
    rate_up_member = make_card(
        "member_rate_up_01",
        active=make_skill(
            "ACTIVE",
            family="activation_rate_up",
            effect_type="activation_rate",
            duration=10.0,
            interval=10.0,
            probability_tier="High",
            magnitude=50.0,
        ),
    )
    passive_member_a = make_card(
        "member_passive_a",
        passive=make_skill(
            "PASSIVE", family="score_support", effect_type="score", magnitude=10.0
        ),
    )
    passive_member_b = make_card(
        "member_passive_b",
        passive=make_skill(
            "PASSIVE", family="score_support", effect_type="score", magnitude=14.0
        ),
    )
    plain_member_a = make_card("member_plain_a")
    outfit = make_card("outfit_01")
    # Five members carrying every axis under test; the leader slot takes a
    # plain card, since these tests are about stacking and activation rate,
    # not about the outfit.
    return make_team(
        [
            outfit,
            active_scorer,
            rate_up_member,
            passive_member_a,
            passive_member_b,
            plain_member_a,
        ]
    )


def _four_combo_team_with_stat_pool():
    """`_four_combo_team` with the two plain members replaced by `stat_up`
    PASSIVEs, so the STAT pool has two rates for `stacking_model` to combine.
    The score pool collapses to one aggregate entry under the attested
    max rule, so the stat pool is now the only place the stacking axis is
    observable."""
    base = _four_combo_team()
    stat_a = make_card(
        "member_stat_a",
        passive=make_skill(
            "PASSIVE", family="stat_up", effect_type="performance", magnitude=10.0
        ),
    )
    stat_b = make_card(
        "member_stat_b",
        passive=make_skill(
            "PASSIVE", family="stat_up", effect_type="performance", magnitude=14.0
        ),
    )
    return make_team([base.leader, *base.members[:3], stat_a, stat_b])


def _cfg_for(stacking_key: str, activation_rate_key: str):
    cfg = make_scoring_config()
    return dataclasses.replace(
        cfg,
        stacking_model=STACKING_MODELS[stacking_key],
        activation_rate_model=ACTIVATION_RATE_MODELS[activation_rate_key],
    )


# Base stat sum: the FIVE MEMBERS at the conftest STATS baseline (300 each) =
# 1500.0. The leader slot contributes no stats.
_BASE_STAT_SUM = 1500.0
# Both ACTIVEs on this team are 10 s effects on a 10 s cooldown, so both carry
# the same uptime — and that uptime is the finite activation lattice over the
# 128.8 s reference run (`scoring._finite_window_uptime`), not the steady-state
# `min(1, duration / interval) = 1.0`. The first check
# sits one whole cooldown in, at t = 10, and from there the 10 s windows abut
# and tile the rest of the run, so the union is the run minus its dead zone:
#
#     (128.8 - 10) / 128.8 = 118.8/128.8 = 0.922360248447205
_ACTIVE_UPTIME = (128.8 - 10.0) / 128.8
# team_rate_up: uptime * apply(base_prob=0.55, 0.0) * _as_rate(50.0) =
# 0.922360248... * 0.275 = 0.253649068...  apply(x, 0.0) == x under both models
# by construction, so this is a fixed constant across all four
# combinations (steady state would give 0.275).
_TEAM_RATE_UP = _ACTIVE_UPTIME * 0.55 * 0.50


def _leader_ev(activation_rate_key: str) -> float:
    base_prob = 0.46  # DEFAULT_CONFIG's probability_tiers["Medium"]
    prob = ACTIVATION_RATE_MODELS[activation_rate_key].apply(base_prob, _TEAM_RATE_UP)
    return _ACTIVE_UPTIME * prob * 0.20  # finite-window uptime, _as_rate(20.0)


def _expected_total(stacking_key: str, activation_rate_key: str) -> float:
    """The two `score_support` PASSIVEs (10 % and 14 %) become the game's `B`
    term, and `B` is a max-of-two-maxima rather than a stack
    — both sit in the ACTIVE bucket at
    occupancy 1.0, so `B = max(0.10, 0.14) = 0.14`, NOT 0.24 additive or
    0.254 multiplicative. `STACKING_MODELS` does not touch the support at
    all; it only combines the finished score pool, which for this team has
    exactly one entry."""
    support = max(0.10, 0.14)
    active = _leader_ev(activation_rate_key) * (1.0 + support)
    combined = STACKING_MODELS[stacking_key].combine([active])
    return _BASE_STAT_SUM * (1.0 + combined)


# The two totals the four combinations collapse onto, worked out by hand from
# `_ACTIVE_UPTIME` above (u = 118.8/128.8 = 0.922360248...) and B = max(0.10,
# 0.14) = 0.14.
#
#   pp   : prob   = 0.46 + team_rate_up = 0.46 + 0.253649068 = 0.713649068...
#          active = u * 0.713649068... * 0.20     = 0.131648304...
#          ratio  = active * 1.14                = 0.150079069...
#          total  = 1500 * 1.150079069...        = 1725.1186039...
#   mult : prob   = 0.46 * (1 + 0.253649068...)  = 0.576678571...
#          active = u * 0.576678571... * 0.20    = 0.106381077...
#          ratio  = active * 1.14                = 0.121274429...
#          total  = 1500 * 1.121274429...        = 1681.9116435...
#
# The steady-state pair, kept as `!=` guards below so a revert to
# `min(1, duration / interval)` fails here rather than quietly re-inflating
# every ACTIVE: 1751.37 (pp) and 1700.583 (multiplicative).
_PP_TOTAL = 1725.1186039
_MULTIPLICATIVE_TOTAL = 1681.9116435
_STEADY_STATE_PP_TOTAL = 1751.37
_STEADY_STATE_MULTIPLICATIVE_TOTAL = 1700.583


def test_four_combo_additive_stacking_with_pp_activation_rate():
    cfg = _cfg_for("additive", "pp")
    total = score_fast(_four_combo_team(), cfg)
    assert total == pytest.approx(_expected_total("additive", "pp"))
    assert total == pytest.approx(_PP_TOTAL)
    assert total != pytest.approx(_STEADY_STATE_PP_TOTAL)


def test_four_combo_additive_stacking_with_multiplicative_activation_rate():
    cfg = _cfg_for("additive", "multiplicative")
    total = score_fast(_four_combo_team(), cfg)
    assert total == pytest.approx(_expected_total("additive", "multiplicative"))
    assert total == pytest.approx(_MULTIPLICATIVE_TOTAL)
    assert total != pytest.approx(_STEADY_STATE_MULTIPLICATIVE_TOTAL)


def test_four_combo_multiplicative_stacking_with_pp_activation_rate():
    cfg = _cfg_for("multiplicative", "pp")
    total = score_fast(_four_combo_team(), cfg)
    assert total == pytest.approx(_expected_total("multiplicative", "pp"))
    assert total == pytest.approx(_PP_TOTAL)
    assert total != pytest.approx(_STEADY_STATE_PP_TOTAL)


def test_four_combo_multiplicative_stacking_with_multiplicative_activation_rate():
    cfg = _cfg_for("multiplicative", "multiplicative")
    total = score_fast(_four_combo_team(), cfg)
    assert total == pytest.approx(_expected_total("multiplicative", "multiplicative"))
    assert total == pytest.approx(_MULTIPLICATIVE_TOTAL)
    assert total != pytest.approx(_STEADY_STATE_MULTIPLICATIVE_TOTAL)


def _totals_over_four_combinations(team) -> dict:
    return {
        (stacking_key, activation_rate_key): score_fast(
            team, _cfg_for(stacking_key, activation_rate_key)
        )
        for stacking_key in ("additive", "multiplicative")
        for activation_rate_key in ("pp", "multiplicative")
    }


def test_stacking_is_inert_on_a_team_whose_score_pool_has_one_entry():
    """A consequence of the score pool becoming ONE aggregate rate: every
    Score UP collapses into `SkillScoreUpRatio` and Score Support combines by
    its own max rule, so `combine()` is called over a single-element list and
    both stacking models return that element unchanged. On this team the
    stacking axis therefore does nothing and only the activation-rate axis
    varies the total — two distinct values, not four.

    This is a narrowing of `model.stacking`'s scope, not a loss of the knob:
    it still governs the STAT pool, where several rates really do meet. Pinned
    so the narrowing is a recorded fact rather than something a reader has to
    rediscover from four equal numbers."""
    totals = _totals_over_four_combinations(_four_combo_team())

    assert len(set(totals.values())) == 2, totals
    assert totals[("additive", "pp")] == totals[("multiplicative", "pp")]


def test_four_combinations_produce_four_distinct_totals_once_the_stat_pool_is_exercised():
    """The original guarantee — all four combinations live and distinct —
    restated on a team that actually exercises both axes. Two `stat_up`
    PASSIVEs give the stat pool two rates to combine, which is where the
    stacking model still bites."""
    team = _four_combo_team_with_stat_pool()
    totals = _totals_over_four_combinations(team)

    assert len(set(totals.values())) == 4, totals
