"""The board-plan engine — choose the nodes to buy that maximise a team's board
value, within the character's point budget and the tree's connectivity.

This is the "optimise boards" step the frontend does per team: `skilltree.py`
supplies the rules (per-node cost, the 4-neighbour grid, the root the bought set
must connect to, per-node level gates); this searches within them.

The problem is a **prize-collecting connected subgraph under a budget** — a node
is only worth buying with a path back to the root, and a valuable node can sit
behind cheap zero-value connector nodes. A pure value/cost frontier greedy would
never buy a connector, so this bundles each step: it scores every reachable node
by the value of *everything newly bought to reach it* over the cost of that
path (a cost-weighted shortest path over unbought nodes), buys the best bundle,
and repeats until nothing affordable adds value. Approximate — like the team
solver's own beam — but connectivity- and budget-exact by construction:
`tree.reachable(plan.selected)` always returns the whole selection.

`board_ceiling.py` decides WHICH teams reach this engine: its upper bound ranks
the candidates and only the top-N are optimised, so the expensive search never
runs on a team that cannot win.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Mapping

from hbuilder.domain.skilltree import SkillTree, ROOT_NODE


@dataclass(frozen=True, slots=True)
class BoardPlan:
    """The bought node set, its point cost, and the team value it realises."""

    selected: frozenset[str]
    spent: int
    value: float
    # The order paths were bought, for an explainable "unlock these next" list.
    steps: tuple[frozenset[str], ...] = field(default_factory=tuple)


def _available(tree: SkillTree, player_level: int | None) -> set[str]:
    """Node ids the account may buy — every node, minus those gated above the
    player level (when a level is supplied; unknown level gates nothing)."""
    out = set()
    for node_id, node in tree.nodes.items():
        need = node.player_level
        if player_level is None or need is None or player_level >= need:
            out.add(node_id)
    return out


def _cheapest_bundles(
    tree: SkillTree,
    selected: frozenset[str],
    buyable: set[str],
    costs: dict[str, int],
) -> dict[str, tuple[int, frozenset[str]]]:
    """For every buyable node not yet selected, the minimum unbought-cost path
    from the current selection to it, as `(added_cost, added_nodes)`. A
    Dijkstra whose edge weight is the *destination* node's cost, seeded with the
    whole selected frontier at cost 0 — so a node already bought is free to pass
    through and reaching an owned node is a no-op."""
    best: dict[str, tuple[int, frozenset[str]]] = {}
    # (cost_so_far, node, path_added_nodes)
    heap: list[tuple[int, str, tuple[str, ...]]] = []
    for node in selected:
        heapq.heappush(heap, (0, node, ()))
    settled: dict[str, int] = {}
    while heap:
        cost, node, added = heapq.heappop(heap)
        if node in settled and settled[node] <= cost:
            continue
        settled[node] = cost
        if node not in selected and node in buyable:
            best[node] = (cost, frozenset(added))
        for nxt in tree.adjacency.get(node, ()):
            if nxt not in buyable and nxt not in selected:
                continue  # cannot route through a level-locked node
            step = costs.get(nxt, 0) if nxt not in selected else 0
            new_cost = cost + step
            if nxt not in settled or new_cost < settled[nxt]:
                new_added = added if nxt in selected else (*added, nxt)
                heapq.heappush(heap, (new_cost, nxt, new_added))
    return best


def optimize_board(
    tree: SkillTree,
    budget: int,
    node_value: Mapping[str, float],
    *,
    player_level: int | None = None,
    owned: frozenset[str] = frozenset(),
) -> BoardPlan:
    """Greedy path-bundling knapsack. Starts from the root (and any `owned`
    nodes, free), and buys the best value/cost bundle each step until the budget
    can afford no further value-adding path."""
    buyable = _available(tree, player_level)
    costs = {nid: tree.nodes[nid].cost for nid in tree.nodes}
    selected: set[str] = {ROOT_NODE} | {n for n in owned if n in tree.nodes}
    spent = tree.cost_of(selected - {ROOT_NODE})
    value = sum(node_value.get(n, 0.0) for n in selected)
    steps: list[frozenset[str]] = []

    while True:
        bundles = _cheapest_bundles(tree, frozenset(selected), buyable, costs)
        best_node = None
        best_ratio = 0.0
        best_bundle: tuple[int, frozenset[str]] = (0, frozenset())
        # SORTED, the candidates and each sum: in set order a tie (or the last bit of
        # a float sum) would follow the per-process string hash seed, so two
        # processes could plan different boards for the same team
        for node in sorted(bundles):
            added_cost, added_nodes = bundles[node]
            if spent + added_cost > budget:
                continue
            gained = sum(node_value.get(n, 0.0) for n in sorted(added_nodes))
            if gained <= 0.0:
                continue
            ratio = gained / added_cost if added_cost > 0 else float("inf")
            if ratio > best_ratio:
                best_ratio, best_node, best_bundle = ratio, node, (added_cost, added_nodes)
        if best_node is None:
            break
        added_cost, added_nodes = best_bundle
        selected |= set(added_nodes)
        spent += added_cost
        value += sum(node_value.get(n, 0.0) for n in sorted(added_nodes))
        steps.append(added_nodes)

    return BoardPlan(
        selected=frozenset(selected), spent=spent, value=value, steps=tuple(steps)
    )
