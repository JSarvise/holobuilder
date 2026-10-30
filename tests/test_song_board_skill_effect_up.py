"""`song.py` must apply the board's `SkillEffectUpPermilUp`.

`scoring._score_up_pool` takes the term as its fourth parameter and puts it
where `CalcScoreUpRatio` puts it:

    v(e) = (1 + SkillEffectUpPermilUp/1000 + B) x ScoreUpByRatio(e)

Both `scoring.py` call sites pass `board_skill_effect_up`, and so must
`song.py`'s. A call site that passed three arguments would let the parameter
take its `0.0` default and drop the whole channel from the per-song driver —
silently, because `0.0` is also the correct answer for every team with no board.

⚠ That would be a SELECTION defect, the same shape
`test_board_skill_effect_up.py` guards. `score_bonus_rate` is a
team-invariant account constant, so a driver missing only that cannot reorder
anything; `skill_effect_up` is leader- and member-scoped, so a driver missing
IT ranks leaders wrongly. On a real account Ina'nis' leader-scope node
alone is **173 permil** against a `score_bonus_rate` of 4.8.

The two drivers must agree. `scoring._board_bonus`'s own docstring
calls itself *"the single place either driver reads the Holomem Board"*, and
these tests are what holds `song.py` to that.

Every number below is derived by hand in its own test. The board is built
directly as a `BoardModel` rather than through `cfg.board_score_bonus`, because
that dict feeds `score_bonus_rate` — a different channel, and the one
`test_score_song.py` already covers.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.board import (
    BoardEffect,
    BoardModel,
    BoardNode,
    CharacterBoard,
)
from hbuilder.domain.song import (
    ChartNote,
    SongChart,
    score_song,
    score_song_detailed,
)

from conftest import make_card, make_scoring_config, make_skill

# Same pinning as `test_score_song.py`: every tier certain, so a live ACTIVE has
# occupancy exactly 1.0 and `E[max]` reduces to `max`.
CERTAIN_TIERS = {"Low": 1.0, "Medium": 1.0, "High": 1.0}

LEADER_SCOPE_PERMIL = 173.0  # Ina'nis' own


def cfg(**overrides):
    base = make_scoring_config(probability_tiers=CERTAIN_TIERS)
    return dataclasses.replace(base, **overrides) if overrides else base


def _skill_effect_up(permil: float, scope: str, *, conditional: bool = False):
    return BoardEffect(
        family="live_active_skill_effect_up_permil_up",
        kind="skill_effect_up",
        stat="",
        value=permil / 1000.0,
        scope=scope,
        conditional=conditional,
    )


def _board(holomem: str, effect: BoardEffect) -> BoardModel:
    return BoardModel(
        characters={
            holomem: CharacterBoard(
                holomem=holomem, nodes=(BoardNode("R-000", (effect,)),)
            )
        }
    )


def _chart(times=(25.0, 30.0)) -> SongChart:
    """Level 5 pins `LiveScoreMusicCoefficient` to exactly 1.0, so the only
    moving part is the score-up pool."""
    return SongChart(
        music_id="m0001",
        difficulty="easy",
        difficulty_level=5,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=t, perfect_permil=1000) for t in times),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )


def _always_live_scorer(card_id: str, magnitude: float, member: str):
    """An ACTIVE live across both of `_chart`'s notes.

    ⚠ The window is `[20, 50)`, not `[0, 30)`. The cooldown starts at 0 and
    there is **no check at t = 0**, so the first
    activation is at `t = interval`. Notes at 1.0 and 2.0 — the obvious choice —
    are before any ACTIVE can have fired, and every test here would then measure
    the plain baseline while looking like it measured the pool."""
    return make_card(
        card_id,
        member=member,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            duration=30.0,
            interval=20.0,
            probability_tier="High",
            magnitude=magnitude,
        ),
    )


def _members():
    """One ACTIVE scorer at 50 % plus four plain cards."""
    return (_always_live_scorer("card-a", 50.0, "Member A"),) + tuple(
        make_card(f"card-{i}", member=f"Member {i}") for i in range(1, 5)
    )


# ---------------------------------------------------------------------------
# The board term reaches the ACTIVE pool
# ---------------------------------------------------------------------------


def test_a_leader_scope_board_node_multiplies_the_active_pool():
    """Hand arithmetic, two 1000-permil notes, `deck_power`
    2000, level 5, combo below 100 throughout:

        constant = 2000 x 2.300 x 1.0 / 2.0 = 2300.0 per note

    Member A's ACTIVE is live across both notes at magnitude 50 %, so without
    the board the pool is 0.50 and each note scores

        ceil(2300.0 x 1.0 x (1 + 0.50)) = 3450   -> total 6900

    Ina leads and her board carries a 173-permil leader-scope
    `skill_effect_up`. `CalcScoreUpRatio` puts it INSIDE the parenthesis that
    multiplies the ACTIVE ratio, so the pool becomes

        0.50 x (1 + 0.173) = 0.5865
        ceil(2300.0 x 1.5865) = ceil(3648.95) = 3649  -> total 7298

    6900 here would mean the board was computed and discarded."""
    members = _members()
    leader = make_card("leader", member="Ina")
    chart = _chart()
    board = _board("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader"))

    without = score_song(members, (leader,), chart, cfg(), deck_power=2000)
    with_board = score_song(
        members, (leader,), chart, cfg(board=board), deck_power=2000
    )

    assert without[0].total_score == 6900
    assert with_board[0].total_score == 7298


def test_it_pays_only_under_its_own_leader():
    """`_recipients`: a red node applies to the whole team, but only while that
    holomem leads. Under Nene, Ina's node pays nothing and the score falls back
    to the no-board number — which is exactly what makes this term able to
    reorder leaders, and `score_bonus_rate` unable to."""
    members = _members()
    chart = _chart()
    board = _board("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader"))

    result = score_song(
        members, (make_card("leader", member="Nene"),), chart, cfg(board=board),
        deck_power=2000,
    )
    assert result[0].total_score == 6900


def test_it_reorders_two_leaders_which_is_the_whole_point():
    """One `score_song` call, two leaders, one board. The per-song driver must
    rank Ina above Nene here, not score them equal.

    This is the assertion that a scaling defect could not produce."""
    members = _members()
    chart = _chart()
    board = _board("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader"))
    leaders = (make_card("lead-ina", member="Ina"), make_card("lead-nene", member="Nene"))

    scores = score_song(members, leaders, chart, cfg(board=board), deck_power=2000)
    by_id = {s.leader_id: s.total_score for s in scores}

    assert by_id["lead-ina"] > by_id["lead-nene"]
    assert by_id["lead-ina"] == 7298
    assert by_id["lead-nene"] == 6900


def test_a_team_with_no_active_score_up_gains_nothing_from_it():
    """The assertion that separates this reading from an additive one, and the
    same one `_score_up_pool`'s docstring makes: `SkillEffectUpPermilUp`
    multiplies the ACTIVE pool, so a team with no ACTIVE Score UP gains exactly
    nothing. Five plain cards, board or no board, must score identically.

    Without it, a wrong wiring that ADDED the term would pass every other test
    in this file."""
    plain = tuple(make_card(f"card-{i}", member=f"Member {i}") for i in range(5))
    chart = _chart()
    board = _board("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader"))
    leader = make_card("leader", member="Ina")

    without = score_song(plain, (leader,), chart, cfg(), deck_power=2000)
    with_board = score_song(
        plain, (leader,), chart, cfg(board=board), deck_power=2000
    )
    assert with_board[0].total_score == without[0].total_score == 4600


def test_a_conditional_node_is_still_excluded_here():
    """A conditional node's trigger is the song's own cast, which `SongChart`
    does not carry — so the per-song driver is no better placed to evaluate it
    than the ranking one, and it stays excluded here.

    In the game, Aki's `R-002` contributes **0** on a deck screen with no song
    and **41 permil** once Aki is the credited Singer. Both halves are real;
    this pins the first."""
    members = _members()
    chart = _chart()
    board = _board("Ina", _skill_effect_up(240.0, "leader", conditional=True))
    leader = make_card("leader", member="Ina")

    result = score_song(members, (leader,), chart, cfg(board=board), deck_power=2000)
    assert result[0].total_score == 6900


# ---------------------------------------------------------------------------
# Parity between the two drivers
# ---------------------------------------------------------------------------


def test_both_drivers_read_the_same_board_term():
    """`scoring._board_bonus` calls itself *"the single place either driver
    reads the Holomem Board"*. That is a claim about `song.py` too, and this
    checks it.

    Rather than compare two absolute scores — the drivers legitimately differ,
    one being a run total and the other a ranking quantity — this asserts the
    two read the SAME number out of the board for the same arrangement."""
    from hbuilder.domain.scoring import _board_bonus

    members = _members()
    leader = make_card("leader", member="Ina")
    board = _board("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader"))

    _power, _rate, skill_effect_up = _board_bonus(leader, members, cfg(board=board))
    assert skill_effect_up == pytest.approx(LEADER_SCOPE_PERMIL / 1000.0)
    assert board.skill_effect_up_for(members, "Ina") == pytest.approx(
        LEADER_SCOPE_PERMIL / 1000.0
    )


# ---------------------------------------------------------------------------
# The SECOND board channel: POWER
# ---------------------------------------------------------------------------
#
# ⚠ `board_power` is the other board channel `song.py` must not compute and
# throw away, and it is easy to miss:
#
#   1. tests that reach `score_song` with a board usually pass an explicit
#      `deck_power=2000`, which makes the default moot. The default path, the
#      one the CLI actually takes, needs its own board coverage;
#   2. `test_score_song.py::test_the_per_song_leader_ORDER_agrees_with_the_
#      ranking_scorer` pins the two scorers together, but its fixture has no
#      board, so a discarded term is zero on both sides and it would agree for
#      the wrong reason.
#
# On a real account `_board_bonus` returns 46,892 of power under Ina'nis (the
# only holomem with leader-scoped nodes) against 17,872 under any other
# leader — 29,020, about 17.7 % of a ~164 k Overall Power. Discarding it makes
# the per-song scorer prefer a different leader on every Expert chart, an
# 8.97 % mean gap against the song-agnostic ranking; with it the two agree.
#
# So: no `deck_power` argument below, deliberately. That is the whole point.


def _all_parameter_up(value: float, scope: str) -> BoardEffect:
    return BoardEffect(
        family="all_parameter_up",
        kind="stat",
        stat="all",
        value=value,
        scope=scope,
    )


def test_a_leader_scope_board_node_adds_its_POWER_to_the_default_deck_power():
    """The default `deck_power` must carry the board, because Overall Power does.

    `up_by_skill_tree [Holomem Board]` is one of the six terms of the
    game's own Overall Power, read off the wire, and `board.py` reproduces both
    of its halves exactly (44,682 with Ina leading / 17,687 without). A scorer
    whose default power omits it is not answering the game's question.
    """
    members = _members()
    leader = make_card("leader-ina", member="Ina")
    chart = _chart()
    board = _board("Ina", _all_parameter_up(100.0, "leader"))

    without = score_song_detailed(members, (leader,), chart, cfg())[0]
    with_board = score_song_detailed(
        members, (leader,), chart, cfg(board=board)
    )[0]

    # 100 flat to each of 3 stats on each of 5 members = 1,500 of power.
    assert with_board.deck_power - without.deck_power == pytest.approx(1500.0)
    assert with_board.total_score > without.total_score


def test_the_two_scorers_AGREE_on_the_leader_when_a_board_is_present():
    """The property `test_score_song.py`'s version claims and cannot check.

    Same assertion, but with a board carrying leader-scoped POWER — the input
    whose absence would let both scorers agree for the wrong reason. Discarding
    it would rank the boarded leader BELOW the plain one, because her whole
    advantage is that term.
    """
    members = _members()
    boarded = make_card("leader-ina", member="Ina")
    plain = make_card("leader-other", member="Other")
    board = _board("Ina", _all_parameter_up(100.0, "leader"))
    chart = _chart()

    per_song = score_song_detailed(
        members, (boarded, plain), chart, cfg(board=board)
    )
    by_score = sorted(per_song, key=lambda s: -s.total_score)

    assert by_score[0].leader_id == "leader-ina", (
        "the leader carrying the board must win; if she does not, the power "
        "term is being discarded again"
    )
    assert by_score[0].deck_power > by_score[1].deck_power
