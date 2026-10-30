"""The generation-green cap in the search's per-character board plans
(`board_plan.grouping_room`, `_chain_is_surplus`): when three ID Gen 1 members
each buy the whole G-008/011/021 chain (1,350), the game counts only 900.

A character's three nodes can feed DIFFERENT groupings (Fubuki: G-008 to Gen 1,
G-011 to GAMERS); assuming one grouping per character, read in set order, drops
a chain GAMERS needs on some processes and not others."""
from __future__ import annotations

from hbuilder.domain import board_plan as bp
from hbuilder.domain.board_optimizer import BoardPlan

from conftest import make_card

GEN1, GAMERS = "character_grouping-grp-gen_1-always", "character_grouping-grp-gamers-always"
SKILLTREE = {"nodes": {
    "G-008": {"chr": {"chr-00001": {"tgt": GEN1, "v": 75}, "chr-00002": {"tgt": GEN1, "v": 75},
                      "chr-00003": {"tgt": GEN1, "v": 75}, "chr-00004": {"tgt": GEN1, "v": 75}}},
    "G-011": {"chr": {"chr-00001": {"tgt": GEN1, "v": 225}, "chr-00002": {"tgt": GEN1, "v": 225},
                      "chr-00003": {"tgt": GEN1, "v": 225}, "chr-00004": {"tgt": GAMERS, "v": 300}}},
    "G-021": {"chr": {"chr-00001": {"tgt": GEN1, "v": 150}, "chr-00002": {"tgt": GEN1, "v": 150},
                      "chr-00003": {"tgt": GEN1, "v": 150}, "chr-00004": {"tgt": GAMERS, "v": 150}}},
}}
CHAIN = frozenset({"S-001", "G-008", "G-011", "G-021"})


def _plans(cards):
    return {c.id: BoardPlan(selected=CHAIN, spent=10, value=0.0) for c in cards}


def test_a_chain_is_counted_per_grouping_it_feeds():
    assert bp._grouping_chain(CHAIN, "chr-00004", SKILLTREE) == {GEN1: 75.0, GAMERS: 450.0}
    assert bp._grouping_chain(CHAIN, "chr-00001", SKILLTREE) == {GEN1: 450.0}


def test_the_third_same_grouping_chain_is_surplus_but_not_one_another_grouping_needs():
    leader = make_card("card-00009-5-uniq-0000-00", member="Leader")
    a, b, c, fubuki = (make_card(f"card-0000{n}-5-uniq-0000-00", member=f"M{n}") for n in (1, 2, 3, 4))
    members = (a, b, c, fubuki)
    plans = _plans(members)
    # Gen 1 gets 3 x 450 + 75 = 1,425: any one 450 chain is surplus (975 >= 900)
    assert bp._chain_is_surplus(c, plans, members, leader, SKILLTREE)
    # Fubuki's board also feeds GAMERS, which has nothing else: never surplus
    assert not bp._chain_is_surplus(fubuki, plans, members, leader, SKILLTREE)
    # with one Gen 1 chain gone, the next is no longer surplus (975 - 450 < 900)
    plans[c.id] = BoardPlan(selected=frozenset({"S-001"}), spent=0, value=0.0)
    assert not bp._chain_is_surplus(b, plans, members, leader, SKILLTREE)


def test_room_is_what_the_other_boards_leave_under_the_cap():
    bench = {"M1": [{"node": "G-008"}, {"node": "G-011"}, {"node": "G-021"}],
             "M2": [{"node": "G-011", "connect_multiplier": 2.0}]}
    names = {"chr-00001": "M1", "chr-00002": "M2", "chr-00003": "M3", "chr-00004": "M4"}
    room = bp.grouping_room(SKILLTREE, bench, names)
    # M3: others give 450 + 450 (M2's G-011 doubled by a Connect card) = 900 -> no room
    assert room["chr-00003"] == {"G-008": 0.0, "G-011": 0.0, "G-021": 0.0}
    # M1: the others give 450 -> room for all of her 450
    assert room["chr-00001"]["G-011"] == 1.0
    # Fubuki's GAMERS nodes have the whole cap; her Gen 1 node is full
    assert room["chr-00004"] == {"G-008": 0.0, "G-011": 1.0, "G-021": 1.0}
