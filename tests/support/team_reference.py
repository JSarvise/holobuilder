"""The team's skill instances, listed straight from the cards.

`hbuilder.domain.gates.resolve_team_gates` builds the same list through the gate
tables and in the same order; the tests compare the two so the product's order
and membership stay pinned to this plain definition.
"""

from __future__ import annotations

from dataclasses import dataclass

from hbuilder.domain.card import Card, SkillSlot, scored_costume_rider_slot
from hbuilder.domain.schema import SKILL_SLOT_KINDS
from hbuilder.domain.team import Team


@dataclass(frozen=True, slots=True)
class SkillInstance:
    card_id: str
    slot_kind: str
    source: str  # "normal" | "costume" | "costume_rider"
    skill: SkillSlot


def effective_skill_instances(team: Team) -> tuple[SkillInstance, ...]:
    """A deterministic tuple: the leader's costume skill first (the one
    team-wide effect), then the outfit's scored RIDER
    (`card.scored_costume_rider_slot`: a `score_support` rider as a second,
    always-on team-wide effect, source `"costume_rider"`, same `"costume"`
    slot_kind), then each member's non-null normal slots in member order, in
    `SKILL_SLOT_KINDS` order within each member.

    The leader's own SPECIAL/ACTIVE/PASSIVE are **not** emitted: the card in
    the outfit slot contributes its outfit buff and nothing else. If that same
    card is also one of the 5, its normal slots are emitted once, from its
    member role — which is why the leader is not iterated separately for them.

    A member's own `costume_skill` is never emitted; only the leader's outfit
    is a team-wide effect."""
    instances: list[SkillInstance] = []

    def normal_instances(card: Card) -> list[SkillInstance]:
        result = []
        for slot_kind in SKILL_SLOT_KINDS:
            skill = card.skill(slot_kind)
            if skill is not None:
                result.append(
                    SkillInstance(card_id=card.id, slot_kind=slot_kind, source="normal", skill=skill)
                )
        return result

    leader = team.leader
    if leader.costume_skill is not None:
        instances.append(
            SkillInstance(
                card_id=leader.id, slot_kind="costume", source="costume", skill=leader.costume_skill
            )
        )
        rider_slot = scored_costume_rider_slot(leader.costume_skill)
        if rider_slot is not None:
            instances.append(
                SkillInstance(
                    card_id=leader.id, slot_kind="costume", source="costume_rider", skill=rider_slot
                )
            )
    for member in team.members:
        instances.extend(normal_instances(member))

    return tuple(instances)
