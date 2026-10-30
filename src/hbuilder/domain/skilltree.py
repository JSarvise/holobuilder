"""The Holomem Board as an EDITABLE selection — what a node costs, what gates
it, and whether a chosen set is legal.

`domain/board.py` scores a board that already exists. This module answers the
question before that one: *may the player buy this set of nodes, and what does
it cost?* It is what the board editor validates against, and what a
node-purchase optimiser would search within.

## Why an editor exists at all, given the board is captured

The current board is read from the account, not typed, and the character level
follows from the captured point balance. That records **current** state.

Editing is a different job:

* **Planning.** Earned points often sit unspent. Answering "where should they
  go" means proposing a board that does not exist yet, which no capture can
  supply.
* **Coverage.** Characters that appear in no capture have no board at all on
  disk.

So: capture for what IS, editing for what COULD BE. This module never writes,
and it is deliberately ignorant of where the selection came from.

## The three rules a selection must satisfy

1. **Connectivity — the expensive one.** Adjacency is the **orthogonal
   4-neighbour grid** on the node positions: the slots form one connected
   component, and every observed live board is connected to the root under
   this rule. Diagonals are NOT edges — under 8-neighbour the three outer
   sockets stop being cut vertices, which contradicts the observed boards, so
   the rule is decidable rather than a preference.

   The consequence is the whole point: **a node cannot be bought without its
   path**. The stat nodes alone cost 228 points in isolation and **293
   connected**, which moves them from character level 37 to 44. An editor that ignored connectivity would quietly price a board the game
   will not sell.

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
from dataclasses import dataclass, field
from typing import Collection, Iterable, Mapping, Sequence

__all__ = [
    "MASTER_ROW_DATA_KEY",
    "ROOT_NODE",
    "TOTAL_BOARD_POINTS",
    "TreeNode",
    "SkillTree",
    "SelectionVerdict",
    "PlacementVerdict",
    "check_placements",
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

# The master cache's own field-bag key inside a decoded row: the numbered
# protobuf fields live under it. Named here for the same reason
# `build_board.ROW_DATA_KEY` is named — so a caller reading master rows does
# not re-type it — and additionally because the bare string is on
# `tests/test_fixture_isolation.py`'s forbidden list, which exists to stop a
# `data/cards.json` path being reconstructed from fragments in a test. That
# guard cannot tell this key from that path, and it is right not to guess.
MASTER_ROW_DATA_KEY = "data"

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
    """4-neighbour adjacency over a model's node positions.

    Orthogonal only. This rule — and not 8-neighbour — reproduces the
    observed boards, so the choice is evidence, not convention."""
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

    @property
    def is_socket(self) -> bool:
        return self.branch == "S"


@dataclass(frozen=True, slots=True)
class SelectionVerdict:
    """Whether a chosen node set is buyable, and every reason it is not.

    Errors are reported as a LIST rather than as a first failure, because an
    editor has to mark every bad node at once — telling the user about one
    disconnected node, then another after they fix it, is the interaction this
    avoids. `spent` and over-budget are reported even when the selection is
    otherwise illegal, so the two questions stay separable."""

    spent: int
    budget: int | None
    disconnected: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    level_locked: tuple[tuple[str, int], ...] = ()

    @property
    def over_budget(self) -> bool:
        return self.budget is not None and self.spent > self.budget

    @property
    def ok(self) -> bool:
        return not (
            self.disconnected or self.unknown or self.level_locked or self.over_budget
        )

    def messages(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.unknown:
            out.append(
                f"{len(self.unknown)} node(s) are not in the tree: "
                + ", ".join(self.unknown[:6])
            )
        if self.disconnected:
            out.append(
                f"{len(self.disconnected)} node(s) have no path to {ROOT_NODE}: "
                + ", ".join(self.disconnected[:6])
                + " — a node cannot be bought without its path"
            )
        if self.level_locked:
            worst = max(level for _node, level in self.level_locked)
            out.append(
                f"{len(self.level_locked)} node(s) need a higher account level "
                f"(up to {worst})"
            )
        if self.over_budget and self.budget is not None:
            out.append(
                f"{self.spent} points spent against a budget of {self.budget} "
                f"— over by {self.spent - self.budget}"
            )
        return tuple(out)


@dataclass(frozen=True, slots=True)
class PlacementVerdict:
    """Whether a whole ACCOUNT's Connect socket placements are legal.

    Every other check in this module is per character. This one cannot be:
    **a card may occupy only one socket account-wide**, which is a constraint
    between characters and therefore has no home on a single `SkillTree`.

    Like `SelectionVerdict`, it reports every category at once rather than
    raising on the first — an editor that surfaced one problem, then another
    after the user fixed it, is the interaction this shape exists to avoid."""

    reused: tuple[tuple[str, tuple[str, ...]], ...] = ()
    not_a_socket: tuple[tuple[str, str], ...] = ()
    socket_not_owned: tuple[tuple[str, str], ...] = ()
    without_effect: tuple[tuple[str, str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not (
            self.reused or self.not_a_socket or self.socket_not_owned
        )

    @property
    def problems(self) -> tuple[str, ...]:
        out: list[str] = []
        for card, where in self.reused:
            out.append(
                f"{card} is in {len(where)} sockets ({', '.join(where)}) — a card "
                f"can occupy only one socket account-wide"
            )
        for holomem, slot in self.not_a_socket:
            out.append(f"{holomem}: {slot} is not a Connect socket")
        for holomem, slot in self.socket_not_owned:
            out.append(f"{holomem}: {slot} holds a card but has not been bought")
        return tuple(out)

    @property
    def warnings(self) -> tuple[str, ...]:
        """Legal, but pointless — kept apart from `problems` deliberately.

        A rarity-3 card in a socket is something the game lets you do and that
        buys nothing: only rarity 4 and 5 carry a Connect effect at all.
        Refusing it would be inventing a rule; staying silent would let the
        user think a slot was working."""
        return tuple(
            f"{holomem}: {card} in {slot} grants nothing — it has no Connect "
            f"effect (only rarity 4 and 5 do)"
            for holomem, slot, card in self.without_effect
        )


def check_placements(
    boards: Mapping[str, Mapping[str, object]],
    *,
    cards_with_connect: Collection[str] | None = None,
) -> PlacementVerdict:
    """Validate Connect socket placements across every character at once.

    `boards` is `{holomem: {"nodes": [...], "slots": {socket: card_id}}}` — the
    shape the collection document and `/api/board` already use.

    Three rules, and one thing that is deliberately NOT a rule:

    1. **One socket per card, account-wide.** The board is per character but the
       collection is not: placing a card on Sora's board consumes it everywhere.
    2. **A slot key must be a socket** (`S-*`).
    3. **The socket must be owned** — except `ROOT_NODE`, which is free and is
       therefore usually absent from a purchased set: captured node lists
       rarely contain S-001 because nobody buys it. A rule that required S-001
       to be present would reject most real placements.

    NOT a rule: a rarity-3 card in a socket. The game permits it and it grants
    nothing, so it is reported as a warning. Pass `cards_with_connect` to get
    that check; omit it and the check is skipped rather than guessed at."""
    where: dict[str, list[str]] = {}
    not_a_socket: list[tuple[str, str]] = []
    socket_not_owned: list[tuple[str, str]] = []
    without_effect: list[tuple[str, str, str]] = []

    for holomem in sorted(boards):
        board = boards[holomem] or {}
        slots = board.get("slots") or {}
        if not isinstance(slots, Mapping):
            continue
        owned = {n for n in (board.get("nodes") or ()) if isinstance(n, str)}
        for slot in sorted(slots):
            card = slots[slot]
            if not isinstance(card, str) or not card:
                continue
            if not slot.startswith("S-"):
                not_a_socket.append((holomem, slot))
            elif slot != ROOT_NODE and slot not in owned:
                socket_not_owned.append((holomem, slot))
            if cards_with_connect is not None and card not in cards_with_connect:
                without_effect.append((holomem, slot, card))
            where.setdefault(card, []).append(f"{holomem}/{slot}")

    reused = tuple(
        (card, tuple(seats)) for card, seats in sorted(where.items()) if len(seats) > 1
    )
    return PlacementVerdict(
        reused=reused,
        not_a_socket=tuple(not_a_socket),
        socket_not_owned=tuple(socket_not_owned),
        without_effect=tuple(without_effect),
    )


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
        `check` names them, and a cost function that raised would stop the
        editor from showing the rest of its report."""
        return sum(
            self.nodes[node].cost for node in node_ids if node in self.nodes
        )

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

    def check(
        self,
        selected: Iterable[str],
        *,
        budget: int | None = None,
        player_level: int | None = None,
    ) -> SelectionVerdict:
        """Validate a selection against all three rules.

        `player_level is None` means "not told us" and SKIPS the gate check
        entirely — it never means level 0. The account level may be unknown,
        and an editor that assumed a number would invent a restriction the
        account may not have."""
        chosen = list(selected)
        unknown = tuple(sorted({n for n in chosen if n not in self.nodes}))
        known = {n for n in chosen if n in self.nodes}

        connected = self.reachable(known)
        disconnected = tuple(sorted(known - connected))

        level_locked: list[tuple[str, int]] = []
        if player_level is not None:
            for node in sorted(known):
                need = self.nodes[node].player_level
                if need is not None and player_level < need:
                    level_locked.append((node, need))

        return SelectionVerdict(
            spent=self.cost_of(known),
            budget=budget,
            disconnected=disconnected,
            unknown=unknown,
            level_locked=tuple(level_locked),
        )


def tree_from_mapping(
    nodes_raw: Mapping[str, Mapping],
    positions: Mapping[str, Sequence[int]],
    costs: Mapping[str, int],
) -> SkillTree:
    """Build a `SkillTree` from `skilltree.json`'s node table, one model's
    positions, and the per-node point costs read from master `SkillTreeNode`
    field 9.

    Costs come in separately because `skilltree.json` does not carry them —
    they are master data, and joining the two here keeps the seam visible
    rather than pretending one file holds both."""
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
    return SkillTree(
        nodes=nodes, positions=positions, adjacency=neighbours(positions)
    )
