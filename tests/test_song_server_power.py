"""The ABSOLUTE per-song score needs the server's Overall Power, not the
modelled stat pool.

`score_song_detailed`'s `deck_power` default is `stat_sum + board_power`, the
modelled stat pool, not the game's DeckPower. An absolute number needs the deck
screen's own Overall Power — which the domain reproduces bit-exactly
(`scoring.deck_screen_unit_score(...).overall_power`, exact on every deck of the
oracle corpus).

Two things these tests pin:

1. **Overall Power is per LEADER.** It carries the leader's own board power and
   the leader-dependent stat effects, so `deck_power` cannot be ONE float shared
   by every leader in the call. A scalar cannot be right for
   more than one leader at a time, and passing the WRONG constant is invisible:
   every leader's score scales by the same factor, so the ranking looks fine
   while every absolute number is off.
2. **The ranking path must not move.** `deck_power=None` stays the modelled
   pool, byte for byte, because a comparison between teams on one song does not
   care about the absolute scale and every existing number depends on it.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.scoring import deck_screen_unit_score, score_with_breakdown
from hbuilder.domain.song import (
    ChartNote,
    SongChart,
    overall_power_for,
    score_song_detailed,
)
from hbuilder.domain.team import Team

from conftest import make_card, make_scoring_config, make_skill

CERTAIN_TIERS = {"Low": 1.0, "Medium": 1.0, "High": 1.0}


def _cfg(**overrides):
    """The two account scalars are SET, and that is the point: with them unset
    the deck screen's Overall Power and the modelled stat pool coincide exactly,
    and a test on a bare fixture could not tell whether the switch did
    anything."""
    from hbuilder.domain.collection import AccountBonuses

    base = dataclasses.replace(
        make_scoring_config(probability_tiers=CERTAIN_TIERS),
        account=AccountBonuses(posters=22, power_up_permyriad=35),
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _chart():
    return SongChart(
        music_id="m0001",
        difficulty="easy",
        difficulty_level=5,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=t, perfect_permil=1000) for t in (1.0, 25.0, 45.0)),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )


def _members():
    return tuple(make_card(f"card-{i}") for i in range(5))


# ==========================================================================
# 1. The helper is the deck screen's own number, not a second derivation
# ==========================================================================


def test_overall_power_for_is_exactly_the_deck_screens_own_overall_power():
    """This must be
    the SAME number the deck-screen path reports, not a re-implementation that
    agrees today: the Overall Power identity is bit-exact against the server and
    a private copy of it would drift out of that silently."""
    members, leader, cfg = _members(), make_card("leader"), _cfg()
    reference = deck_screen_unit_score(
        score_with_breakdown(Team(leader=leader, members=members), cfg)
    ).overall_power
    assert overall_power_for(members, leader, cfg) == reference


def test_overall_power_is_bigger_than_the_modelled_pool_default():
    """The gap is the whole point: Overall Power additionally carries
    the board's power half and the two account scalars (Memory Bonus and the
    power-up permyriad), none of which the modelled stat pool includes."""
    members, leader, cfg = _members(), make_card("leader"), _cfg()
    default = score_song_detailed(members, (leader,), _chart(), cfg)[0].deck_power
    assert overall_power_for(members, leader, cfg) > default


# ==========================================================================
# 2. Per-leader power
# ==========================================================================


def test_deck_power_accepts_a_per_leader_callable():
    """One call, two leaders, two different powers — which a scalar cannot
    express. The score is linear in the power, so the check is exact."""
    members, chart, cfg = _members(), _chart(), _cfg()
    a, b = make_card("leader-a"), make_card("leader-b")
    powers = {"leader-a": 1000.0, "leader-b": 2000.0}

    scores = score_song_detailed(
        members, (a, b), chart, cfg, deck_power=lambda leader: powers[leader.id]
    )
    assert [s.deck_power for s in scores] == [1000.0, 2000.0]
    flat_a = score_song_detailed(members, (a,), chart, cfg, deck_power=1000.0)[0]
    flat_b = score_song_detailed(members, (b,), chart, cfg, deck_power=2000.0)[0]
    assert [s.total_score for s in scores] == [flat_a.total_score, flat_b.total_score]


def test_the_callable_is_asked_once_per_leader_and_is_given_that_leader():
    """It must not be called per note (three notes here) nor per slot order
    (120): `score_song_detailed`'s inner walk runs millions of times on a real
    chart, and a caller whose callable does real work — `overall_power_for`
    scores a whole team — would pay it there."""
    members, chart, cfg = _members(), _chart(), _cfg()
    a, b = make_card("leader-a"), make_card("leader-b")
    seen: list[str] = []

    def power(leader):
        seen.append(leader.id)
        return 1500.0

    score_song_detailed(members, (a, b), chart, cfg, deck_power=power)
    assert seen == ["leader-a", "leader-b"]


def test_passing_the_helper_as_the_callable_scores_on_the_servers_power():
    members, chart, cfg = _members(), _chart(), _cfg()
    leader = make_card("leader")
    score = score_song_detailed(
        members, (leader,), chart, cfg,
        deck_power=lambda ldr: overall_power_for(members, ldr, cfg),
    )[0]
    assert score.deck_power == overall_power_for(members, leader, cfg)


# ==========================================================================
# 3. The ranking path does not move
# ==========================================================================


def test_the_default_is_still_the_modelled_pool_bit_for_bit():
    """`==`, not `approx`. Every per-song number in the repo rests on this."""
    members, chart, cfg = _members(), _chart(), _cfg()
    leader = make_card("leader")
    before = score_song_detailed(members, (leader,), chart, cfg)[0]
    assert before.deck_power == pytest.approx(before.deck_power)
    again = score_song_detailed(members, (leader,), chart, cfg, deck_power=None)[0]
    assert again.total_score == before.total_score
    assert again.deck_power == before.deck_power


# ==========================================================================
# 4. The ranking-mode switch, through the one choke point
# ==========================================================================
#
# Every per-song ranking — one chart, across charts, serial and parallel —
# goes through `song_rank._score_one`, so the choice lives on the config and
# reaches all four paths at once rather than being threaded through four
# signatures that could drift.


def test_the_absolute_power_flag_defaults_ON():
    """Over 150 legal teams from one real pool on m0303 the server power is
    +3.4..5.2 % above the modelled pool and the ranking barely moves (Kendall
    tau 0.993, top-10 identical, same top-1), so this buys a truthful absolute
    number for no change of objective. The CLI header calls the figure an
    "expected run score"; with the modelled pool it would be about 4 % low."""
    assert make_scoring_config().song_absolute_deck_power is True


def test_the_flag_makes_a_ranked_row_report_the_servers_power():
    from hbuilder.domain.song_rank import rank_on_chart
    from hbuilder.domain.team import Team

    members, chart = _members(), _chart()
    team = Team(leader=make_card("leader"), members=members)

    off = rank_on_chart([team], chart, _cfg(song_absolute_deck_power=False))
    on = rank_on_chart([team], chart, _cfg())

    off_power = off.teams[0].per_chart[0].deck_power
    on_power = on.teams[0].per_chart[0].deck_power
    assert off_power < on_power
    assert on_power == overall_power_for(members, team.leader, _cfg())
    assert on.teams[0].expected_score > off.teams[0].expected_score


def test_the_flag_reaches_the_per_team_board_path_too():
    """`score_teams_on_chart` replaces the board per team (the develop-across case).
    Overall Power CARRIES the board's power half, so the power must be computed
    against that team's own board — not against the shared `cfg`'s."""
    from hbuilder.domain.song_rank import score_teams_on_chart
    from hbuilder.domain.team import Team

    members, chart = _members(), _chart()
    team = Team(leader=make_card("leader"), members=members)
    cfg = _cfg()

    rows = score_teams_on_chart([team], chart, cfg, boards=[None])
    assert rows[0].deck_power == overall_power_for(
        members, team.leader, dataclasses.replace(cfg, board=None)
    )


def test_a_float_still_means_the_same_float_for_every_leader():
    members, chart, cfg = _members(), _chart(), _cfg()
    a, b = make_card("leader-a"), make_card("leader-b")
    scores = score_song_detailed(members, (a, b), chart, cfg, deck_power=1234.0)
    assert [s.deck_power for s in scores] == [1234.0, 1234.0]


# ==========================================================================
# 3. The singer-conditional board stat rides on top of the server's power
# ==========================================================================

_SINGER_TECH = ("skill_tree_effect-technique_up_permil_up-100-skill_tree_effect_target-all"
                "-skill_tree_effect_passive_trigger-music_skill_tree_character-set_live_leader")


@pytest.mark.parametrize("absolute", [True, False])
def test_a_singer_conditional_red_node_scores_on_both_power_paths(absolute):
    """R-044/062/063 ("+stat % to all when this holomem sings") is a function of the
    song's cast, which Overall Power does not have. Taking the caller's Overall Power
    in place of `stat_sum + board_power` must not drop it (+0.00 % on the default path
    against +2.58 % on the modelled one, on m0334). A Lui holding all three nodes would
    be undervalued on every song she sings and leads."""
    from hbuilder.domain.board import BoardModel, BoardNode, CharacterBoard, parse_effect_id

    members, leader = _members(), make_card("leader", member="Singer")
    chart = dataclasses.replace(_chart(), singers=("Singer",), singer_type="solo")
    node = BoardNode("R-044", (parse_effect_id(_SINGER_TECH, branch="R"),))
    with_node = BoardModel(characters={"Singer": CharacterBoard("Singer", (node,))})
    without = BoardModel(characters={"Singer": CharacterBoard("Singer", ())})
    assert with_node.conditional_stat_power_for(members, "Singer", ("Singer",)) > 0

    scores = []
    for board in (without, with_node):
        cfg = _cfg(board=board, song_absolute_deck_power=absolute)
        power = (lambda ldr, cfg=cfg: overall_power_for(members, ldr, cfg)) if absolute else None
        scores.append(score_song_detailed(members, (leader,), chart, cfg, deck_power=power)[0])
    assert scores[1].total_score > scores[0].total_score
    # the same node, the same power on either path (it is one additive summand)
    assert scores[1].deck_power - scores[0].deck_power == pytest.approx(
        with_node.conditional_stat_power_for(members, "Singer", ("Singer",)))
    # and nothing on a song the holomem does not sing
    off = dataclasses.replace(chart, singers=("Someone else",))
    cfg = _cfg(board=with_node, song_absolute_deck_power=absolute)
    power = (lambda ldr: overall_power_for(members, ldr, cfg)) if absolute else None
    assert (score_song_detailed(members, (leader,), off, cfg, deck_power=power)[0].total_score
            == score_song_detailed(members, (leader,), off, _cfg(board=without,
                                   song_absolute_deck_power=absolute),
                                   deck_power=(lambda ldr: overall_power_for(
                                       members, ldr, _cfg(board=without,
                                                          song_absolute_deck_power=absolute)))
                                   if absolute else None)[0].total_score)
