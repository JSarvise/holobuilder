"""A PASSIVE that raises teammates' activation rate, and an ACTIVE behind a type gate.

Himemori Luna 5★ 0121's PASSIVE carries two effects: Sense UP to 2 Cute members (the slot's
own stat effect) and Skill Activation Rate UP 19 % to 2 Cute members (its rider), both on
"3 or more Cute". The rate joins each recipient's own activation-rate term
(`with_passive_activation_rates`), in the bracket the board's Act.Prob nodes use. Which two
Cute members the server picks is not measured: they are ranked as a count-limited Score
Support's are, by ACTIVE magnitude. Her ACTIVE and Sakura Miko 0120's fire only with
"2 or more Cute" members.
"""

from __future__ import annotations

import dataclasses

from support.builders import make_card, make_scoring_config, make_skill, make_team

from hbuilder.domain.card import EffectTarget, GateCondition, SlotRider
from hbuilder.domain.scoring import score_card_set, score_with_breakdown, with_passive_activation_rates
from hbuilder.domain.song import ChartNote, OverallPower, SongChart, score_song_detailed

CUTE3 = GateCondition(kind="type", target="Cute", count=3)
TWO_CUTE = EffectTarget(kind="type", value="Cute", count=2)


def _active(magnitude: float = 50.0, condition: GateCondition | None = None):
    return make_skill(
        "ACTIVE",
        family="conditional_score_up",
        effect_type="score",
        magnitude=magnitude,
        duration=10.0,
        interval=20.0,
        probability_tier="Medium",
        condition=condition,
    )


def _luna_passive(rate: float = 19.0):
    return make_skill(
        "PASSIVE",
        family="stat_up",
        effect_type="sense",
        magnitude=17.0,
        condition=CUTE3,
        target=TWO_CUTE,
        rider=SlotRider(
            family="activation_rate_up",
            effect_type="activation_rate",
            magnitude=rate,
            condition=CUTE3,
            target=TWO_CUTE,
        ),
    )


def _team(*, cute: int = 5, rate: float = 19.0):
    """Luna plus four members, `cute` of the five Cute; member i's ACTIVE is 10 x (i + 1)."""
    luna = make_card("luna", member="Luna", type_="Cute", active=_active(140.0), passive=_luna_passive(rate))
    others = [
        make_card(
            f"m{i}",
            member=f"Member {i}",
            type_="Cute" if i < cute - 1 else "Cool",
            active=_active(10.0 * (i + 1)),
        )
        for i in range(4)
    ]
    return make_team([make_card("lead", member="Lead"), luna, *others])


def test_the_rate_reaches_the_two_cute_members_with_the_strongest_active():
    team = _team()
    cfg = with_passive_activation_rates(team.members, make_scoring_config())
    assert cfg.board is not None
    got = {m: cfg.board.uptime_for(m)[0] for m in ("Luna", "Member 0", "Member 1", "Member 2", "Member 3")}
    # Luna (ACTIVE 140) and Member 3 (40) are the two strongest Cute ACTIVEs
    assert got == {"Luna": 0.19, "Member 0": 0.0, "Member 1": 0.0, "Member 2": 0.0, "Member 3": 0.19}


def test_it_raises_the_score_and_only_past_its_gate():
    base = make_scoring_config()
    with_rate = score_with_breakdown(_team(), base).total_score
    without = score_with_breakdown(_team(rate=0.0), base).total_score
    assert with_rate > without
    # two Cute members: the gate (3+) is not met, the rider adds nothing
    assert with_passive_activation_rates(_team(cute=2).members, base) is base


def test_a_team_without_the_passive_is_scored_on_the_same_config():
    cfg = make_scoring_config()
    team = make_team([make_card(f"c{i}", member=f"M{i}", active=_active()) for i in range(6)])
    assert with_passive_activation_rates(team.members, cfg) is cfg


def test_the_three_drivers_agree():
    team = _team()
    cfg = make_scoring_config()
    single = score_with_breakdown(team, cfg).total_score
    batch = score_card_set(team.members, (team.leader,), cfg)[0].total_score
    assert batch == single
    chart = SongChart(
        music_id="m0001",
        difficulty="easy",
        difficulty_level=5,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=t, perfect_permil=1000) for t in (1.0, 21.0, 25.0, 41.0, 45.0, 61.0)),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )
    power = OverallPower.fixed(2000)
    up = score_song_detailed(team.members, (team.leader,), chart, cfg, deck_power=power)[0].total_score
    flat = score_song_detailed(_team(rate=0.0).members, (team.leader,), chart, cfg, deck_power=power)[
        0
    ].total_score
    assert up > flat


def test_an_active_behind_a_type_gate_fires_only_with_the_cute_members():
    """Sakura Miko 0120's ACTIVE: "With 2 or more Cute Type Members"."""
    miko = make_card(
        "miko", member="Miko", type_="Cute", active=_active(130.0, GateCondition("type", "Cute", 2))
    )

    def team(other_type: str):
        others = [
            make_card(f"o{i}", member=f"O{i}", type_=other_type, active=_active(10.0)) for i in range(4)
        ]
        return make_team([make_card("lead", member="Lead"), miko, *others])

    cfg = dataclasses.replace(make_scoring_config())
    alone = score_with_breakdown(team("Cool"), cfg)
    with_cute = score_with_breakdown(team("Cute"), cfg)
    state = {c.slot_kind: c.state for c in alone.contributions if c.card_id == "miko"}
    assert state["ACTIVE"] == "gate_not_met"
    assert with_cute.total_score > alone.total_score
