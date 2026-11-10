"""The event-mode pool: a collapse of look-alike cards must not merge cards whose event
bonus differs (`leaf_inputs.collapse_pool` with a `split_key`)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools" / "web"))

from hbuilder.domain.marathon import MarathonBonusRow, MarathonEvent, card_bonus_table  # noqa: E402


def _account():
    import leaf_inputs  # noqa: PLC0415

    return leaf_inputs


def _card(card_id: str, member: str = "Lamy"):
    return SimpleNamespace(
        id=card_id,
        member=member,
        rarity=card_id.split("-")[2] + "★",
        type="Cute",
        generations=frozenset({"5期生"}),
    )


def test_twins_collapse_without_a_split_key_and_stay_apart_with_the_event_bonus():
    acct = _account()
    plain, event_card = _card("card-00030-5-uniq-0024-00"), _card("card-00030-5-uniq-0089-00")
    same = lambda _c: ("Lamy", "Cute")  # noqa: E731
    reps, dominated = acct.collapse_pool([event_card, plain], same)
    assert [c.id for c in reps] == [plain.id] and dominated == [(event_card.id, plain.id)]

    ev = MarathonEvent(
        name="t",
        rows_by_channel={"live": (MarathonBonusRow(permil=300, card_id=event_card.id),), "music": ()},
    )
    bonus = dict(
        zip([plain.id, event_card.id], card_bonus_table([plain, event_card], ev, blooms={}, music_id=None))
    )
    reps, dominated = acct.collapse_pool([event_card, plain], same, lambda c: bonus[c.id])
    assert {c.id for c in reps} == {plain.id, event_card.id} and dominated == []
