"""The Holomem Board as an EDITABLE selection — what a node costs, what gates
it, and whether a chosen set is legal.

`domain/board.py` scores a board that already exists. This module answers the
question before that one: *may the player buy this set of nodes, and what does
it cost?* It is what the board planner (`board_optimizer`) searches within: the
captured board records what IS, planning proposes what COULD BE (unspent points,
characters with no captured board). This module never writes. The validators
that apply the rules below to a selection, and the account-wide socket placement
rule, live in `tests/support/board_rules.py`.

## The three rules a selection must satisfy

1. **Connectivity.** Adjacency is the **orthogonal 4-neighbour grid** on the
   node positions. Diagonals are NOT edges: under 8-neighbour the three outer
   sockets stop being cut vertices, which contradicts the account's boards.

   So **a node cannot be bought without its path**: the stat nodes alone cost
   228 points in isolation and **293 connected** (character level 37 vs 44).

2. **Budget.** Point costs are master `SkillTreeNode` field 9, summing to
   **453** over the 154 groups. The cost does NOT vary across a group's rows,
   so a node's price is character-independent even for the 15 multi-row groups.

3. **Player level.** Most nodes carry a `cd-player_level-N` unlock (levels
   10 to 30); the rest are ungated. This is ACCOUNT level, not character
   level, and it gates *availability*; character level funds the *budget*.
   Two different mechanisms that must not be collapsed.

`S-001` is the root: cost 0, no unlock requirement, at position (0, 0) on every
model.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "ROOT_NODE",
    "TOTAL_BOARD_POINTS",
    "TreeNode",
    "SkillTree",
    "player_level_requirement",
    "neighbours",
    "tree_from_mapping",
]

# The board's origin: the only node with
# node_type 1, no effect, no item cost, no unlock requirement, at (0, 0) on
# every one of the four models.
ROOT_NODE = "S-001"

# Σ field 9 over all 154 node groups (including G-024 and G-025, two 3-point
# minigame-reward nodes).
TOTAL_BOARD_POINTS = 453

_PLAYER_LEVEL_RE = re.compile(r"cd-player_level-(\d+)")


def player_level_requirement(req: str | None) -> int | None:
    """The account level a node needs, or `None` when it is ungated.

    `None` is "no requirement", never "level 0" — some nodes are
    genuinely ungated and that is a different fact from a gate at zero."""
    if not req:
        return None
    match = _PLAYER_LEVEL_RE.search(req)
    return int(match.group(1)) if match else None


def neighbours(
    positions: Mapping[str, Sequence[int]],
) -> dict[str, frozenset[str]]:
    """4-neighbour adjacency over a model's node positions (orthogonal only: the
    rule that reproduces the account's boards)."""
    by_cell: dict[tuple[int, int], str] = {
        (int(pos[0]), int(pos[1])): node for node, pos in positions.items()
    }
    out: dict[str, set[str]] = {node: set() for node in positions}
    for (x, y), node in by_cell.items():
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            other = by_cell.get((x + dx, y + dy))
            if other is not None:
                out[node].add(other)
    return {node: frozenset(adj) for node, adj in out.items()}


@dataclass(frozen=True, slots=True)
class TreeNode:
    """One of the 154 node groups."""

    node_id: str
    branch: str
    cost: int
    player_level: int | None
    description: str = ""
    icon: str = ""


@dataclass(frozen=True, slots=True)
class SkillTree:
    """The 154 node groups plus one model's geometry.

    Geometry is per-model (there are four), so a `SkillTree` is bound to the
    model the character uses. The node table itself is shared."""

    nodes: Mapping[str, TreeNode]
    positions: Mapping[str, Sequence[int]]
    adjacency: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def cost_of(self, node_ids: Iterable[str]) -> int:
        """Total point cost. Unknown ids contribute 0 rather than raising —
        a validator names them, and a cost function that raised would stop the
        editor from showing the rest of its report."""
        return sum(self.nodes[node].cost for node in node_ids if node in self.nodes)

    def reachable(self, selected: Iterable[str]) -> frozenset[str]:
        """The selected nodes connected to `ROOT_NODE` *through other selected
        nodes*. A flood fill, not a path check per node: the constraint is that
        the bought set forms one component containing the root."""
        chosen = {node for node in selected if node in self.nodes}
        if ROOT_NODE not in chosen:
            return frozenset()
        seen = {ROOT_NODE}
        queue = deque([ROOT_NODE])
        while queue:
            node = queue.popleft()
            for other in self.adjacency.get(node, ()):
                if other in chosen and other not in seen:
                    seen.add(other)
                    queue.append(other)
        return frozenset(seen)


def tree_from_mapping(
    nodes_raw: Mapping[str, Mapping],
    positions: Mapping[str, Sequence[int]],
    costs: Mapping[str, int],
) -> SkillTree:
    """Build a `SkillTree` from `skilltree.json`'s node table, one model's
    positions, and the per-node point costs from master `SkillTreeNode` field 9
    (which `skilltree.json` does not carry)."""
    nodes = {
        node_id: TreeNode(
            node_id=node_id,
            branch=raw.get("br", ""),
            cost=int(costs.get(node_id, 0)),
            player_level=player_level_requirement(raw.get("req")),
            description=((raw.get("e") or {}) or {}).get("d", "") or "",
            icon=((raw.get("e") or {}) or {}).get("icon", "") or "",
        )
        for node_id, raw in nodes_raw.items()
    }
    return SkillTree(nodes=nodes, positions=positions, adjacency=neighbours(positions))
