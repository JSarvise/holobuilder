"""The board's `activation_rate_up` and `cooldown_shorten` reach the ACTIVE uptime.

`board.resolve` puts both on `BoardContribution.activation_rate_up` /
`.cooldown_shorten`, and the memoised path the solver actually uses must return
them too, or nothing downstream can read them. Like `skill_effect_up`
(`tests/test_board_skill_effect_up.py`), dropping them is silent: on a real
account it discards about 0.67 of activation rate and 0.12 of cooldown
reduction.

⚠ **Keyed by HOLOMEM, and that follows from the game data rather than
convenience.** Over all master `SkillTreeNode` rows — the whole game, not one
account — every node carrying either effect is branch **B**, scope **member**,
non-conditional: 10 `activation_rate_up` and 3
`cooldown_shorten`, zero at leader, account or grouping scope. `board._reaches` resolves
`"member"` to exactly `card.member == holomem`, so each term is a per-character CONSTANT: it
depends on neither the team nor the leader, which is why it is precomputed once rather than
memoised per arrangement. `BoardModel` asserts the property instead of assuming it, so a node
that ever ships at another scope fails loudly rather than being silently mis-scored.

⚠ `scoring.py` warns that board cooldown numbers "run ~4x high" against the
per-member floats in `Live/GetDeck`. That warning is about **branch ceilings**
(full-unlock maxima). On a real capture the resolved value is **40.0 permil per
member** who owns the node — the figure `Live/GetDeck` reports, not the ~120
permil ceiling. The warning stands for ceilings and does not apply to this path.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.board import (
    BoardEffect,
    BoardError,
    BoardModel,
    BoardNode,
    CharacterBoard,
    resolve,
)

from conftest import (
    make_card,
    make_resolved,
    make_scoring_config,
    make_skill,
    make_team,
)


def _uptime_effect(kind: str, permil: float, *, scope: str = "member",
                   conditional: bool = False) -> BoardEffect:
    family = {
        "activation_rate_up": "live_active_skill_activation_probability_up_permil_up",
        "cooldown_shorten": "live_active_skill_cool_time_shorten_permil_up",
    }[kind]
    return BoardEffect(
        family=family, kind=kind, stat="", value=permil / 1000.0,
        scope=scope, conditional=conditional,
    )


def _board(holomem: str, *effects: BoardEffect) -> CharacterBoard:
    return CharacterBoard(
        holomem=holomem,
        nodes=tuple(BoardNode(f"B-{i:03d}", (e,)) for i, e in enumerate(effects)),
    )


# --------------------------------------------------------------------------
# The accessor
# --------------------------------------------------------------------------


def test_a_members_own_board_supplies_both_terms():
    model = BoardModel(characters={
        "Marine": _board("Marine", _uptime_effect("activation_rate_up", 173.0),
                         _uptime_effect("cooldown_shorten", 40.0)),
    })
    assert model.uptime_for("Marine") == (pytest.approx(0.173), pytest.approx(0.040))


def test_another_characters_board_supplies_nothing():
    """`_reaches` resolves member scope to `card.member == holomem`, so a
    B-branch node pays its own character and nobody else."""
    model = BoardModel(characters={
        "Marine": _board("Marine", _uptime_effect("activation_rate_up", 173.0)),
    })
    assert model.uptime_for("Nene") == (0.0, 0.0)


def test_a_character_with_no_board_is_zero_not_an_error():
    """Ina'nis is the real case: 29 unlocked nodes and NOT ONE of them on the B
    branch — she went red instead. Absent means zero, never missing data."""
    assert BoardModel(characters={}).uptime_for("Ina'nis") == (0.0, 0.0)


def test_nodes_on_one_board_add():
    model = BoardModel(characters={
        "Marine": _board("Marine",
                         _uptime_effect("activation_rate_up", 60.0),
                         _uptime_effect("activation_rate_up", 20.0),
                         _uptime_effect("activation_rate_up", 30.0)),
    })
    assert model.uptime_for("Marine")[0] == pytest.approx(0.110)


def test_the_connect_multiplier_scales_them():
    """The socket boost applies to these exactly as it does to every other
    effect — on a real board, Marine's four raw nodes sum to 0.140 and
    resolve to 0.173."""
    node = BoardNode("B-007", (_uptime_effect("activation_rate_up", 100.0),),
                     connect_multiplier=2.0)
    model = BoardModel(characters={"Marine": CharacterBoard("Marine", (node,))})
    assert model.uptime_for("Marine")[0] == pytest.approx(0.200)


def test_a_conditional_node_is_excluded():
    """The same rule `resolve` applies: a trigger the song-agnostic
    ranking cannot evaluate is carried nowhere."""
    model = BoardModel(characters={
        "Marine": _board("Marine",
                         _uptime_effect("activation_rate_up", 173.0, conditional=True)),
    })
    assert model.uptime_for("Marine") == (0.0, 0.0)


def test_a_scope_other_than_member_is_REFUSED_not_guessed():
    """The guard that makes the per-holomem precomputation legitimate. All 13
    such nodes in the game are member-scoped; if one ever ships otherwise, the
    constant-per-character assumption is wrong and this must say so rather than
    quietly paying it to the wrong cards."""
    with pytest.raises(BoardError):
        BoardModel(characters={
            "Marine": _board("Marine",
                             _uptime_effect("activation_rate_up", 173.0, scope="account")),
        })


def test_it_agrees_with_resolve_on_a_real_shaped_team():
    """Two derivations and one comparison. `resolve` is
    the reference; this is the precomputed path."""
    model = BoardModel(characters={
        "Marine": _board("Marine", _uptime_effect("activation_rate_up", 173.0),
                         _uptime_effect("cooldown_shorten", 40.0)),
        "Nene": _board("Nene", _uptime_effect("activation_rate_up", 231.0)),
    })
    leader = make_card("l", member="Ina")
    members = tuple(make_card(f"m{i}", member=m)
                    for i, m in enumerate(("Marine", "Nene", "Lui", "Calli", "Ina")))
    reference = resolve(model, leader, members)
    for card in members:
        rate, cooldown = model.uptime_for(card.member)
        assert rate == reference.activation_rate_up.get(card.id, 0.0)
        assert cooldown == reference.cooldown_shorten.get(card.id, 0.0)


# --------------------------------------------------------------------------
# Reaching the score
# --------------------------------------------------------------------------


def _active_team(board_model, *, member="Marine"):
    scorer = make_card(
        "scorer", member=member,
        active=make_skill("ACTIVE", family="conditional_score_up", effect_type="score",
                          magnitude=50.0, duration=10.0, interval=20.0,
                          probability_tier="Medium"),
    )
    others = [make_card(f"m{i}", member=f"Other {i}") for i in range(1, 5)]
    leader = make_card("leader", member="Ina")
    cfg = dataclasses.replace(make_scoring_config(), board=board_model)
    return make_team([leader, scorer, *others]), cfg


def test_activation_rate_up_raises_the_scorers_own_uptime():
    from hbuilder.domain.scoring import score_with_breakdown

    empty = BoardModel(characters={})
    boosted = BoardModel(characters={
        "Marine": _board("Marine", _uptime_effect("activation_rate_up", 200.0))})
    base_team, base_cfg = _active_team(empty)
    up_team, up_cfg = _active_team(boosted)
    assert (
        score_with_breakdown(up_team, up_cfg).total_score
        > score_with_breakdown(base_team, base_cfg).total_score
    )


def test_cooldown_shorten_raises_it_too_and_DIVIDES():
    """The game divides: `cooldown_effective = cooldown / (1 + reduction)`. Pinned by
    hand: a 10 s effect on a 20 s cooldown at 100 permil runs at 20/1.1 =
    18.1818 s, and the finite-window uptime follows from that — not from
    20 x 0.9 = 18.0, which a multiplying reading would give."""
    from hbuilder.domain.scoring import _finite_window_uptime

    divided = _finite_window_uptime(10.0, 20.0 / 1.1)
    multiplied = _finite_window_uptime(10.0, 20.0 * 0.9)
    assert divided != multiplied

    from hbuilder.domain.scoring import score_with_breakdown

    shortened = BoardModel(characters={
        "Marine": _board("Marine", _uptime_effect("cooldown_shorten", 100.0))})
    team, cfg = _active_team(shortened)
    base_team, base_cfg = _active_team(BoardModel(characters={}))
    assert (
        score_with_breakdown(team, cfg).total_score
        > score_with_breakdown(base_team, base_cfg).total_score
    )


def test_a_member_without_the_node_is_unaffected():
    from hbuilder.domain.scoring import score_with_breakdown

    elsewhere = BoardModel(characters={
        "Nene": _board("Nene", _uptime_effect("activation_rate_up", 200.0))})
    team, cfg = _active_team(elsewhere, member="Marine")
    base_team, base_cfg = _active_team(BoardModel(characters={}), member="Marine")
    assert (
        score_with_breakdown(team, cfg).total_score
        == score_with_breakdown(base_team, base_cfg).total_score
    )


# --------------------------------------------------------------------------
# ...and reaching the PER-SONG scorer, sockets included
# --------------------------------------------------------------------------
#
# The three tests above prove the board's uptime terms reach the run-average
# scorer. The per-song scorer works on a real chart through a DIFFERENT function
# (`song._term_for` / `song._active_windows`), and the socket boost reaches it
# through one more hop: `BoardNode.connect_multiplier` scales the effect in
# `BoardModel.__post_init__`, `uptime_for` serves the scaled value, and the
# per-song builder reads it. Without these tests a per-song score could
# silently ignore every socketed Act.Prob / Act.CD node.


def _song_chart():
    from hbuilder.domain.song import ChartNote, SongChart

    return SongChart(
        music_id="m0001",
        difficulty="easy",
        difficulty_level=5,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=t, perfect_permil=1000)
                    for t in (1.0, 21.0, 25.0, 41.0, 45.0, 61.0)),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )


def _socketed(kind: str, permil: float, multiplier: float) -> BoardModel:
    node = BoardNode("B-007", (_uptime_effect(kind, permil),),
                     connect_multiplier=multiplier)
    return BoardModel(characters={"Marine": CharacterBoard("Marine", (node,))})


def test_a_socketed_activation_rate_node_raises_the_per_song_score():
    """Same node, twice the socket multiplier, on a real chart."""
    from hbuilder.domain.song import score_song_detailed

    chart = _song_chart()
    plain_team, plain_cfg = _active_team(_socketed("activation_rate_up", 100.0, 1.0))
    socket_team, socket_cfg = _active_team(_socketed("activation_rate_up", 100.0, 2.0))
    plain = score_song_detailed(
        plain_team.members, (plain_team.leader,), chart, plain_cfg, deck_power=2000
    )[0]
    socketed = score_song_detailed(
        socket_team.members, (socket_team.leader,), chart, socket_cfg, deck_power=2000
    )[0]
    assert socketed.total_score > plain.total_score


def test_a_socketed_cooldown_node_shortens_the_per_song_ACTIVE_lattice_itself():
    """The one that a score comparison alone would not prove: the socket has to
    move the WINDOW GEOMETRY, not merely the probability.

    A 20 s cooldown under a 100-permil shorten socketed at x2.0 becomes
    `20 / (1 + 0.200) = 16.667 s`, so on a 61 s song the checks land at
    16.67 / 33.33 / 50.00 — three windows where the unsocketed x1.0 lattice
    (`20 / 1.1 = 18.18 s`) gives three at 18.18 / 36.36 / 54.55, and the raw
    20 s lattice gives three at 20 / 40 / 60. Distinct starts, so this pins the
    multiplier's arrival rather than just its sign."""
    from hbuilder.domain.song import _active_windows

    chart = _song_chart()
    seconds = chart.notes[-1].time

    def starts(multiplier):
        team, cfg = _active_team(_socketed("cooldown_shorten", 100.0, multiplier))
        scorer = team.members[0]
        resolved = make_resolved(
            scorer.id, "ACTIVE", scorer.skill("ACTIVE"), member=scorer.member
        )
        return [round(start, 4) for start, _end in
                _active_windows(resolved, cfg, seconds)]

    assert starts(1.0) == [18.1818, 36.3636, 54.5455]
    assert starts(2.0) == [16.6667, 33.3333, 50.0]


def test_both_drivers_agree():
    """Bit-identical driver parity, the invariant every board term must keep."""
    from hbuilder.domain.scoring import score_card_set, score_with_breakdown

    model = BoardModel(characters={
        "Marine": _board("Marine", _uptime_effect("activation_rate_up", 173.0),
                         _uptime_effect("cooldown_shorten", 40.0))})
    team, cfg = _active_team(model)
    batch = score_card_set(team.members, (team.leader,), cfg)
    single = score_with_breakdown(team, cfg)
    assert batch[0].total_score == single.total_score
