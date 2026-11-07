"""The board rules (`tests/support/board_rules.py`): connectivity, budget,
account-level gate and Connect socket placement.

**The one that matters is connectivity.** On a real board, 82 stat nodes cost
228 points priced in isolation and **293 connected** — so an editor
that ignored paths would under-price a board by 28 % and move its affordability
from character level 44 to 37. That is not a rounding difference, it is a
different answer.

The geometry fixture below is hand-drawn and small enough to check by eye. The
last test applies the same rules to the boards the exact planner returns.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from hbuilder.domain.skilltree import (
    ROOT_NODE,
    SkillTree,
    TreeNode,
    neighbours,
)
from support.board_rules import (
    MASTER_ROW_DATA_KEY,
    check,
    check_placements,
    check_plan,
    trees_by_character,
)

_REPO = Path(__file__).resolve().parents[1]
_REFRESH = _REPO / "tools" / "refresh"
_DUMPS = _REPO / "tools" / "recon" / "proxy" / "dumps"


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
# 1. Connectivity — a node cannot be bought without its path
# ==========================================================================


def test_a_node_touching_nothing_is_rejected(tree):
    verdict = check(tree, [ROOT_NODE, "FAR"])
    assert verdict.disconnected == ("FAR",)
    assert not verdict.ok
    assert "no path to S-001" in " ".join(verdict.messages())


def test_a_gap_in_the_middle_disconnects_everything_beyond_it(tree):
    """C sits two cells from the root and B is the only way there. Selecting C
    without B is the mistake an editor must catch — the node is adjacent to a
    node that exists, just not to one that was BOUGHT."""
    assert check(tree, [ROOT_NODE, "B", "C"]).ok
    broken = check(tree, [ROOT_NODE, "C"])
    assert broken.disconnected == ("C",)


def test_a_selection_without_the_root_is_entirely_disconnected(tree):
    """The root is the only door. Without it nothing is reachable, and every
    selected node is reported rather than just the first."""
    verdict = check(tree, ["A", "B"])
    assert verdict.disconnected == ("A", "B")
    assert tree.reachable(["A", "B"]) == frozenset()


def test_every_offender_is_reported_not_just_the_first(tree):
    """An editor marks all bad nodes at once; reporting one at a time is an
    interaction where the user fixes five things in five rounds."""
    verdict = check(tree, [ROOT_NODE, "FAR", "C"])
    assert verdict.disconnected == ("C", "FAR")


# ==========================================================================
# 2. Budget
# ==========================================================================


def test_cost_is_summed_and_compared_against_the_budget(tree):
    # root 0 + A 1 + B 2 = 3
    assert check(tree, [ROOT_NODE, "A", "B"], budget=3).spent == 3
    assert check(tree, [ROOT_NODE, "A", "B"], budget=3).over_budget is False
    assert check(tree, [ROOT_NODE, "A", "B"], budget=2).over_budget is True


def test_an_absent_budget_is_not_a_budget_of_zero(tree):
    """`None` means "not stated". A run with no budget stated must not report
    every selection as over-budget."""
    verdict = check(tree, [ROOT_NODE, "A", "B"])
    assert verdict.budget is None
    assert verdict.over_budget is False
    assert verdict.ok


# ==========================================================================
# 3. The account-level gate, which is NOT the character level
# ==========================================================================


def test_a_node_above_the_account_level_is_locked(tree):
    """C needs 20, D needs 10."""
    verdict = check(tree, [ROOT_NODE, "B", "C", "D"], player_level=10)
    assert verdict.level_locked == (("C", 20),)
    assert not verdict.ok

    assert check(tree, [ROOT_NODE, "B", "C", "D"], player_level=20).level_locked == ()


def test_an_unknown_account_level_skips_the_gate_rather_than_assuming_zero(tree):
    """The account level is in no capture. An editor that defaulted it to 0
    would grey out most nodes and tell the user their account cannot buy
    things it may well be able to."""
    verdict = check(tree, [ROOT_NODE, "B", "C", "D"], player_level=None)
    assert verdict.level_locked == ()
    assert verdict.ok


def test_character_level_and_account_level_are_not_collapsed(tree):
    """They are different mechanisms: the account level gates AVAILABILITY,
    the character level funds the BUDGET. Passing a generous budget must not
    unlock a level-gated node, and a high account level must not pay for one."""
    assert check(tree, [ROOT_NODE, "B", "C"], budget=999, player_level=1).level_locked
    starved = check(tree, [ROOT_NODE, "B", "C"], budget=1, player_level=99)
    assert starved.level_locked == ()
    assert starved.over_budget is True


# ==========================================================================
# 4. Unknown ids are named, never silently dropped
# ==========================================================================


def test_an_unknown_node_id_is_reported_and_costs_nothing(tree):
    verdict = check(tree, [ROOT_NODE, "NOPE"])
    assert verdict.unknown == ("NOPE",)
    assert verdict.spent == 0
    assert not verdict.ok


# ==========================================================================
# 5. Connect socket placements — the one rule that spans characters
# ==========================================================================


def test_a_card_may_occupy_only_one_socket_account_wide():
    """The board is per character; the collection is not. Placing a card on
    Sora's board consumes it everywhere, so this is the only rule that cannot
    be checked one character at a time."""
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
    """Only rarity 4 and 5 carry a Connect effect. The game lets you socket a
    rarity-3 card and it grants nothing — refusing it would invent a rule, and
    staying silent would let the user think the slot works."""
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
    # The file existing is not the same as the capture being in it:
    # `build_skilltree.py` writes the geometry from the master alone and merges
    # the account's placements from `dumps/board_state.json`. Rebuilt without
    # that capture, the file carries `"placed": {}`, and absent account data
    # skips rather than fails.
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
    # Coverage, not a pin: the exact placement count is account state. The
    # floor only keeps the check from passing vacuously on a thin capture.
    total = sum(len(s) for s in placed.values())
    assert total >= 20, f"only {total} placements captured — too thin to prove much"


# ==========================================================================
# 6. Every plan the exact planner returns obeys the rules
# ==========================================================================

# The planner tests' team: an outfit leader (the same card again as the fifth
# member) with four members of other characters.
_PLAN_TEAM = ["card-04001-5-uniq-0042-00", "card-00023-5-uniq-0019-00",
              "card-04003-5-uniq-0044-00", "card-00031-5-uniq-0025-00",
              "card-03002-5-uniq-0034-00", "card-04001-5-uniq-0042-00"]


def test_every_board_the_planner_returns_is_connected_within_budget_and_socket_legal():
    """`board_plan_solver.solve_team_board` on the account's own boards: a song
    plan with the bench developed and a song-less plan that may only add nodes,
    both with the Connect sockets filled. Every returned board must be one
    component with the root, cost no more than its character's points (the
    master's prices, not the planner's), and the sockets must hold each card
    once, on bought sockets."""
    pytest.importorskip("highspy")
    needed = [_DUMPS / name for name in ("board_state.json", "skilltree.json",
                                         "master_SkillTreeNode.json")]
    missing = [p.name for p in needed if not p.exists()]
    if missing:
        pytest.skip(f"the planner's account inputs are gitignored dumps: {missing}")
    if str(_REFRESH) not in sys.path:
        sys.path.insert(0, str(_REFRESH))
    import board_plan_solver  # noqa: PLC0415
    import tree_optimise as to  # noqa: PLC0415

    trees = trees_by_character(
        json.loads((_DUMPS / "skilltree.json").read_text(encoding="utf-8")),
        json.loads((_DUMPS / "master_SkillTreeNode.json").read_text(encoding="utf-8")),
        data_key=MASTER_ROW_DATA_KEY,
    )
    balance, _source = to.balance_from_disk()
    _wallet, points, _bare, _levels = to.respec_budget(to._owned_nodes(), to._prices(), balance)

    started = time.perf_counter()
    plans = {
        "song m0334": board_plan_solver.solve_team_board(
            scenario="account", song="m0334", pool=1, for_team=_PLAN_TEAM,
            include_sockets=True),
        "no song, no resets": board_plan_solver.solve_team_board(
            scenario="account", song=None, pool=1, for_team=_PLAN_TEAM,
            include_sockets=True, max_resets=0),
    }
    elapsed = time.perf_counter() - started

    failures: list[str] = []
    boards = sockets = 0
    for name, plan in plans.items():
        assert plan, f"{name}: the planner returned no plan"
        verdicts, placements = check_plan(plan, trees, points)
        boards += len(verdicts)
        sockets += sum(len(board.get("slots") or {}) for board in plan.values())
        failures += [f"{name} {cid}: {msg}" for cid, verdict in sorted(verdicts.items())
                     if not verdict.ok for msg in verdict.messages()]
        failures += [f"{name}: {msg}" for msg in placements.problems]
    print(f"{len(plans)} plans, {boards} boards, {sockets} socketed cards, {elapsed:.1f} s")
    assert not failures, "\n".join(failures)
    assert boards > len(plans) * len(set(_PLAN_TEAM)), "the plans cover only the team's boards"
    assert sockets > 0, "no socket was filled, so the placement rules checked nothing"

    # The same rules reject this very output once it is broken: a node with no
    # bought neighbour, and a socketed card seated a second time.
    plan = plans["song m0334"]
    leader = "chr-" + _PLAN_TEAM[0].split("-")[1]
    tree = trees[leader]
    bought = {ROOT_NODE, *plan[leader]["nodes"]}
    stray = next(n for n in sorted(tree.nodes)
                 if n not in bought and not tree.adjacency.get(n, frozenset()) & bought)
    stray_plan = {**plan, leader: {**plan[leader], "nodes": [*plan[leader]["nodes"], stray]}}
    assert stray in check_plan(stray_plan, trees, points)[0][leader].disconnected
    seated, board = next((cid, b) for cid, b in sorted(plan.items()) if b.get("slots"))
    card = next(iter(board["slots"].values()))
    other = next(cid for cid in sorted(plan) if cid != seated)
    doubled = {**plan, other: {**plan[other],
                               "slots": {**(plan[other].get("slots") or {}), ROOT_NODE: card}}}
    assert card in dict(check_plan(doubled, trees, points)[1].reused)
