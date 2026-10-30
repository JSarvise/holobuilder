"""`board_plan.refine_activation_nodes` — the joint choice of the team's
activation-frequency / activation-probability nodes on the REAL score.

Why it exists:
a shorter cooldown is worth whatever its windows then do against the other
members' windows and the chart's cues, so the greedy planner's per-node value
over-buys them; dropping some raised the song score by up to 3.1 %. The
refinement only ever keeps a change the score function says is better, so the
tests hold it to: never worse, never over budget, never disconnected."""
from __future__ import annotations

from hbuilder.domain.board_optimizer import BoardPlan
from hbuilder.domain.board_plan import TeamBoardPlan, activation_node_ids, refine_activation_nodes
from hbuilder.domain.skilltree import ROOT_NODE, SkillTree, TreeNode, neighbours

# A line board: S-001 - B-001 - B-013 - B-020 , and a side branch S-001 - G-001
#                (x=0)   (x=1)   (x=2)   (x=3)                       (0,1)
POSITIONS = {ROOT_NODE: (0, 0), "B-001": (1, 0), "B-013": (2, 0), "B-020": (3, 0), "G-001": (0, 1)}
COSTS = {ROOT_NODE: 0, "B-001": 3, "B-013": 5, "B-020": 5, "G-001": 2}
ACTIVATION = frozenset({"B-013", "B-020"})


def _tree() -> SkillTree:
    nodes = {n: TreeNode(node_id=n, branch=n[0], cost=c, player_level=None) for n, c in COSTS.items()}
    return SkillTree(nodes=nodes, positions=POSITIONS, adjacency=neighbours(POSITIONS))


def _plan(selected: set[str], card_id: str = "card-1") -> TeamBoardPlan:
    tree = _tree()
    return TeamBoardPlan(total_gain=0.0, per_character={
        card_id: BoardPlan(selected=frozenset(selected), spent=tree.cost_of(selected - {ROOT_NODE}), value=0.0)})


class _Card:
    def __init__(self, card_id: str) -> None:
        self.id = card_id


def _run(plan, score, budget=20):
    tree = _tree()
    return refine_activation_nodes(
        plan, {"card-1": _Card("card-1")}, tree_for=lambda _c: tree, budget_for=lambda _c: budget,
        activation_nodes=ACTIVATION, score=score)


def test_an_activation_node_that_hurts_is_dropped_and_one_that_helps_is_kept():
    start = _plan({ROOT_NODE, "B-001", "B-013", "B-020"})

    def score(plan):            # B-020 (the tip) costs score, B-013 earns it
        sel = plan.per_character["card-1"].selected
        return 100.0 + 10.0 * ("B-013" in sel) - 7.0 * ("B-020" in sel)

    plan, value, moves = _run(start, score)
    assert plan.per_character["card-1"].selected == {ROOT_NODE, "B-001", "B-013"}
    assert value == score(plan) == 110.0 and moves and value >= score(start)
    assert plan.per_character["card-1"].spent == 8


def test_a_node_others_depend_on_is_never_dropped():
    start = _plan({ROOT_NODE, "B-001", "B-013", "B-020"})

    def score(plan):            # B-013 "hurts", but B-020 hangs off it and earns more
        sel = plan.per_character["card-1"].selected
        if "B-020" in sel and "B-013" not in sel:
            raise AssertionError("evaluated a disconnected board")
        return 100.0 - 1.0 * ("B-013" in sel) + 5.0 * ("B-020" in sel)

    plan, value, _moves = _run(start, score)
    assert plan.per_character["card-1"].selected == {ROOT_NODE, "B-001", "B-013", "B-020"}
    assert value == 104.0


def test_an_activation_node_is_bought_only_within_the_budget():
    start = _plan({ROOT_NODE, "G-001"})

    def score(plan):
        sel = plan.per_character["card-1"].selected
        return 50.0 + 3.0 * ("B-013" in sel)

    rich, value, _ = _run(start, score, budget=20)       # path B-001 + B-013 = 8 points
    assert {"B-001", "B-013"} <= rich.per_character["card-1"].selected and value == 53.0
    poor, value, moves = _run(start, score, budget=7)    # 2 spent + 8 > 7: not affordable
    assert poor.per_character["card-1"].selected == {ROOT_NODE, "G-001"} and value == 50.0 and not moves


def test_nothing_changes_when_nothing_helps_and_cache_keys_are_dropped():
    start = TeamBoardPlan(total_gain=1.0, per_character=_plan({ROOT_NODE, "B-001"}).per_character,
                          plan_keys={"card-1": ("k",)})
    plan, value, moves = _run(start, lambda _p: 7.0)
    assert plan.per_character == start.per_character and value == 7.0 and moves == []
    assert plan.plan_keys == {}       # a refined plan is no longer the cache's assembled one


def test_activation_node_ids_reads_the_two_effect_families():
    skilltree = {"nodes": {"B-013": {"e": {"t": "Act.CD"}}, "B-021": {"e": {"t": "Act.Prob"}},
                           "B-003": {"e": {"t": "Tech"}}, "G-021": {}}}
    assert activation_node_ids(skilltree) == frozenset({"B-013", "B-021"})
