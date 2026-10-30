"""EV coverage: the four `EV_FUNCTIONS` entry guards, the ACTIVE
uptime clamp, the UNKNOWN-skip-and-taint path, effect-type/family-weight
routing, and
fast-path invariants (member-order independence, activation-rate-up
folding). Test names state the exact defect or contract each one pins,
following `tests/test_gates.py`'s convention.

Card/skill/team fixtures come from `tests/conftest.py`, extended with
non-`None` `duration`/`interval`/`probability_tier`/`magnitude` values that
`tests/test_gates.py`'s own builders deliberately omit.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

import pytest

from hbuilder.domain.card import EffectTarget, GateCondition, SlotOverride, Stats
from hbuilder.domain.gates import GatePredicate, GateResolution, resolve_team_gates
from hbuilder.domain.loader import load_cards
from hbuilder.domain.schema import COSTUME_SLOT_KIND
from hbuilder.domain.scoring import (
    CONTRIBUTION_STATES,
    EV_FUNCTIONS,
    ScoringError,
    _effective_magnitude,
    _fold_activation_rate_up,
    _resolve_skill,
    _score_active,
    _score_passive,
    _score_special,
    score_fast,
    score_with_breakdown,
)
from hbuilder.domain.scoring_config import REFERENCE_RUN_SECONDS, load_scoring_config
from hbuilder.domain.team import build_team
from hbuilder.domain.unknown import UNKNOWN

import hbuilder.domain.scoring as scoring_module

from conftest import (
    DEFAULT_RUN_STATE,
    FIXTURE_CARDS_PATH,
    make_card,
    make_resolved,
    make_scoring_config,
    make_skill,
    make_team,
)

_CERTAIN_TIERS = {"Low": 1.0, "Medium": 1.0, "High": 1.0}


# --- Entry guards: misrouting a slot kind raises ScoringError ------------


def test_special_shaped_slot_handed_to_active_evaluator_raises_scoring_error_naming_card_and_kinds():
    special_skill = make_skill("SPECIAL", duration=9.0, magnitude=22.0)
    resolved = make_resolved("card_misrouted_01", "SPECIAL", special_skill)
    cfg = make_scoring_config()

    with pytest.raises(ScoringError) as exc_info:
        _score_active(resolved, cfg, 0.0)

    message = str(exc_info.value)
    assert "card_misrouted_01" in message
    assert "ACTIVE" in message
    assert "SPECIAL" in message


def test_active_shaped_slot_handed_to_special_evaluator_raises_scoring_error_naming_card_and_kinds():
    active_skill = make_skill(
        "ACTIVE", duration=10.0, interval=20.0, probability_tier="Medium", magnitude=25.0
    )
    resolved = make_resolved("card_misrouted_02", "ACTIVE", active_skill)
    cfg = make_scoring_config()

    with pytest.raises(ScoringError) as exc_info:
        _score_special(resolved, cfg, 0.0)

    message = str(exc_info.value)
    assert "card_misrouted_02" in message
    assert "SPECIAL" in message
    assert "ACTIVE" in message


def test_passive_shaped_slot_handed_to_active_evaluator_raises_scoring_error_naming_card_and_kinds():
    passive_skill = make_skill("PASSIVE", magnitude=8.0)
    resolved = make_resolved("card_misrouted_03", "PASSIVE", passive_skill)
    cfg = make_scoring_config()

    with pytest.raises(ScoringError) as exc_info:
        _score_active(resolved, cfg, 0.0)

    message = str(exc_info.value)
    assert "card_misrouted_03" in message
    assert "ACTIVE" in message
    assert "PASSIVE" in message


def test_ev_functions_covers_exactly_the_four_slot_kinds_with_no_extras():
    assert set(EV_FUNCTIONS) == {"SPECIAL", "ACTIVE", "PASSIVE", COSTUME_SLOT_KIND}
    assert len(EV_FUNCTIONS) == 4


# --- FAMILY_REQUIRES_SLOT_KIND defence in depth ----------------------------


def test_non_active_activation_rate_up_slot_is_rejected_by_the_schema_constraint_not_by_the_scoring_guard():
    from hbuilder.domain.schema import validate_skill_slot

    record = {
        "family": "activation_rate_up",
        "effect_type": "activation_rate",
        "duration": None,
        "interval": None,
        "probability_tier": None,
        "magnitude": 15.0,
        "condition": None,
        "target": {"kind": "all"},
        "override": {"kind": "none"},
        "rider": {"kind": "none"},
    }
    errors = validate_skill_slot(record, "card_rateup_defense_01", "PASSIVE")
    assert any(
        e.startswith("family") and "activation_rate_up" in e and "ACTIVE" in e
        for e in errors
    )


def test_fold_activation_rate_up_raises_scoring_error_for_a_non_active_slot_that_bypassed_load_time_validation():
    skill = make_skill(
        "PASSIVE", family="activation_rate_up", effect_type="activation_rate", magnitude=15.0
    )
    resolved = make_resolved("card_rateup_defense_02", "PASSIVE", skill)
    cfg = make_scoring_config()

    with pytest.raises(ScoringError) as exc_info:
        _fold_activation_rate_up((resolved,), cfg)

    message = str(exc_info.value)
    assert "card_rateup_defense_02" in message
    assert "PASSIVE" in message


def test_fold_activation_rate_up_raises_scoring_error_even_when_the_gate_did_not_fire():
    skill = make_skill(
        "PASSIVE", family="activation_rate_up", effect_type="activation_rate", magnitude=15.0
    )
    gate = GateResolution(
        predicate=GatePredicate(dimension="type", value="Cute", threshold=3),
        fired=False,
        actual_count=0,
    )
    resolved = make_resolved("card_rateup_defense_03", "PASSIVE", skill, gate=gate)
    cfg = make_scoring_config()

    with pytest.raises(ScoringError) as exc_info:
        _fold_activation_rate_up((resolved,), cfg)

    message = str(exc_info.value)
    assert "card_rateup_defense_03" in message
    assert "PASSIVE" in message


# --- A rider is represented, never scored -----------------------------------


def test_a_specials_activation_rate_up_rider_contributes_exactly_zero_to_team_rate_up():
    """'Represented, not scored' is proved,
    not assumed -- a team's team_rate_up (and total_score) must be
    bit-identical whether or not its SPECIAL slot carries an
    activation_rate_up rider. `_fold_activation_rate_up` reads
    `resolved.family` (the principal) only, so the rider never enters that
    loop at all."""
    from hbuilder.domain.card import SlotRider

    rider = SlotRider(
        family="activation_rate_up",
        effect_type="activation_rate",
        magnitude=55.0,
        condition=GateCondition(kind="run_state", target="life", count=1000),
        target=None,
    )
    special_with_rider = make_skill("SPECIAL", duration=10.0, magnitude=145.0, rider=rider)
    special_without_rider = make_skill("SPECIAL", duration=10.0, magnitude=145.0, rider=None)
    cfg = make_scoring_config()

    def _team(special_skill):
        return make_team(
            [
                make_card("rider_leader", special=special_skill),
                make_card("rider_m1"),
                make_card("rider_m2"),
                make_card("rider_m3"),
                make_card("rider_m4"),
                make_card("rider_m5"),
            ]
        )

    breakdown_with_rider = score_with_breakdown(_team(special_with_rider), cfg)
    breakdown_without_rider = score_with_breakdown(_team(special_without_rider), cfg)

    assert breakdown_with_rider.team_rate_up == 0.0
    assert breakdown_with_rider.team_rate_up == breakdown_without_rider.team_rate_up
    assert breakdown_with_rider.total_score == breakdown_without_rider.total_score


# --- SPECIAL: fires-per-run * duration / REFERENCE_RUN_SECONDS -----------


def test_special_ev_equals_expected_fires_times_duration_over_reference_run_seconds_times_magnitude_rate():
    skill = make_skill("SPECIAL", duration=9.0, magnitude=22.0)
    resolved = make_resolved("card_special_01", "SPECIAL", skill)
    cfg = make_scoring_config(expected_fires_per_run=1.0)

    ev = _score_special(resolved, cfg, 0.0)

    assert ev == pytest.approx(1.0 * (9.0 / REFERENCE_RUN_SECONDS) * 0.22)


def test_special_ev_differs_between_a_twelve_second_and_a_five_second_duration():
    cfg = make_scoring_config()
    twelve = _score_special(
        make_resolved("card_special_02", "SPECIAL", make_skill("SPECIAL", duration=12.0, magnitude=20.0)),
        cfg,
        0.0,
    )
    five = _score_special(
        make_resolved("card_special_03", "SPECIAL", make_skill("SPECIAL", duration=5.0, magnitude=20.0)),
        cfg,
        0.0,
    )

    assert twelve != pytest.approx(five)
    assert twelve > five


# --- PASSIVE: always on, gated only by its condition ----------------------


def test_passive_scores_full_magnitude_rate_when_ungated():
    skill = make_skill("PASSIVE", magnitude=8.0)
    resolved = make_resolved("card_passive_01", "PASSIVE", skill)
    cfg = make_scoring_config()

    rate, state, missing_field = _resolve_skill(resolved, cfg, 0.0)

    assert rate == pytest.approx(0.08)
    assert state == "applied"
    assert missing_field is None


def test_passive_scores_zero_when_its_gate_does_not_fire():
    skill = make_skill("PASSIVE", magnitude=40.0)
    predicate = GatePredicate(dimension="type", value="Pure", threshold=2)
    gate = GateResolution(predicate=predicate, fired=False, actual_count=0)
    resolved = make_resolved("card_passive_02", "PASSIVE", skill, gate=gate)
    cfg = make_scoring_config()

    rate, state, missing_field = _resolve_skill(resolved, cfg, 0.0)

    assert rate == 0.0
    assert state == "gate_not_met"
    assert missing_field is None


# --- COSTUME: its own dispatch entry, same arithmetic as PASSIVE ----------


def test_costume_slot_dispatches_through_its_own_entry_and_matches_passive_evaluator_for_the_same_magnitude():
    passive_skill = make_skill("PASSIVE", magnitude=10.0)
    costume_skill = make_skill(COSTUME_SLOT_KIND, magnitude=10.0)
    passive_resolved = make_resolved("card_costume_01", "PASSIVE", passive_skill)
    costume_resolved = make_resolved(
        "card_costume_01", "costume", costume_skill, source="costume"
    )
    cfg = make_scoring_config()

    passive_ev = _score_passive(passive_resolved, cfg, 0.0)
    costume_ev = EV_FUNCTIONS[COSTUME_SLOT_KIND](costume_resolved, cfg, 0.0)

    assert costume_ev == pytest.approx(passive_ev)
    # A COSTUME slot must route through its own entry, never through
    # PASSIVE's own guard, which rejects a slot kind mismatch.
    with pytest.raises(ScoringError):
        _score_passive(costume_resolved, cfg, 0.0)


# --- ACTIVE uptime clamp / unit contract ------------------------------------


def test_active_ev_at_duration_equal_to_interval_and_probability_one_is_the_finite_window_uptime_times_the_magnitude_as_a_fraction_not_percentage_points():
    """`duration == interval` does NOT mean "up for the whole run" (the
    steady-state duty cycle `min(1, 20/20)` = 1.0 would read back the bare
    magnitude at probability 1). The activation checks sit at `k * 20` for `k = 1 .. 6`, with no
    check at t=0, so a run opens with one whole cooldown of dead zone and the
    effect only covers 20 s .. 128.8 s:

        uptime = (128.8 - 20) / 128.8 = 108.8 / 128.8 = 0.84472049689441
        ev     = 0.84472049689441 * 0.25 = 0.2111801242236025

    The unit contract is what this test is really for: divide the EV back by that uptime and the magnitude comes out as a
    fraction, 25.0 -> 0.25, never the raw percentage-point number and never a
    further /100."""
    skill = make_skill(
        "ACTIVE", duration=20.0, interval=20.0, probability_tier="Medium", magnitude=25.0
    )
    resolved = make_resolved("card_uptime_01", "ACTIVE", skill)
    cfg = make_scoring_config(probability_tiers=dict(_CERTAIN_TIERS))

    ev = _score_active(resolved, cfg, 0.0)

    assert ev == pytest.approx(0.2111801242236025)
    # The magnitude enters as a fraction (25.0 -> 0.25), never the raw
    # percentage-point number and never a further /100.
    assert ev / (108.8 / REFERENCE_RUN_SECONDS) == pytest.approx(0.25)
    assert ev != pytest.approx(25.0)
    assert ev != pytest.approx(0.0025)
    assert ev != pytest.approx(0.25)  # the steady-state duty-cycle answer


def test_active_ev_at_duration_above_interval_equals_ev_at_duration_equal_to_interval_the_uptime_clamp():
    """**The clamp is structural rather than a `min(1, ...)`**: the
    finite window unions overlapping activation windows instead of summing
    them, so a 30 s effect on a 20 s cooldown covers exactly the same
    20 s .. 128.8 s stretch a 20 s effect does — each check re-extends the
    live stretch by at most one interval, and the last check adds nothing it
    was not already covering.

        uptime = (128.8 - 20) / 128.8 = 108.8 / 128.8 = 0.84472049689441
        ev     = 0.84472049689441 * 0.25 = 0.2111801242236025

    The `min(1.0, live / 128.8)` still standing in `_finite_window_uptime` is
    unreachable arithmetic under the union — a guard, not what does this."""
    cfg = make_scoring_config(probability_tiers=dict(_CERTAIN_TIERS))

    def _ev_at(duration: float) -> float:
        skill = make_skill(
            "ACTIVE", duration=duration, interval=20.0, probability_tier="Medium", magnitude=25.0
        )
        resolved = make_resolved("card_uptime_02", "ACTIVE", skill)
        return _score_active(resolved, cfg, 0.0)

    # The re-proc-while-already-active state is not counted twice: a
    # duration well above its interval scores identically to duration
    # exactly at the interval.
    assert _ev_at(30.0) == pytest.approx(_ev_at(20.0))
    assert _ev_at(30.0) == pytest.approx(0.2111801242236025)
    assert _ev_at(30.0) != pytest.approx(0.25)  # the steady-state duty-cycle answer


def test_active_ev_at_duration_below_interval_is_proportional_only_while_the_last_window_fits_inside_the_run():
    """The EV does not simply "scale linearly with duration". Under a
    steady-state `duration / interval` it would be proportional to duration
    everywhere below the interval; under the finite lattice it is only
    *affine*.

    At `interval=20` the checks are at 20, 40, ... 120 — six of them, since
    `floor(128.8 / 20) = 6` and there is no check at t=0 — so below the
    interval the windows never touch and

        live(d) = 6d                     for d <= 8.8   (all six fit)
        live(d) = 5d + 8.8               for 8.8 < d < 20 (the sixth is cut
                                                           off by the end of
                                                           the run)

    Proportionality therefore survives inside the first branch and breaks in
    the second, and this test pins both halves rather than a blanket claim:

        d=10 -> live = 5*10 + 8.8 = 58.8 ; uptime = 58.8 / 128.8
                                                  = 0.45652173913043476
                ev = 0.45652173913043476 * 0.25 = 0.11413043478260869

    Doubling 5 s to 10 s does NOT double the EV: it falls short by exactly
    the 1.2 s the sixth window loses to the end of the run, `0.25 * 1.2 /
    128.8 = 0.002329192546583851`. A steady-state uptime (`10 / 20 = 0.5`)
    would give a flat 0.125."""
    cfg = make_scoring_config(probability_tiers=dict(_CERTAIN_TIERS))

    def _ev_at(duration: float) -> float:
        skill = make_skill(
            "ACTIVE", duration=duration, interval=20.0, probability_tier="Medium", magnitude=25.0
        )
        resolved = make_resolved("card_uptime_03", "ACTIVE", skill)
        return _score_active(resolved, cfg, 0.0)

    assert _ev_at(10.0) == pytest.approx(0.11413043478260869)
    assert _ev_at(10.0) != pytest.approx(0.125)  # the steady-state duty-cycle answer

    # Proportional while every one of the six windows fits inside the run
    # (4 s and 8 s both end before 128.8 s).
    assert _ev_at(8.0) == pytest.approx(2 * _ev_at(4.0))

    # And not proportional once the sixth window is truncated: short by
    # exactly the 1.2 s it loses off the end of the run.
    assert _ev_at(10.0) != pytest.approx(2 * _ev_at(5.0))
    assert _ev_at(10.0) == pytest.approx(
        2 * _ev_at(5.0) - 0.25 * (1.2 / REFERENCE_RUN_SECONDS)
    )


# --- UNKNOWN skip-and-taint path --------------------------------------------


@pytest.mark.parametrize("field_name", ["duration", "interval", "probability_tier", "magnitude"])
def test_active_skill_with_unknown_field_resolves_to_zero_and_data_missing_state_naming_the_field(
    field_name: str,
):
    kwargs = dict(duration=10.0, interval=10.0, probability_tier="Medium", magnitude=25.0)
    kwargs[field_name] = UNKNOWN
    skill = make_skill("ACTIVE", **kwargs)
    resolved = make_resolved(f"card_unknown_{field_name}", "ACTIVE", skill)
    cfg = make_scoring_config()

    rate, state, missing_field = _resolve_skill(resolved, cfg, 0.0)

    # UNKNOWN never reaches arithmetic (Unknown.__float__ would raise
    # SchemaError) — the field is caught by is_known() before dispatch.
    assert rate == 0.0
    assert state == "data_missing"
    assert missing_field == field_name


def test_known_zero_magnitude_is_applied_state_not_confused_with_missing_data():
    skill = make_skill(
        "ACTIVE", duration=10.0, interval=10.0, probability_tier="Medium", magnitude=0.0
    )
    resolved = make_resolved("card_known_zero", "ACTIVE", skill)
    cfg = make_scoring_config()

    rate, state, missing_field = _resolve_skill(resolved, cfg, 0.0)

    # A known zero is a real contribution of zero, not a data gap — the
    # two states must stay distinguishable.
    assert rate == 0.0
    assert state == "applied"
    assert missing_field is None
    assert set(CONTRIBUTION_STATES) == {"applied", "gate_not_met", "data_missing"}


# --- Team-level invariants (fast path) --------------------------------------


def test_team_of_six_skill_less_cards_scores_exactly_its_base_stat_sum():
    cards = [make_card(f"blank_{i}") for i in range(6)]
    team = make_team(cards)
    cfg = make_scoring_config()

    assert score_fast(team, cfg) == pytest.approx(float(team.base_stat_sum()))


def test_team_score_is_invariant_under_member_permutation():
    leader = make_card(
        "perm_leader",
        type_="Happy",
        generations=frozenset({"5期生"}),
        active=make_skill(
            "ACTIVE",
            duration=10.0,
            interval=20.0,
            probability_tier="Medium",
            magnitude=25.0,
            condition=GateCondition(kind="type", target="Cute", count=2),
        ),
    )
    base_members = [
        make_card("perm_m1", type_="Cute", generations=frozenset({"1期生"})),
        make_card("perm_m2", type_="Cute", generations=frozenset({"ゲーマーズ"})),
        make_card(
            "perm_m3",
            type_="Pure",
            generations=frozenset({"2期生"}),
            passive=make_skill(
                "PASSIVE", family="stat_up", effect_type="performance", magnitude=6.0
            ),
        ),
        make_card("perm_m4", type_="Happy", generations=frozenset({"3期生"})),
        make_card("perm_m5", type_="Pure", generations=frozenset({"4期生"})),
    ]
    cfg = make_scoring_config()

    baseline = score_fast(make_team([leader, *base_members]), cfg)

    for perm in itertools.permutations(base_members):
        team = make_team([leader, *perm])
        assert score_fast(team, cfg) == pytest.approx(baseline)


def test_two_activation_rate_up_skills_fold_to_the_same_team_rate_up_regardless_of_order():
    cfg = make_scoring_config(probability_tiers=dict(_CERTAIN_TIERS))
    rate_up_a = make_skill(
        "ACTIVE",
        family="activation_rate_up",
        effect_type="activation_rate",
        duration=10.0,
        interval=10.0,
        probability_tier="Medium",
        magnitude=10.0,
    )
    rate_up_b = make_skill(
        "ACTIVE",
        family="activation_rate_up",
        effect_type="activation_rate",
        duration=8.0,
        interval=16.0,
        probability_tier="Medium",
        magnitude=6.0,
    )
    leader = make_card("ratefold_leader", active=rate_up_a)
    member_with_rate_up = make_card("ratefold_member_b", active=rate_up_b)
    fillers = [make_card(f"ratefold_filler_{i}") for i in range(4)]

    team_first = make_team([leader, member_with_rate_up, *fillers])
    team_last = make_team([leader, *fillers[:2], member_with_rate_up, *fillers[2:]])

    resolved_first = resolve_team_gates(team_first, DEFAULT_RUN_STATE)
    resolved_last = resolve_team_gates(team_last, DEFAULT_RUN_STATE)

    rate_first = _fold_activation_rate_up(resolved_first, cfg)
    rate_last = _fold_activation_rate_up(resolved_last, cfg)

    assert rate_first == pytest.approx(rate_last)
    assert rate_first > 0.0


def test_life_recovery_passive_contributes_zero_at_default_weight_and_nonzero_when_weight_raised():
    life_skill = make_skill("PASSIVE", family="life_recovery", effect_type="life", magnitude=12.0)
    leader = make_card("life_leader", passive=life_skill)
    members = [make_card(f"life_member_{i}") for i in range(5)]
    team = make_team([*members, leader])

    cfg_default = make_scoring_config()
    assert cfg_default.family_weights["life_recovery"] == 0.0
    assert score_fast(team, cfg_default) == pytest.approx(float(team.base_stat_sum()))

    raised_weights = dict(cfg_default.family_weights)
    raised_weights["life_recovery"] = 1.0
    cfg_raised = make_scoring_config(family_weights=raised_weights)

    # The zero is an inspectable config assumption, not a structural rule
    # that drops life_recovery from the model.
    assert score_fast(team, cfg_raised) > score_fast(team, cfg_default)


# --- Structural: no rounding/Decimal/quantize anywhere in scoring ----------


def test_scoring_module_contains_no_rounding_decimal_or_quantize_construct():
    source = Path(scoring_module.__file__).read_text(encoding="utf-8")
    code_text = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )

    assert not re.search(r"\bround\(", code_text)
    assert not re.search(r"\bDecimal\b", code_text)
    assert not re.search(r"\bquantize\(", code_text)


# --- End-to-end sanity against the real shipped fixture --------------------


def test_shipped_fixture_team_with_mio_01_null_active_magnitude_scores_finite_positive_float():
    cards = load_cards(FIXTURE_CARDS_PATH)
    team = build_team(cards, "nene_01", ["fubuki_01", "mio_01", "matsuri_01", "polka_01", "lamy_01"])
    cfg = load_scoring_config()

    score = score_fast(team, cfg)

    assert score == score  # not NaN
    assert score > 0.0


# --- Targeted stat_up applies only to the N resolved target cards' --------
# --- named stat, never the whole team's three-stat sum -------------------


def _targeted_stat_up_cards():
    """Six cards built so the targeted subset (top 2 by performance among
    the three Cute cards) and the untargeted remainder hold DIFFERENT
    values of the targeted stat (performance) — a uniform conftest.STATS
    team passes under both a team-wide and a targeted formula and pins
    nothing. `target_member_low` is Cute (so it
    would qualify by type) but is NOT among the top 2 by performance, and
    `filler_pure` has the highest performance of all six cards but is
    excluded outright by type — both are what prove the scorer targets N
    *cards*, never "every card of the type" and never "every card"."""
    target = EffectTarget(kind="type", value="Cute", count=2)
    skill = make_skill(
        "PASSIVE", family="stat_up", effect_type="performance", magnitude=10.0, target=target
    )
    return [
        # First entry is the OUTFIT card (make_team's cards[0]): a Happy
        # filler, deliberately outside the five so it neither qualifies for
        # the Cute target nor adds stats.
        make_card(
            "filler_happy_2",
            type_="Happy",
            stats=Stats(performance=10, technique=100, sense=100),
        ),
        make_card(
            "target_leader",
            type_="Cute",
            stats=Stats(performance=300, technique=100, sense=100),
            passive=skill,
        ),
        make_card(
            "target_member_high",
            type_="Cute",
            stats=Stats(performance=250, technique=100, sense=100),
        ),
        make_card(
            "target_member_low",
            type_="Cute",
            stats=Stats(performance=50, technique=100, sense=100),
        ),
        make_card(
            "filler_pure",
            type_="Pure",
            stats=Stats(performance=999, technique=100, sense=100),
        ),
        make_card(
            "filler_happy_1",
            type_="Happy",
            stats=Stats(performance=10, technique=100, sense=100),
        ),
    ]


def test_targeted_stat_up_applies_only_to_the_named_stat_of_the_targeted_members():
    team = make_team(_targeted_stat_up_cards())
    cfg = make_scoring_config()

    # Hand-computed: base_stat_sum over the FIVE MEMBERS (2609.0) plus 10% of
    # ONLY the two
    # selected Cute cards' performance (300 and 250, the top two by
    # performance among the three Cute cards). target_member_low (Cute, 50)
    # is excluded by the target's count=2; filler_pure's performance (999)
    # is untouched despite being the highest of all six, because it is not
    # Cute — a target-blind formula would apply the rate to
    # base_stat_sum in full, touching every stat of every card regardless.
    base_stat_sum = (300 + 100 + 100) + (250 + 100 + 100) + (50 + 100 + 100) + (
        999 + 100 + 100
    ) + (10 + 100 + 100)
    expected = base_stat_sum + 0.10 * (300 + 250)
    assert score_fast(team, cfg) == pytest.approx(expected)


def test_targeted_stat_up_total_differs_from_the_team_wide_stat_up_total():
    """Scores the same six cards twice — once with the skill's target
    naming a strict subset (type Cute, count=2) and once with the
    affirmative team-wide target — and asserts the two totals differ.

    Under a target-blind `team.base_stat_sum() * (1 + combined_stat_rate)`
    formula, the target is never read at all: both arrangements produce
    the identical weighted_rate against the identical base_stat_sum, so
    they collapse to one number under that formula. This test is
    therefore red under such a formula by construction, not by luck —
    mirroring `test_scoring_models.py::
    test_four_combinations_produce_four_distinct_totals`'s discipline of
    asserting distinctness rather than merely that a number came out."""
    cfg = make_scoring_config()

    subset_team = make_team(_targeted_stat_up_cards())
    subset_total = score_fast(subset_team, cfg)

    team_wide_skill = make_skill(
        "PASSIVE", family="stat_up", effect_type="performance", magnitude=10.0, target=None
    )
    team_wide_cards = _targeted_stat_up_cards()
    # Rebuild the leader with the identical skill but the affirmative
    # team-wide target, keeping every other card and every stat unchanged.
    # Located by id, not by index: index 0 is the OUTFIT card, and
    # replacing that one would leave the subset-targeted skill in place and
    # make the two totals trivially equal.
    carrier_index = next(
        i for i, card in enumerate(team_wide_cards) if card.id == "target_leader"
    )
    team_wide_cards[carrier_index] = make_card(
        "target_leader",
        type_="Cute",
        stats=Stats(performance=300, technique=100, sense=100),
        passive=team_wide_skill,
    )
    team_wide_team = make_team(team_wide_cards)
    team_wide_total = score_fast(team_wide_team, cfg)

    assert subset_total != team_wide_total


# --- all_parameter_up: all_stats multi-stat arithmetic ---------------------

# --- and multi-stat target-selection ranking ------------------------------


def _all_stats_base_cards():
    """Six non-uniform cards (no populated skills yet), reused as the base
    for both sides of the all_parameter_up equivalence test below —
    non-uniform stats so a target-blind formula cannot pass, same shape
    as `_targeted_stat_up_cards`.

    Index 0 is the OUTFIT card and carries nothing: `make_team` puts it in the
    leader slot, where a normal skill would not be emitted at all. Every
    skill under test therefore goes on indices 1-5, the five members."""
    return [
        make_card(
            "as_outfit", type_="Happy", stats=Stats(performance=40, technique=40, sense=40)
        ),
        make_card(
            "as_leader", type_="Cute", stats=Stats(performance=300, technique=150, sense=120)
        ),
        make_card(
            "as_member_1", type_="Cute", stats=Stats(performance=200, technique=180, sense=90)
        ),
        make_card(
            "as_member_2", type_="Pure", stats=Stats(performance=140, technique=220, sense=60)
        ),
        make_card(
            "as_member_3", type_="Pure", stats=Stats(performance=90, technique=90, sense=300)
        ),
        make_card(
            "as_member_4", type_="Happy", stats=Stats(performance=60, technique=60, sense=200)
        ),
    ]


def test_all_parameter_up_team_wide_total_equals_three_single_stat_team_wide_skills_of_the_same_magnitude():
    """The equivalence case: a team-wide
    all_stats skill of magnitude m raises all three stats of all six cards
    by m/100, and the resulting total equals the total of three separate
    single-stat team-wide skills of the same magnitude under additive
    stacking. Asserted with a bare `==` -- both sides are computed by the
    identical per-(card, stat_name) accumulation, so this is a structural
    identity, never a numeric coincidence a tolerance would paper over."""
    cfg = make_scoring_config()

    all_stats_skill = make_skill(
        "PASSIVE", family="stat_up", effect_type="all_stats", magnitude=10.0, target=None
    )
    all_stats_cards = _all_stats_base_cards()
    all_stats_cards[1] = make_card(
        "as_leader",
        type_="Cute",
        stats=Stats(performance=300, technique=150, sense=120),
        passive=all_stats_skill,
    )
    all_stats_total = score_fast(make_team(all_stats_cards), cfg)

    performance_skill = make_skill(
        "PASSIVE", family="stat_up", effect_type="performance", magnitude=10.0, target=None
    )
    technique_skill = make_skill(
        "PASSIVE", family="stat_up", effect_type="technique", magnitude=10.0, target=None
    )
    sense_skill = make_skill(
        "PASSIVE", family="stat_up", effect_type="sense", magnitude=10.0, target=None
    )
    split_cards = _all_stats_base_cards()
    split_cards[1] = make_card(
        "as_leader",
        type_="Cute",
        stats=Stats(performance=300, technique=150, sense=120),
        passive=performance_skill,
    )
    split_cards[2] = make_card(
        "as_member_1",
        type_="Cute",
        stats=Stats(performance=200, technique=180, sense=90),
        passive=technique_skill,
    )
    split_cards[3] = make_card(
        "as_member_2",
        type_="Pure",
        stats=Stats(performance=140, technique=220, sense=60),
        passive=sense_skill,
    )
    split_total = score_fast(make_team(split_cards), cfg)

    assert all_stats_total == split_total


def _targeted_all_stats_cards():
    """Mirrors `_targeted_stat_up_cards`'s discipline for the three-stat
    case: the targeted subset (top 2 by stat_sum among the three Cute
    cards) and the untargeted remainder differ across performance,
    technique, AND sense, so the targeted-vs-team-wide comparison below
    cannot pass under a formula that ignores the target."""
    target = EffectTarget(kind="type", value="Cute", count=2)
    skill = make_skill(
        "PASSIVE", family="stat_up", effect_type="all_stats", magnitude=10.0, target=target
    )
    return [
        # Index 0 is the OUTFIT card: Happy, so it never qualifies for the
        # Cute target, and outside the five so it adds no stats.
        make_card(
            "as_filler_happy_2", type_="Happy", stats=Stats(performance=10, technique=10, sense=10)
        ),
        make_card(
            "as_target_leader",
            type_="Cute",
            stats=Stats(performance=300, technique=200, sense=100),
            passive=skill,
        ),
        make_card(
            "as_target_high",
            type_="Cute",
            stats=Stats(performance=250, technique=150, sense=100),
        ),
        make_card(
            "as_target_low", type_="Cute", stats=Stats(performance=50, technique=50, sense=50)
        ),
        make_card(
            "as_filler_pure",
            type_="Pure",
            stats=Stats(performance=999, technique=999, sense=999),
        ),
        make_card(
            "as_filler_happy_1", type_="Happy", stats=Stats(performance=10, technique=10, sense=10)
        ),
    ]


def test_all_parameter_up_targeted_total_differs_from_the_team_wide_all_stats_total():
    """A targeted all_stats skill's total differs from the same skill made
    team-wide -- built on a fixture where at least one non-targeted card
    (as_target_low, as_filler_pure, both filler_happy cards) holds
    different stat values, so the assertion cannot pass under a formula
    that ignores the target."""
    cfg = make_scoring_config()

    targeted_total = score_fast(make_team(_targeted_all_stats_cards()), cfg)

    team_wide_skill = make_skill(
        "PASSIVE", family="stat_up", effect_type="all_stats", magnitude=10.0, target=None
    )
    team_wide_cards = _targeted_all_stats_cards()
    # By id, not index: index 0 is the OUTFIT card, so replacing it would
    # leave the targeted skill in place and make the two totals trivially equal.
    _ti = next(i for i, c in enumerate(team_wide_cards) if c.id == "as_target_leader")
    team_wide_cards[_ti] = make_card(
        "as_target_leader",
        type_="Cute",
        stats=Stats(performance=300, technique=200, sense=100),
        passive=team_wide_skill,
    )
    team_wide_total = score_fast(make_team(team_wide_cards), cfg)

    assert targeted_total != team_wide_total


def _all_stats_selection_ranking_cards():
    """Four Cute cards where the top 2 by a single stat (performance,
    `STAT_NAMES_BY_EFFECT_TYPE["all_stats"][0]`) are a DIFFERENT pair than
    the top 2 by `card.stat_sum()`: as_rank_high_perf/as_rank_second_perf
    dominate on performance alone (300/290) but have zero technique/sense,
    while as_rank_high_sum/as_rank_second_sum have much lower performance
    (50/40) but a far higher combined total (250/220) -- so this test would
    fail if all_stats target ranking fell back to a single stat instead of
    stat_sum()."""
    target = EffectTarget(kind="type", value="Cute", count=2)
    skill = make_skill(
        "PASSIVE", family="stat_up", effect_type="all_stats", magnitude=10.0, target=target
    )
    return [
        # Index 0 is the OUTFIT card: Happy, outside the four Cute candidates
        # the ranking is about, and contributing no stats.
        make_card(
            "as_rank_filler_2", type_="Happy", stats=Stats(performance=10, technique=10, sense=10)
        ),
        make_card(
            "as_rank_high_perf",
            type_="Cute",
            stats=Stats(performance=300, technique=0, sense=0),
            passive=skill,
        ),
        make_card(
            "as_rank_second_perf",
            type_="Cute",
            stats=Stats(performance=290, technique=0, sense=0),
        ),
        make_card(
            "as_rank_high_sum",
            type_="Cute",
            stats=Stats(performance=50, technique=200, sense=200),
        ),
        make_card(
            "as_rank_second_sum",
            type_="Cute",
            stats=Stats(performance=40, technique=190, sense=190),
        ),
        make_card(
            "as_rank_filler_1", type_="Happy", stats=Stats(performance=10, technique=10, sense=10)
        ),
    ]


def test_all_parameter_up_target_selection_ranks_candidates_by_stat_sum_not_a_single_stat():
    team = make_team(_all_stats_selection_ranking_cards())
    cfg = make_scoring_config()

    breakdown = score_with_breakdown(team, cfg)
    all_stats_contribution = next(
        contribution
        for contribution in breakdown.contributions
        if contribution.effect_type == "all_stats"
    )

    assert set(all_stats_contribution.target_card_ids) == {
        "as_rank_high_sum",
        "as_rank_second_sum",
    }


# --- Override scoring semantics ---------------------------------------------
# The override REPLACES the base magnitude when its own gate fires; it is
# never summed with the base, and an unfired override leaves the base in
# force.


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


def _plain_active_skill(magnitude: float):
    return make_skill(
        "ACTIVE", duration=20.0, interval=20.0, probability_tier="Medium", magnitude=magnitude
    )


def _team_with_pure_member_count(skill, pure_count: int):
    """A team whose Pure-type MEMBER count controls whether a
    `type=Pure, count=2` gate fires — used here to control the override's own
    gate, exactly as any other gate is controlled via team composition.

    The carrier is a member (a leader's ACTIVE is not emitted) and is
    Happy, as is the outfit card, so neither ever supplies a Pure itself and
    `pure_count` alone decides."""
    carrier = make_card("override_leader", type_="Happy", active=skill)
    types = ["Pure"] * pure_count + ["Cute"] * (4 - pure_count)
    members = [carrier] + [
        make_card(f"override_member_{i}", type_=types[i]) for i in range(4)
    ]
    return make_team([make_card("override_outfit", type_="Happy"), *members])


def test_a_fired_override_scores_exactly_what_a_plain_base_magnitude_slot_of_the_same_value_scores():
    cfg = make_scoring_config(probability_tiers=dict(_CERTAIN_TIERS))

    fired_total = score_fast(
        _team_with_pure_member_count(
            _override_active_skill(magnitude=35.0, override_magnitude=70.0), pure_count=2
        ),
        cfg,
    )
    plain_70_total = score_fast(
        _team_with_pure_member_count(_plain_active_skill(70.0), pure_count=2), cfg
    )

    assert fired_total == plain_70_total


def test_an_unfired_override_scores_exactly_what_a_plain_base_slot_scores():
    cfg = make_scoring_config(probability_tiers=dict(_CERTAIN_TIERS))

    unfired_total = score_fast(
        _team_with_pure_member_count(
            _override_active_skill(magnitude=35.0, override_magnitude=70.0), pure_count=0
        ),
        cfg,
    )
    plain_35_total = score_fast(
        _team_with_pure_member_count(_plain_active_skill(35.0), pure_count=0), cfg
    )

    assert unfired_total == plain_35_total


def test_fired_and_unfired_override_totals_on_the_same_slot_shape_are_distinct():
    """The distinctness assertion that fails if the override is silently
    ignored — the same discipline `test_targeted_stat_up_total_differs_
    from_the_team_wide_stat_up_total` uses for targeting."""
    cfg = make_scoring_config(probability_tiers=dict(_CERTAIN_TIERS))
    skill = _override_active_skill(magnitude=35.0, override_magnitude=70.0)

    fired_total = score_fast(_team_with_pure_member_count(skill, pure_count=2), cfg)
    unfired_total = score_fast(_team_with_pure_member_count(skill, pure_count=0), cfg)

    assert fired_total != unfired_total


def test_a_fired_override_never_sums_with_the_base_strictly_less_than_the_summed_magnitude_team():
    """Proven against the specific wrong answer (summing base + override),
    not merely "some number came out" — a fired 35->70 override must score
    strictly less than the same slot shape carrying a plain 105 (35 + 70)
    magnitude."""
    cfg = make_scoring_config(probability_tiers=dict(_CERTAIN_TIERS))

    fired_total = score_fast(
        _team_with_pure_member_count(
            _override_active_skill(magnitude=35.0, override_magnitude=70.0), pure_count=2
        ),
        cfg,
    )
    summed_total = score_fast(
        _team_with_pure_member_count(_plain_active_skill(105.0), pure_count=2), cfg
    )

    assert fired_total < summed_total


def test_effective_magnitude_returns_the_override_magnitude_only_when_its_own_gate_fired():
    skill = _override_active_skill(magnitude=35.0, override_magnitude=70.0)
    predicate = GatePredicate(dimension="type", value="Pure", threshold=2)

    fired = make_resolved(
        "card_effmag_01",
        "ACTIVE",
        skill,
        override_gate=GateResolution(predicate=predicate, fired=True, actual_count=2),
    )
    assert _effective_magnitude(fired) == 70.0

    unfired = make_resolved(
        "card_effmag_02",
        "ACTIVE",
        skill,
        override_gate=GateResolution(predicate=predicate, fired=False, actual_count=0),
    )
    assert _effective_magnitude(unfired) == 35.0

    no_override = make_resolved(
        "card_effmag_03", "ACTIVE", _plain_active_skill(35.0)
    )
    assert _effective_magnitude(no_override) == 35.0
