"""The Holomem Board rules, written independently of the planner.

`hbuilder.domain.skilltree` holds the board's geometry and prices; this module
applies the game's rules to a chosen board and reports every way it breaks
them. The tests run it on hand-drawn boards and on every plan the board
planner returns, so a planner that bought an unconnected node, overspent a
board or reused a card would fail here rather than be scored as if it could.

Per character (`check`):

1. **Connectivity.** The bought set forms one component containing
   `ROOT_NODE` under the orthogonal 4-neighbour adjacency: a node cannot be
   bought without its path.
2. **Budget.** The point costs sum to no more than the character's budget.
3. **Account level.** A node gated by `cd-player_level-N` needs account level
   N; this is a different mechanism from the character level that funds the
   budget.

Across the account (`check_placements`): a card occupies at most one Connect
socket, a slot key is a socket, and a socket holding a card is bought (the
free root excepted).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from hbuilder.domain.skilltree import ROOT_NODE, SkillTree, tree_from_mapping

__all__ = [
    "MASTER_ROW_DATA_KEY",
    "SelectionVerdict",
    "PlacementVerdict",
    "check",
    "check_placements",
    "trees_by_character",
    "check_plan",
]

# The master cache's own field-bag key inside a decoded row: the numbered
# protobuf fields live under it.
MASTER_ROW_DATA_KEY = "data"


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
        return not (self.disconnected or self.unknown or self.level_locked or self.over_budget)

    def messages(self) -> tuple[str, ...]:
        out: list[str] = []
        if self.unknown:
            out.append(f"{len(self.unknown)} node(s) are not in the tree: " + ", ".join(self.unknown[:6]))
        if self.disconnected:
            out.append(
                f"{len(self.disconnected)} node(s) have no path to {ROOT_NODE}: "
                + ", ".join(self.disconnected[:6])
                + " — a node cannot be bought without its path"
            )
        if self.level_locked:
            worst = max(level for _node, level in self.level_locked)
            out.append(f"{len(self.level_locked)} node(s) need a higher account level (up to {worst})")
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
        return not (self.reused or self.not_a_socket or self.socket_not_owned)

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
            f"{holomem}: {card} in {slot} grants nothing — it has no Connect effect (only rarity 4 and 5 do)"
            for holomem, slot, card in self.without_effect
        )


def check(
    tree: SkillTree,
    selected: Iterable[str],
    *,
    budget: int | None = None,
    player_level: int | None = None,
) -> SelectionVerdict:
    """Validate a selection on `tree` against all three per-character rules.

    `player_level is None` means "not told us" and SKIPS the gate check
    entirely — it never means level 0. The account level may be unknown,
    and an editor that assumed a number would invent a restriction the
    account may not have."""
    chosen = list(selected)
    unknown = tuple(sorted({n for n in chosen if n not in tree.nodes}))
    known = {n for n in chosen if n in tree.nodes}

    connected = tree.reachable(known)
    disconnected = tuple(sorted(known - connected))

    level_locked: list[tuple[str, int]] = []
    if player_level is not None:
        for node in sorted(known):
            need = tree.nodes[node].player_level
            if need is not None and player_level < need:
                level_locked.append((node, need))

    return SelectionVerdict(
        spent=tree.cost_of(known),
        budget=budget,
        disconnected=disconnected,
        unknown=unknown,
        level_locked=tuple(level_locked),
    )


def check_placements(
    boards: Mapping[str, Mapping[str, object]],
    *,
    cards_with_connect: Collection[str] | None = None,
) -> PlacementVerdict:
    """Validate Connect socket placements across every character at once.

    `boards` is `{holomem: {"nodes": [...], "slots": {socket: card_id}}}` — the
    shape the collection document, `/api/board` and the board planner use.

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

    reused = tuple((card, tuple(seats)) for card, seats in sorted(where.items()) if len(seats) > 1)
    return PlacementVerdict(
        reused=reused,
        not_a_socket=tuple(not_a_socket),
        socket_not_owned=tuple(socket_not_owned),
        without_effect=tuple(without_effect),
    )


def trees_by_character(
    skilltree: Mapping[str, object],
    master_rows: Iterable[Mapping[str, object]],
    *,
    data_key: str,
) -> dict[str, SkillTree]:
    """One `SkillTree` per character from `dumps/skilltree.json` (the node
    table, the four models' geometry and which model each character uses) and
    the master `SkillTreeNode` rows (field 1 the node group, field 9 its point
    cost). The prices come from the master rows, not from the planner, so the
    budget check does not reuse the planner's own cost table."""
    costs: dict[str, int] = {}
    for row in master_rows:
        data = row.get(data_key) or {}
        group = (data.get("1") or [None])[0]
        if isinstance(group, str):
            costs[group] = int((data.get("9") or [0])[0])
    by_model = {
        name: tree_from_mapping(skilltree["nodes"], positions, costs)
        for name, positions in skilltree["models"].items()
    }
    return {m["chr"]: by_model[m["model"]] for m in skilltree["members"]}


def check_plan(
    plan: Mapping[str, Mapping[str, object]],
    trees: Mapping[str, SkillTree],
    budgets: Mapping[str, int],
) -> tuple[dict[str, SelectionVerdict], PlacementVerdict]:
    """Every board of a planner result checked per character, and its socket
    placements checked account-wide.

    `plan` is the planner's `{chr: {"nodes": [...], "slots": {...}}}`. The
    planner leaves the free root out of a node list, as captures do, so it is
    added before the connectivity check. A character missing from `trees` is
    reported as a board whose every node is unknown rather than skipped."""
    empty = SkillTree(nodes={}, positions={})
    verdicts = {
        cid: check(
            trees.get(cid, empty),
            {ROOT_NODE, *(board.get("nodes") or ())},
            budget=int(budgets.get(cid, 0)),
        )
        for cid, board in plan.items()
    }
    return verdicts, check_placements(plan)
