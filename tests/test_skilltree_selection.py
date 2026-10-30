"""Validating an EDITED board selection: connectivity, budget, level gate.

`domain/board.py` scores a board that exists; `domain/skilltree.py` decides
whether a proposed one is buyable. The three rules and their evidence are in
that module's docstring; these tests pin the behaviour that would otherwise be
silently wrong.

**The one that matters is connectivity.** On a real board, 82 stat nodes cost
228 points priced in isolation and **293 connected** — so an editor
that ignored paths would under-price a board by 28 % and move its affordability
from character level 44 to 37. That is not a rounding difference, it is a
different answer.

The geometry fixture below is hand-drawn and small enough to check by eye. The
real 152-node models are exercised separately in
`test_the_real_models_have_the_measured_degree_histogram`, which is the guard
that the 4-neighbour rule is the right one — that histogram comes from the
game's data, and a wrong adjacency rule would not reproduce it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hbuilder.domain.skilltree import (
    MASTER_ROW_DATA_KEY,
    ROOT_NODE,
    TOTAL_BOARD_POINTS,
    SkillTree,
    TreeNode,
    check_placements,
    neighbours,
    player_level_requirement,
    tree_from_mapping,
)

_DUMPS = Path(__file__).resolve().parents[1] / "tools" / "recon" / "proxy" / "dumps"


# --------------------------------------------------------------------------
# A hand-drawn board, checkable by eye.
#
#            (0,2) C  cost 5, needs account level 20
#              |
#            (0,1) B  cost 2
#              |
#   (-1,0) D --(0,0) S-001 -- (1,0) A      D cost 3, A cost 1
#            root, cost 0
#
#   (3,3) FAR  cost 4 — touching nothing, the disconnected case
# --------------------------------------------------------------------------
_POSITIONS = {
    ROOT_NODE: [0, 0],
    "A": [1, 0],
    "B": [0, 1],
    "C": [0, 2],
    "D": [-1, 0],
    "FAR": [3, 3],
}
_NODES = {
    ROOT_NODE: TreeNode(ROOT_NODE, "S", 0, None),
    "A": TreeNode("A", "G", 1, None),
    "B": TreeNode("B", "G", 2, None),
    "C": TreeNode("C", "R", 5, 20),
    "D": TreeNode("D", "B", 3, 10),
    "FAR": TreeNode("FAR", "G", 4, None),
}


@pytest.fixture
def tree() -> SkillTree:
    return SkillTree(
        nodes=_NODES, positions=_POSITIONS, adjacency=neighbours(_POSITIONS)
    )


# ==========================================================================
# 1. Adjacency — orthogonal only
# ==========================================================================


def test_adjacency_is_orthogonal_and_excludes_diagonals():
    """The alternative fails: under 8-neighbour the three outer sockets stop
    being cut vertices, which contradicts the observed boards. So the diagonal
    exclusion is measured, not stylistic."""
    adj = neighbours({"o": [0, 0], "right": [1, 0], "up": [0, 1], "diag": [1, 1]})
    assert adj["o"] == frozenset({"right", "up"})
    assert "diag" not in adj["o"]
    # ... and the relation is symmetric.
    assert "o" in adj["right"] and "o" in adj["up"]


def test_the_real_models_have_the_measured_degree_histogram():
    """The guard that the adjacency rule is RIGHT, not merely implemented.

    Over the real geometry the 154 slots form one connected component with
    degree histogram {1:33, 2:72, 3:27, 4:22} (G-024 hangs off G-022 and G-025
    off G-023, two leaves). A wrong rule would not produce that, so this is a
    check against the game's data rather than against the project's own code."""
    path = _DUMPS / "skilltree.json"
    if not path.exists():
        pytest.skip("dumps/ is gitignored; skilltree.json unavailable here")

    document = json.loads(path.read_text(encoding="utf-8"))
    for model_name, positions in document["models"].items():
        assert len(positions) == 154, model_name
        adj = neighbours(positions)
        histogram: dict[int, int] = {}
        for node in positions:
            histogram[len(adj[node])] = histogram.get(len(adj[node]), 0) + 1
        assert histogram == {1: 33, 2: 72, 3: 27, 4: 22}, (model_name, histogram)

        # One component, no isolated node — reached from the root.
        seen, queue = {ROOT_NODE}, [ROOT_NODE]
        while queue:
            for other in adj[queue.pop()]:
                if other not in seen:
                    seen.add(other)
                    queue.append(other)
        assert len(seen) == 154, model_name


# ==========================================================================
# 2. Connectivity — a node cannot be bought without its path
# ==========================================================================


def test_a_node_touching_nothing_is_rejected(tree):
    verdict = tree.check([ROOT_NODE, "FAR"])
    assert verdict.disconnected == ("FAR",)
    assert not verdict.ok
    assert "no path to S-001" in " ".join(verdict.messages())


def test_a_gap_in_the_middle_disconnects_everything_beyond_it(tree):
    """C sits two cells from the root and B is the only way there. Selecting C
    without B is the mistake an editor must catch — the node is adjacent to a
    node that exists, just not to one that was BOUGHT."""
    assert tree.check([ROOT_NODE, "B", "C"]).ok
    broken = tree.check([ROOT_NODE, "C"])
    assert broken.disconnected == ("C",)


def test_a_selection_without_the_root_is_entirely_disconnected(tree):
    """The root is the only door. Without it nothing is reachable, and every
    selected node is reported rather than just the first."""
    verdict = tree.check(["A", "B"])
    assert verdict.disconnected == ("A", "B")
    assert tree.reachable(["A", "B"]) == frozenset()


def test_every_offender_is_reported_not_just_the_first(tree):
    """An editor marks all bad nodes at once; reporting one at a time is an
    interaction where the user fixes five things in five rounds."""
    verdict = tree.check([ROOT_NODE, "FAR", "C"])
    assert verdict.disconnected == ("C", "FAR")


# ==========================================================================
# 3. Budget
# ==========================================================================


def test_cost_is_summed_and_compared_against_the_budget(tree):
    # root 0 + A 1 + B 2 = 3
    assert tree.check([ROOT_NODE, "A", "B"], budget=3).spent == 3
    assert tree.check([ROOT_NODE, "A", "B"], budget=3).over_budget is False
    assert tree.check([ROOT_NODE, "A", "B"], budget=2).over_budget is True


def test_an_absent_budget_is_not_a_budget_of_zero(tree):
    """`None` means "not stated". A run with no budget stated must not report
    every selection as over-budget."""
    verdict = tree.check([ROOT_NODE, "A", "B"])
    assert verdict.budget is None
    assert verdict.over_budget is False
    assert verdict.ok


def test_the_real_node_costs_sum_to_the_measured_board_total():
    """453 over 154 groups. Also checked here: field 9 does not vary across a group's rows, so a node's price is
    character-independent even for the 15 multi-row groups."""
    path = _DUMPS / "master_SkillTreeNode.json"
    if not path.exists():
        pytest.skip("dumps/ is gitignored; master_SkillTreeNode.json unavailable")

    rows = json.loads(path.read_text(encoding="utf-8"))
    by_group: dict[str, set[int]] = {}
    for row in rows:
        data = row.get(MASTER_ROW_DATA_KEY) or {}
        group = (data.get("1") or [None])[0]
        by_group.setdefault(group, set()).add(int((data.get("9") or [0])[0]))

    assert len(by_group) == 154
    varying = {g: c for g, c in by_group.items() if len(c) > 1}
    assert varying == {}, f"cost varies within a group: {varying}"
    assert sum(next(iter(c)) for c in by_group.values()) == TOTAL_BOARD_POINTS


# ==========================================================================
# 4. The account-level gate, which is NOT the character level
# ==========================================================================


def test_the_gate_is_parsed_and_absence_is_not_zero():
    assert player_level_requirement("cd-player_level-30") == 30
    assert player_level_requirement(None) is None
    assert player_level_requirement("") is None
    # 25 of the 152 nodes are genuinely ungated; that is not "gated at 0".
    assert player_level_requirement("cd-something-else") is None


def test_a_node_above_the_account_level_is_locked(tree):
    """C needs 20, D needs 10."""
    verdict = tree.check([ROOT_NODE, "B", "C", "D"], player_level=10)
    assert verdict.level_locked == (("C", 20),)
    assert not verdict.ok

    assert tree.check([ROOT_NODE, "B", "C", "D"], player_level=20).level_locked == ()


def test_an_unknown_account_level_skips_the_gate_rather_than_assuming_zero(tree):
    """⚠ The account level is in NO capture. An editor that defaulted it to 0
    would grey out 127 of 152 nodes and tell the user their account cannot buy
    things it may well be able to."""
    verdict = tree.check([ROOT_NODE, "B", "C", "D"], player_level=None)
    assert verdict.level_locked == ()
    assert verdict.ok


def test_character_level_and_account_level_are_not_collapsed(tree):
    """They are different mechanisms: the account level gates AVAILABILITY,
    the character level funds the BUDGET. Passing a generous budget must not
    unlock a level-gated node, and a high account level must not pay for one."""
    assert tree.check([ROOT_NODE, "B", "C"], budget=999, player_level=1).level_locked
    starved = tree.check([ROOT_NODE, "B", "C"], budget=1, player_level=99)
    assert starved.level_locked == ()
    assert starved.over_budget is True


# ==========================================================================
# 5. Unknown ids are named, never silently dropped
# ==========================================================================


def test_an_unknown_node_id_is_reported_and_costs_nothing(tree):
    verdict = tree.check([ROOT_NODE, "NOPE"])
    assert verdict.unknown == ("NOPE",)
    assert verdict.spent == 0
    assert not verdict.ok


def test_tree_from_mapping_joins_the_two_sources(tree):
    """`skilltree.json` has the node table and the geometry; the COSTS are
    master data. The join is explicit so the seam stays visible."""
    built = tree_from_mapping(
        {"S-001": {"br": "S"}, "X": {"br": "G", "req": "cd-player_level-15",
                                     "e": {"d": "Grants All Stats UP.", "icon": "ic"}}},
        {"S-001": [0, 0], "X": [1, 0]},
        {"S-001": 0, "X": 7},
    )
    assert built.nodes["X"].cost == 7
    assert built.nodes["X"].player_level == 15
    assert built.nodes["X"].description == "Grants All Stats UP."
    assert built.nodes["S-001"].is_socket
    assert built.adjacency["S-001"] == frozenset({"X"})


# ==========================================================================
# 6. Connect socket placements — the one rule that spans characters
# ==========================================================================


def test_a_card_may_occupy_only_one_socket_account_wide():
    """The board is per character; the collection is not. Placing a card on
    Sora's board consumes it everywhere, so this is the only rule in the module
    that cannot be checked one character at a time."""
    verdict = check_placements(
        {
            "chr-1": {"nodes": [ROOT_NODE], "slots": {ROOT_NODE: "card-A"}},
            "chr-2": {"nodes": [ROOT_NODE], "slots": {ROOT_NODE: "card-A"}},
        }
    )
    assert not verdict.ok
    assert verdict.reused == (("card-A", ("chr-1/S-001", "chr-2/S-001")),)
    assert "only one socket account-wide" in verdict.problems[0]


def test_the_free_root_never_has_to_be_bought_to_hold_a_card():
    """S-001 is often absent from captured node lists, because it is free and
    nobody buys it — while many real placements sit in it. A rule that demanded
    it be present would reject a real board.
    The three OUTER sockets are bought, and there the rule does apply."""
    ok = check_placements({"c": {"nodes": [], "slots": {ROOT_NODE: "card-A"}}})
    assert ok.ok and not ok.socket_not_owned

    unbought = check_placements({"c": {"nodes": [ROOT_NODE], "slots": {"S-003": "card-A"}}})
    assert not unbought.ok
    assert unbought.socket_not_owned == (("c", "S-003"),)

    bought = check_placements(
        {"c": {"nodes": [ROOT_NODE, "S-003"], "slots": {"S-003": "card-A"}}}
    )
    assert bought.ok


def test_a_non_socket_slot_is_rejected():
    verdict = check_placements({"c": {"nodes": ["G-001"], "slots": {"G-001": "card-A"}}})
    assert not verdict.ok
    assert verdict.not_a_socket == (("c", "G-001"),)


def test_a_card_without_a_connect_effect_warns_but_is_legal():
    """Only rarity 4 and 5 carry a Connect effect (113 of 167). The game lets
    you socket a rarity-3 card and it grants nothing — refusing it would invent
    a rule, and staying silent would let the user think the slot works."""
    verdict = check_placements(
        {"c": {"nodes": [], "slots": {ROOT_NODE: "card-3star"}}},
        cards_with_connect={"card-5star"},
    )
    assert verdict.ok, "pointless is not illegal"
    assert verdict.warnings and "grants nothing" in verdict.warnings[0]

    skipped = check_placements({"c": {"nodes": [], "slots": {ROOT_NODE: "card-3star"}}})
    assert not skipped.warnings, "omitting the card set SKIPS the check, never guesses"


def test_the_real_capture_satisfies_every_placement_rule():
    """The guard that the rules match the GAME rather than an idea of it: the
    account's own real placements must pass unchanged."""
    path = _DUMPS / "skilltree.json"
    if not path.exists():
        pytest.skip("dumps/ is gitignored; skilltree.json unavailable here")

    document = json.loads(path.read_text(encoding="utf-8"))
    placed = document["placed"]
    # ⚠ The file EXISTING is not the same as the capture being in it.
    # `build_skilltree.py` writes the geometry (152 nodes x 4 layouts) from the
    # master alone and merges the account's placements from
    # `dumps/board_state.json`, which is a live capture. Rebuilt without that
    # capture, the file is present, valid, and carries `"placed": {}` -- and
    # absent ACCOUNT DATA must skip, as every other dumps-backed test here does,
    # not fail.
    if not placed:
        pytest.skip(
            "skilltree.json carries no captured placements -- refresh with "
            "account_snapshot.py --refresh-board-state, then build_skilltree.py"
        )
    unlocked = document["unlocked"]
    boards = {
        holomem: {"nodes": unlocked.get(holomem) or [], "slots": slots}
        for holomem, slots in placed.items()
    }
    verdict = check_placements(boards, cards_with_connect=set(document["card_connect"]))
    assert verdict.ok, verdict.problems
    assert not verdict.warnings, verdict.warnings
    # Coverage, not a pin. An exact placement count is ACCOUNT STATE: it moves
    # the next time a socket is filled, and that failure would say nothing about
    # the rules. What is load-bearing is the two assertions above: the real
    # placements satisfy every rule. The floor below only keeps the check from
    # passing vacuously on a thin capture.
    total = sum(len(s) for s in placed.values())
    assert total >= 20, f"only {total} placements captured — too thin to prove much"
