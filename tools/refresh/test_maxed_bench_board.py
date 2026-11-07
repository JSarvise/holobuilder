"""The maxed account's bench boards (`precompute_optimisations.maxed_account_board_state`).

A max-level holomem holds fewer points than her whole tree costs. Off the team only her
greens, yellows and Connect sockets reach a team (`bench_reach_nodes`); the bench board
buys those by their cheapest paths from the centre, within her points, and the board it
gives must be one the game allows: rooted at the centre and connected on the game's
adjacency. Real dumps; skipped without them.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _p in (HERE, REPO / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hbuilder import paths  # noqa: E402
from hbuilder.domain.board import BoardError, parse_effect_id  # noqa: E402
from hbuilder.domain.board_plan import node_costs_from_master  # noqa: E402
from hbuilder.errors import MissingInput  # noqa: E402
from hbuilder.inputs import max_level_board_points  # noqa: E402


def _inputs():
    try:
        skilltree = json.loads((paths.DUMPS / "skilltree.json").read_text(encoding="utf-8"))
        node_master = json.loads((paths.DUMPS / "master_SkillTreeNode.json").read_text(encoding="utf-8"))
        points = max_level_board_points()
    except (OSError, ValueError, MissingInput) as exc:
        pytest.skip(f"the real inputs are not available here: {exc!r}")
    import precompute_optimisations as pc
    return pc, skilltree, node_master, points


def _connected(graph: dict, nodes: set, root: str) -> bool:
    if root not in nodes:
        return False
    seen, stack = {root}, [root]
    while stack:
        a = stack.pop()
        for b in graph.get(a, ()):
            if b in nodes and b not in seen:
                seen.add(b)
                stack.append(b)
    return seen == nodes


def _check(pc, skilltree, node_master, state, points, *, every_target: bool):
    import tree_optimise as to

    costs = node_costs_from_master(node_master)
    models = skilltree["models"]
    assert set(state) == {m["chr"] for m in skilltree["members"] if m.get("model") in models}
    for member in skilltree["members"]:
        chr_id = member["chr"]
        nodes = set(state[chr_id]["nodes"])
        assert state[chr_id]["slots"] == {}
        assert nodes <= set(models[member["model"]]), chr_id
        assert sum(costs.get(n, 0) for n in nodes) <= points, chr_id
        assert _connected(to.adjacency(models[member["model"]]), nodes, to.ROOT), chr_id
        targets = set(pc.bench_reach_nodes(skilltree, node_master, chr_id))
        if every_target:
            assert targets <= nodes, (chr_id, sorted(targets - nodes))
        else:
            assert nodes & (targets - {to.ROOT}), chr_id      # something bought


def test_at_max_level_every_bench_holomem_buys_all_that_reaches_a_team():
    pc, skilltree, node_master, points = _inputs()
    state = pc.maxed_account_board_state(skilltree, node_master=node_master)
    _check(pc, skilltree, node_master, state, points, every_target=True)
    # the default is the max-level grant
    assert state == pc.maxed_account_board_state(skilltree, points, node_master=node_master)


def test_a_small_budget_buys_the_nearest_targets_and_never_overspends():
    pc, skilltree, node_master, _points = _inputs()
    for budget in (5, 30):
        state = pc.maxed_account_board_state(skilltree, budget, node_master=node_master)
        _check(pc, skilltree, node_master, state, budget, every_target=False)


def test_the_reach_nodes_are_the_scoring_greens_and_yellows_and_the_sockets():
    """Every green or yellow left out carries only effects the scorer ignores; every one
    kept carries one it counts; no red or blue is kept."""
    import build_board

    pc, skilltree, node_master, _points = _inputs()
    rows = build_board._effect_rows_by_group(node_master)
    for member in skilltree["members"][:6]:
        chr_id = member["chr"]
        kept = set(pc.bench_reach_nodes(skilltree, node_master, chr_id))
        for node in skilltree["models"][member["model"]]:
            if node.startswith("S-"):
                assert node in kept
                continue
            if node[0] not in "GY":
                assert node not in kept, (chr_id, node)
                continue
            row, _why = build_board._select_row(rows.get(node) or [], chr_id)
            kinds = set()
            for effect_id in (row[build_board.ROW_DATA_KEY].get("6") or []) if row else []:
                try:
                    kinds.add(parse_effect_id(effect_id, branch=node[0]).kind)
                except BoardError:
                    kinds.add("unknown")
            assert (node in kept) == bool(kinds - {"ignored"}), (chr_id, node, kinds)
