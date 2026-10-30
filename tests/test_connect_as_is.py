"""Connect sockets AS THEY ARE.

The account's boards (`board_state.json`, from `User/Get`) name the card in each
socket. Scoring them "as they are" must keep those cards: re-assigning the
sockets with the develop's matching can put a team's Overall Power a few hundred
under the server and move per-member shares by hundreds or thousands. Pinned here on the synthetic boards of `test_fast_leaf`:
`_fixed_connect_placements` keeps the named card even where the matching
prefers another, covers only selected nodes, ignores cards with no Connect
effect, and `board_from_state(use_slots=...)` switches between the two."""
from __future__ import annotations

from hbuilder.domain.board_optimizer import BoardPlan
from hbuilder.domain.board_plan import (
    TeamBoardPlan, _fixed_connect_placements, board_model_from_plan,
)
from hbuilder.solver.parallel_refine import board_from_state

from conftest import make_scoring_config
from test_fast_leaf import CONNECT, EFFECTS, K, SKILLTREE, TREE, _POS, _team

ALL = frozenset(EFFECTS) - {"Y-001"}  # every stat node bought (sockets need not be)


def _per_char(selected=ALL, values=None):
    vals = values or {n: 10.0 for n in EFFECTS}
    return {"chr-00001": (_POS, frozenset(selected), vals)}


def test_fixed_placement_keeps_the_named_card_and_its_shape():
    # cc-c (-1,0),(-2,0),(1,0) at 900 from S-001 [0,0] -> G-001, G-002, R-001
    mults, placements = _fixed_connect_placements(
        _per_char(), CONNECT, {"chr-00001": {"S-001": "cc-c"}})
    assert mults == {"chr-00001": {"G-001": 1.9, "G-002": 1.9, "R-001": 1.9}}
    assert [(p.chr, p.socket, p.card, p.multiplier) for p in placements] == [
        ("chr-00001", "S-001", "cc-c", 1.9)]
    assert placements[0].value == 3 * 10.0 * 900 / 1000


def test_fixed_placement_covers_only_selected_nodes_and_skips_unknown_cards():
    selected = ALL - {"G-002"}
    mults, placements = _fixed_connect_placements(
        _per_char(selected), CONNECT,
        {"chr-00001": {"S-001": "cc-c", "S-003": "card-without-connect"}})
    assert mults == {"chr-00001": {"G-001": 1.9, "R-001": 1.9}}
    assert [p.card for p in placements] == ["cc-c"]


def test_fixed_placement_does_not_need_the_socket_bought():
    # S-003 [-1,1] is not in `selected`; a socket holding a card is unlocked.
    # cc-a1 (1,0),(2,0) from it -> [0,1] B-001 and [1,1] (the S-002 socket, no value)
    mults, placements = _fixed_connect_placements(
        _per_char(), CONNECT, {"chr-00001": {"S-003": "cc-a1"}})
    assert mults == {"chr-00001": {"B-001": 2.5}}
    assert [(p.socket, p.card) for p in placements] == [("S-003", "cc-a1")]


def _plan(team):
    per = {team.leader.id: BoardPlan(selected=ALL, spent=len(ALL), value=0.0)}
    return TeamBoardPlan(total_gain=0.0, per_character=per)


def _assemble(team, fixed):
    placements = []
    board = board_model_from_plan(
        _plan(team), team.leader, team.members, SKILLTREE, EFFECTS,
        overall_power=20000.0, bonus_permil=300.0, k_constant=K, active_pool_rate=0.4,
        connect_cards=CONNECT, placements_out=placements, fixed_slots=fixed)
    mults = {n.node_id: n.connect_multiplier
             for n in board.characters[team.leader.member].nodes}
    return mults, placements


def test_board_model_from_plan_takes_the_fixed_slots_instead_of_the_matching():
    team = _team(1, [2, 3, 4, 5, 6])
    assigned_mults, assigned = _assemble(team, None)
    fixed_mults, fixed = _assemble(team, {"chr-00001": {"S-001": "cc-c"}})
    assert [(p.socket, p.card) for p in fixed] == [("S-001", "cc-c")]
    # the matching does something else on this board, so the switch is observable
    assert [(p.socket, p.card) for p in assigned] != [("S-001", "cc-c")]
    assert fixed_mults["G-001"] == fixed_mults["R-001"] == 1.9
    assert fixed_mults["R-002"] == 1.0
    assert assigned_mults != fixed_mults
    # an empty socket map means "nothing socketed": no Connect anywhere
    none_mults, none = _assemble(team, {"chr-00001": {}})
    assert none == [] and set(none_mults.values()) == {1.0}


def test_board_from_state_use_slots_switch():
    team = _team(1, [2, 3, 4, 5, 6])
    state = {"chr-00001": {"nodes": sorted(ALL), "slots": {"S-001": "cc-c"}}}
    cfg = make_scoring_config()

    def placements(use_slots):
        _board, out = board_from_state(
            team, state, cfg=cfg, skilltree=SKILLTREE, node_effect_ids=EFFECTS,
            tree_for=lambda c: TREE, connect_cards=CONNECT, use_slots=use_slots)
        return [(p.chr, p.socket, p.card) for p in out]

    assert placements(True) == [("chr-00001", "S-001", "cc-c")]
    assert placements(False) != placements(True)          # the develop's matching
