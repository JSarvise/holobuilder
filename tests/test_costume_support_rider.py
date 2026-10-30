"""A leader outfit's `score_support` RIDER is a second, always-on, team-wide
support (objective-v2.2).

Several outfits in `data/cards.json` carry a `costume_skill.rider` of family
`score_support` (effect_type `score`, magnitude 25.0, target all, gated on 2+
of a type — e.g. Mori Calliope "Radiant Summer Heatwave": sense +80 % gated 2+
Cute AND Score Support 25 % gated 2+ Cute). A scorer that produced only ONE
`ResolvedSkill` per leader outfit would silently drop the 25 % support.

The treatment here is the one a PRINCIPAL costume `score_support` already gets
(no separate ratio exists in code — `family_weights["score_support"]` is 1.0
and the occupancy is `_always_on_occupancy`'s 1.0): a synthesized COSTUME
`score_support` slot, resolved against the FIVE MEMBERS' aggregates
(`gates.py`), joining `B` in the ACTIVE bucket.
"""

from __future__ import annotations

import dataclasses

from hbuilder.domain.card import GateCondition, SlotRider, scored_costume_rider_slot
from hbuilder.domain.gates import (
    resolve_leader_costume,
    resolve_leader_costume_skills,
    resolve_team_gates,
    aggregate,
)
from hbuilder.domain.scoring import score_card_set, score_fast, score_with_breakdown
from hbuilder.domain.song import ChartNote, SongChart, score_song_detailed
from hbuilder.domain.team import Team
from hbuilder.solver.dominance import dominant_leaders, outfit_signature

from conftest import DEFAULT_RUN_STATE, make_card, make_scoring_config, make_skill

CUTE2 = GateCondition(kind="type", target="Cute", count=2)
CERTAIN = {"Low": 1.0, "Medium": 1.0, "High": 1.0}

RIDER = SlotRider(
    family="score_support", effect_type="score", magnitude=25.0, condition=CUTE2, target=None
)


def _outfit(rider):
    # Radiant Summer Heatwave's shape: sense +80 gated 2+ Cute, plus the rider.
    return make_skill(
        "COSTUME", family="stat_up", effect_type="sense", magnitude=80.0,
        condition=CUTE2, rider=rider,
    )


def _leader(rider):
    return make_card("card-lead", type_="Cute", costume_skill=_outfit(rider), member="Lead")


def _members(cute: int):
    """Five members, `cute` of them Cute, an ACTIVE Score UP on members 0 and 1."""
    active = make_skill("ACTIVE", family="conditional_score_up", effect_type="score",
                        magnitude=50.0, duration=10.0, interval=20.0, probability_tier="High")
    cards = []
    for i in range(5):
        cards.append(make_card(
            f"card-m{i}", type_="Cute" if i < cute else "Happy",
            active=active if i < 2 else None, member=f"M{i}",
        ))
    return tuple(cards)


def _cfg():
    return make_scoring_config(probability_tiers=CERTAIN)


def _team(rider, cute):
    return Team(leader=_leader(rider), members=_members(cute))


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------
def test_scored_costume_rider_slot_is_a_costume_score_support_slot_with_the_riders_own_gate():
    slot = scored_costume_rider_slot(_outfit(RIDER))
    assert slot is not None
    assert slot.kind == "COSTUME"
    assert slot.family == "score_support" and slot.effect_type == "score"
    assert slot.magnitude == 25.0 and slot.condition == CUTE2 and slot.target is None
    assert slot.override is None and slot.rider is None
    # any other rider family, or no rider: nothing to score, unchanged
    assert scored_costume_rider_slot(_outfit(None)) is None
    other = dataclasses.replace(RIDER, family="life_recovery", effect_type="life")
    assert scored_costume_rider_slot(_outfit(other)) is None


def test_leader_costume_skills_emits_principal_then_rider_and_the_single_costume_api_is_unchanged():
    leader = _leader(RIDER)
    agg = aggregate(_members(cute=2))
    skills = resolve_leader_costume_skills(leader, agg, DEFAULT_RUN_STATE)
    assert [s.source for s in skills] == ["costume", "costume_rider"]
    assert skills[0] == resolve_leader_costume(leader, agg, DEFAULT_RUN_STATE)
    rider = skills[1]
    assert rider.family == "score_support" and rider.slot_kind == "costume"
    assert rider.gate is not None and rider.gate.fired
    # ...and the rider's gate is resolved against the FIVE MEMBERS: a Cute
    # leader with one Cute member does not fire a 2+ Cute rider on its own.
    skills1 = resolve_leader_costume_skills(leader, aggregate(_members(cute=1)), DEFAULT_RUN_STATE)
    assert not skills1[1].gate.fired
    # a rider-less outfit yields exactly one entry
    assert len(resolve_leader_costume_skills(_leader(None), agg, DEFAULT_RUN_STATE)) == 1
    # resolve_team_gates and Team.effective_skill_instances() stay in lockstep
    team = _team(RIDER, 2)
    resolved = resolve_team_gates(team, DEFAULT_RUN_STATE)
    instances = team.effective_skill_instances()
    assert len(resolved) == len(instances)
    assert [(r.card_id, r.slot_kind, r.source) for r in resolved] == [
        (i.card_id, i.slot_kind, i.source) for i in instances
    ]


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------
def test_gate_not_met_the_rider_adds_nothing():
    cfg = _cfg()
    with_rider = score_with_breakdown(_team(RIDER, cute=1), cfg)
    without = score_with_breakdown(_team(None, cute=1), cfg)
    assert with_rider.combined_support_rate == without.combined_support_rate == 0.0
    assert with_rider.total_score == without.total_score
    rows = [c for c in with_rider.contributions if c.source == "costume_rider"]
    assert len(rows) == 1 and rows[0].state == "gate_not_met" and rows[0].pool == "support"


