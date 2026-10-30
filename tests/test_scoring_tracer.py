"""End-to-end tracer: `config.json` on disk -> a `ScoringConfig` and a
`RunState` -> `resolve_team_gates` -> a single run-state-gated ACTIVE
skill scored through the dispatch table and the two-stage multiplicative
total -> a bare float. One path, verified whole — not per-layer unit
tests, in `tests/test_tracer_gates.py`'s style.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.card import Card, GateCondition, SkillSlot, Stats
from hbuilder.domain.gates import RunState
from hbuilder.domain.scoring import score_fast
from hbuilder.domain.scoring_config import load_scoring_config
from hbuilder.domain.team import Team

_STATS = Stats(performance=100, technique=100, sense=100)


def _blank_card(card_id: str) -> Card:
    return Card(
        id=card_id,
        member=f"Member {card_id}",
        generations=frozenset({"1期生"}),
        type="Cute",
        rarity="SSR",
        costume_name=f"Costume {card_id}",
        stats=_STATS,
        skills=(None, None, None),
        costume_skill=None,
    )


def _leader_with_run_state_gated_active() -> Card:
    active_skill = SkillSlot(
        kind="ACTIVE",
        # An ACTIVE that grants score is `conditional_score_up`. Under
        # `score_support` this slot would route to the support pool and
        # multiply an empty ACTIVE pool, scoring the bare stat sum and making
        # this end-to-end tracer measure nothing.
        family="conditional_score_up",
        effect_type="score",
        duration=10.0,
        interval=20.0,
        probability_tier="Medium",
        magnitude=25.0,
        condition=GateCondition(kind="run_state", target="combo", count=100),
        target=None,
        override=None,
        rider=None,
    )
    return Card(
        id="leader_01",
        member="Leader",
        generations=frozenset({"1期生"}),
        type="Cute",
        rarity="SSR",
        costume_name="Costume leader_01",
        stats=_STATS,
        skills=(None, active_skill, None),
        costume_skill=None,
    )


def _build_team() -> Team:
    """The gated ACTIVE lives on a MEMBER: a leader's own normal slots are
    never emitted, so parked on the leader it would score
    nothing and this tracer would measure nothing. The outfit slot takes a blank
    card and contributes no stats, so base_stat_sum is the five members'."""
    carrier = _leader_with_run_state_gated_active()
    members = (carrier, *(_blank_card(f"m{i}") for i in range(1, 5)))
    return Team(leader=_blank_card("outfit_01"), members=members)


def test_run_state_gated_active_skill_scores_end_to_end_from_config_json():
    team = _build_team()
    # This tracer pins the in-run FINITE-window lattice math (below). config.json
    # now ships model.active_uptime = "steady" for the DECK-SCREEN Unit Score, so
    # override back to "finite" explicitly: score_fast is the in-run score, whose
    # uptime is the finite lattice regardless of the deck-screen choice.
    cfg = dataclasses.replace(load_scoring_config(), active_uptime_model="finite")

    # base_stat_sum = 5 members * 300 = 1500 (the leader contributes no
    # stats); stat pool empty (factor 1.0); score pool: one rate =
    # uptime * Medium(0.46) * magnitude_rate(0.25).
    #
    # The uptime is the FINITE activation lattice over the 128.8 s reference
    # run, not the steady-state duty cycle. At duration 10 s, interval 20 s the
    # checks sit at 20, 40, 60, 80, 100 and 120 s — six of them, none at t = 0 —
    # and each is live for 10 s, the last truncated by the end of the run:
    #
    #     5 * 10 + (128.8 - 120) = 50 + 8.8 = 58.8 s live
    #     uptime = 58.8 / 128.8 = 0.4565217391304348   (= 21/46)
    #
    # so the rate is 21/46 * 0.46 * 0.25 = 0.21 * 0.25 = 0.0525 exactly
    # -> 1500 * (1 + 0.0525) = 1578.75
    assert score_fast(team, cfg) == pytest.approx(1578.75)
    # The steady-state answer: uptime min(1, 10/20) = 0.5,
    # rate 0.0575, total 1586.25. The dead first cooldown is the difference.
    assert score_fast(team, cfg) != pytest.approx(1586.25)


def test_run_state_gate_flipped_off_scores_exactly_base_stat_sum():
    team = _build_team()
    cfg = load_scoring_config()
    cfg_off = dataclasses.replace(
        cfg, run_state=RunState(life=0, combo=0)
    )

    assert score_fast(team, cfg_off) == 1500.0  # five members * 300


def test_uptime_clamps_identically_above_and_at_the_interval():
    cfg = load_scoring_config()

    def _team_with_duration(duration: float) -> Team:
        active_skill = SkillSlot(
            kind="ACTIVE",
            family="score_support",
            effect_type="score",
            duration=duration,
            interval=20.0,
            probability_tier="Medium",
            magnitude=25.0,
            condition=GateCondition(kind="run_state", target="combo", count=100),
            target=None,
        )
        leader = Card(
            id="leader_02",
            member="Leader Two",
            generations=frozenset({"1期生"}),
            type="Cute",
            rarity="SSR",
            costume_name="Costume leader_02",
            stats=_STATS,
            skills=(None, active_skill, None),
            costume_skill=None,
        )
        members = tuple(_blank_card(f"n{i}") for i in range(1, 6))
        return Team(leader=leader, members=members)

    at_interval = score_fast(_team_with_duration(20.0), cfg)
    above_interval = score_fast(_team_with_duration(30.0), cfg)

    assert above_interval == pytest.approx(at_interval)


def test_load_scoring_config_on_nonexistent_path_records_every_value_as_default():
    from pathlib import Path

    cfg = load_scoring_config(Path("does/not/exist/config.json"))
    assert all(value == "default" for value in cfg.provenance.values())
