"""`song.py` must apply the board's ACTIVE-uptime nodes (cooldown / activation
rate). It did not — the per-song driver read only `active_cooldown_reduction`
and `team_rate_up`, dropping `cfg.board.uptime_for(member)` that the general
scorer (`scoring._active_occupancy`) folds in.

Same shape as `test_song_board_skill_effect_up.py`: a board channel computed by
`scoring.py` and silently discarded by `song.py`, so every per-song board plan
valued Act.CD (cooldown) and Act.Prob (activation-rate) nodes at exactly 0 and
under-developed them. Consequence caught in the wild: the cooldown timeline
showed 2-4 members' activation windows getting denser under the board while the
per-song score delta was +0.0.

The fix mirrors `_active_occupancy`: the cooldown term DIVIDES the interval
(`interval / (1 + reduction + board_cooldown)`) and the rate term lifts the proc
probability (`apply(base, team_rate_up + board_rate)`). These assert the
DIRECTION — a board node that shortens the cooldown or raises the rate can only
raise a cooldown-sensitive ACTIVE's per-song score — plus the control that a team
with no ACTIVE is untouched.
"""

from __future__ import annotations

import dataclasses

from hbuilder.domain.board import BoardEffect, BoardModel, BoardNode, CharacterBoard
from hbuilder.domain.song import ChartNote, SongChart, score_song

from conftest import make_card, make_scoring_config, make_skill

CERTAIN_TIERS = {"Low": 1.0, "Medium": 1.0, "High": 1.0}
UNCERTAIN_TIERS = {"Low": 0.5, "Medium": 0.5, "High": 0.5}


def cfg(**overrides):
    base = make_scoring_config(probability_tiers=CERTAIN_TIERS)
    return dataclasses.replace(base, **overrides) if overrides else base


def cfg_uncertain(**overrides):
    base = make_scoring_config(probability_tiers=UNCERTAIN_TIERS)
    return dataclasses.replace(base, **overrides) if overrides else base


def _cooldown(permil: float) -> BoardEffect:
    return BoardEffect(
        family="live_active_skill_cool_time_shorten_permil_up",
        kind="cooldown_shorten", stat="", value=permil / 1000.0, scope="member",
    )


def _rate(permil: float) -> BoardEffect:
    return BoardEffect(
        family="live_active_skill_activation_probability_up_permil_up",
        kind="activation_rate_up", stat="", value=permil / 1000.0, scope="member",
    )


def _board(holomem: str, effect: BoardEffect) -> BoardModel:
    return BoardModel(
        characters={holomem: CharacterBoard(
            holomem=holomem, nodes=(BoardNode("B-000", (effect,)),))},
    )


def _cooldown_sensitive_scorer(member: str):
    """An ACTIVE whose windows are SPARSE (duration 5 < interval 30), so a note in
    the gap before the first window is uncovered at the base cooldown and becomes
    covered once the board shortens the interval. Base windows start at [30, 35);
    a 500-permil shorten pulls the interval to 20 s, so the first window is [20, 25)."""
    return make_card(
        member.lower().replace(" ", "-"),
        member=member,
        active=make_skill(
            "ACTIVE", family="conditional_score_up", effect_type="score",
            duration=5.0, interval=30.0, probability_tier="High", magnitude=80.0,
        ),
    )


def _team():
    scorer = _cooldown_sensitive_scorer("Member A")
    filler = tuple(make_card(f"card-{i}", member=f"Member {i}") for i in range(1, 5))
    leader = make_card("leader", member="Leader")
    return (scorer, *filler), (leader,)


def _chart_with_note_in_the_gap() -> SongChart:
    """One note at t = 22 s — inside the base active's first GAP (before [30,35))
    but inside the shortened active's first window [20,25). So the board node moves
    it from baseline to boosted."""
    return SongChart(
        music_id="m0001", difficulty="easy", difficulty_level=5,
        live_score_coefficient_permil=4,
        notes=(ChartNote(time=22.0, perfect_permil=1000),),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )


def test_a_board_cooldown_node_raises_a_cooldown_sensitive_active_score():
    members, leaders = _team()
    chart = _chart_with_note_in_the_gap()
    board = _board("Member A", _cooldown(500.0))  # interval 30 -> 20

    without = score_song(members, leaders, chart, cfg(), deck_power=2000)[0]
    with_board = score_song(members, leaders, chart, cfg(board=board), deck_power=2000)[0]

    assert with_board.total_score > without.total_score, (
        "the board cooldown node must shorten the interval and cover the gap note; "
        "if scores are equal the per-song scorer is discarding board_cooldown again"
    )


def test_a_board_activation_rate_node_raises_an_uncertain_active_score():
    members, leaders = _team()
    # A note squarely inside a window at BOTH cooldowns, so only the PROBABILITY
    # (not the window coverage) moves: t = 33 is in the base window [30, 35).
    chart = SongChart(
        music_id="m0001", difficulty="easy", difficulty_level=5,
        live_score_coefficient_permil=4,
        notes=(ChartNote(time=33.0, perfect_permil=1000),),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )
    board = _board("Member A", _rate(400.0))

    without = score_song(members, leaders, chart, cfg_uncertain(), deck_power=2000)[0]
    with_board = score_song(members, leaders, chart, cfg_uncertain(board=board), deck_power=2000)[0]

    assert with_board.total_score > without.total_score, (
        "the board activation-rate node must raise the ACTIVE proc chance; equal "
        "scores mean the per-song scorer is discarding board_rate again"
    )


def test_a_team_with_no_active_is_untouched_by_a_board_cooldown_node():
    """The control: a cooldown node multiplies nothing when there is no ACTIVE, so
    a plain team scores identically with or without the board. Guards against a
    wiring that adds the term unconditionally."""
    plain = tuple(make_card(f"card-{i}", member=f"Member {i}") for i in range(5))
    leaders = (make_card("leader", member="Leader"),)
    chart = _chart_with_note_in_the_gap()
    board = _board("Member A", _cooldown(500.0))

    without = score_song(plain, leaders, chart, cfg(), deck_power=2000)[0]
    with_board = score_song(plain, leaders, chart, cfg(board=board), deck_power=2000)[0]
    assert with_board.total_score == without.total_score


def test_active_all_fire_is_a_ceiling_above_the_probabilistic_estimate():
    """`active_all_fire` forces every ACTIVE to proc (probability 1) — the "max if
    all actives fired" ceiling the board plan shows beside the estimate. With
    uncertain tiers (proc 0.5) it must beat the estimate; with certain tiers
    (already 1.0) it must be a no-op."""
    members, leaders = _team()
    # a note INSIDE the base active's first window [30,35), so the active COVERS it
    # and its PROC CHANCE matters: uncertain (0.5) half-covers, all-fire fully covers.
    chart = dataclasses.replace(
        _chart_with_note_in_the_gap(),
        notes=(ChartNote(time=32.0, perfect_permil=1000),))

    estimate = score_song(members, leaders, chart, cfg_uncertain(), deck_power=2000)[0]
    ceiling = score_song(members, leaders, chart,
                         cfg_uncertain(active_all_fire=True), deck_power=2000)[0]
    assert ceiling.total_score > estimate.total_score, (
        "all-fire must raise a probabilistic active's score above the estimate")

    certain = score_song(members, leaders, chart, cfg(), deck_power=2000)[0]
    certain_all = score_song(members, leaders, chart,
                             cfg(active_all_fire=True), deck_power=2000)[0]
    assert certain_all.total_score == certain.total_score, (
        "an active that already fires with probability 1 is unchanged by all-fire")
