"""R-064: a red node whose activation rate reaches every member while its holomem leads.

Master `SkillTreeNode` R-064 (`...activation_probability_up_permil_up-100-skill_tree_effect_
target-all-set_live_leader`) is the first activation-rate node outside branch B. It is not a
per-character constant like the blue ones (`uptime_for`): it depends on who leads, and then
adds to every member's rate, in the same bracket as the board's own rate and the SPECIAL
riders (`CalcActivationProbabilityPermil`). The model carries it as
`BoardModel.leader_activation_rate`, and every scorer adds it to the team's `team_rate_up`.
"""

from __future__ import annotations

import dataclasses

import pytest
from support.builders import make_card, make_scoring_config, make_skill, make_team

from hbuilder.domain.board import BoardEffect, BoardError, BoardModel, BoardNode, CharacterBoard
from hbuilder.domain.scoring import score_card_set, score_with_breakdown
from hbuilder.domain.song import ChartNote, OverallPower, SongChart, score_song_detailed

RATE = "live_active_skill_activation_probability_up_permil_up"
SHORTEN = "live_active_skill_cool_time_shorten_permil_up"


def _effect(
    permil: float, *, scope: str = "leader", kind: str = "activation_rate_up", conditional: bool = False
) -> BoardEffect:
    family = RATE if kind == "activation_rate_up" else SHORTEN
    return BoardEffect(
        family=family, kind=kind, stat="", value=permil / 1000.0, scope=scope, conditional=conditional
    )


def _board(holomem: str, *effects: BoardEffect) -> CharacterBoard:
    return CharacterBoard(
        holomem=holomem, nodes=tuple(BoardNode(f"R-{i:03d}", (e,)) for i, e in enumerate(effects))
    )


def _active(card_id: str, member: str, tier: str = "Medium"):
    return make_card(
        card_id,
        member=member,
        active=make_skill(
            "ACTIVE",
            family="conditional_score_up",
            effect_type="score",
            magnitude=50.0,
            duration=10.0,
            interval=20.0,
            probability_tier=tier,
        ),
    )


MEMBERS = tuple(_active(f"m{i}", m) for i, m in enumerate(("Marine", "Nene", "Lui", "Calli", "Ina")))
MODEL = BoardModel(characters={"Pekora": _board("Pekora", _effect(100.0))})


def _cfg(model: BoardModel = MODEL):
    return dataclasses.replace(make_scoring_config(), board=model)


def _chart() -> SongChart:
    return SongChart(
        music_id="m0001",
        difficulty="easy",
        difficulty_level=5,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=t, perfect_permil=1000) for t in (1.0, 21.0, 25.0, 41.0, 45.0, 61.0)),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )


def test_the_model_routes_it_by_leader_and_keeps_it_out_of_uptime_for():
    assert MODEL.leader_activation_rate("Pekora") == pytest.approx(0.100)
    assert MODEL.leader_activation_rate("Marine") == 0.0
    assert MODEL.leader_activation_rate(None) == 0.0
    assert MODEL.uptime_for("Pekora") == (0.0, 0.0)  # not her own card's term


def test_her_leading_raises_every_members_active_and_her_membership_does_not():
    pekora_leads = make_team([make_card("lead", member="Pekora"), *MEMBERS])
    other_leads = make_team([make_card("lead", member="Suisei"), *MEMBERS])
    cfg = _cfg()
    led = score_with_breakdown(pekora_leads, cfg).total_score
    plain = score_with_breakdown(other_leads, cfg).total_score
    assert led > plain
    # under another leader the node pays nothing: the same score as with no board at all
    assert plain == score_with_breakdown(other_leads, make_scoring_config()).total_score
    # Pekora as a MEMBER (not leading) adds nothing either: the node is the leader's
    with_pekora = make_team([make_card("lead", member="Suisei"), *MEMBERS[:4], _active("p", "Pekora")])
    assert (
        score_with_breakdown(with_pekora, cfg).total_score
        == score_with_breakdown(with_pekora, make_scoring_config()).total_score
    )


def test_the_probability_is_capped_at_one():
    """Past certainty a larger leader rate fires no more checks."""
    big = BoardModel(characters={"Pekora": _board("Pekora", _effect(5000.0))})
    huge = BoardModel(characters={"Pekora": _board("Pekora", _effect(9000.0))})
    team = make_team([make_card("lead", member="Pekora"), *MEMBERS])
    assert (
        score_with_breakdown(team, _cfg(big)).total_score
        == score_with_breakdown(team, _cfg(huge)).total_score
    )


def test_a_batch_of_mixed_leaders_scores_each_as_its_own_team_does():
    """`score_card_set` batches leaders by the rate they add; each answer is bit-identical
    to scoring that leader's team alone."""
    leaders = (
        make_card("a", member="Suisei"),
        make_card("b", member="Pekora"),
        make_card("c", member="Miko"),
        make_card("d", member="Pekora"),
    )
    cfg = _cfg()
    batch = score_card_set(MEMBERS, leaders, cfg)
    for leader, got in zip(leaders, batch, strict=True):
        assert got.leader_id == leader.id
        assert got.total_score == score_with_breakdown(make_team([leader, *MEMBERS]), cfg).total_score
    assert batch[1].total_score > batch[0].total_score


def test_the_song_scorer_with_mixed_leaders_matches_one_call_per_leader():
    leaders = (
        make_card("a", member="Suisei"),
        make_card("b", member="Pekora"),
        make_card("c", member="Miko"),
    )
    cfg = _cfg()
    chart = _chart()
    power = OverallPower.fixed(2000)
    together = score_song_detailed(MEMBERS, leaders, chart, cfg, deck_power=power)
    alone = [score_song_detailed(MEMBERS, (leader,), chart, cfg, deck_power=power)[0] for leader in leaders]
    assert [s.total_score for s in together] == [s.total_score for s in alone]
    assert together[1].total_score > together[0].total_score  # Pekora's node fires
    # a held team setup is rebuilt when the next leader adds another rate
    setup: dict = {}
    held = [
        score_song_detailed(MEMBERS, (leader,), chart, cfg, deck_power=power, team_setup=setup)[0]
        for leader in leaders
    ]
    assert [s.total_score for s in held] == [s.total_score for s in alone]


@pytest.mark.parametrize(
    "effect",
    [
        _effect(100.0, kind="cooldown_shorten"),
        _effect(100.0, conditional=True),
        _effect(100.0, scope="grouping"),
        _effect(100.0, scope="account"),
    ],
    ids=["leader cooldown", "conditional leader rate", "grouping rate", "account rate"],
)
def test_any_other_uptime_shape_is_still_refused(effect):
    with pytest.raises(BoardError):
        BoardModel(characters={"Pekora": _board("Pekora", effect)})
