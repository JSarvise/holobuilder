"""DOM-03/DOM-04 coverage: malformed team rejection and leader
costume-skill layering.

Card fixtures are built in-memory here, independent of `data/cards.json`
and of plan 01-04's fixture expansion.
"""

from __future__ import annotations

import pytest

from hbuilder.domain.card import Card, GateCondition, SkillSlot, Stats
from hbuilder.domain.team import (
    MEMBER_COUNT,
    TeamError,
    Team,
    build_team,
)

_STATS = Stats(performance=100, technique=100, sense=100)


def _skill(kind: str, family: str = "score_support") -> SkillSlot:
    return SkillSlot(
        kind=kind,
        family=family,
        effect_type="score",
        duration=None,
        interval=None,
        probability_tier=None,
        magnitude=10,
        condition=None,
        target=None,
        override=None,
        rider=None,
    )


def _card(
    card_id: str,
    *,
    generations: frozenset[str] = frozenset({"1期生"}),
    type_: str = "Cute",
    special: SkillSlot | None = None,
    active: SkillSlot | None = None,
    passive: SkillSlot | None = None,
    costume_skill: SkillSlot | None = None,
    member: str | None = None,
) -> Card:
    # Two cards may share a holomem (two skins of one character); the default
    # keeps distinct ids distinct unless a test says otherwise.
    return Card(
        id=card_id,
        member=member if member is not None else f"Member {card_id}",
        generations=generations,
        type=type_,
        rarity="SSR",
        costume_name=f"Costume {card_id}",
        stats=_STATS,
        skills=(special, active, passive),
        costume_skill=costume_skill,
    )


def _plain_cards(n: int) -> list[Card]:
    return [_card(f"card_{i}") for i in range(n)]


def _valid_leader_and_members() -> tuple[Card, tuple[Card, ...]]:
    leader = _card("leader_01", costume_skill=_skill("COSTUME", "life_recovery"))
    members = tuple(_card(f"member_{i:02d}") for i in range(1, MEMBER_COUNT + 1))
    return leader, members


# --- Malformed team shape ---------------------------------------------


def test_four_members_raises_team_error_stating_4_and_5():
    leader = _card("leader_01")
    members = tuple(_card(f"member_{i}") for i in range(4))
    with pytest.raises(TeamError) as exc_info:
        Team(leader=leader, members=members)
    message = str(exc_info.value)
    assert "4" in message
    assert "5" in message


def test_six_members_raises_team_error_stating_6_and_5():
    leader = _card("leader_01")
    members = tuple(_card(f"member_{i}") for i in range(6))
    with pytest.raises(TeamError) as exc_info:
        Team(leader=leader, members=members)
    message = str(exc_info.value)
    assert "6" in message
    assert "5" in message


def test_zero_members_raises_team_error():
    leader = _card("leader_01")
    with pytest.raises(TeamError):
        Team(leader=leader, members=())


def test_leader_may_also_be_one_of_the_five_members():
    """The leader slot is the OUTFIT applied to the
    team, and that outfit may come from a card that is among the 5 members or
    one that is not. It is a slot outside the five, so it does not collide
    with them."""
    shared = _card("shared_01")
    members = (
        shared,
        _card("member_02"),
        _card("member_03"),
        _card("member_04"),
        _card("member_05"),
    )

    team = Team(leader=shared, members=members)

    assert team.leader is shared
    assert team.members[0] is shared


def test_two_skins_of_the_same_holomem_cannot_both_be_members():
    """R1: a holomem cannot repeat among the members.
    Distinct card ids are NOT enough — two skins of one character share a
    holomem and are still forbidden."""
    members = (
        _card("nene_summer", member="Nene"),
        _card("nene_winter", member="Nene"),
        _card("member_03"),
        _card("member_04"),
        _card("member_05"),
    )

    with pytest.raises(TeamError) as exc_info:
        Team(leader=_card("leader_01"), members=members)

    assert "Nene" in str(exc_info.value)


def test_the_leader_may_share_a_holomem_with_a_member():
    """R2: the leader slot is exempt from R1 — it is not a member."""
    members = (
        _card("nene_summer", member="Nene"),
        _card("member_02"),
        _card("member_03"),
        _card("member_04"),
        _card("member_05"),
    )

    team = Team(leader=_card("nene_winter", member="Nene"), members=members)

    assert team.leader.member == "Nene"


