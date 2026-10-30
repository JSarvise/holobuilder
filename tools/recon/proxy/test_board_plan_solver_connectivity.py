"""The exact plan's board connectivity without a flow.

`board_plan_solver._connectivity` expresses connectivity with bridge precedences and minimal
vertex separators instead of a single-commodity flow. It must admit EXACTLY the node sets a flow
admits -- the rooted connected ones -- or the plan's optimum moves. Checked exhaustively on a small graph with cycles
and bridges, and on random node sets of a real board graph.
"""
from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for _p in (HERE, REPO / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import board_plan_solver as bps  # noqa: E402
import tree_optimise as to  # noqa: E402


def _connected(g: dict, chosen: set) -> bool:
    if to.ROOT not in chosen:
        return False
    seen, stack = {to.ROOT}, [to.ROOT]
    while stack:
        a = stack.pop()
        for b in g.get(a, ()):
            if b in chosen and b not in seen:
                seen.add(b)
                stack.append(b)
    return seen == chosen


def _admitted(g: dict, nodes: list, chosen: set) -> bool:
    unreachable, precedences, separators = bps._connectivity(g, nodes)
    if to.ROOT not in chosen or any(n in chosen for n in unreachable):
        return False
    if any(w in chosen and u not in chosen for w, u in precedences):
        return False
    return all(v not in chosen or any(t in chosen for t in sep) for v, sep in separators)


def _undirected(edges) -> dict:
    g: dict = {}
    for a, b in edges:
        g.setdefault(a, []).append(b)
        g.setdefault(b, []).append(a)
    return g


def test_every_subset_of_a_graph_with_cycles_and_bridges():
    r = to.ROOT
    # a 4-cycle at the root, a bridge to a triangle, a bridge to a 5-cycle with a chord, a
    # pendant chain, and one node unreachable from the root
    g = _undirected([(r, "a"), ("a", "b"), ("b", "c"), ("c", r),
                     ("b", "t1"), ("t1", "t2"), ("t2", "t3"), ("t3", "t1"),
                     ("t2", "p1"), ("p1", "p2"), ("p2", "p3"), ("p3", "p4"), ("p4", "p5"),
                     ("p5", "p1"), ("p2", "p4"),
                     ("c", "d1"), ("d1", "d2")])
    g["z"] = []
    nodes = sorted(g)
    others = [n for n in nodes if n != r]
    for k in range(len(others) + 1):
        for combo in itertools.combinations(others, k):
            chosen = {r, *combo}
            assert _admitted(g, nodes, chosen) == _connected(g, chosen), sorted(chosen)


def test_random_node_sets_of_a_real_board():
    skilltree_path = HERE / "dumps" / "skilltree.json"
    if not skilltree_path.exists():
        pytest.skip("dumps/ not on this checkout")
    import json

    graphs = to._graphs(json.loads(skilltree_path.read_text(encoding="utf-8")))
    cid, g = sorted(graphs.items())[0]
    nodes = list(g.keys())
    rng = random.Random(20261029)
    checked = connected = 0
    for _ in range(3000):
        # grow a connected set, then perturb it (drop or add a few nodes)
        chosen = {to.ROOT}
        for _step in range(rng.randrange(1, len(nodes))):
            frontier = sorted({b for a in chosen for b in g.get(a, ()) if b not in chosen})
            if not frontier:
                break
            chosen.add(rng.choice(frontier))
        for _flip in range(rng.randrange(0, 4)):
            n = rng.choice(nodes)
            if n != to.ROOT:
                chosen ^= {n}
        ok = _connected(g, chosen)
        assert _admitted(g, nodes, chosen) == ok, (cid, sorted(chosen))
        checked += 1
        connected += ok
    assert checked == 3000 and 0 < connected < checked      # both kinds of sets were tested
