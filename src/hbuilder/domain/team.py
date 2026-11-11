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

from collections.abc import Iterable
from dataclasses import dataclass

from hbuilder.domain.card import Card

MEMBER_COUNT = 5


class TeamError(ValueError):
    """Raised when a team cannot be assembled as specified."""


@dataclass(frozen=True, slots=True)
class Team:
    leader: Card
    members: tuple[Card, ...]

    def __post_init__(self) -> None:
        # Check 1: exactly MEMBER_COUNT members.
        if len(self.members) != MEMBER_COUNT:
            raise TeamError(f"Team requires exactly {MEMBER_COUNT} members, got {len(self.members)}")

        # Check 2: the same card may not be listed twice AS A MEMBER (the leader
        # is a separate slot and may also be a member). `getattr` lets Check 4
        # report a non-Card element.
        seen_ids: set[object] = set()
        for card in self.members:
            card_id = getattr(card, "id", None)
            key = card_id if card_id is not None else id(card)
            if key in seen_ids:
                raise TeamError(f"duplicate card id in team: {card_id!r}")
            seen_ids.add(key)

        # Check 3: a holomem may not repeat among the members (two skins of one
        # character are different cards with the same `member`).
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
                raise TeamError(f"Team members must all be Card instances, got {type(card).__name__}")

    def base_stat_sum(self) -> int:
        """The 5 members' stats. The leader slot contributes none — if
        its card is also a member, it is counted there and only there."""
        return sum(card.stat_sum() for card in self.members)


def build_team(cards: Iterable[Card], leader_id: str, member_ids: Iterable[str]) -> Team:
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
