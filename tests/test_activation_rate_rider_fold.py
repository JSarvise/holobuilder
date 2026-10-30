"""The `activation_rate_up` RIDER on a SPECIAL slot folds into `team_rate_up`.

`_fold_activation_rate_up` reads the slot's PRINCIPAL family, but all 31 real
`activation_rate_up` instances live on `SkillSlot.rider`; without the rider fold
`team_rate_up` would be 0.0 for every team and a rider-carrying card would rank
identically to a rider-less one. These pin the fold's recorded shape, its own
LIFE>=1000 gate, the taint discipline, and that it reaches `team_rate_up` end to
end through `resolve_team_gates`.
"""
from __future__ import annotations

import pytest

from hbuilder.domain.card import GateCondition, SlotRider
from hbuilder.domain.gates import RunState, resolve_team_gates
from hbuilder.domain.scoring import (
    REFERENCE_RUN_SECONDS,
    _fold_rider_activation_rate_up,
)
from hbuilder.domain.unknown import UNKNOWN

from conftest import make_card, make_resolved, make_scoring_config, make_skill, make_team

_RIDER_MAGNITUDE = 55.0  # +55% activation rate, as on card-00002-5-uniq-0001-00
_HOST_DURATION = 10.0
_LIFE_GTE_1000 = GateCondition(kind="run_state", target="life", count=1000)


def _rate_rider(condition):
    return SlotRider(
        family="activation_rate_up",
        effect_type="activation_rate",
        magnitude=_RIDER_MAGNITUDE,
        condition=condition,
        target=None,
    )


def _special_with_rider(*, condition, magnitude=_RIDER_MAGNITUDE, duration=_HOST_DURATION):
    return make_skill(
        "SPECIAL",
        family="score_support",
        effect_type="score",
        duration=duration,
        magnitude=145.0,
        probability_tier=None,
        rider=SlotRider(
            family="activation_rate_up",
            effect_type="activation_rate",
            magnitude=magnitude,
            condition=condition,
            target=None,
        ),
    )


def _expected(cfg, *, magnitude=_RIDER_MAGNITUDE, duration=_HOST_DURATION):
    # _special_occupancy's uptime term with the rider's magnitude.
    return cfg.expected_fires_per_run * (duration / REFERENCE_RUN_SECONDS) * (magnitude / 100.0)


def test_ungated_rider_folds_to_the_recorded_shape_and_is_nonzero():
    cfg = make_scoring_config()
    resolved = make_resolved("m1", "SPECIAL", _special_with_rider(condition=None))
    value = _fold_rider_activation_rate_up((resolved,), cfg)
    assert value == pytest.approx(_expected(cfg))
    # The headline: without the rider fold this is 0.0 for every team.
    assert value > 0.0


def test_unknown_rider_magnitude_or_host_duration_taints_to_zero():
    cfg = make_scoring_config()
    bad_mag = make_resolved("m1", "SPECIAL", _special_with_rider(condition=None, magnitude=UNKNOWN))
    bad_dur = make_resolved("m1", "SPECIAL", _special_with_rider(condition=None, duration=UNKNOWN))
    assert _fold_rider_activation_rate_up((bad_mag,), cfg) == 0.0
    assert _fold_rider_activation_rate_up((bad_dur,), cfg) == 0.0


def test_a_non_activation_rate_rider_is_ignored():
    cfg = make_scoring_config()
    other = make_skill(
        "SPECIAL", family="score_support", effect_type="score", duration=_HOST_DURATION,
        rider=SlotRider(family="life_recovery", effect_type="life", magnitude=30.0,
                        condition=None, target=None),
    )
    resolved = make_resolved("m1", "SPECIAL", other)
    assert _fold_rider_activation_rate_up((resolved,), cfg) == 0.0


def _team_with_rider_carrier(condition):
    carrier = make_card("m1", member="Carrier", special=_special_with_rider(condition=condition))
    leader = make_card("L0", member="Leader")
    rest = [make_card(f"m{i}", member=name) for i, name in
            zip(range(2, 6), ("Mem2", "Mem3", "Mem4", "Mem5"))]
    return make_team([leader, carrier, *rest])


def test_life_gate_fires_at_full_life_end_to_end():
    """Through `resolve_team_gates`: the rider's own LIFE>=1000 gate is resolved,
    fires under the perfect-play run state, and the fold reaches `team_rate_up`."""
    cfg = make_scoring_config(run_state=RunState(life=1000, combo=0))
    team = _team_with_rider_carrier(_LIFE_GTE_1000)
    resolved = resolve_team_gates(team, cfg.run_state)
    assert _fold_rider_activation_rate_up(resolved, cfg) == pytest.approx(_expected(cfg))


def test_life_gate_does_not_fire_below_threshold():
    cfg = make_scoring_config(run_state=RunState(life=999, combo=0))
    team = _team_with_rider_carrier(_LIFE_GTE_1000)
    resolved = resolve_team_gates(team, cfg.run_state)
    assert _fold_rider_activation_rate_up(resolved, cfg) == 0.0


def test_a_rider_less_team_contributes_zero():
    cfg = make_scoring_config(run_state=RunState(life=1000, combo=0))
    plain = [make_card(f"c{i}", member=f"C{i}") for i in range(6)]
    resolved = resolve_team_gates(make_team(plain), cfg.run_state)
    assert _fold_rider_activation_rate_up(resolved, cfg) == 0.0
