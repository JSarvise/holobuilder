"""The board-plan engine (`domain/board_optimizer.py`).

The greedy is approximate, but three things must hold exactly and are pinned
here: the plan is always connected to the root, never over budget, and buys a
zero-value connector node when a valuable node sits behind it (the case a plain
value/cost frontier greedy gets wrong)."""

from __future__ import annotations

from hbuilder.domain.skilltree import SkillTree, TreeNode, ROOT_NODE
from hbuilder.domain.board_optimizer import optimize_board


def _node(node_id: str, cost: int, *, level: int | None = None) -> TreeNode:
    return TreeNode(node_id=node_id, branch="G", cost=cost, player_level=level)


def _line_tree() -> SkillTree:
    #   ROOT(0) - A(1,val0) - B(2,val10)     and     ROOT - C(5,val3)
    nodes = {
        ROOT_NODE: _node(ROOT_NODE, 0),
        "A": _node("A", 1),
        "B": _node("B", 2),
        "C": _node("C", 5),
    }
    adjacency = {
        ROOT_NODE: frozenset({"A", "C"}),
        "A": frozenset({ROOT_NODE, "B"}),
        "B": frozenset({"A"}),
        "C": frozenset({ROOT_NODE}),
    }
    return SkillTree(nodes=nodes, positions={}, adjacency=adjacency)


VALUES = {"A": 0.0, "B": 10.0, "C": 3.0}


def test_buys_the_connector_to_reach_the_valuable_node():
    tree = _line_tree()
    plan = optimize_board(tree, budget=3, node_value=VALUES)
    # A is worthless alone but is the only path to B (value 10) — both bought.
    assert plan.selected == frozenset({ROOT_NODE, "A", "B"})
    assert plan.value == 10.0
    assert plan.spent == 3


def test_never_exceeds_budget_and_stays_connected():
    tree = _line_tree()
    for budget in range(0, 9):
        plan = optimize_board(tree, budget=budget, node_value=VALUES)
        assert plan.spent <= budget
        # every bought node has a path to the root through bought nodes
        assert tree.reachable(plan.selected) == plan.selected


def test_tight_budget_takes_the_only_affordable_value():
    tree = _line_tree()
    # budget 2 cannot afford A+B (3) nor C (5): nothing value-adding is buyable.
    plan = optimize_board(tree, budget=2, node_value=VALUES)
    assert plan.value == 0.0
    assert plan.selected == frozenset({ROOT_NODE})


def test_bigger_budget_reaches_more_value():
    tree = _line_tree()
    # budget 8 affords A+B (3) then C (5) too -> 13.
    plan = optimize_board(tree, budget=8, node_value=VALUES)
    assert plan.selected == frozenset({ROOT_NODE, "A", "B", "C"})
    assert plan.value == 13.0
    assert plan.spent == 8


def test_level_locked_node_is_neither_bought_nor_routed_through():
    tree = _line_tree()
    # B needs player level 40; at level 30 it is unavailable, so A (its only
    # reason to exist) buys nothing and the plan stays at the root.
    nodes = dict(tree.nodes)
    nodes["B"] = TreeNode(node_id="B", branch="G", cost=2, player_level=40)
    gated = SkillTree(nodes=nodes, positions={}, adjacency=tree.adjacency)
    plan = optimize_board(gated, budget=8, node_value=VALUES, player_level=30)
    assert "B" not in plan.selected
    # C is still reachable and affordable, so it is taken.
    assert "C" in plan.selected