def test_gate_met_the_rider_is_a_team_wide_support_on_every_active():
    cfg = _cfg()
    with_rider = score_with_breakdown(_team(RIDER, cute=2), cfg)
    without = score_with_breakdown(_team(None, cute=2), cfg)
    # exactly the rider's folded amount: magnitude/100 x family_weights (1.0) x occupancy 1.0
    assert without.combined_support_rate == 0.0
    assert with_rider.combined_support_rate == 0.25 * cfg.family_weights["score_support"]
    # ...and it multiplies the ACTIVE maximum: (1 + B) x max_ACTIVE
    assert without.combined_score_rate > 0.0
    assert abs(with_rider.combined_score_rate - 1.25 * without.combined_score_rate) < 1e-12
    assert with_rider.total_score > without.total_score
    rows = [c for c in with_rider.contributions if c.source == "costume_rider"]
    assert len(rows) == 1
    row = rows[0]
    assert row.state == "applied" and row.pool == "support" and row.family == "score_support"
    assert row.card_id == "card-lead" and row.slot_kind == "costume"
    assert row.rate == 0.25 and row.gate_fired is True
    # the principal (sense +80) is still there and unchanged
    principal = [c for c in with_rider.contributions if c.source == "costume"]
    assert len(principal) == 1 and principal[0].family == "stat_up"
    assert with_rider.effective_stat_sum == without.effective_stat_sum
    # score_fast agrees with the breakdown path
    assert score_fast(_team(RIDER, 2), cfg) == with_rider.total_score


def test_a_leader_without_a_rider_is_bit_identical_to_the_v2_1_pin():
    """One float pinned from the pre-v2.2 scorer on the shipped fixture team
    (`tests/fixtures/cards.json`, leader `nene_01`, members the other five, under
    `make_scoring_config()` defaults): the change must be a no-op for every
    leader whose outfit has no `score_support` rider."""
    from hbuilder.domain.loader import load_cards
    from conftest import FIXTURE_CARDS_PATH

    cards = load_cards(FIXTURE_CARDS_PATH)
    assert not any(
        isinstance(getattr(c.costume_skill, "rider", None), SlotRider) for c in cards
    ), "the fixture must stay rider-free for this pin to mean anything"
    team = Team(leader=cards[0], members=tuple(cards[1:6]))
    cfg = make_scoring_config()
    assert score_fast(team, cfg) == PINNED_V2_1_FIXTURE_SCORE
    # ...and the batch entry point over the same six candidate leaders
    assert [s.total_score for s in score_card_set(tuple(cards[1:6]), tuple(cards), cfg)] == (
        [PINNED_V2_1_FIXTURE_SCORE] * 6
    )


# Pinned by running the v2.1 scorer (commit 41471a5) on the fixture team above:
# `score_fast(Team(cards[0], cards[1:6]), make_scoring_config())`.
PINNED_V2_1_FIXTURE_SCORE = 8402.795174252431


def test_per_song_total_rises_with_the_rider():
    times = tuple(float(i) for i in range(60))
    chart = SongChart(
        music_id="m9002", difficulty="EXPERT", difficulty_level=5,
        live_score_coefficient_permil=4,
        notes=tuple(ChartNote(time=t, perfect_permil=1000) for t in times),
        special_cues=(10.0, 20.0, 30.0, 40.0, 50.0),
    )
    cfg = _cfg()
    members = _members(cute=2)
    (with_rider,) = score_song_detailed(members, (_leader(RIDER),), chart, cfg, deck_power=10000.0)
    (without,) = score_song_detailed(members, (_leader(None),), chart, cfg, deck_power=10000.0)
    assert with_rider.total_score > without.total_score
    # gate not met: identical
    members1 = _members(cute=1)
    (a,) = score_song_detailed(members1, (_leader(RIDER),), chart, cfg, deck_power=10000.0)
    (b,) = score_song_detailed(members1, (_leader(None),), chart, cfg, deck_power=10000.0)
    assert a.total_score == b.total_score


# ---------------------------------------------------------------------------
# dominance
# ---------------------------------------------------------------------------
def test_dominance_keeps_two_outfits_that_differ_only_by_the_rider():
    with_rider = _leader(RIDER)
    bigger_no_rider = dataclasses.replace(
        make_card("card-lead2", type_="Cute", member="Lead2",
                  costume_skill=make_skill("COSTUME", family="stat_up", effect_type="sense",
                                           magnitude=100.0, condition=CUTE2)),
    )
    assert outfit_signature(with_rider) != outfit_signature(bigger_no_rider)
    kept = dominant_leaders((with_rider, bigger_no_rider))
    assert {c.id for c in kept} == {"card-lead", "card-lead2"}
