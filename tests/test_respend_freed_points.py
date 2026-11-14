"""`board_refine.respend_freed_points` — the points a refinement freed, spent again.

Why it exists: dropping a frequency (`Act.CD`) node frees its points, and on a board the
plan could not fill (the account's levels) they buy what the plan left out. The tests hold
it to: only boards the refinement changed grow; the growth starts from the refined board
and never buys a frozen (frequency) node, not even as a path, so the chosen counts stay;
within the budget; a board with nothing of value left buys nothing; the memo changes
nothing."""

from __future__ import annotations

from hbuilder.domain.board_optimizer import BoardPlan
from hbuilder.domain.board_plan import TeamBoardPlan
from hbuilder.domain.board_refine import respend_freed_points
from hbuilder.domain.skilltree import ROOT_NODE, SkillTree, TreeNode, neighbours

# S-001 (0,0) - G-001 (0,1) - G-004 (0,2)
#   |
# B-020 (1,0) - G-003 (2,0)          G-003 is reached only through B-020
POSITIONS = {
    ROOT_NODE: (0, 0),
    "G-001": (0, 1),
    "G-004": (0, 2),
    "B-020": (1, 0),
    "G-003": (2, 0),
}
COSTS = {ROOT_NODE: 0, "G-001": 2, "G-004": 4, "B-020": 5, "G-003": 1}
VALUES = {"G-001": 1.0, "G-004": 3.0, "B-020": 2.0, "G-003": 10.0}
FROZEN = frozenset({"B-020"})


def _tree() -> SkillTree:
    nodes = {n: TreeNode(node_id=n, branch=n[0], cost=c, player_level=None) for n, c in COSTS.items()}
    return SkillTree(nodes=nodes, positions=POSITIONS, adjacency=neighbours(POSITIONS))


TREE = _tree()


class _Card:
    def __init__(self, card_id: str) -> None:
        self.id = card_id


def _plan(selected_by_card: dict[str, set[str]], keys: bool = True) -> TeamBoardPlan:
    return TeamBoardPlan(
        total_gain=0.0,
        per_character={
            cid: BoardPlan(
                selected=frozenset(sel), spent=TREE.cost_of(frozenset(sel) - {ROOT_NODE}), value=0.0
            )
            for cid, sel in selected_by_card.items()
        },
        plan_keys={cid: ("chr-x", "member", 0, False, cid) for cid in selected_by_card} if keys else {},
    )


def _run(start, plan, budget, values_for=lambda _cid: VALUES, memo=None):
    cards = {cid: _Card(cid) for cid in plan.per_character}
    return respend_freed_points(
        start,
        plan,
        cards,
        tree_for=lambda _c: TREE,
        budget_for=lambda _c: budget,
        values_for=values_for,
        frozen=FROZEN,
        memo=memo,
    )


def test_the_freed_points_buy_what_the_plan_left_and_never_a_frozen_node():
    # the start holds B-020 (5 points); the refinement drops it. With 8 points the plain
    # greedy would buy B-020 + G-003 again (10 over 6 points, the best ratio): frozen, it
    # buys G-004 with the freed points instead, and G-003 stays out of reach
    start = _plan({"m1": {ROOT_NODE, "G-001", "B-020"}})
    refined = _plan({"m1": {ROOT_NODE, "G-001"}}, keys=False)
    plan, changes = _run(start, refined, budget=8)
    sel = plan.per_character["m1"].selected
    assert sel == {ROOT_NODE, "G-001", "G-004"}
    assert plan.per_character["m1"].spent == 6 <= 8
    assert TREE.reachable(sel) == sel
    assert changes == ["m1 re-spent +G-004"]
    assert plan.plan_keys == {}


def test_a_board_the_refinement_did_not_change_is_left_as_it_is():
    # m2 has points left and G-004 within reach, but its board is the plan's own: the plan
    # stopped there for its own reasons, and a refinement that moved nothing frees nothing
    start = _plan({"m1": {ROOT_NODE, "G-001", "B-020"}, "m2": {ROOT_NODE, "G-001"}})
    refined = _plan({"m1": {ROOT_NODE, "G-001"}, "m2": {ROOT_NODE, "G-001"}}, keys=False)
    plan, _changes = _run(start, refined, budget=8)
    assert plan.per_character["m2"] is refined.per_character["m2"]


def test_nothing_to_buy_returns_the_plan_itself():
    # every node of value is held but the frozen one (the maxed budget): nothing to buy
    start = _plan({"m1": {ROOT_NODE, "G-001", "G-004", "B-020", "G-003"}})
    refined = _plan({"m1": {ROOT_NODE, "G-001", "G-004", "B-020", "G-003"} - {"G-003"}}, keys=False)
    # G-003 dropped (not frozen): with values it is bought back
    plan, changes = _run(start, refined, budget=100)
    assert "G-003" in plan.per_character["m1"].selected and changes
    # a board whose drop left nothing of value to buy
    start = _plan({"m1": {ROOT_NODE, "G-001", "G-004", "B-020"}})
    refined = _plan({"m1": {ROOT_NODE, "G-001", "G-004"}}, keys=False)
    plan, changes = _run(start, refined, budget=100)
    assert plan is refined and changes == []


def test_no_values_or_no_points_leave_the_board():
    start = _plan({"m1": {ROOT_NODE, "G-001", "B-020"}})
    refined = _plan({"m1": {ROOT_NODE, "G-001"}}, keys=False)
    plan, changes = _run(start, refined, budget=8, values_for=lambda _cid: None)
    assert plan is refined and changes == []
    plan, changes = _run(start, refined, budget=2)  # G-001's 2 points: none left
    assert plan is refined and changes == []


def test_the_memo_grows_a_board_once_and_changes_nothing():
    start = _plan({"m1": {ROOT_NODE, "G-001", "B-020"}})
    refined = _plan({"m1": {ROOT_NODE, "G-001"}}, keys=False)
    asked = []

    def values_for(cid):
        asked.append(cid)
        return VALUES

    memo: dict = {}
    first = _run(start, refined, budget=8, values_for=values_for, memo=memo)
    second = _run(start, refined, budget=8, values_for=values_for, memo=memo)
    assert asked == ["m1"]
    assert first[0].per_character == second[0].per_character and first[1] == second[1]
    assert first[0].per_character == _run(start, refined, budget=8)[0].per_character
