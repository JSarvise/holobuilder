"""The board's `skill_effect_up` reaches the score.

`board_score_rate` is fed by exactly one effect family, `song_score_bonus`
with `song_scope == "all"`, which is a team-INVARIANT account constant. The
team's own leader-scope `skill_effect_up` (Ina'nis' alone is **173 permil**)
is a different term, and the largest single board term on a team.

Where it belongs comes from the client. `CalcScoreUpRatio`'s body is

    v(e) = (1 + SkillEffectUpPermilUp/1000 + B) x ScoreUpByRatio(e)

so it shares the ACTIVE pool's multiplier with `B` (Score Support). That is the
same slot `_score_up_pool` already builds. Every board effect carries its own
explicit `scope`, and `board._recipients` reads it, so the recipients of this
term are not ambiguous.

⚠⚠ **These tests pin the model's own arithmetic, never the deck screen's
board line.** The deck screen shows base-magnitude Actives and the board's
score line multiplies the Active ratio, so it inherits that contamination and
is not a valid oracle for this term. Do not fit against it.
"""

from __future__ import annotations

import dataclasses

import pytest

from hbuilder.domain.board import (
    BoardEffect,
    BoardModel,
    BoardNode,
    CharacterBoard,
    resolve,
)

from conftest import make_card, make_scoring_config, make_team

LEADER_SCOPE_PERMIL = 173.0  # Ina'nis' own leader-scope value
MEMBER_SCOPE_PERMIL = 40.0


def _skill_effect_up(permil: float, scope: str, *, conditional: bool = False):
    return BoardEffect(
        family="live_active_skill_effect_up_permil_up",
        kind="skill_effect_up",
        stat="",
        value=permil / 1000.0,
        scope=scope,
        conditional=conditional,
    )


def _model(*effects_by_holomem):
    """`_model(("Ina", effect), ("Nene", effect))` -> a one-node board each."""
    characters = {}
    for index, (holomem, effect) in enumerate(effects_by_holomem):
        characters[holomem] = CharacterBoard(
            holomem=holomem, nodes=(BoardNode(f"R-{index:03d}", (effect,)),)
        )
    return BoardModel(characters=characters)


def _team(leader_member="Ina"):
    leader = make_card("leader", member=leader_member)
    members = [make_card(f"m{i}", member=f"Member {i}") for i in range(1, 6)]
    return make_team([leader, *members])


# --------------------------------------------------------------------------
# The accessor: same rule as `resolve`, memoised for the solver's hot loop
# --------------------------------------------------------------------------


def test_a_leader_scope_node_pays_only_under_its_own_leader():
    """`_recipients`: a red node applies to the whole team, but only while that
    holomem leads. This is the term worth 173 permil on a team Ina'nis leads."""
    model = _model(("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader")))
    team = _team(leader_member="Ina")
    assert model.skill_effect_up_for(
        team.members, "Ina"
    ) == pytest.approx(LEADER_SCOPE_PERMIL / 1000.0)


def test_a_leader_scope_node_pays_nothing_under_a_different_leader():
    model = _model(("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader")))
    team = _team(leader_member="Nene")
    assert model.skill_effect_up_for(team.members, "Nene") == 0.0


def test_a_leader_scope_node_pays_nothing_with_no_leader():
    """The leader-invariant answer, which is what `score_card_set` needs before
    it knows which card leads."""
    model = _model(("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader")))
    team = _team()
    assert model.skill_effect_up_for(team.members, None) == 0.0


def test_it_is_counted_ONCE_for_the_team_not_once_per_member():
    """`resolve`'s own rule, and the game's: `SkillEffectUpPermilUp` is a single
    addend inside `CalcScoreUpRatio`'s parenthesis. Paying it per recipient
    would multiply this term by five."""
    model = _model(("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader")))
    team = _team(leader_member="Ina")
    assert model.skill_effect_up_for(team.members, "Ina") == pytest.approx(
        LEADER_SCOPE_PERMIL / 1000.0
    )


def test_a_conditional_node_is_excluded():
    """A trigger the song-agnostic ranking cannot evaluate is carried
    nowhere rather than counted. Ina's conditional 240 permil is real and is
    NOT this term."""
    model = _model(("Ina", _skill_effect_up(240.0, "leader", conditional=True)))
    team = _team(leader_member="Ina")
    assert model.skill_effect_up_for(team.members, "Ina") == 0.0


def test_two_boards_add():
    model = _model(
        ("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader")),
        ("Member 1", _skill_effect_up(MEMBER_SCOPE_PERMIL, "account")),
    )
    team = _team(leader_member="Ina")
    assert model.skill_effect_up_for(team.members, "Ina") == pytest.approx(
        (LEADER_SCOPE_PERMIL + MEMBER_SCOPE_PERMIL) / 1000.0
    )


