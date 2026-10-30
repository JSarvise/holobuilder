"""The SPECIAL activation-rate rider on the per-song time axis (`song._rider_terms`).

A rider is a second in-run effect on a SPECIAL, live for that SPECIAL's duration: every
ACTIVE check inside it rolls with `base x (1 + rates + rider)`. Without it, a +55 % rider
on m0334 changes nothing (493,188 either way). The leaf models a boosted check as an
extra roll `q = (p' - p) / (1 - p)` on the same window, which `E[max]` combines with the
base roll into exactly `p'` -- held to that here against a team whose base probability
simply IS `p'`.
"""
from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.card import GateCondition, SlotRider
from hbuilder.domain.song import ChartNote, SongChart, score_song_detailed

from conftest import make_card, make_scoring_config, make_skill

CUES = (5.0, 15.0, 25.0, 35.0, 45.0)
RIDER = 55.0                     # +55 %, as on card-00002-5-uniq-0001-00


def _chart():
    # a note every half second for a minute; the ACTIVE below checks once, at 50 s
    return SongChart(
        music_id="m9999", difficulty="EXPERT", difficulty_level=20,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=0.5 * k, perfect_permil=1000) for k in range(1, 121)),
        special_cues=CUES,
    )


def _team(*, rider_condition=None, with_rider=True):
    active = make_skill("ACTIVE", family="conditional_score_up", effect_type="score",
                        duration=5.0, interval=50.0, probability_tier="Medium", magnitude=40.0)
    host_special = make_skill(
        "SPECIAL", family="score_support", effect_type="score", duration=60.0, magnitude=20.0,
        rider=SlotRider(family="activation_rate_up", effect_type="activation_rate",
                        magnitude=RIDER, condition=rider_condition, target=None)
        if with_rider else None)
    plain_special = make_skill("SPECIAL", family="score_support", effect_type="score",
                               duration=60.0, magnitude=20.0)
    members = (
        make_card("card-a", member="A", active=active, special=host_special),
        *(make_card(f"card-{i}", member=f"M{i}", special=plain_special) for i in range(4)),
    )
    return members, make_card("leader", member="L")


def _score(members, leader, cfg):
    return score_song_detailed(members, (leader,), _chart(), cfg)[0].total_score


def test_a_rider_covering_every_check_scores_as_the_raised_probability():
    cfg = make_scoring_config()
    base = cfg.probability_tiers["Medium"]
    boosted = cfg.activation_rate_model.apply(base, RIDER / 100.0)
    assert base < boosted < 1.0
    members, leader = _team()
    with_rider = _score(members, leader, cfg)

    plain, leader = _team(with_rider=False)
    raised = make_scoring_config(probability_tiers={**cfg.probability_tiers, "Medium": boosted})
    reference = _score(plain, leader, raised)
    no_rider = _score(plain, leader, cfg)

    assert with_rider > no_rider
    # exact up to the per-note ceil of a last-ulp difference (q vs p' in E[max])
    assert with_rider == pytest.approx(reference, abs=len(_chart().notes))


def test_a_combo_gate_is_read_at_the_cue_not_from_the_static_run_state():
    # combo >= 100 at a cue needs 100 notes behind it: a note every 0.5 s puts 99 before
    # the 50 s check's earliest cue window ... and cue 1 (5 s) has only 9. Gated at 9 notes
    # it fires from cue 1; gated past the chart's note count it never fires.
    cfg = make_scoring_config()
    plain, leader = _team(with_rider=False)
    no_rider = _score(plain, leader, cfg)
    ungated, leader = _team()
    always = _score(ungated, leader, cfg)

    never, leader = _team(rider_condition=GateCondition(kind="run_state", target="combo", count=10_000))
    assert _score(never, leader, cfg) == no_rider        # the static run state says combo 100
    early, leader = _team(rider_condition=GateCondition(kind="run_state", target="combo", count=9))
    assert _score(early, leader, cfg) == always


def test_the_kernel_and_the_python_walk_agree_with_a_rider():
    pytest.importorskip("ctypes")
    import sys
    from pathlib import Path

    kernel_dir = Path(__file__).resolve().parents[1] / "tools" / "web" / "kernel"
    if str(kernel_dir) not in sys.path:
        sys.path.insert(0, str(kernel_dir))
    try:
        import notewalk
    except Exception:  # noqa: BLE001
        pytest.skip("no note-walk kernel here")
    from hbuilder.domain import song

    import os

    cfg = make_scoring_config()
    members, leader = _team()
    python_total = _score(members, leader, cfg)
    saved, saved_env = song.NOTE_WALK_KERNEL, os.environ.get(song.NOTE_WALK_KERNEL_ENV)
    try:
        if not notewalk.enable():
            pytest.skip("the kernel could not be built")
        assert _score(members, leader, cfg) == python_total
    finally:
        song.NOTE_WALK_KERNEL = saved
        if saved_env is None:
            os.environ.pop(song.NOTE_WALK_KERNEL_ENV, None)
        else:
            os.environ[song.NOTE_WALK_KERNEL_ENV] = saved_env


def test_the_score_distribution_agrees_with_the_leaf_on_a_rider_team():
    # the web shows "Verified" only when the simulated law's mean is the leaf's own score;
    # a rider is a random extra roll there, not a certain SPECIAL window
    from hbuilder.domain.song_distribution import run_score_distribution

    cfg = make_scoring_config()
    members, leader = _team()
    best = score_song_detailed(members, (leader,), _chart(), cfg)[0]
    dist = run_score_distribution(members, leader, _chart(), cfg, best.member_order, best.deck_power)
    assert dist["expected_total"] == best.total_score          # what `verified` compares
    # the law's mean is of per-note-ceiled runs, a hair under the ceiled expectation
    # (0.08 % on this 3,830-point chart); without the rider in the law it is far off
    assert dist["mean"] == pytest.approx(best.total_score, rel=2e-3)
