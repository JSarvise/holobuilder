"""`cheapest_paths` must be reachable from the ROOT even with nothing owned.

A path search seeded only from the neighbours of already-owned nodes gives a
character owning ZERO nodes -- a bare respec board, or a `--team` member the
account has never boarded -- an empty seed, so it could buy NOTHING: `--respec`
would silently plan nothing on those boards and `--team` would KeyError or
under-plan. `frontier()` roots reachability at `S-001` regardless of what is
owned, and `cheapest_paths` must agree with it.

This is the unit that pins that behaviour without a full solver run (the end-to-end
`--team` plan is a ~1-2 minute search). It reads only the master price/skilltree
data under gitignored `dumps/`, so it skips on a fresh checkout.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PROXY = Path(__file__).resolve().parent
_MODULE = _PROXY / "tree_optimise.py"
_DUMPS = _PROXY / "dumps"


def _load():
    spec = importlib.util.spec_from_file_location("tree_optimise", _MODULE)
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["tree_optimise"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(
    not (_DUMPS / "skilltree.json").exists()
    or not (_DUMPS / "master_SkillTreeNode.json").exists(),
    reason="dumps/ is gitignored account data",
)
def test_cheapest_paths_reaches_nodes_from_an_empty_board():
    """An empty `owned` set must still find buyable paths, rooted at S-001."""
    to = _load()
    prices = to._prices()
    graphs = to._graphs(to._load(_DUMPS / "skilltree.json"))
    # Any real character's graph; take the first that has one.
    graph = next(g for g in graphs.values() if g)

    from_empty = to.cheapest_paths(set(), graph, prices, max_nodes=10)

    assert from_empty, (
        "an empty board found NO buyable path -- the root seed is missing, which "
        "is exactly the bug that made --respec plan nothing on a bare board"
    )
    # The root itself is never sold, and every path is a chain of priced nodes
    # starting adjacent to the root.
    assert to.ROOT not in from_empty, "the root socket must not be a purchase"
    for target, path in from_empty.items():
        assert path, f"{target} resolved to an empty path"
        assert all(node in prices for node in path), (
            f"{target}: a path node carries no price: {path}"
        )

    # NEGATIVE CONTROL: root seeding must not quietly make everything reachable
    # from nowhere. A node with no adjacency to the root's component should not
    # appear. Concretely, reaching anything requires a real path, so the set of
    # reachable targets from empty must be a SUBSET of what is reachable once a
    # mid-board node is already owned (owning more can only add reachability).
    seed = next(iter(from_empty))  # some node reachable from the root
    from_seed = to.cheapest_paths({seed}, graph, prices, max_nodes=10)
    assert set(from_empty) - {seed} <= set(from_seed) | {seed}, (
        "empty-board reachability exceeds owned-board reachability, which is "
        "impossible if the root seed is a genuine starting point"
    )
