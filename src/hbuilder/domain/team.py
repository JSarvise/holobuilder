"""Team assembly: 5 Members + 1 Leader/Outfit slot. Pure, no I/O.

**The leader is not a sixth participant.** It is the outfit applied to the
team, and it contributes exactly two things: its Outfit Skill, and its red
Leader-scope board nodes (the latter not modelled here). It contributes
**no stats**, **no SPECIAL/ACTIVE/PASSIVE skills**, and **nothing to gate counts** —
gates resolve over the 5 members alone.

Two consequences that trip people up:

- The leader's card **may also be one of the 5 members**. The slots are
  independent, so such a card contributes its normal skills once (as a
  member) and its outfit skill once (as the leader).
- Member distinctness is by **holomem** (`Card.member`), not by card id: two
  skins of the same character cannot both be members. The leader is
  exempt precisely because it is not a member.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from hbuilder.domain.card import Card, SkillSlot, scored_costume_rider_slot
from hbuilder.domain.schema import SKILL_SLOT_KINDS

TEAM_SIZE = 6
MEMBER_COUNT = 5


class TeamError(ValueError):
    """Raised when a team cannot be assembled as specified."""


@dataclass(frozen=True, slots=True)
class SkillInstance:
    card_id: str
    slot_kind: str
    source: str  # "normal" | "costume" | "costume_rider"
    skill: SkillSlot


@dataclass(frozen=True, slots=True)
class Team:
    leader: Card
    members: tuple[Card, ...]

    def __post_init__(self) -> None:
        # Check 1: exactly MEMBER_COUNT members.
        if len(self.members) != MEMBER_COUNT:
            raise TeamError(
                f"Team requires exactly {MEMBER_COUNT} members, got "
                f"{len(self.members)}"
            )

        # Check 2: the same card may not be listed twice AS A MEMBER.
        # `getattr` guards against a non-Card element that has no `.id`, so
        # Check 4 below still gets a chance to report the more specific
        # "not a Card" failure for that case. The LEADER is deliberately not
        # checked here: it is a separate slot and may name a card that is
        # also a member.
        seen_ids: set[object] = set()
        for card in self.members:
            card_id = getattr(card, "id", None)
            key = card_id if card_id is not None else id(card)
            if key in seen_ids:
                raise TeamError(f"duplicate card id in team: {card_id!r}")
            seen_ids.add(key)

        # Check 3: a holomem may not repeat among the members. Distinct
        # card ids are not sufficient, because two skins of one character are
        # two different cards with the same `member`.
        seen_members: set[str] = set()
        for card in self.members:
            holomem = getattr(card, "member", None)
            if holomem is None:
                continue  # not a Card; Check 4 reports it properly
            if holomem in seen_members:
                raise TeamError(
                    f"holomem {holomem!r} appears twice among the members; "
                    f"two skins of the same character cannot both be members"
                )
            seen_members.add(holomem)

        # Check 4: every element is a Card instance.
        for card in (self.leader, *self.members):
            if not isinstance(card, Card):
                raise TeamError(
                    f"Team members must all be Card instances, got "
                    f"{type(card).__name__}"
                )

    def base_stat_sum(self) -> int:
        """The 5 members' stats. The leader slot contributes none — if
        its card is also a member, it is counted there and only there."""
        return sum(card.stat_sum() for card in self.members)

    def effective_skill_instances(self) -> tuple[SkillInstance, ...]:
        """A deterministic tuple: the leader's costume skill first (the one
        team-wide effect), then the outfit's scored RIDER (`card.scored_costume_rider_slot`: a `score_support` rider as a
        second, always-on team-wide effect, source `"costume_rider"`, same
        `"costume"` slot_kind), then each member's non-null normal slots in
        member order, in `SKILL_SLOT_KINDS` order within each member.

        The leader's own SPECIAL/ACTIVE/PASSIVE are **not** emitted: the
        card in the outfit slot contributes its outfit buff and nothing else.
        If that same card is also one of the 5, its normal slots are emitted
        once, from its member role — which is why the leader is not iterated
        separately for them.

        A member's own `costume_skill` is never emitted; only the leader's
        outfit is a team-wide effect. Determinism matters because identical
        inputs must produce identical rankings."""
        instances: list[SkillInstance] = []

        def normal_instances(card: Card) -> list[SkillInstance]:
            result = []
            for slot_kind in SKILL_SLOT_KINDS:
                skill = card.skill(slot_kind)
                if skill is not None:
                    result.append(
                        SkillInstance(
                            card_id=card.id,
                            slot_kind=slot_kind,
                            source="normal",
                            skill=skill,
                        )
                    )
            return result

        if self.leader.costume_skill is not None:
            instances.append(
                SkillInstance(
                    card_id=self.leader.id,
                    slot_kind="costume",
                    source="costume",
                    skill=self.leader.costume_skill,
                )
            )
            rider_slot = scored_costume_rider_slot(self.leader.costume_skill)
            if rider_slot is not None:
                instances.append(
                    SkillInstance(
                        card_id=self.leader.id,
                        slot_kind="costume",
                        source="costume_rider",
                        skill=rider_slot,
                    )
                )
        for member in self.members:
            instances.extend(normal_instances(member))

        return tuple(instances)


def build_team(
    cards: Iterable[Card], leader_id: str, member_ids: Iterable[str]
) -> Team:
    """Resolve `leader_id`/`member_ids` against `cards` and build a `Team`.

    Raises `TeamError` naming any id not found among `cards`.
    """
    index = {card.id: card for card in cards}

    def resolve(card_id: str) -> Card:
        try:
            return index[card_id]
        except KeyError:
            raise TeamError(f"unknown card id: {card_id!r}") from None

    leader = resolve(leader_id)
    members = tuple(resolve(member_id) for member_id in member_ids)
    return Team(leader=leader, members=members)