def test_duplicate_member_id_raises_team_error_naming_id():
    leader = _card("leader_01")
    members = (
        _card("dup_02"),
        _card("dup_02"),
        _card("member_03"),
        _card("member_04"),
        _card("member_05"),
    )
    with pytest.raises(TeamError) as exc_info:
        Team(leader=leader, members=members)
    assert "dup_02" in str(exc_info.value)


def test_build_team_unknown_leader_id_raises_team_error_naming_id():
    cards = _plain_cards(6)
    with pytest.raises(TeamError) as exc_info:
        build_team(cards, "nonexistent_leader", [c.id for c in cards[:5]])
    assert "nonexistent_leader" in str(exc_info.value)


def test_build_team_unknown_member_id_raises_team_error_naming_id():
    cards = _plain_cards(6)
    member_ids = [cards[1].id, cards[2].id, cards[3].id, cards[4].id, "nonexistent_member"]
    with pytest.raises(TeamError) as exc_info:
        build_team(cards, cards[0].id, member_ids)
    assert "nonexistent_member" in str(exc_info.value)


# --- Leader costume skill layering --------------------------------------


def test_leader_contributes_its_outfit_skill_and_nothing_else():
    """The card in the leader/costume
    slot does NOT contribute its SPECIAL/ACTIVE/PASSIVE skills. Only the
    outfit buff. A leader that is not among the 5 members therefore emits
    exactly one instance."""
    leader = _card(
        "leader_01",
        special=_skill("SPECIAL"),
        active=_skill("ACTIVE"),
        passive=_skill("PASSIVE"),
        costume_skill=_skill("COSTUME", "life_recovery"),
    )
    members = tuple(_card(f"member_{i:02d}") for i in range(1, MEMBER_COUNT + 1))
    team = Team(leader=leader, members=members)

    leader_instances = [
        i for i in team.effective_skill_instances() if i.card_id == leader.id
    ]

    assert len(leader_instances) == 1, "the leader's own SP/A/P must not count"
    assert leader_instances[0].source == "costume"
    assert leader_instances[0].slot_kind == "costume"


def test_a_leader_who_is_also_a_member_contributes_both_roles_once_each():
    """The two roles are independent: the outfit skill comes from the leader
    slot, the normal slots from being one of the 5 — so a card doing both jobs
    emits its three normal slots exactly once, plus one costume instance."""
    shared = _card(
        "shared_01",
        special=_skill("SPECIAL"),
        active=_skill("ACTIVE"),
        passive=_skill("PASSIVE"),
        costume_skill=_skill("COSTUME", "life_recovery"),
    )
    members = (shared, *(_card(f"member_{i:02d}") for i in range(2, MEMBER_COUNT + 1)))
    team = Team(leader=shared, members=members)

    mine = [i for i in team.effective_skill_instances() if i.card_id == shared.id]

    assert len([i for i in mine if i.source == "normal"]) == 3
    assert len([i for i in mine if i.source == "costume"]) == 1


def test_member_costume_skill_contributes_no_costume_instance():
    leader, members = _valid_leader_and_members()
    # Give one member a costume_skill too — it must never appear as a
    # costume instance; only the Leader's costume skill is team-wide.
    doped_member = _card(
        "member_dope",
        costume_skill=_skill("COSTUME", "life_recovery"),
    )
    members = (doped_member, *members[1:])
    team = Team(leader=leader, members=members)

    instances = team.effective_skill_instances()
    costume_instances = [i for i in instances if i.source == "costume"]
    assert len(costume_instances) == 1
    assert costume_instances[0].card_id == leader.id
    assert all(i.card_id != doped_member.id for i in costume_instances)


def test_base_stat_sum_counts_the_five_members_only_never_the_leader():
    """The leader slot contributes its outfit buff and nothing else — no
    stats. A leader outside the five must not inflate the team's stat sum."""
    leader, members = _valid_leader_and_members()
    team = Team(leader=leader, members=members)

    expected = sum(card.stat_sum() for card in members)
    assert team.base_stat_sum() == expected
    assert isinstance(team.base_stat_sum(), int)
    assert team.base_stat_sum() < expected + leader.stat_sum()


def test_base_stat_sum_counts_a_leader_who_is_also_a_member_exactly_once():
    shared = _card("shared_01")
    members = (shared, *(_card(f"member_{i:02d}") for i in range(2, MEMBER_COUNT + 1)))
    team = Team(leader=shared, members=members)

    assert team.base_stat_sum() == sum(card.stat_sum() for card in members)
