"""The board-plan engine (`domain/board_optimizer.py`).

The greedy is approximate, but three things must hold exactly and are pinned
here: the plan is always connected to the root, never over budget, and buys a
zero-value connector node when a valuable node sits behind it (the case a plain
value/cost frontier greedy gets wrong)."""

from __future__ import annotations

from hbuilder.domain.board_optimizer import keep_preferred, optimize_board
from hbuilder.domain.skilltree import ROOT_NODE, SkillTree, TreeNode


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


# -- the account's own nodes on a plan made from scratch (`prefer`) -----------------------
def _fork_tree() -> SkillTree:
    #   ROOT - A(1) - B(2)      ROOT - C(1)      ROOT - D(1) - E(3)
    nodes = {ROOT_NODE: _node(ROOT_NODE, 0), "A": _node("A", 1), "B": _node("B", 2), "C": _node("C", 1)}
    nodes |= {"D": _node("D", 1), "E": _node("E", 3)}
    adjacency = {
        ROOT_NODE: frozenset({"A", "C", "D"}),
        "A": frozenset({ROOT_NODE, "B"}),
        "B": frozenset({"A"}),
        "C": frozenset({ROOT_NODE}),
        "D": frozenset({ROOT_NODE, "E"}),
        "E": frozenset({"D"}),
    }
    return SkillTree(nodes=nodes, positions={}, adjacency=adjacency)


def test_prefer_breaks_a_tie_towards_the_node_the_account_has():
    tree = _fork_tree()
    values = {"A": 5.0, "C": 5.0}
    assert optimize_board(tree, budget=1, node_value=values).selected == {ROOT_NODE, "A"}
    kept = optimize_board(tree, budget=1, node_value=values, prefer={"C"})
    assert kept.selected == {ROOT_NODE, "C"} and kept.value == 5.0


def test_prefer_never_trades_value_for_an_owned_node():
    tree = _fork_tree()
    plan = optimize_board(tree, budget=1, node_value={"A": 5.0, "C": 4.0}, prefer={"C"})
    assert plan.selected == {ROOT_NODE, "A"}


def test_points_left_keep_the_owned_nodes_they_pay_for():
    tree = _fork_tree()
    # B (via A) is worth buying: 3 points; 2 are left. The account owns C, D and E: C and D
    # (1 point each) are kept with them, E (3 more behind D) is not.
    plan = optimize_board(tree, budget=5, node_value={"B": 10.0}, prefer={"C", "D", "E"})
    assert plan.selected == {ROOT_NODE, "A", "B", "C", "D"}
    assert plan.spent == 5 and plan.value == 10.0
    # without them nothing is bought for nothing
    assert optimize_board(tree, budget=5, node_value={"B": 10.0}).selected == {ROOT_NODE, "A", "B"}


def test_an_owned_node_is_kept_only_through_owned_nodes():
    tree = _fork_tree()
    # E is the account's, D (its only way in) is not: keeping E would mean buying D
    plan = optimize_board(tree, budget=8, node_value={}, prefer={"E"})
    assert plan.selected == {ROOT_NODE}


def test_no_owned_node_is_dropped_while_the_points_left_pay_for_it():
    import random

    rng = random.Random(7)
    for _ in range(200):
        size = rng.randint(3, 12)
        ids = [f"N{i}" for i in range(size)]
        nodes = {ROOT_NODE: _node(ROOT_NODE, 0)} | {n: _node(n, rng.randint(1, 4)) for n in ids}
        parent = {n: rng.choice([ROOT_NODE, *ids[:k]]) for k, n in enumerate(ids)}
        adjacency: dict[str, set[str]] = {n: set() for n in nodes}
        for child, par in parent.items():
            adjacency[child].add(par)
            adjacency[par].add(child)
        tree = SkillTree(nodes=nodes, positions={}, adjacency={k: frozenset(v) for k, v in adjacency.items()})
        # the account's board: a connected set grown from the root, as the game keeps it
        owned: set[str] = set()
        for n in ids:
            if (parent[n] == ROOT_NODE or parent[n] in owned) and rng.random() < 0.6:
                owned.add(n)
        values = {n: rng.choice([0.0, 0.0, 1.0, 2.5, 4.0]) for n in ids}
        budget = rng.randint(0, 20)
        plan = optimize_board(tree, budget=budget, node_value=values, prefer=owned)
        assert plan.spent <= budget
        assert tree.reachable(plan.selected) == plan.selected
        # not a single owned node outside the plan that its unspent points could keep
        _sel, _spent, more = keep_preferred(tree, plan.selected, plan.spent, budget, owned)
        assert more == ()