def test_the_memoised_accessor_agrees_with_resolve_bit_for_bit():
    """Two derivations and one comparison, not one
    derivation that reports itself. `resolve` stays the reference; this is the
    hot-loop path, and they must not drift."""
    model = _model(
        ("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader")),
        ("Member 1", _skill_effect_up(MEMBER_SCOPE_PERMIL, "account")),
        ("Member 2", _skill_effect_up(12.5, "account")),
    )
    for leader_member in ("Ina", "Member 1", "Nene"):
        team = _team(leader_member=leader_member)
        reference = resolve(model, team.leader, team.members).skill_effect_up
        assert model.skill_effect_up_for(team.members, leader_member) == reference


# --------------------------------------------------------------------------
# Reaching the score: it multiplies the ACTIVE pool, it is not a summand
# --------------------------------------------------------------------------


def _active_card(card_id="scorer", member="Member 1"):
    from conftest import make_skill

    return make_card(
        card_id,
        member=member,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            magnitude=50.0,
            duration=10.0,
            interval=10.0,
            probability_tier="Medium",
        ),
    )


def _cfg_with(model):
    return dataclasses.replace(make_scoring_config(), board=model)


def test_the_board_term_raises_a_team_with_an_active_skill():
    """The term in one assertion. Same team, same board, the only difference
    being whether the 173-permil node exists."""
    scorer = _active_card()
    others = [make_card(f"m{i}", member=f"Member {i}") for i in range(2, 6)]
    leader = make_card("leader", member="Ina")
    team = make_team([leader, scorer, *others])

    without = _cfg_with(_model(("Ina", _skill_effect_up(0.0, "leader"))))
    with_it = _cfg_with(_model(("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader"))))

    from hbuilder.domain.scoring import score_with_breakdown

    base = score_with_breakdown(team, without)
    lifted = score_with_breakdown(team, with_it)
    assert lifted.total_score > base.total_score


def test_it_multiplies_the_active_pool_rather_than_adding_to_the_score_rate():
    """From the client's `CalcScoreUpRatio`: `(1 + SkillEffectUpPermilUp/1000 + B)`
    multiplies `ScoreUpByRatio(e)`. So a team with NO active Score UP gains
    exactly nothing from this term — an additive reading would still lift it,
    and that is the one assertion that separates the two."""
    plain = make_card("plain", member="Member 1")
    others = [make_card(f"m{i}", member=f"Member {i}") for i in range(2, 6)]
    leader = make_card("leader", member="Ina")
    team = make_team([leader, plain, *others])

    from hbuilder.domain.scoring import score_with_breakdown

    without = _cfg_with(_model(("Ina", _skill_effect_up(0.0, "leader"))))
    with_it = _cfg_with(_model(("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader"))))
    assert (
        score_with_breakdown(team, with_it).total_score
        == score_with_breakdown(team, without).total_score
    )


def test_the_lift_is_exactly_the_attested_multiplier():
    """Pinned by hand, not copied from the implementation. With one ACTIVE Score
    UP and no Score Support, `B = 0`, so the pool goes from `A` to
    `(1 + 0.173) * A` and the score rate moves by exactly that factor."""
    scorer = _active_card()
    others = [make_card(f"m{i}", member=f"Member {i}") for i in range(2, 6)]
    leader = make_card("leader", member="Ina")
    team = make_team([leader, scorer, *others])

    from hbuilder.domain.scoring import score_with_breakdown

    base = score_with_breakdown(
        team, _cfg_with(_model(("Ina", _skill_effect_up(0.0, "leader"))))
    )
    lifted = score_with_breakdown(
        team, _cfg_with(_model(("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader"))))
    )
    assert lifted.combined_score_rate == pytest.approx(
        base.combined_score_rate * (1.0 + LEADER_SCOPE_PERMIL / 1000.0)
    )


def test_both_drivers_agree(monkeypatch):
    """Bit-identical parity: `score_card_set` and `_score_team` must read
    this term through the same function, not through two summing loops."""
    from hbuilder.domain.scoring import score_card_set, score_with_breakdown

    scorer = _active_card()
    others = [make_card(f"m{i}", member=f"Member {i}") for i in range(2, 6)]
    leader = make_card("leader", member="Ina")
    members = (scorer, *others)
    cfg = _cfg_with(_model(("Ina", _skill_effect_up(LEADER_SCOPE_PERMIL, "leader"))))

    batch = score_card_set(members, (leader,), cfg)
    single = score_with_breakdown(make_team([leader, *members]), cfg)
    assert batch[0].total_score == single.total_score
